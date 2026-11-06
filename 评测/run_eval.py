"""eval_v2 跑题脚本。

每题按所属库（clean / dirty）跑 N 次，每次都是全新的 run_agent 调用。
结果逐条落盘到 runs_raw.json，支持中断后断点续跑（已存在的 (题目, 第几次) 会跳过）。

用法：
    python run_eval.py                 # 全部 22 题 × 3 次
    python run_eval.py --only 常规-01   # 只跑一题
    python run_eval.py --runs 1        # 每题只跑 1 次（试跑用）
"""
import argparse
import json
import pathlib
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / "agent_software"))

from main import run_agent, MODEL_NAME, TEMPERATURE, MAX_STEPS  # noqa: E402
from tools.query_sql import set_db  # noqa: E402
from run_paths import resolve_dir  # noqa: E402

QFILE = BASE / "questions.json"


def load_runs(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_runs(path, runs):
    path.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑指定题号，如 常规-01")
    ap.add_argument("--runs", type=int, default=3, help="每题跑几次，默认 3")
    ap.add_argument("--no-schema", action="store_true",
                    help="停用 inspect_schema（工具注册 + 提示词一起移除），用于 A/B 对照组")
    ap.add_argument("--dir", default=None,
                    help="批次目录，如 runs/20261106_val0.2_role；省略则取最近一个批次")
    args = ap.parse_args()

    out_dir = resolve_dir(args.dir, create=bool(args.dir))
    OUT = out_dir / "runs_raw.json"
    print(f"批次目录：{OUT.parent}")

    if args.no_schema:
        import main as _main
        _main.all_tools_schema = [s for s in _main.all_tools_schema
                                  if s["function"]["name"] != "inspect_schema"]
        _main.available_functions.pop("inspect_schema", None)
        _main.SYSTEM_PROMPT = _main.SYSTEM_PROMPT.replace(
            "\n8. 需要确认表名、字段名、枚举取值或字段含义时，先调 inspect_schema 查看结构，"
            "不要用 SELECT name, sql FROM sqlite_master 或猜字段名。", "")
        print("已停用 inspect_schema（工具注册与提示词一并移除）")
        print("剩余工具:", [s["function"]["name"] for s in _main.all_tools_schema])

    questions = json.loads(QFILE.read_text(encoding="utf-8"))
    if args.only:
        questions = [q for q in questions if q["id"] == args.only]
        if not questions:
            print(f"未找到题号 {args.only}")
            return

    runs = load_runs(OUT)
    done = {(r["question_id"], r["run"]) for r in runs}

    total = len(questions) * args.runs
    print(f"模型 {MODEL_NAME} ｜ temperature {TEMPERATURE} ｜ MAX_STEPS {MAX_STEPS}")
    print(f"计划 {len(questions)} 题 × {args.runs} 次 = {total} 次运行")
    print(f"已有记录 {len(runs)} 条\n")

    n = 0
    for q in questions:
        for i in range(1, args.runs + 1):
            n += 1
            rid = f"{q['id']}#{i}"
            if (q["id"], i) in done:
                print(f"[{n}/{total}] {rid} 已存在，跳过")
                continue

            set_db(q["db"])
            t0 = time.time()
            rec = {
                "question_id": q["id"],
                "run_id": rid,
                "run": i,
                "db": q["db"],
                "question": q["question"],
            }
            try:
                r = run_agent(q["question"])
                rec.update({
                    "ok": r.get("ok", False),
                    "answer": r.get("answer"),
                    "steps": r.get("steps"),
                    "tokens": r.get("tokens"),
                    "tool_trace": r.get("tool_trace") or [],
                    "error": r.get("error"),
                })
            except Exception as e:  # 单次失败不中断整轮
                rec.update({
                    "ok": False, "answer": None, "steps": None, "tokens": None,
                    "tool_trace": [], "error": f"{type(e).__name__}: {e}",
                })
            rec["elapsed_sec"] = round(time.time() - t0, 1)

            runs.append(rec)
            save_runs(OUT, runs)  # 每条立刻落盘

            print(f"[{n}/{total}] {rid} ok={rec['ok']} steps={rec['steps']} "
                  f"tokens={rec['tokens']} {rec['elapsed_sec']}s")
            if rec.get("error"):
                print(f"    error: {rec['error']}")

    print(f"\n完成，共 {len(runs)} 条记录 -> {OUT}")


if __name__ == "__main__":
    main()
