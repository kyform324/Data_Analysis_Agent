"""表结构查询工具。

用法：
- 不传 table：返回所有表的**紧凑字段清单**（字段名 + 类型 + 主键标记），
  一次就能看到全库结构，够写 SQL 用；不放说明、枚举、单位，避免上下文过长。
- 传 table：返回该表的完整信息，含字段含义、枚举取值、外键与唯一约束。

设计约定：
- 真实字段结构永远来自 PRAGMA，语义文件只是注释层；语义过期只会少信息。
- `money` 标记只供 query_sql 内部识别金额列，**不返回给模型**——
  模型看到「以分存储」会自己做二次换算，而工具返回的数值其实已经是元。
"""
import json
import sqlite3
from pathlib import Path

try:  # 包内导入（正常路径）
    from . import query_sql as _qs
    import envelope as _env
except ImportError:  # 直接以 tools/ 为 sys.path 根时
    import query_sql as _qs
    import envelope as _env


inspect_schema_schema = {
    "type": "function",
    "function": {
        "name": "inspect_schema",
        "description": (
            "查看数据库结构。不传 table 时返回所有表及其字段清单（字段名、类型、主键），"
            "一次即可了解全库；传入 table 时返回该表完整说明，含字段含义、枚举取值、外键与唯一约束。"
            "写查询之前先看结构，不要猜字段名或枚举值。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "要查看的表名；省略则列出所有表及其字段",
                }
            },
            "required": [],
        },
    },
}

_SEMANTICS_PATH = Path(__file__).resolve().parent.parent / "config" / "schema_semantics.json"


def load_semantics():
    """返回 (语义字典, 错误说明)。加载失败时错误说明不为 None，便于把问题暴露给调用方。"""
    try:
        return json.loads(_SEMANTICS_PATH.read_text(encoding="utf-8")), None
    except Exception as e:
        return {"tables": {}}, f"语义文件加载失败（{type(e).__name__}: {e}），当前只返回物理字段结构"


def _table_names(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _unique_constraints(conn, table) -> list:
    """返回唯一约束的列组合，如 [['order_id', 'product_id']]。"""
    out = []
    for _seq, idx_name, is_unique, _origin, _partial in conn.execute(f"PRAGMA index_list({table})"):
        if is_unique:
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx_name})")]
            out.append(cols)
    return out


def _foreign_keys(conn, table) -> list:
    return [
        {"column": r[3], "references": f"{r[2]}.{r[4]}"}
        for r in conn.execute(f"PRAGMA foreign_key_list({table})")
    ]


def inspect_schema_func(table=None):
    semantics, sem_warn = load_semantics()
    sem_tables = semantics.get("tables", {})

    try:
        conn = sqlite3.connect(f"file:{_qs._db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return _env.fail("internal", f"无法打开数据库：{e}")

    try:
        names = _table_names(conn)

        # ── 不传 table：一次给出全库紧凑字段清单 ──
        if not table:
            tables = []
            for n in names:
                info = conn.execute(f"PRAGMA table_info({n})").fetchall()
                tables.append({
                    "name": n,
                    "desc": sem_tables.get(n, {}).get("desc"),
                    "columns": [{"name": r[1], "type": r[2], "pk": bool(r[5])} for r in info],
                })
            data = {"tables": tables}
            if sem_warn:
                data["warnings"] = [sem_warn]
            return _env.ok(data)

        # ── 传 table：完整信息 ──
        if table not in names:
            return _env.fail("bad_parameter",
                             f"表 {table!r} 不存在。可用的表：{', '.join(names)}")

        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        sem_table = sem_tables.get(table, {})
        sem_cols = sem_table.get("columns", {}) or {}
        physical = {r[1] for r in info}

        columns = []
        for _cid, name, ctype, notnull, _dflt, pk in info:
            meta = sem_cols.get(name, {})
            item = {"name": name, "type": ctype, "desc": meta.get("desc")}
            if meta.get("enum"):
                item["enum"] = meta["enum"]
            if meta.get("format"):
                item["format"] = meta["format"]
            if notnull:
                item["not_null"] = True
            if pk:
                item["primary_key"] = True
            # money 标记故意不返回：模型看到「以分存储」会二次换算
            columns.append(item)

        warnings = [f"语义文件里的字段 {n} 在表 {table} 中不存在，已忽略（语义可能过期）"
                    for n in sem_cols if n not in physical]
        if sem_warn:
            warnings.append(sem_warn)

        data = {
            "table": table,
            "desc": sem_table.get("desc"),
            "notes": sem_table.get("notes", []),
            "columns": columns,
            "foreign_keys": _foreign_keys(conn, table),
            "unique_constraints": _unique_constraints(conn, table),
        }
        if warnings:
            data["warnings"] = warnings
        return _env.ok(data)

    except sqlite3.Error as e:
        return _env.fail("sql_error", str(e))
    finally:
        conn.close()
