"""Независимый опрос источников с продолжением при частичном отказе API."""

import re
from dataclasses import dataclass

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import parse_timestamp, to_iso, utc_now, validate_currency_code
from valutatrade_hub.logging_config import get_logger
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
    _positive_number,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RateStorage


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Итог опроса: изменённые пары, безопасные ошибки и время успешного refresh."""

    updated: int
    errors: list[str]
    last_refresh: str | None


class RatesUpdater:
    """Объединять валидные ответы; полное падение источников сохраняет старый кэш."""

    def __init__(self, clients=None, storage=None, config=None):
        """Подготовить штатные или переданные для проверки источники и хранилище."""
        self.config = config if config is not None else ParserConfig()
        self.clients = (
            [CoinGeckoClient(self.config), ExchangeRateApiClient(self.config)]
            if clients is None
            else list(clients)
        )
        self.storage = storage if storage is not None else RateStorage()

    def _safe_error(self, source, error):
        """Скрыть секреты и полные URL даже в ошибках пользовательского API-клиента."""
        message = str(error)
        for secret in (self.config.exchangerate_api_key, self.config.coingecko_api_key):
            if secret:
                message = message.replace(secret, "[ключ скрыт]")
        message = re.sub(r"https?://\S+", "[URL скрыт]", message)
        return f"{source}: {message}"

    @staticmethod
    def _records(client, rates):
        """Проверить всю выдачу источника, включая пары и обязательное время."""
        if not isinstance(rates, dict) or not rates:
            raise ApiRequestError("источник не вернул ни одного курса")
        records = []
        for pair, rate in rates.items():
            if not isinstance(pair, str) or pair.count("_") != 1:
                raise ApiRequestError("источник вернул некорректную валютную пару")
            from_code, to_code = pair.split("_")
            from_code = validate_currency_code(from_code)
            to_code = validate_currency_code(to_code)
            metadata = client.metadata.get(pair)
            if not isinstance(metadata, dict) or not isinstance(metadata.get("meta", {}), dict):
                raise ApiRequestError(f"отсутствуют метаданные курса {pair}")
            source_time = parse_timestamp(metadata.get("timestamp"))
            if source_time > utc_now():
                raise ApiRequestError(f"время курса {pair} находится в будущем")
            timestamp = to_iso(source_time)
            records.append(
                {
                    "from_currency": from_code,
                    "to_currency": to_code,
                    "rate": _positive_number(rate, f"Курс {pair}"),
                    "timestamp": timestamp,
                    "source": client.source,
                    "meta": metadata.get("meta", {}).copy(),
                }
            )
        return records

    def run_update(self) -> UpdateResult:
        """Опросить все источники и записать успешные; при полном отказе не писать."""
        logger = get_logger("parser")
        records, errors = [], []
        for client in self.clients:
            logger.info("Получение курсов: %s", client.source)
            try:
                source_records = self._records(client, client.fetch_rates())
            except (ApiRequestError, ValueError, TypeError, KeyError) as error:
                message = self._safe_error(client.source, error)
                errors.append(message)
                logger.warning("Источник недоступен: %s", message)
                continue
            records.extend(source_records)
            logger.info("Источник %s вернул %s пар", client.source, len(source_records))
        if not records:
            detail = "; ".join(errors) if errors else "список источников пуст"
            raise ApiRequestError(f"Не удалось обновить курсы: {detail}") from None
        updated = self.storage.save(records)
        result = UpdateResult(updated, errors, self.storage.last_refresh)
        logger.info("Обновлено %s пар; ошибок источников: %s", updated, len(errors))
        return result
