"""JSON-файлы с атомарной заменой и защитой от порчи данных."""

import copy
import json
import os
import tempfile
from threading import RLock

from valutatrade_hub.infra.settings import SettingsLoader


class DatabaseManager:
    """Singleton для чтения и записи файлов одного процесса CLI."""

    _instance = None
    NAMES = {"users", "portfolios", "rates", "exchange_rates"}

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._lock = RLock()
            cls._instance = instance
        return cls._instance

    def _path(self, name):
        if name not in self.NAMES:
            raise ValueError(f"Неизвестное хранилище '{name}'")
        return SettingsLoader().get("DATA_DIR") / f"{name}.json"

    def read(self, name, default=None):
        """Отсутствующий файл возвращает default, повреждённый вызывает ошибку."""
        with self._lock:
            path = self._path(name)
            if not path.exists():
                return copy.deepcopy(default)
            try:
                with path.open(encoding="utf-8") as file:
                    return json.load(file)
            except (json.JSONDecodeError, UnicodeError) as error:
                raise ValueError(
                    f"Повреждён файл {path.name}. Исправьте его или восстановите копию"
                ) from error

    def write(self, name, data):
        """Записать временный файл рядом с исходным и заменить через os.replace."""
        with self._lock:
            path = self._path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{name}-",
                    suffix=".tmp",
                    delete=False,
                ) as file:
                    temporary_path = file.name
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, path)
            finally:
                if temporary_path and os.path.exists(temporary_path):
                    os.unlink(temporary_path)

    def update(self, name, callback, default=None):
        """Выполнить read/modify/write под блокировкой в рамках процесса."""
        with self._lock:
            data = self.read(name, default)
            result = callback(data)
            self.write(name, data)
            return result
