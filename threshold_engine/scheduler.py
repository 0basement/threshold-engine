"""
Periodic scheduler — runs the engine cycle at the configured interval.
"""
from __future__ import annotations

import asyncio
import logging

from .config import Config
from .engine import ThresholdEngine
from .prometheus import PrometheusClient

logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    client = PrometheusClient(config.prometheus)
    engine = ThresholdEngine(config, client)
    interval_seconds = config.engine.update_interval_minutes * 60

    logger.info(
        "Starting threshold-engine — %d metric(s), %d session(s), cycle every %dm",
        len(config.metrics),
        len(config.sessions),
        config.engine.update_interval_minutes,
    )

    try:
        while True:
            try:
                await engine.run_cycle()
            except Exception:
                logger.exception("Cycle failed — will retry in %ds", interval_seconds)
            await asyncio.sleep(interval_seconds)
    finally:
        await client.close()
