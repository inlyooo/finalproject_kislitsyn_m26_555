"""Явно запускаемый пользователем цикл обновления; импорт не запускает задачи."""

import argparse
import math
import time

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import configure_console_encoding
from valutatrade_hub.parser_service.updater import RatesUpdater


def run_scheduler(interval=3600, iterations=None, updater=None):
    """Обновлять курсы с паузой до Ctrl+C или заданного числа итераций."""
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError("Интервал должен быть положительным числом секунд")
    if iterations is not None and (
        isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0
    ):
        raise ValueError("Число итераций должно быть положительным целым числом")
    updater = updater if updater is not None else RatesUpdater()
    count = 0
    while iterations is None or count < iterations:
        try:
            report = updater.run_update()
            print(f"Обновлено пар: {report.updated}. Последняя проверка: {report.last_refresh}")
            for error in report.errors:
                print(f"Предупреждение: {error}")
        except ApiRequestError as error:
            print(f"Ошибка обновления: {error}")
        count += 1
        if iterations is None or count < iterations:
            time.sleep(interval)


def main(argv=None):
    """Запустить scheduler только по отдельной команде пользователя."""
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="Периодическое обновление курсов ValutaTrade Hub")
    parser.add_argument("--interval", type=int, default=3600, help="пауза между запросами, секунды")
    parser.add_argument("--iterations", type=int, help="число опросов; по умолчанию до Ctrl+C")
    arguments = parser.parse_args(argv)
    try:
        run_scheduler(arguments.interval, arguments.iterations)
    except ValueError as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("Обновление остановлено пользователем.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
