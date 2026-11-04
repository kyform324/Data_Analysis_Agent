"""判分辅助：按期望关键数字自动标出可疑记录。

不直接给分，只回答「这条答案里，期望的辨识性数字是否都在」。
判分仍由人按评测集的判据决定。

用法：python judge_helper.py
"""
import json
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parent

# 每题用于比对的「辨识性数字」与「必须出现的词」。0 与 1 太容易误匹配，一律不列。
EXPECT = {
    "常规-01": {"nums": [24, 18, 6, 9, 5], "words": ["跑步鞋", "休闲帽"]},
    "常规-02": {"nums": [11, 9, 4, 5], "words": []},
    "常规-03": {"nums": [476704, 15374, 7230], "words": ["数码", "食品", "服饰"]},
    "常规-04": {"nums": [67838, 40164, 29416], "words": ["客户050", "客户016", "客户008"]},
    "常规-05": {"nums": [18, 16, 6, 4, 3], "words": ["深圳市", "杭州市", "济南市"]},
    "常规-06": {"nums": [10], "words": ["休闲帽"]},
    "常规-07": {"nums": [], "words": ["智能手机", "蓝牙耳机", "运动T恤", "混合坚果", "挂耳咖啡"]},
    "常规-08": {"nums": [20, 33, 13], "words": []},
    "常规-09": {"nums": [83, 45, 24817], "words": ["蓝牙耳机"]},
    "常规-10": {"nums": [6], "words": ["智能手机", "蓝牙耳机", "龙井茶叶", "挂耳咖啡"]},
    "边界-01": {"nums": [], "words": ["没有", "无"]},
    "边界-02": {"nums": [28], "words": ["智能手机", "蓝牙耳机"]},
    "边界-03": {"nums": [], "words": ["跑步鞋", "休闲帽"]},
    "边界-04": {"nums": [62], "words": ["蓝牙耳机"]},
    "边界-05": {"nums": [30], "words": ["智能手机"]},
    "边界-06": {"nums": [89, 99], "words": ["挂耳咖啡"]},
    "边界-07": {"nums": [55, 25, 67838, 8593], "words": ["客户050", "客户047"]},
    "脏库-01": {"nums": [14, 13, 84], "words": []},
    "脏库-02": {"nums": [120, 80], "words": ["蓝牙耳机"]},
    "脏库-03": {"nums": [111], "words": ["龙井茶叶", "已删除"]},
    "脏库-04": {"nums": [354, 1429, 1629], "words": []},
    "脏库-05": {"nums": [3, 2, 41], "words": []},
}


def norm(s: str) -> str:
    return (s or "").replace(",", "").replace("，", "").replace(" ", "")


def has_num(text: str, n: int) -> bool:
    # 1429 / 1629 代表 14:29 / 16:29
    if n in (1429, 1629):
        hh, mm = divmod(n, 100)
        return f"{hh:02d}:{mm:02d}" in text or f"{hh}:{mm:02d}" in text
    return re.search(rf"(?<![\d]){n}(?![\d])", text) is not None


def main():
    runs = json.loads((BASE / "runs_raw.json").read_text(encoding="utf-8"))
    by_q = {}
    for r in runs:
        by_q.setdefault(r["question_id"], []).append(r)

    print(f"{'run_id':14s} {'steps':>5s}  {'状态':6s}  缺失项")
    print("-" * 78)
    suspicious = []
    for qid in sorted(by_q, key=lambda x: (x.split("-")[0] != "常规", x)):
        exp = EXPECT.get(qid, {"nums": [], "words": []})
        for r in sorted(by_q[qid], key=lambda x: x["run"]):
            ans = norm(r.get("answer"))
            miss = [str(n) for n in exp["nums"] if not has_num(ans, n)]
            miss += [w for w in exp["words"] if w not in ans]
            flag = "OK" if not miss else "??"
            if miss:
                suspicious.append(r["run_id"])
            print(f"{r['run_id']:14s} {str(r.get('steps')):>5s}  {flag:6s}  "
                  f"{', '.join(miss) if miss else '-'}")
            if not r.get("ok", True):
                print(f"    !! ok=False error={r.get('error')}")
    print("-" * 78)
    print(f"共 {len(runs)} 条，可疑 {len(suspicious)} 条")
    if suspicious:
        print("需人工细看：", ", ".join(suspicious))


if __name__ == "__main__":
    main()
