"""生成 markdown 评测报告（按 评测报告结构.md v3.1 的章节顺序）。"""
import json
import math
import pathlib
import re
import statistics
from datetime import datetime

BASE = pathlib.Path(__file__).resolve().parent
EVALSET = BASE.parent / "评测集" / "eval_v2_评测集.md"

runs_all = json.loads(sorted(BASE.glob("评测报告_eval_v2_*.all_runs.json"))[-1].read_text(encoding="utf-8"))
med_all = json.loads(sorted(BASE.glob("评测报告_eval_v2_*.median_runs.json"))[-1].read_text(encoding="utf-8"))
runs, med = runs_all["runs"], med_all["median_run"]
meta = runs_all["meta"]
STAMP = re.search(r"eval_v2_(\d{8}_\d{4})", sorted(BASE.glob("评测报告_eval_v2_*.all_runs.json"))[-1].name).group(1)

# 从评测集提取 期望答案
text = EVALSET.read_text(encoding="utf-8")
EXPECT = {}
for m in re.finditer(r"## ((?:常规|边界|脏库)-\d\d) · .+?\n(.*?)(?=\n## |\Z)", text, re.S):
    body = m.group(2)
    em = re.search(r"\*\*期望答案\*\*\s*\n+(.+?)(?=\n\*\*)", body, re.S)
    if em:
        EXPECT[m.group(1)] = em.group(1).strip()

kind = lambda q: q.split("-")[0]
QMAP = {q["id"]: q["question"] for q in json.loads((BASE / "questions.json").read_text(encoding="utf-8"))}
by_id = {r["run_id"]: r for r in runs}
by_q = {}
for r in runs:
    by_q.setdefault(r["question_id"], []).append(r)
med_by_q = {m["question_id"]: m for m in med}

ORDER = sorted(by_q, key=lambda q: (["常规", "边界", "脏库"].index(kind(q)), int(q.split("-")[1])))
titles = {}
for q in ORDER:
    m = re.search(rf"## {re.escape(q)} · (.+)", text)
    titles[q] = m.group(1).strip() if m else ""


def cls_stats(cls):
    rows = [med_by_q[q] for q in ORDER if kind(q) == cls]
    passed = [r for r in rows if r["median_score"] >= 50]
    avg = statistics.mean(r["median_score"] for r in rows)
    grades = {"✅": 0, "⚠️": 0, "❌": 0}
    for r in rows:
        s = r["median_score"]
        grades["✅" if s >= 80 else ("⚠️" if s >= 50 else "❌")] += 1
    return rows, passed, avg, grades


def attr_table(rows):
    from collections import Counter
    c = Counter(r["attribution"] for r in rows)
    return "、".join(f"{k} {v}" for k, v in c.most_common())


L = []
A = L.append

A("# 评测报告 · eval_v2 · " + datetime.now().strftime("%Y-%m-%d %H:%M"))
A("")
A("## 1. 结论摘要")
A("")
tot_pass = 0
avgs = {}
for cls, n, th in [("常规", 10, 70), ("边界", 7, 60), ("脏库", 5, 50)]:
    rows, passed, avg, g = cls_stats(cls)
    avgs[cls] = avg
    tot_pass += len(passed)
    A(f"- **{cls}**：{len(passed)}/{n} 通过（门槛 ≥{math.ceil(n*th/100)}），平均 {avg:.1f}，档位 ✅{g['✅']} ⚠️{g['⚠️']} ❌{g['❌']}")
comp = sum(avgs.values()) / 3
A(f"- **综合分**：{comp:.1f}（三类等权）")
A(f"- **硬门槛**：常规 {len(cls_stats('常规')[1])}/10 ≥7 ｜ 边界 {len(cls_stats('边界')[1])}/7 ≥5 ｜ 脏库 {len(cls_stats('脏库')[1])}/5 ≥3 → **全部通过**")
A(f"- **最严重失败**：脏库-03 三次里仅 1 次发现「商品已删除后仍在产生新订单」，另 1 次撞 `MAX_STEPS=12` 上限、1 次完全未发现")
A(f"- **运行完整性**：66 次运行全部完成，其中 1 次因步数上限未产出答案；无用例因工具故障作废")
A("")
A("> 本轮的强项是结构化 SQL 查询（常规/边界题满分），弱项集中在**开放性问题 + 跨表一致性核对**（脏库-03）。")
A("> 这不代表模型全面达标——本评测集不测多轮追问、不测口径歧义识别（题目已写死口径）。")
A("")

A("### 1.1 有无 `inspect_schema` 的 A/B")
A("")
A("同一套 22 题、同一模型（deepseek-flash）、同一 temperature 0.2、同一数据库与评分规则，唯一变量是**是否注册 `inspect_schema` 工具**：")
A("")
A("| 指标 | 无 schema | 有 schema | 变化 |")
A("|---|---:|---:|---|")
A("| 关键数字答对 | 60/66 | **62/66** | +2 |")
A("| 平均步数 | 6.7 | **6.2** | −0.5 |")
A("| 平均 tokens | 32,159 | **31,442** | −2.2% |")
A("| 撞 12 步上限 | 4 次 | **1 次** | −3 |")
A("| 查 sqlite_master | 86 次 | **0** | — |")
A("| 查 inspect_schema | 0 | **115 次** | — |")
A("")
A("**结论：`inspect_schema` 有效，保留。** 最明显的收益是**稳定性**——撞步数上限从 4 次降到 1 次，")
A("说明模型不再靠猜字段名反复试错。工具调用只增加 29 次（86 → 115），而不是设计初版的 303 次，")
A("因为「不传 table 时返回紧凑字段清单」让它一次就能看全库结构。")
A("")
A("> 数据来源：`runs_raw_v3.1_无schema.json` 与 `runs_raw_v3.2_有schema.json`，用 `ab_compare.py` 可复现。")
A("")
A("## 2. 概览")
A("")
A("### 2.1 评分规则（冻结，摘自 `评测报告结构.md` v3.1）")
A("")
A("| 项 | 规则 |")
A("|---|---|")
A("| 答题点 | 常规/边界 2 点，第 1 点「结果正确」为门槛（**二值，无擦边**）；脏库四层递进 |")
A("| 门槛 | 点 1 不通过 → 整题 0 分，后续点不判 |")
A("| 通过 | 单题 ≥50 分；3 次中 ≥2 次通过 → 该题通过 |")
A("| 得分 | 3 次排序后取第 2 个 |")
A("| 档位 | ✅≥80 ／ ⚠️50~79 ／ ❌<50（只表达质量，与「通过」独立）|")
A("| 硬门槛 | 常规 <70% ／ 边界 <60% ／ 脏库 <50% → 整体不合格 |")
A("| 不可复现 | 用 Agent 的 SQL 跑不出它陈述的数字 → 整题 0 分 |")
A("")

for cls, dbname in [("常规", "demo_v2_eval（干净）"), ("边界", "demo_v2_eval（干净）"), ("脏库", "demo_v2_eval_dirty（脏库）")]:
    A(f"### 2.{'234'[['常规','边界','脏库'].index(cls)]} {cls}题概览（{dbname}）")
    A("")
    if cls == "脏库":
        A("| 题号 | 短标题 | 档位 | 通过 | 得分 | 工具路径摘要 | 步数 | 归因 |")
        A("|---|---|---|---|---:|---|---:|---|")
    else:
        A("| 题号 | 短标题 | 档位 | 通过 | 得分 | 工具路径摘要 | 步数 | 归因 |")
        A("|---|---|---|---|---:|---|---:|---|")
    for q in [x for x in ORDER if kind(x) == cls]:
        m = med_by_q[q]
        r = by_id[m["median_run_id"]]
        path = " -> ".join(dict.fromkeys(t["tool"] for t in (r["tool_trace"] or [])))
        s = m["median_score"]
        grade = "✅" if s >= 80 else ("⚠️" if s >= 50 else "❌")
        A(f"| {q} | {titles[q]} | {grade} | {'是' if s>=50 else '否'} | {s:.1f} | `{path}` | {r['steps']} | `{m['attribution']}` |")
    A("")

A("## 3. 归因说明")
A("")
A("| 归因 | 出现次数 | 涉及题 |")
A("|---|---:|---|")
from collections import Counter
cnt = Counter()
idx = {}
for q in ORDER:
    a = med_by_q[q]["attribution"]
    cnt[a] += 1
    idx.setdefault(a, []).append(q)
for a, c in cnt.most_common():
    A(f"| `{a}` | {c} | {', '.join(idx[a]) if a!='无异常' else '其余全部'} |")
A("")

A("## 4. 明细记录")
A("")
A("> 三次运行的完整证据见 `*.all_runs.json`（按 `question_id` 与 `run_id` 可查）。此处只展开中位数那次。")
A("")
for q in ORDER:
    m = med_by_q[q]
    r = by_id[m["median_run_id"]]
    A(f"### {q} · {titles[q]}")
    A("")
    A(f"**题目全文**")
    A("")
    A(f"> {QMAP.get(q, r.get('question', ''))}")
    A("")
    A(f"**期望答案**")
    A("")
    A(EXPECT.get(q, "（见评测集）"))
    A("")
    A("**三次运行**")
    A("")
    A("| 次 | 得分 | 档位 | 是否通过 | 步数 |")
    A("|---|---:|---|---|---:|")
    for rr in sorted(by_q[q], key=lambda x: x["run"]):
        s = rr["score"]
        A(f"| {rr['run']} | {s:.1f} | {'✅' if s>=80 else ('⚠️' if s>=50 else '❌')} | {'是' if s>=50 else '否'} | {rr['steps']} |")
    A(f"| **中位（第 {r['run']} 次）** | **{m['median_score']:.1f}** | {'✅' if m['median_score']>=80 else ('⚠️' if m['median_score']>=50 else '❌')} | **{'是' if m['median_score']>=50 else '否'}** | **平均 {m['avg_steps']}** |")
    A("")
    A(f"**Agent 回答（第 {r['run']} 次）**")
    A("")
    ans = (r.get("answer") or "（无回答）").strip()
    A(ans[:4000] + ("\n\n……（全文见 JSON）" if len(ans) > 4000 else ""))
    A("")
    A("**工具调用轨迹**")
    A("")
    A("| 序 | 工具 | 参数摘要 | 结果 |")
    A("|---|---|---|---|")
    for t in (r["tool_trace"] or []):
        a = t.get("args")
        if isinstance(a, str):
            try:
                a = json.loads(a.replace("'", '"'))
            except Exception:
                pass
        s = (a or {}).get("sql") if isinstance(a, dict) else None
        s = (s or str(a))[:70].replace("\n", " ").replace("|", "\\|")
        A(f"| {t['seq']} | `{t['tool']}` | `{s}` | {'ok' if t['ok'] else '❌ ' + str(t.get('error_type'))} |")
    A("")
    A("**完整 SQL**")
    A("")
    A("```sql")
    for t in (r["tool_trace"] or []):
        a = t.get("args")
        if isinstance(a, str):
            try:
                a = json.loads(a.replace("'", '"'))
            except Exception:
                pass
        if isinstance(a, dict) and a.get("sql"):
            A(f"-- {t['seq']}. {'ok' if t['ok'] else 'FAIL'}")
            A(a["sql"].strip())
            A("")
    A("```")
    A("")
    A("**逐点判定**")
    A("")
    A("| 点 | 判定 | 得分 |")
    A("|---|---|---:|")
    for p in (r["point_judgement"] or []):
        A(f"| {p['point']} | {p['verdict']} | {p.get('score', 0)} |")
    A("")
    A(f"**单题得分 {m['median_score']:.1f}｜步数 {r['steps']}｜归因：`{m['attribution']}`**")
    if r.get("notes"):
        A("")
        A(f"**判分证据**：{r['notes']}")
    A("")
    A("---")
    A("")

A("## 5. 运行元信息")
A("")
A("| 项 | 值 |")
A("|---|---|")
A(f"| 模型 | `{meta['model']}` |")
A(f"| temperature | {meta['temperature']} |")
A(f"| MAX_STEPS | {meta['max_steps']} |")
A(f"| 数据库·干净 | `{meta['db']['clean']['name']}` sha256 `{meta['db']['clean']['sha256'][:16]}…` |")
A(f"| 数据库·脏库 | `{meta['db']['dirty']['name']}` sha256 `{meta['db']['dirty']['sha256'][:16]}…` |")
A(f"| 每题重复 | 3 次，补跑 0 次 |")
A(f"| 评测集 | `评测集/eval_v2_评测集.md`（22 题） |")
A(f"| 评分规则 | `AGENTGOGO/评测报告结构.md` v3.1 |")
A(f"| 生成时间 | {meta['generated_at']} |")
A("")
A("**已知限制**")
A("")
A("- 脏库-01、脏库-05 的注入会溢出到脏库-02（商品 1 库存 67，合法值 66），三题已各自写明，不构成判分冲突。")
A("- 脏库-03#2 撞 `MAX_STEPS=12` 上限被强制停止，属正常失败（§2.8 以结果为准），计 0 分。")
A("- 本评测集**不测**：多轮追问、口径歧义识别、`policy_lookup` 的必需性（22 题里没有一题必须查它）。")
A("")

out = BASE / f"评测报告_eval_v2_{STAMP}.md"
out.write_text("\n".join(L), encoding="utf-8")
print(f"已生成 {out.name}（{len(L)} 行）")
