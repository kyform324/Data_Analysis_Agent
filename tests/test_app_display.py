"""界面展示层的回归测试。

背景：左侧结果表格现在由"本次所有成功查询"驱动——
默认展示最近一条，多条时出现下拉框可切换，行数提示跟着选中项走。
本文件验证这套展示逻辑，不启动 Gradio 服务器、也不调用真实的模型 API。

覆盖：
  1. 多条结果：默认展示最后一条、下拉可见、选项数正确、默认选中最后一条
  2. 单条结果：下拉隐藏，表格照常展示
  3. 无任何成功查询：显示明确空态，不残留上一题表格，也不从回答里抽表
  4. 下拉切换：表格与行数提示同时变化（同源）
  5. 超过 500 行：提示同时写"Agent 仅分析前 25 行"和"界面只展示前 500 行"
  6. 切换到不存在的 id：返回空态而不是崩溃
  7. build() 能构造出界面对象

做法：把 app.run_agent 换成一个假的——它只执行预先写好的 SQL（走真实的
query_sql 逻辑），然后返回一个固定结构的 dict。这样测的是展示层，不烧 API。

运行：python tests/test_app_display.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                    # 根目录的 app.py
sys.path.insert(0, str(ROOT / "agent_software"))  # main / tools / envelope

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")      # 先加载配置，否则 main.py 里的 OpenAI 客户端会因缺 key 报错

import app  # noqa: E402
from tools import query_sql as qs  # noqa: E402

fails = []


def check(tag, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {tag}  {detail}")
    if not cond:
        fails.append(tag)


def make_agent(sqls, answer="答案", steps=3):
    """假 agent：执行给定 SQL 后返回固定结构，替代真实模型调用。"""
    def _run(question):
        for sql in sqls:
            qs.query_sql_func(sql)
        return {"ok": True, "answer": answer, "steps": steps, "tokens": 1000,
                "tool_trace": [], "error": None}
    return _run


def rows_in(html_text):
    n = html_text.count("<tr>")
    return n - 1 if n else 0          # 减去表头；没有表格时是 0


SMALL = "SELECT * FROM categories"                                    # 3 行
MID = "SELECT * FROM order_status_history"                            # 352 行
TINY = "SELECT order_status, COUNT(*) FROM orders GROUP BY order_status"  # 5 行
HUGE = "SELECT * FROM inventory_daily"                                # 661 行

# ── 1. 三条成功查询 → 默认展示最后一条，下拉可见 ──
app.run_agent = make_agent([SMALL, MID, TINY])
left, text, trace, meta, selector, trunc = app.ask("测试", "干净库")
check("ask 返回 6 个值", True)
check("表格用最后一条结果", rows_in(left) == 5, f"表格行数={rows_in(left)} (期望 5)")
check("下拉可见（多条结果）", selector.get("visible") is True, str(selector.get("visible")))
check("下拉有 3 个选项", len(selector.get("choices") or []) == 3,
      str([c[0][:24] for c in (selector.get("choices") or [])]))
check("下拉默认选中最后一条", selector.get("value") == 3, f"value={selector.get('value')}")
check("行数提示跟着最后一条", "共 5 行" in trunc, trunc[:80])
check("底部 meta 不含行数提示", "行" not in meta.replace("步", ""), meta[:80])

# ── 2. 只有一条成功查询 → 下拉隐藏 ──
app.run_agent = make_agent([SMALL])
left2, _, _, _, sel2, _ = app.ask("测试", "干净库")
check("单条结果时下拉隐藏", sel2.get("visible") is False, str(sel2.get("visible")))
check("单条结果仍显示表格", rows_in(left2) == 3, f"表格行数={rows_in(left2)}")

# ── 3. 没有任何成功查询 → 明确空态，不残留上一题 ──
app.run_agent = make_agent([])
left3, _, _, _, sel3, _ = app.ask("测试", "干净库")
check("无结果时给空态文案", "本次没有可展示的成功查询结果" in left3, left3[:60])
check("无结果时下拉隐藏且清空选项",
      sel3.get("visible") is False and not sel3.get("choices"), str(sel3.get("choices")))
check("无结果时不残留上一题表格", rows_in(left3) == 0)

# ── 4. 下拉切换 → 表格与提示同时变 ──
app.run_agent = make_agent([SMALL, MID, TINY])
app.ask("测试", "干净库")
t1, hint1 = app.switch_result(1)
check("切到查询 1 表格变成 3 行", rows_in(t1) == 3, f"表格行数={rows_in(t1)}")
check("切到查询 1 提示同步", "共 3 行" in hint1, hint1[:80])
t2, hint2 = app.switch_result(2)
check("切到查询 2（352 行）表格全展示", rows_in(t2) == 352, f"表格行数={rows_in(t2)}")
check("切到查询 2 提示写 352 行", "共 352 行" in hint2, hint2[:80])

# ── 5. 超大结果的行数提示 ──
app.run_agent = make_agent([HUGE])
left5, _, _, _, _, trunc5 = app.ask("测试", "干净库")
check("661 行：界面只展示 500 行", "界面只展示前 500 行" in trunc5, trunc5[:120])
check("661 行：Agent 仅分析前 25 行", "仅分析前 25 行" in trunc5, trunc5[:120])
check("661 行：表格渲染 500 行", rows_in(left5) == 500, f"表格行数={rows_in(left5)}")

# ── 6. 切换不存在的 id → 空态而非崩溃 ──
t_none, hint_none = app.switch_result(999)
check("切换到不存在的 id 返回空态", "本次没有可展示的成功查询结果" in t_none)
check("不存在 id 时提示为空", hint_none == "")

# ── 7. 默认展示按 role 选：display → analysis → 最后一次 ──
rs_mixed = [
    {"result_id": 1, "role": "analysis", "total": 5, "sql": "SELECT 1"},
    {"result_id": 2, "role": "display", "total": 248, "sql": "SELECT 2"},
    {"result_id": 3, "role": "verification", "total": 8, "sql": "SELECT 3"},
]
check("优先选最后一个 display", app._pick_default(rs_mixed)["result_id"] == 2)
rs_no_display = [
    {"result_id": 1, "role": "verification", "total": 3, "sql": "SELECT 1"},
    {"result_id": 2, "role": "analysis", "total": 7, "sql": "SELECT 2"},
]
check("没有 display 时退到最后一个 analysis",
      app._pick_default(rs_no_display)["result_id"] == 2)
rs_only_verification = [
    {"result_id": 1, "role": "verification", "total": 3, "sql": "SELECT 1"},
    {"result_id": 2, "role": "verification", "total": 9, "sql": "SELECT 2"},
]
check("全无 display/analysis 时退回最后一条",
      app._pick_default(rs_only_verification)["result_id"] == 2)
check("下拉标签含 role", "display" in app._selector_choices(rs_mixed)[1][0],
      app._selector_choices(rs_mixed)[1][0][:40])

# ── 8. build() 能构造出界面（不 launch） ──
demo = app.build()
check("build() 成功构造界面", demo is not None, type(demo).__name__)

print()
print("失败项:", fails if fails else "无")
sys.exit(1 if fails else 0)
