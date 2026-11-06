"""query_sql 结果缓存的回归测试。

背景：早期策略是"只保留行数最多的那次查询结果"，结果被审计类查询顶掉——
主查询 8 行商品销量，一次历史状态核对返回 116 行，界面就显示成审计表。
现在改成"每次成功查询都追加进列表，由界面选择展示哪一条"，本文件守住这个行为。

覆盖：
  1. 多次成功查询全部保留，result_id 从 1 递增
  2. 更宽的后续查询不会覆盖前面的结果（旧 bug 的回归点）
  3. SQL 报错不追加、也不清空已成功的结果
  4. 0 行的成功查询同样保留（"确实没有"本身是结果）
  5. 超过 500 行：total 真实、界面 500 行、Agent 25 行、display_truncated 标记
  6. get_result_by_id 命中/未命中
  7. 金额列仍自动换算（分 → 元，列名 _cents → _yuan）
  8. clear 之后 result_id 从 1 重计
  9. 超过 MAX_CACHED_RESULTS 时丢最旧、保留最新

不依赖网络和 API：只读本地 SQLite（data/demo_v2_eval.sqlite3）。
运行：python tests/test_query_cache.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent_software"))

from tools import query_sql as qs  # noqa: E402

fails = []


def check(tag, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {tag}  {detail}")
    if not cond:
        fails.append(tag)


def run(sql):
    return json.loads(qs.query_sql_func(sql))


def snapshot():
    rs = qs.get_query_results()
    return (len(rs), [r["result_id"] for r in rs],
            [len(r["rows"]) for r in rs], [r["total"] for r in rs])


qs.clear_query_results()

# ── 1. 三次查询：3 行 → 352 行 → 5 行，行数多少都不覆盖 ──
run("SELECT * FROM categories")
run("SELECT * FROM order_status_history")
run("SELECT order_status, COUNT(*) AS cnt FROM orders GROUP BY order_status")
n, ids, rows, totals = snapshot()
check("三次查询全部保留", n == 3, f"条数={n} ids={ids} rows={rows}")
check("result_id 递增从 1 开始", ids == [1, 2, 3], f"ids={ids}")
check("主查询未被更宽的查询覆盖", rows[0] == 3 and totals[0] == 3, f"第一条 rows={rows[0]}")

# ── 2. SQL 报错：不追加、也不清空已有结果 ──
bad = run("SELECT * FROM 不存在的表")
n2, _, _, _ = snapshot()
check("报错返回 sql_error",
      bad["ok"] is False and bad["error_type"] == "sql_error", bad["error_message"][:40])
check("报错后已有结果未被清空", n2 == 3, f"条数={n2}")

# ── 3. 0 行的成功查询：保留，且记作 0 行而不是失败 ──
zero = run("SELECT * FROM categories WHERE 1=0")
n3, _, rows3, _ = snapshot()
check("0 行查询返回 ok", zero["ok"] is True, f"total={zero['data']['total']}")
check("0 行成功查询被保留", n3 == 4 and rows3[-1] == 0, f"条数={n3} 末条 rows={rows3[-1]}")

# ── 4. 超过 500 行：双通道各自的上限 ──
big = run("SELECT * FROM inventory_daily")
d = big["data"]
n4, _, rows4, _ = snapshot()
check("超 500 行 total 正确", d["total"] > 500, f"total={d['total']}")
check("超 500 行 display_truncated=True",
      qs.get_query_results()[-1]["display_truncated"] is True)
check("界面保留 500 行", rows4[-1] == 500, f"rows={rows4[-1]}")
check("Agent 只拿到 25 行", d["row_count"] == 25, f"row_count={d['row_count']}")

# ── 5. 按 id 取结果 ──
got = qs.get_result_by_id(2)
check("get_result_by_id 命中", got is not None and got["result_id"] == 2)
check("get_result_by_id 未命中返回 None", qs.get_result_by_id(999) is None)

# ── 6. 金额列仍然换算 ──
m = run("SELECT product_name, price_cents FROM products")
check("金额列改名为 _yuan", m["data"]["columns"][-1] == "price_yuan",
      str(m["data"]["columns"]))
check("金额数值已换算成元", m["data"]["rows"][0][-1] == 3999.0, str(m["data"]["rows"][0]))

# ── 7. 清空：条数归零、id 从 1 重计 ──
qs.clear_query_results()
check("清空后条数为 0", len(qs.get_query_results()) == 0)
run("SELECT * FROM categories")
check("清空后 result_id 从 1 重计", qs.get_query_results()[0]["result_id"] == 1)

# ── 8. 内存保险丝：超过上限丢最旧 ──
qs.clear_query_results()
for _ in range(qs.MAX_CACHED_RESULTS + 5):
    run("SELECT * FROM categories")
n8, ids8, _, _ = snapshot()
check("条数被裁到上限", n8 == qs.MAX_CACHED_RESULTS, f"条数={n8}")
check("丢最旧、保留最新", ids8[0] == 6 and ids8[-1] == qs.MAX_CACHED_RESULTS + 5,
      f"首={ids8[0]} 尾={ids8[-1]}")

# ── 9. role 参数：默认 analysis、显式取值被记录、非法值被拒 ──
qs.clear_query_results()
run("SELECT * FROM categories")                                  # 不传 role
qs.query_sql_func("SELECT * FROM categories", role="display")     # 显式声明
rs9 = qs.get_query_results()
check("未传 role 记为 analysis", rs9[0]["role"] == "analysis", str(rs9[0]["role"]))
check("传 role=display 被记录", rs9[1]["role"] == "display", str(rs9[1]["role"]))
bad_role = json.loads(qs.query_sql_func("SELECT 1", role="nonsense"))
check("非法 role 返回 bad_parameter",
      bad_role["ok"] is False and bad_role["error_type"] == "bad_parameter",
      str(bad_role.get("error_message"))[:60])

print()
print("失败项:", fails if fails else "无")
sys.exit(1 if fails else 0)
