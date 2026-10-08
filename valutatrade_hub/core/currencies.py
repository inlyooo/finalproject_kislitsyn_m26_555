"""Общие сведения о фиатных и цифровых валютах."""

from abc import ABC, abstractmethod
from math import isfinite

from valutatrade_hub.core.exceptions import CurrencyNotFoundError


def _nonempty_text(value, field):
    """Проверить текстовое поле и убрать внешние пробелы."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} не может быть пустым.")
    return value.strip()


def _currency_code(code):
    """Проверить 2–5 заглавных латинских букв кода валюты."""
    if (
        not isinstance(code, str)
        or not 2 <= len(code) <= 5
        or not code.isascii()
        or not code.isalpha()
        or not code.isupper()
    ):
        raise ValueError("Код валюты: от 2 до 5 заглавных латинских букв.")
    return code


class Currency(ABC):
    """Валюта с названием, кодом и способом отображения."""

    def __init__(self, name: str, code: str):
        self.name = _nonempty_text(name, "Название валюты")
        self.code = _currency_code(code)

    @abstractmethod
    def get_display_info(self) -> str:
        """Вернуть краткую информацию для пользователя."""


class FiatCurrency(Currency):
    """Валюта, которую выпускает указанная страна."""

    def __init__(self, name: str, code: str, issuing_country: str):
        super().__init__(name, code)
        self.issuing_country = _nonempty_text(
            issuing_country, "Страна выпуска"
        )

    def get_display_info(self) -> str:
        """Показать название и страну выпуска валюты."""
        return (
            f"[FIAT] {self.code} — {self.name} "
            f"(Issuing: {self.issuing_country})"
        )


class CryptoCurrency(Currency):
    """Криптовалюта с алгоритмом и справочной капитализацией."""

    def __init__(
        self, name: str, code: str, algorithm: str, market_cap: float
    ):
        super().__init__(name, code)
        self.algorithm = _nonempty_text(algorithm, "Алгоритм")
        if (
            isinstance(market_cap, bool)
            or not isinstance(market_cap, (int, float))
            or not isfinite(market_cap)
            or market_cap < 0
        ):
            raise ValueError("Капитализация должна быть конечным числом >= 0.")
        self.market_cap = float(market_cap)

    def get_display_info(self) -> str:
        """Показать название, алгоритм и справочную капитализацию."""
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {self.market_cap:.2e})"
        )


# Капитализация здесь учебная справка, а не текущая рыночная оценка.
_CURRENCIES = {
    "USD": FiatCurrency("US Dollar", "USD", "United States"),
    "EUR": FiatCurrency("Euro", "EUR", "Eurozone"),
    "GBP": FiatCurrency("British Pound", "GBP", "United Kingdom"),
    "RUB": FiatCurrency("Russian Ruble", "RUB", "Russia"),
    "BTC": CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.12e12),
    "ETH": CryptoCurrency("Ethereum", "ETH", "Proof-of-Stake", 4.5e11),
    "SOL": CryptoCurrency("Solana", "SOL", "Proof-of-Stake", 6e10),
}


def get_currency(code) -> Currency:
    """Найти валюту по корректному коду или сообщить об отсутствии."""
    code = _currency_code(code)
    if code not in _CURRENCIES:
        raise CurrencyNotFoundError(code)
    return _CURRENCIES[code]


def list_currencies() -> tuple[Currency, ...]:
    """Вернуть поддерживаемые валюты в порядке реестра."""
    return tuple(_CURRENCIES.values())
