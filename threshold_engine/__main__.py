"""
Entry point: python -m threshold_engine --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .scheduler import run


def main() -> None:
    parser = argparse.ArgumentParser(
        description="threshold-engine: session-aware dynamic alerting thresholds",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to configuration YAML (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )

    try:
        config = Config.from_yaml(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: failed to load config: {exc}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(config))


if __name__ == "__main__":
    main()
