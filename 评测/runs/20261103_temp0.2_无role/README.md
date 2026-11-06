# 20261103 · temperature 0.2 · 无结果角色

这一批是 **eval_v2 首次全量评测**（22 题 × 3 次 = 66 次），也是 README 里 93.3 分的来源。

条件：`temperature=0.2`、`MAX_STEPS=12`、当时还没有 `query_sql` 的 `role` 参数与结果选择器。

本目录只放报告与派生结果：

- `评测报告_eval_v2_20261103_1711.md` —— 完整报告（含 A/B 对照、逐题明细）
- `评测报告_eval_v2_20261103_1711.median_runs.json` —— 22 条中位派生

原始证据（`runs_raw.json`、`all_runs.json`、`judgement.json`）体积较大，放在
仓库外的 `_archive/评测原始轨迹_20261103/`，用同一套脚本可从 `runs_raw.json` 重算。
