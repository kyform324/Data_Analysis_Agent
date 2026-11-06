"""金额列定位与换算的回归测试（针对 CTE 错位事故）。

事故：`WITH spend AS (SELECT ... SUM(unit_price_cents*quantity) AS amt ...) SELECT ... r.amt ...`
旧实现用正则取「第一个 SELECT ... FROM」，拿到的是内层 CTE 的投影，列位置与外层不符，
金额没被换算 → 6,783,800 分被当元输出 → 模型又自行换算成"678.38 万元"。

原则：映射不上就不换算（宁可不换，也不按位置猜）。

运行：python tests/test_money_columns.py
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


def cols(d):
    return d["data"]["columns"]


def rows(d):
    return d["data"]["rows"]


# 事故现场的原始 SQL（常规-04#3）
ACCIDENT = """
WITH spend AS (
  SELECT o.customer_id AS cid, SUM(oi.unit_price_cents * oi.quantity) AS amt
  FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
  WHERE o.order_status = '已完成'
  GROUP BY o.customer_id
), ranked AS (
  SELECT cid, amt, RANK() OVER (ORDER BY amt DESC) AS rk FROM spend
)
SELECT c.customer_name AS "客户", c.province AS "省份", r.amt AS "累计消费额", r.rk AS "排名"
FROM ranked r JOIN customers c ON c.customer_id = r.cid
WHERE r.rk <= 3
ORDER BY r.rk, c.customer_name
"""

# 1. 事故 SQL：金额必须换算成元
d = run(ACCIDENT)
vals = [r[2] for r in rows(d)]
check("① 事故 SQL 金额为 67838/40164/29416",
      vals == [67838.0, 40164.0, 29416.0], str(vals))
check("① 排名列未被除以 100", [r[3] for r in rows(d)] == [1, 2, 3],
      str([r[3] for r in rows(d)]))

# 2. 外层换序：金额位置变了也不能错位
REORDER = ACCIDENT.replace(
    'SELECT c.customer_name AS "客户", c.province AS "省份", r.amt AS "累计消费额", r.rk AS "排名"',
    'SELECT r.rk AS "排名", r.amt AS "累计消费额", c.customer_name AS "客户"')
d = run(REORDER)
check("② 换序后排名仍为 1", rows(d)[0][0] == 1, str(rows(d)[0]))
check("② 换序后金额仍为 67838", rows(d)[0][1] == 67838.0, str(rows(d)[0]))

# 3. 单 CTE：金额在内层第 2 列，外层引用别名
import sqlite3  # noqa: E402

conn = sqlite3.connect("file:" + qs._db + "?mode=ro", uri=True)
max_cents = conn.execute(
    "SELECT MAX(s) FROM (SELECT SUM(unit_price_cents * quantity) AS s"
    " FROM order_items GROUP BY product_id)").fetchone()[0]
conn.close()

d = run("""
WITH t AS (SELECT product_id AS pid, SUM(unit_price_cents * quantity) AS amt
           FROM order_items GROUP BY product_id)
SELECT pid, amt FROM t ORDER BY amt DESC LIMIT 1
""")
check("③ 单 CTE 的金额被换算为元",
      rows(d)[0][1] == round(max_cents / 100, 2),
      f"amt={rows(d)[0][1]} 单品原始分值={max_cents}")

# 4. 两个 CTE，第一个含金额表达式（同上事故 SQL），已在 ① 覆盖
check("④ 两个 CTE 的场景由 ① 覆盖", True)

# 5. SELECT 投影里带标量子查询
d = run("""
SELECT c.customer_name AS "客户",
       (SELECT SUM(oi.unit_price_cents * oi.quantity) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.customer_id = c.customer_id AND o.order_status = '已完成') AS "金额"
FROM customers c ORDER BY "金额" DESC LIMIT 1
""")
check("⑤ 标量子查询的金额被换算", rows(d)[0][1] == 67838.0, str(rows(d)[0]))

# 6. 无 CTE 的普通金额查询（原功能不回归）
d = run('SELECT product_name AS "商品", price_cents AS "价格" FROM products '
        "ORDER BY price_cents DESC LIMIT 1")
check("⑥ 无 CTE 时金额仍换算", rows(d)[0][1] == 3999.0, str(rows(d)[0]))

# 7. 金额只出现在 WHERE：输出是计数，不能换算
d = run("SELECT COUNT(*) AS cnt FROM order_items WHERE unit_price_cents > 100")
check("⑦ 金额只出现在 WHERE 时不换算", rows(d)[0][0] > 1, str(rows(d)[0]))

# 8. SELECT * 无法建立位置映射：不能静默除以 100
d = run("SELECT * FROM (SELECT SUM(unit_price_cents * quantity) AS x FROM order_items)")
check("⑧ SELECT * 时不静默除 100", rows(d)[0][0] > 50000, str(rows(d)[0]))

# 9. 列名以 _cents 结尾：改名为 _yuan 且除以 100
d = run("SELECT price_cents FROM products ORDER BY price_cents DESC LIMIT 1")
check("⑨ _cents 列改名为 _yuan", cols(d) == ["price_yuan"], str(cols(d)))
check("⑨ _cents 列数值已换算", rows(d)[0][0] == 3999.0, str(rows(d)[0]))

# 10. 报错查询不写入前端结果缓存
qs.clear_query_results()
before = len(qs.get_query_results())
bad = run("SELECT * FROM 不存在的表")
check("⑩ 报错查询不写入缓存",
      bad["ok"] is False and len(qs.get_query_results()) == before,
      f"缓存 {before} → {len(qs.get_query_results())}")

print()
print("失败项:", fails if fails else "无")
sys.exit(1 if fails else 0)
