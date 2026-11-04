"""三轮 A/B 对比。

v2.1 术语问法 / 无 schema
v2.2 口语问法 / 无 schema
v2.3 口语问法 / 有 schema

关注四项：关键数字答对、平均步数、平均 tokens、以及「查 sqlite_master 的次数」。
最后一项是 inspect_schema 价值的直接指标——加了工具之后应该归零。
"""
import json
import pathlib
import statistics

from judge_helper import EXPECT, has_num, norm

BASE = pathlib.Path(__file__).resolve().parent
ROUNDS = [
    ("v3.1 无schema", "runs_raw_v3.1_无schema.json"),
    ("v3.2 有schema", "runs_raw.json"),
]


def is_sqlite_master(t):
    a = t.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a.replace("'", '"'))
        except Exception:
            a = {}
    sql = (a or {}).get("sql") if isinstance(a, dict) else ""
    return "sqlite_master" in (sql or "")


def analyse(path):
    p = BASE / path
    if not p.exists():
        return None
    runs = json.loads(p.read_text(encoding="utf-8"))
    if not runs:
        return None
    steps = [r["steps"] for r in runs if r.get("steps")]
    toks = [r["tokens"] for r in runs if r.get("tokens")]
    ok_num = 0
    for r in runs:
        exp = EXPECT.get(r["question_id"], {"nums": [], "words": []})
        a = norm(r.get("answer"))
        miss = [n for n in exp["nums"] if not has_num(a, n)]
        miss += [w for w in exp["words"] if w not in a]
        if not miss and r.get("ok"):
            ok_num += 1
    master = sum(1 for r in runs for t in (r.get("tool_trace") or []) if is_sqlite_master(t))
    schema = sum(1 for r in runs for t in (r.get("tool_trace") or []) if t.get("tool") == "inspect_schema")
    fails = sum(1 for r in runs if not r.get("ok"))
    return {
        "n": len(runs),
        "ok_num": ok_num,
        "steps": statistics.mean(steps) if steps else 0,
        "tokens": statistics.mean(toks) if toks else 0,
        "master": master,
        "schema": schema,
        "fails": fails,
        "total_tokens": sum(toks),
    }


def main():
    res = [(label, analyse(f)) for label, f in ROUNDS]
    res = [(l, r) for l, r in res if r]

    print(f"{'轮次':22s}{'已完成':>7s}{'数字对':>8s}{'平均步数':>9s}{'平均tokens':>12s}{'查master':>9s}{'查schema':>9s}{'失败':>6s}")
    print("-" * 84)
    for label, r in res:
        print(f"{label:22s}{r['n']:>7d}{r['ok_num']:>8d}{r['steps']:>9.1f}"
              f"{r['tokens']:>12,.0f}{r['master']:>9d}{r['schema']:>9d}{r['fails']:>6d}")

    if len(res) >= 2:
        print("\n相邻两轮的变化：")
        for (l1, r1), (l2, r2) in zip(res, res[1:]):
            print(f"  {l1} → {l2}")
            print(f"    平均步数 {r1['steps']:.1f} → {r2['steps']:.1f}（{r2['steps']-r1['steps']:+.1f}）")
            print(f"    平均tokens {r1['tokens']:,.0f} → {r2['tokens']:,.0f}"
                  f"（{(r2['tokens']-r1['tokens'])/r1['tokens']*100:+.1f}%）")
            print(f"    查 sqlite_master {r1['master']} → {r2['master']}")
            print(f"    查 inspect_schema {r1['schema']} → {r2['schema']}")


if __name__ == "__main__":
    main()
