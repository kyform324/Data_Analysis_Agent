# 评测批次

题目只有一份（上一级的 `eval_v2_评测集.md` 与 `questions.json`），变的是"用哪版代码跑它"。
因此按批次建目录，名字格式 `<日期>_<条件>`，条件写清影响结果的变量。

| 批次 | 条件 | 内容 |
|---|---|---|
| `20261103_temp0.2_无role` | temperature 0.2，尚无结果角色与选择器 | 报告 md + median_runs。原始轨迹与 all_runs 体积较大，放在 `_archive/评测原始轨迹_20261103/`（不入库，可复算） |
| `20261103_ab_inspect_schema` | 同一套 22 题，A/B 两组：不注册 / 注册 `inspect_schema` | 原始轨迹同在 `_archive/评测原始轨迹_20261103/`（`runs_raw_v3.1_无schema.json`、`runs_raw_v3.2_有schema.json`） |
| `20261106_val0.2_role` | temperature 0.2，含 `role` 参数与结果选择器 | 本轮全量重跑（66 次） |

## 命令

```bash
cd 评测
python run_eval.py --dir runs/20261106_val0.2_role     # 跑题，断点续跑
python make_judgement.py --dir runs/20261106_val0.2_role
python report.py --dir runs/20261106_val0.2_role
```

省略 `--dir` 时取名字最大的批次（日期前缀保证有序）。
