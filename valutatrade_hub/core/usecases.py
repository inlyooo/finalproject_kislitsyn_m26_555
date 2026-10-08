"""Бизнес-операции. CLI отвечает только за ввод и отображение результата."""

from valutatrade_hub.core.currencies import CryptoCurrency, get_currency
from valutatrade_hub.core.exceptions import ApiRequestError, InsufficientFundsError
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.utils import (
    create_rate_cache,
    parse_timestamp,
    read_cached_rate,
    utc_now,
    validate_amount,
)
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader

_resolve_rate = create_rate_cache()


def _read_list(name):
    data = DatabaseManager().read(name, [])
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"Некорректный формат {name}.json")
    return data


def _user(user_id):
    for entry in _read_list("users"):
        if entry.get("user_id") == user_id:
            return User.from_dict(entry)
    raise ValueError("Пользователь не найден. Сначала выполните login")


@log_action
def register(username, password):
    """Создать пользователя и пустой портфель, не сохраняя исходный пароль."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Имя пользователя не может быть пустым")
    username = username.strip()
    database = DatabaseManager()

    with database.transaction():
        users = _read_list("users")
        portfolios = _read_list("portfolios")
        if any(entry.get("username") == username for entry in users):
            raise ValueError(f"Имя пользователя '{username}' уже занято")
        user_id = max((entry["user_id"] for entry in users), default=0) + 1
        user = User.create(user_id, username, password)

        if any(entry.get("user_id") == user_id for entry in portfolios):
            raise ValueError("Найден портфель с занятым user_id. Проверьте данные")
        portfolios.append(Portfolio(user).to_dict())
        users.append(user.to_dict())
        database.write_many({"portfolios": portfolios, "users": users})
        return user


@log_action
def login(username, password):
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Имя пользователя не может быть пустым")
    username = username.strip()
    for entry in _read_list("users"):
        if entry.get("username") == username:
            user = User.from_dict(entry)
            if not user.verify_password(password):
                raise ValueError("Неверный пароль")
            return user
    raise ValueError(f"Пользователь '{username}' не найден")


def get_portfolio(user_id):
    user = _user(user_id)
    for entry in _read_list("portfolios"):
        if entry.get("user_id") == user_id:
            return Portfolio.from_dict(entry, user, rate_getter=get_rate)
    raise ValueError("Портфель пользователя не найден. Проверьте portfolios.json")


def _modify_portfolio(user_id, operation):
    user = _user(user_id)

    def modify(portfolios):
        if not isinstance(portfolios, list):
            raise ValueError("Некорректный формат portfolios.json")
        for index, entry in enumerate(portfolios):
            if entry.get("user_id") == user_id:
                portfolio = Portfolio.from_dict(entry, user, rate_getter=get_rate)
                result = operation(portfolio)
                portfolios[index] = portfolio.to_dict()
                result.update(user_id=user.user_id, username=user.username)
                return result
        raise ValueError("Портфель пользователя не найден")

    return DatabaseManager().update("portfolios", modify, [])


@log_action
def deposit(user_id, currency, amount):
    """Пополнение виртуального USD-кошелька для учебной симуляции."""
    get_currency(currency)
    if currency != "USD":
        raise ValueError("Для симуляции пополняется только USD-кошелёк")
    amount = validate_amount(amount)

    def operation(portfolio):
        wallet = portfolio.add_currency("USD")
        before = wallet.balance
        wallet.deposit(amount)
        return {"currency": currency, "amount": amount, "before": before, "after": wallet.balance}

    return _modify_portfolio(user_id, operation)


def _trade(user_id, currency, amount, selling):
    get_currency(currency)
    if currency == "USD":
        raise ValueError("USD — расчётная валюта. Для пополнения используйте deposit")
    amount = validate_amount(amount)
    rate = get_rate(currency, "USD")["rate"]
    total = validate_amount(amount * rate)

    def operation(portfolio):
        target = portfolio.get_wallet(currency)
        usd = portfolio.get_wallet("USD")
        usd_before = usd.balance if usd else 0.0
        before = target.balance if target else 0.0
        # Все проверки проходят до изменения балансов и создания кошельков.
        if selling:
            if target is None:
                raise ValueError(f"У вас нет кошелька '{currency}'")
            if target.balance < amount:
                raise InsufficientFundsError(target.balance, amount, currency)
            validate_amount(usd_before + total, allow_zero=True)
            usd = portfolio.add_currency("USD")
            target.withdraw(amount)
            usd.deposit(total)
        else:
            if usd_before < total:
                raise InsufficientFundsError(usd_before, total, "USD")
            validate_amount(before + amount, allow_zero=True)
            target = portfolio.add_currency(currency)
            usd.withdraw(total)
            target.deposit(amount)
        return {
            "currency": currency,
            "amount": amount,
            "rate": rate,
            "total": total,
            "before": before,
            "after": target.balance,
            "usd_before": usd_before,
            "usd_after": usd.balance,
        }

    return _modify_portfolio(user_id, operation)


@log_action
def buy(user_id, currency, amount):
    return _trade(user_id, currency, amount, selling=False)


@log_action
def sell(user_id, currency, amount):
    return _trade(user_id, currency, amount, selling=True)


def update_rates(source=None):
    from valutatrade_hub.parser_service.api_clients import CoinGeckoClient, ExchangeRateApiClient
    from valutatrade_hub.parser_service.config import ParserConfig
    from valutatrade_hub.parser_service.updater import RatesUpdater

    config = ParserConfig()
    if source not in (None, "coingecko", "exchangerate"):
        raise ValueError("Источник должен быть coingecko или exchangerate")
    clients = []
    if source in (None, "coingecko"):
        clients.append(CoinGeckoClient(config))
    if source in (None, "exchangerate"):
        clients.append(ExchangeRateApiClient(config))
    return RatesUpdater(clients=clients, config=config).run_update()


def get_rate(from_currency, to_currency):
    """Свежий кэш; при отсутствии или истечении TTL попробовать обновление."""
    get_currency(from_currency)
    get_currency(to_currency)
    database = DatabaseManager()
    ttl = SettingsLoader().get("RATES_TTL_SECONDS")
    snapshot = database.read("rates", {"pairs": {}, "last_refresh": None})
    try:
        return _resolve_rate(from_currency, to_currency, snapshot, ttl)
    except ApiRequestError:
        pass
    error_message = None
    # Для фиатной пары запрос CoinGecko не нужен, для криптовалюты к USD — наоборот.
    codes = (from_currency, to_currency)
    crypto_count = sum(isinstance(get_currency(code), CryptoCurrency) for code in codes)
    source = "exchangerate" if crypto_count == 0 else "coingecko"
    if crypto_count == 1 and "USD" not in codes:
        source = None
    try:
        update_rates(source)
    except ApiRequestError as error:
        error_message = str(error)
    snapshot = database.read("rates", {"pairs": {}, "last_refresh": None})
    try:
        return _resolve_rate(from_currency, to_currency, snapshot, ttl)
    except ApiRequestError as error:
        reason = f"Курс {from_currency}→{to_currency} недоступен или устарел"
        if error_message:
            reason += f". {error_message}"
        raise ApiRequestError(reason) from error


def show_rates(currency=None, top=None, base="USD"):
    """Читать кэш без сетевых запросов, разрешая показ устаревших значений."""
    get_currency(base)
    if currency is not None:
        get_currency(currency)
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top <= 0):
        raise ValueError("--top должен быть положительным целым числом")
    snapshot = DatabaseManager().read("rates", {"pairs": {}, "last_refresh": None})
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pairs"), dict):
        raise ValueError("Некорректный формат rates.json")
    pairs = snapshot["pairs"]
    if not pairs:
        raise ValueError(
            "Локальный кэш курсов пуст. Выполните 'update-rates', чтобы загрузить данные"
        )
    codes = {part for pair in pairs for part in pair.split("_") if part != "USD"}
    if currency is not None:
        codes = {currency} if currency in codes or currency == "USD" else set()
    rows = []
    ttl = SettingsLoader().get("RATES_TTL_SECONDS")
    for code in sorted(codes):
        item = get_currency(code)
        if top is not None and not isinstance(item, CryptoCurrency):
            continue
        record = read_cached_rate(code, base, snapshot)
        age = (utc_now() - parse_timestamp(record["updated_at"])).total_seconds()
        rows.append(
            {
                "currency": code,
                "pair": f"{code}_{base}",
                **record,
                "stale": age > ttl,
            }
        )
    if not rows:
        if currency:
            raise ValueError(f"Курс для '{currency}' не найден в кэше")
        raise ValueError("Курсы криптовалют не найдены в кэше")
    if top is not None:
        rows = sorted(rows, key=lambda row: row["rate"], reverse=True)[:top]
    return rows
