"""logging_setup.py — rolling log file + console output for the macro."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def build_logger(config: dict) -> logging.Logger:
    log_cfg = config["logging"]
    log_dir = Path(log_cfg.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_cfg.get("file_name", "macro.log")

    logger = logging.getLogger("summon_heroes_macro")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=log_cfg.get("max_bytes", 2_000_000),
        backupCount=log_cfg.get("backup_count", 3),
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger
