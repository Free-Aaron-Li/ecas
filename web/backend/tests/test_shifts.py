"""班次模型、班次报表与 `/api/shifts`、`/api/export/shifts` 的测试。

分三层：
1. **纯逻辑**（不需要数据库）：`shifts.py` 的边界、命名、切分与上限；
2. **纯渲染**：`report.build_shift_workbook()` 的时区归一 —— 这条是回归测试，
   守的是一个真出过的 bug：服务器跑 UTC 时报表时间比看板早 8 小时；
3. **接口**（需要本地 `ecas_dev`）：预览与导出必须**对同一批班次、同一个文件名**说话。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import convert, report, shifts
from app.settings import get_settings

TOKEN = "shifts-token"
TZ = ZoneInfo("Asia/Shanghai")


# ==================================================================== 第一层：纯逻辑


def test_班次边界各十二小时且首尾相接() -> None:
    """夜班归属**结束日**（2026-11-11 先生定）：夜班(D) = D-1 20:00 → D 08:00，紧接白班(D)。"""
    day = shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25))
    night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25))
    next_night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 26))

    d_start, d_end = day.bounds(TZ)
    n_start, n_end = night.bounds(TZ)

    assert (d_start.hour, d_end.hour) == (8, 20), "白班必须是 08:00–20:00"
    assert (n_start.hour, n_end.hour) == (20, 8), "夜班必须是 20:00–次日 08:00"
    assert n_end == d_start, "夜班终点必须正好接上白班起点（无缝）"
    assert n_start.date() == date(2026, 9, 24), "夜班归属结束日 ⇒ 从前一晚 20:00 开始"
    assert n_end.date() == date(2026, 9, 25), "夜班终点落在归属日早上 08:00"
    # 白班终点必须接上**下一个**夜班的起点
    assert d_end == next_night.bounds(TZ)[0], "白班终点必须接上当晚 20:00 开始的夜班"
    # 两边都是 12 小时
    assert (d_end - d_start).total_seconds() == 12 * 3600
    assert (n_end - n_start).total_seconds() == 12 * 3600


def test_夜班归属结束那天() -> None:
    """2026-11-11 先生定：11.10 20:00～11.11 08:00 统计在 11.11 夜班。

    ⇒「2026-09-25 夜班」= 09-24 20:00 → 09-25 08:00。
    """
    night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25))
    start, end = night.bounds(TZ)
    assert start.date() == date(2026, 9, 24)
    assert end.date() == date(2026, 9, 25)
    assert night.label == "2026-09-25 夜班"
    assert night.label_long(TZ) == "2026-09-25 夜班（09-24 20:00 ~ 09-25 08:00）"
    assert night.slug == "20260925-night"


def test_边界时刻归属左闭右开() -> None:
    """`contains` 左闭右开：08:00 整属于白班，20:00 整属于（次日的）夜班，不能双算。"""
    day = shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25))
    night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 26))  # 覆盖 09-25 20:00 起
    prev_night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25))  # 覆盖 09-24 20:00 → 09-25 08:00
    at = lambda h, m=0: datetime(2026, 9, 25, h, m, tzinfo=TZ)  # noqa: E731

    assert day.contains(at(8), TZ) and not day.contains(at(7, 59), TZ)
    assert not day.contains(at(20), TZ), "20:00 整应归夜班，白班不能重复计入"
    assert night.contains(at(20), TZ)
    assert night.contains(at(23, 59), TZ)
    assert not night.contains(at(8), TZ), "08:00 整应归白班"
    assert prev_night.contains(at(7, 59), TZ), "09-25 早上 07:59 属「09-25 夜班」"
    assert not prev_night.contains(at(8), TZ), "08:00 整归白班，夜班右开"


def test_进行中判定() -> None:
    night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25))  # 09-24 20:00 → 09-25 08:00
    assert not night.is_complete(datetime(2026, 9, 25, 7, 59, tzinfo=TZ), TZ)
    assert night.is_complete(datetime(2026, 9, 25, 8, 0, tzinfo=TZ), TZ)


@pytest.mark.parametrize(
    ("moment", "kind", "day"),
    [
        ((2026, 9, 25, 7, 0), shifts.KIND_NIGHT, (2026, 9, 25)),  # 凌晨属「当天结束」的夜班
        ((2026, 9, 25, 8, 0), shifts.KIND_DAY, (2026, 9, 25)),  # 08:00 整 = 白班
        ((2026, 9, 25, 19, 59), shifts.KIND_DAY, (2026, 9, 25)),
        ((2026, 9, 25, 20, 0), shifts.KIND_NIGHT, (2026, 9, 26)),  # 20:00 起 = 次日结束的夜班
        ((2026, 9, 26, 0, 30), shifts.KIND_NIGHT, (2026, 9, 26)),  # 午夜仍属该夜班
        ((2026, 9, 26, 8, 0), shifts.KIND_DAY, (2026, 9, 26)),
    ],
)
def test_current_shift(moment: tuple, kind: str, day: tuple) -> None:
    now = datetime(*moment, tzinfo=TZ)
    cur = shifts.current_shift(now, TZ)
    assert (cur.kind, cur.day) == (kind, date(*day))


def test_当前班次与包含关系自洽() -> None:
    """`current_shift()` 给出的班必须真的 `contains()` 这个时刻（避免边界 off-by-one）。"""
    for hour in range(24):
        for minute in (0, 30, 59):
            now = datetime(2026, 9, 25, hour, minute, tzinfo=TZ)
            assert shifts.current_shift(now, TZ).contains(now, TZ), f"{now} 不在自己所属的班里"


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        ("day,night", ("day", "night")),
        ("night,day", ("day", "night")),  # 输出顺序固定：白班在前
        ("白班,夜班", ("day", "night")),
        ("夜班", ("night",)),
        ("day", ("day",)),
        (["day", "day", "night"], ("day", "night")),  # 去重
        (" day , night ", ("day", "night")),  # 容忍空格
        (None, ("day", "night")),  # 缺省 = 两班都要
        ("DAY,Night", ("day", "night")),  # 大小写不敏感
    ],
)
def test_parse_kinds(value, expect: tuple) -> None:
    assert shifts.parse_kinds(value) == expect


@pytest.mark.parametrize("bad", ["", "   ", "morning", "白班,xxx", ","])
def test_parse_kinds_非法值报错(bad: str) -> None:
    """非法值必须报错，不能静默吞成「两班都要」—— 少导一个班次比报错更难发现。"""
    with pytest.raises(shifts.ShiftRangeError):
        shifts.parse_kinds(bad)


def test_切分顺序与数量() -> None:
    one = shifts.shifts_in_range(date(2026, 9, 25), date(2026, 9, 25))
    assert [s.label for s in one] == ["2026-09-25 白班", "2026-09-25 夜班"], "白班在前、夜班在后"

    two = shifts.shifts_in_range(date(2026, 9, 25), date(2026, 9, 26))
    assert len(two) == 4
    assert [s.kind for s in two] == ["day", "night", "day", "night"]
    # 按时间排序后必须首尾相接（夜班归属结束日 ⇒ 时间序是 夜(25) → 白(25) → 夜(26) → 白(26)）
    chrono = sorted(two, key=lambda s: s.bounds(TZ)[0])
    bounds = [s.bounds(TZ) for s in chrono]
    for (_, prev_end), (next_start, _) in zip(bounds, bounds[1:]):
        assert prev_end == next_start

    nights = shifts.shifts_in_range(date(2026, 9, 25), date(2026, 9, 26), ("night",))
    assert [s.label for s in nights] == ["2026-09-25 夜班", "2026-09-26 夜班"]


def test_起止颠倒自动纠正() -> None:
    """用户在两个日期框里填反是常事，纠正而不是报错。"""
    forward = shifts.shifts_in_range(date(2026, 9, 25), date(2026, 9, 26))
    backward = shifts.shifts_in_range(date(2026, 9, 26), date(2026, 9, 25))
    assert [s.label for s in forward] == [s.label for s in backward]


def test_上限校验() -> None:
    # 31 天 × 2 班 = 62 班，正好卡在上限
    full = shifts.validate_range(date(2026, 9, 1), date(2026, 10, 1), shifts.KIND_ORDER)
    assert len(full) == 62 == shifts.MAX_SHIFTS

    with pytest.raises(shifts.ShiftRangeError, match="上限"):
        shifts.validate_range(date(2026, 9, 1), date(2026, 10, 2), shifts.KIND_ORDER)

    with pytest.raises(shifts.ShiftRangeError, match="上限"):
        shifts.validate_range(date(2026, 1, 1), date(2026, 12, 31), shifts.KIND_ORDER)


def test_未知种类构造即报错() -> None:
    with pytest.raises(shifts.ShiftRangeError):
        shifts.Shift("morning", date(2026, 9, 25))


def test_describe_不给易误导的_filename() -> None:
    """`describe()` 不该给 `filename`：真实文件名带型号前缀，只有 settings 知道。"""
    item = shifts.describe(shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25)), TZ)
    assert item["label_long"] == "2026-09-25 夜班（09-24 20:00 ~ 09-25 08:00）"
    assert item["start"] == "2026-09-24T20:00:00+08:00"
    assert item["end"] == "2026-09-25T08:00:00+08:00"
    assert "filename" not in item


def test_文件名拼接() -> None:
    settings = get_settings()
    day = shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25))
    night = shifts.Shift(shifts.KIND_NIGHT, date(2026, 9, 25))

    name = report.shift_filename(settings, night)
    assert name == "ecas_DPRO-66_20260925-night.xlsx"
    # HTTP 头按协议只能是 ASCII（Content-Disposition 的编码坑）
    name.encode("ascii")

    bundle = report.bundle_filename(settings, [day, night])
    assert bundle == "ecas_DPRO-66_shifts_20260925-20260925.zip"
    bundle.encode("ascii")


def test_型号名里的非_ASCII_被换成下划线() -> None:
    """`Content-Disposition` 只能放 ASCII ⇒ 型号里的中文与空格一律折成 `_`。"""
    class Fake:
        model_name = "浓度计 DPRO-66"

    name = report.shift_filename(Fake(), shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)))
    # 「浓度计」3 字 + 1 个空格 = 4 个下划线；`DPRO-66` 的 `-` 在白名单里、保留
    assert name == "ecas_" + "_" * 4 + "DPRO-66_20260925-day.xlsx"
    name.encode("ascii")  # 不抛就说明纯 ASCII


# ==================================================================== 第二层：纯渲染


def _summary(n: int = 1, raw: int = 12853) -> dict:
    """照 `SQL_SUMMARY` 的返回形状造假（`raw_avg` 用 Decimal —— psycopg 就是这么给的）。"""
    return {
        "n": n,
        "n_valid": n,
        "broken": 0,
        "low": 0,
        "over": 0,
        "n_below_alarm": 0,
        "n_above_alarm": 0,
        "raw_min": raw,
        "raw_max": raw,
        "raw_avg": Decimal(raw),
    }


def _workbook_bytes(
    rows: list[dict],
    *,
    tz: ZoneInfo = TZ,
    complete: bool = True,
    shift=None,
    flow_rows: list[dict] | None = None,
    flow_summary: dict | None = None,
    with_detail: bool = True,
    brief: bool = False,
) -> bytes:
    settings = get_settings()
    return report.build_shift_workbook(
        settings=settings,
        shift=shift or shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)),
        tz=tz,
        rows=rows,
        summary=_summary(len(rows)),
        events=[],
        alarms_now=convert.resolve_alarms(settings),
        rows_total=len(rows),
        generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=tz),
        complete=complete,
        flow_rows=flow_rows or (),
        flow_summary=flow_summary,
        with_detail=with_detail,
        brief=brief,
    )


def _all_text(payload: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(payload))
    out: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    out.append(str(cell.value))
    return "\n".join(out)


def _media(payload: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return [n for n in z.namelist() if n.startswith("xl/media/")]


def test_时区归一_UTC入参也要按展示时区出报表() -> None:
    """回归测试：库里是 timestamptz，服务器若跑 UTC，报表时间会整体早 8 小时。

    这个 bug 真出过 —— 报表上写「07:58」，看板上却写着「15:58」，对账时没人信得过报表。
    """
    rows = [{"ts": datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc), "model": "DPRO-66", "raw": 12853}]
    text = _all_text(_workbook_bytes(rows))

    assert "2026-09-25 09:00:00" in text, "UTC 01:00 必须显示成本地 09:00"
    assert "2026-09-25 01:00:00" not in text, "不能把裸 UTC 时间直接写进报表"


def test_生成时间也按展示时区() -> None:
    rows = [{"ts": datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc), "model": "DPRO-66", "raw": 12853}]
    # generated_at 给 UTC 20:00 ⇒ 本地应是次日 04:00
    settings = get_settings()
    payload = report.build_shift_workbook(
        settings=settings,
        shift=shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)),
        tz=TZ,
        rows=rows,
        summary=_summary(),
        events=[],
        alarms_now=convert.resolve_alarms(settings),
        rows_total=1,
        generated_at=datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc),
        complete=True,
    )
    assert "2026-09-26 04:00:00" in _all_text(payload)


def test_工作簿结构_两张表加内嵌曲线图() -> None:
    from openpyxl import load_workbook

    rows = [
        {"ts": datetime(2026, 9, 25, 8, 0 + i, tzinfo=TZ), "model": "DPRO-66", "raw": 12000 + i * 10}
        for i in range(12)
    ]
    payload = _workbook_bytes(rows)

    wb = load_workbook(io.BytesIO(payload))
    assert wb.sheetnames == ["班次报表", "读数明细"]

    detail = wb["读数明细"]
    assert detail.freeze_panes == "A2", "明细要冻结表头"
    assert detail.auto_filter.ref == f"A1:I{len(rows) + 1}", "明细要开筛选器"
    assert [detail.cell(row=1, column=c).value for c in range(1, 10)] == [
        "序号",
        "时间",
        "型号",
        "原始值",
        "电流(mA)",
        "未补偿浓度",
        "浓度",
        "状态",
        "浓度越限",
    ]
    # 每行都能被换算函数复现（报表不自立口径）
    settings = get_settings()
    for i, row in enumerate(rows, start=1):
        expect = convert.convert(row["raw"], settings)
        assert detail.cell(row=i + 1, column=1).value == i
        assert detail.cell(row=i + 1, column=2).value == f"{row['ts']:%Y-%m-%d %H:%M:%S}"
        assert detail.cell(row=i + 1, column=4).value == row["raw"]
        assert detail.cell(row=i + 1, column=7).value == pytest.approx(expect["conc"], abs=1e-9)
        assert detail.cell(row=i + 1, column=8).value == expect["status"]

    media = _media(payload)
    assert media == ["xl/media/image1.png"], "曲线图必须真的嵌进 xlsx，而不是只给个链接或路径"


def test_曲线图中文不走豆腐块() -> None:
    """图里的中文必须用真字形渲染，不能退化成方块/英文。

    本机读图工具不可用（vision 引擎缺失），所以用程序化断言：
    渲染期间 matplotlib 一条 `missing from font` 警告都不许有。
    """
    import warnings

    rows = [
        {"ts": datetime(2026, 9, 25, 8, 0 + i, tzinfo=TZ), "model": "DPRO-66", "raw": 11500 + i * 60}
        for i in range(20)
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _workbook_bytes(rows)
    missing = [str(w.message) for w in caught if "missing from font" in str(w.message).lower()]
    assert not missing, f"图表缺字形（会显示成豆腐块）：{missing}"


def test_没有数据也要出得来报表() -> None:
    """班次里一条数据都没有是常态（停机、断流），报表必须照样生成而不是崩。

    有效点不足 2 个时**故意不画图**（空图没有信息量），改成一行写明原因的说明 ——
    这比一张空白坐标轴有用：看报表的人一眼就知道是「这段真没数据」，而不是「图坏了」。
    """
    payload = _workbook_bytes([])
    text = _all_text(payload)
    assert "班次报表" in text
    assert not _media(payload), "没有有效点就不该硬塞一张空图"
    assert "无法成图" in text, "不画图时必须写明原因，不能让人以为是图挂了"


def test_单班行数上限会写明截断() -> None:
    """超过单班行数上限时，报表要明说「只导出了前 N 行」，不能悄悄少给。"""
    class Tight:
        model_name = "DPRO-66"
        export_rows_max = 3

        def __getattr__(self, name: str):  # 其余字段照抄真实 settings
            return getattr(get_settings(), name)

    rows = [
        {"ts": datetime(2026, 9, 25, 8, 0, i, 0, tzinfo=TZ), "model": "DPRO-66", "raw": 12000 + i}
        for i in range(10)
    ]
    payload = report.build_shift_workbook(
        settings=Tight(),
        shift=shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)),
        tz=TZ,
        rows=rows[:3],
        summary=_summary(10),
        events=[],
        alarms_now=convert.resolve_alarms(get_settings()),
        rows_total=10,
        generated_at=datetime(2026, 9, 25, 20, 0, tzinfo=TZ),
        complete=True,
    )
    assert "只导出了前" in _all_text(payload)
    # 这行长提示横跨 A:I，不该把「序号」列撑宽（宽度由行高/换行承载）
    assert _load(payload)["读数明细"].column_dimensions["A"].width <= 12


# ============================================================ 第三层：排版自适应
# 先生的要求：「单元格中文本自动换行，单元格长度宽度自动匹配最佳」。
# 要点：Excel 只在**没被显式设过行高**的非合并单元格上自动撑高，合并单元格一律不撑
# （Excel 与 LibreOffice 都是这个行为）。本报表大量用合并单元格放长句，所以行高必须
# 我们自己按折行数估。下面这组断言盯的就是三件事：wrap 开了没有、列宽够不够、
# 长句所在的行有没有被加高。


def _load(payload: bytes):
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(payload))


def _find_row(sheet, text: str) -> int:
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value == text:
                return cell.row
    raise AssertionError(f"工作表 {sheet.title!r} 里没找到 {text!r}")


def _rows(n: int = 5, raw: int = 12853) -> list[dict]:
    return [
        {"ts": datetime(2026, 9, 25, 8, 0, i * 7, tzinfo=TZ), "model": "DPRO-66", "raw": raw + i * 40}
        for i in range(n)
    ]


def test_排版_每个有值的格子都开了自动换行() -> None:
    wb = _load(_workbook_bytes(_rows()))
    bad = [
        f"{ws.title}!{cell.coordinate}"
        for ws in wb.worksheets
        for row in ws.iter_rows()
        for cell in row
        if cell.value is not None and not cell.alignment.wrap_text
    ]
    assert bad == [], f"这些格子没开自动换行：{bad[:8]}"


def test_排版_跨列合并的长句按折行数加高行() -> None:
    """「异常阈值」的说明横跨 B:D，远长于列宽 ⇒ 必然折行。

    这条正是回归点：合并单元格 Excel 不会自动撑高，我们不算高度的话，用户看到的是
    一行被截断的说明文字。
    """
    ws = _load(_workbook_bytes(_rows()))["班次报表"]
    height = ws.row_dimensions[_find_row(ws, "异常阈值")].height
    assert height and height > 20, f"长句行没被加高，说明会被截断：height={height!r}"


def test_排版_列宽按内容自适应且长句不把单列撑爆() -> None:
    """列宽由「本列最宽的那个内容」决定；被上限截住的列交给换行去承载。

    这条同时盯住了两个方向：列不能太窄（放不下内容），也不能被跨列长句撑得失控。
    """
    from openpyxl.utils import get_column_letter

    ws = _load(_workbook_bytes(_rows()))["班次报表"]
    max_width, margin = 28.0, 2.4      # 与 _fit_columns 的 (max_width, +2.4 余量) 对齐
    spans = {(r.min_row, r.min_col) for r in ws.merged_cells.ranges if r.max_col > r.min_col}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None or (cell.row, cell.column) in spans:
                continue
            width = ws.column_dimensions[get_column_letter(cell.column)].width or 8.43
            assert width <= max_width + 0.5, f"{cell.coordinate} 列宽失控：{width}"
            need = report._display_width(cell.value)
            if need + margin <= max_width:     # 没触上限 ⇒ 必须真放得下
                assert width >= need, f"{cell.coordinate} 列宽放不下内容：{width} < {need}"


def test_排版_读数明细列宽按内容且序号列不被撑宽() -> None:
    detail = _load(_workbook_bytes(_rows()))["读数明细"]
    assert detail.column_dimensions["B"].width >= 19, "时间列放不下 19 字符的时间戳"
    assert detail.column_dimensions["A"].width <= 12, "序号列被长内容无故撑宽"


# ==================================================================== 第三层：接口


def _db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(get_settings().dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # 库没起 / 没建库都算跳过
        return False


needs_db = pytest.mark.skipif(not _db_up(), reason="本地 ecas_dev 未运行")


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    return TOKEN


@needs_db
def test_班次清单需要令牌(client: TestClient, with_token: str) -> None:
    assert client.get("/api/shifts?start=2026-09-25&end=2026-09-25").status_code == 401
    assert client.get(f"/api/shifts?start=2026-09-25&end=2026-09-25&token={with_token}").status_code == 200


@needs_db
def test_班次清单内容(client: TestClient, with_token: str) -> None:
    body = client.get(f"/api/shifts?start=2026-09-25&end=2026-09-26&token={with_token}").json()
    assert body["count"] == 4
    assert body["tz"] == get_settings().tz
    assert [s["label"] for s in body["shifts"]] == [
        "2026-09-25 白班",
        "2026-09-25 夜班",
        "2026-09-26 白班",
        "2026-09-26 夜班",
    ]
    assert body["shifts"][0]["label_long"] == "2026-09-25 白班（09-25 08:00 ~ 09-25 20:00）"
    assert body["shifts"][1]["label_long"] == "2026-09-25 夜班（09-24 20:00 ~ 09-25 08:00）"
    assert body["shifts"][1]["start"] == "2026-09-24T20:00:00+08:00"


@needs_db
def test_班次清单的_filename_与实际导出一致(client: TestClient, with_token: str) -> None:
    """预览报的文件名必须**逐字**等于真下下来的文件名，否则 zip 里没法对账。"""
    name = client.get(f"/api/shifts?start=2026-09-25&end=2026-09-25&kinds=night&token={with_token}").json()[
        "shifts"
    ][0]["filename"]

    resp = client.get(f"/api/export/shifts?start=2026-09-25&end=2026-09-25&kinds=night&token={with_token}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == f'attachment; filename="{name}"'


@needs_db
def test_班次报表可以不带读数明细(client: TestClient, with_token: str) -> None:
    """先生 2026-11-04：报表要能选「要不要数据明细」——`detail=false` 时不建「读数明细」表。"""
    from io import BytesIO

    from openpyxl import load_workbook

    def fetch(*, detail: bool) -> bytes:
        url = f"/api/export/shifts?start=2026-09-25&end=2026-09-25&kinds=night&token={with_token}"
        if not detail:
            url += "&detail=false"
        resp = client.get(url)
        assert resp.status_code == 200
        return resp.content

    full, lean = fetch(detail=True), fetch(detail=False)
    full_sheets = load_workbook(BytesIO(full)).sheetnames
    lean_sheets = load_workbook(BytesIO(lean)).sheetnames
    assert "读数明细" in full_sheets
    assert "读数明细" not in lean_sheets
    # 统计、趋势、报警事件都在第一张表里，删明细不影响它
    assert lean_sheets[0] == full_sheets[0] == "班次报表"
    # 明细是大头：去掉后文件确实更小
    assert len(lean) < len(full)

@pytest.mark.parametrize(
    "query",
    [
        "start=2026-9-25&end=2026-09-25",  # 月份没补零
        "start=&end=2026-09-25",  # 空
        "start=2026-09-25&end=昨天",  # 中文
        "start=2026-09-25&end=",  # 只给了起点
        "start=09/25/2026&end=2026-09-25",  # 美式写法
    ],
)
@needs_db
def test_班次清单_日期非法报_400(client: TestClient, with_token: str, query: str) -> None:
    """非法日期要说清「要什么格式」，不能只丢一个 HTTP 400 让人猜。"""
    resp = client.get(f"/api/shifts?{query}&token={with_token}")
    assert resp.status_code == 400, query
    assert "YYYY-MM-DD" in resp.json()["detail"]


@needs_db
def test_班次清单_容忍紧凑与完整_ISO_写法(client: TestClient, with_token: str) -> None:
    """`20260925`（ISO 8601 basic）与 `2026-09-25T08:00` 都算同一天 —— 只取日期部分。"""
    tight = client.get(f"/api/shifts?start=20260925&end=20260925&token={with_token}")
    assert tight.status_code == 200
    assert tight.json()["count"] == 2


@needs_db
def test_班次清单_超上限报_400(client: TestClient, with_token: str) -> None:
    resp = client.get(f"/api/shifts?start=2026-01-01&end=2026-12-31&token={with_token}")
    assert resp.status_code == 400
    assert "上限" in resp.json()["detail"]


@needs_db
def test_班次清单_容忍完整_ISO_时间串(client: TestClient, with_token: str) -> None:
    """用户把 `2026-09-25T08:00` 粘进日期框也得能用（只取日期部分）。"""
    resp = client.get(f"/api/shifts?start=2026-09-25T08:00&end=2026-09-25T20:00&token={with_token}")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


@needs_db
def test_单班导出_xlsx(client: TestClient, with_token: str) -> None:
    # 新口径：夜班归属结束日 ⇒ 数据窗口 09-25 20:00–09-26 08:00 对应「09-26 夜班」
    resp = client.get(f"/api/export/shifts?start=2026-09-26&end=2026-09-26&kinds=night&token={with_token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content[:2] == b"PK", "xlsx 本质是 zip"

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["班次报表", "读数明细"]
    assert len(wb["读数明细"]["B"]) >= 2, "这个班次应至少有若干条数据"


@needs_db
def test_多班导出_zip_且附说明(client: TestClient, with_token: str) -> None:
    resp = client.get(f"/api/export/shifts?start=2026-09-26&end=2026-09-26&token={with_token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    name = resp.headers["content-disposition"].split('filename="')[1].rstrip('"')
    assert name == report.bundle_filename(get_settings(), shifts.shifts_in_range(date(2026, 9, 26), date(2026, 9, 26)))
    name.encode("ascii")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        members = z.namelist()
        assert "说明.txt" in members, "多班打包要附一份文件↔班次对账说明"
        assert members.count("说明.txt") == 1

        books = [m for m in members if m.endswith(".xlsx")]
        assert len(books) == 2, "白班 + 夜班 = 2 份"
        assert books == ["ecas_DPRO-66_20260926-day.xlsx", "ecas_DPRO-66_20260926-night.xlsx"]

        # 说明里列的文件必须真的都在包里（且名字逐字一致）
        readme = z.read("说明.txt").decode("utf-8")
        assert "班次边界：白班 08:00–20:00，夜班 20:00–次日 08:00" in readme
        for book in books:
            assert book in readme
            assert zipfile.ZipFile(io.BytesIO(z.read(book))).read("xl/media/image1.png"), f"{book} 缺曲线图"


@needs_db
def test_报表统计与_summary_同窗口逐项一致(client: TestClient, with_token: str) -> None:
    """报表数字必须与看板 `/api/summary` 对得上 —— 这是这份报表可信的前提。"""
    from openpyxl import load_workbook

    query = "start=2026-09-26&end=2026-09-26&kinds=night"
    resp = client.get(f"/api/export/shifts?{query}&token={with_token}")
    assert resp.status_code == 200

    summary = client.get(
        f"/api/summary?start=2026-09-25T20:00:00%2B08:00&end=2026-09-26T08:00:00%2B08:00&token={with_token}"
    ).json()

    ws = load_workbook(io.BytesIO(resp.content))["班次报表"]
    rows = list(ws.iter_rows(values_only=True))
    head_at = {str(row[0]): i for i, row in enumerate(rows) if row[0]}
    ore_i = head_at["具体干矿量（浓度 × 流量逐点配对）"]
    conc_i = head_at["具体浓度（断线点不画）"]
    flow_i = head_at["具体流量（电磁流量计 FMC400）"]
    stats_i = head_at["区间统计"]
    alarm_i = next(i for s, i in head_at.items() if s.startswith("报警事件"))

    # 「区间统计」：A=分组、B=指标、C=值
    stats = {
        str(row[1]): str(row[2])
        for row in rows[stats_i:alarm_i]
        if row[1] and row[2] is not None
    }
    # 「综合汇总」「具体浓度」是 A=标签、B=值（值合并到 D，openpyxl 只在 B 留值）
    overview = {str(row[0]): row[1] for row in rows[:ore_i] if row[0]}
    conc_detail: dict[str, object] = {}
    for row in rows[conc_i:flow_i]:
        if row[0]:
            conc_detail[str(row[0])] = row[1]
        if row[4]:
            conc_detail[str(row[4])] = row[5]

    assert stats["有效样本"].startswith(str(summary["n_valid"]))
    assert stats["断线"].startswith(str(summary["broken"]))
    assert stats["原始最低"] == str(summary["raw_min"])
    assert stats["原始最高"] == str(summary["raw_max"])
    assert overview["平均浓度"] == f"{summary['conc_avg']:.3f} %"
    assert conc_detail["最低"] == f"{summary['conc_min']:.3f} %"
    assert conc_detail["最高"] == f"{summary['conc_max']:.3f} %"
    assert stats["低于下限"].startswith(str(summary["n_below_alarm"]))
    assert stats["高于上限"].startswith(str(summary["n_above_alarm"]))


@needs_db
def test_导出的明细与_readings_同窗口一致(client: TestClient, with_token: str) -> None:
    """明细行数 = 同窗口读数条数，且每行的原始值来自库里（不自造数据）。"""
    from openpyxl import load_workbook

    resp = client.get(f"/api/export/shifts?start=2026-09-26&end=2026-09-26&kinds=night&token={with_token}")
    ws = load_workbook(io.BytesIO(resp.content))["读数明细"]
    raws = [ws.cell(row=r, column=4).value for r in range(2, ws.max_row + 1)]
    raws = [r for r in raws if isinstance(r, int)]

    page = client.get(
        f"/api/readings?start=2026-09-25T20:00:00%2B08:00&end=2026-09-26T08:00:00%2B08:00&limit=5000&token={with_token}"
    ).json()
    assert len(raws) == page["count"]
    # readings 是倒序，明细是正序 —— 反转后应逐个相等
    assert raws == [item["raw"] for item in reversed(page["items"])]


@needs_db
def test_只导白班则只有白班(client: TestClient, with_token: str) -> None:
    """只勾白班：单日 ⇒ 单班 xlsx；跨日 ⇒ zip，且包内一条夜班都没有。"""
    one = client.get(f"/api/export/shifts?start=2026-09-25&end=2026-09-25&kinds=day&token={with_token}")
    assert one.status_code == 200
    assert one.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert one.headers["content-disposition"].endswith('filename="ecas_DPRO-66_20260925-day.xlsx"')

    two = client.get(f"/api/export/shifts?start=2026-09-25&end=2026-09-26&kinds=day&token={with_token}")
    assert two.status_code == 200
    assert two.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(two.content)) as z:
        books = [m for m in z.namelist() if m.endswith(".xlsx")]
    assert books == ["ecas_DPRO-66_20260925-day.xlsx", "ecas_DPRO-66_20260926-day.xlsx"]
    assert not any("night" in b for b in books), "只勾了白班，包里不该出现夜班"


# ==================================================================== 流量：报表整合


def _flow_rows(n: int = 3) -> list[dict]:
    """现场帧（正常）：211.16 m³/h、3.319 m/s、累积 5000 m³。"""
    regs = [0, 3319, 0, 21116, 6598, 0, 0, 5000, 0, 0, 0, 0, 0, 1]
    base = datetime(2026, 9, 25, 8, 0, tzinfo=TZ)
    return [
        {"ts": base + timedelta(minutes=10 * i), "model": "FMC400", "regs": list(regs)}
        for i in range(n)
    ]


def test_班次报表含流量汇总段() -> None:
    """有流量数据的班次：Sheet1 多一段「流量（电磁流量计 FMC400）」。"""
    settings = get_settings()
    payload = report.build_shift_workbook(
        settings=settings,
        shift=shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)),
        tz=TZ,
        rows=[{"ts": datetime(2026, 9, 25, 9, 0, tzinfo=TZ), "model": "DPRO-66", "raw": 12853}],
        summary=_summary(1),
        events=[],
        alarms_now=convert.resolve_alarms(settings),
        rows_total=1,
        generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=TZ),
        flow_rows=_flow_rows(),
        flow_summary={
            "n": 3,
            "n_empty_pipe": 0,
            "n_reverse": 0,
            "n_alarm": 0,
            "flow_avg": Decimal("211.16"),
            "flow_min": Decimal("211.16"),
            "flow_max": Decimal("211.16"),
            "velocity_avg": Decimal("3.319"),
            "velocity_max": Decimal("3.319"),
            "percent_avg": Decimal("65.98"),
            "empty_pipe_avg": Decimal("0"),
            "total_first": 5000,
            "total_last": 5000,
        },
    )
    text = _all_text(payload)
    assert "流量（电磁流量计 FMC400）" in text
    assert "211.16" in text
    assert "65.98" in text


def test_班次报表有流量时多建流量明细表() -> None:
    """流量明细表只在真有流量行时创建：老班次（无流量）保持两张表的结构。"""
    from io import BytesIO

    from openpyxl import load_workbook

    settings = get_settings()
    rows = [{"ts": datetime(2026, 9, 25, 9, 0, tzinfo=TZ), "model": "DPRO-66", "raw": 12853}]

    def build(flow_rows: list[dict]) -> bytes:
        return report.build_shift_workbook(
            settings=settings,
            shift=shifts.Shift(shifts.KIND_DAY, date(2026, 9, 25)),
            tz=TZ,
            rows=rows,
            summary=_summary(1),
            events=[],
            alarms_now=convert.resolve_alarms(settings),
            rows_total=1,
            generated_at=datetime(2026, 9, 25, 12, 0, tzinfo=TZ),
            flow_rows=flow_rows,
        )

    with_flow = load_workbook(BytesIO(build(_flow_rows())))
    assert with_flow.sheetnames == ["班次报表", "读数明细", "流量明细"]
    detail = with_flow["流量明细"]
    assert detail.cell(row=1, column=3).value == "瞬时流量(m³/h)"
    assert detail.cell(row=2, column=3).value == pytest.approx(211.16, abs=0.01)
    assert detail.cell(row=2, column=9).value == "正常"

    without_flow = load_workbook(BytesIO(build([])))
    assert without_flow.sheetnames == ["班次报表", "读数明细"]

# ==================================================================== 综合干矿量：报表整合


def test_班次报表含综合干矿量段与干矿量明细() -> None:
    """浓度与流量能配上对时：Sheet1 多「综合干矿量」段，并另建干矿量明细表。"""
    payload = _workbook_bytes(_rows(5), flow_rows=_flow_rows())
    wb = _load(payload)

    assert wb.sheetnames == ["班次报表", "读数明细", "流量明细", "干矿量明细"]
    text = _all_text(payload)
    assert "具体干矿量（浓度 × 流量逐点配对）" in text
    assert "Q = V / (1/ρp + (1−C)/C)" in text
    assert "本班次累计" in text
    assert "配对数" in text
    # 三张图：流量趋势、浓度趋势、干矿量趋势
    assert _media(payload) == ["xl/media/image1.png", "xl/media/image2.png", "xl/media/image3.png"]

    detail = wb["干矿量明细"]
    assert [detail.cell(row=1, column=i).value for i in range(1, 9)] == [
        "序号",
        "浓度时刻",
        "浓度(%)",
        "配对流量时刻",
        "瞬时流量(m³/h)",
        "流速(m/s)",
        "传输延时(s)",
        "瞬时干矿量(t/h)",
    ]
    assert detail.freeze_panes == "A2"
    assert detail.auto_filter.ref == "A1:H6"


def test_配不上对时给出原因而不是硬造数字() -> None:
    """浓度在 09:00、流量在 08:00–08:20（差 >90 s）⇒ 明确写「无法计算」。"""
    payload = _workbook_bytes(
        [{"ts": datetime(2026, 9, 25, 9, 0, tzinfo=TZ), "model": "DPRO-66", "raw": 12853}],
        flow_rows=_flow_rows(),
    )
    text = _all_text(payload)
    assert "本班次无法计算综合干矿量" in text
    assert "有效配对仅 0 点" in text
    assert "干矿量明细" not in _load(payload).sheetnames


def test_关闭明细会去掉全部明细表() -> None:
    """先生 2026-11-06：`detail=false` 时读数/流量/干矿量明细都不给，只留管理视图。"""
    payload = _workbook_bytes(_rows(5), flow_rows=_flow_rows(), with_detail=False)
    wb = _load(payload)
    assert wb.sheetnames == ["班次报表"]
    text = _all_text(payload)
    assert "具体干矿量（浓度 × 流量逐点配对）" in text
    assert len(_media(payload)) == 3  # 三张趋势图仍保留


def test_报表段落顺序符合管理视图() -> None:
    """先生 2026-11-06 定的顺序：综合汇总 → 干矿量 → 浓度 → 流量 → 区间统计 → 报警事件。"""
    payload = _workbook_bytes(_rows(5), flow_rows=_flow_rows())
    ws = _load(payload)["班次报表"]
    heads = [str(c.value) for c in ws["A"] if c.value]
    order = ["综合汇总", "具体干矿量", "具体浓度", "具体流量", "区间统计", "报警事件"]
    positions = [next(i for i, s in enumerate(heads) if s.startswith(prefix)) for prefix in order]
    assert positions == sorted(positions), f"段落顺序不对：{heads}"
    assert "班末读数" not in " ".join(heads), "班末读数段应已删除"


# ==================================================================== 概述（汇报版）


def test_概述版只给四项指标与三张趋势图() -> None:
    """先生 2026-11-06：概述版标题不带型号，只要时间/班次/四项指标/三张趋势图。"""
    payload = _workbook_bytes(_rows(5), flow_rows=_flow_rows(3), brief=True, with_detail=False)
    wb = _load(payload)

    assert wb.sheetnames == ["概述"]
    assert len(_media(payload)) == 3, "干矿量 / 浓度 / 流量 三张趋势图都要有"
    text = _all_text(payload)
    assert "ECAS 班次综合报表" in text
    assert "2026-09-25 08:00 ~ 2026-09-25 20:00" in text
    assert "2026-09-25 白班" in text
    assert "综合干矿量" in text and "本班次累计" in text
    assert "平均浓度" in text and "平均流量" in text
    assert "DPRO-66" not in text, "概述版标题与正文都不应出现具体型号"
    for absent in ("区间统计", "报警事件", "具体浓度", "具体流量", "读数明细", "流量明细", "干矿量明细"):
        assert absent not in text, f"概述版不该出现「{absent}」"


def test_概述版也可以附带三张明细表() -> None:
    """先生 2026-11-06：概述版同样允许勾选「包含数据明细」。"""
    payload = _workbook_bytes(_rows(5), flow_rows=_flow_rows(3), brief=True, with_detail=True)
    wb = _load(payload)

    assert wb.sheetnames == ["概述", "读数明细", "流量明细", "干矿量明细"]
    assert len(_media(payload)) == 3, "概述页仍要有三张趋势图"
    text = _all_text(payload)
    assert "ECAS 班次综合报表" in text
    for absent in ("区间统计", "报警事件", "具体浓度", "具体流量"):
        assert absent not in text, f"概述版不该出现「{absent}」"


def test_概述版时段报表用时段称谓() -> None:
    settings = get_settings()
    payload = report.build_window_workbook(
        settings=settings,
        start=datetime(2026, 9, 25, 8, 0, tzinfo=TZ),
        end=datetime(2026, 9, 25, 12, 30, tzinfo=TZ),
        tz=TZ,
        rows=_rows(5),
        summary=_summary(5),
        events=[],
        alarms_now=convert.resolve_alarms(settings),
        rows_total=5,
        generated_at=datetime(2026, 9, 25, 12, 30, tzinfo=TZ),
        flow_rows=_flow_rows(3),
        brief=True,
        with_detail=False,
    )
    assert _load(payload).sheetnames == ["概述"]
    text = _all_text(payload)
    assert "ECAS 时段综合报表" in text
    assert "本时段累计" in text
    assert "2026-09-25 08:00 ~ 2026-09-25 12:30" in text


# ==================================================================== 自定义时段导出


@needs_db
def test_自定义时段导出_起止精确到分钟(client: TestClient, with_token: str) -> None:
    """先生 2026-11-06 要的「导出指定时间点」：起止都到分钟的自定义时段报表。"""
    from openpyxl import load_workbook

    resp = client.get(
        "/api/export/window"
        f"?start=2026-09-25T20:00:00%2B08:00&end=2026-09-25T21:00:00%2B08:00&token={with_token}"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["content-disposition"] == (
        'attachment; filename="ecas_DPRO-66_window_20260925-2000_20260925-2100.xlsx"'
    )

    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames[0] == "时段报表"
    text = _all_text(resp.content)
    assert "ECAS 浓度计时段报表" in text
    assert "时段起止" in text
    assert "具体干矿量（浓度 × 流量逐点配对）" in text


@needs_db
def test_自定义时段导出_概述版(client: TestClient, with_token: str) -> None:
    from openpyxl import load_workbook

    resp = client.get(
        "/api/export/window"
        f"?start=2026-09-25T20:00:00%2B08:00&end=2026-09-25T21:00:00%2B08:00"
        f"&brief=true&detail=false&token={with_token}"
    )
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["概述"]
    text = _all_text(resp.content)
    assert "ECAS 时段综合报表" in text
    assert "本时段累计" in text


@needs_db
def test_自定义时段导出可关掉读数明细(client: TestClient, with_token: str) -> None:
    from openpyxl import load_workbook

    resp = client.get(
        "/api/export/window"
        f"?start=2026-09-25T20:00:00%2B08:00&end=2026-09-25T21:00:00%2B08:00"
        f"&detail=false&token={with_token}"
    )
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.content))
    assert "读数明细" not in wb.sheetnames


@needs_db
def test_自定义时段_统计与_summary_同窗口一致(client: TestClient, with_token: str) -> None:
    """时段报表的「区间统计」必须与看板 `/api/summary` 对得上（同一段 SQL）。"""
    from openpyxl import load_workbook

    start = "2026-09-25T20:00:00%2B08:00"
    end = "2026-09-25T22:00:00%2B08:00"
    resp = client.get(f"/api/export/window?start={start}&end={end}&token={with_token}")
    assert resp.status_code == 200

    summary = client.get(f"/api/summary?start={start}&end={end}&token={with_token}").json()
    ws = load_workbook(io.BytesIO(resp.content))["时段报表"]
    stats: dict[str, str] = {}
    for row in ws.iter_rows(min_col=2, max_col=3, values_only=True):
        metric, value = row
        if metric and value is not None:
            stats[str(metric)] = str(value)
    assert stats["有效样本"].startswith(str(summary["n_valid"]))
    assert stats["原始最低"] == str(summary["raw_min"])
    assert stats["原始最高"] == str(summary["raw_max"])


@needs_db
def test_自定义时段_起止非法给_400(client: TestClient, with_token: str) -> None:
    bad_order = client.get(
        "/api/export/window"
        f"?start=2026-09-25T21:00:00%2B08:00&end=2026-09-25T20:00:00%2B08:00&token={with_token}"
    )
    assert bad_order.status_code == 400
    assert "起点必须早于终点" in bad_order.json()["detail"]

    too_long = client.get(
        "/api/export/window"
        f"?start=2026-09-01T00:00:00%2B08:00&end=2026-10-02T00:00:00%2B08:00&token={with_token}"
    )
    assert too_long.status_code == 400
    assert "最长 720 小时" in too_long.json()["detail"]


@needs_db
def test_自定义时段_需要令牌(client: TestClient, with_token: str) -> None:
    assert (
        client.get("/api/export/window?start=2026-09-25T20:00:00%2B08:00&end=2026-09-25T21:00:00%2B08:00")
        .status_code
        == 401
    )
