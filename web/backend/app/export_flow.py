"""流量计（FMC400）导出：`/api/export/flow.xlsx` 的工作簿 + 行口径。

与浓度计导出一致的三条原则
--------------------------
* **口径同源**：寄存器解码只走 `app.flow.decode`，本模块不做第二次换算；
* **不带采集细节**：表格里只有工程量（流量/流速/累积/流向/状态），不写站号、寄存器号、
  传输链路 —— 那些是排查用的，`/api/flow/readings` 里本来就有；
* **openpyxl 延迟导入**：看板不该为「偶尔导一次」付启动开销。

2026-11-04 先生定：流量计区块也要按班次看、也要能导出。
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from . import flow

#: 数据区表头（顺序即列序）
HEADERS: tuple[str, ...] = ("时间", "流量(m³/h)", "流速(m/s)", "正向累积(m³)", "流向", "状态")

SHEET_TITLE = "流量数据"


def _local_naive(value: datetime, tz: Any) -> datetime:
    return value.astimezone(tz).replace(tzinfo=None)


def decode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """库行（`ts` + `regs`）→ 工程量行；解码失败（寄存器块不完整）时整行跳过并保留原因。

    流量计的 14 个寄存器缺一块就算不出可信的流量，宁可不写这一行，也不要半块拼出假数。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            decoded = flow.decode(row["regs"])
        except (TypeError, ValueError):
            continue
        out.append({"ts": row["ts"], **decoded})
    return out


def build_flow_workbook(
    *,
    start: datetime,
    end: datetime,
    hours: float,
    rows: list[dict[str, Any]],
    tz: Any,
    generated_at: datetime | None = None,
) -> bytes:
    """把流量工程量行写成工作簿（标题区写清区间/样本数，便于事后核对）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    now = generated_at or datetime.now().astimezone()
    items = decode_rows(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE

    last_col = len(HEADERS)
    last_letter = get_column_letter(last_col)

    ws.merge_cells(f"A1:{last_letter}1")
    title = ws["A1"]
    title.value = f"流量监测 · {flow.MODEL}"
    title.font = Font(size=14, bold=True)

    header_row = 2 + 7  # 标题 + 7 行元信息 + 1 空行 ⇒ 表头落在第 9 行
    meta = [
        ("区间", f"{start.astimezone(tz):%Y-%m-%d %H:%M} ~ {end.astimezone(tz):%Y-%m-%d %H:%M}（{hours:g} 小时）"),
        ("生成时间", f"{now.astimezone(tz):%Y-%m-%d %H:%M:%S}"),
        ("样本数", f"{len(items)} 行"),
        ("单位", "流量 m³/h ｜ 流速 m/s ｜ 累积 m³（32 位计数，会回绕）"),
        ("流向", "仪表判定（0=反向，1=正向）"),
        ("状态", "正常 / 空管 / 反流 / 报警（判据见 /api/flow/params）"),
        ("说明", "正向累积是仪表累计值；区间净增量请用 /api/flow/summary（回绕/清零时不给）。"),
    ]
    label_font = Font(bold=True)
    for i, (key, value) in enumerate(meta, start=2):
        ws.cell(row=i, column=1, value=key).font = label_font
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=last_col)
        ws.cell(row=i, column=2, value=value).alignment = Alignment(vertical="center")

    head_fill = PatternFill("solid", fgColor="F1F5F9")
    thin = Side(style="thin", color="CBD5E1")
    for col, name in enumerate(HEADERS, start=1):
        cell = ws.cell(row=header_row, column=col, value=name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = Border(bottom=thin)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r, item in enumerate(items, start=header_row + 1):
        ts_cell = ws.cell(row=r, column=1, value=_local_naive(item["ts"], tz))
        ts_cell.number_format = "yyyy-mm-dd hh:mm:ss"
        flow_cell = ws.cell(row=r, column=2, value=round(float(item["flow_m3h"]), 2))
        flow_cell.number_format = "0.00"
        vel_cell = ws.cell(row=r, column=3, value=round(float(item["velocity_mps"]), 3))
        vel_cell.number_format = "0.000"
        ws.cell(row=r, column=4, value=int(item["total_m3"]))
        ws.cell(row=r, column=5, value=item["direction_label"])
        ws.cell(row=r, column=6, value=item["status"])

    last_row = header_row + max(len(items), 0)
    ws.freeze_panes = f"A{header_row + 1}"
    if items:
        ws.auto_filter.ref = f"A{header_row}:{last_letter}{last_row}"

    for col, width in enumerate((22, 12, 11, 13, 8, 9), start=1):
        ws.column_dimensions[get_column_letter(col)].width = width

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
