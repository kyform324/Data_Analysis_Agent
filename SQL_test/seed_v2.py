#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扩容版造数脚本 seed_v2 —— 依据《数据扩容特性清单.md》v2（2026-10-29）

定位
----
本脚本是**锚点法**造数器：先由配额定死各维度数量（§6.1），再由代码**确定性构造**
§三 的 14 条数据场景（§6.2 声明占用），最后只在剩余空间里随机填充（§6.3）。
场景是构造出来的，不是靠换种子筛出来的。

与 seed_demo_data.py（已归档）的关系
------------------------------------
旧脚本已移至 AGENTGOGO/归档_Data_Analysis_Agent/，不在本项目中。复用的资产（逐项，可对照旧脚本行号核对）：
  · product_span()（旧 274 行）—— 保留同名同义，改为读 v2 的 lifecycle 字典
  · derive_daily()（旧 316 行）—— 流水派生快照的算法与「按 < D+1 日 00:00:00 切分」
    这个日界规则原样沿用（旧脚本最容易被写错的一处，直接抄结论）
  · price_at()（旧 255 行）→ v2 的 unit_price(order, product_id)：签名变了，因为 D1 要求
    价格依赖订单是否已完成，而不是依赖时刻
  · completion_time()（旧 267 行）→ v2 在 build_histories() 里顺带算出 order["completed_at"]
  · open_db()（旧 371 行）/ inject_dirty()（旧 464 行）—— 建库与脏库注入的骨架
  · check_01~check_22（旧 517~835 行）—— 验收检查的写法；其中「自动快照 04:00~05:00 窗口」
    「流水方向语义」「已删除后无业务事实」三条判定被改写进 §五/§四 校验
  · main() 的「先建 -> 再校验 -> 全过才发布」骨架（旧 858 行）
v2 的验收按 §五 10 条 + §四 补充 7 条 + §三 14 条重写，不沿用旧 22 条的具体条目。

发布
----
先写 data/_tmp_demo_v2_eval.sqlite3（脏库 _tmp_demo_v2_eval_dirty.sqlite3），
全部校验通过后才 os.replace 到正式名。校验失败不发布，并打印失败项。

用法
----
    python SQL_test/seed_v2.py
    python SQL_test/seed_v2.py --seed 20261029 --no-publish
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ============================================================ 一、路径与基础常量

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
DATA_DIR = PROJECT_ROOT / "data"

CLEAN_DB_PATH = DATA_DIR / "demo_v2_eval.sqlite3"
DIRTY_DB_PATH = DATA_DIR / "demo_v2_eval_dirty.sqlite3"
TMP_CLEAN_PATH = DATA_DIR / "_tmp_demo_v2_eval.sqlite3"
TMP_DIRTY_PATH = DATA_DIR / "_tmp_demo_v2_eval_dirty.sqlite3"

DATA_START = date(2026, 7, 1)
DATA_END = date(2026, 9, 30)
DATA_END_TS = "2026-09-30T23:59:59"

# §四 18：自动快照落在业务日次日 04:00~05:00
AUTO_REFRESH_TIMES = ["04:05:00", "04:17:00", "04:26:00", "04:38:00", "04:52:00"]
WINDOW_OPEN, WINDOW_CLOSE = "04:00:00", "05:00:00"

MONTHS = ["2026-07", "2026-08", "2026-09"]

# ============================================================ 二、§6.1 配额表

# --- 订单状态总量（110）
STATUS_QUOTA = {"已完成": 66, "已取消": 17, "待支付": 9, "已支付": 9, "运输中": 9}

# --- 月份分布（递增趋势，不能均匀）
MONTH_ORDER_QUOTA = {"2026-07": 25, "2026-08": 40, "2026-09": 45}

# --- 状态 × 月份（两张配额表拉直后的联立解；由 assert 在构造前校验）
STATUS_MONTH_QUOTA = {
    ("已完成", "2026-07"): 20, ("已完成", "2026-08"): 33, ("已完成", "2026-09"): 13,
    ("已取消", "2026-07"): 5, ("已取消", "2026-08"): 7, ("已取消", "2026-09"): 5,
    ("待支付", "2026-09"): 9,
    ("已支付", "2026-09"): 9,
    ("运输中", "2026-09"): 9,
}

# --- 状态年龄窗口（§6.1）：非终态订单距 09-30 的最大天数
STATUS_AGE_LIMIT = {"待支付": 3, "已支付": 5, "运输中": 7}

# --- 客户与复购（55 人 / 110 单）
# (每人订单数, 人数)：30×1 + 15×2 + 6×3 + 3×6 + 1×14 = 30+30+18+18+14 = 110
CUSTOMER_ACTIVITY_PLAN = [(1, 30), (2, 15), (3, 6), (6, 3), (14, 1)]
CUSTOMER_TOTAL = sum(n for _, n in CUSTOMER_ACTIVITY_PLAN)      # 55
ORDER_TOTAL = sum(q * n for q, n in CUSTOMER_ACTIVITY_PLAN)     # 110

# --- 客户当前地址分布（写 customers.province/city；覆盖 9 市 ≥ 6 市）
CUSTOMER_CITY_PLAN = {
    "深圳市": 12,   # 广东 12
    "广州市": 10,   # 广东 10
    "东莞市": 3,    # 广东 3 —— 「其他」档落在同省，用于拉开发货省分布差异（D2）
    "杭州市": 7,
    "成都市": 6,
    "苏州市": 5,
    "青岛市": 5,
    "武汉市": 4,
    "宜昌市": 3,
}   # 合计 55

# --- 订单收货地址分布（写 orders.shipping_province/city，与上表不同，否则 D2 失效）
ORDER_CITY_PLAN = {
    "深圳市": 18,
    "广州市": 13,
    "杭州市": 16,
    "成都市": 13,
    "苏州市": 13,
    "青岛市": 13,
    "武汉市": 10,
    "宜昌市": 9,
    "济南市": 5,
}   # 合计 110

# --- 商品热度权重（决定明细分配，主次分明）
PRODUCT_WEIGHT = {1: 28, 2: 20, 3: 6, 4: 5, 5: 5, 6: 11, 7: 13, 8: 12}

# ============================================================ 三、静态实体

CATEGORIES = [(1, "数码"), (2, "服饰"), (3, "食品")]

# (product_id, 名称, sku, price_cents, category_id)
# §四 8：商品编号 1~8 的业务身份不变
PRODUCTS = [
    (1, "智能手机", "SKU-001", 399900, 1),
    (2, "蓝牙耳机", "SKU-002", 29900, 1),
    (3, "运动T恤", "SKU-003", 9900, 2),
    (4, "跑步鞋", "SKU-004", 45900, 2),
    (5, "休闲帽", "SKU-005", 6900, 2),
    (6, "混合坚果", "SKU-006", 12900, 3),
    (7, "龙井茶叶", "SKU-007", 25900, 3),
    (8, "挂耳咖啡", "SKU-008", 9900, 3),
]
PRODUCT_PRICE = {pid: price for pid, _, _, price, _ in PRODUCTS}
PRODUCT_NAMES = {pid: name for pid, name, _, _, _ in PRODUCTS}

# §五 6：省市配对合法（DB 只做行级 CHECK，配对由这里兜底）
PROVINCE_CITIES = {
    "广东省": {"深圳市", "广州市", "东莞市"},
    "浙江省": {"杭州市", "宁波市", "温州市"},
    "江苏省": {"南京市", "苏州市", "无锡市"},
    "四川省": {"成都市", "绵阳市"},
    "湖北省": {"武汉市", "宜昌市"},
    "山东省": {"青岛市", "济南市"},
}
CITY_PROVINCE = {city: prov for prov, cities in PROVINCE_CITIES.items() for city in cities}

# --- D1：商品 8 的成交价口径
# 已完成订单一律 8900（唯一成交价）；未完成订单（待支付/已支付/运输中/已取消）一律 9900。
# products.price_cents = 9900 是「现价」，正是本题要防止被当历史价的那个陷阱。
P8_COMPLETED_PRICE = 8900
P8_OPEN_PRICE = 9900


def unit_price(order: dict, product_id: int) -> int:
    """该订单该商品的成交单价（分）。"""
    if product_id == 8:
        return P8_COMPLETED_PRICE if order["status"] == "已完成" else P8_OPEN_PRICE
    return PRODUCT_PRICE[product_id]


# ============================================================ 四、商品生命周期（§6.2 E1/D3/D4）

# status_after, status_changed
# 商品 4 = 7 条（全区间最多，B4）；商品 6 = 2 条且都在九月（九月最多，B4）
# 商品 4/5 末条固定「未上架」（D4）；商品 7 末条固定「已删除」（D3）
PRODUCT_STATUS_PLAN = {
    1: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-02T09:00:00"),
        ("未上架", "2026-08-08T10:00:00"), ("已上架", "2026-08-09T09:00:00"),
        ("未上架", "2026-08-22T10:00:00"), ("已上架", "2026-08-23T09:00:00")],
    2: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-03T09:00:00"),
        ("未上架", "2026-08-16T10:00:00"), ("已上架", "2026-08-17T09:00:00")],
    3: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-02T09:00:00"),
        ("未上架", "2026-08-12T10:00:00"), ("已上架", "2026-08-13T09:00:00")],
    4: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-02T09:00:00"),
        ("未上架", "2026-07-25T10:00:00"), ("已上架", "2026-07-26T09:00:00"),
        ("未上架", "2026-08-10T10:00:00"), ("已上架", "2026-08-11T09:00:00"),
        ("未上架", "2026-08-30T10:00:00")],
    5: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-05T09:00:00"),
        ("未上架", "2026-08-20T10:00:00")],
    6: [("未上架", "2026-09-10T09:00:00"), ("已上架", "2026-09-11T09:00:00")],
    7: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-03T09:00:00"),
        ("已删除", "2026-09-27T10:00:00")],
    8: [("未上架", "2026-07-01T09:00:00"), ("已上架", "2026-07-02T09:00:00")],
}

# 商品创建时刻 = 首条状态时间（§四 17b）
PRODUCT_CREATED = {pid: rows[0][1] for pid, rows in PRODUCT_STATUS_PLAN.items()}
PRODUCT_DELETED = {pid: next((ts for st, ts in rows if st == "已删除"), None)
                   for pid, rows in PRODUCT_STATUS_PLAN.items()}

# ============================================================ 五、库存参数（§6.2 F1）

# 初始库存（§四 3：每个商品有且仅有一条「初始库存」流水，作为累计起点）
INITIAL_STOCK = {1: 35, 2: 35, 3: 25, 4: 23, 5: 20, 6: 25, 7: 23, 8: 23}
# 安全水位：日终库存低于它，次日 07:30 自动补货
INV_LOW = {1: 20, 2: 20, 3: 10, 4: 8, 5: 5, 6: 10, 7: 8, 8: 8}
# 单次补货量 ≈ 该商品日终库存的极差（F1 用极差排名）
INV_RESTOCK_QTY = {1: 45, 2: 60, 3: 35, 4: 30, 5: 20, 6: 25, 7: 30, 8: 40}

# ============================================================ 六、订单布局（城市 × 状态 × 月）

# 深圳 18 / 杭州 16 全部「已完成」：
#   B3（深圳并列最高）与 B2（杭州唯一最高）只在成交口径下构造，
#   把非终态与已取消订单全部放到其他城市，使这两个城市在
#   「已完成口径」与「全量口径」下完全等价，避免口径歧义。
ORDER_PLAN = {
    "深圳市": {"已完成": {"2026-07": 4, "2026-08": 10, "2026-09": 4}},
    "杭州市": {"已完成": {"2026-07": 3, "2026-08": 9, "2026-09": 4}},
    "广州市": {"已完成": {"2026-07": 2, "2026-08": 3, "2026-09": 1},
               "已取消": {"2026-07": 1, "2026-08": 1, "2026-09": 1},
               "待支付": {"2026-09": 1}, "已支付": {"2026-09": 1}, "运输中": {"2026-09": 2}},
    "成都市": {"已完成": {"2026-07": 2, "2026-08": 3, "2026-09": 1},
               "已取消": {"2026-07": 1, "2026-08": 1, "2026-09": 1},
               "待支付": {"2026-09": 1}, "已支付": {"2026-09": 2}, "运输中": {"2026-09": 1}},
    "苏州市": {"已完成": {"2026-07": 3, "2026-08": 2, "2026-09": 1},
               "已取消": {"2026-07": 1, "2026-08": 1},
               "待支付": {"2026-09": 2}, "已支付": {"2026-09": 1}, "运输中": {"2026-09": 2}},
    "青岛市": {"已完成": {"2026-07": 2, "2026-08": 3, "2026-09": 1},
               "已取消": {"2026-07": 1, "2026-08": 1, "2026-09": 1},
               "待支付": {"2026-09": 2}, "已支付": {"2026-09": 1}, "运输中": {"2026-09": 1}},
    "武汉市": {"已完成": {"2026-07": 2, "2026-08": 1, "2026-09": 1},
               "已取消": {"2026-07": 1, "2026-08": 1},
               "待支付": {"2026-09": 1}, "已支付": {"2026-09": 1}, "运输中": {"2026-09": 2}},
    "宜昌市": {"已完成": {"2026-07": 1, "2026-08": 2},
               "已取消": {"2026-08": 1, "2026-09": 1},
               "待支付": {"2026-09": 1}, "已支付": {"2026-09": 2}, "运输中": {"2026-09": 1}},
    "济南市": {"已完成": {"2026-07": 1},
               "已取消": {"2026-08": 1, "2026-09": 1},
               "待支付": {"2026-09": 1}, "已支付": {"2026-09": 1}},
}

# --- 显式场景订单：深圳 18 单（B3 并列最高）+ 杭州 16 单（B2 唯一最高）
# 每单的 (product_id, quantity) 完全写死，不再随机——这是「构造」而非「筛选」。
# 深圳：商品 1 与商品 2 的成交件数精确并列 28 件，其余商品 ≤ 5 件。
# 杭州：商品 1 出现 13 单 / 33 件，笔数与件数双料唯一最高。
EXPLICIT_ORDERS = [
    # ---------------- 深圳市 18 单（全部已完成）----------------
    {"city": "深圳市", "status": "已完成", "month": "2026-07", "time": "2026-07-04T10:20:00",
     "items": [(1, 2), (2, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-07", "time": "2026-07-11T15:40:00",
     "items": [(1, 3), (3, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-07", "time": "2026-07-18T11:05:00",
     "items": [(2, 3), (7, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-07", "time": "2026-07-24T20:10:00",
     "items": [(1, 2), (2, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-02T09:50:00",
     "items": [(1, 3), (2, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-03T14:15:00",
     "items": [(1, 2), (8, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-04T19:30:00",
     "items": [(2, 4), (7, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-05T10:45:00",
     "items": [(1, 2), (2, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-06T16:00:00",
     "items": [(1, 3), (3, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-07T12:35:00",
     "items": [(2, 3), (5, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-18T09:25:00",
     "items": [(1, 2), (7, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-19T17:50:00",
     "items": [(2, 2), (8, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-24T13:10:00",
     "items": [(1, 4), (3, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-08", "time": "2026-08-25T18:40:00",
     "items": [(2, 1), (8, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-09", "time": "2026-09-12T10:30:00",
     "items": [(1, 3), (6, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-09", "time": "2026-09-15T14:55:00",
     "items": [(2, 4), (8, 1)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-09", "time": "2026-09-17T19:05:00",
     "items": [(1, 2), (2, 2)]},
    {"city": "深圳市", "status": "已完成", "month": "2026-09", "time": "2026-09-19T11:20:00",
     "items": [(2, 2), (7, 1)]},
    # ---------------- 杭州市 16 单（全部已完成）----------------
    {"city": "杭州市", "status": "已完成", "month": "2026-07", "time": "2026-07-05T14:20:00",
     "items": [(1, 3), (2, 2)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-07", "time": "2026-07-12T09:40:00",
     "items": [(1, 2), (3, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-07", "time": "2026-07-19T16:30:00",
     "items": [(4, 1), (7, 2)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-03T10:10:00",
     "items": [(1, 3), (8, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-04T15:25:00",
     "items": [(1, 2), (2, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-05T11:50:00",
     "items": [(1, 4), (7, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-06T18:15:00",
     "items": [(1, 2), (5, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-07T13:35:00",
     "items": [(8, 1), (5, 2)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-18T09:30:00",
     "items": [(1, 3), (3, 2)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-19T20:00:00",
     "items": [(1, 1), (8, 2)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-24T12:40:00",
     "items": [(2, 2), (7, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-08", "time": "2026-08-25T17:10:00",
     "items": [(1, 2), (3, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-09", "time": "2026-09-13T10:05:00",
     "items": [(1, 3), (6, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-09", "time": "2026-09-14T15:45:00",
     "items": [(1, 2), (8, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-09", "time": "2026-09-16T19:20:00",
     "items": [(1, 4), (2, 1)]},
    {"city": "杭州市", "status": "已完成", "month": "2026-09", "time": "2026-09-18T11:15:00",
     "items": [(1, 2), (7, 1)]},
]

# --- 已取消订单的显式构造（B1：商品 2/4/8 在已取消池中各占 6 单，其余商品绝不出现）
# 17 单 / 18 条明细（其中 1 单 2 条）。商品 4 全部落在 7-8 月，九月不含商品 4（A1）。
CANCELLED_ORDER_TEMPLATES = [
    {"city": "广州市", "status": "已取消", "month": "2026-07", "time": "2026-07-06T11:30:00",
     "items": [(4, 1)]},
    {"city": "成都市", "status": "已取消", "month": "2026-07", "time": "2026-07-09T16:20:00",
     "items": [(4, 1)]},
    {"city": "苏州市", "status": "已取消", "month": "2026-07", "time": "2026-07-13T10:05:00",
     "items": [(2, 1)]},
    {"city": "青岛市", "status": "已取消", "month": "2026-07", "time": "2026-07-16T14:50:00",
     "items": [(8, 2)]},
    {"city": "武汉市", "status": "已取消", "month": "2026-07", "time": "2026-07-22T19:35:00",
     "items": [(4, 2)]},
    {"city": "广州市", "status": "已取消", "month": "2026-08", "time": "2026-08-01T09:15:00",
     "items": [(4, 1)]},
    {"city": "成都市", "status": "已取消", "month": "2026-08", "time": "2026-08-02T13:40:00",
     "items": [(4, 1)]},
    {"city": "苏州市", "status": "已取消", "month": "2026-08", "time": "2026-08-04T18:25:00",
     "items": [(4, 1)]},
    {"city": "青岛市", "status": "已取消", "month": "2026-08", "time": "2026-08-05T11:00:00",
     "items": [(2, 2)]},
    {"city": "武汉市", "status": "已取消", "month": "2026-08", "time": "2026-08-06T15:35:00",
     "items": [(8, 1)]},
    {"city": "宜昌市", "status": "已取消", "month": "2026-08", "time": "2026-08-07T10:50:00",
     "items": [(2, 1)]},
    {"city": "济南市", "status": "已取消", "month": "2026-08", "time": "2026-08-08T17:05:00",
     "items": [(2, 3), (8, 1)]},
    {"city": "广州市", "status": "已取消", "month": "2026-09", "time": "2026-09-04T12:10:00",
     "items": [(2, 1)]},
    {"city": "成都市", "status": "已取消", "month": "2026-09", "time": "2026-09-08T16:45:00",
     "items": [(2, 2)]},
    {"city": "青岛市", "status": "已取消", "month": "2026-09", "time": "2026-09-11T10:25:00",
     "items": [(8, 1)]},
    {"city": "宜昌市", "status": "已取消", "month": "2026-09", "time": "2026-09-15T14:05:00",
     "items": [(8, 2)]},
    {"city": "济南市", "status": "已取消", "month": "2026-09", "time": "2026-09-19T18:55:00",
     "items": [(8, 1)]},
]

# --- 随机单每月的明细条数配额（显式单与取消单之外的剩余空间）
# {"已完成","2026-07"} 13 单 45 条；{"已完成","2026-08"} 14 单 49 条；
# {"已完成","2026-09"} 5 单 18 条；三个非终态各 9 单共 54 条（每单 2 条）。
RANDOM_ITEM_PLAN = {
    ("已完成", "2026-07"): 45,
    ("已完成", "2026-08"): 49,
    ("已完成", "2026-09"): 18,
    ("待支付", "2026-09"): 18,
    ("已支付", "2026-09"): 18,
    ("运输中", "2026-09"): 18,
}

# --- 九月随机「已完成」单的下单日期窗口
# 起点取 09-11：商品 6（混合坚果）09-11 才上架，九月成交单必须能看到它；
# 终点取 09-25：完成时刻落在 09-27 左右，使 C1 的 09-24~09-30 窗口内确实存在
# 「已完成但没有退货」的订单——否则「窗口内无退货」会退化成空集，失去区分度。
DATE_WINDOWS = {
    ("已完成", "2026-07"): (date(2026, 7, 3), date(2026, 7, 30)),
    ("已完成", "2026-08"): (date(2026, 8, 2), date(2026, 8, 29)),
    ("已完成", "2026-09"): (date(2026, 9, 11), date(2026, 9, 25)),
    ("待支付", "2026-09"): (date(2026, 9, 27), date(2026, 9, 30)),
    ("已支付", "2026-09"): (date(2026, 9, 25), date(2026, 9, 30)),
    ("运输中", "2026-09"): (date(2026, 9, 23), date(2026, 9, 29)),
}

# --- 城市权重加成：让深圳 / 杭州之外的随机单也照热度分布走
CITY_BOOST = {
    "深圳市": {1: 2.0, 2: 2.0},
    "杭州市": {1: 2.0},
}

# ============================================================ 七、§6.2 场景占用与冲突检查

# 每个场景声明它占用了什么，随机填充只能使用剩余空间。
SCENARIO_OCCUPANCY = {
    "A1": {"name": "指定商品九月零笔数零销量", "sep_excluded_products": {4, 5}},
    "A2": {"name": "城市维度零值有意义", "city_with_completed": "青岛市"},
    "B1": {"name": "已取消订单涉及商品完全并列", "cancelled_products": {2, 4, 8},
           "cancelled_product_month_scope": {"商品4": {"2026-07", "2026-08"}}},
    "B2": {"name": "城市唯一最高（杭州）", "city": "杭州市", "top_product": 1},
    "B3": {"name": "城市并列最高（深圳）", "city": "深圳市", "tied_products": (1, 2)},
    "B4": {"name": "双最高（九月与全区间）", "month": "2026-09",
           "month_top_product": 6, "global_top_product": 4},
    "C1": {"name": "窗口内无退货", "window": ("2026-09-24", "2026-09-30")},
    "C2": {"name": "路线决策：只做空结果题", "route": "empty_result_only"},
    "D1": {"name": "成交价唯一性", "product": 8},
    "D2": {"name": "发货省分布 ≠ 客户所在省分布", "province": "广东省", "min_gap_pp": 15.0},
    "D3": {"name": "已删除必须是最后一条", "product": 7},
    "D4": {"name": "至少 2 个未上架", "products": {4, 5}},
    "E1": {"name": "快照天数差 ≥ 60", "short_product": 6, "min_day_gap": 60},
    "F1": {"name": "库存极差最高唯一且领先", "product": 2, "min_lead": 5},
    "F2": {"name": "九月销量最高唯一", "month": "2026-09", "product": 1},
}


class SpecViolation(RuntimeError):
    """构造逻辑违反规格（不是数据巧合）。"""


def check_scenario_conflicts() -> list[tuple[str, str, str]]:
    """§6.2：生成前跑一遍占用集合两两冲突检查；冲突直接抛异常。

    返回已检查的 (场景A, 场景B, 结论) 清单，供摘要打印。
    """
    occ = SCENARIO_OCCUPANCY
    checked: list[tuple[str, str, str]] = []
    problems: list[str] = []

    def rule(a: str, b: str, ok: bool, why: str) -> None:
        checked.append((a, b, ("不冲突：" if ok else "冲突：") + why))
        if not ok:
            problems.append(f"{a} × {b}：{why}")

    # A1（九月排除 4、5）不能撞上任何一个「九月必须有某商品」的场景
    sep_excluded = occ["A1"]["sep_excluded_products"]
    rule("A1", "F2", occ["F2"]["product"] not in sep_excluded,
         f"F2 要求九月最高是商品{occ['F2']['product']}，A1 排除 {sorted(sep_excluded)}")
    rule("A1", "B4", occ["B4"]["month_top_product"] not in sep_excluded,
         f"B4 要求九月变动最多是商品{occ['B4']['month_top_product']}")
    rule("A1", "B2", occ["B2"]["top_product"] not in sep_excluded,
         f"B2 要求 {occ['B2']['city']} 最高是商品{occ['B2']['top_product']}")
    rule("A1", "B3", not (set(occ["B3"]["tied_products"]) & sep_excluded),
         f"B3 并列组 {occ['B3']['tied_products']}")

    # B1（已取消池只允许 2/4/8）与 A1（九月不含 4、5）：
    # 商品 4 被 B1 占用，因此九月已取消单里绝不能出现商品 4 —— 用月份范围声明消解。
    p4_months = occ["B1"]["cancelled_product_month_scope"]["商品4"]
    rule("B1", "A1", "2026-09" not in p4_months,
         "商品4 的已取消名额只落在 2026-07/2026-08，九月已取消池不含商品4")

    # D4（4、5 当前未上架）与 A1（九月零销量）同向，且都不能与「九月要卖」冲突
    rule("D4", "A1", occ["D4"]["products"] == sep_excluded,
         "D4 的未上架商品与 A1 的九月排除商品是同一集合，语义一致（一个说状态、一个说销量）")
    rule("D4", "F2", occ["F2"]["product"] not in occ["D4"]["products"], "F2 的商品不在未上架集合内")

    # B2 / B3 是不同城市，互不侵占
    rule("B2", "B3", occ["B2"]["city"] != occ["B3"]["city"],
         f"B2={occ['B2']['city']}、B3={occ['B3']['city']}")
    rule("B2", "B3", set(occ["B3"]["tied_products"]) == {1, 2},
         "深圳并列组固定为商品1/2，杭州最高固定为商品1，城市不同故不矛盾")

    # B4 的两个最高必须是不同商品（否则「月份最高 ≠ 全区间最高」不成立）
    rule("B4", "B4", occ["B4"]["month_top_product"] != occ["B4"]["global_top_product"],
         f"九月最高=商品{occ['B4']['month_top_product']}，全区间最高=商品{occ['B4']['global_top_product']}")

    # D1（商品8 成交价锁定）与 B1（已取消池含商品8）：
    # 商品8 在已取消单里属于「未完成订单」，正好作为第二种价的合法来源。
    rule("D1", "B1", occ["D1"]["product"] in occ["B1"]["cancelled_products"],
         "商品8 的第二种价由已取消单承载，与 B1 的并列组同源，属互补而非冲突")

    # D3（商品7 删除）不能被任何「某月要卖商品7」的场景占用
    for scen in ("B2", "B3", "F2"):
        if scen == "B3":
            busy = set(occ[scen]["tied_products"])
        elif scen == "F2":
            busy = {occ[scen]["product"]}
        else:
            busy = {occ[scen]["top_product"]}
        rule("D3", scen, occ["D3"]["product"] not in busy, "商品7 不是并列/最高组的主角")

    # E1（商品6 晚 71 天创建）与 F1（商品2 极差最高）
    rule("E1", "F1", occ["E1"]["short_product"] != occ["F1"]["product"],
         "商品6 只管快照天数，商品2 只管库存极差")
    rule("E1", "A1", occ["E1"]["short_product"] not in sep_excluded,
         "商品6 九月才创建，必须能出现在九月订单里")

    # F1 与 F2 是不同口径（库存极差 vs 九月销量），不同商品
    rule("F1", "F2", occ["F1"]["product"] != occ["F2"]["product"], "商品2 与商品1 分工")

    if problems:
        raise SpecViolation("§6.2 场景占用冲突：\n  " + "\n  ".join(problems))
    return checked


# ============================================================ 八、工具函数

def parse_dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")


def fmt_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def next_day_boundary(day: date) -> str:
    """业务日 day 的日界：D+1 日 00:00:00（严格小于它才计入当日快照）。"""
    return f"{(day + timedelta(days=1)).isoformat()}T00:00:00"


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def rand_day(start: date, end: date, rng: random.Random) -> date:
    span = (end - start).days
    return start + timedelta(days=rng.randint(0, span))


def rand_time_on(day: date, rng: random.Random, lo: int = 9, hi: int = 21) -> str:
    hour = rng.randint(lo, hi - 1)
    minute = rng.randint(0, 59)
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"


def weighted_pick(items, weights, rng: random.Random):
    total = sum(weights)
    point = rng.random() * total
    acc = 0.0
    for item, weight in zip(items, weights):
        acc += weight
        if point < acc:
            return item
    return items[-1]


def validate_quota_plan() -> list[str]:
    """§6.1 配额表自洽性：订单状态、月份、状态×月份三张表必须同时成立。

    这是「先定数量，不先定内容」的落地方式——ORDER_PLAN 是配额表拉直后的解，
    这里校验它没有把配额表写错。
    """
    status_seen, month_seen, cross_seen = Counter(), Counter(), Counter()
    for city, status_map in ORDER_PLAN.items():
        for status, month_map in status_map.items():
            for month, count in month_map.items():
                status_seen[status] += count
                month_seen[month] += count
                cross_seen[(status, month)] += count

    problems = []
    for status, want in STATUS_QUOTA.items():
        if status_seen.get(status, 0) != want:
            problems.append(f"状态「{status}」{status_seen.get(status, 0)} ≠ {want}")
    for month, want in MONTH_ORDER_QUOTA.items():
        if month_seen.get(month, 0) != want:
            problems.append(f"月份 {month} {month_seen.get(month, 0)} ≠ {want}")
    for (status, month), want in STATUS_MONTH_QUOTA.items():
        if cross_seen.get((status, month), 0) != want:
            problems.append(f"{status}/{month} {cross_seen.get((status, month), 0)} ≠ {want}")
    if sum(ORDER_CITY_PLAN.values()) != ORDER_TOTAL:
        problems.append(f"订单城市分布合计 {sum(ORDER_CITY_PLAN.values())} ≠ {ORDER_TOTAL}")
    if sum(CUSTOMER_CITY_PLAN.values()) != CUSTOMER_TOTAL:
        problems.append(f"客户城市分布合计 {sum(CUSTOMER_CITY_PLAN.values())} ≠ {CUSTOMER_TOTAL}")
    if sum(q * n for q, n in CUSTOMER_ACTIVITY_PLAN) != ORDER_TOTAL:
        problems.append("客户复购配额合计 ≠ 订单总量")
    if problems:
        raise SpecViolation("§6.1 配额表不自洽：\n  " + "\n  ".join(problems))
    return [
        f"订单状态 {dict(status_seen)}",
        f"月份 {dict(month_seen)}",
        f"状态×月份 {len(cross_seen)} 组全部对齐",
        f"订单城市分布 {sum(ORDER_CITY_PLAN.values())} 单、"
        f"客户城市分布 {sum(CUSTOMER_CITY_PLAN.values())} 人",
    ]


def validate_region_mapping() -> None:
    """schema.sql 声明的合法省市，必须与 PROVINCE_CITIES 双向一致。

    两种不一致的后果不同：
      · schema 有、映射表没有 → 该值永远不会被生成（静默失效，最难发现）
      · 映射表有、schema 没有 → 造数插库时被 CHECK 拦截（会当场炸）
    所以两个方向都要查。另外 schema 里 province / city 的 CHECK 各写了两遍
    （customers 一份、orders.shipping_* 一份），两处列表必须完全一致。
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8")

    # "province IN (...)" 与 "shipping_province IN (...)" 都能命中（后缀相同）
    prov_blocks = re.findall(r"province\s+IN\s*\(([^)]*)\)", text, re.S | re.I)
    city_blocks = re.findall(r"city\s+IN\s*\(([^)]*)\)", text, re.S | re.I)

    if not prov_blocks or not city_blocks:
        raise SpecViolation("无法从 schema.sql 解析出省/市 CHECK 列表，正则可能需要更新")

    def _parse(block: str) -> set[str]:
        return {part.strip().strip("'\"") for part in block.split(",") if part.strip()}

    schema_provinces = _parse(prov_blocks[0])
    schema_cities = _parse(city_blocks[0])
    problems: list[str] = []

    # 同一份合法值在 schema 里应出现两处，必须一致（否则两表规则不同，更隐蔽）
    for idx, block in enumerate(prov_blocks[1:], start=2):
        if _parse(block) != schema_provinces:
            problems.append(f"schema.sql 第 {idx} 处 province 列表与第 1 处不一致")
    for idx, block in enumerate(city_blocks[1:], start=2):
        if _parse(block) != schema_cities:
            problems.append(f"schema.sql 第 {idx} 处 city 列表与第 1 处不一致")

    for label, schema_set, mapping_set in (
        ("省份", schema_provinces, set(PROVINCE_CITIES.keys())),
        ("城市", schema_cities, set(CITY_PROVINCE.keys())),
    ):
        missing = schema_set - mapping_set
        extra = mapping_set - schema_set
        if missing:
            problems.append(
                f"{label}：schema 有、映射表没有 {sorted(missing)}"
                "——这些值永远不会被生成")
        if extra:
            problems.append(
                f"{label}：映射表有、schema 没有 {sorted(extra)}"
                "——造数会被 CHECK 拦截")

    # 映射表内部：同一城市不能同时挂在两个省下
    owner: dict[str, str] = {}
    for prov, cities in PROVINCE_CITIES.items():
        for city in cities:
            if city in owner and owner[city] != prov:
                problems.append(f"城市 {city} 同时属于「{owner[city]}」和「{prov}」")
            owner[city] = prov

    if problems:
        raise SpecViolation("省市映射与 schema.sql 不一致：\n  " + "\n  ".join(problems))


def distribute_counts(total: int, count: int, rng: random.Random,
                      lo: int = 1, hi: int = 4) -> list[int]:
    """把 total 条明细分给 count 张单，每单 lo~hi 条。"""
    if not (lo * count <= total <= hi * count):
        raise SpecViolation(f"明细配额不可行：{total} 条给 {count} 单，每单 {lo}~{hi}")
    values = [lo] * count
    rest = total - lo * count
    while rest > 0:
        candidates = [idx for idx, value in enumerate(values) if value < hi]
        idx = candidates[rng.randrange(len(candidates))]
        values[idx] += 1
        rest -= 1
    return values


# ============================================================ 九、生命周期与可用性

def build_lifecycle() -> dict:
    """由商品状态历史派生：创建时刻、删除时刻、已上架区间、快照日区间。"""
    lifecycle = {}
    for product_id, rows in PRODUCT_STATUS_PLAN.items():
        created = rows[0][1]
        deleted = next((ts for status, ts in rows if status == "已删除"), None)
        listed, open_at = [], None
        for status, ts in rows:
            if status == "已上架":
                open_at = ts
            elif status in ("未上架", "已删除") and open_at is not None:
                listed.append((open_at, ts))
                open_at = None
        if open_at is not None:
            listed.append((open_at, None))
        first_day = date.fromisoformat(created[:10])
        if deleted:
            last_day = date.fromisoformat(deleted[:10]) - timedelta(days=1)
        else:
            last_day = DATA_END
        lifecycle[product_id] = {
            "created_at": created,
            "deleted_at": deleted,
            "listed": listed,
            "first_day": first_day,
            "last_day": last_day,
            "current_status": rows[-1][0],
        }
    return lifecycle


def product_available(product_id: int, moment: str, lifecycle: dict) -> bool:
    """该时刻商品是否可被下单：未创建 / 已删除 / 未上架区间都不行。"""
    info = lifecycle[product_id]
    if moment < info["created_at"]:
        return False
    if info["deleted_at"] and moment >= info["deleted_at"]:
        return False
    for start, end in info["listed"]:
        if moment >= start and (end is None or moment < end):
            return True
    return False


def product_span(product_id: int, lifecycle: dict) -> tuple[date, date]:
    """快照闭区间 [创建日, 删除日−1 或 DATA_END]（§四 19）。"""
    info = lifecycle[product_id]
    return info["first_day"], info["last_day"]


# ============================================================ 十、客户

def build_customers(rng: random.Random) -> list[dict]:
    """55 个客户：城市按配额表，复购频次按 CUSTOMER_ACTIVITY_PLAN 打散后随机分配。"""
    city_seq = []
    for city, count in CUSTOMER_CITY_PLAN.items():
        city_seq.extend([city] * count)
    if len(city_seq) != CUSTOMER_TOTAL:
        raise SpecViolation(f"客户城市配额合计 {len(city_seq)} ≠ {CUSTOMER_TOTAL}")

    quota_seq = []
    for orders_per_customer, people in CUSTOMER_ACTIVITY_PLAN:
        quota_seq.extend([orders_per_customer] * people)
    if sum(quota_seq) != ORDER_TOTAL:
        raise SpecViolation(f"客户订单配额合计 {sum(quota_seq)} ≠ {ORDER_TOTAL}")

    rng.shuffle(city_seq)
    rng.shuffle(quota_seq)

    customers = []
    for idx in range(CUSTOMER_TOTAL):
        city = city_seq[idx]
        # 注册时间落在数据开始之前，保证早于首单（§四 20）
        day = date(2026, 2, 1) + timedelta(days=rng.randint(0, 149))
        customers.append({
            "customer_id": idx + 1,
            "customer_name": f"客户{idx + 1:03d}",
            "registered_at": f"{day.isoformat()}T{rng.randint(9, 20):02d}:{rng.randint(0, 59):02d}:00",
            "province": CITY_PROVINCE[city],
            "city": city,
            "quota": quota_seq[idx],
        })
    if sum(c["quota"] for c in customers) != ORDER_TOTAL:
        raise SpecViolation("客户订单配额合计与订单总量不一致")
    return customers


# ============================================================ 十一、订单骨架

def build_order_skeleton(rng: random.Random) -> list[dict]:
    """先按 (城市, 状态, 月份) 三张配额表铺出 110 个坑位，再填日期与明细条数。"""
    orders: list[dict] = []

    # 1) 显式场景单（深圳 18 + 杭州 16）
    for tpl in EXPLICIT_ORDERS:
        orders.append({
            "city": tpl["city"], "status": tpl["status"], "month": tpl["month"],
            "order_time": tpl["time"],
            "items": [[pid, qty] for pid, qty in tpl["items"]],
            "explicit": True,
        })

    # 2) 已取消单（B1 的并列组，明细写死）
    for tpl in CANCELLED_ORDER_TEMPLATES:
        orders.append({
            "city": tpl["city"], "status": tpl["status"], "month": tpl["month"],
            "order_time": tpl["time"],
            "items": [[pid, qty] for pid, qty in tpl["items"]],
            "explicit": True,
        })

    # 3) 其余坑位：配额表减去已显式占用的部分
    used = Counter((o["city"], o["status"], o["month"]) for o in orders)
    random_orders: list[dict] = []
    for city, status_map in ORDER_PLAN.items():
        for status, month_map in status_map.items():
            for month, total in month_map.items():
                remain = total - used[(city, status, month)]
                if remain < 0:
                    raise SpecViolation(f"{city}/{status}/{month} 显式单超出配额")
                for _ in range(remain):
                    random_orders.append({"city": city, "status": status, "month": month,
                                          "items": None, "explicit": False})

    # 4) 随机单的下单日期
    for order in random_orders:
        start, end = DATE_WINDOWS[(order["status"], order["month"])]
        day = rand_day(start, end, rng)
        order["order_time"] = rand_time_on(day, rng)

    # 5) 随机单的明细条数（配额内的具体条数可随机，总条数不可随机）
    groups = defaultdict(list)
    for order in random_orders:
        groups[(order["status"], order["month"])].append(order)
    for key, group in groups.items():
        total_items = RANDOM_ITEM_PLAN[key]
        counts = distribute_counts(total_items, len(group), rng, lo=1, hi=4)
        rng.shuffle(counts)
        for order, count in zip(group, counts):
            order["n_items"] = count

    orders.extend(random_orders)
    if len(orders) != ORDER_TOTAL:
        raise SpecViolation(f"订单坑位 {len(orders)} ≠ {ORDER_TOTAL}")
    return orders


def assign_customers(orders: list[dict], customers: list[dict], rng: random.Random) -> None:
    """把订单分给客户：先钉住 D2 需要的「收货地 = 客户所在地」样本，其余按剩余配额随机。"""
    remaining = {c["customer_id"]: c["quota"] for c in customers}
    by_city = defaultdict(list)
    for customer in customers:
        by_city[customer["city"]].append(customer["customer_id"])

    completed = [o for o in orders if o["status"] == "已完成"]
    rng.shuffle(completed)
    matched_target = 6
    matched = 0
    for order in completed:
        if matched >= matched_target:
            break
        candidates = [cid for cid in by_city[order["city"]] if remaining[cid] > 0]
        if not candidates:
            continue
        cid = candidates[rng.randrange(len(candidates))]
        order["customer_id"] = cid
        remaining[cid] -= 1
        matched += 1

    pool = [o for o in orders if o.get("customer_id") is None]
    for order in pool:
        candidates = [cid for cid, left in remaining.items() if left > 0]
        if not candidates:
            raise SpecViolation("客户配额耗尽，仍有订单未分配")
        weights = [remaining[cid] for cid in candidates]
        cid = weighted_pick(candidates, weights, rng)
        order["customer_id"] = cid
        remaining[cid] -= 1

    leftovers = {cid: left for cid, left in remaining.items() if left != 0}
    if leftovers:
        raise SpecViolation(f"客户配额未用尽：{leftovers}")


# ============================================================ 十二、明细分配（配额内的剩余空间）

def month_product_quotas(month_orders: list[dict], total_items: int,
                         lifecycle: dict) -> dict:
    """某月随机单的 (商品) 配额。

    两条硬边界让配额必然可落地：
      ① 同一单不重复出现同一商品（§四 12）→ 某商品在该月的上限 = 该月「该商品可用」的单数；
      ② 单商品在该月的比例仍按热度权重（D'Hondt：权重 / (已分+1) 取大，撞上限则跳过）。
    """
    capacity = {}
    for product_id in PRODUCT_WEIGHT:
        usable_orders = sum(1 for o in month_orders
                            if product_available(product_id, o["order_time"], lifecycle))
        if usable_orders:
            capacity[product_id] = usable_orders
    if not capacity:
        raise SpecViolation(f"{month_orders[0]['month']} 没有任何商品可用")

    counts = {pid: 0 for pid in capacity}
    for _ in range(total_items):
        candidates = [pid for pid in capacity if counts[pid] < capacity[pid]]
        if not candidates:
            raise SpecViolation("本月明细配额超过可放置上限")
        pid = max(candidates,
                  key=lambda p: (PRODUCT_WEIGHT[p] / (counts[p] + 1), -p))
        counts[pid] += 1
    return counts


def allocate_products(orders: list[dict], lifecycle: dict, rng: random.Random) -> list[str]:
    """只给随机单分配商品；显式单与取消单的明细不动。

    贪心按「稀缺度」分配：pool[pid] / 还能吃下它的单数，避免早期订单把稀缺商品
    占满后剩下的单配不齐。返回回填说明（正常情况下为空）。
    """
    random_orders = [o for o in orders if o["items"] is None]
    by_group = defaultdict(list)
    for order in random_orders:
        by_group[(order["status"], order["month"])].append(order)

    notes: list[str] = []
    for group_key, group in by_group.items():
        total_items = sum(o["n_items"] for o in group)
        pool = month_product_quotas(group, total_items, lifecycle)

        indices = list(range(len(group)))
        rng.shuffle(indices)
        eligible = {pid: {i for i in indices
                          if product_available(pid, group[i]["order_time"], lifecycle)}
                    for pid in pool}
        assigned = {i: set() for i in indices}

        for pos, i in enumerate(indices):
            order = group[i]
            while len(assigned[i]) < order["n_items"]:
                candidates = [pid for pid in pool
                              if pool[pid] > 0 and pid not in assigned[i] and i in eligible[pid]]
                if not candidates:
                    break

                def scarcity(pid):
                    future = sum(1 for j in indices[pos:]
                                 if j in eligible[pid] and pid not in assigned[j])
                    boost = CITY_BOOST.get(order["city"], {}).get(pid, 1.0)
                    return pool[pid] * boost / max(1, future)

                picked = max(candidates, key=lambda p: (scarcity(p), PRODUCT_WEIGHT[p], -p))
                pool[picked] -= 1
                assigned[i].add(picked)

        for i in indices:
            group[i]["items"] = [[pid, 0] for pid in sorted(assigned[i])]

        # 回填：配额里还有剩下的，找本月还能放该商品的单补一条明细
        for product_id, left in list(pool.items()):
            while left > 0:
                placed = False
                for limit in (5, 6):
                    for i in indices:
                        order = group[i]
                        if product_id in [pid for pid, _ in order["items"]]:
                            continue
                        if len(order["items"]) >= limit:
                            continue
                        if not product_available(product_id, order["order_time"], lifecycle):
                            continue
                        order["items"].append([product_id, 0])
                        left -= 1
                        placed = True
                        break
                    if placed:
                        break
                if not placed:
                    notes.append(f"{group_key[1]}/{group_key[0]} 商品{product_id} 有 {left} 条配额"
                                 f"受可用性/去重约束未能落地，已并入其他商品")
                    break
            pool[product_id] = 0
    return notes


def apply_quantities(orders: list[dict], rng: random.Random) -> None:
    """给随机单的明细生成购买数量；显式单的数量是写死的。"""
    for order in orders:
        for item in order["items"]:
            if item[1] == 0:
                item[1] = rng.choice([1, 1, 1, 2, 2, 3])


def total_qty_by_product(orders) -> dict:
    totals = defaultdict(int)
    for order in orders:
        for pid, qty in order["items"]:
            totals[pid] += qty
    return totals


def ensure_september_top(orders: list[dict]) -> None:
    """F2 兜底：九月商品 1 的销量必须唯一最高（已完成口径与全量口径都要成立）。

    深圳的并列组不能动，否则 B3 被破坏；杭州不动（商品1 本来就是它的最高）。
    """
    sep_orders = [o for o in orders if o["order_time"][:7] == "2026-09"]
    scopes = [("已完成口径", [o for o in sep_orders if o["status"] == "已完成"]),
              ("全量口径", sep_orders)]
    for label, scope in scopes:
        totals = total_qty_by_product(scope)
        others = [qty for pid, qty in totals.items() if pid != 1]
        if not others:
            continue
        need = max(others) + 3 - totals.get(1, 0)
        if need <= 0:
            continue
        slots = [(o, idx) for o in scope if o["city"] != "深圳市"
                 for idx, (pid, _) in enumerate(o["items"]) if pid == 1]
        if not slots:
            raise SpecViolation(f"{label} 九月没有可加量的商品1 明细")
        cursor = 0
        while need > 0 and cursor < len(slots) * 5:
            order, idx = slots[cursor % len(slots)]
            if order["items"][idx][1] < 6:
                order["items"][idx][1] += 1
                need -= 1
            cursor += 1
        if need > 0:
            raise SpecViolation(f"{label} 九月商品1 加量失败，仍差 {need} 件")


# ============================================================ 十三、状态历史

# §6.6 订单状态转移矩阵（from → (to, reason_code)）
ORDER_FLOW = {
    "待支付": [("已支付", "paid"), ("已取消", "timeout_cancel")],
    "已支付": [("运输中", "shipped"), ("已取消", "refund_before_ship")],
    "运输中": [("已完成", "delivered"), ("已取消", "refund_in_transit")],
    "已完成": [],
    "已取消": [],
}
# 本数据集的状态路径（与 §二 的 352 条历史精确对应：66×4 + 17×2 + 9×1 + 9×2 + 9×3）
STATUS_PATH = {
    "待支付": [],
    "已支付": [("已支付", "paid")],
    "运输中": [("已支付", "paid"), ("运输中", "shipped")],
    "已完成": [("已支付", "paid"), ("运输中", "shipped"), ("已完成", "delivered")],
    "已取消": [("已取消", "timeout_cancel")],
}
REASON_LABEL = {
    "created": "下单成功，等待支付",
    "paid": "支付成功",
    "shipped": "仓库发货",
    "delivered": "已签收",
    "timeout_cancel": "超时未支付自动取消",
    "refund_before_ship": "发货前申请退款",
    "refund_in_transit": "运输途中退款",
}

# 相邻状态之间的时间跨度（分钟）
STEP_MINUTES = {
    "paid": (20, 180),
    "shipped": (360, 1200),
    "delivered": (1200, 3000),
    "timeout_cancel": (60, 1080),
    "refund_before_ship": (120, 1440),
    "refund_in_transit": (1440, 4320),
}


def build_histories(orders: list[dict], rng: random.Random) -> None:
    """为每单生成状态历史；时间严格递增，且不越过数据末尾（商品 7 还须早于删除）。"""
    for order in orders:
        created = parse_dt(order["order_time"])
        chain = [(None, "待支付", created, "created")]
        cursor = created
        for to_status, reason in STATUS_PATH[order["status"]]:
            low, high = STEP_MINUTES[reason]
            cursor = cursor + timedelta(minutes=rng.randint(low, high))
            chain.append((chain[-1][1], to_status, cursor, reason))

        deadline = parse_dt(DATA_END_TS)
        if any(pid == 7 for pid, _ in order["items"]):
            deadline = min(deadline, parse_dt(PRODUCT_DELETED[7]))

        last = chain[-1][2]
        if last > deadline:
            # 压缩：按比例缩到 deadline 之内，保持严格递增（语义上相当于物流更快）
            span = (last - created).total_seconds()
            room = (deadline - created).total_seconds()
            if room <= 60 * (len(chain) - 1):
                raise SpecViolation(f"订单 {order['order_time']} 的流程装不进时间窗")
            factor = (room * 0.9) / span
            rebuilt = [(None, "待支付", created, "created")]
            cursor = created
            for to_status, reason in STATUS_PATH[order["status"]]:
                low, high = STEP_MINUTES[reason]
                step = max(5, int(rng.randint(low, high) * factor))
                cursor = cursor + timedelta(minutes=step)
                rebuilt.append((rebuilt[-1][1], to_status, cursor, reason))
            chain = rebuilt

        order["history"] = []
        for from_status, to_status, moment, reason in chain:
            order["history"].append({
                "from_status": from_status,
                "to_status": to_status,
                "changed_at": fmt_dt(moment),
                "reason_code": reason,
                "change_note": REASON_LABEL[reason],
            })

        if order["status"] == "已完成":
            order["completed_at"] = chain[-1][2].strftime("%Y-%m-%dT%H:%M:%S")
        else:
            order["completed_at"] = None


# ============================================================ 十四、库存流水

class Movement:
    __slots__ = ("product_id", "movement_type", "delta", "occurred_at", "ref", "note")

    def __init__(self, product_id, movement_type, delta, occurred_at, ref=None, note=None):
        self.product_id = product_id
        self.movement_type = movement_type
        self.delta = delta
        self.occurred_at = occurred_at
        self.ref = ref          # (order_id, product_id) 或 None
        self.note = note

    def __repr__(self):
        return f"<P{self.product_id} {self.movement_type} {self.delta:+d} @{self.occurred_at}>"


# --- 报损 / 人工调整（写死的业务事实，带原因，避免“无法回答为什么少了”）
LOSS_EVENTS = [
    (1, "2026-07-18T09:00:00", -2, "仓库盘点发现 2 件外箱破损，做报损处理"),
    (2, "2026-08-03T09:30:00", -1, "运输途中耳机外壳划伤，做报损"),
    (3, "2026-08-14T09:15:00", -2, "样衣外借后归还污损，做报损"),
    (4, "2026-08-22T10:00:00", -1, "鞋盒受潮，鞋面染色，做报损"),
    (6, "2026-09-18T09:40:00", -1, "坚果受潮结块，做报损"),
    (7, "2026-09-05T09:20:00", -1, "茶叶罐磕碰漏气，做报损"),
    (8, "2026-09-12T09:10:00", -2, "咖啡包装破损，做报损"),
    (5, "2026-08-08T09:05:00", -1, "帽子样品老化，做报损"),
    (2, "2026-07-07T09:00:00", -1, "抽检发现耳机左声道无声，做报损"),
    (7, "2026-07-14T09:30:00", -1, "茶叶受潮，做报损"),
    (3, "2026-09-09T09:10:00", -1, "T恤印花瑕疵，做报损"),
    (8, "2026-07-26T09:45:00", -1, "咖啡袋封口不良，做报损"),
]
ADJUST_EVENTS = [
    (1, "2026-08-06T10:30:00", 3, "盘点差异：账面少记 3 件，人工补正"),
    (1, "2026-09-22T11:00:00", -2, "盘点差异：账面多记 2 件，人工冲减"),
    (2, "2026-07-28T10:10:00", 2, "盘点差异：账面少记 2 件，人工补正"),
    (3, "2026-09-03T10:40:00", 4, "盘点差异：账面少记 4 件，人工补正"),
    (5, "2026-07-15T10:00:00", 2, "盘点差异：账面少记 2 件，人工补正"),
    (6, "2026-09-24T10:20:00", 3, "盘点差异：账面少记 3 件，人工补正"),
    (7, "2026-08-11T10:50:00", 2, "盘点差异：账面少记 2 件，人工补正"),
    (8, "2026-08-26T10:05:00", -1, "盘点差异：账面多记 1 件，人工冲减"),
    (4, "2026-08-27T10:35:00", 1, "盘点差异：账面少记 1 件，人工补正"),
    (2, "2026-09-14T10:25:00", -1, "盘点差异：账面多记 1 件，人工冲减"),
    (8, "2026-07-09T10:15:00", 4, "盘点差异：账面少记 4 件，人工补正"),
    (6, "2026-09-13T10:30:00", 2, "盘点差异：账面少记 2 件，人工补正"),
    (3, "2026-07-21T10:45:00", 2, "盘点差异：账面少记 2 件，人工补正"),
    (2, "2026-08-29T10:05:00", 3, "盘点差异：账面少记 3 件，人工补正"),
]


def build_returns(orders: list[dict], lifecycle: dict, rng: random.Random) -> list[Movement]:
    """退货入库：≥2 笔，全部落在 2026-09-24 之前（C1 的窗口外）。"""
    candidates = []
    for order in orders:
        if order["status"] != "已完成":
            continue
        if order["completed_at"] >= "2026-09-05T00:00:00":
            continue                       # 留足时间让退货落在 09-24 之前
        for pid, qty in order["items"]:
            if pid in (4, 5):
                continue                   # 不给 A1 的商品 4/5 造退货，避免零销量被“入库”搅浑
            candidates.append((order, pid, qty))
    if len(candidates) < 6:
        raise SpecViolation(f"可退货明细只有 {len(candidates)} 条，不足以造 6 笔退货")

    rng.shuffle(candidates)
    returns = []
    used_orders = set()
    for order, pid, qty in candidates:
        if len(returns) >= 6:
            break
        if order["order_id"] in used_orders:
            continue
        back_qty = min(qty, 1 + (len(returns) % 2))     # 1 或 2 件，且 ≤ 购买数量（§四 15）
        base = parse_dt(order["completed_at"]) + timedelta(hours=rng.randint(30, 96))
        if base.strftime("%Y-%m-%dT%H:%M:%S") >= "2026-09-20T00:00:00":
            continue
        used_orders.add(order["order_id"])
        returns.append(Movement(pid, "退货入库", back_qty, fmt_dt(base),
                                ref=(order["order_id"], pid), note="客户七天无理由退货，质检后重新入库"))
    if len(returns) < 2:
        raise SpecViolation("退货笔数不足 2 笔（C1 要求全库退货 ≥ 2）")
    return returns


def simulate_restocks(base_movements: list[Movement], lifecycle: dict) -> list[Movement]:
    """按日模拟库存水位：日终低于安全线，次日 07:30 自动补货到安全线 + 单次补货量。

    「补货」是业务事实（真实补货也是低库存触发），同时把各商品的日终库存极差
    控制在单次补货量附近——F1 的「商品 2 极差最高且领先 ≥5」由此可构造。
    """
    by_product = defaultdict(list)
    for movement in base_movements:
        if movement.movement_type == "初始库存":
            continue
        by_product[movement.product_id].append(movement)

    restocks: list[Movement] = []
    for product_id in sorted(lifecycle):
        events = sorted(by_product[product_id], key=lambda m: m.occurred_at)
        info = lifecycle[product_id]
        level = INITIAL_STOCK[product_id]
        pending = 0
        idx = 0
        day = info["first_day"]
        while day <= info["last_day"]:
            if pending:
                stamp = f"{day.isoformat()}T07:30:00"
                restocks.append(Movement(product_id, "补货", pending, stamp, None,
                                         "日终库存低于安全水位，次日自动补货"))
                level += pending
                pending = 0
            boundary = next_day_boundary(day)
            while idx < len(events) and events[idx].occurred_at < boundary:
                level += events[idx].delta
                if level < 0:
                    raise SpecViolation(
                        f"商品{product_id} 在 {events[idx].occurred_at} 库存转负（{level}）")
                idx += 1
            if level < INV_LOW[product_id] and day < info["last_day"]:
                pending = INV_LOW[product_id] + INV_RESTOCK_QTY[product_id] - level
            day += timedelta(days=1)
    return restocks


def build_movements(orders: list[dict], lifecycle: dict, rng: random.Random) -> list[Movement]:
    """§四 3：先有初始库存流水，再由流水派生快照。"""
    movements: list[Movement] = []

    # 1) 初始库存：每个商品有且仅有一条，时刻 = 商品创建时刻
    for product_id in sorted(lifecycle):
        movements.append(Movement(product_id, "初始库存", INITIAL_STOCK[product_id],
                                  lifecycle[product_id]["created_at"], None,
                                  "建仓初始入库"))

    # 2) 销售出库：只有已完成订单生成，时刻 = 订单完成时刻（§四 14）
    for order in orders:
        if order["status"] != "已完成":
            continue
        for pid, qty in order["items"]:
            movements.append(Movement(pid, "销售出库", -qty, order["completed_at"],
                                      ref=(order["order_id"], pid), note=None))

    # 3) 退货入库（C1：全部早于 09-24 窗口）
    movements.extend(build_returns(orders, lifecycle, rng))

    # 4) 报损 / 人工调整
    for pid, stamp, delta, note in LOSS_EVENTS:
        movements.append(Movement(pid, "报损", delta, stamp, None, note))
    for pid, stamp, delta, note in ADJUST_EVENTS:
        movements.append(Movement(pid, "人工调整", delta, stamp, None, note))

    # 5) 补货（模拟触发，见 simulate_restocks）
    movements.extend(simulate_restocks(movements, lifecycle))

    movements.sort(key=lambda m: (m.occurred_at, m.product_id, m.movement_type))
    return movements


def derive_daily(movements: list[Movement], lifecycle: dict) -> list[tuple]:
    """日终快照由流水派生：按 `< D+1 日 00:00:00` 切分后累计（§四 3 / §五 4）。"""
    rows = []
    for product_id in sorted(lifecycle):
        first_day, last_day = product_span(product_id, lifecycle)
        own = [m for m in movements if m.product_id == product_id]
        for day in daterange(first_day, last_day):
            boundary = next_day_boundary(day)
            total = sum(m.delta for m in own if m.occurred_at < boundary)
            auto_time = AUTO_REFRESH_TIMES[(day.day + product_id) % len(AUTO_REFRESH_TIMES)]
            rows.append((product_id, day.isoformat(), total,
                         f"{(day + timedelta(days=1)).isoformat()}T{auto_time}"))
    return rows


# ============================================================ 十五、数据集装配

def build_dataset(seed: int) -> dict:
    validate_region_mapping()
    quota_report = validate_quota_plan()
    conflict_report = check_scenario_conflicts()
    rng = random.Random(seed)

    lifecycle = build_lifecycle()
    customers = build_customers(rng)
    orders = build_order_skeleton(rng)
    assign_customers(orders, customers, rng)
    allocate_products(orders, lifecycle, rng)
    apply_quantities(orders, rng)
    ensure_september_top(orders)

    # 订单号按时间排序后连续编号（读起来像真实业务流水）
    orders.sort(key=lambda o: (o["order_time"], o["city"], o["status"]))
    for index, order in enumerate(orders, start=1):
        order["order_id"] = index

    build_histories(orders, rng)

    movements = build_movements(orders, lifecycle, rng)
    daily_rows = derive_daily(movements, lifecycle)

    return {
        "seed": seed,
        "quota_report": quota_report,
        "conflict_report": conflict_report,
        "lifecycle": lifecycle,
        "customers": customers,
        "orders": orders,
        "movements": movements,
        "daily_rows": daily_rows,
    }


# ============================================================ 十六、写库

def open_db(path: Path | str, fresh: bool = True) -> sqlite3.Connection:
    """建连接。fresh=True 时按 schema.sql 重建（含 :memory:）。复用了旧脚本的写法。"""
    if fresh and str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if Path(path).exists():
            Path(path).unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def build(conn: sqlite3.Connection, dataset: dict) -> dict:
    """把一个完整数据集写进 conn。返回 (order_id, product_id) → order_item_id 映射。"""
    conn.executemany("INSERT INTO categories(category_id,category_name) VALUES (?,?)",
                     CATEGORIES)
    conn.executemany(
        "INSERT INTO products(product_id,product_name,sku,price_cents,category_id)"
        " VALUES (?,?,?,?,?)", PRODUCTS)
    conn.executemany(
        "INSERT INTO customers(customer_id,customer_name,registered_at,province,city)"
        " VALUES (?,?,?,?,?)",
        [(c["customer_id"], c["customer_name"], c["registered_at"], c["province"], c["city"])
         for c in dataset["customers"]])

    for product_id in sorted(PRODUCT_STATUS_PLAN):
        conn.executemany(
            "INSERT INTO product_status_history(product_id,status_after,status_changed)"
            " VALUES (?,?,?)",
            [(product_id, status, changed) for status, changed in PRODUCT_STATUS_PLAN[product_id]])

    conn.executemany(
        "INSERT INTO orders(order_id,customer_id,order_time,order_status,"
        "shipping_province,shipping_city) VALUES (?,?,?,?,?,?)",
        [(o["order_id"], o["customer_id"], o["order_time"], o["status"],
          CITY_PROVINCE[o["city"]], o["city"]) for o in dataset["orders"]])

    item_ids: dict[tuple[int, int], int] = {}
    for order in dataset["orders"]:
        for pid, qty in order["items"]:
            cur = conn.execute(
                "INSERT INTO order_items(order_id,product_id,unit_price_cents,quantity)"
                " VALUES (?,?,?,?)",
                (order["order_id"], pid, unit_price(order, pid), qty))
            item_ids[(order["order_id"], pid)] = cur.lastrowid

    history_rows = []
    for order in dataset["orders"]:
        for event in order["history"]:
            history_rows.append((order["order_id"], event["from_status"], event["to_status"],
                                 event["changed_at"], event["reason_code"],
                                 event["change_note"]))
    conn.executemany(
        "INSERT INTO order_status_history(order_id,from_status,to_status,changed_at,"
        "reason_code,change_note) VALUES (?,?,?,?,?,?)", history_rows)

    conn.executemany(
        "INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,"
        "occurred_at,order_item_id,note) VALUES (?,?,?,?,?,?)",
        [(m.product_id, m.movement_type, m.delta, m.occurred_at,
          item_ids[m.ref] if m.ref else None, m.note) for m in dataset["movements"]])

    conn.executemany(
        "INSERT INTO inventory_daily(product_id,snapshot_date,inventory_num,recorded_at)"
        " VALUES (?,?,?,?)", dataset["daily_rows"])

    conn.commit()
    return {"item_ids": item_ids}


def recompute_inventory_daily(conn: sqlite3.Connection, product_id: int) -> int:
    """按流水重算某商品的全部日终快照（与 derive_daily 同一条日界规则）。

    在脏库里插入 / 改动流水后必须调用：否则快照与流水累计不符，
    会顺带触发不变量 4，破坏「一处异常只违反一条规矩」。返回被改写的快照行数。
    """
    changed = 0
    rows = conn.execute("SELECT snapshot_date, inventory_num FROM inventory_daily"
                        " WHERE product_id=? ORDER BY snapshot_date", (product_id,)).fetchall()
    for snapshot_date, stored in rows:
        boundary = next_day_boundary(date.fromisoformat(snapshot_date))
        expected = conn.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements"
            " WHERE product_id=? AND occurred_at < ?", (product_id, boundary)).fetchone()[0]
        if stored != expected:
            conn.execute("UPDATE inventory_daily SET inventory_num=?"
                         " WHERE product_id=? AND snapshot_date=?",
                         (expected, product_id, snapshot_date))
            changed += 1
    return changed


def inject_dirty(conn: sqlite3.Connection, dataset: dict) -> list[str]:
    """§七：只注入五类「结构合法、跨表语义错误」的异常，每处只违反一条不变量。"""
    injected = []
    orders = dataset["orders"]

    # ① 主表 / 历史不一致：拿一张「运输中」的单，把 order_status 提前写成「已完成」。
    #    这样做不会顺带踩到状态年龄窗口（运输中 ≤7 天），保证一处异常只违反一条规矩。
    victim = next(o for o in orders if o["status"] == "运输中")
    conn.execute("UPDATE orders SET order_status='已完成' WHERE order_id=?", (victim["order_id"],))
    injected.append(f"① 订单{victim['order_id']}（{victim['order_time'][:10]}）的 "
                    f"orders.order_status 改为「已完成」（历史最新仍是「运输中」）"
                    f"→ 违反不变量 3")

    # ② 派生值错误：某商品最后一天的日终库存改错
    conn.execute("UPDATE inventory_daily SET inventory_num = inventory_num + 40"
                 " WHERE product_id=2 AND snapshot_date=?", (DATA_END.isoformat(),))
    injected.append("② 商品2 在 2026-09-30 的 inventory_num 加 40（与流水累计不符）"
                    "→ 违反不变量 4")

    # ③ 生命周期越界：让已删除的商品 7 出现在删除之后的新订单里
    #    新订单只到「待支付」，不生成销售出库，因此不引入第二条异常。
    new_order_id = max(o["order_id"] for o in orders) + 1
    customer_id = dataset["customers"][0]["customer_id"]
    conn.execute(
        "INSERT INTO orders(order_id,customer_id,order_time,order_status,"
        "shipping_province,shipping_city) VALUES (?,?,?,?,?,?)",
        (new_order_id, customer_id, "2026-09-28T10:00:00", "待支付", "广东省", "广州市"))
    conn.execute(
        "INSERT INTO order_status_history(order_id,from_status,to_status,changed_at,"
        "reason_code,change_note) VALUES (?,NULL,'待支付','2026-09-28T10:00:00',"
        "'created','下单成功，等待支付')", (new_order_id,))
    conn.execute(
        "INSERT INTO order_items(order_id,product_id,unit_price_cents,quantity)"
        " VALUES (?,?,?,?)", (new_order_id, 7, 25900, 1))
    injected.append(f"③ 新增订单{new_order_id}（2026-09-28、商品7×1、待支付），"
                    f"晚于商品7 删除日 2026-09-27 → 违反不变量 9")

    # ④ 累计退货超购买：同一个明细退两次，单条都不超、累计超。
    #    选一条已有退货、购买量 ≥2 的明细（本数据集是 order_item 41：购买 2、已退 1），
    #    再插一条退「购买量」件的流水 → 累计 3 > 2。单条 2 ≤ 2，硬约束 H5 仍然通过。
    return_item_id, return_product_id, bought = conn.execute(
        "SELECT m.order_item_id, m.product_id, i.quantity"
        " FROM inventory_movements m JOIN order_items i ON i.order_item_id=m.order_item_id"
        " WHERE m.movement_type='退货入库' AND i.quantity >= 2"
        " ORDER BY m.order_item_id LIMIT 1").fetchone()
    b_order_id = conn.execute("SELECT order_id FROM order_items WHERE order_item_id=?",
                              (return_item_id,)).fetchone()[0]
    last_return_at = max(r[0] for r in conn.execute(
        "SELECT occurred_at FROM inventory_movements"
        " WHERE movement_type='退货入库' AND order_item_id=?", (return_item_id,)))
    completed_at = conn.execute(
        "SELECT changed_at FROM order_status_history WHERE order_id=? AND to_status='已完成'"
        " ORDER BY changed_at LIMIT 1", (b_order_id,)).fetchone()[0]
    second_return_at = fmt_dt(parse_dt(max(last_return_at, completed_at)) + timedelta(days=1))
    conn.execute(
        "INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,"
        "occurred_at,order_item_id,note) VALUES (?,?,?,?,?,?)",
        (return_product_id, "退货入库", bought, second_return_at, return_item_id,
         "二次部分退货（注入）"))
    refreshed = recompute_inventory_daily(conn, return_product_id)
    injected.append(f"④ 明细{return_item_id}（购买 {bought} 件）再插一条退货入库 "
                    f"{bought} 件 @{second_return_at}，累计退货超过购买量"
                    f"（同步重算商品{return_product_id} 的 {refreshed} 条快照）→ 违反不变量 10")

    # ⑤ 状态历史连续重复：挑一张「已完成」订单（与 ① ③ ④ 不同单），
    #    在历史末尾追加一条 from/to 都等于上一条的记录，时刻晚于上一条。
    #    订单分支没有显式的「连续重复」规则，会以「非法跳转 已完成 → 已完成」被不变量 7 抓到。
    duplicate_order = conn.execute(
        "SELECT o.order_id, h.to_status, h.changed_at, h.reason_code"
        " FROM orders o JOIN order_status_history h ON h.order_id=o.order_id"
        " WHERE o.order_status='已完成' AND o.order_id NOT IN (?,?)"
        "   AND h.order_history_id=(SELECT MAX(order_history_id)"
        "                            FROM order_status_history WHERE order_id=o.order_id)"
        " ORDER BY h.changed_at LIMIT 1",
        (victim["order_id"], b_order_id)).fetchone()
    duplicate_order_id, duplicate_status, duplicate_prev_at, duplicate_reason = duplicate_order
    duplicate_at = fmt_dt(parse_dt(duplicate_prev_at) + timedelta(hours=2))
    conn.execute(
        "INSERT INTO order_status_history(order_id,from_status,to_status,changed_at,"
        "reason_code,change_note) VALUES (?,?,?,?,?,?)",
        (duplicate_order_id, duplicate_status, duplicate_status, duplicate_at,
         duplicate_reason, "重复上报同一状态（注入）"))
    injected.append(f"⑤ 订单{duplicate_order_id} 历史末尾追加一条「{duplicate_status} → "
                    f"{duplicate_status}」@{duplicate_at}（与上一条同状态）"
                    f"→ 违反不变量 7（表现为非法跳转）")

    conn.commit()
    return injected


# ============================================================ 十七、§五 通用不变量（10 条）

def latest_order_status(conn, order_id):
    row = conn.execute(
        "SELECT to_status FROM order_status_history WHERE order_id=?"
        " ORDER BY changed_at DESC, order_history_id DESC LIMIT 1", (order_id,)).fetchone()
    return row[0] if row else None


def latest_product_status(conn, product_id):
    row = conn.execute(
        "SELECT status_after FROM product_status_history WHERE product_id=?"
        " ORDER BY status_changed DESC, status_history_id DESC LIMIT 1",
        (product_id,)).fetchone()
    return row[0] if row else None


def inv_1_integrity(conn):
    rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    ok = rows == ["ok"]
    return ok, "ok" if ok else f"异常：{rows}"


def inv_2_foreign_keys(conn):
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return (not rows), "无违规" if not rows else f"{len(rows)} 条违规：{rows[:3]}"


def inv_3_status_consistency(conn):
    bad = []
    for order_id, status in conn.execute("SELECT order_id,order_status FROM orders"):
        latest = latest_order_status(conn, order_id)
        if latest != status:
            bad.append(f"订单{order_id} order_status={status} ≠ 历史最新={latest}")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(products)")]
    status_like = [c for c in columns
                   if c.lower() in ("status", "product_status", "current_status")]
    if status_like:
        bad.append(f"products 表出现了状态列 {status_like}，状态必须只能从历史推导")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_4_snapshot_equals_ledger(conn):
    bad = []
    for product_id, snapshot_date, stored in conn.execute(
            "SELECT product_id,snapshot_date,inventory_num FROM inventory_daily"):
        boundary = next_day_boundary(date.fromisoformat(snapshot_date))
        expected = conn.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements"
            " WHERE product_id=? AND occurred_at < ?", (product_id, boundary)).fetchone()[0]
        if stored != expected:
            bad.append(f"商品{product_id} {snapshot_date} 快照 {stored} ≠ 累计 {expected}")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_5_snapshot_range(conn):
    bad = []
    for product_id, in conn.execute("SELECT DISTINCT product_id FROM inventory_daily"):
        rows = conn.execute(
            "SELECT status_after,status_changed FROM product_status_history"
            " WHERE product_id=? ORDER BY status_history_id", (product_id,)).fetchall()
        created = date.fromisoformat(rows[0][1][:10])
        deleted = next((date.fromisoformat(ts[:10]) for status, ts in rows if status == "已删除"),
                       None)
        end = (deleted - timedelta(days=1)) if deleted else DATA_END
        expected = {d.isoformat() for d in daterange(created, end)}
        actual = {r[0] for r in conn.execute(
            "SELECT snapshot_date FROM inventory_daily WHERE product_id=?", (product_id,))}
        if expected != actual:
            missing = sorted(expected - actual)[:3]
            extra = sorted(actual - expected)[:3]
            bad.append(f"商品{product_id} 快照区间不符：缺 {len(expected - actual)} {missing}，"
                       f"多 {len(actual - expected)} {extra}")
    return (not bad), "通过（稠密，无空洞）" if not bad else "；".join(bad[:5])


def inv_6_region_pairs(conn):
    bad = []
    for table, columns in (("customers", "province,city"),
                           ("orders", "shipping_province,shipping_city")):
        for province, city in conn.execute(f"SELECT {columns} FROM {table}"):
            if city not in PROVINCE_CITIES.get(province, set()):
                bad.append(f"{table}: {province}/{city} 不是合法配对")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_7_transitions(conn):
    bad = []
    # 订单：无连续重复 + 合法跳转
    for order_id, in conn.execute("SELECT DISTINCT order_id FROM order_status_history"):
        rows = conn.execute(
            "SELECT from_status,to_status FROM order_status_history WHERE order_id=?"
            " ORDER BY changed_at, order_history_id", (order_id,)).fetchall()
        previous = None
        for from_status, to_status in rows:
            if from_status != previous:
                bad.append(f"订单{order_id} 历史链断裂：{previous} → {from_status}")
                break
            if previous is None and to_status != "待支付":
                bad.append(f"订单{order_id} 首条状态 {to_status} ≠ 待支付")
            if previous is not None:
                allowed = {t for t, _ in ORDER_FLOW.get(previous, [])}
                if to_status not in allowed:
                    bad.append(f"订单{order_id} 非法跳转 {previous} → {to_status}")
            previous = to_status
    # 商品：无连续重复 + 合法跳转（已删除为终态）
    product_flow = {None: {"已上架", "未上架"},
                    "已上架": {"未上架", "已删除"},
                    "未上架": {"已上架", "已删除"},
                    "已删除": set()}
    for product_id, in conn.execute("SELECT DISTINCT product_id FROM product_status_history"):
        rows = [r[0] for r in conn.execute(
            "SELECT status_after FROM product_status_history WHERE product_id=?"
            " ORDER BY status_history_id", (product_id,))]
        previous = None
        for status in rows:
            if status == previous:
                bad.append(f"商品{product_id} 连续重复状态 {status}")
                break
            if status not in product_flow.get(previous, set()):
                bad.append(f"商品{product_id} 非法跳转 {previous} → {status}")
                break
            previous = status
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_8_movement_direction(conn):
    positive = {"初始库存", "补货", "退货入库"}
    negative = {"销售出库", "报损"}
    bad = []
    for movement_id, mtype, delta in conn.execute(
            "SELECT movement_id,movement_type,quantity_delta FROM inventory_movements"):
        if mtype in positive and delta <= 0:
            bad.append(f"流水{movement_id} {mtype} {delta} 应为正")
        elif mtype in negative and delta >= 0:
            bad.append(f"流水{movement_id} {mtype} {delta} 应为负")
        elif mtype == "人工调整" and delta == 0:
            bad.append(f"流水{movement_id} 人工调整为 0")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_9_deleted_no_activity(conn):
    bad = []
    deleted = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id,status_changed FROM product_status_history WHERE status_after='已删除'")}
    for product_id, at in deleted.items():
        for order_id, order_time in conn.execute(
                "SELECT DISTINCT o.order_id,o.order_time FROM orders o"
                " JOIN order_items i ON i.order_id=o.order_id"
                " WHERE i.product_id=? AND o.order_time >= ?", (product_id, at)):
            bad.append(f"订单{order_id}（{order_time}）在商品{product_id} 删除后下单")
        for movement_id, occurred_at in conn.execute(
                "SELECT movement_id,occurred_at FROM inventory_movements"
                " WHERE product_id=? AND occurred_at >= ?", (product_id, at)):
            bad.append(f"流水{movement_id}（{occurred_at}）在商品{product_id} 删除后发生")
        for status_history_id, changed in conn.execute(
                "SELECT status_history_id,status_changed FROM product_status_history"
                " WHERE product_id=? AND status_changed > ?", (product_id, at)):
            bad.append(f"商品状态{status_history_id}（{changed}）在删除之后")
        if not conn.execute("SELECT 1 FROM products WHERE product_id=?", (product_id,)).fetchone():
            bad.append(f"商品{product_id} 已删除却不在 products 表中（变成物理删除）")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def inv_10_returns_within_purchase(conn):
    """§五 10：同一明细的累计退货量不得超过购买数量。

    按 order_item_id 汇总全部「退货入库」流水后与 order_items.quantity 比较；
    order_item_id 为 NULL 的退货无法归属明细，单独兜底报出（不能被 JOIN 静默丢弃）。
    """
    bad = []
    for order_item_id, returned, rows, bought in conn.execute(
            "SELECT m.order_item_id, SUM(m.quantity_delta), COUNT(*), i.quantity"
            " FROM inventory_movements m"
            " LEFT JOIN order_items i ON i.order_item_id = m.order_item_id"
            " WHERE m.movement_type='退货入库'"
            " GROUP BY m.order_item_id"):
        if order_item_id is None:
            bad.append(f"{rows} 条退货流水缺 order_item_id（累计 {returned} 件无法归属明细）")
        elif bought is None:
            bad.append(f"明细{order_item_id} 不存在，却有 {rows} 条退货流水累计 {returned} 件")
        elif returned > bought:
            bad.append(f"明细{order_item_id} 累计退货 {returned} 件 > 购买 {bought} 件")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


INVARIANTS = [
    ("1", "integrity_check = ok", inv_1_integrity),
    ("2", "foreign_key_check 无违规", inv_2_foreign_keys),
    ("3", "orders.order_status = 历史最新；商品状态只能从历史推导", inv_3_status_consistency),
    ("4", "每条快照 = 截至当日全部流水累计", inv_4_snapshot_equals_ledger),
    ("5", "快照日期集合 = 创建/删除区间（稠密无空洞）", inv_5_snapshot_range),
    ("6", "省市配对合法", inv_6_region_pairs),
    ("7", "状态历史无连续重复、无非法跳转", inv_7_transitions),
    ("8", "流水方向符合业务含义", inv_8_movement_direction),
    ("9", "已删除商品没有任何后续业务事实", inv_9_deleted_no_activity),
    ("10", "同一明细累计退货量 ≤ 购买数量", inv_10_returns_within_purchase),
]


# ============================================================ 十七之二、§四 补充硬约束
# §五 10 条覆盖不到的 §四 条目，一并进验收闸门（任一条失败也不发布）。

def hard_1_status_age(conn):
    """§6.1：待支付/已支付/运输中的 order_time 距 2026-09-30 不得超过 3/5/7 天。"""
    bad, detail = [], []
    for status, limit in STATUS_AGE_LIMIT.items():
        ages = [(DATA_END - date.fromisoformat(row[0][:10])).days for row in conn.execute(
            "SELECT order_time FROM orders WHERE order_status=?", (status,))]
        if ages:
            detail.append(f"{status} 最长 {max(ages)} 天/限 {limit} 天")
            bad.extend(f"{status} 有 {sum(1 for a in ages if a > limit)} 单超过 {limit} 天"
                       for _ in [0] if any(a > limit for a in ages))
    return (not bad), "；".join(detail) + ("" if not bad else "❌ " + "；".join(bad))


def hard_2_registration(conn):
    """§四 20：客户注册时间早于其首笔订单。"""
    bad = [f"客户{r[0]}" for r in conn.execute(
        "SELECT c.customer_id FROM customers c JOIN orders o ON o.customer_id=c.customer_id"
        " GROUP BY c.customer_id HAVING c.registered_at >= MIN(o.order_time)")]
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def hard_3_history_monotonic(conn):
    """§四 10：状态时刻按插入序严格递增，且首条 = orders.order_time。

    必须按 order_history_id（AUTOINCREMENT，可视为插入序）取顺序再验证；
    若先 ORDER BY changed_at 再比较，排序后必然单调，倒序永远抓不到。
    """
    bad = []
    for order_id, order_time in conn.execute("SELECT order_id,order_time FROM orders"):
        times = [r[0] for r in conn.execute(
            "SELECT changed_at FROM order_status_history WHERE order_id=?"
            " ORDER BY order_history_id", (order_id,))]
        if not times or times[0] != order_time:
            bad.append(f"订单{order_id} 首条变更 {times[0] if times else None} ≠ {order_time}")
        elif any(b <= a for a, b in zip(times, times[1:])):
            bad.append(f"订单{order_id} 变更时刻按插入序未严格递增")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def hard_4_snapshot_window(conn):
    """§四 18：自动快照 recorded_at 落在业务日次日 04:00~05:00。"""
    bad = []
    for product_id, snapshot_date, recorded_at in conn.execute(
            "SELECT product_id,snapshot_date,recorded_at FROM inventory_daily"):
        expected = (date.fromisoformat(snapshot_date) + timedelta(days=1)).isoformat()
        if not (recorded_at.startswith(expected)
                and WINDOW_OPEN <= recorded_at[len(expected) + 1:] < WINDOW_CLOSE):
            bad.append(f"商品{product_id} {snapshot_date} 快照写入时刻 {recorded_at} 越界")
    return (not bad), f"全部落在次日 04:00~05:00" if not bad else "；".join(bad[:5])


def hard_5_returns(conn):
    """§四 15：退货晚于订单完成时间，数量 ≤ 该明细购买数量。"""
    bad = []
    for movement_id, qty, occurred_at, product_id, order_item_id in conn.execute(
            "SELECT movement_id,quantity_delta,occurred_at,product_id,order_item_id"
            " FROM inventory_movements WHERE movement_type='退货入库'"):
        if order_item_id is None:
            bad.append(f"退货流水{movement_id} 缺 order_item_id")
            continue
        item_product, bought, order_id = conn.execute(
            "SELECT product_id,quantity,order_id FROM order_items WHERE order_item_id=?",
            (order_item_id,)).fetchone()
        completed = conn.execute(
            "SELECT changed_at FROM order_status_history WHERE order_id=? AND to_status='已完成'"
            " ORDER BY changed_at LIMIT 1", (order_id,)).fetchone()
        if item_product != product_id:
            bad.append(f"退货流水{movement_id} 商品与明细不一致")
        if not completed or occurred_at <= completed[0]:
            bad.append(f"退货流水{movement_id} 不晚于完成时间")
        if qty > bought:
            bad.append(f"退货流水{movement_id} 退 {qty} 件 > 购买 {bought} 件")
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def hard_6_no_activity_before_creation(conn):
    """§四 17a：商品创建前不得出现在任何订单、明细、流水中。"""
    created = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, MIN(status_changed) FROM product_status_history GROUP BY product_id")}
    bad = [f"订单{oid}/商品{pid}@{ot}" for oid, pid, ot in conn.execute(
        "SELECT o.order_id,i.product_id,o.order_time FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id")
        if ot < created[pid]]
    bad += [f"流水{mid}/商品{pid}@{at}" for mid, pid, at in conn.execute(
        "SELECT movement_id,product_id,occurred_at FROM inventory_movements")
        if at < created[pid]]
    bad += [f"快照{pid}@{d}" for pid, d in conn.execute(
        "SELECT product_id,snapshot_date FROM inventory_daily")
        if d < created[pid][:10]]        # 快照按业务日，创建当天可以有日终快照
    return (not bad), "通过" if not bad else "；".join(bad[:5])


def hard_7_every_order_has_items(conn):
    """§四 13：每单至少一条明细（已取消订单也保留明细）。"""
    bad = [f"订单{r[0]}" for r in conn.execute(
        "SELECT o.order_id FROM orders o WHERE NOT EXISTS"
        " (SELECT 1 FROM order_items i WHERE i.order_id=o.order_id)")]
    return (not bad), "通过（含已取消订单）" if not bad else "；".join(bad[:5])


HARD_CONSTRAINTS = [
    ("H1", "状态年龄窗口 3/5/7 天（§6.1）", hard_1_status_age),
    ("H2", "客户注册早于首单（§四 20）", hard_2_registration),
    ("H3", "状态时刻严格递增且首条 = order_time（§四 10）", hard_3_history_monotonic),
    ("H4", "自动快照落在次日 04:00~05:00（§四 18）", hard_4_snapshot_window),
    ("H5", "退货晚于完成且数量 ≤ 购买数量（§四 15）", hard_5_returns),
    ("H6", "商品创建前无任何业务事实（§四 17a）", hard_6_no_activity_before_creation),
    ("H7", "每单至少一条明细（含已取消，§四 13）", hard_7_every_order_has_items),
]


# ============================================================ 十八、§三 场景验收（14 条）

def sc_a1(conn):
    """A1：至少 2 个商品在九月同时满足「订单笔数 = 0」和「销量 = 0」。"""
    rows = conn.execute(
        "SELECT i.product_id, COUNT(DISTINCT i.order_id) AS orders,"
        " COALESCE(SUM(i.quantity),0) AS qty"
        " FROM order_items i JOIN orders o ON o.order_id=i.order_id"
        " WHERE substr(o.order_time,1,7)='2026-09'"
        " GROUP BY i.product_id").fetchall()
    seen = {pid: (orders, qty) for pid, orders, qty in rows}
    zeros = [pid for pid in range(1, 9) if seen.get(pid, (0, 0)) == (0, 0)]
    ok = len(zeros) >= 2
    detail = f"九月零笔数且零销量的商品：{zeros}（要求 ≥2，含商品4/5）"
    if not ok:
        detail += "；九月有明细的商品：" + str({p: seen[p] for p in sorted(seen)})
    return ok, detail


def sc_a2(conn):
    """A2：某城市某商品为 0 有意义——该城市至少有 1 笔已完成订单。"""
    city = "青岛市"
    completed = conn.execute(
        "SELECT count(*) FROM orders WHERE shipping_city=? AND order_status='已完成'",
        (city,)).fetchone()[0]
    qty = dict(conn.execute(
        "SELECT i.product_id, COALESCE(SUM(i.quantity),0) FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE o.order_status='已完成' AND o.shipping_city=? GROUP BY i.product_id", (city,)))
    zero_products = [pid for pid in range(1, 9) if qty.get(pid, 0) == 0]
    ok = completed >= 1 and len(zero_products) >= 1
    return ok, (f"{city} 已完成 {completed} 笔；其中零销量商品 "
                f"{zero_products}（零值有业务含义，非空集）")


def sc_b1(conn):
    """B1：已取消订单涉及的商品完全并列（≥3 商品，各商品笔数 max = min）。"""
    rows = conn.execute(
        "SELECT i.product_id, COUNT(DISTINCT i.order_id) AS cnt FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE o.order_status='已取消' GROUP BY i.product_id ORDER BY i.product_id").fetchall()
    if len(rows) < 3:
        return False, f"已取消订单只涉及 {len(rows)} 个商品：{rows}"
    counts = [cnt for _, cnt in rows]
    ok = max(counts) == min(counts)
    return ok, f"已取消涉及商品 {dict(rows)}，max={max(counts)} min={min(counts)}，商品数 {len(rows)}"


def sc_b2(conn):
    """B2：至少 1 个城市有唯一最高商品，且「件数最高」=「笔数最高」（杭州）。"""
    city = "杭州市"
    qty = dict(conn.execute(
        "SELECT i.product_id, COALESCE(SUM(i.quantity),0) FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE o.order_status='已完成' AND o.shipping_city=? GROUP BY i.product_id", (city,)))
    cnt = dict(conn.execute(
        "SELECT i.product_id, COUNT(DISTINCT i.order_id) FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE o.order_status='已完成' AND o.shipping_city=? GROUP BY i.product_id", (city,)))
    if not qty:
        return False, f"{city} 没有已完成订单"
    top_qty = max(qty.values())
    qty_tops = [pid for pid, value in qty.items() if value == top_qty]
    top_cnt = max(cnt.values())
    cnt_tops = [pid for pid, value in cnt.items() if value == top_cnt]
    ok = len(qty_tops) == 1 and cnt_tops == qty_tops
    return ok, (f"{city} 件数最高 {qty_tops}（{top_qty} 件）唯一；"
                f"笔数最高 {cnt_tops}（{top_cnt} 笔）；两者一致={cnt_tops == qty_tops}")


def sc_b3(conn):
    """B3：至少 1 个城市按销量件数有 ≥2 个并列最高（深圳）。"""
    city = "深圳市"
    qty = dict(conn.execute(
        "SELECT i.product_id, COALESCE(SUM(i.quantity),0) FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE o.order_status='已完成' AND o.shipping_city=? GROUP BY i.product_id", (city,)))
    top = max(qty.values())
    tops = sorted(pid for pid, value in qty.items() if value == top)
    ok = len(tops) >= 2
    return ok, f"{city} 件数最高 {top} 件，并列商品 {tops}（要求 ≥2）"


def sc_b4(conn):
    """B4：九月内变动最多的商品 ≠ 全区间变动最多的商品，且两个最高都唯一。"""
    global_counts = dict(conn.execute(
        "SELECT product_id, count(*) FROM product_status_history GROUP BY product_id"))
    sep_counts = dict(conn.execute(
        "SELECT product_id, count(*) FROM product_status_history"
        " WHERE substr(status_changed,1,7)='2026-09' GROUP BY product_id"))
    for pid in range(1, 9):
        global_counts.setdefault(pid, 0)
        sep_counts.setdefault(pid, 0)
    gtop = max(global_counts.values())
    stop = max(sep_counts.values())
    gtops = sorted(p for p, v in global_counts.items() if v == gtop)
    stops = sorted(p for p, v in sep_counts.items() if v == stop)
    ok = (len(gtops) == 1 and len(stops) == 1 and gtops != stops and stop >= 1)
    return ok, (f"全区间最高 {gtops}（{gtop} 条）唯一；九月最高 {stops}（{stop} 条）唯一；"
                f"两者不同={gtops != stops}")


def sc_c1(conn):
    """C1：窗口 2026-09-24~09-30 内退货 = 0；全库退货 ≥2 且全部在窗口外。"""
    rows = [r[0] for r in conn.execute(
        "SELECT occurred_at FROM inventory_movements WHERE movement_type='退货入库'")]
    in_window = [ts for ts in rows if "2026-09-24" <= ts[:10] <= "2026-09-30"]
    outside = [ts for ts in rows if ts not in in_window]
    ok = len(in_window) == 0 and len(rows) >= 2 and len(outside) >= 2
    return ok, (f"全库退货 {len(rows)} 笔，窗口内 {len(in_window)} 笔，窗口外 {len(outside)} 笔；"
                f"最近一笔 {max(rows) if rows else None}")


def sc_c2(conn):
    """C2 是路线决策（只做空结果题），数据侧的表现与 C1 同源，这里只做记录。"""
    rows = [r[0] for r in conn.execute(
        "SELECT occurred_at FROM inventory_movements WHERE movement_type='退货入库'")]
    in_window = [ts for ts in rows if "2026-09-24" <= ts[:10] <= "2026-09-30"]
    ok = len(in_window) == 0
    return ok, "路线已定为「空结果题」：窗口内退货为 0，不构造退货率排名所需样本"


def sc_d1(conn):
    """D1：商品 8 已完成成交价唯一；全量出现第二种价，且第二种价只来自未完成订单。"""
    completed = [r[0] for r in conn.execute(
        "SELECT DISTINCT i.unit_price_cents FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE i.product_id=8 AND o.order_status='已完成' ORDER BY 1")]
    every = [r[0] for r in conn.execute(
        "SELECT DISTINCT unit_price_cents FROM order_items WHERE product_id=8 ORDER BY 1")]
    open_prices = [r[0] for r in conn.execute(
        "SELECT DISTINCT i.unit_price_cents FROM order_items i"
        " JOIN orders o ON o.order_id=i.order_id"
        " WHERE i.product_id=8 AND o.order_status<>'已完成' ORDER BY 1")]
    list_price = conn.execute(
        "SELECT price_cents FROM products WHERE product_id=8").fetchone()[0]
    ok = (len(completed) == 1 and len(every) >= 2
          and set(open_prices) == set(every) - set(completed)
          and set(every) - set(completed))
    return ok, (f"已完成成交价 {completed}（唯一）；全量 {every}；"
                f"未完成订单价 {open_prices}；products 现价 {list_price}")


def sc_d2(conn):
    """D2：发货省分布 ≠ 客户所在省分布；且已完成单里一致 ≥1、不一致 ≥3。"""
    customer_counts = Counter(r[0] for r in conn.execute("SELECT province FROM customers"))
    order_counts = Counter(r[0] for r in conn.execute("SELECT shipping_province FROM orders"))
    customer_total = sum(customer_counts.values())
    order_total = sum(order_counts.values())
    customer_share = {p: 100.0 * n / customer_total for p, n in customer_counts.items()}
    order_share = {p: 100.0 * n / order_total for p, n in order_counts.items()}
    c_top = max(customer_share, key=customer_share.get)
    o_top = max(order_share, key=order_share.get)
    shared = max(set(customer_share) | set(order_share),
                 key=lambda p: abs(customer_share.get(p, 0) - order_share.get(p, 0)))
    gap = abs(customer_share.get(shared, 0) - order_share.get(shared, 0))

    matched = conn.execute(
        "SELECT count(*) FROM orders o JOIN customers c ON c.customer_id=o.customer_id"
        " WHERE o.order_status='已完成' AND o.shipping_city=c.city").fetchone()[0]
    mismatched = conn.execute(
        "SELECT count(*) FROM orders o JOIN customers c ON c.customer_id=o.customer_id"
        " WHERE o.order_status='已完成' AND o.shipping_city<>c.city").fetchone()[0]
    ok = (c_top != o_top or gap >= 15.0) and matched >= 1 and mismatched >= 3
    return ok, (f"客户最大省 {c_top} {customer_share[c_top]:.1f}%，订单最大省 {o_top} "
                f"{order_share[o_top]:.1f}%；{shared} 差 {gap:.1f}pp（要求 ≥15 或最大省不同）；"
                f"已完成单一致 {matched} 笔、不一致 {mismatched} 笔")


def sc_d3(conn):
    """D3：≥1 个商品当前状态「已删除」，且是该商品状态历史最后一条，且仍在 products 表。"""
    rows = conn.execute(
        "SELECT product_id, status_after FROM product_status_history ORDER BY product_id,"
        " status_history_id").fetchall()
    by_product = defaultdict(list)
    for product_id, status in rows:
        by_product[product_id].append(status)
    deleted = [pid for pid, statuses in by_product.items() if statuses[-1] == "已删除"]
    in_products = [pid for pid in deleted
                   if conn.execute("SELECT 1 FROM products WHERE product_id=?", (pid,)).fetchone()]
    ok = len(deleted) >= 1 and len(in_products) == len(deleted)
    return ok, f"当前「已删除」商品 {deleted}，仍在 products 表中 {in_products}，且都是历史最后一条"


def sc_d4(conn):
    """D4：至少 2 个商品当前状态「未上架」。"""
    rows = conn.execute(
        "SELECT product_id, status_after FROM product_status_history ORDER BY product_id,"
        " status_history_id").fetchall()
    by_product = defaultdict(list)
    for product_id, status in rows:
        by_product[product_id].append(status)
    unlisted = [pid for pid, statuses in by_product.items() if statuses[-1] == "未上架"]
    ok = len(unlisted) >= 2
    return ok, f"当前「未上架」商品 {unlisted}（要求 ≥2）"


def sc_e1(conn):
    """E1：各商品快照天数不同，极差 ≥60，差异来自创建/删除时点而非随机丢数据。"""
    days = {}
    for product_id, in conn.execute("SELECT DISTINCT product_id FROM inventory_daily"):
        days[product_id] = conn.execute(
            "SELECT count(*) FROM inventory_daily WHERE product_id=?", (product_id,)).fetchone()[0]
    gap = max(days.values()) - min(days.values())
    consistent = True
    for product_id, count in days.items():
        rows = conn.execute(
            "SELECT status_after,status_changed FROM product_status_history"
            " WHERE product_id=? ORDER BY status_history_id", (product_id,)).fetchall()
        created = date.fromisoformat(rows[0][1][:10])
        deleted = next((date.fromisoformat(ts[:10]) for status, ts in rows if status == "已删除"),
                       None)
        expected = ((deleted - timedelta(days=1)) if deleted else DATA_END) - created
        if count != expected.days + 1:
            consistent = False
    ok = gap >= 60 and consistent
    return ok, (f"各商品天数 {dict(sorted(days.items()))}；极差 {gap} 天（要求 ≥60），"
                f"且与创建/删除时点一致={consistent}")


def sc_f1(conn):
    """F1：库存极差最高者唯一且领先 ≥5。"""
    spans = {}
    for product_id, low, high in conn.execute(
            "SELECT product_id, MIN(inventory_num), MAX(inventory_num)"
            " FROM inventory_daily GROUP BY product_id"):
        spans[product_id] = high - low
    ordered = sorted(spans.items(), key=lambda kv: -kv[1])
    top_pid, top_value = ordered[0]
    second_value = ordered[1][1]
    tops = [pid for pid, value in spans.items() if value == top_value]
    ok = len(tops) == 1 and top_value - second_value >= 5
    return ok, (f"极差 {dict(ordered)}；最高 {top_pid}={top_value} 唯一，"
                f"领先第二名 {top_value - second_value}（要求 ≥5）")


def sc_f2(conn):
    """F2：九月销量最高者唯一（已完成口径与全量口径都要成立）。"""
    def tops(where: str):
        rows = conn.execute(
            "SELECT i.product_id, SUM(i.quantity) FROM order_items i"
            " JOIN orders o ON o.order_id=i.order_id"
            f" WHERE substr(o.order_time,1,7)='2026-09' {where} GROUP BY i.product_id").fetchall()
        if not rows:
            return [], {}
        values = dict(rows)
        top = max(values.values())
        return sorted(pid for pid, value in values.items() if value == top), values

    done_tops, done_values = tops("AND o.order_status='已完成'")
    all_tops, all_values = tops("")
    ok = len(done_tops) == 1 and len(all_tops) == 1 and done_tops == all_tops == [1]
    return ok, (f"九月已完成口径最高 {done_tops}（{done_values.get(1)} 件）；"
                f"全量口径最高 {all_tops}（{all_values.get(1)} 件）")


SCENARIOS = [
    ("A1", "指定商品九月零笔数零销量（≥2 个商品）", sc_a1),
    ("A2", "城市维度零值有意义（该城市 ≥1 笔已完成）", sc_a2),
    ("B1", "已取消订单涉及商品完全并列（≥3 且 max=min）", sc_b1),
    ("B2", "城市唯一最高 + 笔数/件数同商品（杭州）", sc_b2),
    ("B3", "城市并列最高（深圳，件数 ≥2 并列）", sc_b3),
    ("B4", "月份最高 ≠ 全区间最高，且两者都唯一", sc_b4),
    ("C1", "窗口内无退货、窗口外有退货", sc_c1),
    ("C2", "路线决策：只做空结果题（数据侧表现）", sc_c2),
    ("D1", "已完成成交价唯一、全量出现第二种价", sc_d1),
    ("D2", "发货省分布 ≠ 客户所在省分布 + 一致/不一致样本", sc_d2),
    ("D3", "「已删除」必须是状态历史最后一条且仍在 products", sc_d3),
    ("D4", "至少 2 个商品当前「未上架」", sc_d4),
    ("E1", "快照天数极差 ≥60 且来自创建/删除时点", sc_e1),
    ("F1", "库存极差最高唯一且领先 ≥5", sc_f1),
    ("F2", "九月销量最高者唯一", sc_f2),
]


def run_checks(conn: sqlite3.Connection):
    """跑 §五 10 条、§四 补充硬约束、§三 14 条。异常不静默：校验本身写错也要报出来。"""
    invariant_results, hard_results, scenario_results = [], [], []
    for bucket, table in ((invariant_results, INVARIANTS),
                          (hard_results, HARD_CONSTRAINTS),
                          (scenario_results, SCENARIOS)):
        for num, name, fn in table:
            try:
                ok, detail = fn(conn)
            except Exception as exc:                              # noqa: BLE001
                ok, detail = False, f"校验抛异常 {type(exc).__name__}: {exc}"
            bucket.append((num, name, ok, detail))
    return invariant_results, hard_results, scenario_results


# ============================================================ 十九、摘要
def table_counts(conn: sqlite3.Connection) -> dict:
    tables = ("categories", "products", "customers", "orders", "order_items",
              "order_status_history", "product_status_history",
              "inventory_movements", "inventory_daily")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


EXPECTED_RANGES = {
    "categories": (3, 3),
    "products": (8, 8),
    "customers": (50, 60),
    "orders": (100, 120),
    "order_items": (220, 300),
    "order_status_history": (352, 352),
    "product_status_history": (30, 40),
    "inventory_movements": (220, 300),
    "inventory_daily": (645, 665),
}


def check_row_counts(counts: dict) -> list[tuple[str, int, tuple[int, int], bool]]:
    rows = []
    for table, (low, high) in EXPECTED_RANGES.items():
        value = counts[table]
        rows.append((table, value, (low, high), low <= value <= high))
    return rows


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def print_report(seed, counts, invariant_results, hard_results, scenario_results,
                 conflict_report, injected, sha_map) -> None:
    print("=" * 78)
    print(f"seed_v2 造数摘要    seed = {seed}")
    print("=" * 78)

    print("\n[§6.2 场景占用冲突检查（构造前）]")
    for left, right, verdict in conflict_report:
        print(f"  {left} × {right}: {verdict}")

    print("\n[行数]")
    for table, value, (low, high), ok in check_row_counts(counts):
        print(f"  {'✅' if ok else '❌'} {table:<24} {value:>5}   目标 {low}~{high}")

    print("\n[§五 通用不变量 10 条]")
    for num, name, ok, detail in invariant_results:
        print(f"  {'✅' if ok else '❌'} #{num} {name}\n         {detail}")

    print("\n[§四 补充硬约束 7 条]")
    for num, name, ok, detail in hard_results:
        print(f"  {'✅' if ok else '❌'} {num} {name}\n         {detail}")

    print("\n[§三 场景验收 14 条（+ C2 路线决策）]")
    for num, name, ok, detail in scenario_results:
        print(f"  {'✅' if ok else '❌'} {num} {name}\n         {detail}")

    if injected:
        print("\n[脏库注入 §七]")
        for line in injected:
            print(f"  - {line}")

    if sha_map:
        print("\n[文件 sha256]")
        for path, digest in sha_map.items():
            print(f"  {path}: {digest}")


# ============================================================ 二十、主流程

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="扩容版造数脚本（110 单评测库）")
    parser.add_argument("--seed", type=int, default=20261029, help="随机种子，默认 20261029")
    parser.add_argument("--no-publish", action="store_true", help="只构建到 data/_tmp_*，不替换正式文件")
    args = parser.parse_args(argv)

    print(f"[1/6] 配额表自洽检查 + 场景占用冲突检查 + 生成数据集（seed={args.seed}）")
    dataset = build_dataset(args.seed)
    for line in dataset["quota_report"]:
        print(f"      §6.1 {line}")
    print(f"      订单 {len(dataset['orders'])} 单，"
          f"明细 {sum(len(o['items']) for o in dataset['orders'])} 条，"
          f"流水 {len(dataset['movements'])} 条，"
          f"快照 {len(dataset['daily_rows'])} 条")

    print("[2/6] 建临时干净库 data/_tmp_demo_v2_eval.sqlite3")
    conn = open_db(TMP_CLEAN_PATH)
    build(conn, dataset)

    print("[3/6] 校验干净库（§五 10 条 + §四 补充 7 条 + §三 14 条 + 行数配额）")
    counts = table_counts(conn)
    invariant_results, hard_results, scenario_results = run_checks(conn)
    range_results = check_row_counts(counts)
    failed = ([r for r in invariant_results if not r[2]]
              + [r for r in hard_results if not r[2]]
              + [r for r in scenario_results if not r[2]]
              + [(t, f"{t} 行数", ok, f"{v} 目标 {lo}~{hi}")
                 for t, v, (lo, hi), ok in range_results if not ok])
    conn.close()

    if failed:
        print_report(args.seed, counts, invariant_results, hard_results, scenario_results,
                     dataset["conflict_report"], [], {})
        print("\n[未发布] 存在失败项，data/ 下的原库与正式名均未改动：")
        for num, name, _ok, detail in failed:
            print(f"  ❌ {num} {name}：{detail}")
        TMP_CLEAN_PATH.unlink(missing_ok=True)
        return 1
    print("      全部通过")

    print("[4/6] 由干净库派生脏库 data/_tmp_demo_v2_eval_dirty.sqlite3")
    # 直接复制干净库文件，再注入五类语义异常（§七），保证脏库只比干净库多这五处
    TMP_DIRTY_PATH.unlink(missing_ok=True)
    shutil.copyfile(TMP_CLEAN_PATH, TMP_DIRTY_PATH)
    dirty_conn = sqlite3.connect(TMP_DIRTY_PATH)
    dirty_conn.execute("PRAGMA foreign_keys = ON")
    injected = inject_dirty(dirty_conn, dataset)
    dirty_inv, dirty_hard, _ = run_checks(dirty_conn)
    dirty_failed = sorted(num for num, _name, ok, _detail in dirty_inv if not ok)
    dirty_hard_failed = sorted(num for num, _name, ok, _detail in dirty_hard if not ok)
    dirty_counts = table_counts(dirty_conn)
    integrity = dirty_conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_rows = dirty_conn.execute("PRAGMA foreign_key_check").fetchall()
    dirty_conn.close()

    expected_dirty = {"3", "4", "7", "9", "10"}
    print("[5/6] 脏库自检：integrity_check={}, foreign_key_check={} 条违规，"
          "失败不变量={}，失败硬约束={}".format(integrity, len(fk_rows), dirty_failed,
                                              dirty_hard_failed))
    if integrity != "ok" or fk_rows or set(dirty_failed) != expected_dirty or dirty_hard_failed:
        print("[未发布] 脏库没有正好违反 3/4/7/9/10 这五条不变量（其余 5 条不变量与 7 条硬约束"
              "必须全过），按 §七 视为注入 bug。")
        TMP_CLEAN_PATH.unlink(missing_ok=True)
        TMP_DIRTY_PATH.unlink(missing_ok=True)
        return 1

    print("[6/6] 原子发布")
    sha_map = {}
    if args.no_publish:
        print("      按 --no-publish 跳过发布")
        sha_map[TMP_CLEAN_PATH.name] = file_sha256(TMP_CLEAN_PATH)
        sha_map[TMP_DIRTY_PATH.name] = file_sha256(TMP_DIRTY_PATH)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(TMP_CLEAN_PATH, CLEAN_DB_PATH)
        os.replace(TMP_DIRTY_PATH, DIRTY_DB_PATH)
        sha_map[str(CLEAN_DB_PATH.relative_to(PROJECT_ROOT))] = file_sha256(CLEAN_DB_PATH)
        sha_map[str(DIRTY_DB_PATH.relative_to(PROJECT_ROOT))] = file_sha256(DIRTY_DB_PATH)

    print()
    print_report(args.seed, counts, invariant_results, hard_results, scenario_results,
                 dataset["conflict_report"], injected, sha_map)
    print("\n脏库行数：" + "  ".join(f"{t}={n}" for t, n in dirty_counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
