"""Diegetic business calendar: world time as dates, never raw tick numbers.

MASTER_DESIGN P2/P3 (data/design/MASTER_DESIGN.md sections 3, 5-8) require that
world-visible text never carry experimenter-plane machinery -- "tick" is the
kernel's internal half-day counter (see §2 term table: "tick=世界時間の最小刻み
（半日）"), and a blind SME review correctly flagged "第{tick}ティック" strings
in rendered records as a simulation-clock leak (MASTER_DESIGN never says the
world may speak in ticks; §5-8 diegetic principles require conditions -- and
by extension ordinary world facts like "what day is it" -- to be given through
business artifacts, not raw counters).

This module is a pure, deterministic mapping from tick -> business calendar
date/half-day, used only for RENDERING. The experimenter plane (recorder,
ledger, world_config, oracles) keeps tick numbers untouched; only the text a
seat or customer actually reads is rendered through here.

tick=1 is defined as the first half-day of the campaign: 2026-04-01 AM.
Ticks advance one half-day at a time and skip Saturday/Sunday, mirroring an
ordinary sales-floor business calendar (no half-day falls on a weekend).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

CAMPAIGN_START_DATE = date(2026, 4, 1)  # tick 1 = 2026-04-01 AM (business calendar epoch)

_WEEKDAY_LABELS_JA = ("月", "火", "水", "木", "金", "土", "日")
_HALF_DAY_LABELS_JA = ("午前", "午後")


@dataclass(frozen=True)
class WorldDate:
    """A rendered business-calendar instant corresponding to one tick."""

    tick: int
    calendar_date: date
    half_day: str  # "午前" | "午後"

    @property
    def weekday_label(self) -> str:
        return _WEEKDAY_LABELS_JA[self.calendar_date.weekday()]

    @property
    def iso_date(self) -> str:
        return self.calendar_date.isoformat()

    def display(self) -> str:
        """e.g. '2026年4月1日(水)午前'."""
        return f"{self.calendar_date.year}年{self.calendar_date.month}月{self.calendar_date.day}日({self.weekday_label}){self.half_day}"

    def short_display(self) -> str:
        """e.g. '4月1日(水)午前' -- for inline use where the year is redundant."""
        return f"{self.calendar_date.month}月{self.calendar_date.day}日({self.weekday_label}){self.half_day}"

    def date_only(self) -> str:
        """e.g. '2026年4月1日(水)' -- for customer-facing deadlines (no half-day)."""
        return f"{self.calendar_date.year}年{self.calendar_date.month}月{self.calendar_date.day}日({self.weekday_label})"


def _business_days_for_half_day_index(n: int) -> date:
    """Return the calendar date for the n-th business half-day slot (0-indexed),
    skipping Saturday/Sunday. Two half-day slots (AM, PM) share each business day.
    """
    day_index = n // 2
    current = CAMPAIGN_START_DATE
    counted = 0
    while True:
        if current.weekday() < 5:  # Mon-Fri
            if counted == day_index:
                return current
            counted += 1
        current += timedelta(days=1)


def tick_to_world_date(tick: int) -> WorldDate:
    """Map a 1-indexed world tick to its business calendar date/half-day.

    tick=1 -> 2026-04-01 AM, tick=2 -> 2026-04-01 PM, tick=3 -> 2026-04-02 AM
    (2026-04-02 is a Thursday; weekends are skipped automatically), etc.
    Non-positive ticks clamp to tick=1 so defensive callers never render a
    negative or zero date.
    """
    safe_tick = max(int(tick), 1)
    n = safe_tick - 1
    calendar_date = _business_days_for_half_day_index(n)
    half_day = _HALF_DAY_LABELS_JA[n % 2]
    return WorldDate(tick=safe_tick, calendar_date=calendar_date, half_day=half_day)


def render_tick_as_date(tick: int) -> str:
    """Convenience: full diegetic rendering of a tick, e.g. '2026年4月1日(水)午前'."""
    return tick_to_world_date(tick).display()


def render_deadline_date(deadline_tick: int) -> str:
    """Diegetic rendering of a deadline as a calendar date (no half-day
    granularity), e.g. '2026年4月8日(水)' -- used for customer-facing deadline
    phrasing so it reads as a real due date rather than a relative business-day
    count (MASTER_DESIGN P3: conditions must be diegetic, not template-parameter
    phrasing like "約2営業日以内")."""
    return tick_to_world_date(deadline_tick).date_only()
