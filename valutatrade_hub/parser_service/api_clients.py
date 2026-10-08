"""Проверяемые адаптеры CoinGecko и ExchangeRate-API без вымышленных курсов."""

import math
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import quote

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import to_iso, utc_now, validate_currency_code
from valutatrade_hub.parser_service.config import ParserConfig


def _positive_number(value, description):
    """Принять только конечное положительное JSON-число; bool не является курсом."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiRequestError(f"{description}: требуется положительное число")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ApiRequestError(f"{description}: некорректное число") from None
    if not math.isfinite(result) or result <= 0:
        raise ApiRequestError(f"{description}: требуется конечное положительное число")
    return result


def _source_timestamp(value, source):
    """Преобразовать Unix-время источника в UTC ISO; время запроса не подставлять."""
    seconds = _positive_number(value, f"{source}: время обновления источника")
    try:
        timestamp = datetime.fromtimestamp(seconds, UTC)
    except (ValueError, OSError, OverflowError):
        raise ApiRequestError(f"{source}: некорректное время обновления") from None
    if timestamp > utc_now():
        raise ApiRequestError(f"{source}: время обновления находится в будущем")
    return to_iso(timestamp)


class BaseApiClient(ABC):
    """Единый контракт курсов FROM→TO и метаданных времени источника."""

    source = "API"

    def __init__(self, config=None):
        """Подготовить конфигурацию и отдельный словарь метаданных ответа."""
        self.config = config if config is not None else ParserConfig()
        self.metadata = {}

    @abstractmethod
    def fetch_rates(self) -> dict[str, float]:
        """Вернуть курсы и заполнить metadata только после полной валидации ответа."""

    def _request_json(self, url, *, params=None, headers=None):
        """Выполнить запрос без вывода URL, ответа или текста исключения с ключом."""
        try:
            timeout = _positive_number(self.config.request_timeout, "Таймаут запроса")
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.Timeout:
            raise ApiRequestError(f"{self.source}: превышено время ожидания ответа") from None
        except requests.RequestException:
            raise ApiRequestError(f"{self.source}: не удалось соединиться с API") from None
        status = response.status_code
        if status == 429:
            raise ApiRequestError(f"{self.source}: лимит запросов (HTTP 429)")
        if status in (401, 403):
            raise ApiRequestError(
                f"{self.source}: доступ отклонён (HTTP {status}); проверьте API-ключ"
            )
        if not 200 <= status < 300:
            raise ApiRequestError(f"{self.source}: ошибка сервера (HTTP {status})")
        try:
            payload = response.json()
        except ValueError:
            raise ApiRequestError(f"{self.source}: API вернул некорректный JSON") from None
        if not isinstance(payload, dict):
            raise ApiRequestError(f"{self.source}: API вернул неожиданный формат данных")
        return payload


class CoinGeckoClient(BaseApiClient):
    """Получать криптовалютные цены через /simple/price и CoinGecko IDs."""

    source = "CoinGecko"

    def fetch_rates(self) -> dict[str, float]:
        """Вернуть цены криптовалют в базовой валюте с last_updated_at источника."""
        self.metadata = {}
        base = validate_currency_code(self.config.base_currency)
        currencies = tuple(validate_currency_code(code) for code in self.config.crypto_currencies)
        if not currencies:
            raise ApiRequestError("CoinGecko: список криптовалют пуст")
        try:
            ids = [self.config.crypto_id_map[code] for code in currencies]
        except KeyError:
            raise ApiRequestError("CoinGecko: отсутствует CoinGecko ID для валюты") from None
        if any(not isinstance(coin_id, str) or not coin_id for coin_id in ids):
            raise ApiRequestError("CoinGecko: некорректный CoinGecko ID")
        headers = {}
        if self.config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self.config.coingecko_api_key
        payload = self._request_json(
            self.config.coingecko_url,
            params={
                "ids": ",".join(ids),
                "vs_currencies": base.lower(),
                "include_last_updated_at": "true",
            },
            headers=headers,
        )
        rates, metadata = {}, {}
        for code, coin_id in zip(currencies, ids, strict=True):
            record = payload.get(coin_id)
            if not isinstance(record, dict):
                raise ApiRequestError(f"CoinGecko: в ответе нет курса {code}")
            pair = f"{code}_{base}"
            rates[pair] = _positive_number(record.get(base.lower()), f"CoinGecko: курс {pair}")
            metadata[pair] = {
                "timestamp": _source_timestamp(record.get("last_updated_at"), self.source),
                "meta": {"coin_id": coin_id, "base_currency": base},
            }
        self.metadata = metadata
        return rates


class ExchangeRateApiClient(BaseApiClient):
    """Получать фиатные курсы Standard API и приводить их к CURRENCY→BASE."""

    source = "ExchangeRate-API"

    def fetch_rates(self) -> dict[str, float]:
        """Инвертировать conversion_rates, сохранив время time_last_update_unix."""
        self.metadata = {}
        key = self.config.exchangerate_api_key
        if not key:
            raise ApiRequestError("ExchangeRate-API: задайте EXCHANGERATE_API_KEY в окружении")
        base = validate_currency_code(self.config.base_currency)
        try:
            url = self.config.exchangerate_api_url.format(api_key=quote(key, safe=""), base=base)
        except (KeyError, ValueError):
            raise ApiRequestError(
                "ExchangeRate-API: некорректный шаблон URL в конфигурации"
            ) from None
        payload = self._request_json(url)
        if payload.get("result") != "success":
            explanations = {
                "unsupported-code": "базовая валюта не поддерживается",
                "unknown-code": "код валюты неизвестен",
                "malformed-request": "некорректный формат запроса",
                "invalid-key": "API-ключ недействителен",
                "inactive-account": "аккаунт API не активирован",
                "quota-reached": "исчерпана квота запросов",
            }
            error_type = payload.get("error-type")
            reason = (
                explanations.get(error_type, "API отклонил запрос")
                if isinstance(error_type, str)
                else "API отклонил запрос"
            )
            raise ApiRequestError(f"ExchangeRate-API: {reason}")
        if payload.get("base_code") != base:
            raise ApiRequestError("ExchangeRate-API: базовая валюта ответа не совпадает с запросом")
        conversions = payload.get("conversion_rates")
        if not isinstance(conversions, dict):
            raise ApiRequestError("ExchangeRate-API: в ответе нет conversion_rates")
        timestamp = _source_timestamp(payload.get("time_last_update_unix"), self.source)
        rates, metadata = {}, {}
        for raw_code in self.config.fiat_currencies:
            code = validate_currency_code(raw_code)
            if code == base:
                continue
            value = _positive_number(conversions.get(code), f"ExchangeRate-API: курс {base}_{code}")
            pair = f"{code}_{base}"
            rates[pair] = _positive_number(1 / value, f"ExchangeRate-API: курс {pair}")
            metadata[pair] = {"timestamp": timestamp, "meta": {"base_currency": base}}
        if not rates:
            raise ApiRequestError("ExchangeRate-API: список фиатных валют для обновления пуст")
        self.metadata = metadata
        return rates
