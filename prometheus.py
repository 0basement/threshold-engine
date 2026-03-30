"""
Prometheus / Mimir HTTP client.

Pulls metric time series via the HTTP API and writes synthetic bound
metrics back via remote_write.
"""
from __future__ import annotations

import logging
import time as time_mod
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pandas as pd

from .config import PrometheusConfig

logger = logging.getLogger(__name__)


class PrometheusClient:
    def __init__(self, cfg: PrometheusConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(timeout=30.0)

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
    ) -> pd.Series:
        """
        Execute a range query and return a pandas Series indexed by UTC datetime.
        Returns an empty Series if the query returns no data.
        """
        params = {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": self._cfg.step,
        }
        url = f"{self._cfg.url}/api/v1/query_range"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("data", {}).get("result", [])
        if not results:
            return pd.Series(dtype=float)

        # Merge all result vectors (handles multi-series queries gracefully)
        frames = []
        for result in results:
            values = result.get("values", [])
            if not values:
                continue
            ts = [datetime.fromtimestamp(v[0], tz=timezone.utc) for v in values]
            vals = [float(v[1]) for v in values]
            frames.append(pd.Series(vals, index=ts))

        if not frames:
            return pd.Series(dtype=float)

        combined = pd.concat(frames).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined

    async def remote_write(self, metrics: list[dict[str, Any]]) -> None:
        """
        Write synthetic metrics back to Mimir via the Prometheus remote_write
        compatible endpoint.

        Each metric dict:
          {
            "name": "te_request_latency_p99_upper",
            "labels": {"session": "continuous"},
            "timestamp_ms": 1234567890000,
            "value": 0.042,
          }

        Uses the simpler JSON push format supported by Mimir and
        Victoria Metrics (/api/v1/import/prometheus).
        Falls back to line protocol if JSON push is unavailable.
        """
        if not metrics:
            return

        lines = []
        for m in metrics:
            label_str = ",".join(
                f'{k}="{v}"' for k, v in sorted(m.get("labels", {}).items())
            )
            name = m["name"]
            if label_str:
                metric_id = f"{name}{{{label_str}}}"
            else:
                metric_id = name
            ts_ms = m.get("timestamp_ms", int(time_mod.time() * 1000))
            lines.append(f"{metric_id} {m['value']} {ts_ms}")

        body = "\n".join(lines)

        # Mimir's Prometheus-compatible push endpoint
        push_url = self._cfg.remote_write_url.rstrip("/")
        if not push_url.endswith("/api/v1/push"):
            push_url = push_url.replace("/api/v1/push", "") + "/api/v1/push"

        try:
            resp = await self._client.post(
                push_url,
                content=body.encode(),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
            logger.debug("remote_write: pushed %d metrics", len(metrics))
        except httpx.HTTPStatusError as exc:
            logger.error("remote_write failed: %s — %s", exc.response.status_code, exc.response.text)

    async def close(self) -> None:
        await self._client.aclose()
