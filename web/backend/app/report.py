"""班次 / 自定义时段报表（Excel）生成：看板上能看到的东西全部落进 .xlsx。

一个工作簿对应一段「时段」：班次报表是一条 12 h 的时段，自定义时段报表则是任意
起止（分钟精度）。两者共用同一套段结构、同一套口径，只有标题/称谓/文件名不同。

设计要点
--------
* **口径同源**：换算全部走 `app.convert`，阈值走 `alarms.effective_alarms`，
  统计走与 `/api/summary` 完全相同的那段 SQL —— 报表里的每个数字都必须能在网页上
  逐一对上。报表自己**不重算、不另立口径**；干矿量也走与看板相同的抽样与配对规则
  （见 `app.ore`）。
* **图表**：matplotlib（Agg 后端）渲成 PNG 再嵌进 xlsx。中文字体在运行时探测，
  探测不到就退化成英文标签 —— 宁可标签是英文，也不要一片方块。
* **重依赖延迟导入**：matplotlib 只在真要出图时才 import，看板本身不该为报表功能
  付启动开销（matplotlib 首次 import 约 0.4 s）。
* matplotlib **不是线程安全**的，出图走模块级锁串行化。
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from . import convert, flow, ore
from .shifts import Shift

log = logging.getLogger("ecas.backend.report")

#: 与网页图表一致的配色（TrendChart.tsx）
COLOR_LINE = "#22c55e"
COLOR_HIGH = "#f59e0b"
COLOR_LOW = "#ef4444"
COLOR_AVG = "#475569"
#: 流量与干矿量另用两色，避免和浓度曲线混淆
COLOR_FLOW = "#0ea5e9"
COLOR_ORE = "#7c3aed"

#: 单张图的像素尺寸（约 130 dpi × 10.0 × 3.8 in）
CHART_PX = (980, 372)


# ---------------------------------------------------------------- 中文字体探测


#: 常见中文字体位置：macOS 开发机 + Linux 服务器（含 OpenCloudOS / CentOS / Debian）
_CJK_FONT_PATHS: tuple[str, ...] = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

_CJK_FONT_GLOBS: tuple[str, ...] = (
    "/usr/share/fonts/**/NotoSansCJK*.tt[cf]",
    "/usr/share/fonts/**/SourceHanSans*.tt[cf]",
    "/usr/share/fonts/**/wqy-*.tt[cf]",
    "/usr/share/fonts/**/*CJK*.otf",
)

#: 探针字：报表里真实会出现的字。只查一个"中"字不够——某些字体只覆盖了部分 CJK 区。
_PROBE_CHARS = "浓度计班次报表电流未补偿状态时间原始值"


@lru_cache(maxsize=64)
def _has_cjk_glyphs(path: str) -> bool:
    """字体文件里真的有这些中文字形吗（光"文件存在"不作数）。"""
    try:
        from fontTools.ttLib import TTCollection, TTFont

        if path.lower().endswith(".ttc"):
            font = TTCollection(path, lazy=True).fonts[0]
        else:
            font = TTFont(path, lazy=True)
        cmap = font.getBestCmap()
    except Exception:  # 字体损坏 / 格式不认识 —— 换下一个候选
        return False
    return bool(cmap) and all(ord(ch) in cmap for ch in _PROBE_CHARS)


@lru_cache(maxsize=1)
def resolve_cjk_font() -> tuple[str | None, str]:
    """找一个带中文字形的字体，返回 `(文件路径, matplotlib 家族名)`。

    找不到就返回 `(None, "")`，调用方据此把图里的标签换成英文。
    """
    import glob

    candidates: list[str] = [p for p in _CJK_FONT_PATHS if Path(p).exists()]
    for pattern in _CJK_FONT_GLOBS:
        candidates.extend(sorted(glob.glob(pattern, recursive=True)))

    for path in dict.fromkeys(candidates):  # 去重且保序
        if not _has_cjk_glyphs(path):
            continue
        try:
            from matplotlib import font_manager

            font_manager.fontManager.addfont(path)
            family = font_manager.FontProperties(fname=path).get_name()
        except Exception as exc:
            log.debug("字体 %s 注册失败：%s", path, exc)
            continue
        log.info("班次报表使用中文字体：%s（%s）", path, family)
        return path, family
    log.warning("未找到带中文字形的字体，班次报表的图表标签将退化为英文")
    return None, ""


# ------------------------------------------------------------------ 曲线图渲染


_CHART_LOCK = threading.Lock()

#: 图表文案：有中文字体走中文，否则退化成英文（绝不出一堆方块）
_LABELS_ZH = {
    "title": "浓度趋势（断线点不画）",
    "x": "时间",
    "y": "浓度",
    "conc": "浓度",
    "flow": "瞬时流量",
    "flow_y": "流量（m3/h）",
    "flow_title": "流量趋势（FMC400）",
    "ore": "瞬时干矿量",
    "ore_y": "干矿量（t/h）",
    "ore_title": "综合干矿量趋势（逐点配对）",
    "avg": "区间均值",
    "high": "报警上限",
    "low": "报警下限",
}
_LABELS_EN = {
    "title": "Concentration trend (broken links omitted)",
    "x": "Time",
    "y": "Concentration",
    "conc": "Concentration",
    "flow": "Flow",
    "flow_y": "Flow (m3/h)",
    "flow_title": "Flow trend (FMC400)",
    "ore": "Dry ore",
    "ore_y": "Dry ore (t/h)",
    "ore_title": "Dry ore trend (paired points)",
    "avg": "Average",
    "high": "Alarm high",
    "low": "Alarm low",
}


def _render_line_png(
    *,
    times: Sequence[datetime],
    values: Sequence[float],
    title: str,
    series_label: str,
    y_label: str,
    x_label: str,
    color: str,
    average: float | None = None,
    average_label: str | None = None,
    guides: Sequence[tuple[float, str, str]] = (),
    has_cjk: bool = True,
) -> bytes | None:
    """任意「时间 → 数值」折线图的公共实现（浓度 / 流量 / 干矿量共用）。

    有效点少于 2 个时返回 `None`（无曲线可画）。设计上刻意与网页一致：参考线
    只画落在可视区间内的那几条 —— 报警线离数据很远时硬撑 Y 轴只会把曲线压成直线。
    """
    if len(times) < 2 or len(times) != len(values):
        return None

    import matplotlib

    # matplotlib 默认往 `~/.matplotlib` 写字体缓存。服务化部署（systemd、容器）里
    # HOME 常常不可写，它就会退化到临时目录、每次启动重建一遍缓存 —— 又慢又刷警告。
    # 显式挪到一个我们确定可写的地方（且必须在 import matplotlib 之前设置）。
    cache_dir = Path(tempfile.gettempdir()) / "ecas-matplotlib"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    except OSError as exc:
        log.debug("MPLCONFIGDIR 设置失败（不致命）：%s", exc)

    matplotlib.use("Agg")  # 无显示环境（服务器）也能出图
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    _, family = resolve_cjk_font() if has_cjk else (None, "")

    # matplotlib 画图态是全局的，多线程同时出图会串；串行化最省心
    with _CHART_LOCK, plt.rc_context(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [family, "DejaVu Sans"] if family else ["DejaVu Sans"],
            "axes.unicode_minus": False,  # 否则负号是方块
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    ):
        fig, ax = plt.subplots(figsize=(CHART_PX[0] / 130, CHART_PX[1] / 130), dpi=130)
        buf = BytesIO()
        try:
            # tz-aware → 本地墙上时间，x 轴刻度才不会漂
            xs = [t.replace(tzinfo=None) for t in times]
            ax.plot(xs, values, color=color, linewidth=1.6, label=series_label, zorder=3)
            baseline = min(values) - (max(values) - min(values) or 1.0) * 0.08
            ax.fill_between(xs, values, baseline, color=color, alpha=0.14, zorder=2)

            lo = min(values)
            hi = max(values)
            pad = (hi - lo or 1.0) * 0.12
            y0, y1 = lo - pad, hi + pad

            drawn = {series_label}
            for level, guide_color, guide_label in guides:
                if y0 <= level <= y1:
                    ax.axhline(level, color=guide_color, linestyle="--", linewidth=1.2, label=guide_label, zorder=4)
                    drawn.add(guide_label)
            if average is not None and average_label and y0 <= average <= y1:
                ax.axhline(average, color=COLOR_AVG, linestyle=":", linewidth=1.2, label=average_label, zorder=4)
                drawn.add(average_label)

            ax.set_ylim(y0, y1)
            ax.set_xlabel(x_label, fontsize=9, color="#475569")
            ax.set_ylabel(y_label, fontsize=9, color="#475569")
            ax.set_title(title, fontsize=11, color="#1e293b", pad=8)
            ax.grid(True, linestyle=":", linewidth=0.6, color="#cbd5e1", zorder=1)
            ax.tick_params(labelsize=8, colors="#475569")
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            if len(drawn) > 1:
                ax.legend(fontsize=8, loc="upper right", framealpha=0.9)

            fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        finally:
            plt.close(fig)

    return buf.getvalue()


def render_trend_png(
    *,
    times: Sequence[datetime],
    values: Sequence[float],
    alarm_low: float | None,
    alarm_high: float | None,
    average: float | None,
    title: str | None = None,
    has_cjk: bool = True,
) -> bytes | None:
    """把「时间 → 浓度」画成 PNG；有效点少于 2 个时返回 `None`（无曲线可画）。

    刻意与网页保持一致：**断线点不画**（调用方负责先剔除），参考线只画落在
    可视区间内的那几条。
    """
    labels = _LABELS_ZH if has_cjk else _LABELS_EN
    guides: list[tuple[float, str, str]] = []
    if alarm_high is not None:
        guides.append((alarm_high, COLOR_HIGH, labels["high"]))
    if alarm_low is not None:
        guides.append((alarm_low, COLOR_LOW, labels["low"]))
    return _render_line_png(
        times=times,
        values=values,
        title=title or labels["title"],
        series_label=labels["conc"],
        y_label=labels["y"],
        x_label=labels["x"],
        color=COLOR_LINE,
        average=average,
        average_label=labels["avg"],
        guides=tuple(guides),
        has_cjk=has_cjk,
    )


def render_flow_png(
    *,
    times: Sequence[datetime],
    values: Sequence[float],
    average: float | None = None,
    title: str | None = None,
    has_cjk: bool = True,
) -> bytes | None:
    """流量趋势图（瞬时流量 m³/h）；与浓度图共用渲染实现与配色规则。"""
    labels = _LABELS_ZH if has_cjk else _LABELS_EN
    return _render_line_png(
        times=times,
        values=values,
        title=title or labels["flow_title"],
        series_label=labels["flow"],
        y_label=labels["flow_y"],
        x_label=labels["x"],
        color=COLOR_FLOW,
        average=average,
        average_label=labels["avg"],
        has_cjk=has_cjk,
    )


def render_ore_png(
    *,
    times: Sequence[datetime],
    values: Sequence[float],
    average: float | None = None,
    title: str | None = None,
    has_cjk: bool = True,
) -> bytes | None:
    """综合干矿量趋势图（逐点配对的瞬时 Q，t/h），均值线就是「综合干矿量」。"""
    labels = _LABELS_ZH if has_cjk else _LABELS_EN
    return _render_line_png(
        times=times,
        values=values,
        title=title or labels["ore_title"],
        series_label=labels["ore"],
        y_label=labels["ore_y"],
        x_label=labels["x"],
        color=COLOR_ORE,
        average=average,
        average_label=labels["avg"],
        has_cjk=has_cjk,
    )


# ---------------------------------------------------------------------- 小工具


def _fmt_duration(seconds: float) -> str:
    """与 `AlarmPanel.tsx` 的 `fmtDuration` 逐档一致。"""
    s = max(0, int(round(seconds)))
    if s < 90:
        return f"{s} 秒"
    if s < 5400:
        return f"{s / 60:.1f} 分钟"
    if s < 172800:
        return f"{s / 3600:.1f} 小时"
    return f"{s / 86400:.1f} 天"


def _fmt_age(seconds: float | None) -> str:
    """与 `SummaryStrip.tsx` 的 `ageParts` 逐档一致。"""
    if seconds is None:
        return "—"
    s = max(0, int(round(seconds)))
    if s < 90:
        return f"{s} 秒"
    if s < 5400:
        return f"{s / 60:.1f} 分钟"
    if s < 172800:
        return f"{s / 3600:.1f} 小时"
    return f"{s / 86400:.1f} 天"


def _num(value: Any, digits: int = 3) -> str:
    """数值展示：`None` 给「—」（不要写成 nan 或 None，那是给机器看的）。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _trim(value: Any, digits: int = 3) -> str:
    """去尾零（对齐前端的 `trimNum`）：`+0.48` 不该写成 `+0.480`。"""
    return f"{round(float(value), digits):g}"


def _offset_text(settings: Any) -> str:
    """补偿口径的人话写法，与 `KpiCards.tsx` 的 `offsetText` 一致。"""
    if getattr(settings, "offset_mode", "add") == "mul":
        return f"×{1 + settings.offset_pct / 100:.4f}"
    return f"+{_trim(settings.offset_conc)}"


# ------------------------------------------------------------- xlsx 排版自适应
# 目标：单元格文本自动换行，列宽按内容自适应，行高按折行后的行数自适应。
#
# 两个必须自己算的理由（不能指望 Excel）：
#   1. **合并单元格不会被自动撑高** —— Excel/LibreOffice 都不会。本报表大量用合并
#      单元格放长句（口径公式、阈值说明），行高只能我们估。
#   2. **openpyxl 测不到真实字宽** —— 下面全部按字符类别估算（CJK 算两格），
#      宁可估宽一点：留白好过截断。

#: East Asian Width 里按两个字符宽计数的类别（Wide / Fullwidth）
_WIDE_EAST = ("W", "F")


def _display_width(text: Any) -> float:
    """文本占多少个「字符宽」：CJK 与全角算 2，组合符算 0，其余算 1。"""
    total = 0.0
    for ch in str(text):
        if unicodedata.combining(ch):
            continue
        total += 2.0 if unicodedata.east_asian_width(ch) in _WIDE_EAST else 1.0
    return total


def _wrapped_lines(text: Any, width: float) -> int:
    """这段文本在 `width` 字符宽的格子里大概会折成几行。"""
    if text is None or str(text) == "":
        return 1
    # 先扣掉单元格内边距，再乘 0.95 —— 西文是按词断行的，按字数除会低估行数
    usable = max(1.0, (width - 1.2) * 0.95)
    lines = 0
    for paragraph in str(text).split("\n"):
        lines += max(1, math.ceil(_display_width(paragraph) / usable))
    return lines


def _column_spans(sheet: Any) -> dict[tuple[int, int], int]:
    """跨列合并的锚点 → 跨了几列（跨行不影响宽度估算，故忽略）。"""
    spans: dict[tuple[int, int], int] = {}
    for rng in sheet.merged_cells.ranges:
        if rng.max_col > rng.min_col:
            spans[(rng.min_row, rng.min_col)] = rng.max_col - rng.min_col + 1
    return spans


def _measured_columns(sheet: Any) -> dict[int, float]:
    """当前每列宽度，键是 1-based 列号。"""
    from openpyxl.utils import column_index_from_string

    widths: dict[int, float] = {}
    for letter, dim in sheet.column_dimensions.items():
        if dim.width:
            widths[column_index_from_string(letter)] = float(dim.width)
    return widths


def _enable_wrap(sheet: Any) -> None:
    """整表打开「自动换行」，保留原有的水平/垂直对齐与缩进。"""
    from openpyxl.styles import Alignment

    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:      # 含合并区里的 MergedCell，它们没有值
                continue
            old = cell.alignment
            cell.alignment = Alignment(
                horizontal=old.horizontal,
                vertical=old.vertical or "center",
                wrap_text=True,
                indent=old.indent,
            )


def _fit_columns(
    sheet: Any,
    *,
    min_width: float = 9.0,
    max_width: float = 28.0,
) -> dict[int, float]:
    """按内容量列宽，返回列号 → 最终宽度。

    跨列合并的格子**跳过**：一段长句横跨 6 列时，拿它的文字长度去定单列的宽度会把
    那一列撑到荒唐的尺寸。这类内容交给「行高 + 换行」呈现更自然。
    """
    from openpyxl.utils import get_column_letter

    spans = _column_spans(sheet)
    want: dict[int, float] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None or (cell.row, cell.column) in spans:
                continue
            width = _display_width(cell.value)
            if width > want.get(cell.column, 0.0):
                want[cell.column] = width
    for column, width in want.items():
        # +2.4 余量：Excel 的列宽单位略窄于一个字符，且西文字形宽窄不一
        target = max(min_width, min(max_width, width + 2.4))
        sheet.column_dimensions[get_column_letter(column)].width = target
    return _measured_columns(sheet)


def _fit_rows(
    sheet: Any,
    widths: dict[int, float],
    *,
    line: float = 14.5,
    padding: float = 3.0,
    max_height: float = 260.0,
) -> None:
    """给需要折行的行估高度。**只增不减**，不会压掉调用方已设好的高度。

    只处理会折成两行以上的行：单行内容保持 Excel 的默认行高，视觉上更自然。
    """
    spans = _column_spans(sheet)
    needed: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            span = spans.get((cell.row, cell.column), 1)
            span_width = sum(widths.get(col, 10.0) for col in range(cell.column, cell.column + span))
            lines = _wrapped_lines(cell.value, span_width)
            if lines > needed.get(cell.row, 0):
                needed[cell.row] = lines
    for index, lines in needed.items():
        if lines <= 1:
            continue
        current = sheet.row_dimensions[index].height or 0.0
        target = min(max_height, lines * line + padding)
        if target > current:
            sheet.row_dimensions[index].height = target


# ----------------------------------------------------------------- xlsx 组装


@dataclass(frozen=True)
class Period:
    """报表覆盖的一段连续时间。

    「班次报表」与「自定义时段报表」共用同一套段结构与口径，只有称谓/标题/文件名
    不同：`noun` 进正文（班次 / 时段），`headline` 进标题，`open_label` 描述未结束状态。
    """

    start: datetime
    end: datetime
    label: str
    noun: str = "班次"
    headline: str = "ECAS 浓度计班次报表"
    open_label: str = "进行中"
    sheet_title: str = "班次报表"


def _build_period_workbook(
    *,
    settings: Any,
    period: Period,
    tz: ZoneInfo,
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    events: Iterable[dict[str, Any]],
    alarms_now: dict[str, Any],
    rows_total: int,
    generated_at: datetime,
    complete: bool = True,
    flow_rows: Sequence[dict[str, Any]] = (),
    flow_summary: dict[str, Any] | None = None,
    with_detail: bool = True,
    brief: bool = False,
) -> bytes:
    """生成一个时段（班次或自定义起止）的报表工作簿，返回 xlsx 字节。

    参数全部由调用方从库里取好（本模块不碰数据库），这样报表内容与看板用的是
    **同一批查询结果**，不存在"报表自己又算了一遍"的风险。

    * `period`：时段起止、称谓与标题（班次 → `Period(…, noun="班次")`）；
    * `rows`：该时段全部原始读数，`ts` 正序；
    * `summary`：`SQL_SUMMARY` 的行（区间统计）；
    * `events`：`alarms.events()` 的结果（报警事件表）；
    * `complete=False` 表示时段还没结束（终点在未来 / 仍在班中）；
    * `flow_rows` / `flow_summary`：电磁流量计（FMC400）同时段的逐行读数与区间统计。
      没有流量数据的时段（表未建 / 采集未覆盖）照样出报表，只是标「本时段无流量数据」。
    * `with_detail=False` 时不建任何明细工作表（读数明细 / 流量明细 / 干矿量明细），
      报表只剩给管理者看的部分（综合汇总、三段具体数据与趋势、区间统计、报警事件）。
    * `brief=True` 时主表改为「概述」：标题（不带型号）、时间/班次、四项核心指标与
      三张趋势图，用于汇报；区间统计与报警事件不再出现，但 `with_detail=True` 时
      仍会在「概述」后附读数 / 流量 / 干矿量三张明细表。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    start, end = period.start, period.end
    noun = period.noun
    label = period.label
    # 库里回来的是 timestamptz（psycopg 按连接时区还原成 aware datetime）。报表是给人看的，
    # 这里统一换成展示时区 —— 否则服务器跑在 UTC 时整份报表的时间会比看板早 8 小时。
    rows = [{"ts": row["ts"].astimezone(tz), "model": row["model"], "raw": row["raw"]} for row in rows]
    generated_at = generated_at.astimezone(tz)
    # 时段还没结束的话，「段末」就是导出时刻，不能拿未来的 end 去算滞后
    ref_end = end if complete else generated_at
    params = convert.params(settings, alarms_now["low"], alarms_now["high"])
    n_rows = len(rows)
    n_expected = int(summary.get("n") or 0)
    truncated = n_expected > n_rows
    first_ts = rows[0]["ts"] if rows else None
    last_ts = rows[-1]["ts"] if rows else None

    # —— 样式 ——
    title_font = Font(size=15, bold=True, color="1E3A8A")
    section_font = Font(size=11, bold=True, color="0F172A")
    section_fill = PatternFill("solid", fgColor="E2E8F0")
    label_font = Font(size=10, bold=True, color="334155")
    value_font = Font(size=10, color="0F172A")
    head_font = Font(size=10, bold=True, color="0F172A")
    head_fill = PatternFill("solid", fgColor="F1F5F9")
    warn_font = Font(size=10, bold=True, color="B91C1C")
    thin = Side(style="thin", color="CBD5E1")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    merged_value = Alignment(vertical="center", horizontal="left")

    wb = Workbook()

    # ================================================== Sheet 1：主报表
    ws = wb.active
    ws.title = period.sheet_title
    for col, width in zip("ABCDEFGHIJKL", (18, 20, 12, 14, 16, 22, 12, 12, 12, 12, 12, 12)):
        ws.column_dimensions[col].width = width
    last_col = 12  # A..L

    def section(r: int, text: str) -> int:
        cell = ws.cell(row=r, column=1, value=text)
        cell.font = section_font
        cell.fill = section_fill
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last_col)
        ws.row_dimensions[r].height = 20
        return r + 1

    def pair(r: int, label: str, value: Any, label2: str | None = None, value2: Any = None) -> int:
        """A/B 与 E/G 两对「标签: 值」同行，值横向合并方便读长句。"""
        ws.cell(row=r, column=1, value=label).font = label_font
        cell = ws.cell(row=r, column=2, value=value)
        cell.font = value_font
        cell.alignment = merged_value
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        if label2 is not None:
            ws.cell(row=r, column=5, value=label2).font = label_font
            cell2 = ws.cell(row=r, column=6, value=value2)
            cell2.font = value_font
            cell2.alignment = merged_value
            ws.merge_cells(start_row=r, start_column=6, end_row=r, end_column=last_col)
        return r + 1

    def add_chart(png: bytes | None, r: int, *, reason: str = "", failed: bool = False) -> int:
        """把 PNG 贴到 A{r}；没有图就写一行原因 —— 出图失败不能让整份报表生不出来。"""
        if png:
            from openpyxl.drawing.image import Image as XLImage

            img = XLImage(BytesIO(png))
            img.width, img.height = CHART_PX
            ws.add_image(img, f"A{r}")
            # 图片浮在单元格之上，不会撑开行高；主动空出足够行数，免得盖住后面的内容
            for _ in range(CHART_PX[1] // 20 + 1):
                ws.row_dimensions[r].height = 20
                r += 1
            return r
        cell = ws.cell(row=r, column=1, value=reason or "有效点不足 2 个，无法成图")
        cell.font = warn_font if failed else value_font
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last_col)
        return r + 1

    # —— 标题 ——
    ws.cell(row=1, column=1, value=f"{period.headline} ｜ {settings.model_name} ｜ {label}").font = title_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    ws.row_dimensions[1].height = 26
    r = 2

    # —— 元信息（口径、阈值、区间；报表自带说明，脱离网页也读得懂）——
    r = pair(r, "型号", settings.model_name, noun, f"{label}（{period.open_label if not complete else '已结束'}）")
    r = pair(
        r,
        f"{noun}起止",
        f"{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}",
        "生成时间",
        generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )
    r = pair(
        r,
        "数据行数",
        f"导出 {n_rows} 行（区间内共 {n_expected} 行）" + ("　⚠ 已达上限被截断" if truncated else ""),
        "数据覆盖",
        f"{first_ts:%Y-%m-%d %H:%M:%S} ~ {last_ts:%Y-%m-%d %H:%M:%S}" if first_ts else f"本{noun}无数据",
    )
    r = pair(r, "量程", f"{settings.i_lo}~{settings.i_hi} mA ↔ {settings.c_lo}~{settings.c_hi} 浓度", "断流阈值", f"{settings.stale_seconds} 秒")
    r = pair(r, "口径(电流)", f"ma = {params['formula']['ma']}", "口径(未补偿)", f"conc_raw = {params['formula']['conc_raw']}")
    r = pair(
        r,
        "口径(补偿)",
        f"conc = {params['formula']['conc']}",
        "补偿参数",
        f"{_offset_text(settings)}（offset_mode={settings.offset_mode}、offset_conc={settings.offset_conc}、offset_pct={settings.offset_pct}）",
    )
    r = pair(
        r,
        "浓度报警",
        f"下限 {alarms_now['low']}（raw < {alarms_now['low_raw']}）｜上限 {alarms_now['high']}（raw > {alarms_now['high_raw']}）"
        + ("（自定义值）" if alarms_now["custom"] else "（出厂默认值）"),
    )
    r = pair(
        r,
        "异常阈值",
        f"断线 raw < {settings.break_low} ｜ 偏低 {settings.break_low}~{int(settings.i_lo * 1000)} ｜ "
        f"正常 {int(settings.i_lo * 1000)}~{settings.over_high} ｜ 超量程 raw > {settings.over_high}",
    )
    r = pair(r, "注意", "断线/超量程行的浓度是数学外推值，无物理意义 —— 请按状态列判断")
    if truncated:
        cell = ws.cell(row=r, column=1, value=f"⚠ 本{noun}共 {n_expected} 行，超过单次导出行数上限，仅导出前 {n_rows} 行")
        cell.font = warn_font
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last_col)
        r += 1
    r += 1

    # —— 先把统计量算好：综合汇总与三个「具体」段共用同一批数字 ——
    n_valid = int(summary.get("n_valid") or 0)
    dropped = max(0, n_expected - n_valid)
    valid = [r_ for r_ in rows if convert.raw_to_status(int(r_["raw"]), settings) != "断线"]
    change = None
    if len(valid) >= 2:
        from_c = convert.raw_to_conc(int(valid[0]["raw"]), settings)
        to_c = convert.raw_to_conc(int(valid[-1]["raw"]), settings)
        change = (from_c, to_c, to_c - from_c)

    conc_avg = convert.raw_to_conc(summary["raw_avg"], settings) if summary.get("raw_avg") is not None else None
    conc_min = convert.raw_to_conc(summary["raw_min"], settings) if summary.get("raw_min") is not None else None
    conc_max = convert.raw_to_conc(summary["raw_max"], settings) if summary.get("raw_max") is not None else None

    # 流量：逐行解码一次，统计与趋势图共用
    flow_decoded: list[tuple[datetime, dict[str, Any]]] = []
    f_n = 0
    f_avg = f_min = f_max = v_avg = v_max = p_avg = None
    n_empty = n_rev = n_alarm = 0
    total_first = total_last = None
    flow_delta = None
    flow_t0 = flow_t1 = None
    if flow_rows:
        flow_decoded = [
            (row["ts"].astimezone(tz), flow.decode([int(v) for v in row["regs"]]))
            for row in flow_rows
        ]
    if flow_decoded:
        fs = flow_summary or {}
        flows = [d["flow_m3h"] for _, d in flow_decoded]
        vels = [d["velocity_mps"] for _, d in flow_decoded]
        pcts = [d["percent"] for _, d in flow_decoded]
        f_n = int(fs.get("n") or len(flow_decoded))
        f_avg = float(fs["flow_avg"]) if fs.get("flow_avg") is not None else sum(flows) / len(flows)
        f_min = float(fs["flow_min"]) if fs.get("flow_min") is not None else min(flows)
        f_max = float(fs["flow_max"]) if fs.get("flow_max") is not None else max(flows)
        v_avg = float(fs["velocity_avg"]) if fs.get("velocity_avg") is not None else sum(vels) / len(vels)
        v_max = float(fs["velocity_max"]) if fs.get("velocity_max") is not None else max(vels)
        p_avg = float(fs["percent_avg"]) if fs.get("percent_avg") is not None else sum(pcts) / len(pcts)
        n_empty = int(fs.get("n_empty_pipe") or sum(1 for _, d in flow_decoded if d["status"] == flow.EMPTY_PIPE))
        n_rev = int(fs.get("n_reverse") or sum(1 for _, d in flow_decoded if d["status"] == flow.REVERSE))
        n_alarm = int(fs.get("n_alarm") or sum(1 for _, d in flow_decoded if d["status"] == flow.ALARM))
        total_first = int(fs["total_first"]) if fs.get("total_first") is not None else int(flow_decoded[0][1]["total_m3"])
        total_last = int(fs["total_last"]) if fs.get("total_last") is not None else int(flow_decoded[-1][1]["total_m3"])
        flow_delta = total_last - total_first if total_last >= total_first else None
        flow_t0, flow_t1 = flow_decoded[0][0], flow_decoded[-1][0]

    # 干矿量：与看板同源抽样（width_bucket 居中行）再逐点配对
    sampled_rows = ore.sample_centered([(row["ts"], row) for row in rows], start=start, end=ref_end)
    ore_readings = ore.from_rows([row for _, row in sampled_rows], convert, settings)
    sampled_flows = ore.sample_centered(flow_decoded, start=start, end=ref_end)
    ore_flows = [(ts, float(d["flow_m3h"]), float(d["velocity_mps"])) for ts, d in sampled_flows]
    ore_hours = max(0.0, (ref_end - start).total_seconds() / 3600.0)
    ore_summary = ore.summarize(ore.pair_points(ore_readings, ore_flows), hours=ore_hours)

    # 三张趋势图先渲染好，正文只负责按顺序贴图
    chart_times = [r_["ts"] for r_ in rows if convert.raw_to_status(int(r_["raw"]), settings) != "断线"]
    chart_values = [convert.raw_to_conc(int(r_["raw"]), settings) for r_ in rows if convert.raw_to_status(int(r_["raw"]), settings) != "断线"]
    conc_png = None
    conc_chart_error = ""
    try:
        conc_png = render_trend_png(
            times=chart_times,
            values=chart_values,
            alarm_low=alarms_now["low"],
            alarm_high=alarms_now["high"],
            average=conc_avg,
            title=f"{label}　浓度趋势（断线点不画，共 {len(chart_values)} 个有效点）",
        )
    except Exception as exc:  # 出图失败不能让整份报表生不出来
        conc_chart_error = str(exc)
        log.warning("%s %s 浓度趋势图渲染失败：%s", noun, label, exc)

    ore_png = None
    ore_chart_error = ""
    try:
        ore_png = render_ore_png(
            times=[p.ts for p in ore_summary.points],
            values=[p.q_tph for p in ore_summary.points],
            average=ore_summary.avg_tph,
            title=f"{label}　综合干矿量趋势（逐点配对，共 {ore_summary.n} 个点）",
        )
    except Exception as exc:
        ore_chart_error = str(exc)
        log.warning("%s %s 干矿量趋势图渲染失败：%s", noun, label, exc)

    flow_chart_points: list[tuple[datetime, dict[str, Any]]] = []
    flow_chart_png = None
    flow_chart_error = ""
    if flow_decoded:
        try:
            flow_chart_points = ore.sample_centered(flow_decoded, start=start, end=ref_end)
            flow_chart_png = render_flow_png(
                times=[ts for ts, _ in flow_chart_points],
                values=[d["flow_m3h"] for _, d in flow_chart_points],
                average=f_avg,
                title=f"{label}　流量趋势（FMC400，共 {len(flow_chart_points)} 个测点）",
            )
        except Exception as exc:
            flow_chart_error = str(exc)
            log.warning("%s %s 流量趋势图渲染失败：%s", noun, label, exc)

    # —— 概述版：只给领导看的四项指标 + 三张趋势图，一页 A4 宽度，数据精炼 ——
    if brief:
        # 概述版不用完整报表的标题/元信息：先清掉已写好的行与合并区，再从空白页排
        for merged in list(ws.merged_cells.ranges):
            ws.unmerge_cells(str(merged))
        ws.delete_rows(1, ws.max_row)
        last_col = 8  # A..H
        ws.title = "概述"
        ws.sheet_view.showGridLines = False
        for col in "ABCDEFGH":
            ws.column_dimensions[col].width = 18.0

        ws.cell(row=1, column=1, value=f"ECAS {noun}综合报表").font = Font(size=20, bold=True, color="1E3A8A")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        ws.row_dimensions[1].height = 38

        info = (
            (
                "时间",
                f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}",
                "班次" if noun == "班次" else "类型",
                label if noun == "班次" else "自定义时段",
            ),
            (
                "生成时间",
                generated_at.strftime("%Y-%m-%d %H:%M"),
                "状态",
                "已结束" if complete else ("未结束（统计到导出时刻）" if noun == "时段" else "进行中"),
            ),
        )
        rr = 3
        for left_label, left_value, right_label, right_value in info:
            for label_cell, label_text in ((1, left_label), (5, right_label)):
                cell = ws.cell(row=rr, column=label_cell, value=label_text)
                cell.font = label_font
                cell.alignment = center
            for col_start, col_end, text in ((2, 4, left_value), (6, 8, right_value)):
                cell = ws.cell(row=rr, column=col_start, value=text)
                cell.font = value_font
                cell.alignment = merged_value
                ws.merge_cells(start_row=rr, start_column=col_start, end_row=rr, end_column=col_end)
            rr += 1

        def card(row: int, col_start: int, col_end: int, title: str, value: str, *, big: bool = True) -> None:
            for r_ in (row, row + 1):
                for c_ in range(col_start, col_end + 1):
                    ws.cell(row=r_, column=c_).border = box
            label_cell = ws.cell(row=row, column=col_start, value=title)
            label_cell.font = Font(size=10, bold=True, color="475569")
            label_cell.fill = PatternFill("solid", fgColor="F1F5F9")
            label_cell.alignment = center
            ws.merge_cells(start_row=row, start_column=col_start, end_row=row, end_column=col_end)
            ws.row_dimensions[row].height = 22
            value_cell = ws.cell(row=row + 1, column=col_start, value=value)
            value_cell.font = Font(size=20 if big else 16, bold=True, color="0F172A")
            value_cell.alignment = center
            ws.merge_cells(start_row=row + 1, start_column=col_start, end_row=row + 1, end_column=col_end)
            ws.row_dimensions[row + 1].height = 34

        ore_value = f"{ore_summary.avg_tph:.2f} t/h" if ore_summary.avg_tph is not None else "—"
        total_value = f"≈ {ore_summary.total_t:.1f} t" if ore_summary.total_t is not None else "—"
        conc_value = f"{conc_avg:.3f} %" if conc_avg is not None else "—"
        flow_value = f"{f_avg:.2f} m³/h" if f_avg is not None else "—"
        cumulative_label = "本班次累计" if noun == "班次" else "本时段累计"

        card(5, 1, 4, "综合干矿量", ore_value)
        card(5, 5, 8, cumulative_label, total_value)
        card(8, 1, 4, "平均浓度", conc_value, big=False)
        card(8, 5, 8, "平均流量", flow_value, big=False)

        r = 11
        r = section(r, "干矿量趋势")
        r = add_chart(
            ore_png,
            r,
            reason=f"有效配对仅 {ore_summary.n} 个，无法成图",
            failed=bool(ore_chart_error),
        )
        r += 1
        r = section(r, "浓度趋势（断线点不画）")
        r = add_chart(
            conc_png,
            r,
            reason=f"有效点仅 {len(chart_values)} 个，无法成图",
            failed=bool(conc_chart_error),
        )
        r += 1
        r = section(r, "流量趋势（FMC400）")
        r = add_chart(
            flow_chart_png,
            r,
            reason=(
                f"流量测点仅 {len(flow_chart_points)} 个，无法成图"
                if flow_decoded
                else f"本{noun}无流量数据，无法成图"
            ),
            failed=bool(flow_chart_error),
        )
        r += 1

        note_text = (
            f"生成时间 {generated_at:%Y-%m-%d %H:%M} ｜ 累计 = 平均干矿量 × {noun}时长（外推值）；"
            "浓度只取正常点，断线/异常点不参与配对"
        )
        if truncated:
            note_text = f"⚠ 数据量超过单次导出上限（{n_expected} 行中导出 {n_rows} 行）｜ " + note_text
        note = ws.cell(row=r, column=1, value=note_text)
        note.font = Font(size=9, color="64748B")
        note.alignment = Alignment(vertical="center", wrap_text=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last_col)
        ws.row_dimensions[r].height = 24

        _append_detail_sheets(
            wb,
            rows=rows,
            flow_decoded=flow_decoded,
            ore_summary=ore_summary,
            settings=settings,
            alarms_now=alarms_now,
            n_rows=n_rows,
            n_expected=n_expected,
            truncated=truncated,
            with_detail=with_detail,
        )

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # —— 综合汇总（干矿量 + 浓度 + 流量，管理者先看这三个数）——
    r = section(r, "综合汇总")
    if ore_summary.n >= 2 and ore_summary.avg_tph is not None and ore_summary.total_t is not None:
        r = pair(
            r,
            "综合干矿量",
            f"{ore_summary.avg_tph:.2f} t/h",
            f"本{noun}累计",
            f"≈ {ore_summary.total_t:.1f} t（{ore_hours:.1f} h × 均值）",
        )
    else:
        r = pair(r, "综合干矿量", "—（有效配对不足）", f"本{noun}累计", "—")
    r = pair(
        r,
        "平均浓度",
        f"{conc_avg:.3f} %" if conc_avg is not None else "—",
        "平均流量",
        f"{f_avg:.2f} m³/h" if f_avg is not None else "—",
    )
    r = pair(
        r,
        "有效样本",
        f"{n_valid} 行",
        "数据覆盖",
        f"{first_ts:%Y-%m-%d %H:%M:%S} ~ {last_ts:%Y-%m-%d %H:%M:%S}" if first_ts and last_ts else f"本{noun}无数据",
    )
    r += 1

    # —— 具体干矿量 + 趋势 ——
    r = section(r, "具体干矿量（浓度 × 流量逐点配对）")
    if ore_summary.n >= 2:
        assert ore_summary.avg_tph is not None and ore_summary.total_t is not None
        r = pair(r, "配对数", f"{ore_summary.n} 点", "配对平均流量", f"{ore_summary.avg_flow_m3h:.2f} m³/h")
        r = pair(
            r,
            "配对平均浓度",
            f"{ore_summary.avg_conc:.3f} %",
            "有效配对区间",
            f"{ore_summary.first_ts:%Y-%m-%d %H:%M:%S} ~ {ore_summary.last_ts:%Y-%m-%d %H:%M:%S}",
        )
        r = pair(
            r,
            "口径(干矿量)",
            "Q = V / (1/ρp + (1−C)/C)，ρp = 4.3 t/m³（V 为 m³，C 为质量浓度 %）",
            "延时补偿",
            "Δt = 2.5 m / v：浓度计在上游、流量计在下游，按实测流速把流量点时间轴后错",
        )
        r = pair(
            r,
            "说明",
            f"浓度只取「正常」点；配对容差 ±90 秒；累计 = 平均干矿量 × {noun}时长（不用梯形积分）；"
            f"抽样与看板一致（≤ {ore.SAMPLE_LIMIT} 点）",
        )
    else:
        r = pair(
            r,
            f"本{noun}无法计算综合干矿量",
            f"有效配对仅 {ore_summary.n} 点（需要浓度与流量在同一时刻 ±90 秒内同时有正常数据）",
        )
    r = add_chart(ore_png, r, reason=f"有效配对仅 {ore_summary.n} 个，无法成图", failed=bool(ore_chart_error))
    r += 1

    # —— 具体浓度 + 趋势 ——
    r = section(r, "具体浓度（断线点不画）")
    r = pair(
        r,
        "平均浓度",
        f"{conc_avg:.3f} %" if conc_avg is not None else "—",
        "有效浓度点",
        f"{len(chart_values)} 点",
    )
    r = pair(
        r,
        "最低",
        f"{conc_min:.3f} %" if conc_min is not None else "—",
        "最高",
        f"{conc_max:.3f} %" if conc_max is not None else "—",
    )
    if change:
        r = pair(r, "区间变化", f"{change[2]:+.3f} %", "起止", f"{change[0]:.3f} → {change[1]:.3f}")
    else:
        r = pair(r, "区间变化", "—", "起止", "有效点不足 2 个")
    r = pair(
        r,
        "说明",
        f"仅统计有效测量点（已排除 {dropped} 行断线/偏低/超量程）" if dropped else "仅统计有效测量点",
    )
    r = add_chart(conc_png, r, reason=f"有效点仅 {len(chart_values)} 个，无法成图", failed=bool(conc_chart_error))
    r += 1

    # —— 具体流量 + 趋势 ——
    r = section(r, "具体流量（电磁流量计 FMC400）")
    if flow_decoded:
        assert f_avg is not None and f_min is not None and f_max is not None
        assert v_avg is not None and v_max is not None and p_avg is not None
        assert flow_t0 is not None and flow_t1 is not None
        assert total_first is not None and total_last is not None
        r = pair(
            r,
            "流量测点",
            f"{f_n} 行（{flow_t0:%Y-%m-%d %H:%M:%S} ~ {flow_t1:%Y-%m-%d %H:%M:%S}）",
            "满量程均值",
            f"{p_avg:.1f}%",
        )
        r = pair(
            r,
            "瞬时流量",
            f"均值 {f_avg:.2f} ｜ 最低 {f_min:.2f} ｜ 最高 {f_max:.2f} m³/h",
            "流速",
            f"均值 {v_avg:.3f} ｜ 最大 {v_max:.3f} m/s",
        )
        r = pair(
            r,
            "正向累积",
            f"{total_first} → {total_last} m³"
            + (f"（本{noun} +{flow_delta} m³）" if flow_delta is not None else "（回绕/清零，不给增量）"),
            "空管/反流/报警",
            f"{n_empty} / {n_rev} / {n_alarm} 次",
        )
        r = pair(
            r,
            "口径(流量)",
            "velocity = s32(r0,r1)/1000 ｜ flow = s32(r2,r3)/100 ｜ total = u32(r6,r7) m³",
            "采集",
            "站号 8 ｜ FC03 读 0~13（14 个寄存器）",
        )
    else:
        r = pair(
            r,
            f"本{noun}无流量数据",
            f"flow_reading 在该{noun}区间没有行（采集链尚未覆盖本时段），浓度计部分不受影响",
        )
    r = add_chart(
        flow_chart_png,
        r,
        reason=f"流量测点仅 {len(flow_chart_points)} 个，无法成图" if flow_decoded else f"本{noun}无流量数据，无法成图",
        failed=bool(flow_chart_error),
    )
    r += 1

    # —— 区间统计（数据量与异常计数；浓度明细见「具体浓度」）——
    r = section(r, "区间统计")
    for i, text in enumerate(("分组", "指标", "值", "补充说明"), start=1):
        cell = ws.cell(row=r, column=i, value=text)
        cell.font, cell.fill, cell.alignment, cell.border = head_font, head_fill, center, box
    r += 1

    stats: list[tuple[str, str, Any, str, str | None]] = [
        ("数据量", "有效样本", n_valid, "行", f"已排除 {dropped} 行（断线/偏低/超量程）" if dropped else None),
        ("数据量", "库存总量", rows_total, "行", "全表累计行数"),
        ("数据量", "原始最低", summary.get("raw_min") if summary.get("raw_min") is not None else "—", "", None),
        ("数据量", "原始最高", summary.get("raw_max") if summary.get("raw_max") is not None else "—", "", None),
        (
            "数据量",
            "数据滞后",
            _fmt_age((ref_end - last_ts).total_seconds()) if last_ts else None,
            "",
            (
                f"{noun}结束于 {end:%H:%M:%S}，最后一条数据 {last_ts:%H:%M:%S}"
                if complete
                else f"导出于 {generated_at:%H:%M:%S}，最后一条数据 {last_ts:%H:%M:%S}"
            )
            if last_ts
            else None,
        ),
        ("越限与异常", "断线", int(summary.get("broken") or 0), "行", f"raw < {settings.break_low}"),
        ("越限与异常", "偏低", int(summary.get("low") or 0), "行", "raw 3600~4000"),
        ("越限与异常", "超量程", int(summary.get("over") or 0), "行", f"raw > {settings.over_high}"),
        ("越限与异常", "低于下限", int(summary.get("n_below_alarm") or 0), "行", f"浓度 < {alarms_now['low']}"),
        ("越限与异常", "高于上限", int(summary.get("n_above_alarm") or 0), "行", f"浓度 > {alarms_now['high']}"),
    ]
    for group, metric, value, unit, note in stats:
        ws.cell(row=r, column=1, value=group).font = label_font
        ws.cell(row=r, column=2, value=metric).font = value_font
        cell = ws.cell(row=r, column=3, value=value)
        cell.font = value_font
        cell.alignment = center
        ws.cell(row=r, column=4, value=(note or "") if unit == "" else (f"{unit}　{note}" if note else unit)).font = value_font
        for c in range(1, 5):
            ws.cell(row=r, column=c).border = box
        r += 1
    r += 1

    # —— 报警事件（对应网页报警事件表）——
    event_rows = list(events)
    r = section(r, f"报警事件（{len(event_rows)} 条，含进行中）")
    ev_head = ("开始时间", "种类", "级别", "持续", "峰值 raw", "峰值浓度", "行数", "状态")
    for i, text in enumerate(ev_head, start=1):
        cell = ws.cell(row=r, column=i, value=text)
        cell.font, cell.fill, cell.alignment, cell.border = head_font, head_fill, center, box
    r += 1
    if not event_rows:
        ws.cell(row=r, column=1, value=f"本{noun}没有报警事件").font = value_font
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(ev_head))
        r += 1
    for event in event_rows:
        kind = event["kind_label"] + (f"（{event['note']}）" if event.get("note") else "")
        started = event["started_ts"].astimezone(tz)
        ended = event["ended_ts"].astimezone(tz) if event.get("ended_ts") else None
        values = [
            f"{started:%Y-%m-%d %H:%M:%S}",
            kind,
            event["level"],
            _fmt_duration(event["duration_seconds"]),
            event.get("peak_raw") if event.get("peak_raw") is not None else "—",
            _num(event.get("peak_conc"), 3),
            int(event.get("n_rows") or 0),
            "进行中" if event.get("active") else (f"已结束 {ended:%Y-%m-%d %H:%M:%S}" if ended else "已结束"),
        ]
        for i, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=i, value=value)
            cell.border = box
            cell.font = value_font
            if i in (5, 6, 7):
                cell.alignment = center
        r += 1

    _append_detail_sheets(
        wb,
        rows=rows,
        flow_decoded=flow_decoded,
        ore_summary=ore_summary,
        settings=settings,
        alarms_now=alarms_now,
        n_rows=n_rows,
        n_expected=n_expected,
        truncated=truncated,
        with_detail=with_detail,
    )

    # —— 排版自适应：自动换行 + 列宽按内容 + 行高按折行数 ——
    # 必须等两个 sheet 的内容、合并区、图片锚点全部定好再做：列宽一变，折行数就变。
    _enable_wrap(ws)
    _fit_rows(ws, _fit_columns(ws, max_width=28.0))

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _append_detail_sheets(
    wb: Any,
    *,
    rows: Sequence[dict[str, Any]],
    flow_decoded: Sequence[tuple[datetime, dict[str, Any]]],
    ore_summary: "ore.OreSummary",
    settings: Any,
    alarms_now: dict[str, Any],
    n_rows: int,
    n_expected: int,
    truncated: bool,
    with_detail: bool,
) -> None:
    """按需追加三张明细表（读数 / 流量 / 干矿量）；完整报表与概述版共用。"""
    if not with_detail:
        return
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    head_font = Font(size=10, bold=True, color="0F172A")
    head_fill = PatternFill("solid", fgColor="F1F5F9")
    warn_font = Font(size=10, bold=True, color="B91C1C")
    thin = Side(style="thin", color="CBD5E1")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")

    detail = wb.create_sheet("读数明细")
    detail_head = ("序号", "时间", "型号", "原始值", "电流(mA)", "未补偿浓度", "浓度", "状态", "浓度越限")
    for i, text in enumerate(detail_head, start=1):
        cell = detail.cell(row=1, column=i, value=text)
        cell.font, cell.fill, cell.alignment, cell.border = head_font, head_fill, center, box
    detail.column_dimensions["A"].width = 8
    detail.column_dimensions["B"].width = 21
    detail.column_dimensions["C"].width = 12
    for col in ("D", "E", "F", "G"):
        detail.column_dimensions[col].width = 14
    detail.column_dimensions["H"].width = 10
    detail.column_dimensions["I"].width = 14
    detail.freeze_panes = "A2"

    for idx, row in enumerate(rows, start=1):
        reading = convert.convert(int(row["raw"]), settings)
        side = ""
        if reading["status"] == "正常":
            if reading["conc"] > alarms_now["high"]:
                side = "↑越限"
            elif reading["conc"] < alarms_now["low"]:
                side = "↓越限"
        values = [
            idx,
            f"{row['ts']:%Y-%m-%d %H:%M:%S}",
            str(row["model"]),
            int(row["raw"]),
            round(reading["ma"], 3),
            round(reading["conc_raw"], 3),
            round(reading["conc"], 3),
            reading["status"],
            side,
        ]
        for i, value in enumerate(values, start=1):
            cell = detail.cell(row=idx + 1, column=i, value=value)
            cell.border = box
            if i in (5, 6, 7):
                cell.number_format = "0.000"
            if i in (1, 4, 8, 9):
                cell.alignment = center
        if side:
            detail.cell(row=idx + 1, column=9).font = Font(size=10, bold=True, color="B91C1C")

    if n_rows:
        detail.auto_filter.ref = f"A1:I{n_rows + 1}"
    if truncated:
        cell = detail.cell(row=n_rows + 3, column=1, value=f"⚠ 区间内共 {n_expected} 行，因单次导出行数上限只导出了前 {n_rows} 行")
        cell.font = warn_font
        # 横向合并，免得这行长提示把「序号」列撑宽（合并区会跳过列宽测量，改由行高承载）
        detail.merge_cells(start_row=n_rows + 3, start_column=1, end_row=n_rows + 3, end_column=len(detail_head))
    # ================================================== Sheet 3：流量明细（选了明细且有数据才建）
    fdetail = None
    if flow_decoded:
        fdetail = wb.create_sheet("流量明细")
        fhead = (
            "序号",
            "时间",
            "瞬时流量(m³/h)",
            "流速(m/s)",
            "满量程(%)",
            "空管(%)",
            "正向累积(m³)",
            "流向",
            "状态",
            "报警",
        )
        for i, text in enumerate(fhead, start=1):
            cell = fdetail.cell(row=1, column=i, value=text)
            cell.font, cell.fill, cell.alignment, cell.border = head_font, head_fill, center, box
        for col, width in zip("ABCDEFGHIJ", (8, 21, 16, 12, 12, 10, 16, 10, 10, 24)):
            fdetail.column_dimensions[col].width = width
        fdetail.freeze_panes = "A2"
        for idx, (ts, d) in enumerate(flow_decoded, start=1):
            values = [
                idx,
                f"{ts:%Y-%m-%d %H:%M:%S}",
                round(d["flow_m3h"], 2),
                round(d["velocity_mps"], 3),
                round(d["percent"], 2),
                round(d["empty_pipe_pct"], 2),
                int(d["total_m3"]),
                d["direction_label"],
                d["status"],
                "、".join(d["alarm_labels"]),
            ]
            for i, value in enumerate(values, start=1):
                cell = fdetail.cell(row=idx + 1, column=i, value=value)
                cell.border = box
                if i in (3, 4, 5, 6):
                    cell.number_format = "0.00"
                if i in (1, 8, 9):
                    cell.alignment = center
            if d["status"] != flow.NORMAL:
                fdetail.cell(row=idx + 1, column=9).font = Font(size=10, bold=True, color="B91C1C")
        fdetail.auto_filter.ref = f"A1:J{len(flow_decoded) + 1}"


    # ================================================== Sheet 4：干矿量明细（选了明细且有配对才建）
    odetail = None
    if ore_summary.n >= 2:
        odetail = wb.create_sheet("干矿量明细")
        ohead = (
            "序号",
            "浓度时刻",
            "浓度(%)",
            "配对流量时刻",
            "瞬时流量(m³/h)",
            "流速(m/s)",
            "传输延时(s)",
            "瞬时干矿量(t/h)",
        )
        for i, text in enumerate(ohead, start=1):
            cell = odetail.cell(row=1, column=i, value=text)
            cell.font, cell.fill, cell.alignment, cell.border = head_font, head_fill, center, box
        for col, width in zip("ABCDEFGH", (8, 21, 12, 21, 16, 12, 12, 16)):
            odetail.column_dimensions[col].width = width
        odetail.freeze_panes = "A2"
        for idx, point in enumerate(ore_summary.points, start=1):
            values = [
                idx,
                f"{point.ts:%Y-%m-%d %H:%M:%S}",
                round(point.conc, 3),
                f"{point.flow_ts:%Y-%m-%d %H:%M:%S}",
                round(point.flow_m3h, 2),
                round(point.velocity_mps, 3),
                round(point.delay_s, 3),
                round(point.q_tph, 3),
            ]
            for i, value in enumerate(values, start=1):
                cell = odetail.cell(row=idx + 1, column=i, value=value)
                cell.border = box
                if i in (3, 5, 6, 7, 8):
                    cell.number_format = "0.000"
                if i in (1, 2, 4):
                    cell.alignment = center
        odetail.auto_filter.ref = f"A1:H{ore_summary.n + 1}"

    _enable_wrap(detail)
    _fit_rows(detail, _fit_columns(detail, min_width=8.0, max_width=30.0))
    if fdetail is not None:
        _enable_wrap(fdetail)
        _fit_rows(fdetail, _fit_columns(fdetail, min_width=8.0, max_width=30.0))
    if odetail is not None:
        _enable_wrap(odetail)
        _fit_rows(odetail, _fit_columns(odetail, min_width=8.0, max_width=30.0))


def _ascii_slug(name: str) -> str:
    """型号名 → 纯 ASCII 的文件名片段（`Content-Disposition` 按协议只能放 ASCII）。

    ⚠️ 别用 `str.isalnum()`：它对中文也返回 True（`"浓".isalnum() is True`），
    中文型号名会原样进 HTTP 头 —— 轻则乱码，重则响应头构造失败。
    口径与 `main.py` 里 CSV 导出的 `re.sub(r"[^A-Za-z0-9._-]", "_", …)` 保持一致。

    做坏了的样本：`"浓度计 DPRO-66"` → `"____DPRO-66"`（中文与空格都折成 `_`）。
    """
    slug = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_" for ch in name)
    return slug or "ecas"


def shift_filename(settings: Any, shift: Shift) -> str:
    """单班报表文件名（纯 ASCII，避免 Content-Disposition 编码坑）。"""
    return f"ecas_{_ascii_slug(settings.model_name)}_{shift.slug}.xlsx"


def bundle_filename(settings: Any, shifts: Sequence[Shift]) -> str:
    """多班打包的 zip 文件名：`ecas_DPRO-66_shifts_20260925-20260926.zip`。"""
    first, last = shifts[0], shifts[-1]
    return f"ecas_{_ascii_slug(settings.model_name)}_shifts_{first.day:%Y%m%d}-{last.day:%Y%m%d}.zip"


def build_shift_workbook(
    *,
    settings: Any,
    shift: Shift,
    tz: ZoneInfo,
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    events: Iterable[dict[str, Any]],
    alarms_now: dict[str, Any],
    rows_total: int,
    generated_at: datetime,
    complete: bool = True,
    flow_rows: Sequence[dict[str, Any]] = (),
    flow_summary: dict[str, Any] | None = None,
    with_detail: bool = True,
    brief: bool = False,
) -> bytes:
    """生成一个班次的报表工作簿（`Period` 称谓为「班次」，标题为「班次报表」）。"""
    start, end = shift.bounds(tz)
    return _build_period_workbook(
        settings=settings,
        period=Period(start=start, end=end, label=shift.label),
        tz=tz,
        rows=rows,
        summary=summary,
        events=events,
        alarms_now=alarms_now,
        rows_total=rows_total,
        generated_at=generated_at,
        complete=complete,
        flow_rows=flow_rows,
        flow_summary=flow_summary,
        with_detail=with_detail,
        brief=brief,
    )


def build_window_workbook(
    *,
    settings: Any,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    events: Iterable[dict[str, Any]],
    alarms_now: dict[str, Any],
    rows_total: int,
    generated_at: datetime,
    complete: bool = True,
    flow_rows: Sequence[dict[str, Any]] = (),
    flow_summary: dict[str, Any] | None = None,
    with_detail: bool = True,
    brief: bool = False,
) -> bytes:
    """生成一个**自定义时段**的报表工作簿（起止精确到分钟，可跨班次）。

    结构与班次报表完全一致（区间统计 / 流量 / 综合干矿量 / 趋势图 / 报警事件 +
    可选明细），只是标题、称谓与文件名换成「时段」；口径不做任何特殊化。
    """
    period = Period(
        start=start,
        end=end,
        label=f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}",
        noun="时段",
        headline="ECAS 浓度计时段报表",
        open_label="未结束（统计到导出时刻）",
        sheet_title="时段报表",
    )
    return _build_period_workbook(
        settings=settings,
        period=period,
        tz=tz,
        rows=rows,
        summary=summary,
        events=events,
        alarms_now=alarms_now,
        rows_total=rows_total,
        generated_at=generated_at,
        complete=complete,
        flow_rows=flow_rows,
        flow_summary=flow_summary,
        with_detail=with_detail,
        brief=brief,
    )


def window_filename(settings: Any, start: datetime, end: datetime) -> str:
    """自定义时段报表文件名：`ecas_DPRO-66_window_20261105-0800_20261105-1430.xlsx`。"""
    return (
        f"ecas_{_ascii_slug(settings.model_name)}_window_"
        f"{start:%Y%m%d-%H%M}_{end:%Y%m%d-%H%M}.xlsx"
    )
