"""Изолированные проверки реальных форматов API, отказов и правил хранения."""

import copy
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import to_iso
from valutatrade_hub.parser_service.api_clients import CoinGeckoClient, ExchangeRateApiClient
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.scheduler import run_scheduler
from valutatrade_hub.parser_service.storage import RateStorage
from valutatrade_hub.parser_service.updater import RatesUpdater, UpdateResult

SOURCE_TIME = 1770000000
SOURCE_ISO = to_iso(datetime.fromtimestamp(SOURCE_TIME, UTC))
NETWORK_CALL = "valutatrade_hub.parser_service.api_clients.requests.get"


def configuration(**kwargs):
    """Задать тестовую конфигурацию явно, независимо от API-ключей машины."""
    values = {
        "exchangerate_api_key": "test-private-key",
        "coingecko_api_key": "",
        "coingecko_url": "https://api.coingecko.com/api/v3/simple/price",
        "exchangerate_api_url": "https://v6.exchangerate-api.com/v6/{api_key}/latest/{base}",
        "base_currency": "USD",
        "fiat_currencies": ("EUR",),
        "crypto_currencies": ("BTC",),
        "request_timeout": 7,
    }
    values.update(kwargs)
    return ParserConfig(**values)


def response(payload, status=200):
    """Подготовить ответ HTTP без обращения к сети."""
    return Mock(status_code=status, json=Mock(return_value=payload))


def fiat_payload(**kwargs):
    """Структура Standard API: conversion_rates направлены от USD к EUR."""
    payload = {
        "result": "success",
        "base_code": "USD",
        "conversion_rates": {"EUR": 0.927},
        "time_last_update_unix": SOURCE_TIME,
    }
    payload.update(kwargs)
    return payload


class MemoryDatabase:
    """Воспроизвести контракт мутации callback, не изменяя файлов пользователя."""

    def __init__(self, initial=None):
        self.values = copy.deepcopy(initial or {})
        self.writes = []

    def read(self, name, default=None):
        """Возвращать отдельный снимок, как JSON-хранилище."""
        return copy.deepcopy(self.values.get(name, default))

    def update(self, name, callback, default=None):
        """Записать только успешно выполненную мутацию."""
        value = self.read(name, default)
        result = callback(value)
        self.values[name] = value
        self.writes.append(name)
        return result


class StubClient:
    """Источник с заранее заданным ответом или ошибкой для сценариев объединения."""

    def __init__(self, source="Test", rates=None, timestamp=SOURCE_ISO, error=None):
        self.source = source
        self.rates = rates if rates is not None else {"BTC_USD": 65000.0}
        self.metadata = {pair: {"timestamp": timestamp, "meta": {}} for pair in self.rates}
        self.error = error

    def fetch_rates(self):
        """Возвращать фикстуру исключительно внутри теста."""
        if self.error is not None:
            raise self.error
        return self.rates


class ApiClientTests(unittest.TestCase):
    """Направление курсов, время источника и безопасная обработка ошибок API."""

    @patch(NETWORK_CALL)
    def test_coingecko_ids_and_source_timestamp(self, get):
        get.return_value = response({"bitcoin": {"usd": 65000, "last_updated_at": SOURCE_TIME}})
        client = CoinGeckoClient(configuration())
        self.assertEqual(client.fetch_rates(), {"BTC_USD": 65000.0})
        self.assertEqual(client.metadata["BTC_USD"]["timestamp"], SOURCE_ISO)
        self.assertEqual(get.call_args.kwargs["timeout"], 7)
        self.assertEqual(get.call_args.kwargs["params"]["ids"], "bitcoin")
        self.assertEqual(get.call_args.kwargs["params"]["include_last_updated_at"], "true")

    @patch(NETWORK_CALL)
    def test_fiat_conversion_is_inverted(self, get):
        get.return_value = response(fiat_payload())
        client = ExchangeRateApiClient(configuration())
        self.assertAlmostEqual(client.fetch_rates()["EUR_USD"], 1 / 0.927)
        self.assertEqual(client.metadata["EUR_USD"]["timestamp"], SOURCE_ISO)

    @patch(NETWORK_CALL)
    def test_coingecko_rejects_missing_and_invalid_rates(self, get):
        for value in (None, True, False, 0, -1, float("nan"), float("inf"), "65000"):
            with self.subTest(value=value):
                get.return_value = response(
                    {"bitcoin": {"usd": value, "last_updated_at": SOURCE_TIME}}
                )
                client = CoinGeckoClient(configuration())
                with self.assertRaises(ApiRequestError):
                    client.fetch_rates()
                self.assertEqual(client.metadata, {})
        get.return_value = response({})
        with self.assertRaises(ApiRequestError):
            CoinGeckoClient(configuration()).fetch_rates()

    @patch(NETWORK_CALL)
    def test_missing_source_timestamp_is_not_replaced_with_now(self, get):
        get.return_value = response({"bitcoin": {"usd": 65000}})
        with self.assertRaisesRegex(ApiRequestError, "время обновления"):
            CoinGeckoClient(configuration()).fetch_rates()
        get.return_value = response(fiat_payload(time_last_update_unix=None))
        with self.assertRaisesRegex(ApiRequestError, "время обновления"):
            ExchangeRateApiClient(configuration()).fetch_rates()

    @patch(NETWORK_CALL)
    def test_future_source_timestamp_is_rejected(self, get):
        future = (datetime.now(UTC) + timedelta(days=1)).timestamp()
        get.return_value = response({"bitcoin": {"usd": 65000, "last_updated_at": future}})
        with self.assertRaisesRegex(ApiRequestError, "будущем"):
            CoinGeckoClient(configuration()).fetch_rates()

    @patch(NETWORK_CALL)
    def test_fiat_rejects_missing_and_invalid_rates(self, get):
        for value in (None, True, 0, -1, float("nan"), float("inf"), "0.927"):
            with self.subTest(value=value):
                get.return_value = response(fiat_payload(conversion_rates={"EUR": value}))
                with self.assertRaises(ApiRequestError):
                    ExchangeRateApiClient(configuration()).fetch_rates()
        get.return_value = response(fiat_payload(base_code="EUR"))
        with self.assertRaisesRegex(ApiRequestError, "базовая валюта"):
            ExchangeRateApiClient(configuration()).fetch_rates()

    @patch(NETWORK_CALL)
    def test_missing_key_fails_without_http_or_fallback(self, get):
        with self.assertRaisesRegex(ApiRequestError, "EXCHANGERATE_API_KEY"):
            ExchangeRateApiClient(configuration(exchangerate_api_key="")).fetch_rates()
        get.assert_not_called()

    @patch(NETWORK_CALL)
    def test_timeout_does_not_expose_key_or_url(self, get):
        get.side_effect = requests.Timeout("https://server/test-private-key request failed")
        with self.assertRaises(ApiRequestError) as context:
            ExchangeRateApiClient(configuration()).fetch_rates()
        self.assertIn("время ожидания", str(context.exception))
        self.assertNotIn("test-private-key", str(context.exception))
        self.assertNotIn("https://", str(context.exception))

    @patch(NETWORK_CALL)
    def test_http_and_provider_errors_are_safe(self, get):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                get.return_value = response({"error": "test-private-key"}, status)
                with self.assertRaises(ApiRequestError) as context:
                    ExchangeRateApiClient(configuration()).fetch_rates()
                self.assertIn(str(status), str(context.exception))
                self.assertNotIn("test-private-key", str(context.exception))
        get.return_value = response({"result": "error", "error-type": "test-private-key"})
        with self.assertRaises(ApiRequestError) as context:
            ExchangeRateApiClient(configuration()).fetch_rates()
        self.assertNotIn("test-private-key", str(context.exception))
        get.return_value = response({"result": "error", "error-type": "invalid-key"})
        with self.assertRaisesRegex(ApiRequestError, "ключ недействителен"):
            ExchangeRateApiClient(configuration()).fetch_rates()

    @patch(NETWORK_CALL)
    def test_invalid_json_and_payload_are_domain_errors(self, get):
        get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError("secret")))
        with self.assertRaisesRegex(ApiRequestError, "JSON"):
            ExchangeRateApiClient(configuration()).fetch_rates()
        get.return_value = response([])
        with self.assertRaisesRegex(ApiRequestError, "формат"):
            CoinGeckoClient(configuration()).fetch_rates()


class StorageAndUpdateTests(unittest.TestCase):
    """История, newer-wins и сохранение кэша при отказе всех источников."""

    def setUp(self):
        self.database = MemoryDatabase()
        self.storage = RateStorage(self.database)
        logger_patch = patch("valutatrade_hub.parser_service.updater.get_logger")
        self.logger = logger_patch.start()
        self.addCleanup(logger_patch.stop)

    @staticmethod
    def record(timestamp=SOURCE_ISO, rate=65000):
        return {
            "from_currency": "BTC",
            "to_currency": "USD",
            "rate": rate,
            "timestamp": timestamp,
            "source": "CoinGecko",
            "meta": {"coin_id": "bitcoin"},
        }

    def test_history_deduplicates_and_refresh_keeps_source_time(self):
        record = self.record()
        self.assertEqual(self.storage.save([record, record]), 1)
        self.assertEqual(self.storage.save([record]), 0)
        history = self.database.read("exchange_rates")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], f"BTC_USD_{SOURCE_ISO}")
        snapshot = self.database.read("rates")
        self.assertEqual(snapshot["pairs"]["BTC_USD"]["updated_at"], SOURCE_ISO)
        self.assertIsNotNone(snapshot["last_refresh"])

    def test_newer_timestamp_wins_regardless_of_source_order(self):
        older = self.record("2026-02-02T00:00:00Z", 60000)
        newer = self.record("2026-02-03T00:00:00Z", 70000)
        self.assertEqual(self.storage.save([newer, older]), 1)
        self.assertEqual(self.storage.save([older]), 0)
        snapshot = self.database.read("rates")
        self.assertEqual(snapshot["pairs"]["BTC_USD"]["rate"], 70000)
        self.assertEqual(snapshot["pairs"]["BTC_USD"]["updated_at"], newer["timestamp"])
        self.assertEqual(len(self.database.read("exchange_rates")), 2)

    def test_invalid_batch_does_not_write_either_file(self):
        invalid = self.record(rate=float("nan"))
        with self.assertRaises(ApiRequestError):
            self.storage.save([self.record(), invalid])
        self.assertEqual(self.database.writes, [])

    def test_future_timestamp_and_invalid_metadata_do_not_write(self):
        future = to_iso(datetime.now(UTC) + timedelta(days=1))
        record = self.record(timestamp=future)
        with self.assertRaises(ApiRequestError):
            self.storage.save([record])
        record = self.record()
        record["meta"] = {"invalid": float("nan")}
        with self.assertRaises(ApiRequestError):
            self.storage.save([record])
        self.assertEqual(self.database.writes, [])

    def test_partial_failure_saves_success_and_reports_error(self):
        clients = [
            StubClient(error=ApiRequestError("временная ошибка")),
            StubClient(source="Healthy"),
        ]
        report = RatesUpdater(clients, self.storage, configuration()).run_update()
        self.assertEqual(report.updated, 1)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(self.database.read("rates")["pairs"]["BTC_USD"]["source"], "Healthy")
        self.assertIsNotNone(report.last_refresh)

    def test_all_failures_preserve_cache_and_history(self):
        self.storage.save([self.record()])
        before = copy.deepcopy(self.database.values)
        self.database.writes.clear()
        client = StubClient(error=ApiRequestError("недоступен"))
        with self.assertRaises(ApiRequestError):
            RatesUpdater([client], self.storage, configuration()).run_update()
        self.assertEqual(self.database.values, before)
        self.assertEqual(self.database.writes, [])

    def test_updater_redacts_untrusted_client_error(self):
        client = StubClient(error=ApiRequestError("test-private-key https://api/key"))
        with self.assertRaises(ApiRequestError) as context:
            RatesUpdater([client], self.storage, configuration()).run_update()
        self.assertNotIn("test-private-key", str(context.exception))
        self.assertNotIn("https://", str(context.exception))

    def test_missing_metadata_fails_source_but_continues_other(self):
        invalid = StubClient(source="Broken")
        invalid.metadata = {}
        report = RatesUpdater([invalid, StubClient()], self.storage, configuration()).run_update()
        self.assertEqual(report.updated, 1)
        self.assertIn("метаданные", report.errors[0])

    @patch("valutatrade_hub.parser_service.scheduler.time.sleep")
    @patch("builtins.print")
    def test_scheduler_only_runs_requested_iterations(self, print_mock, sleep):
        updater = Mock()
        updater.run_update.return_value = UpdateResult(1, [], SOURCE_ISO)
        run_scheduler(interval=120, iterations=2, updater=updater)
        self.assertEqual(updater.run_update.call_count, 2)
        sleep.assert_called_once_with(120)


if __name__ == "__main__":
    unittest.main()
