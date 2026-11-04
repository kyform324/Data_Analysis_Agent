import sqlite3
import time
import json
import envelope
import re
from pathlib import Path

query_sql_schema = {
    "type":"function",
    "function":{
        "name":"query_sql",
        "description":"对数据库执行一条只读查询语句，返回列名与行数据。金额列（以分为单位储存）在返回前会自动换算成元，返回的数值直接就是元；不要在SQL里对金额列做单位换算",
        "parameters":{
            "type":"object",
            "properties":{
                "sql":{
                    "type":"string",
                    "description":"一条完整的SELECT或WITH查询语句"
                }
            },
            "required":["sql"]
        }

    }
}

SQL_TIMEOUT = 5
def _load_money_columns() -> set:
    """金额列从语义文件推导（money == true），不再硬编码。

    读不到语义文件时回退到原来的硬编码集合。
    """
    fallback = {"price_cents", "unit_price_cents"}
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "schema_semantics.json"
        sem = json.loads(p.read_text(encoding="utf-8"))
        cols = {name for t in sem.get("tables", {}).values()
                for name, meta in (t.get("columns") or {}).items()
                if meta.get("money")}
        return cols or fallback
    except Exception:
        return fallback


_MONEY_COLUMNS = _load_money_columns()

# Agent 每次能看到的最大行数（上下文保护）
_AGENT_LIMIT = 25
# 前端最多能展示的行数
_DISPLAY_LIMIT = 500

# 最近一次成功查询的完整结果，供前端展示（不进入 Agent 上下文）
_last_result = {}


def get_last_result():
    """返回最近一次成功查询的 {columns, rows, total, truncated, display_truncated, preview_count, sql}。"""
    return dict(_last_result)


def clear_last_result():
    """清空缓存（开新题、查询失败、空结果时调用）。"""
    _last_result.clear()
_MONEY_FUNCS = {"sum","avg","max","min","total"}
_MONEY_CONVERT = re.compile(r"/\s*100(\.0+)?\b|\*\s*0\.01\b")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_DB_PATHS = {
    "clean": (_PROJECT_ROOT / "data" / "demo_v2_eval.sqlite3").as_posix(),
    "dirty": (_PROJECT_ROOT / "data" / "demo_v2_eval_dirty.sqlite3").as_posix(),
}
_db = _DB_PATHS["clean"]


def set_db(name="clean"):
    """切换本次查询使用的库。模型看不到这个函数——它不在 tools schema 里。"""
    global _db
    if name not in _DB_PATHS:
        raise ValueError(f"未知库名 {name!r}，可选：{list(_DB_PATHS)}")
    _db = _DB_PATHS[name]
    return _db


def query_sql_func(sql: str):
    new_sql = sql.strip()
    while True:
        new_sql = new_sql.strip()

        if new_sql.startswith("--"):
            find_end = new_sql.find("\n")
            if find_end == -1:
                return envelope.fail("bad_parameter","SQL里没有任何语句")
            new_sql = new_sql[find_end + 1:]
            continue

        if new_sql.startswith("/*"):
            find_end = new_sql.find("*/",2)
            if find_end == -1:
                return envelope.fail("bad_parameter","注释没有闭合")
            new_sql = new_sql[find_end + 2:]
            continue
        break

    if not new_sql.lower().startswith(("select","with")):
        return envelope.fail("forbidden","只允许SELECT或WITH查询")

    low_sql = sql.lower()
    has_money_col = False
    for col in _MONEY_COLUMNS:
        if col in low_sql:
            has_money_col = True
            break
    has_convert = _MONEY_CONVERT.search(low_sql) is not None

    if has_money_col and has_convert:
        return envelope.fail(
            "forbidden",
            "金额列请原样查询，工具会自动把[分]换算成[元]；不要自己写/100或*0.01",
            retryable=True
        )

    conn = sqlite3.connect("file:"+ _db + "?mode=ro", uri=True)
    cursor = conn.cursor()
    cursor.execute("PRAGMA query_only = on")
    
    try:
        clean = sql.strip().rstrip(";").strip()
        has_order_by = "order by" in clean.lower()

        deadline = time.time()+5
        conn.set_progress_handler(lambda:1 if time.time() > deadline else 0,1000)

        count_sql = f"SELECT COUNT(*) FROM ({clean})"
        total = cursor.execute(count_sql).fetchone()[0]

        # 只拉一次数据：最多取 _DISPLAY_LIMIT 行，其中前 _AGENT_LIMIT 行给 Agent
        cur = cursor.execute(f"SELECT * FROM ({clean}) LIMIT {_DISPLAY_LIMIT}")
        columns = [col[0] for col in cur.description]
        display_rows = cur.fetchall()

        truncated = total > _AGENT_LIMIT
        display_truncated = total > _DISPLAY_LIMIT

        money_cols = {i for i,name in enumerate(columns) if _is_money_expr(name)}
        if len(money_cols) < len(columns):
            money_cols |= set(_money_indexes_from_sql(clean, len(columns)))
        money_cols = sorted(money_cols)
        if money_cols:
            renamed = list(columns)
            for i in money_cols:
                if renamed[i].lower().endswith("_cents"):
                    renamed[i] = renamed[i][:-6] + "_yuan"
            columns = renamed
            new_rows = []
            for row in display_rows:
                new_row = []
                for i,v in enumerate(row):
                    if i in money_cols and isinstance(v,(int,float)):
                        new_row.append(round(v/100,2))
                    else:
                        new_row.append(v)
                new_rows.append(new_row)
            display_rows = new_rows

        # 切前 N 行给 Agent；剩下的留在缓存里给前端
        agent_rows = display_rows[:_AGENT_LIMIT]

        note = None
        if truncated:
            omitted = total - _AGENT_LIMIT
            if has_order_by:
                note = (f"结果共 {total} 行，已按指定顺序排序，当前只返回前 {_AGENT_LIMIT} 行；"
                        f"另有 {omitted} 行未返回，不要声称已列出全部")
            else:
                note = (f"结果共 {total} 行，未排序，当前只返回前 {_AGENT_LIMIT} 行，可能是任意子集；"
                        f"另有 {omitted} 行未返回")

        # 缓存完整结果供前端展示（不进入 Agent 上下文）。
        # 策略：只保留「行数最多」的那次查询——Agent 常在同题里跑多次验证性查询
        # （COUNT / MIN / MAX / 查重复），若按「最后一次」覆盖，前端会展示验证结果而不是主结果。
        if len(display_rows) >= len(_last_result.get("rows") or []):
            _last_result.clear()
            _last_result.update({
                "columns": columns,
                "rows": display_rows,
                "total": total,
                "truncated": truncated,
                "display_truncated": display_truncated,
                "preview_count": len(agent_rows),
                "sql": clean[:300],
            })

        return envelope.ok({
            "columns": columns,
            "rows": agent_rows,
            "row_count": len(agent_rows),
            "total": total,
            "truncated": truncated,
            "has_order_by": has_order_by,
            "note": note,
        })

    except sqlite3.Error as e:
        _last_result.clear()
        if "interrupted" in str(e):
            return envelope.fail("timeout","查询超过5秒已中断，请收窄范围或加LIMIT",retryable=True)
        return envelope.fail("sql_error",str(e))
    finally:
        conn.close()


def _is_money_expr(name:str) -> bool:
    """表达式是否产出金额：含金额列、且不是比较/逻辑运算的结果。

    兼容 `SUM(oi.quantity*oi.unit_price_cents)`、`COALESCE(SUM(...),0)`、`SUM(x) AS amt` 等写法。
    """
    key = re.sub(r"\b\w+\.","",re.sub(r"\s+","",name).lower())
    if not any(c in key for c in _MONEY_COLUMNS):
        return False
    # 比较或逻辑运算的结果是布尔/类别，不是金额
    if re.search(r"[<>=!]",key):
        return False
    return True


def _money_indexes_from_sql(sql:str, ncols:int):
    """按 SELECT 列表位置判定金额列，用于 `AS 别名` 导致列名丢失类型信息的情况。"""
    m = re.search(r"select\s+(.*?)\s+from\b", sql, re.I|re.S)
    if not m:
        return []
    parts, depth, buf = [], 0, ""
    for ch in m.group(1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf); buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [i for i,p in enumerate(parts[:ncols])
            if _is_money_expr(re.split(r"\bas\b", p, maxsplit=1, flags=re.I)[0])]