"""Ротация журналов и единая настройка логирования."""

import logging
from logging.handlers import RotatingFileHandler

from valutatrade_hub.infra.settings import SettingsLoader


def get_logger(name="actions"):
    settings = SettingsLoader()
    directory = settings.get("LOG_DIR")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.log"
    logger = logging.getLogger(f"valutatrade.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers or any(
        getattr(item, "baseFilename", None) != str(path) for item in logger.handlers
    ):
        for item in logger.handlers[:]:
            logger.removeHandler(item)
            item.close()
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(settings.get("LOG_FORMAT")))
        logger.addHandler(handler)
    return logger


def configure_logging():
    """Настроить оба журнала при запуске CLI или парсера."""
    get_logger("actions")
    get_logger("parser")
