"""
Configuration models for threshold-engine.

All exchange/platform-specific knowledge (sessions, events, calendar, metrics)
lives in the user's config YAML — nothing domain-specific is hardcoded here.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import pytz
import yaml
from pydantic import BaseModel

# Short day names used in YAML config
DAY_NAMES: dict[str, int] = {
    "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3,
    "Fri": 4, "Sat": 5, "Sun": 6,
}


def _parse_step_seconds(step: str) -> int:
    step = step.strip()
    if step.endswith("s"):
        return int(step[:-1])
    if step.endswith("m"):
        return int(step[:-1]) * 60
    if step.endswith("h"):
        return int(step[:-1]) * 3600
    return int(step)


class PrometheusConfig(BaseModel):
    url: str
    remote_write_url: str
    step: str = "60s"
    lookback_days: int = 90

    @property
    def step_seconds(self) -> int:
        return _parse_step_seconds(self.step)


class EngineConfig(BaseModel):
    update_interval_minutes: int = 60
    prediction_horizon_minutes: int = 30
    anomaly_multiplier: float = 1.5
    min_training_points: int = 1000
    output_metric_prefix: str = "te"


class WindowConfig(BaseModel):
    """A named time window that recurs on specific weekdays."""

    name: str
    days: list[str]   # e.g. ["Mon", "Tue", "Wed", "Thu", "Fri"]
    start: str        # "HH:MM" 24-hour, local to timezone
    end: str          # "HH:MM" — may be earlier than start (midnight-crossing)
    timezone: str = "UTC"

    @property
    def weekdays(self) -> set[int]:
        return {DAY_NAMES[d] for d in self.days}

    @property
    def crosses_midnight(self) -> bool:
        sh, sm = map(int, self.start.split(":"))
        eh, em = map(int, self.end.split(":"))
        return (eh * 60 + em) < (sh * 60 + sm)

    @property
    def length_minutes(self) -> int:
        sh, sm = map(int, self.start.split(":"))
        eh, em = map(int, self.end.split(":"))
        start_m = sh * 60 + sm
        end_m = eh * 60 + em
        if end_m <= start_m:
            end_m += 24 * 60
        return end_m - start_m

    def contains(self, ts: datetime) -> bool:
        """Return True if ts falls within this window."""
        tz = pytz.timezone(self.timezone)
        local = ts.astimezone(tz)

        if local.weekday() not in self.weekdays:
            return False

        t = local.time()
        sh, sm = map(int, self.start.split(":"))
        eh, em = map(int, self.end.split(":"))
        start = time(sh, sm)
        end = time(eh, em)

        if not self.crosses_midnight:
            return start <= t < end
        else:
            # Overnight: active from start until midnight AND from midnight until end
            return t >= start or t < end

    def relative_minute(self, ts: datetime) -> int:
        """Minutes elapsed since window start for a timestamp within this window."""
        tz = pytz.timezone(self.timezone)
        local = ts.astimezone(tz)

        sh, sm = map(int, self.start.split(":"))
        session_start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)

        # For midnight-crossing sessions, if current time is before start time,
        # the session began on the previous calendar day.
        if self.crosses_midnight and local.time() < time(sh, sm):
            session_start -= timedelta(days=1)

        return int((local - session_start).total_seconds() / 60)


# Both sessions and events share the same shape.
SessionConfig = WindowConfig
EventConfig = WindowConfig


class HalfDay(BaseModel):
    date: date
    close_time: str


class CalendarConfig(BaseModel):
    holidays: list[date] = []
    half_days: list[HalfDay] = []

    def holiday_set(self) -> set[date]:
        return set(self.holidays)


class MetricConfig(BaseModel):
    name: str
    query: str
    unit: str


class Config(BaseModel):
    prometheus: PrometheusConfig
    engine: EngineConfig
    sessions: list[SessionConfig]
    events: list[EventConfig] = []
    calendar: CalendarConfig = CalendarConfig()
    metrics: list[MetricConfig]

    @classmethod
    def from_yaml(cls, path: str) -> Config:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
