"""生成 judgement.json。

默认按题型给满分（常规/边界 2 点 × 50；脏库 4 层 × 25），只对已验证的失败项覆盖。
失败项依据见 notes，判分依据是评测集的判据 + 逐条核对答案。
"""
import argparse
import json
import pathlib

from run_paths import resolve_dir

BASE = pathlib.Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--dir", default=None,
                help="批次目录，如 runs/20261106_val0.2_role；省略则取最近一个批次")
args = ap.parse_args()
OUT_DIR = resolve_dir(args.dir)

runs = json.loads((OUT_DIR / "runs_raw.json").read_text(encoding="utf-8"))
questions = json.loads((BASE / "questions.json").read_text(encoding="utf-8"))
kind_of = {q["id"]: q["kind"] for q in questions}

# 覆盖项来源：优先读批次目录里的 overrides.json（每个批次的人工裁定不一样，
# 不能再把 11-03 那三条写死在这里）；没有该文件时退回 11-03 的历史裁定。
# overrides.json 格式：[{run_id, score, attribution, notes, points?}]
OVERRIDE_FILE = OUT_DIR / "overrides.json"
if OVERRIDE_FILE.exists():
    OVERRIDE = {
        o["run_id"]: {
            "score": o["score"],
            "attribution": o.get("attribution", "模型理解"),
            "notes": o.get("notes", ""),
            "points": o.get("points"),
        }
        for o in json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
    }
else:
    _LEGACY = {
        "脏库-01#3": (0.0, "答 14 笔，并把与 13 笔的差异解释成「跨月完成」——这是错误归因。实际差异来自订单 84 的主表状态与状态历史不一致（历史只到「运输中」），模型全程未提订单 84。四层全 0。", "模型理解"),
        "脏库-03#2": (0.0, "达到 MAX_STEPS=12 上限被强制停止，未产出答案。按 §2.8 以结果为准，判不通过。", "查询设计"),
        "脏库-03#3": (0.0, "第 12 步返回空回答（ok=True 但 content 为空），未给出任何结论。", "模型理解"),
    }
    OVERRIDE = {k: {"score": v[0], "notes": v[1], "attribution": v[2], "points": None}
                for k, v in _LEGACY.items()}

out = {}
for r in runs:
    rid = r["run_id"]
    qid = r["question_id"]
    kind = kind_of[qid]

    if rid in OVERRIDE:
        ov = OVERRIDE[rid]
        if ov["points"]:
            pts = ov["points"]
        elif kind == "脏库":
            pts = [{"point": n, "verdict": "❌", "score": 0.0} for n in ("发现", "定位", "影响", "保留")]
        else:
            pts = [{"point": "结果正确", "verdict": "❌", "score": 0.0},
                   {"point": "方法与口径正确", "verdict": "❌", "score": 0.0}]
        out[rid] = {"score": ov["score"], "points": pts, "misreport": False,
                    "attribution": ov["attribution"], "notes": ov["notes"]}
    else:
        if kind == "脏库":
            pts = [{"point": n, "verdict": "✅", "score": 25.0} for n in ("发现", "定位", "影响", "保留")]
        else:
            pts = [{"point": "结果正确", "verdict": "✅", "score": 50.0},
                   {"point": "方法与口径正确", "verdict": "✅", "score": 50.0}]
        out[rid] = {"score": 100.0, "points": pts, "misreport": False,
                    "attribution": "无异常", "notes": ""}

# 11-03 那次的历史备注：仅当该 run 未被本批次覆盖时补充
if "脏库-01#2" in out and out["脏库-01#2"]["score"] == 100.0 and not OVERRIDE_FILE.exists():
    out["脏库-01#2"]["notes"] = "发现与定位正确；影响层措辞略不精确（说「缺少状态流转记录」，实为缺「已完成」那一条），结论 13 vs 14 正确，不扣分。"

(OUT_DIR / "judgement.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已写入 {OUT_DIR / 'judgement.json'}，共 {len(out)} 条")
bad = [k for k, v in out.items() if v["score"] < 100]
print("非满分：", ", ".join(bad))
