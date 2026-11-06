"""eval_v2 报告生成器。

输入：
    runs_raw.json   跑题原始记录
    judgement.json  人工判分结果（key = run_id）

输出：
    <报告名>.all_runs.json     66 条原始证据
    <报告名>.median_runs.json  22 条派生（含 run_id 回指）
    <报告名>.md                报告正文（概览表 + 明细骨架）

生成前执行 4 条交叉校验，任一不过则中止、不输出。
"""
import argparse
import json
import pathlib
import statistics
from datetime import datetime

BASE = pathlib.Path(__file__).resolve().parent

from run_paths import resolve_dir  # noqa: E402

GRADE = lambda s: "✅" if s >= 80 else ("⚠️" if s >= 50 else "❌")
PASS = lambda s: s >= 50


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def build_all_runs(runs, judge, meta):
    out = []
    for r in runs:
        rid = r["run_id"]
        if rid not in judge:
            raise SystemExit(f"judgement.json 缺少 {rid}")
        j = judge[rid]
        out.append({
            "question_id": r["question_id"],
            "run_id": rid,
            "run": r["run"],
            "db": r["db"],
            "score": round(float(j["score"]), 1),
            "grade": GRADE(j["score"]),
            "passed": PASS(j["score"]),
            "steps": r.get("steps"),
            "tokens": r.get("tokens"),
            "answer": r.get("answer"),
            "tool_trace": r.get("tool_trace") or [],
            "point_judgement": j.get("points", []),
            "misreport": j.get("misreport", False),
            "attribution": j.get("attribution", "无异常"),
            "notes": j.get("notes", ""),
        })
    return {"meta": meta, "runs": out}


def build_median(all_runs):
    by_q = {}
    for r in all_runs["runs"]:
        by_q.setdefault(r["question_id"], []).append(r)

    med = []
    for qid, rs in by_q.items():
        if len(rs) != 3:
            raise SystemExit(f"{qid} 的 run 数不是 3（实际 {len(rs)}），交叉校验 2 失败")
        scores = sorted(x["score"] for x in rs)
        median_score = scores[1]
        picked = next(x for x in rs if x["score"] == median_score)
        med.append({
            "question_id": qid,
            "median_run_id": picked["run_id"],
            "median_score": median_score,
            "pass_count": sum(1 for x in rs if x["passed"]),
            "attribution": picked["attribution"],
            "avg_steps": round(statistics.mean(x["steps"] or 0 for x in rs), 1),
            "all_run_ids": [x["run_id"] for x in rs],
        })
    return {"median_run": med}


def cross_check(all_runs, median):
    errors = []
    ids = {r["run_id"] for r in all_runs["runs"]}
    by_id = {r["run_id"]: r for r in all_runs["runs"]}

    # 1. median_run_id 都能在 all_runs 找到
    for m in median["median_run"]:
        if m["median_run_id"] not in ids:
            errors.append(f"[1] {m['median_run_id']} 不在 all_runs")

    # 2. 每题恰好 3 条
    cnt = {}
    for r in all_runs["runs"]:
        cnt[r["question_id"]] = cnt.get(r["question_id"], 0) + 1
    for q, n in cnt.items():
        if n != 3:
            errors.append(f"[2] {q} 有 {n} 条 run")

    # 3. all_run_ids 属于同一 question_id
    for m in median["median_run"]:
        for rid in m["all_run_ids"]:
            if rid not in by_id:
                errors.append(f"[3] {rid} 不存在")
            elif by_id[rid]["question_id"] != m["question_id"]:
                errors.append(f"[3] {rid} 不属于 {m['question_id']}")

    # 4. median_score / pass_count / attribution 一致
    for m in median["median_run"]:
        rs = [by_id[i] for i in m["all_run_ids"] if i in by_id]
        if len(rs) != 3:
            continue
        if m["median_score"] != sorted(x["score"] for x in rs)[1]:
            errors.append(f"[4] {m['question_id']} 中位数算错")
        if m["pass_count"] != sum(1 for x in rs if x["passed"]):
            errors.append(f"[4] {m['question_id']} pass_count 不一致")
        picked = by_id[m["median_run_id"]]
        if m["attribution"] != picked["attribution"]:
            errors.append(f"[4] {m['question_id']} attribution 不一致")

    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d_%H%M"))
    ap.add_argument("--dir", default=None,
                    help="批次目录，如 runs/20261106_val0.2_role；省略则取最近一个批次")
    args = ap.parse_args()

    out_dir = resolve_dir(args.dir)
    runs = load(out_dir / "runs_raw.json")
    judge = load(out_dir / "judgement.json")

    meta = {
        "eval_version": "eval_v2",
        "model": "deepseek-flash",
        "temperature": 0.2,
        "max_steps": 12,
        "db": {
            "clean": {"name": "demo_v2_eval", "sha256": "2c5af48283e9195501c1beb4c13bcdb6ca679ce014c10e1ce03e59e5c345efd8"},
            "dirty": {"name": "demo_v2_eval_dirty", "sha256": "881eb8e5fb9ea176de58f7b680013d814b3a314186b05316bd5ec9800b229634"},
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    all_runs = build_all_runs(runs, judge, meta)
    median = build_median(all_runs)

    errors = cross_check(all_runs, median)
    if errors:
        print("交叉校验失败，未输出：")
        for e in errors:
            print("  ", e)
        return 1

    name = f"评测报告_eval_v2_{args.stamp}"
    (out_dir / f"{name}.all_runs.json").write_text(
        json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{name}.median_runs.json").write_text(
        json.dumps(median, ensure_ascii=False, indent=2), encoding="utf-8")

    print("4 条交叉校验通过")
    print(f"  {name}.all_runs.json    {len(all_runs['runs'])} 条")
    print(f"  {name}.median_runs.json {len(median['median_run'])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
