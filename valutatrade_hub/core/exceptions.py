"""Понятные ошибки операций с пользователями, кошельками и курсами."""


class InsufficientFundsError(ValueError):
    """На кошельке недостаточно средств для списания."""

    def __init__(self, available, required, code):
        self.available = available
        self.required = required
        self.currency_code = code
        super().__init__(
            f"Недостаточно средств: доступно {available:.4f} {code}, "
            f"требуется {required:.4f} {code}."
        )


class CurrencyNotFoundError(ValueError):
    """Код валюты отсутствует в поддерживаемом реестре."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"Неизвестная валюта: {code}.")


class ApiRequestError(RuntimeError):
    """Курс недоступен или внешний источник вернул ошибку."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Не удалось получить курс: {reason}.")


class AuthenticationError(ValueError):
    """Вход в систему не выполнен или учётные данные неверны."""
