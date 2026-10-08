"""Проверки бизнес-сценариев на реальных временных файлах без внешнего API."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tests.test_infra import TemporaryStorageTestCase
from valutatrade_hub.core import usecases
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.utils import create_rate_cache, read_cached_rate, to_iso, utc_now


class UsecaseTests(TemporaryStorageTestCase):
    """Сделки сохраняют деньги, ошибки не меняют портфель и пароли не логируются."""

    def setUp(self):
        super().setUp()
        self.resolver = patch.object(usecases, "_resolve_rate", create_rate_cache())
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.user = usecases.register("alice", "unique password")
        self.write_rates()

    def write_rates(self, age=0, btc=50_000.0):
        timestamp = to_iso(utc_now() - timedelta(seconds=age))
        self.database.write(
            "rates",
            {
                "pairs": {
                    "BTC_USD": {
                        "rate": btc,
                        "updated_at": timestamp,
                        "source": "CoinGecko",
                    },
                    "USD_EUR": {
                        "rate": 0.8,
                        "updated_at": timestamp,
                        "source": "ExchangeRate-API",
                    },
                },
                "last_refresh": to_iso(utc_now()),
            },
        )

    def portfolio_bytes(self):
        return (self.directory / "data" / "portfolios.json").read_bytes()

    def test_registration_login_and_no_password_in_files_or_log(self):
        self.assertEqual("alice", usecases.login("alice", "unique password").username)
        with self.assertRaisesRegex(ValueError, "Неверный пароль"):
            usecases.login("alice", "wrong password needle")
        with self.assertRaises(ValueError):
            usecases.register("bob", "bad")
        self.assertEqual({}, usecases.get_portfolio(self.user.user_id).wallets)
        second = usecases.register("bob", "other password needle")
        self.assertNotEqual(self.user.user_id, second.user_id)
        users = (self.directory / "data" / "users.json").read_text(encoding="utf-8")
        logs = (self.directory / "logs" / "actions.log").read_text(encoding="utf-8")
        for password in ("unique password", "wrong password needle", "other password needle"):
            self.assertNotIn(password, users)
            self.assertNotIn(password, logs)
        self.assertIn('"action": "REGISTER"', logs)
        self.assertIn('"result": "ERROR"', logs)

    def test_normalized_duplicate_name_is_rejected(self):
        before = self.portfolio_bytes()
        with self.assertRaises(ValueError):
            usecases.register(" alice ", "another password")
        self.assertEqual(1, len(self.database.read("users")))
        self.assertEqual(before, self.portfolio_bytes())

    def test_failed_user_write_does_not_leave_orphan_portfolio(self):
        users_path = self.directory / "data" / "users.json"
        users_before = users_path.read_bytes()
        portfolios_before = self.portfolio_bytes()
        original_write = self.database.write

        def fail_user_write(name, data):
            if name == "users":
                raise OSError("Симуляция отказа записи users.json")
            return original_write(name, data)

        with (
            patch.object(self.database, "write", side_effect=fail_user_write),
            self.assertRaises(OSError),
        ):
            usecases.register("bob", "another password")
        self.assertEqual(users_before, users_path.read_bytes())
        self.assertEqual(portfolios_before, self.portfolio_bytes())

    def test_full_trade_and_persistence(self):
        user_id = self.user.user_id
        usecases.deposit(user_id, "USD", 1000)
        purchase = usecases.buy(user_id, "BTC", 0.01)
        self.assertEqual(500, purchase["total"])
        self.assertEqual(500, purchase["usd_after"])
        sale = usecases.sell(user_id, "BTC", 0.005)
        self.assertEqual(250, sale["total"])
        self.assertEqual(750, sale["usd_after"])
        portfolio = usecases.get_portfolio(user_id)
        self.assertEqual(750, portfolio.get_wallet("USD").balance)
        self.assertEqual(0.005, portfolio.get_wallet("BTC").balance)
        self.assertEqual(1000, portfolio.get_total_value())
        self.assertEqual("alice", purchase["username"])
        logs = (self.directory / "logs" / "actions.log").read_text(encoding="utf-8")
        self.assertIn('"action": "BUY"', logs)
        self.assertIn('"action": "SELL"', logs)
        self.assertNotIn("unique password", logs)

    def test_insufficient_funds_do_not_create_wallet_or_change_file(self):
        user_id = self.user.user_id
        before = self.portfolio_bytes()
        with self.assertRaises(InsufficientFundsError):
            usecases.buy(user_id, "BTC", 1)
        self.assertEqual(before, self.portfolio_bytes())
        self.assertEqual({}, usecases.get_portfolio(user_id).wallets)
        usecases.deposit(user_id, "USD", 1000)
        usecases.buy(user_id, "BTC", 0.01)
        before = self.portfolio_bytes()
        with self.assertRaises(InsufficientFundsError):
            usecases.sell(user_id, "BTC", 1)
        self.assertEqual(before, self.portfolio_bytes())

    def test_invalid_amount_currency_or_nonusd_deposit_preserves_file(self):
        before = self.portfolio_bytes()
        with patch.object(usecases, "update_rates") as update:
            for amount in (0, -1, True, None, "10", float("nan"), float("inf")):
                for action in (usecases.deposit, usecases.buy, usecases.sell):
                    code = "USD" if action is usecases.deposit else "BTC"
                    with self.subTest(action=action.__name__, amount=amount):
                        with self.assertRaises(ValueError):
                            action(self.user.user_id, code, amount)
                    self.assertEqual(before, self.portfolio_bytes())
            for currency in ("ZZZ", "btc", " BTC", "USD "):
                with self.subTest(currency=currency), self.assertRaises(ValueError):
                    usecases.buy(self.user.user_id, currency, 1)
            with self.assertRaises(CurrencyNotFoundError):
                usecases.deposit(self.user.user_id, "ZZZ", 1)
            with self.assertRaises(ValueError):
                usecases.deposit(self.user.user_id, "EUR", 1)
            update.assert_not_called()
        self.assertEqual(before, self.portfolio_bytes())

    def test_stale_rates_and_failed_refresh_preserve_portfolio(self):
        usecases.deposit(self.user.user_id, "USD", 1000)
        self.write_rates(age=70)
        before = self.portfolio_bytes()
        with patch.object(
            usecases, "update_rates", side_effect=ApiRequestError("Сеть недоступна")
        ) as update:
            with self.assertRaises(ApiRequestError):
                usecases.buy(self.user.user_id, "BTC", 0.01)
        update.assert_called_once_with("coingecko")
        self.assertEqual(before, self.portfolio_bytes())
        self.assertIsNone(usecases.get_portfolio(self.user.user_id).get_wallet("BTC"))
        rows = usecases.show_rates(currency="BTC")
        self.assertTrue(rows[0]["stale"])

    def test_missing_rate_refreshes_and_uses_new_file(self):
        self.database.write("rates", {"pairs": {}, "last_refresh": None})
        with patch.object(usecases, "update_rates", side_effect=lambda _: self.write_rates()):
            self.assertEqual(50_000, usecases.get_rate("BTC", "USD")["rate"])
        self.write_rates(btc=60_000)
        with patch.object(usecases, "update_rates") as update:
            self.assertEqual(60_000, usecases.get_rate("BTC", "USD")["rate"])
            update.assert_not_called()

    def test_inverse_cross_and_oldest_source_timestamp(self):
        snapshot = self.database.read("rates")
        older = to_iso(utc_now() - timedelta(seconds=40))
        snapshot["pairs"]["USD_EUR"]["updated_at"] = older
        self.database.write("rates", snapshot)
        with patch.object(usecases, "update_rates") as update:
            self.assertEqual(1.25, usecases.get_rate("EUR", "USD")["rate"])
            cross = usecases.get_rate("BTC", "EUR")
            self.assertEqual(40_000, cross["rate"])
            self.assertEqual(older, cross["updated_at"])
            update.assert_not_called()
        snapshot["pairs"]["USD_EUR"]["updated_at"] = to_iso(utc_now() - timedelta(seconds=70))
        self.database.write("rates", snapshot)
        with patch.object(
            usecases, "update_rates", side_effect=ApiRequestError("Сеть недоступна")
        ) as update:
            with self.assertRaises(ApiRequestError):
                usecases.get_rate("BTC", "EUR")
            update.assert_called_once_with(None)


class RateCacheTests(TemporaryStorageTestCase):
    """Обновление timestamp и самой записи меняет допустимость кэшированного курса."""

    def test_cached_rate_expires_even_when_snapshot_has_not_changed(self):
        moment = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
        snapshot = {
            "pairs": {"BTC_USD": {"rate": 50_000, "updated_at": to_iso(moment), "source": "test"}}
        }
        resolve = create_rate_cache()
        with patch("valutatrade_hub.core.utils.utc_now", return_value=moment):
            self.assertEqual(50_000, resolve("BTC", "USD", snapshot, 60)["rate"])
        with patch(
            "valutatrade_hub.core.utils.utc_now", return_value=moment + timedelta(seconds=61)
        ):
            with self.assertRaises(ApiRequestError):
                resolve("BTC", "USD", snapshot, 60)

    def test_mutated_snapshot_invalidates_cached_value(self):
        moment = utc_now() - timedelta(seconds=1)
        snapshot = {
            "pairs": {"BTC_USD": {"rate": 50_000, "updated_at": to_iso(moment), "source": "test"}}
        }
        resolve = create_rate_cache()
        self.assertEqual(50_000, resolve("BTC", "USD", snapshot, 60)["rate"])
        snapshot["pairs"]["BTC_USD"]["rate"] = 60_000
        self.assertEqual(60_000, resolve("BTC", "USD", snapshot, 60)["rate"])

    def test_invalid_rate_time_and_numeric_value_are_domain_errors(self):
        now = utc_now()
        for rate, timestamp in (
            (True, to_iso(now)),
            (0, to_iso(now)),
            (float("nan"), to_iso(now)),
            (1, "wrong date"),
            (1, to_iso(now + timedelta(days=1))),
        ):
            snapshot = {"pairs": {"BTC_USD": {"rate": rate, "updated_at": timestamp}}}
            with self.subTest(rate=rate, timestamp=timestamp), self.assertRaises(ApiRequestError):
                read_cached_rate("BTC", "USD", snapshot, 60)

    def test_invalid_source_metadata_is_a_domain_error_for_cross_rate(self):
        timestamp = to_iso(utc_now() - timedelta(seconds=1))
        snapshot = {
            "pairs": {
                "BTC_USD": {"rate": 50_000, "updated_at": timestamp, "source": []},
                "EUR_USD": {"rate": 1.25, "updated_at": timestamp, "source": "test"},
            }
        }
        with self.assertRaises(ApiRequestError):
            read_cached_rate("BTC", "EUR", snapshot, 60)
