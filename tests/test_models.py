"""Проверки инвариантов пользователя, валют и валютного портфеля."""

import hashlib
import unittest

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    Currency,
    FiatCurrency,
    get_currency,
    list_currencies,
)
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.models import Portfolio, User, Wallet


class UserTests(unittest.TestCase):
    """Пароль защищён солью, данные сохраняются без потери поведения."""

    def test_unique_salt_and_password_verification(self):
        first = User.create(1, "alice", "secret")
        second = User.create(2, "bob", "secret")
        data = first.to_dict()
        self.assertTrue(first.verify_password("secret"))
        self.assertFalse(first.verify_password("wrong"))
        self.assertFalse(first.verify_password(None))
        self.assertNotEqual(data["salt"], second.to_dict()["salt"])
        self.assertNotEqual(
            data["hashed_password"], second.to_dict()["hashed_password"]
        )
        expected = hashlib.sha256(
            ("secret" + data["salt"]).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, data["hashed_password"])
        self.assertNotIn("secret", repr(data))
        self.assertEqual(
            set(first.get_user_info()),
            {"user_id", "username", "registration_date"},
        )

    def test_serialization_and_password_change(self):
        user = User.create(1, " alice ", "secret")
        old = user.to_dict()
        restored = User.from_dict(old)
        self.assertEqual("alice", restored.username)
        self.assertTrue(restored.verify_password("secret"))
        self.assertEqual(old, restored.to_dict())
        self.assertTrue(old["registration_date"].endswith("Z"))
        with self.assertRaises(ValueError):
            restored.change_password("abc")
        self.assertTrue(restored.verify_password("secret"))
        restored.change_password("next password")
        self.assertFalse(restored.verify_password("secret"))
        self.assertTrue(restored.verify_password("next password"))
        self.assertNotEqual(old["salt"], restored.to_dict()["salt"])
        self.assertEqual(
            old["registration_date"], restored.to_dict()["registration_date"]
        )

    def test_invalid_identity_and_password(self):
        for value in ("", "   ", None, 123):
            with self.subTest(username=value), self.assertRaises(ValueError):
                User.create(1, value, "secret")
        for value in ("", "abc", None, 123):
            with self.subTest(password=value), self.assertRaises(ValueError):
                User.create(1, "alice", value)
        for value in (0, -1, True, "1", 1.2):
            with self.subTest(user_id=value), self.assertRaises(ValueError):
                User.create(value, "alice", "secret")
        user = User.create(1, "alice", "1234")
        with self.assertRaises(AttributeError):
            user.user_id = 2
        with self.assertRaises(AttributeError):
            user.registration_date = "2000-01-01"


class WalletTests(unittest.TestCase):
    """Баланс конечен и неотрицателен даже после ошибочной операции."""

    def test_deposit_withdraw_and_round_trip(self):
        wallet = Wallet("USD", 10)
        wallet.deposit(2.5)
        wallet.withdraw(1.5)
        self.assertEqual(11.0, wallet.balance)
        self.assertEqual(
            {"currency_code": "USD", "balance": 11.0},
            wallet.get_balance_info(),
        )
        restored = Wallet.from_dict("USD", wallet.to_dict())
        self.assertEqual(wallet.get_balance_info(), restored.get_balance_info())
        restored.withdraw(11)
        self.assertEqual(0, restored.balance)

    def test_invalid_balances_and_amounts_do_not_change_wallet(self):
        invalid = (True, False, "12", None, float("nan"), float("inf"), -1)
        wallet = Wallet("USD", 10)
        for value in invalid:
            with self.subTest(balance=value), self.assertRaises(ValueError):
                wallet.balance = value
            self.assertEqual(10, wallet.balance)
        for value in (*invalid, 0):
            for operation in (wallet.deposit, wallet.withdraw):
                with self.subTest(amount=value), self.assertRaises(ValueError):
                    operation(value)
                self.assertEqual(10, wallet.balance)

    def test_insufficient_funds_preserves_balance(self):
        wallet = Wallet("BTC", 0.1)
        with self.assertRaises(InsufficientFundsError) as error:
            wallet.withdraw(0.2)
        self.assertEqual(0.1, wallet.balance)
        self.assertEqual(0.1, error.exception.available)
        self.assertEqual(0.2, error.exception.required)
        self.assertEqual("BTC", error.exception.currency_code)
        self.assertIn("Недостаточно средств", str(error.exception))


class CurrencyTests(unittest.TestCase):
    """Реестр содержит только описанные и корректно оформленные валюты."""

    def test_abstract_base_registry_and_display(self):
        with self.assertRaises(TypeError):
            Currency("Dollar", "USD")
        self.assertEqual(
            {"USD", "EUR", "GBP", "RUB", "BTC", "ETH", "SOL"},
            {currency.code for currency in list_currencies()},
        )
        self.assertEqual(
            "[FIAT] USD — US Dollar (Issuing: United States)",
            get_currency("USD").get_display_info(),
        )
        crypto = get_currency("BTC")
        self.assertIsInstance(crypto, CryptoCurrency)
        self.assertIn("Algo: SHA-256", crypto.get_display_info())
        self.assertIsInstance(get_currency("EUR"), FiatCurrency)
        with self.assertRaises(CurrencyNotFoundError):
            get_currency("ZZZ")

    def test_invalid_codes_and_currency_attributes(self):
        for code in ("usd", " USD", "USD ", "U", "ABCDEF", "РУБ", "U1D", None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                get_currency(code)
        for value in ("", " ", None, 1):
            with self.subTest(name=value), self.assertRaises(ValueError):
                FiatCurrency(value, "USD", "United States")
            with self.subTest(country=value), self.assertRaises(ValueError):
                FiatCurrency("Dollar", "USD", value)
            with self.subTest(algorithm=value), self.assertRaises(ValueError):
                CryptoCurrency("Bitcoin", "BTC", value, 1)
        for value in (True, "10", None, -1, float("nan"), float("inf")):
            with self.subTest(market_cap=value), self.assertRaises(ValueError):
                CryptoCurrency("Bitcoin", "BTC", "SHA-256", value)


class PortfolioTests(unittest.TestCase):
    """Кошельки уникальны, владелец постоянен, стоимость следует курсу."""

    def setUp(self):
        self.user = User.create(1, "alice", "secret")

    def test_unique_wallets_and_read_only_owner(self):
        portfolio = Portfolio(self.user)
        first = portfolio.add_currency("USD")
        first.deposit(20)
        self.assertIs(first, portfolio.add_currency("USD"))
        self.assertEqual(20, portfolio.get_wallet("USD").balance)
        copied = portfolio.wallets
        copied.clear()
        self.assertEqual(1, len(portfolio.wallets))
        self.assertIsNone(portfolio.get_wallet("EUR"))
        self.assertEqual(1, portfolio.user_id)
        with self.assertRaises(AttributeError):
            portfolio.user = User.create(2, "bob", "secret")
        with self.assertRaises(CurrencyNotFoundError):
            portfolio.add_currency("ZZZ")

    def test_total_value_and_serialization(self):
        rates = {("EUR", "USD"): 1.2, ("BTC", "USD"): 50_000.0}
        seen = []

        def rate_getter(source, target):
            seen.append((source, target))
            return {"rate": rates[(source, target)]}

        portfolio = Portfolio(self.user, rate_getter=rate_getter)
        portfolio.add_currency("USD").deposit(100)
        portfolio.add_currency("EUR").deposit(10)
        portfolio.add_currency("BTC").deposit(0.01)
        portfolio.add_currency("GBP")
        self.assertEqual(612, portfolio.get_total_value())
        self.assertEqual([("EUR", "USD"), ("BTC", "USD")], seen)
        restored = Portfolio.from_dict(
            portfolio.to_dict(), self.user, rate_getter
        )
        self.assertEqual(portfolio.to_dict(), restored.to_dict())
        self.assertEqual(612, restored.get_total_value())
        euro = Portfolio(self.user, {"EUR": Wallet("EUR", 10)})
        self.assertEqual(10, euro.get_total_value("EUR"))
        self.assertEqual(0, Portfolio(self.user).get_total_value())

    def test_rate_failure_is_not_a_made_up_valuation(self):
        def unavailable(source, target):
            raise ApiRequestError("Источник недоступен")

        portfolio = Portfolio(
            self.user, {"BTC": Wallet("BTC", 1)}, unavailable
        )
        with self.assertRaises(ApiRequestError):
            portfolio.get_total_value()
        self.assertEqual(1, portfolio.get_wallet("BTC").balance)
        for rate in (0, -1, True, float("nan"), float("inf")):
            portfolio = Portfolio(
                self.user,
                {"BTC": Wallet("BTC", 1)},
                lambda *_, value=rate: value,
            )
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                portfolio.get_total_value()
        numeric_rate = Portfolio(
            self.user, {"EUR": Wallet("EUR", 2)}, lambda *_: 1.5
        )
        self.assertEqual(3, numeric_rate.get_total_value())

    def test_invalid_owner_wallet_and_owner_id(self):
        with self.assertRaises(ValueError):
            Portfolio(None)
        with self.assertRaises(ValueError):
            Portfolio(self.user, {"USD": Wallet("EUR")})
        with self.assertRaises(ValueError):
            Portfolio(self.user, rate_getter="rates")
        with self.assertRaises(ValueError):
            Portfolio.from_dict({"user_id": 2, "wallets": {}}, self.user)


if __name__ == "__main__":
    unittest.main()
