"""Записать фактический CLI на учебных фикстурах через локальный HTTP API.

Цены BTC=50000, ETH=3000, SOL=100 USD и фиатные conversion_rates — учебные
фикстуры, а не текущие рыночные значения. Данные пользователя и журналы
создаются во временном каталоге. usecases, API-клиенты и JSON-хранилище
исполняются без подмены. Для демонстрации подтверждений заменяется только input.
Паузы asciicast рассчитаны для чтения, а не измеряют скорость набора команд.
"""

import argparse
import contextlib
import io
import json
import logging
import math
import os
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from valutatrade_hub.cli.interface import Session
from valutatrade_hub.core.utils import to_iso, utc_now
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader

NOTICE = (
    "УЧЕБНАЯ ДЕМОНСТРАЦИЯ: все цены — локальные фикстуры, не рыночные котировки. "
    "CLI, requests и JSON-хранилище работают реально; HTTP API запущен на 127.0.0.1."
)


@contextlib.contextmanager
def fixture_api():
    """Сервировать форматы CoinGecko/ExchangeRate-API с учебными ценами."""
    timestamp = int((utc_now() - timedelta(seconds=30)).timestamp())
    requested_paths = []

    class FixtureHandler(BaseHTTPRequestHandler):
        """Отдаёт только два локальных API-маршрута."""

        def do_GET(self):
            """Вернуть API-ответ с реальным временем и явно учебными ценами."""
            path = urlsplit(self.path).path
            requested_paths.append(path)
            if path == "/coingecko/simple/price":
                response = {
                    coin: {"usd": price, "last_updated_at": timestamp}
                    for coin, price in (
                        ("bitcoin", 50000.0),
                        ("ethereum", 3000.0),
                        ("solana", 100.0),
                    )
                }
            elif path == "/exchangerate/latest/USD":
                response = {
                    "result": "success",
                    "base_code": "USD",
                    "time_last_update_unix": timestamp,
                    "conversion_rates": {
                        "USD": 1.0,
                        "EUR": 1 / 1.1,
                        "GBP": 0.8,
                        "RUB": 90.0,
                    },
                }
            else:
                self.send_error(404, "Unknown local fixture endpoint")
                return
            payload = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            """Не смешивать служебный HTTP-журнал с записью пользовательского CLI."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        yield address, requested_paths, timestamp
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def close_demo_logs():
    """Закрыть файловые журналы перед удалением временного каталога в Windows."""
    for name in ("actions", "parser"):
        logger = logging.getLogger(f"valutatrade.{name}")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def recording_steps():
    """Вернуть команды, ожидаемые коды результата и ответы на подтверждения."""
    return [
        ("update-rates", 0, ()),
        ("register --username demo --password dummy1234", 0, ()),
        ("login --username demo --password dummy1234", 0, ()),
        ("show-portfolio", 0, ()),
        ("deposit --amount 10000", 0, ()),
        ("buy --currency BTC --amount 0.05", 0, ("y",)),
        ("buy --currency ETH --amount 1", 1, ("n",)),
        ("sell --currency BTC --amount 0.01 --yes", 0, ()),
        ("buy --currency EUR --amount 100 --yes", 0, ()),
        ("show-portfolio --base USD", 0, ()),
        ("show-portfolio --base EUR", 0, ()),
        ("get-rate --from USD --to BTC", 0, ()),
        ("show-rates --top 2", 0, ()),
        ("show-rates --currency RUB", 0, ()),
        ("show-rates --base EUR", 0, ()),
        ("buy --currency BTC --amount 1000 --yes", 1, ()),
        ("buy --currency ZZZ --amount 1 --yes", 1, ()),
        ("deposit --amount nan", 1, ()),
        ("logout", 0, ()),
        ("buy --currency BTC --amount 0.01 --yes", 1, ()),
        ("exit", 0, ()),
    ]


def run_recording():
    """Исполнить настоящий CLI; проверить HTTP-запросы и итоговые JSON-балансы."""
    events = []
    pacing_time = 0.0
    started = time.monotonic()
    created_at = to_iso(utc_now())

    def add_event(command, output, code=0, expected=0):
        nonlocal pacing_time
        event = {
            "time": round(pacing_time, 3),
            "command": command,
            "output": output,
            "expected_code": expected,
            "actual_code": code,
        }
        events.append(event)
        print(output, end="")
        # Режиссёрская пауза для читаемости записи; не время выполнения команды.
        pacing_time += max(2.5, min(9.0, len(output.splitlines()) * 0.38))

    add_event(None, NOTICE + "\n\n")
    try:
        with TemporaryDirectory(prefix="valutatrade-demo-") as directory, fixture_api() as api:
            address, requested_paths, provider_timestamp = api
            environment = {
                "VALUTATRADE_DATA_DIR": str(Path(directory) / "data"),
                "VALUTATRADE_LOG_DIR": str(Path(directory) / "logs"),
                "VALUTATRADE_COINGECKO_URL": address + "/coingecko/simple/price",
                "VALUTATRADE_EXCHANGERATE_API_URL": address + "/exchangerate/latest/{base}",
                "VALUTATRADE_RATES_TTL_SECONDS": "86400",
                "VALUTATRADE_REQUEST_TIMEOUT": "2",
                "VALUTATRADE_FIAT_CURRENCIES": "EUR,GBP,RUB",
                "VALUTATRADE_CRYPTO_CURRENCIES": "BTC,ETH,SOL",
                "EXCHANGERATE_API_KEY": "local-demo-fixture-key",
                "COINGECKO_API_KEY": "",
                "NO_PROXY": "127.0.0.1,localhost",
            }
            with patch.dict(os.environ, environment):
                SettingsLoader().reload()
                session = Session()
                try:
                    for command, expected_code, answers in recording_steps():
                        pending_answers = iter(answers)

                        def answer_input(prompt="", answers=pending_answers):
                            answer = next(answers)
                            print(prompt + answer)
                            return answer

                        captured = io.StringIO()
                        with contextlib.redirect_stdout(captured):
                            print(f">>> {command}")
                            with patch("builtins.input", side_effect=answer_input):
                                code = session.execute(command, interactive=True)
                        output = captured.getvalue() + "\n"
                        add_event(command, output, code, expected_code)
                        assert code == expected_code, (command, code, expected_code, output)
                        assert "Traceback" not in output
                    database = DatabaseManager()
                    users = database.read("users", [])
                    portfolios = database.read("portfolios", [])
                    cache = database.read("rates", {})
                    history = database.read("exchange_rates", [])
                    wallets = portfolios[0]["wallets"]
                    balances = {code: wallet["balance"] for code, wallet in wallets.items()}
                    for code, expected in {"USD": 7890.0, "BTC": 0.04, "EUR": 100.0}.items():
                        assert math.isclose(balances[code], expected, rel_tol=1e-12)
                    assert "ETH" not in balances, "Отменённая покупка создала кошелёк"
                    assert len(users) == 1 and users[0]["username"] == "demo"
                    assert "password" not in users[0]
                    assert len(cache["pairs"]) == 6
                    assert len(history) == 6
                    assert requested_paths == [
                        "/coingecko/simple/price",
                        "/exchangerate/latest/USD",
                    ], requested_paths
                    verification = {
                        "balances": balances,
                        "cached_pairs": len(cache["pairs"]),
                        "history_records": len(history),
                        "http_requests": requested_paths,
                        "provider_timestamp": provider_timestamp,
                        "cancel_preserved_wallets": "ETH" not in balances,
                        "invalid_operations_preserved_balances": True,
                        "session_logged_out": session.current_user is None,
                        "session_exited": not session.running,
                    }
                    proof = io.StringIO()
                    with contextlib.redirect_stdout(proof):
                        print("Проверка фактических JSON завершена:")
                        print(json.dumps(verification, ensure_ascii=False, indent=2))
                        print("Все команды и итоговые балансы соответствуют проверкам.")
                    add_event(None, proof.getvalue())
                finally:
                    close_demo_logs()
    finally:
        SettingsLoader().reload()
    metadata = {
        "title": "ValutaTrade Hub — реальные команды на учебных фикстурах",
        "created_at": created_at,
        "data_mode": "local_http_fixture",
        "notice": NOTICE,
        "pacing": "Время событий рассчитано для чтения; скорость набора не измерялась.",
        "real_elapsed_seconds": round(time.monotonic() - started, 3),
        "display_duration_seconds": round(pacing_time, 3),
        "width": 110,
        "height": 30,
    }
    return {"metadata": metadata, "events": events, "verification": verification}


def save_recording(recording, directory):
    """Сохранить одни и те же фактически выведенные строки в трёх форматах."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "demo-events.json").write_text(
        json.dumps(recording, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "demo-transcript.txt").write_text(
        "".join(event["output"] for event in recording["events"]), encoding="utf-8"
    )
    header = {
        "version": 2,
        "width": recording["metadata"]["width"],
        "height": recording["metadata"]["height"],
        "timestamp": int(time.time()),
        "title": recording["metadata"]["title"],
        "env": {"TERM": "xterm-256color", "SHELL": "python"},
    }
    rows = [json.dumps(header, ensure_ascii=False)]
    rows.extend(
        json.dumps([event["time"], "o", event["output"].replace("\n", "\r\n")], ensure_ascii=False)
        for event in recording["events"]
    )
    (directory / "demo.cast").write_text("\n".join(rows) + "\n", encoding="utf-8")


def main():
    """Записать демо в docs либо в явно переданный каталог."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    save_recording(run_recording(), arguments.output_dir)


if __name__ == "__main__":
    main()
