from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import config

LOG_DIR = Path(__file__).parent.parent / "log"
LOG_FILE = LOG_DIR / "pipeline.log"
BACKUP_DAYS = config.LOG_BACKUP_DAYS


def configure_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    logging.captureWarnings(True)
