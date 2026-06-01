"""Small logging setup helper for the app runtime."""

from __future__ import annotations

import logging
from pathlib import Path


def setup_logger(log_dir: str, level: str = "INFO", logger_name: str = "phenohive") -> logging.Logger:
    """Create and configure the application logger."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(Path(log_dir) / "phenohive.log")
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
