"""Проверки настройки и настоящих JSON-файлов в отдельной временной папке."""

import logging
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class TemporaryStorageTestCase(unittest.TestCase):
    """Изолировать данные, журналы и настройки каждого файлового сценария."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "VALUTATRADE_CONFIG": str(self.directory / "missing.toml"),
                "VALUTATRADE_DATA_DIR": str(self.directory / "data"),
                "VALUTATRADE_LOG_DIR": str(self.directory / "logs"),
                "VALUTATRADE_RATES_TTL_SECONDS": "60",
                "VALUTATRADE_REQUEST_TIMEOUT": "2",
                "VALUTATRADE_BASE_CURRENCY": "USD",
            },
        )
        self.environment.start()
        self.settings = SettingsLoader()
        self.settings.reload()
        self.database = DatabaseManager()

    def tearDown(self):
        for name in ("actions", "parser"):
            logger = logging.getLogger(f"valutatrade.{name}")
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()
        self.environment.stop()
        self.settings.reload()
        self.temporary.cleanup()


class SettingsTests(TemporaryStorageTestCase):
    """Окружение перекрывает файл; reload меняет настройки всех потребителей."""

    def test_singleton_environment_and_reload(self):
        self.assertIs(self.settings, SettingsLoader())
        self.assertEqual(self.directory / "data", self.settings.get("DATA_DIR"))
        self.assertEqual(60, self.settings.get("RATES_TTL_SECONDS"))
        self.assertEqual("fallback", self.settings.get("UNKNOWN", "fallback"))
        other = self.directory / "other"
        with patch.dict(os.environ, {"VALUTATRADE_DATA_DIR": str(other)}):
            self.settings.reload()
            self.database.write("users", [{"user_id": 1}])
        self.assertTrue((other / "users.json").is_file())
        self.assertFalse((self.directory / "data" / "users.json").exists())

    def test_environment_overrides_config_and_config_overrides_default(self):
        config = self.directory / "settings.toml"
        config.write_text(
            '[tool.valutatrade]\nRATES_TTL_SECONDS = 123\nREQUEST_TIMEOUT = 4\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"VALUTATRADE_CONFIG": str(config)}):
            self.settings.reload()
            self.assertEqual(60, self.settings.get("RATES_TTL_SECONDS"))
            os.environ.pop("VALUTATRADE_RATES_TTL_SECONDS")
            self.settings.reload()
            self.assertEqual(123, self.settings.get("RATES_TTL_SECONDS"))

    def test_invalid_ttl_does_not_replace_working_settings(self):
        for raw in ("0", "-1", "nan", "1.5", "True"):
            with (
                self.subTest(value=raw),
                patch.dict(os.environ, {"VALUTATRADE_RATES_TTL_SECONDS": raw}),
                self.assertRaises(ValueError),
            ):
                self.settings.reload()
            self.assertEqual(60, self.settings.get("RATES_TTL_SECONDS"))


class DatabaseTests(TemporaryStorageTestCase):
    """Ошибки не затирают файл, а update защищает совместный доступ процесса."""

    def test_singleton_missing_default_and_unicode_round_trip(self):
        self.assertIs(self.database, DatabaseManager())
        default = {"users": []}
        received = self.database.read("users", default)
        received["users"].append("Анна")
        self.assertEqual({"users": []}, default)
        data = [{"username": "Анна", "user_id": 1}]
        self.database.write("users", data)
        self.assertEqual(data, self.database.read("users"))
        saved = (self.directory / "data" / "users.json").read_text(encoding="utf-8")
        self.assertIn("Анна", saved)
        self.assertEqual([], list((self.directory / "data").glob("*.tmp")))

    def test_storage_name_cannot_escape_data_directory(self):
        for name in ("../users", "users.json", "../../outside", "other"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.database.write(name, [])
        self.assertFalse((self.directory / "outside.json").exists())

    def test_broken_json_is_reported_without_overwrite(self):
        path = self.directory / "data" / "users.json"
        path.parent.mkdir()
        path.write_text('{"username":', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Повреждён файл"):
            self.database.read("users", [])
        self.assertEqual(before, path.read_bytes())

    def test_failed_replace_preserves_old_file_and_removes_temporary(self):
        self.database.write("users", [{"user_id": 1}])
        path = self.directory / "data" / "users.json"
        before = path.read_bytes()
        with (
            patch(
                "valutatrade_hub.infra.database.os.replace",
                side_effect=OSError("симуляция отказа диска"),
            ),
            self.assertRaises(OSError),
        ):
            self.database.write("users", [{"user_id": 2}])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_failed_callback_and_nonfinite_json_preserve_file(self):
        self.database.write("users", [{"user_id": 1}])
        path = self.directory / "data" / "users.json"
        before = path.read_bytes()

        def fail(data):
            data.clear()
            raise ValueError("Отмена")

        with self.assertRaises(ValueError):
            self.database.update("users", fail, [])
        self.assertEqual(before, path.read_bytes())
        with self.assertRaises(ValueError):
            self.database.write("users", [{"value": float("nan")}])
        self.assertEqual(before, path.read_bytes())

    def test_concurrent_updates_do_not_lose_increments(self):
        self.database.write("portfolios", {"count": 0})

        def increment(_):
            def operation(data):
                data["count"] += 1

            self.database.update("portfolios", operation)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(increment, range(20)))
        self.assertEqual(20, self.database.read("portfolios")["count"])


if __name__ == "__main__":
    unittest.main()
