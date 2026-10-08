"""Проверки чисел, UTC-времени и разрешение курсов из локального кэша."""

import copy
import math
import re
import sys
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import ApiRequestError


def configure_console_encoding():
    """UTF-8 сохраняет русский текст и стрелки при перенаправлении в Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def utc_now():
    return datetime.now(UTC)


def to_iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Время обновления должно быть строкой ISO 8601")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Некорректное время обновления курса") from error
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def validate_currency_code(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{2,5}", value) is None:
        raise ValueError("Код валюты должен состоять из 2–5 заглавных латинских букв")
    return value


def validate_amount(value, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("'amount' должен быть положительным числом")
    try:
        amount = float(value)
    except OverflowError as error:
        raise ValueError("Число слишком велико") from error
    if not math.isfinite(amount) or amount < 0 or (not allow_zero and amount == 0):
        raise ValueError("'amount' должен быть положительным конечным числом")
    return amount


def _valid_record(record):
    if not isinstance(record, dict):
        raise ValueError("Курс должен быть объектом")
    rate = validate_amount(record.get("rate"))
    timestamp = parse_timestamp(record.get("updated_at"))
    source = record.get("source", "unknown")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Источник курса должен быть непустой строкой")
    if timestamp > utc_now():
        raise ValueError("Дата курса находится в будущем")
    return {
        "rate": rate,
        "updated_at": to_iso(timestamp),
        "source": source,
    }


def read_cached_rate(from_currency, to_currency, snapshot, max_age=None):
    """Прямой, обратный либо кросс-курс. Свежесть по самому старому измерению."""
    validate_currency_code(from_currency)
    validate_currency_code(to_currency)
    if from_currency == to_currency:
        return {"rate": 1.0, "updated_at": to_iso(utc_now()), "source": "identity"}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pairs", {}), dict):
        raise ApiRequestError("Некорректный формат локального кэша курсов")
    pairs = snapshot.get("pairs", {})
    direct = f"{from_currency}_{to_currency}"
    inverse = f"{to_currency}_{from_currency}"
    try:
        if direct in pairs:
            result = _valid_record(pairs[direct])
        elif inverse in pairs:
            result = _valid_record(pairs[inverse])
            result["rate"] = validate_amount(1 / result["rate"])
        else:

            def usd_rate(code):
                if code == "USD":
                    return {"rate": 1.0, "updated_at": to_iso(utc_now()), "source": "identity"}
                pair = f"{code}_USD"
                if pair in pairs:
                    return _valid_record(pairs[pair])
                pair = f"USD_{code}"
                if pair in pairs:
                    entry = _valid_record(pairs[pair])
                    entry["rate"] = validate_amount(1 / entry["rate"])
                    return entry
                raise KeyError(code)

            left, right = usd_rate(from_currency), usd_rate(to_currency)
            timestamp = min(
                parse_timestamp(left["updated_at"]), parse_timestamp(right["updated_at"])
            )
            sources = sorted({left["source"], right["source"]} - {"identity"})
            result = {
                "rate": validate_amount(left["rate"] / right["rate"]),
                "updated_at": to_iso(timestamp),
                "source": " + ".join(sources),
            }
        if max_age is not None:
            age = (utc_now() - parse_timestamp(result["updated_at"])).total_seconds()
            if age > max_age:
                raise ApiRequestError(
                    f"Курс {from_currency}→{to_currency} устарел. Выполните update-rates"
                )
        return result
    except (ValueError, KeyError, ZeroDivisionError, OverflowError) as error:
        raise ApiRequestError(
            f"Корректный курс {from_currency}→{to_currency} отсутствует в кэше"
        ) from error


def create_rate_cache():
    """Замыкание кэширует расчёт, но проверяет TTL при каждом обращении."""
    cached = {}
    previous_snapshot = None

    def resolve(from_currency, to_currency, snapshot, max_age):
        nonlocal previous_snapshot
        if previous_snapshot != snapshot:
            cached.clear()
            previous_snapshot = copy.deepcopy(snapshot)
        key = (from_currency, to_currency)
        if key not in cached or from_currency == to_currency:
            cached[key] = read_cached_rate(from_currency, to_currency, snapshot)
        result = cached[key].copy()
        age = (utc_now() - parse_timestamp(result["updated_at"])).total_seconds()
        if max_age is not None and age > max_age:
            raise ApiRequestError(
                f"Курс {from_currency}→{to_currency} устарел. Выполните update-rates"
            )
        return result

    return resolve
