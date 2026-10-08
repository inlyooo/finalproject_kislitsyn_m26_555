"""Детерминированные проверки сессии CLI без сетевых запросов и реальных данных."""

import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from valutatrade_hub.cli.interface import Session, main
from valutatrade_hub.core.utils import to_iso, utc_now
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class CliTests(unittest.TestCase):
    """Проверяет пользовательские сценарии и продолжение после ошибок."""

    def setUp(self):
        """Создаёт изолированные сессию и вывод."""
        self.session = Session()
        self.user = SimpleNamespace(user_id=1, username="Анна")
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_redirected_legacy_windows_encoding_preserves_unicode(self):
        """cp1251 в дочернем процессе не ломает вывод USD→USD в пайпе."""
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1251"
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8=0",
                "-c",
                "from valutatrade_hub.cli.interface import main; "
                "raise SystemExit(main(['get-rate','--from','USD','--to','USD']))",
            ],
            env=environment,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USD→USD: 1", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_full_interactive_scenario(self):
        """Полный цикл, включая пробелы в аргументах и выход."""
        portfolio = SimpleNamespace(
            user=self.user,
            wallets={"USD": SimpleNamespace(balance=1000)},
        )
        trade = {
            "currency": "BTC",
            "amount": 0.01,
            "rate": 50000.0,
            "total": 500.0,
            "before": 0,
            "after": 0.01,
            "usd_before": 1000.0,
            "usd_after": 500.0,
        }
        cached = {
            "currency": "BTC",
            "pair": "BTC_USD",
            "rate": 50000.0,
            "updated_at": "2026-10-08T09:00:00Z",
            "source": "CoinGecko",
            "stale": False,
        }
        commands = [
            'register --username "Анна" --password "secret words"',
            'login --username "Анна" --password "secret words"',
            "deposit --amount 1000",
            "buy --currency BTC --amount 0.01 --yes",
            "sell --currency BTC --amount 0.01 --yes",
            "show-portfolio",
            "get-rate --from BTC --to USD",
            "update-rates",
            "show-rates",
            "logout",
            "exit",
        ]
        with (
            patch("builtins.input", side_effect=commands),
            patch(
                "valutatrade_hub.cli.interface.usecases.register", return_value=self.user
            ) as register,
            patch("valutatrade_hub.cli.interface.usecases.login", return_value=self.user),
            patch(
                "valutatrade_hub.cli.interface.usecases.deposit",
                return_value={"currency": "USD", "amount": 1000, "after": 1000},
            ),
            patch("valutatrade_hub.cli.interface.usecases.buy", return_value=trade),
            patch("valutatrade_hub.cli.interface.usecases.sell", return_value=trade),
            patch("valutatrade_hub.cli.interface.usecases.get_portfolio", return_value=portfolio),
            patch("valutatrade_hub.cli.interface.usecases.get_rate", return_value=cached),
            patch(
                "valutatrade_hub.cli.interface.usecases.update_rates",
                return_value=SimpleNamespace(
                    updated=1, last_refresh=cached["updated_at"], errors=[]
                ),
            ),
            patch("valutatrade_hub.cli.interface.usecases.show_rates", return_value=[cached]),
        ):
            self.assertEqual(main([]), 0)
        register.assert_called_once_with("Анна", "secret words")
        output = self.output.getvalue()
        for phrase in (
            "зарегистрирован",
            "Вы вошли",
            "Покупка выполнена",
            "Продажа выполнена",
            "Общая стоимость",
            "Обратный курс",
            "Обновлено курсов",
            "свежий",
            "Сессия пользователя завершена",
            "До свидания",
        ):
            self.assertIn(phrase, output)
        self.assertNotIn("secret words", output)

    def test_auth_required_before_trade(self):
        """Без login сделка не вызывается."""
        with patch("valutatrade_hub.cli.interface.usecases.buy") as buy:
            self.assertEqual(self.session.execute("buy --currency BTC --amount 1 --yes"), 1)
        buy.assert_not_called()
        self.assertIn("Сначала выполните login", self.output.getvalue())

    def test_cancel_then_confirm_trade(self):
        """Отмена не меняет состояние, подтверждение вызывает сценарий один раз."""
        self.session.current_user = self.user
        trade = {
            "currency": "EUR",
            "amount": 1.0,
            "rate": 1.1,
            "total": 1.1,
            "before": 0,
            "after": 1,
            "usd_before": 10,
            "usd_after": 8.9,
        }
        with patch("valutatrade_hub.cli.interface.usecases.buy", return_value=trade) as buy:
            with patch("builtins.input", return_value="n"):
                self.assertEqual(self.session.execute("buy --currency EUR --amount 1"), 1)
            buy.assert_not_called()
            with patch("builtins.input", return_value="да"):
                self.assertEqual(self.session.execute("buy --currency EUR --amount 1"), 0)
            buy.assert_called_once_with(1, "EUR", 1.0)

    def test_invalid_command_does_not_end_session(self):
        """Ошибка парсинга, незакрытые кавычки и help сохраняют работающую сессию."""
        for text in ("unknown", "get-rate --from USD", 'login --username "broken'):
            self.assertEqual(self.session.execute(text), 1)
            self.assertTrue(self.session.running)
        self.assertEqual(self.session.execute("help"), 0)
        self.assertEqual(self.session.execute("buy --help"), 0)
        self.assertEqual(self.session.execute("exit"), 0)
        self.assertFalse(self.session.running)

    def test_domain_error_does_not_print_traceback(self):
        """Ожидаемая ошибка отображается дружелюбно."""
        with patch(
            "valutatrade_hub.cli.interface.usecases.get_rate",
            side_effect=RuntimeError("Курс устарел. Выполните update-rates."),
        ):
            self.assertEqual(main(["get-rate", "--from", "BTC", "--to", "USD"]), 1)
        self.assertIn("Курс устарел", self.output.getvalue())
        self.assertNotIn("Traceback", self.output.getvalue())

    def test_failed_login_preserves_existing_user(self):
        """Неудачный вход не сбрасывает уже вошедшего пользователя."""
        self.session.current_user = self.user
        with patch(
            "valutatrade_hub.cli.interface.usecases.login",
            side_effect=ValueError("Неверный пароль"),
        ):
            self.assertEqual(self.session.execute("login --username other --password wrong"), 1)
        self.assertIs(self.session.current_user, self.user)

    def test_stale_rates_and_partial_update_are_visible(self):
        """Кеш показывает устаревание, частичный успех — предупреждение источника."""
        cached = {
            "pair": "BTC_USD",
            "rate": 50000,
            "updated_at": "2020-01-01T00:00:00Z",
            "source": "CoinGecko",
            "stale": True,
        }
        report = SimpleNamespace(
            updated=1, last_refresh="2026-10-08T09:00:00Z", errors=["ExchangeRate-API недоступен"]
        )
        with (
            patch("valutatrade_hub.cli.interface.usecases.show_rates", return_value=[cached]),
            patch("valutatrade_hub.cli.interface.usecases.update_rates", return_value=report),
        ):
            self.assertEqual(self.session.execute("show-rates --currency BTC --top 1"), 0)
            self.assertEqual(self.session.execute("update-rates"), 0)
        self.assertIn("устарел", self.output.getvalue())
        self.assertIn("ExchangeRate-API недоступен", self.output.getvalue())

    def test_eof_exits_interactive_mode(self):
        """Конец ввода завершает сессию штатно."""
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(main([]), 0)
        self.assertIn("До свидания", self.output.getvalue())

    def test_unknown_base_rejected_for_empty_portfolio(self):
        """Валидация базы действует даже до первого пополнения."""
        self.session.current_user = self.user
        portfolio = SimpleNamespace(user=self.user, wallets={})
        with patch("valutatrade_hub.cli.interface.usecases.get_portfolio", return_value=portfolio):
            self.assertEqual(self.session.execute("show-portfolio --base ZZZ"), 1)
        self.assertNotIn("Портфель пуст", self.output.getvalue())

    def test_portfolio_does_not_repeat_failed_rate_request(self):
        """Недоступность курса не вызывает повторное обновление ради итоговой суммы."""
        self.session.current_user = self.user
        portfolio = SimpleNamespace(
            user=self.user,
            wallets={"BTC": SimpleNamespace(balance=0.01)},
        )
        with (
            patch("valutatrade_hub.cli.interface.usecases.get_portfolio", return_value=portfolio),
            patch(
                "valutatrade_hub.cli.interface.usecases.get_rate",
                side_effect=RuntimeError("Источник недоступен"),
            ) as get_rate,
        ):
            self.assertEqual(self.session.execute("show-portfolio"), 0)
        get_rate.assert_called_once_with("BTC", "USD")
        self.assertIn("Общая стоимость недоступна", self.output.getvalue())

    def test_real_cli_trading_cycle_with_isolated_json(self):
        """CLI передаёт числовые суммы ядру; сделки и ошибки сохраняют верные JSON."""
        try:
            with tempfile.TemporaryDirectory() as directory:
                environment = {
                    "VALUTATRADE_DATA_DIR": str(Path(directory) / "data"),
                    "VALUTATRADE_LOG_DIR": str(Path(directory) / "logs"),
                }
                with patch.dict(os.environ, environment):
                    SettingsLoader().reload()
                    database = DatabaseManager()
                    stamp = to_iso(utc_now())
                    database.write(
                        "rates",
                        {
                            "pairs": {
                                "BTC_USD": {
                                    "rate": 50000.0,
                                    "updated_at": stamp,
                                    "source": "test",
                                }
                            },
                            "last_refresh": stamp,
                        },
                    )
                    commands = [
                        'register --username "Test User" --password "secret words"',
                        'login --username "Test User" --password "secret words"',
                        "show-portfolio",
                        "deposit --amount 1000",
                        "buy --currency BTC --amount 0.01 --yes",
                        "sell --currency BTC --amount 0.005 --yes",
                        "show-portfolio",
                        "get-rate --from BTC --to USD",
                        "show-rates --currency BTC",
                        "buy --currency BTC --amount 1 --yes",
                        "buy --currency ZZZ --amount 1 --yes",
                        "deposit --amount nan",
                        "exit",
                    ]
                    try:
                        with (
                            patch("builtins.input", side_effect=commands),
                            patch(
                                "valutatrade_hub.core.usecases.update_rates",
                                side_effect=AssertionError("Сеть не нужна для свежего кеша"),
                            ),
                        ):
                            self.assertEqual(main([]), 0)
                        users = database.read("users")
                        portfolios = database.read("portfolios")
                        self.assertEqual(users[0]["username"], "Test User")
                        self.assertNotIn("password", users[0])
                        wallets = portfolios[0]["wallets"]
                        self.assertAlmostEqual(wallets["USD"]["balance"], 750.0)
                        self.assertAlmostEqual(wallets["BTC"]["balance"], 0.005)
                        self.assertIn("Портфель пуст", self.output.getvalue())
                        self.assertIn("Покупка выполнена", self.output.getvalue())
                        self.assertIn("Продажа выполнена", self.output.getvalue())
                        self.assertIn("Общая стоимость: 1000.00 USD", self.output.getvalue())
                        self.assertIn("Недостаточно", self.output.getvalue())
                        self.assertNotIn("Traceback", self.output.getvalue())
                        self.assertNotIn("secret words", self.output.getvalue())
                    finally:
                        for name in ("actions", "parser"):
                            logger = logging.getLogger(f"valutatrade.{name}")
                            for handler in logger.handlers[:]:
                                logger.removeHandler(handler)
                                handler.close()
        finally:
            SettingsLoader().reload()


if __name__ == "__main__":
    unittest.main()
