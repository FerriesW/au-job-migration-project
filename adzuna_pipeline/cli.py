"""Shared entry-point plumbing for the pipeline's command-line scripts.

Every script under `scripts/` had its own copy of the same three things: locate
the project root, load `.env` before importing anything that reads settings,
and configure logging while muzzling the noisy third-party loggers. Six copies
of the logging setup had already drifted apart in which loggers they silenced.

Keeping it here means a change — a new noisy dependency, a different log
format — is made once. The env-loading half has to stay inline in each script,
because it must run before the settings imports do.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Libraries that log at INFO on every request. httpx is the important one: for
# Adzuna the request URL carries the application key as a query parameter, so
# leaving it at INFO writes credentials into stdout and any log file.
NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "google.auth",
    "google.api_core",
    "urllib3",
    "botocore",
    "boto3",
    "snowflake.connector",
)

LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Set the root log level and cap the third-party loggers at WARNING."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
