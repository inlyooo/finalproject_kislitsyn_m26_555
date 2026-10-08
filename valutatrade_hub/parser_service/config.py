"""Централизованные настройки источников курсов и путей хранения."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from valutatrade_hub.infra.settings import SettingsLoader

CRYPTO_ID_MAP = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def _setting(name, default):
    """Прочитать общую настройку при создании экземпляра конфигурации."""
    return SettingsLoader().get(name, default)


def _currency_list(variable, default):
    """Прочитать список кодов из переменной окружения."""
    value = os.getenv(variable)
    if value is None:
        return default
    return tuple(code.strip() for code in value.split(",") if code.strip())


@dataclass(slots=True)
class ParserConfig:
    """Настройки API; секреты читаются из окружения и не попадают в repr."""

    data_dir: Path = field(default_factory=lambda: Path(_setting("DATA_DIR", "data")))
    log_dir: Path = field(default_factory=lambda: Path(_setting("LOG_DIR", "logs")))
    exchangerate_api_key: str = field(
        default_factory=lambda: os.getenv("EXCHANGERATE_API_KEY", ""), repr=False
    )
    coingecko_api_key: str = field(
        default_factory=lambda: os.getenv("COINGECKO_API_KEY", ""), repr=False
    )
    coingecko_url: str = field(
        default_factory=lambda: os.getenv(
            "VALUTATRADE_COINGECKO_URL", "https://api.coingecko.com/api/v3/simple/price"
        ),
        repr=False,
    )
    exchangerate_api_url: str = field(
        default_factory=lambda: os.getenv(
            "VALUTATRADE_EXCHANGERATE_API_URL",
            "https://v6.exchangerate-api.com/v6/{api_key}/latest/{base}",
        ),
        repr=False,
    )
    base_currency: str = field(default_factory=lambda: _setting("BASE_CURRENCY", "USD"))
    fiat_currencies: tuple[str, ...] = field(
        default_factory=lambda: _currency_list("VALUTATRADE_FIAT_CURRENCIES", ("EUR", "GBP", "RUB"))
    )
    crypto_currencies: tuple[str, ...] = field(
        default_factory=lambda: _currency_list(
            "VALUTATRADE_CRYPTO_CURRENCIES", tuple(CRYPTO_ID_MAP)
        )
    )
    crypto_id_map: dict[str, str] = field(default_factory=lambda: CRYPTO_ID_MAP.copy())
    request_timeout: int = field(default_factory=lambda: _setting("REQUEST_TIMEOUT", 10))

    def __post_init__(self):
        """Сохранить отдельные копии изменяемых коллекций и нормализовать пути."""
        self.data_dir = Path(self.data_dir)
        self.log_dir = Path(self.log_dir)
        self.fiat_currencies = tuple(self.fiat_currencies)
        self.crypto_currencies = tuple(self.crypto_currencies)
        self.crypto_id_map = dict(self.crypto_id_map)
