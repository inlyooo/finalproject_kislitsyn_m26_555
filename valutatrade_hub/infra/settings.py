"""Единые настройки: pyproject.toml, затем переменные окружения."""

import os
import tomllib
from pathlib import Path


class SettingsLoader:
    """Singleton: все модули используют один объект настроек."""

    _instance = None
    DEFAULTS = {
        "DATA_DIR": "data",
        "LOG_DIR": "logs",
        "RATES_TTL_SECONDS": 86400,
        "BASE_CURRENCY": "USD",
        "REQUEST_TIMEOUT": 10,
        "LOG_FORMAT": "%(asctime)s %(levelname)s %(message)s",
    }

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            instance.reload()
            cls._instance = instance
        return cls._instance

    def reload(self):
        """Перечитать настройки; полезно при изменении окружения и в тестах."""
        values = self.DEFAULTS.copy()
        config_path = Path(os.getenv("VALUTATRADE_CONFIG", "pyproject.toml"))
        if config_path.is_file():
            with config_path.open("rb") as file:
                document = tomllib.load(file)
            values.update(document.get("tool", {}).get("valutatrade", {}))
        for key in self.DEFAULTS:
            override = os.getenv(f"VALUTATRADE_{key}")
            if override is not None:
                values[key] = override
        for key in ("RATES_TTL_SECONDS", "REQUEST_TIMEOUT"):
            raw = values[key]
            if isinstance(raw, bool):
                raise ValueError(f"Настройка {key} должна быть положительным целым числом")
            try:
                value = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Настройка {key} должна быть целым числом") from error
            if value <= 0 or (isinstance(raw, float) and raw != value):
                raise ValueError(f"Настройка {key} должна быть положительным целым числом")
            values[key] = value
        for key in ("DATA_DIR", "LOG_DIR"):
            values[key] = Path(values[key]).expanduser().resolve()
        if values["BASE_CURRENCY"] != "USD":
            raise ValueError("Расчётная валюта торгового кошелька — USD")
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)
