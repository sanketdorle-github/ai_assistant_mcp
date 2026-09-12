"""Centralized logging configuration.

Previously, logging was configured as an import-time side effect inside
`core/middleware.py` via a bare `logging.basicConfig(level=logging.INFO,
format=...)` call with no `handlers`/`filename` argument. `basicConfig()`
with no handlers attaches only a `StreamHandler` (stdout/stderr) to the
root logger - that's why logs were visible in the console but never
written anywhere on disk, and why there was no rotation or persistence
across restarts.

This module replaces that with an explicit setup, called once from
`main.py` at startup, that attaches BOTH:
  - a console handler (so `docker logs` / your terminal still show output)
  - a rotating file handler (so logs actually persist to disk)

Being import-time-side-effect-free and centralized also avoids the
fragile ordering problem where logging only worked because `middleware.py`
happened to get imported early enough to win the "first basicConfig() call
wins" race against uvicorn's own logging setup.
"""

import logging
import logging.handlers
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Where log files are written. Override via LOG_DIR env var (e.g. to point
# at a mounted volume in Docker). Defaults to ./logs relative to the
# working directory the app is started from.
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "mcp_orchestration.log"

# Root/app log level. Override via LOG_LEVEL env var (DEBUG/INFO/WARNING/...).
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Keep noisy third-party libraries at a higher level than the app's own
# logger so app logs aren't drowned out. Override individually via env
# vars if you need to debug one of them.
_THIRD_PARTY_LEVELS = {
    "httpx": os.getenv("LOG_LEVEL_HTTPX", "WARNING"),
    "httpx2": os.getenv("LOG_LEVEL_HTTPX", "WARNING"),
    "pymongo": os.getenv("LOG_LEVEL_PYMONGO", "WARNING"),
    "uvicorn.access": os.getenv("LOG_LEVEL_UVICORN_ACCESS", "INFO"),
}

_configured = False


def configure_logging() -> None:
    """Attach console + rotating-file handlers to the root logger.

    Idempotent - safe to call more than once (e.g. once from `main.py` and
    once from a test fixture); only configures on the first call.
    """
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 10 MB per file, keep 5 rotated backups, so logs persist across
    # restarts without growing unbounded.
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    for logger_name, level in _THIRD_PARTY_LEVELS.items():
        logging.getLogger(logger_name).setLevel(level)

    logging.getLogger("mcp_orchestration.core.logging_config").info(
        f"Logging configured: level={LOG_LEVEL}, file={LOG_FILE.resolve()}"
    )

    _configured = True
