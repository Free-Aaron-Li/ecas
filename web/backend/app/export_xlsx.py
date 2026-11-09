"""`/api/export.xlsx` 的工作簿生成：同区间的**浓度结果**，不含仪表原始计数。

与 `/api/export.csv` 的分工
---------------------------
* CSV 是给对账/排查用的：型号、原始计数 raw、未补偿浓度一应俱全；
* 本工作簿是给看数据的人用的：时间 / 浓度 / 状态 / 越限，**没有 raw、也没有电流列**
  （电流同样是仪表内部量，2026-11-04 先生定：导出与看板都只留浓度）。
  这是先生 2026-11-04 要的「多一个选择」——同一区间、同一口径，只是不带原始数据。

设计要点
--------
* **口径同源**：换算全部走 `app.convert`，阈值由调用方从 `alarms.effective_alarms` 取，
  这里绝不另立口径 —— 表格里的每个数字都必须能在网页上逐一对上；
* **openpyxl 延迟导入**（与 `report.py` 一致）：看板本身不该为导出功能付启动开销；
* 样式只加在标题/表头上：5 万行时逐格设样式会明显拖慢导出，数值列按 3 位小数取值即可。
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any, Mapping, Sequence

from . import convert

#: 数据区表头（顺序即列序）。只有浓度口径的列 —— raw / 电流 / 型号都不进表
#: （型号写进标题区；raw 与电流保留在 CSV 那份「对账用」的导出里）。
HEADERS: tuple[str, ...] = ("时间", "浓度(%)", "状态", "浓度越限")

SHEET_TITLE = "浓度数据"


def _stamp(value: datetime | None, tz: Any) -> str:
    """标题区用的本地时间串（看着像人写的，不是 ISO）；空值写「无数据」。"""
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else "无数据"


def _local_naive(value: datetime, tz: Any) -> datetime:
    """数据区的时间列：写 Excel 原生日期（可排序/筛选），去掉时区信息。"""
    return value.astimezone(tz).replace(tzinfo=None)


def _offset_text(settings: Any) -> str:
    """补偿方式的人话（与看板 KPI 的 sub 一致）。"""
    if getattr(settings, "offset_mode", "add") == "mul":
        return f"×{1 + settings.offset_pct / 100:.4f}"
    value = settings.offset_conc
    return f"{value:+g}"


def _exceed(conc: float, status: str, low: float, high: float) -> str:
    """越限标注：只在「正常」状态有意义（断线/超量程的浓度是外推值）。"""
    if status != "正常":
        return ""
    if conc > high:
        return "高于上限"
    if conc < low:
        return "低于下限"
    return ""


def build_readings_workbook(
    *,
    settings: Any,
    start: datetime,
    end: datetime,
    hours: int,
    rows: Sequence[Mapping[str, Any]],
    alarms_now: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> bytes:
    """把一段区间的读数做成 xlsx 字节流（不含 raw）。

    `rows` 是 `SQL_EXPORT` 的结果（`ts` / `model` / `raw`，按时间正序）；
    `alarms_now` 是 `alarms.effective_alarms()` 的输出（阈值与看板同源）。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    tz = settings.display_tz
    now = generated_at or datetime.now(tz)
    low, high = float(alarms_now["low"]), float(alarms_now["high"])

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE

    # ── 标题区：口径说明写清楚，但只写「这份表是什么」，不写采集链路 ──
    ws.merge_cells("A1:D1")
    title = ws["A1"]
    title.value = f"浓度监测 · {settings.model_name}"
    title.font = Font(size=14, bold=True)

    info: list[tuple[str, str]] = [
        ("区间", f"{_stamp(start, tz)} ~ {_stamp(end, tz)}（{hours} 小时）"),
        ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
        ("样本数", f"{len(rows)} 行"),
        ("补偿", _offset_text(settings)),
        (
            "报警阈值",
            f"下限 {low:g} ｜ 上限 {high:g}" + ("（自定义）" if alarms_now.get("custom") else "（默认）"),
        ),
        ("说明", "断线/超量程行的浓度为外推值，无物理意义 —— 请按状态列判断。"),
    ]
    label_font = Font(bold=True)
    last_label_row = 1
    for i, (label, value) in enumerate(info, start=2):
        ws.cell(row=i, column=1, value=label).font = label_font
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)
        cell = ws.cell(row=i, column=2, value=value)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        last_label_row = i

    # ── 数据区：第 9 行表头（上面留一行空行，冻结时表头跟着走）──
    head_row = last_label_row + 2
    head_fill = PatternFill("solid", fgColor="F1F5F9")
    thin = Side(style="thin", color="CBD5E1")
    border = Border(bottom=thin)
    for col, name in enumerate(HEADERS, start=1):
        cell = ws.cell(row=head_row, column=col, value=name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r, row in enumerate(rows, start=head_row + 1):
        reading = convert.convert(int(row["raw"]), settings)
        ts_cell = ws.cell(row=r, column=1, value=_local_naive(row["ts"], tz))
        ts_cell.number_format = "yyyy-mm-dd hh:mm:ss"
        conc_cell = ws.cell(row=r, column=2, value=round(float(reading["conc"]), 3))
        conc_cell.number_format = "0.000"
        ws.cell(row=r, column=3, value=reading["status"])
        # 浓度越限按「生效阈值」判定，和看板报警线同源
        exceed = _exceed(float(reading["conc"]), reading["status"], low, high)
        if exceed:
            ws.cell(row=r, column=4, value=exceed)

    # ── 表格可用性：冻结表头 + 自动筛选 + 列宽 ──
    ws.freeze_panes = f"A{head_row + 1}"
    last_row = max(head_row, head_row + len(rows))
    ws.auto_filter.ref = f"A{head_row}:{get_column_letter(len(HEADERS))}{last_row}"

    widths = (22, 11, 10, 11)
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
