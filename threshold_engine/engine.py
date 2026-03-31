"""
Core threshold engine.

For each configured metric and session, trains an empirical baseline model
from historical Prometheus data, then writes session-aware dynamic bounds
back to Mimir so Prometheus alert rules can compare live metrics against them.

Algorithm
---------
For each (metric, session) pair:

1. Pull the full history for the metric over the configured lookback window.
2. Extract only the data points that fall within the session's time windows,
   excluding holidays and half-days.
3. Group data points by their *relative minute* within the session
   (minute 0 = session start, minute N = session end).
4. For each relative minute, compute the mean and standard deviation across
   all historical occurrences.
5. Write synthetic metrics for the current time:
     te_{name}_predicted     — historical mean at the current relative minute
     te_{name}_upper         — mean + (anomaly_multiplier × std)
     te_{name}_lower         — mean - (anomaly_multiplier × std)
     te_{name}_anomaly_score — z-score of the most recent actual value
     te_{name}_model_ok      — 1 if the model has sufficient data, else 0

The engine writes bounds for the session that is active right now.
When no session is active, only the model_ok flags are written.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

from .config import Config, SessionConfig
from .prometheus import PrometheusClient

logger = logging.getLogger(__name__)

# _nearest() will not interpolate across gaps wider than this.
MAX_GAP_MINUTES = 5


@dataclass
class SessionModel:
    session_name: str
    metric_name: str
    # relative_minute -> (mean, raw_std)
    # Multiplier is applied at read time in bounds().
    baseline: dict[int, tuple[float, float]] = field(default_factory=dict)
    trained_at: Optional[datetime] = None
    n_points: int = 0

    @property
    def is_trained(self) -> bool:
        return bool(self.baseline)

    def bounds(self, relative_minute: int, multiplier: float) -> tuple[float, float, float]:
        """
        Return (predicted, upper, lower) for the given relative minute.

        Falls back to the nearest baseline entry within MAX_GAP_MINUTES
        to handle sparse edges of a session window.
        """
        entry = self._nearest(relative_minute)
        if entry is None:
            return 0.0, 0.0, 0.0
        mean, raw_std = entry
        band = raw_std * multiplier
        return mean, mean + band, mean - band

    def z_score(self, relative_minute: int, value: float) -> float:
        """Z-score of value against the historical distribution at relative_minute."""
        entry = self._nearest(relative_minute)
        if entry is None:
            return 0.0
        mean, raw_std = entry
        return (value - mean) / raw_std if raw_std > 0 else 0.0

    def _nearest(self, relative_minute: int) -> Optional[tuple[float, float]]:
        if relative_minute in self.baseline:
            return self.baseline[relative_minute]
        if not self.baseline:
            return None
        nearest = min(self.baseline, key=lambda m: abs(m - relative_minute))
        if abs(nearest - relative_minute) > MAX_GAP_MINUTES:
            return None
        return self.baseline[nearest]


class ThresholdEngine:
    def __init__(self, config: Config, client: PrometheusClient) -> None:
        self._cfg = config
        self._client = client
        # metric_name -> session_name -> model
        self._models: dict[str, dict[str, SessionModel]] = {}

    async def run_cycle(self) -> None:
        """
        Train / refresh models for all (metric, session) pairs and write
        current bounds to Mimir.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self._cfg.prometheus.lookback_days)

        ts_ms = int(now.timestamp() * 1000)
        prefix = self._cfg.engine.output_metric_prefix
        multiplier = self._cfg.engine.anomaly_multiplier

        # Heartbeat — lets ThresholdEngineCycleStale alert fire if we stop running
        await self._client.remote_write([{
            "name": f"{prefix}_engine_last_cycle_timestamp",
            "labels": {},
            "timestamp_ms": ts_ms,
            "value": now.timestamp(),
        }])

        for metric in self._cfg.metrics:
            logger.info("Processing metric: %s", metric.name)

            series = await self._client.query_range(metric.query, start, now)
            if series.empty:
                logger.warning("No data for metric %s — skipping", metric.name)
                continue

            assert isinstance(series.index, pd.DatetimeIndex), (
                f"query_range must return a DatetimeIndex, got {type(series.index)}"
            )

            for session in self._cfg.sessions:
                model = self._build_model(series, session, metric.name)
                self._models.setdefault(metric.name, {})[session.name] = model

                metrics_out = []
                in_session = session.contains(now)

                if in_session and model.is_trained:
                    rel_min = session.relative_minute(now)
                    predicted, upper, lower = model.bounds(rel_min, multiplier)

                    metrics_out.extend([
                        _m(f"{prefix}_{metric.name}_predicted", session.name, predicted, ts_ms),
                        _m(f"{prefix}_{metric.name}_upper",     session.name, upper,     ts_ms),
                        _m(f"{prefix}_{metric.name}_lower",     session.name, lower,     ts_ms),
                    ])

                    # Anomaly score from the most recent actual sample
                    recent = series.loc[:now]
                    if not recent.empty:
                        current_val = float(recent.iloc[-1])
                        score = model.z_score(rel_min, current_val)
                        metrics_out.append(
                            _m(f"{prefix}_{metric.name}_anomaly_score", session.name, score, ts_ms)
                        )

                metrics_out.append(
                    _m(f"{prefix}_{metric.name}_model_ok", session.name,
                       1.0 if model.is_trained else 0.0, ts_ms)
                )

                await self._client.remote_write(metrics_out)
                logger.debug(
                    "Wrote %d metrics for %s/%s (trained=%s, in_session=%s)",
                    len(metrics_out), metric.name, session.name,
                    model.is_trained, in_session,
                )

        logger.info("Cycle complete — %s", now.isoformat())

    def _build_model(
        self,
        series: pd.Series,
        session: SessionConfig,
        metric_name: str,
    ) -> SessionModel:
        """
        Build an empirical per-minute baseline for one (metric, session) pair.
        """
        model = SessionModel(
            session_name=session.name,
            metric_name=metric_name,
        )

        holiday_dates = self._cfg.calendar.holiday_set()

        # Bucket historical values by relative minute within the session
        buckets: dict[int, list[float]] = {}

        for ts, val in series.items():
            if ts.date() in holiday_dates:
                continue
            if not session.contains(ts):
                continue

            rel_min = session.relative_minute(ts)
            if rel_min < 0 or rel_min > session.length_minutes:
                continue

            buckets.setdefault(rel_min, []).append(float(val))

        total_points = sum(len(v) for v in buckets.values())
        model.n_points = total_points

        if total_points < self._cfg.engine.min_training_points:
            logger.warning(
                "Insufficient data for %s/%s: %d points (need %d)",
                metric_name, session.name,
                total_points, self._cfg.engine.min_training_points,
            )
            return model

        for rel_min, values in buckets.items():
            arr = np.array(values, dtype=float)
            model.baseline[rel_min] = (float(np.mean(arr)), float(np.std(arr)))

        model.trained_at = datetime.now(timezone.utc)
        logger.debug(
            "Trained %s/%s: %d minute buckets, %d total points",
            metric_name, session.name, len(model.baseline), total_points,
        )
        return model


def _m(name: str, session: str, value: float, ts_ms: int) -> dict:
    return {"name": name, "labels": {"session": session}, "timestamp_ms": ts_ms, "value": value}
