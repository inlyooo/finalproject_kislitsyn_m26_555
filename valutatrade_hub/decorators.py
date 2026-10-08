"""Логирование операций и подтверждение действий без изменения бизнес-правил."""

import inspect
import json
from functools import wraps

from valutatrade_hub.core.utils import to_iso, utc_now
from valutatrade_hub.logging_config import get_logger


def log_action(func):
    """Логировать исход операции; исключения передавать вызывающему CLI."""
    signature = inspect.signature(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        entry = {"timestamp": to_iso(utc_now()), "action": func.__name__.upper()}
        for key in ("user_id", "username", "currency", "amount"):
            if key in arguments:
                entry[key] = arguments[key]
        try:
            result = func(*args, **kwargs)
        except Exception as error:
            entry.update(result="ERROR", error_type=type(error).__name__, error=str(error))
            get_logger().info(json.dumps(entry, ensure_ascii=False, default=str))
            raise
        entry["result"] = "OK"
        if isinstance(result, dict):
            for key in (
                "user_id",
                "username",
                "rate",
                "total",
                "before",
                "after",
                "usd_before",
                "usd_after",
            ):
                if key in result:
                    entry[key] = result[key]
            entry["base"] = "USD"
        elif hasattr(result, "user_id"):
            entry["user_id"] = result.user_id
            entry["username"] = result.username
        get_logger().info(json.dumps(entry, ensure_ascii=False, default=str))
        return result

    return wrapper


def confirm_action(prompt):
    """Подтвердить пользовательское действие; confirmed=True подходит для --yes."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            confirmed = kwargs.pop("confirmed", False)
            if not confirmed:
                message = prompt(*args, **kwargs) if callable(prompt) else prompt
                try:
                    answer = input(f"{message} [y/N]: ").strip().lower()
                except EOFError:
                    answer = ""
                if answer not in {"y", "yes", "д", "да"}:
                    raise ValueError("Операция отменена")
            return func(*args, **kwargs)

        return wrapper

    return decorator
