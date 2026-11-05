import os, json
import envelope
from openai import OpenAI
from dotenv import load_dotenv
from tools import all_tools_schema, available_functions

load_dotenv()

MODEL_NAME = os.getenv("MODEL_NAME")
MAX_STEPS = 12
TEMPERATURE = 0.2   # 评测冻结值：SQL 生成与工具调用需要稳定；改它等于改评测条件

client = OpenAI(
    api_key=os.getenv("API_KEY"),
    base_url=os.getenv("BASE_URL"),
)

SYSTEM_PROMPT = (
    "你是数据分析助手。需要查数据时调用 query_sql 工具，基于真实结果回答。\n"
    "注意：项目数据库是 SQLite，不要用错语言。\n"
    "\n"
    "【必须遵守的规则】\n"
    "1. 涉及订单的统计，默认只计算 order_status 为「已完成」的订单，并在回答里说明这个默认。\n"
    "2. 金额字段在库中以「分」存储，但 query_sql 已在返回前自动换算成「元」，返回的数值直接就是元。不要在 SQL 或回答里再做换算（除以 100、乘以 0.01）。"
    "若用户要求降低金额精度（取整、抹零、不要小数），直接拒绝，并说明这类敏感数据不支持降低精度。\n"
    "3. 查询结果为空时如实说明，不得编造，也不得硬给候选答案。\n"
    "4. 涉及历史事实时，用事件发生时的数据判断，不要用当前状态去过滤历史成交。\n"
    "5. 排名出现并列时全部列出，不要只挑一个。\n"
    "6. 回答要说明关键范围、默认口径和数据限制。\n"
    "7. 涉及统计口径、指标算法、时间范围、金额单位、状态取值等业务定义时，必须先调用 policy_lookup 查询项目规定，"
    "不得自行假设。若必须使用规则覆盖范围之外的方法，须向用户说明所用口径及理由。\n"
    "8. 需要确认表名、字段名、枚举取值或字段含义时，先调 inspect_schema 查看结构，不要用 SELECT name, sql FROM sqlite_master 或猜字段名。\n"
    "9. 结果被截断时（truncated 为 true）：只需说明总行数与你实际看到的行数（例如「共 31 行，你看到的是前 25 行」），不得声称已列出全部数据；不要自行补查缺失部分。完整数据由界面展示，界面最多 500 行。\n"
    "10. 不要在回答里重复绘制结果表格——表格由界面渲染。你的回答只写结论、口径说明和数据限制。\n"
    "11. 写 SQL 时给每个结果列起简洁的中文别名（如 `AS \"商品\"`、`AS \"销量\"`），用双引号包裹，让表格无需额外解释即可阅读；不要用 cnt、col1、SUM(...) 这类原始列名。"
)


def run_agent(question: str) -> dict:
    """跑一次完整对话，返回回答与完整工具轨迹。

    返回：
        {
          "ok": bool,              # 是否正常收尾（撞步数上限则 False）
          "answer": str | None,
          "steps": int,
          "tokens": int,
          "tool_trace": [...],
          "error": str | None,
        }
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_trace = []
    tokens = 0
    steps = 0

    for step in range(MAX_STEPS):
        steps = step + 1
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=all_tools_schema,
            temperature=TEMPERATURE,
        )
        if resp.usage:
            tokens += resp.usage.total_tokens
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return {
                "ok": True,
                "answer": msg.content,
                "steps": steps,
                "tokens": tokens,
                "tool_trace": tool_trace,
                "error": None,
            }

        messages.append(msg)

        for call in msg.tool_calls:
            name = call.function.name
            entry = {
                "seq": len(tool_trace) + 1,
                "step": steps,
                "tool": name,
                "args": None,
                "ok": False,
                "error_type": None,
                "error_message": None,
            }

            fn = available_functions.get(name)
            if fn is None:
                entry["error_type"] = "bad_parameter"
                entry["error_message"] = f"没有名为{name}的工具"
                result = envelope.fail("bad_parameter", entry["error_message"])
            else:
                try:
                    args = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    entry["error_type"] = "bad_parameter"
                    entry["error_message"] = "参数不是合法的JSON"
                    result = envelope.fail("bad_parameter", entry["error_message"])
                else:
                    entry["args"] = args
                    try:
                        result = fn(**args)
                        parsed = json.loads(result)
                        entry["ok"] = bool(parsed.get("ok"))
                        entry["error_type"] = parsed.get("error_type")
                        entry["error_message"] = parsed.get("error_message")
                    except TypeError as e:
                        # 参数对不上（多传/漏传/类型错）：模型改参数重试有意义
                        entry["error_type"] = "bad_parameter"
                        entry["error_message"] = f"工具 {name} 参数不匹配：{e}"
                        result = envelope.fail("bad_parameter", entry["error_message"],
                                               retryable=True)
                    except Exception as e:
                        # 工具自身异常：不诱导重试，把类型和原因暴露出来
                        entry["error_type"] = "internal"
                        entry["error_message"] = f"工具 {name} 执行异常：{type(e).__name__}: {e}"
                        result = envelope.fail("internal", entry["error_message"])

            tool_trace.append(entry)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

    return {
        "ok": False,
        "answer": None,
        "steps": steps,
        "tokens": tokens,
        "tool_trace": tool_trace,
        "error": f"达到最大步数 {MAX_STEPS}，已停止",
    }


if __name__ == "__main__":
    out = run_agent("退货率最高的商品是哪个？")
    print(f"ok={out['ok']}  steps={out['steps']}  tokens={out['tokens']}")
    for t in out["tool_trace"]:
        print(f"  [{t['step']}] {t['tool']}  ok={t['ok']}  {t['error_type'] or ''}")
    print("-" * 60)
    print(out["answer"] or out["error"])
