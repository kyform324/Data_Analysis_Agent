"""评测批次目录解析。

约定：题目只有一份（评测集/ 与 questions.json），变的是"用哪版代码跑它"。
所以每次运行的结果放进 评测/runs/<日期>_<条件>/，条件写清影响结果的变量
（temperature、是否注册某个工具等）。

批次目录里的文件：
    runs_raw.json      跑题原始记录（断点续跑依据）
    judgement.json     判分结果
    <报告名>.all_runs.json / .median_runs.json / .md
"""
import pathlib

BASE = pathlib.Path(__file__).resolve().parent
RUNS_ROOT = BASE / "runs"


def list_dirs():
    if not RUNS_ROOT.exists():
        return []
    return sorted(d for d in RUNS_ROOT.iterdir() if d.is_dir())


def resolve_dir(arg=None, create=False):
    """--dir 给了就用它（可创建）；没给就取名字最大的那个批次（日期前缀保证有序）。

    相对路径的解析规则：带目录的（如 ``runs/20261106_x``）相对 ``评测/``，
    单个名字（如 ``20261106_x``）视为 ``评测/runs/`` 下的批次名。
    """
    if arg:
        p = pathlib.Path(arg)
        if not p.is_absolute():
            p = (BASE / p) if len(p.parts) > 1 else (RUNS_ROOT / p)
        if create:
            p.mkdir(parents=True, exist_ok=True)
        elif not p.is_dir():
            raise SystemExit(f"批次目录不存在：{p}")
        return p

    existing = list_dirs()
    if not existing:
        raise SystemExit("没有可用的评测批次，请用 --dir 指定，例如 --dir runs/20261106_val0.2_role")
    return existing[-1]
