"""История котировок и атомарно записываемый кэш последних курсов."""

import json

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import parse_timestamp, to_iso, utc_now, validate_currency_code
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.parser_service.api_clients import _positive_number


class RateStorage:
    """Сохранять уникальные записи истории и только более новые курсы кэша."""

    def __init__(self, database=None):
        """Использовать общий JSON-менеджер или переданный изолированный менеджер."""
        self.database = database if database is not None else DatabaseManager()
        self.last_refresh = None

    @staticmethod
    def _normalize(record):
        """Проверить запись до изменения файлов и построить устойчивый ID истории."""
        if not isinstance(record, dict):
            raise ApiRequestError("Хранилище курсов: запись должна быть объектом")
        try:
            from_code = validate_currency_code(record["from_currency"])
            to_code = validate_currency_code(record["to_currency"])
            source_time = parse_timestamp(record["timestamp"])
            if source_time > utc_now():
                raise ValueError("Время курса находится в будущем")
            timestamp = to_iso(source_time)
            source = record["source"]
            metadata = record.get("meta", {})
        except (KeyError, TypeError, ValueError):
            raise ApiRequestError(
                "Хранилище курсов: некорректная запись или время источника"
            ) from None
        if not isinstance(source, str) or not source or not isinstance(metadata, dict):
            raise ApiRequestError("Хранилище курсов: некорректные метаданные источника")
        try:
            json.dumps(metadata, allow_nan=False)
        except (TypeError, ValueError):
            raise ApiRequestError("Хранилище курсов: метаданные не соответствуют JSON") from None
        rate = _positive_number(record.get("rate"), "Хранилище курсов: курс")
        return {
            "id": f"{from_code}_{to_code}_{timestamp}",
            "from_currency": from_code,
            "to_currency": to_code,
            "rate": rate,
            "timestamp": timestamp,
            "source": source,
            "meta": metadata.copy(),
        }

    def save(self, records: list[dict]) -> int:
        """Вернуть число изменённых пар; last_refresh отражает успешную запись batch."""
        normalized = [self._normalize(record) for record in records]
        if not normalized:
            raise ApiRequestError("Хранилище курсов: нет курсов для сохранения")
        # Один источник/момент пары — одна запись истории, даже при повторном опросе.
        unique = {}
        for record in normalized:
            unique.setdefault(record["id"], record)
        normalized = list(unique.values())
        refresh = to_iso(utc_now())

        def append_history(history):
            if not isinstance(history, list):
                raise ApiRequestError("История курсов повреждена: ожидался список")
            known = {record.get("id") for record in history if isinstance(record, dict)}
            history.extend(record for record in normalized if record["id"] not in known)

        self.database.update("exchange_rates", append_history, [])
        updated = 0

        def merge_cache(snapshot):
            nonlocal updated
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pairs", {}), dict):
                raise ApiRequestError("Кэш курсов повреждён: ожидался объект pairs")
            pairs = dict(snapshot.get("pairs", {}))
            changed_pairs = set()
            for record in normalized:
                pair = f"{record['from_currency']}_{record['to_currency']}"
                current = pairs.get(pair)
                if current is not None:
                    if not isinstance(current, dict) or "updated_at" not in current:
                        raise ApiRequestError(f"Кэш курсов повреждён: запись {pair}")
                    try:
                        current_time = parse_timestamp(current["updated_at"])
                    except (TypeError, ValueError):
                        raise ApiRequestError(
                            f"Кэш курсов повреждён: время записи {pair}"
                        ) from None
                    if current_time > utc_now():
                        raise ApiRequestError(f"Кэш курсов повреждён: время {pair} в будущем")
                    if current_time >= parse_timestamp(record["timestamp"]):
                        continue
                pairs[pair] = {
                    "rate": record["rate"],
                    "updated_at": record["timestamp"],
                    "source": record["source"],
                }
                changed_pairs.add(pair)
            updated = len(changed_pairs)
            snapshot.clear()
            snapshot.update({"pairs": pairs, "last_refresh": refresh})

        self.database.update("rates", merge_cache, {"pairs": {}, "last_refresh": None})
        self.last_refresh = refresh
        return updated
