"""Пользователь, кошелёк и валютный портфель учебной платформы."""

import hashlib
import secrets
from datetime import datetime

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import InsufficientFundsError
from valutatrade_hub.core.utils import (
    parse_timestamp,
    to_iso,
    utc_now,
    validate_amount,
)

MIN_PASSWORD_LENGTH = 4
SALT_BYTES = 16


class User:
    """Пользователь с приватными полями и солёным SHA-256 пароля."""

    def __init__(
        self, user_id, username, hashed_password, salt, registration_date
    ):
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("ID пользователя должен быть целым числом.")
        if user_id <= 0:
            raise ValueError("ID пользователя должен быть положительным.")
        if not isinstance(hashed_password, str) or not hashed_password:
            raise ValueError("Хеш пароля не может быть пустым.")
        if not isinstance(salt, str) or not salt:
            raise ValueError("Соль пароля не может быть пустой.")
        self._user_id = user_id
        self.username = username
        self._hashed_password = hashed_password
        self._salt = salt
        if isinstance(registration_date, datetime):
            registration_date = to_iso(registration_date)
        self._registration_date = parse_timestamp(registration_date)

    @staticmethod
    def _validate_password(password):
        """Проверить учебное требование к длине пароля."""
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Пароль должен содержать минимум {MIN_PASSWORD_LENGTH} символа."
            )

    @staticmethod
    def _hash_password(password, salt):
        """Посчитать SHA-256 строки из пароля и уникальной соли."""
        return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

    @classmethod
    def create(cls, user_id, username, password):
        """Создать пользователя с новой солью и временем регистрации."""
        cls._validate_password(password)
        salt = secrets.token_hex(SALT_BYTES)
        return cls(
            user_id,
            username,
            cls._hash_password(password, salt),
            salt,
            utc_now(),
        )

    @property
    def user_id(self):
        """Неизменяемый идентификатор пользователя."""
        return self._user_id

    @property
    def username(self):
        """Имя пользователя."""
        return self._username

    @username.setter
    def username(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Имя пользователя не может быть пустым.")
        self._username = value.strip()

    @property
    def registration_date(self):
        """Неизменяемая дата регистрации."""
        return self._registration_date

    def get_user_info(self):
        """Вернуть общедоступные сведения без хеша и соли пароля."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "registration_date": to_iso(self.registration_date),
        }

    def verify_password(self, password):
        """Проверить пароль без сохранения открытого текста."""
        if not isinstance(password, str):
            return False
        candidate = self._hash_password(password, self._salt)
        return secrets.compare_digest(candidate, self._hashed_password)

    def change_password(self, new_password):
        """Заменить пароль и соль после проверки нового значения."""
        self._validate_password(new_password)
        salt = secrets.token_hex(SALT_BYTES)
        self._hashed_password = self._hash_password(new_password, salt)
        self._salt = salt

    def to_dict(self):
        """Подготовить запись пользователя для users.json."""
        return {
            **self.get_user_info(),
            "hashed_password": self._hashed_password,
            "salt": self._salt,
        }

    @classmethod
    def from_dict(cls, data):
        """Восстановить пользователя без повторного хеширования пароля."""
        return cls(
            data["user_id"],
            data["username"],
            data["hashed_password"],
            data["salt"],
            data["registration_date"],
        )


class Wallet:
    """Кошелёк одной валюты с неотрицательным балансом."""

    def __init__(self, currency_code, balance=0):
        self._currency_code = get_currency(currency_code).code
        self.balance = balance

    @property
    def currency_code(self):
        """Неизменяемый код валюты кошелька."""
        return self._currency_code

    @property
    def balance(self):
        """Баланс кошелька."""
        return self._balance

    @balance.setter
    def balance(self, value):
        self._balance = validate_amount(value, allow_zero=True)

    def deposit(self, amount):
        """Добавить положительную сумму на кошелёк."""
        amount = validate_amount(amount)
        self.balance += amount

    def withdraw(self, amount):
        """Списать положительную сумму, сохраняя баланс >= 0."""
        amount = validate_amount(amount)
        if amount > self.balance:
            raise InsufficientFundsError(
                self.balance, amount, self.currency_code
            )
        self.balance -= amount

    def get_balance_info(self):
        """Вернуть код и числовой баланс для вывода в CLI."""
        return {"currency_code": self.currency_code, "balance": self.balance}

    def to_dict(self):
        """Подготовить запись кошелька; код хранится в ключе портфеля."""
        return {"balance": self.balance}

    @classmethod
    def from_dict(cls, currency_code, data):
        """Восстановить кошелёк из записи portfolios.json."""
        return cls(currency_code, data["balance"])


class Portfolio:
    """Набор уникальных валютных кошельков одного пользователя."""

    def __init__(self, user: User, wallets=None, rate_getter=None):
        if not isinstance(user, User):
            raise ValueError("Владельцем портфеля должен быть User.")
        if wallets is not None and not isinstance(wallets, dict):
            raise ValueError("Кошельки должны быть словарём.")
        if rate_getter is not None and not callable(rate_getter):
            raise ValueError("Получатель курсов должен быть функцией.")
        self._user = user
        self._wallets = dict(wallets or {})
        for code, wallet in self._wallets.items():
            get_currency(code)
            if not isinstance(wallet, Wallet) or wallet.currency_code != code:
                raise ValueError("Код в портфеле должен совпадать с кошельком.")
        self._rate_getter = rate_getter

    @property
    def user(self):
        """Владелец портфеля, доступный только для чтения."""
        return self._user

    @property
    def user_id(self):
        """Идентификатор владельца портфеля."""
        return self.user.user_id

    @property
    def wallets(self):
        """Вернуть копию словаря, сохранив управление его составом."""
        return self._wallets.copy()

    def add_currency(self, currency_code):
        """Создать кошелёк отсутствующей валюты или вернуть существующий."""
        code = get_currency(currency_code).code
        if code not in self._wallets:
            self._wallets[code] = Wallet(code)
        return self._wallets[code]

    def get_wallet(self, currency_code):
        """Вернуть имеющийся кошелёк; отсутствие обозначается None."""
        code = get_currency(currency_code).code
        return self._wallets.get(code)

    def get_total_value(self, base_currency="USD"):
        """Сложить стоимость кошельков по переданному получателю курсов."""
        base_currency = get_currency(base_currency).code
        rate_getter = self._rate_getter
        total = 0.0
        for code, wallet in self._wallets.items():
            if wallet.balance == 0:
                continue
            if code == base_currency:
                rate = 1.0
            else:
                if rate_getter is None:
                    from valutatrade_hub.core.usecases import get_rate

                    rate_getter = get_rate
                result = rate_getter(code, base_currency)
                rate = result["rate"] if isinstance(result, dict) else result
                rate = validate_amount(rate)
            total += wallet.balance * rate
        return validate_amount(total, allow_zero=True)

    def to_dict(self):
        """Подготовить портфель для portfolios.json."""
        return {
            "user_id": self.user_id,
            "wallets": {
                code: wallet.to_dict() for code, wallet in self._wallets.items()
            },
        }

    @classmethod
    def from_dict(cls, data, user, rate_getter=None):
        """Восстановить портфель, сохранив связь с его владельцем."""
        if data["user_id"] != user.user_id:
            raise ValueError("ID портфеля не совпадает с владельцем.")
        wallets = {
            code: Wallet.from_dict(code, wallet)
            for code, wallet in data["wallets"].items()
        }
        return cls(user, wallets, rate_getter)
