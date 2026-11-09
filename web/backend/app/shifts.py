"""班次（白班/夜班）的时间模型：定义、切分与命名。

口径照用户定的来：**白班 08:00–20:00，夜班 20:00–次日 08:00**，各 12 小时，
首尾相接、无缝无重叠。一个班次归属「它开始的那一天」—— 所以「2026-09-25 夜班」
指的是 09-25 20:00 到 09-26 08:00，而不是 09-24 那天夜里。

这个模块只管时间，不碰数据库、不碰 Excel：报表和接口都从这里取同一种班次定义，
避免两处各切一套边界。班次边界一律是**带时区**的 datetime —— 库里 `ts` 是
`timestamptz`，裸 datetime 传参会被当成服务器时区，那是隐性 bug。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

#: 班次边界的小时数（用户口径，勿随意改）
DAY_START_HOUR = 8
DAY_END_HOUR = 20
NIGHT_START_HOUR = 20
NIGHT_END_HOUR = 8

KIND_DAY = "day"
KIND_NIGHT = "night"
KIND_ORDER: tuple[str, ...] = (KIND_DAY, KIND_NIGHT)
KIND_LABELS: dict[str, str] = {KIND_DAY: "白班", KIND_NIGHT: "夜班"}

#: 一次导出最多允许的班次数量（62 = 31 天 × 2 班，约一个月的班次报表）
MAX_SHIFTS = 62

#: 一次导出最多允许跨越的自然日数（含首尾）
MAX_DAYS = 31


class ShiftRangeError(ValueError):
    """班次区间不合法（超出上限、起止颠倒、种类为空）—— 接口层翻成 400。"""


@dataclass(frozen=True, slots=True)
class Shift:
    """一个班次。

    `day` 是班次**开始**的那一天（白班 = 当日；夜班 = 当晚）。
    """

    kind: str
    day: date

    def __post_init__(self) -> None:
        if self.kind not in KIND_LABELS:
            raise ShiftRangeError(f"未知的班次种类：{self.kind}")

    # ---------------------------------------------------------------- 时间

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """返回 `(起点, 终点)`，均为带时区 datetime；终点取 `起点 + 12h` 以避开 DST 陷阱。"""
        if self.kind == KIND_NIGHT:
            start = datetime.combine(self.day, time(NIGHT_START_HOUR), tzinfo=tz)
        else:
            start = datetime.combine(self.day, time(DAY_START_HOUR), tzinfo=tz)
        return start, start + timedelta(hours=12)

    def contains(self, moment: datetime, tz: ZoneInfo) -> bool:
        start, end = self.bounds(tz)
        return start <= moment < end

    def is_complete(self, now: datetime, tz: ZoneInfo) -> bool:
        """班次是否已经结束（导出时刻仍在班中 → False，报表里会标「进行中」）。"""
        return self.bounds(tz)[1] <= now

    # ---------------------------------------------------------------- 命名

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def label(self) -> str:
        return f"{self.day:%Y-%m-%d} {self.kind_label}"

    @property
    def slug(self) -> str:
        """文件名用的标识：`20260925-day` / `20260925-night`（纯 ASCII）。"""
        return f"{self.day:%Y%m%d}-{self.kind}"

    def label_long(self, tz: ZoneInfo) -> str:
        """带起止时刻的长标签，脱离上下文也不会误解是哪 12 小时。"""
        start, end = self.bounds(tz)
        return f"{self.label}（{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}）"


# ------------------------------------------------------------------ 区间切分


def parse_kinds(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """解析 `kinds` 参数：`"day,night"` / `["day"]` / `None`（= 两班都要）。

    容忍逗号、空格、中文「白班/夜班」，前端写成什么都能落地；非法值直接报错，
    不静默吞掉 —— 少导一个班次比报错更难发现。
    """
    if value is None:
        return KIND_ORDER
    raw = value.split(",") if isinstance(value, str) else list(value)
    alias = {"白班": KIND_DAY, "夜班": KIND_NIGHT, "day": KIND_DAY, "night": KIND_NIGHT}
    picked: list[str] = []
    for item in raw:
        token = str(item).strip()
        if not token:
            continue
        kind = alias.get(token) or alias.get(token.lower())
        if kind is None:
            raise ShiftRangeError(f"未知的班次种类：{item!r}（可用 day / night / 白班 / 夜班）")
        if kind not in picked:
            picked.append(kind)
    if not picked:
        raise ShiftRangeError("至少要选一种班次（白班 / 夜班）")
    return tuple(sorted(picked, key=KIND_ORDER.index))


def shifts_in_range(
    start: date,
    end: date,
    kinds: Sequence[str] = KIND_ORDER,
) -> list[Shift]:
    """列出 `[start, end]` 闭区间内全部班次，按时间先后排序（白班在前、夜班在后）。

    起止颠倒会自动纠正 —— 用户在两个日期框里填反是常事，没必要为此报错。
    """
    if end < start:
        start, end = end, start
    ordered = tuple(sorted(set(kinds), key=KIND_ORDER.index))
    out: list[Shift] = []
    day = start
    while day <= end:
        for kind in ordered:
            out.append(Shift(kind, day))
        day += timedelta(days=1)
    return out


def validate_range(start: date, end: date, kinds: Sequence[str]) -> list[Shift]:
    """切分班次并校验上限；不合法抛 `ShiftRangeError`（接口层 → HTTP 400）。"""
    if end < start:
        start, end = end, start
    span_days = (end - start).days + 1
    if span_days > MAX_DAYS:
        raise ShiftRangeError(f"区间跨度 {span_days} 天，超过上限 {MAX_DAYS} 天")
    shifts = shifts_in_range(start, end, kinds)
    if not shifts:
        raise ShiftRangeError("该区间没有可导出的班次")
    if len(shifts) > MAX_SHIFTS:
        raise ShiftRangeError(f"共 {len(shifts)} 个班次，超过单次上限 {MAX_SHIFTS} 个")
    return shifts


def current_shift(now: datetime, tz: ZoneInfo) -> Shift:
    """`now` 落在哪个班次里（用来给界面预填一个合理的默认日期）。"""
    local = now.astimezone(tz)
    if DAY_START_HOUR <= local.hour < DAY_END_HOUR:
        return Shift(KIND_DAY, local.date())
    # 00:00–08:00 属于「前一天开始的夜班」
    day = local.date() if local.hour >= NIGHT_START_HOUR else local.date() - timedelta(days=1)
    return Shift(KIND_NIGHT, day)


def describe(shift: Shift, tz: ZoneInfo) -> dict[str, Any]:
    """给接口/前端用的班次摘要（不含数据）。

    ⚠️ 这里**故意不给 `filename`**：真实文件名是 `report.shift_filename()` 拼的，
    带型号前缀（`ecas_DPRO-66_20260925-night.xlsx`），而型号只有 `settings` 知道。
    本模块只管时间、不碰 Excel，让接口层去补那个字段 —— 否则预览报的名字
    跟真正下下来的文件不是同一个，zip 里也没法对账。
    """
    start, end = shift.bounds(tz)
    return {
        "kind": shift.kind,
        "kind_label": shift.kind_label,
        "day": shift.day.isoformat(),
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "label": shift.label,
        "label_long": shift.label_long(tz),
        "slug": shift.slug,
    }
