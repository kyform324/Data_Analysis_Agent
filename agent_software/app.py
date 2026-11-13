"""Data Analysis Agent 的 Gradio 演示页面。

布局：
    顶栏      左：标题 + 项目解释        右：数据库胶囊（干净库 / 脏库）
    左上      提问框（固定高）+ 提问 / 清空
    左下      结果表格区（固定高、带边框、超出可滚动）
    右下      卡片区，Tab 切换「回复」与「工具轨迹」（固定高、带边框、超出可滚动）

约定：
- 结果区只放表格；「回复」里把表格去掉，只留文字说明，避免两处重复。
- 一次提问可能产生多条成功查询：默认展示最近一条，多条时可下拉切换；程序不再猜"哪条是主结果"。
- 数据库切换用原生 Radio + CSS 胶囊，选中状态由浏览器维护，不依赖动态 class。

本文件只调用后端，不改动 main.py / tools/ 的任何逻辑。
"""
import html
import json
import re
import time

import gradio as gr

from main import run_agent
from tools.query_sql import (
    set_db, get_query_results, get_result_by_id, clear_query_results,
)

EXAMPLES = [
    "九月都卖出去啥了？各卖了多少？",
    "按品类算，各卖了多少钱？",
    "现在哪个商品快卖断货了？",
    "一共退过几次货？都退了什么？",
    "哪三个客户花钱最多？",
    "九月完成了多少笔订单？",
]

_EMPTY = "<div class='dsh-empty'>还没有结果</div>"
_EMPTY_REPLY = "_提问后，这里会显示分析结论。_"
_EMPTY_TRACE = "<div class='dsh-empty'>还没有工具调用</div>"
_LOADING = (
    "<div class='dsh-loading'>"
    "<span class='dsh-spinner'></span>正在查询中……</div>"
)
_LOADING_MD = "正在查询中……"


def _with_result_head(body: str) -> str:
    """结果卡片的内嵌标题，和在输入卡里的「提问」同一位置。"""
    return f"<div class='dsh-card-head'>结果</div>{body}"


_RESULT_EMPTY = _with_result_head(_EMPTY)
_RESULT_LOADING = _with_result_head(_LOADING)
# 本次提问没有任何一条成功查询时显示的空态：不显示上一题残影，也不从回答里猜表
_NO_RESULT = _with_result_head(
    "<div class='dsh-empty'>本次没有可展示的成功查询结果</div>")

CSS = """
/* ───────── 设计变量 ───────── */
:root {
  --dsh-bg: #f5f6f8;
  --dsh-card: #ffffff;
  --dsh-soft: #f7f8fa;
  --dsh-border: #e4e8ee;
  --dsh-border-strong: #d5dbe4;
  --dsh-text: #1b1f27;
  --dsh-muted: #6a7280;
  --dsh-accent: #4f46e5;
  --dsh-accent-hover: #4338ca;
  --dsh-accent-soft: #eef0ff;
  --dsh-btn-primary-text: #ffffff;
  --dsh-pill-shadow: 0 1px 2px rgba(17,24,39,.12);
  --dsh-code-bg: #f6f7f9;
  --dsh-ok-bg: #e7f6ed;  --dsh-ok-fg: #177245;
  --dsh-fail-bg: #fdecec; --dsh-fail-fg: #c0392b;
  --dsh-shadow: 0 1px 2px rgba(17,24,39,.04), 0 6px 18px rgba(17,24,39,.04);
  --dsh-radius: 14px;
  --dsh-result-h: 420px;
}
/* Gradio 的暗色由 body.dark 驱动，不跟媒体查询，这里显式覆盖一次 */
body.dark {
  --dsh-bg: #0d1117; --dsh-card: #161b22; --dsh-soft: #1b2129;
  --dsh-border: #2d333b; --dsh-border-strong: #3d444d;
  --dsh-text: #e6edf3; --dsh-muted: #9198a3;
  --dsh-accent: #8b93ff; --dsh-accent-hover: #a5abff;
  --dsh-accent-soft: rgba(99,102,241,.16);
  --dsh-btn-primary-text: #0b0f14;
  --dsh-pill-shadow: 0 1px 2px rgba(0,0,0,.55);
  --dsh-code-bg: #0f141b;
  --dsh-ok-bg: #12301f; --dsh-ok-fg: #56d364;
  --dsh-fail-bg: #3a1d20; --dsh-fail-fg: #ff7b72;
  --dsh-shadow: none;
}

/* ───────── 页面骨架 ───────── */
.gradio-container {
  width: 100% !important;
  max-width: 1240px !important;
  margin: 0 auto !important;
  background: transparent !important;
}
body, .gradio-container {
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI",
               "PingFang SC", "Microsoft YaHei", sans-serif !important;
  color: var(--dsh-text);
}
gradio-app, .gradio-container, .main.fillable { background: var(--dsh-bg) !important; }
.main.fillable { padding: 28px 32px 28px !important; }
footer { display: none !important; }

/* ───────── 顶栏 ───────── */
.dsh-header-wrap, .dsh-header-wrap .html-container { padding: 0 !important; }
.dsh-header { padding: 2px 0 6px; }
.dsh-title {
  font-size: 30px; font-weight: 700; letter-spacing: -.02em;
  line-height: 1.2; color: var(--dsh-text); margin: 0;
}
.dsh-sub {
  margin-top: 8px; font-size: 14px; line-height: 1.65;
  color: var(--dsh-muted); max-width: 640px;
}

/* 数据库胶囊：外框浅灰、选中白底浮起 */
.pill-radio {
  display: flex !important; align-items: flex-start !important;
  justify-content: flex-end; flex-grow: 0 !important; align-self: stretch;
  width: 100% !important; min-width: 0 !important; height: auto !important;
  padding: 0 !important; margin: 6px 0 0 !important;
  border: none !important; background: transparent !important;
  box-shadow: none !important; overflow: visible !important;
}
.pill-radio .wrap:has(label) {
  display: inline-flex !important; flex-wrap: nowrap !important;
  align-items: center !important; height: auto !important;
  gap: 2px !important; padding: 3px !important;
  border: 1px solid var(--dsh-border-strong) !important;
  border-radius: 999px !important;
  background: var(--dsh-soft) !important;
}
.pill-radio label {
  margin: 0 !important; padding: 3px 16px !important;
  border: none !important; border-radius: 999px !important;
  background: transparent !important; color: var(--dsh-muted) !important;
  font-size: 12.5px !important; font-weight: 600 !important;
  line-height: 1.6 !important; box-shadow: none !important;
  cursor: pointer !important; transform: none !important;
}
.pill-radio label:hover { color: var(--dsh-text) !important; background: transparent !important; }
.pill-radio label.selected,
.pill-radio label:has(input:checked) {
  background: var(--dsh-card) !important; color: var(--dsh-text) !important;
  box-shadow: var(--dsh-pill-shadow) !important;
}
.pill-radio label + label { border-left: none !important; }
.pill-radio input { display: none !important; }

/* ───────── 两栏 ───────── */
.dsh-left, .dsh-right { gap: 14px !important; }
/* contain:size 让右栏不参与撑高整行：高度只由左栏决定，右栏再随行拉伸 */
.dsh-right { position: relative; align-self: stretch; contain: size; overflow: visible !important; }
.dsh-right > .tabs { flex: 1 1 auto; min-height: 0; }
/* 统计条目挂在卡片下边框外侧：不占卡片高度，两张卡片的下边框仍然对齐 */
.dsh-meta-wrap {
  position: absolute !important; top: 100% !important; right: 0 !important;
  margin-top: 10px !important; z-index: 4;
  width: auto !important; max-width: none !important;
  padding: 0 !important; pointer-events: none !important;
}
.dsh-meta-wrap:not(:has(.dsh-meta)) { display: none !important; }

/* ───────── 输入卡片 ───────── */
.form:has(.dsh-input) {
  background: transparent !important;
  border: none !important; box-shadow: none !important;
  padding: 0 !important;
}
.dsh-input {
  padding: 0 !important; border: none !important;
  background: transparent !important; box-shadow: none !important;
}
.dsh-input label.container {
  display: block !important; padding: 12px 14px 10px !important;
  border: 1px solid var(--dsh-border) !important;
  border-radius: var(--dsh-radius) !important;
  background: var(--dsh-card) !important;
  box-shadow: var(--dsh-shadow) !important;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.dsh-input label.container:focus-within {
  border-color: var(--dsh-accent) !important;
  box-shadow: 0 0 0 3px var(--dsh-accent-soft) !important;
}
.dsh-input span[data-testid="block-info"] {
  display: block !important; margin: 0 0 8px !important; padding: 0 !important;
  background: transparent !important; border: none !important;
  border-radius: 0 !important; color: var(--dsh-muted) !important;
  font-size: 12px !important; font-weight: 600 !important;
  letter-spacing: .04em; line-height: 1.4;
}
.dsh-input textarea {
  background: transparent !important; border: none !important;
  box-shadow: none !important; padding: 0 !important;
  color: var(--dsh-text) !important;
  font-size: 14.5px !important; line-height: 1.65 !important;
  resize: none !important;
}
.dsh-input textarea::placeholder { color: #9aa3af !important; }
.dsh-input textarea:focus { outline: none !important; }

/* 按钮：叠放在输入卡右上角，与「提问」同一条基线 */
.dsh-left { position: relative; }
.dsh-actions {
  position: absolute !important; top: 8px; right: 14px; z-index: 50;
  width: fit-content !important; pointer-events: none !important;
  gap: 8px !important; justify-content: flex-end !important;
}
.dsh-actions button { pointer-events: auto !important; }
.dsh-actions button {
  flex: 0 0 auto !important; width: auto !important;
  min-width: 0 !important; max-width: none !important;
  height: 26px !important; min-height: 26px !important;
  padding: 0 14px !important; border-radius: 8px !important;
  font-size: 12px !important; font-weight: 600 !important; line-height: 1 !important;
  box-shadow: none !important; transition: background .15s ease, border-color .15s ease;
}
.dsh-actions button.primary {
  background: var(--dsh-accent) !important; color: var(--dsh-btn-primary-text) !important;
  border: 1px solid var(--dsh-accent) !important; padding: 0 16px !important;
}
.dsh-actions button.primary:hover { background: var(--dsh-accent-hover) !important; }
.dsh-actions button.secondary {
  background: var(--dsh-card) !important; color: var(--dsh-text) !important;
  border: 1px solid var(--dsh-border-strong) !important;
}
.dsh-actions button.secondary:hover { background: var(--dsh-soft) !important; }

/* ───────── 查询选择器 ───────── */
/* 标题与行数提示排在同一行，标题由 _trunc_html 渲染，Dropdown 自带 label 关掉 */
.dsh-trunc-wrap {
  margin: -2px 0 -8px !important;
  padding: 0 !important; width: auto !important; min-width: 0 !important;
}
.dsh-trunc-wrap .html-container,
.dsh-trunc-wrap .prose {
  padding: 0 !important; margin: 0 !important; min-height: 0 !important;
}
.dsh-trunc-wrap:not(:has(.dsh-meta)) { display: none !important; }
.dsh-meta.dsh-meta-left { justify-content: flex-start; }
.dsh-meta span.dsh-sel-chip {
  padding: 2px 8px; border-radius: 6px;
  background: var(--dsh-accent-soft); border: 1px solid transparent;
  color: var(--dsh-accent); font-weight: 600;
}

/* ───────── 结果卡片 ───────── */
.dsh-card-head {
  flex: 0 0 auto;
  margin: 0 0 10px; color: var(--dsh-muted);
  font-size: 12px; font-weight: 600; letter-spacing: .04em; line-height: 1.4;
}
.dsh-left .block.scroll-area {
  height: var(--dsh-result-h) !important;
  padding: 0 !important;
  border: 1px solid var(--dsh-border) !important;
  border-radius: var(--dsh-radius) !important;
  background: var(--dsh-card) !important;
  box-shadow: var(--dsh-shadow) !important;
  overflow: auto !important;
  box-sizing: border-box !important;
  display: flex !important; flex-direction: column !important;
}
.dsh-left .block.scroll-area > .html-container {
  display: flex !important; flex-direction: column !important;
  flex: 1 1 auto !important; min-height: 0 !important;
  box-sizing: border-box !important;
  padding: 14px 18px !important;
}
.dsh-left .block.scroll-area > .html-container > .prose {
  display: flex !important; flex-direction: column !important;
  flex: 1 1 auto !important; min-height: 0 !important;
}

/* elem_classes 会同时落到内层 .prose 上，这里把内层样式清掉，避免双层边框 */
.prose.scroll-area {
  height: auto !important; padding: 0 !important; margin: 0 !important;
  border: none !important; border-radius: 0 !important;
  background: transparent !important; overflow: visible !important;
}

/* 滚动条 */
.block.scroll-area::-webkit-scrollbar { width: 10px; height: 10px; }
.block.scroll-area::-webkit-scrollbar-thumb {
  background: var(--dsh-border-strong); border-radius: 999px;
  border: 2px solid var(--dsh-card);
}
.block.scroll-area::-webkit-scrollbar-track { background: transparent; }
.block.scroll-area { scrollbar-width: thin; scrollbar-color: var(--dsh-border-strong) transparent; }

/* ───────── 右侧卡片 ───────── */
.dsh-right .tabs {
  display: flex !important; flex-direction: column !important;
  min-height: 0 !important;
  border: 1px solid var(--dsh-border) !important;
  border-radius: var(--dsh-radius) !important;
  background: var(--dsh-card) !important;
  box-shadow: var(--dsh-shadow) !important;
  overflow: hidden !important;
}
.dsh-right .tab-wrapper {
  flex: 0 0 auto !important; height: auto !important;
  margin: 0 !important;
}
.dsh-right .tab-container[role="tablist"] {
  display: flex !important; align-items: center !important;
  gap: 24px !important; height: auto !important; min-height: 46px !important;
  padding: 0 20px !important;
  border-bottom: 1px solid var(--dsh-border) !important;
  overflow: visible !important;
}
.dsh-right .tab-container[role="tablist"]:after { display: none !important; }
.dsh-right .tab-container button[role="tab"] {
  height: 46px !important; margin: 0 !important; padding: 0 !important;
  background: transparent !important; border: none !important;
  border-bottom: 2px solid transparent !important; border-radius: 0 !important;
  color: var(--dsh-muted) !important;
  font-size: 13.5px !important; font-weight: 600 !important;
  box-shadow: none !important;
}
.dsh-right .tab-container button[role="tab"]:hover {
  background: transparent !important; color: var(--dsh-text) !important;
}
.dsh-right .tab-container button[role="tab"]::after { display: none !important; }
.dsh-right .tab-container button[role="tab"].selected {
  color: var(--dsh-accent) !important;
  border-bottom-color: var(--dsh-accent) !important;
}
.dsh-right .tabitem {
  flex: 1 1 auto !important;
  min-height: 0 !important; padding: 0 !important;
}
.dsh-right .tabitem > .column {
  display: flex !important; flex-direction: column !important;
  flex: 1 1 auto !important; min-height: 0 !important;
}
.dsh-right .block.scroll-area {
  flex: 1 1 auto !important; height: auto !important; min-height: 0 !important;
  padding: 0 !important;
  border: none !important; border-radius: 0 !important;
  background: transparent !important; box-shadow: none !important;
  overflow: auto !important;
}
.dsh-right .block.scroll-area > .html-container,
.dsh-right .block.scroll-area [data-testid="markdown-wrapper"] { padding: 16px 20px !important; }
.dsh-right .block.scroll-area > .html-container {
  min-height: 100% !important; box-sizing: border-box !important;
}
.dsh-meta-wrap .html-container { padding: 0 !important; min-height: 0 !important; }

/* 回复正文 */
.dsh-right .md { font-size: 14px; line-height: 1.75; color: var(--dsh-text) !important; }
.dsh-right .md em { color: var(--dsh-muted) !important; font-style: normal; }
.dsh-right .md p { margin: 0 0 10px; }
.dsh-right .md h1, .dsh-right .md h2, .dsh-right .md h3,
.dsh-right .md h4, .dsh-right .md h5, .dsh-right .md h6 {
  margin: 2px 0 8px; font-size: 14.5px; font-weight: 650;
}
.dsh-right .md ul, .dsh-right .md ol { margin: 0 0 10px; padding-left: 20px; }
.dsh-right .md li { margin: 2px 0; }
.dsh-right .md blockquote {
  margin: 0 0 12px; padding: 8px 12px;
  background: var(--dsh-accent-soft); border-left: 3px solid var(--dsh-accent);
  border-radius: 8px; color: var(--dsh-text);
}
.dsh-right .md code {
  background: var(--dsh-code-bg); border: 1px solid var(--dsh-border);
  border-radius: 6px; padding: 1px 5px; font-size: 12.5px;
}
.dsh-right .md pre {
  background: var(--dsh-code-bg); border: 1px solid var(--dsh-border);
  border-radius: 8px; padding: 10px 12px; overflow: auto;
}

/* ───────── 结果表格 ───────── */
.dsh-left .dsh-table {
  width: 100% !important; margin: 0 !important;
  border-collapse: separate !important; border-spacing: 0 !important;
  border: none !important; font-size: 13px; color: var(--dsh-text);
}
.dsh-left .dsh-table th {
  position: sticky; top: 0; z-index: 2;
  padding: 9px 12px !important; text-align: left; white-space: nowrap;
  background: var(--dsh-soft) !important; color: var(--dsh-muted) !important;
  font-size: 12px !important; font-weight: 600 !important; letter-spacing: .02em;
  border: none !important; border-bottom: 1px solid var(--dsh-border) !important;
}
.dsh-left .dsh-table td {
  padding: 9px 12px !important; white-space: nowrap;
  color: var(--dsh-text) !important;
  border: none !important; border-bottom: 1px solid var(--dsh-border) !important;
}
.dsh-left .dsh-table tbody tr:last-child td { border-bottom: none !important; }
.dsh-left .dsh-table tbody tr:hover td { background: var(--dsh-soft); }
.dsh-left .dsh-table td.num { text-align: right; font-variant-numeric: tabular-nums; }

/* ───────── 空态 / 加载态 ───────── */
.dsh-empty, .dsh-loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; flex: 1 1 auto; height: auto; min-height: 180px;
  color: var(--dsh-muted); font-size: 13.5px; text-align: center;
}
.dsh-empty:before {
  content: ""; width: 40px; height: 30px;
  border: 1.5px dashed var(--dsh-border-strong); border-radius: 8px;
}
.dsh-loading { flex-direction: row; gap: 10px; }
.dsh-spinner {
  width: 16px; height: 16px; flex: none;
  border: 2px solid var(--dsh-border-strong); border-top-color: var(--dsh-accent);
  border-radius: 50%; display: inline-block; animation: dsh-spin .7s linear infinite;
}
@keyframes dsh-spin { to { transform: rotate(360deg); } }

/* ───────── 工具轨迹 ───────── */
.dsh-trace { display: flex; flex-direction: column; gap: 10px; }
.dsh-step {
  padding: 10px 12px;
  background: var(--dsh-soft);
  border: 1px solid var(--dsh-border);
  border-radius: 10px;
}
.dsh-step-head {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
.dsh-step-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px; font-weight: 600; color: var(--dsh-text);
}
.dsh-step pre {
  margin: 8px 0 0; padding: 10px 12px;
  background: var(--dsh-code-bg); border: 1px solid var(--dsh-border);
  border-radius: 8px; color: var(--dsh-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word;
}
.dsh-note { margin-top: 6px; color: var(--dsh-muted); font-size: 12px; }
.dsh-fail-step { color: var(--dsh-fail-fg); font-size: 13px; }
.dsh-badge {
  display: inline-flex; align-items: center; flex: none;
  padding: 1px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 600; line-height: 1.7;
}
.dsh-ok { background: var(--dsh-ok-bg); color: var(--dsh-ok-fg); }
.dsh-fail { background: var(--dsh-fail-bg); color: var(--dsh-fail-fg); }

/* ───────── 运行信息 ───────── */
.dsh-meta {
  display: flex; flex-wrap: nowrap; gap: 6px; justify-content: flex-end;
  padding: 0 2px; color: var(--dsh-muted); font-size: 12px; white-space: nowrap;
}
.dsh-meta span {
  padding: 2px 10px; border-radius: 999px;
  background: var(--dsh-card); border: 1px solid var(--dsh-border);
}
/* 截断提示：由程序补，不依赖模型转述 */
.dsh-meta span.dsh-trunc {
  background: #fff8c5; border-color: #d4a72c; color: #9a6700; font-weight: 600;
}

/* ───────── 示例问题 ───────── */
.dsh-examples-title h4 {
  margin: 14px 0 2px !important; padding: 0 !important;
  color: var(--dsh-muted) !important;
  font-size: 13px !important; font-weight: 600 !important;
  letter-spacing: .02em;
}
.dsh-examples {
  display: grid !important; grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
  gap: 10px !important;
}
.dsh-examples button {
  height: auto !important; min-height: 40px !important;
  padding: 8px 14px !important;
  border: 1px solid var(--dsh-border) !important; border-radius: 10px !important;
  background: var(--dsh-card) !important; color: var(--dsh-text) !important;
  font-size: 12.5px !important; font-weight: 500 !important;
  line-height: 1.45 !important; text-align: left !important;
  white-space: normal !important; box-shadow: none !important;
  transition: border-color .15s ease, color .15s ease, background .15s ease;
}
.dsh-examples button:hover {
  border-color: var(--dsh-accent) !important;
  background: var(--dsh-accent-soft) !important;
  color: var(--dsh-accent) !important;
}
"""


# ────────────────────────── 渲染工具 ──────────────────────────

def _inline(text: str) -> str:
    """转义 + 处理 **粗体**，用于表格单元格和普通段落。"""
    s = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def extract_first_table(md: str):
    """取出第一个 Markdown 表格块；没有就返回 None。"""
    block, blocks = [], []
    for line in md.splitlines():
        if line.strip().startswith("|"):
            block.append(line)
        else:
            if len(block) >= 2:
                blocks.append("\n".join(block))
            block = []
    if len(block) >= 2:
        blocks.append("\n".join(block))
    return blocks[0] if blocks else None


def strip_tables(md: str) -> str:
    """去掉 Markdown 表格块，保留其余文字（用于「回复」栏，避免和结果区重复）。"""
    out, skip = [], False
    for line in md.splitlines():
        if line.strip().startswith("|"):
            skip = True
            continue
        if skip:
            skip = False
            if not line.strip():
                continue          # 吞掉表格后面紧跟的空行
        out.append(line)
    text = "\n".join(out).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


_NUM = re.compile(r"^[-+]?[\d,]+(?:\.\d+)?%?$")


def _render_table(rows) -> str:
    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]] if len(rows) > 2 else []
    th = "".join(f"<th>{_inline(c)}</th>" for c in header)
    trs = []
    for r in body:
        tds = "".join(
            f"<td class='num'>{_inline(c)}</td>" if _NUM.match(c) else f"<td>{_inline(c)}</td>"
            for c in r
        )
        trs.append(f"<tr>{tds}</tr>")
    return (f"<table class='dsh-table'><thead><tr>{th}</tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def _md_to_html(text: str) -> str:
    """极简 Markdown 渲染：表格、标题、粗体、段落。"""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")) + 2, 6)
            out.append(f"<h{level}>{_inline(line.lstrip('#').strip())}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    return "".join(out)


def _render_left(answer: str) -> str:
    """左下结果区：卡片内嵌「结果」标题，只放表格。"""
    if not answer or not answer.strip():
        return _RESULT_EMPTY
    table = extract_first_table(answer)
    if table:
        body = _md_to_html(table)
    else:
        body = ("<div class='dsh-empty'>这次回答里没有表格，"
                "完整内容见右侧「回复」。</div>")
    return _with_result_head(body)


def _render_cache(cached: dict) -> str:
    """用缓存的结构化结果渲染左栏表格（优先于从 Agent 回答里抽表）。"""
    cols = cached.get("columns") or []
    rows = cached.get("rows") or []
    if not cols:
        return _EMPTY
    th = "".join(
        f"<th style='padding:8px 12px;background:#f6f8fa;border:1px solid #d0d7de;"
        f"text-align:left;font-weight:600;white-space:nowrap'>{_inline(str(c))}</th>"
        for c in cols
    )
    trs = []
    for r in rows:
        tds = "".join(
            f"<td style='padding:7px 12px;border:1px solid #d0d7de;white-space:nowrap'>"
            f"{_inline(str(v))}</td>" for v in r
        )
        trs.append(f"<tr>{tds}</tr>")
    return (f"<table style='border-collapse:collapse;font-size:13px;width:100%;margin:4px 0'>"
            f"<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>")


def _fmt_trace(tool_trace) -> str:
    if not tool_trace:
        return _EMPTY_TRACE

    parts = []
    for t in tool_trace:
        seq = html.escape(str(t.get("seq", "")))
        name = html.escape(str(t.get("tool", "")))
        ok = bool(t.get("ok"))
        badge = ("<span class='dsh-badge dsh-ok'>ok</span>" if ok
                 else f"<span class='dsh-badge dsh-fail'>"
                      f"{html.escape(str(t.get('error_type')))}</span>")

        args = t.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args.replace("'", '"'))
            except Exception:
                pass

        detail = ""
        if isinstance(args, dict):
            if args.get("sql"):
                detail = f"<pre>{html.escape(args['sql'])}</pre>"
            elif args.get("themes"):
                detail = (f"<div class='dsh-note'>themes: "
                          f"{html.escape(', '.join(map(str, args['themes'])))}</div>")
            elif args.get("table"):
                detail = (f"<div class='dsh-note'>table: "
                          f"{html.escape(str(args['table']))}</div>")
            elif args:
                detail = (f"<div class='dsh-note'>"
                          f"{html.escape(str(args)[:180])}</div>")

        parts.append(
            f"<div class='dsh-step'><div class='dsh-step-head'>"
            f"<span class='dsh-step-name'>{seq}. {name}</span>{badge}</div>"
            f"{detail}</div>"
        )

    return f"<div class='dsh-trace'>{''.join(parts)}</div>"


# ────────────────────────── 交互逻辑 ──────────────────────────

def loading_state():
    return (_RESULT_LOADING, _LOADING_MD, _LOADING, "",
            gr.update(), "")


def _selector_choices(results):
    """下拉项：查询 N · 用途 · 共 X 行 · SQL 片段（截 60 字），让人能认出是哪条查询。"""
    choices = []
    for r in results:
        sql = " ".join((r.get("sql") or "").split())
        if len(sql) > 60:
            sql = sql[:60] + "…"
        role = r.get("role") or "analysis"
        label = f"查询 {r['result_id']} · {role} · 共 {r.get('total', 0)} 行 · {sql}"
        choices.append((label, r["result_id"]))
    return choices


def _pick_default(results):
    """默认展示哪一条：最后一个 display → 最后一个 analysis → 最后一次成功查询。

    display 由模型在调用时显式声明（query_sql 的 role 参数）；
    模型没声明就退回最后一条成功查询，行为不比改造前差。
    """
    for role in ("display", "analysis"):
        for r in reversed(results):
            if (r.get("role") or "analysis") == role:
                return r
    return results[-1]


def _trunc_html(result, with_label=False):
    """行数提示：只跟着当前选中的那条结果，不读全局状态。

    with_label=True 时在同一行前面补上「展示哪一次查询」标题芯片，
    这样标题和行数提示挨在一起，而不是分处两行。
    """
    if not result:
        return ""
    total = result.get("total", 0)
    if result.get("truncated"):
        spans = [f"<span class='dsh-trunc'>当前查询共 {total} 行，"
                 f"Agent 仅分析前 {result.get('preview_count', 25)} 行</span>"]
    else:
        spans = [f"<span class='dsh-trunc'>当前查询共 {total} 行，已全部展示</span>"]
    if result.get("display_truncated"):
        spans.append(f"<span class='dsh-trunc'>界面只展示前 "
                     f"{len(result.get('rows') or [])} 行</span>")
    chip = ("<span class='dsh-sel-chip'>展示哪一次查询</span>"
            if with_label else "")
    return f"<div class='dsh-meta dsh-meta-left'>{chip}{''.join(spans)}</div>"


def switch_result(result_id):
    """下拉切换：只换左侧表格与行数提示，不动 Agent 的回答与上下文。"""
    result = get_result_by_id(result_id) if result_id is not None else None
    if not result:
        return _NO_RESULT, ""
    return (_render_cache(result),
            _trunc_html(result, with_label=len(get_query_results()) > 1))


def ask(question, db_label):
    question = (question or "").strip()
    if not question:
        return (_RESULT_EMPTY, "**请先输入问题。**", _EMPTY_TRACE, "",
                gr.update(), "")

    db_key = "dirty" if "脏" in (db_label or "") else "clean"
    set_db(db_key)
    clear_query_results()        # 开新题先清，避免沿用上一题结果

    t0 = time.time()
    try:
        result = run_agent(question)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        return (_RESULT_EMPTY, f"**运行出错**：`{err}`",
                f"<div class='dsh-step dsh-fail-step'>{html.escape(err)}</div>",
                "", gr.update(choices=[], value=None, visible=False), "")
    elapsed = time.time() - t0

    full = result.get("answer") or "（模型没有返回内容）"
    if not result.get("ok"):
        full = f"> ⚠️ {result.get('error') or '未正常收尾'}\n\n{full}"

    has_table = extract_first_table(full) is not None
    text_only = strip_tables(full) if has_table else full
    if has_table:
        text_only = "> 表格见左侧「结果」区。\n\n" + text_only

    steps = result.get("steps") or 0
    tokens = result.get("tokens") or 0
    # 底部只放"这次提问"的信息；行数提示跟着选中的查询走，不放在这里
    meta = (f"<div class='dsh-meta'>"
            f"<span>{'脏库' if db_key == 'dirty' else '干净库'}</span>"
            f"<span>{steps} 步</span><span>{tokens:,} tokens</span>"
            f"<span>{elapsed:.1f}s</span></div>")

    results = get_query_results()
    if not results:
        # 没有任何成功查询：明确空态，不留上一题残影、也不从回答里猜表
        return (_NO_RESULT, text_only,
                _fmt_trace(result.get("tool_trace") or []), meta,
                gr.update(choices=[], value=None, visible=False),
                "<div class='dsh-meta'><span>本次没有成功的查询结果</span></div>")

    # 默认展示：模型声明为 display 的那条（没有就退回最后一条成功查询）
    selected = _pick_default(results)
    selector = gr.update(choices=_selector_choices(results),
                         value=selected["result_id"],
                         visible=len(results) > 1)

    return (_render_cache(selected), text_only,
            _fmt_trace(result.get("tool_trace") or []), meta,
            selector, _trunc_html(selected, with_label=len(results) > 1))


def build():
    with gr.Blocks(title="Data Analyst Agent") as demo:
        # ── 顶栏：标题 + 数据库胶囊 ──
        with gr.Row(equal_height=True, elem_classes=["dsh-header-row"]):
            with gr.Column(scale=4, min_width=380):
                gr.HTML(
                    "<div class='dsh-header'>"
                    "<div class='dsh-title'>Data Analyst Agent</div>"
                    "<div class='dsh-sub'>基于 DeepSeek Function Calling 的自然语言数据分析："
                    "由模型自己决定查哪些表、写什么 SQL。</div>"
                    "</div>",
                    elem_classes=["dsh-header-wrap"],
                )
            with gr.Column(scale=2, min_width=220):
                db = gr.Radio(
                    choices=["干净库", "脏库"],
                    value="干净库",
                    show_label=False,
                    container=False,
                    elem_classes=["pill-radio"],
                )

        with gr.Row(equal_height=False):
            # ── 左侧：输入 + 结果 ──
            with gr.Column(scale=3, elem_classes=["dsh-left"]):
                question = gr.Textbox(
                    label="提问",
                    placeholder="例如：九月都卖出去啥了？",
                    lines=5,
                    max_lines=5,
                    elem_classes=["dsh-input"],
                )
                with gr.Row(elem_classes=["dsh-actions"]):
                    clear = gr.Button("清空", size="sm", scale=0)
                    submit = gr.Button("提问", variant="primary", size="sm", scale=0)

                trunc = gr.HTML("", elem_classes=["dsh-trunc-wrap"])
                selector = gr.Dropdown(
                    show_label=False,
                    choices=[],
                    value=None,
                    visible=False,
                    interactive=True,
                    elem_classes=["dsh-selector"],
                )
                left = gr.HTML(_RESULT_EMPTY, elem_classes=["scroll-area"])

            # ── 右侧：卡片切换 ──
            with gr.Column(scale=2, elem_classes=["dsh-right"]):
                with gr.Tabs():
                    with gr.Tab("回复"):
                        answer = gr.Markdown(_EMPTY_REPLY, elem_classes=["scroll-area"])
                    with gr.Tab("工具轨迹"):
                        trace = gr.HTML(_EMPTY_TRACE, elem_classes=["scroll-area"])
                meta = gr.HTML(elem_classes=["dsh-meta-wrap"])

        gr.Markdown("#### 试试这些", elem_classes=["dsh-examples-title"])
        with gr.Row(elem_classes=["dsh-examples"]):
            for ex in EXAMPLES:
                gr.Button(ex, size="sm").click(lambda x=ex: x, outputs=question)

        _OUTS = [left, answer, trace, meta, selector, trunc]
        submit.click(loading_state, outputs=_OUTS) \
              .then(ask, inputs=[question, db], outputs=_OUTS)
        question.submit(loading_state, outputs=_OUTS) \
                .then(ask, inputs=[question, db], outputs=_OUTS)

        selector.change(switch_result, inputs=[selector], outputs=[left, trunc])

        clear.click(
            lambda: ("", _RESULT_EMPTY, _EMPTY_REPLY, _EMPTY_TRACE, "",
                     gr.update(choices=[], value=None, visible=False), ""),
            outputs=[question] + _OUTS,
        )

    return demo


if __name__ == "__main__":
    build().queue(default_concurrency_limit=1).launch(theme=gr.themes.Soft(), css=CSS)
