# ValutaTrade Hub

Итоговый проект по Python: виртуальный валютный кошелёк с покупкой и
продажей валют. Курсы поступают из CoinGecko и ExchangeRate-API.

Кислицын Дмитрий Геннадьевич, группа М26-555.

## Запуск

Нужны Python 3.12+ и uv. Из папки проекта:

```powershell
uv sync --frozen
uv run project
```

Для фиатных курсов нужен ключ [ExchangeRate-API](https://www.exchangerate-api.com/).
В PowerShell его можно задать перед запуском:

```powershell
$env:EXCHANGERATE_API_KEY = "ваш-ключ"
```

В Linux/macOS: `export EXCHANGERATE_API_KEY="ваш-ключ"`.
CoinGecko работает без ключа. Файл `.env` автоматически не загружается.

## Пример работы

Команды вводятся в одной сессии приложения:

```text
update-rates
register --username alice --password demo1234
login --username alice --password demo1234
deposit --amount 10000
buy --currency BTC --amount 0.01 --yes
sell --currency BTC --amount 0.005 --yes
show-portfolio --base USD
get-rate --from USD --to BTC
show-rates --top 2
show-rates --currency EUR --base USD
logout
exit
```

Новый портфель пустой. `deposit` пополняет виртуальные USD, покупка списывает
их, продажа зачисляет обратно. `amount` — количество валюты, а не сумма в USD.
Без `--yes` сделку нужно подтвердить. Все команды доступны через `help`.

## Устройство проекта

- `core` — пользователи, кошельки и операции.
- `infra` — настройки и JSON-хранилище.
- `parser_service` — получение и сохранение курсов.
- `cli` — консольный интерфейс.

Данные хранятся в `data/`, журналы — в `logs/`.
Настройки находятся в `[tool.valutatrade]` файла `pyproject.toml`.
`rates.json` хранит последние курсы, `exchange_rates.json` — историю.
Срок актуальности по времени источника — сутки; изменить его можно через
`VALUTATRADE_RATES_TTL_SECONDS`. Перед сделкой старый курс обновляется,
а `show-rates` показывает кэш и отмечает устаревшие значения.

Парсер можно запустить отдельно, раз в час до Ctrl+C:

```powershell
uv run parser --interval 3600
```

## Проверки и сборка

```powershell
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
uv build
```

В Makefile есть команды `install`, `run`, `lint`, `test`, `build`
и `package-install`. Сборки появляются в `dist/`.

## Демонстрация

[GIF](docs/demo.gif) · [asciicast](docs/demo.cast)

Для повторения: `uv run python docs/demo.py`.
Демо использует учебные цены и отдельный временный каталог.
