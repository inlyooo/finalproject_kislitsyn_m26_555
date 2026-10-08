"""Разбор команд, сессия пользователя и русскоязычный консольный интерфейс."""

import argparse
import shlex
import sys

from prettytable import PrettyTable

from valutatrade_hub.core import usecases
from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.decorators import confirm_action


class CommandParseError(ValueError):
    """Ошибка аргументов команды, которая не завершает консольный цикл."""


class CommandParser(argparse.ArgumentParser):
    """ArgumentParser без завершения процесса при ошибках пользовательского ввода."""

    def format_help(self):
        """Переводит стандартные подписи справки на русский."""
        return (
            super()
            .format_help()
            .replace("usage:", "Использование:")
            .replace("positional arguments:", "Команды:")
            .replace("options:", "Аргументы:")
            .replace("show this help message and exit", "показать справку")
        )

    def error(self, message):
        """Передаёт сообщение об ошибке обработчику консольного интерфейса."""
        for original, translated in (
            ("the following arguments are required:", "Не хватает аргументов:"),
            ("unrecognized arguments:", "Неизвестные аргументы:"),
            ("invalid choice:", "Недопустимое значение:"),
            ("(choose from", "(допустимы"),
            ("expected one argument", "ожидается одно значение"),
            ("invalid int value:", "ожидается целое число:"),
            ("invalid float value:", "ожидается число:"),
            ("argument", "аргумент"),
        ):
            message = message.replace(original, translated)
        raise CommandParseError(message)

    def exit(self, status=0, message=None):
        """Передаёт стандартный --help обратно в текущую сессию."""
        if message:
            self._print_message(message)
        if status:
            raise CommandParseError(message or "Неверные аргументы команды.")
        raise HelpRequested


class HelpRequested(Exception):
    """Пользователь запросил справку через --help."""


def build_parser():
    """Создаёт единое описание интерактивных и однократных команд."""
    parser = CommandParser(
        prog="project",
        description="ValutaTrade Hub — симуляция торговли валютами.",
    )
    commands = parser.add_subparsers(dest="command", metavar="команда")
    for name, description in (
        ("register", "зарегистрировать пользователя"),
        ("login", "войти в текущую сессию"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--username", required=True, help="имя пользователя")
        command.add_argument("--password", required=True, help="пароль")
    portfolio = commands.add_parser("show-portfolio", help="показать портфель")
    portfolio.add_argument("--base", default="USD", help="валюта оценки, USD")
    for name, description in (
        ("buy", "купить валюту за виртуальные USD"),
        ("sell", "продать валюту за виртуальные USD"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--currency", required=True, help="код валюты")
        command.add_argument("--amount", type=float, required=True, help="количество валюты")
        command.add_argument("--yes", action="store_true", help="подтвердить сделку")
    rate = commands.add_parser("get-rate", help="показать прямой и обратный курсы")
    rate.add_argument("--from", dest="from_currency", required=True)
    rate.add_argument("--to", dest="to_currency", required=True)
    update = commands.add_parser("update-rates", help="обновить курсы из API")
    update.add_argument("--source", help="источник: coingecko или exchangerate")
    rates = commands.add_parser("show-rates", help="показать кеш курсов")
    rates.add_argument("--currency", help="фильтр по валюте")
    rates.add_argument("--top", type=int, help="показать первые N курсов")
    rates.add_argument("--base", default="USD", help="валюта котировки, USD")
    deposit = commands.add_parser("deposit", help="пополнить виртуальный USD")
    deposit.add_argument("--amount", type=float, required=True, help="сумма пополнения")
    deposit.add_argument("--currency", default="USD", help="виртуальный USD")
    commands.add_parser("logout", help="завершить сессию пользователя")
    commands.add_parser("help", help="показать справку")
    commands.add_parser("exit", help="закрыть приложение")
    return parser


def format_amount(value):
    """Форматирует баланс без лишних нулей и обнуления малых сумм."""
    return f"{float(value):.12g}"


@confirm_action(
    lambda action, user_id, currency, amount: (
        f"Подтвердить {'покупку' if action == 'buy' else 'продажу'} {amount} {currency}?"
    )
)
def execute_trade(action, user_id, currency, amount):
    """Передаёт подтверждённую сделку в слой сценариев приложения."""
    return getattr(usecases, action)(user_id, currency, amount)


class Session:
    """Хранит авторизацию только в памяти процесса и исполняет команды."""

    def __init__(self):
        """Создаёт сессию без вошедшего пользователя."""
        self.current_user = None
        self.running = True
        self.parser = build_parser()

    def require_user(self):
        """Возвращает пользователя или предлагает выполнить login."""
        if self.current_user is None:
            raise ValueError("Сначала выполните login --username ... --password ...")
        return self.current_user

    def execute(self, command, interactive=False):
        """Исполняет строку или argv; возвращает код результата без traceback."""
        try:
            if isinstance(command, str):
                try:
                    argv = shlex.split(command)
                except ValueError as error:
                    raise CommandParseError(
                        "Не закрыты кавычки или не завершено экранирование."
                    ) from error
            else:
                argv = list(command)
            if not argv:
                return 0
            args = self.parser.parse_args(argv)
            self.dispatch(args)
            return 0
        except HelpRequested:
            return 0
        except (ValueError, RuntimeError, OSError) as error:
            print(f"Ошибка: {error}")
            return 1
        except KeyboardInterrupt:
            print("Операция отменена.")
            return 1

    def dispatch(self, args):
        """Вызывает сценарий и формирует сообщение о результате."""
        command = args.command
        if command == "register":
            user = usecases.register(args.username, args.password)
            print(
                f"Пользователь '{user.username}' зарегистрирован "
                f"(id={user.user_id}). Выполните login."
            )
        elif command == "login":
            self.current_user = usecases.login(args.username, args.password)
            print(f"Вы вошли как '{self.current_user.username}'.")
        elif command == "logout":
            self.current_user = None
            print("Сессия пользователя завершена.")
        elif command == "show-portfolio":
            user = self.require_user()
            self.show_portfolio(usecases.get_portfolio(user.user_id), args.base)
        elif command in ("buy", "sell"):
            user = self.require_user()
            result = execute_trade(
                command,
                user.user_id,
                args.currency,
                args.amount,
                confirmed=args.yes,
            )
            self.show_trade(command, result)
        elif command == "deposit":
            user = self.require_user()
            result = usecases.deposit(user.user_id, args.currency, args.amount)
            print(
                f"Виртуальный баланс пополнен на {format_amount(result['amount'])} "
                f"{result['currency']}. Баланс: {format_amount(result['after'])}."
            )
        elif command == "get-rate":
            rate = usecases.get_rate(args.from_currency, args.to_currency)
            print(
                f"Курс {args.from_currency}→{args.to_currency}: "
                f"{float(rate['rate']):.12g}; обновлён: {rate['updated_at']}; "
                f"источник: {rate.get('source', 'кеш')}."
            )
            print(
                f"Обратный курс {args.to_currency}→{args.from_currency}: "
                f"{1 / float(rate['rate']):.12g}."
            )
        elif command == "update-rates":
            report = usecases.update_rates(args.source)
            print(
                f"Обновлено курсов: {report.updated}. "
                f"Последнее обновление: {report.last_refresh or 'нет данных'}."
            )
            for error in report.errors:
                print(f"Предупреждение: {error}")
        elif command == "show-rates":
            rates = usecases.show_rates(args.currency, args.top, args.base)
            self.show_rates(rates)
        elif command == "help":
            self.parser.print_help()
            print(
                "После register выполните login. Для симуляции пополните USD "
                "командой deposit --amount 10000. "
                "Коды валют вводятся заглавными: USD, EUR, BTC. "
                "Аргументы с пробелами заключайте в кавычки."
            )
        elif command == "exit":
            self.running = False
            print("До свидания!")

    @staticmethod
    def show_trade(action, result):
        """Показывает изменение валютного и долларового кошельков."""
        print(
            f"{'Покупка' if action == 'buy' else 'Продажа'} выполнена: "
            f"{format_amount(result['amount'])} {result['currency']} "
            f"по курсу {float(result['rate']):.12g} USD. "
            f"{'Стоимость' if action == 'buy' else 'Выручка'}: "
            f"{float(result['total']):.2f} USD."
        )
        print(
            f"{result['currency']}: {format_amount(result['before'])} → "
            f"{format_amount(result['after'])}; "
            f"USD: {format_amount(result['usd_before'])} → "
            f"{format_amount(result['usd_after'])}."
        )

    @staticmethod
    def show_portfolio(portfolio, base):
        """Показывает кошельки и оценку; сообщает о недоступных курсах."""
        get_currency(base)
        print(f"Портфель пользователя '{portfolio.user.username}' (база: {base}):")
        wallets = portfolio.wallets
        if not wallets:
            print("Портфель пуст. Пополните виртуальный USD: deposit --amount 10000.")
            return
        table = PrettyTable(["Валюта", "Баланс", f"Стоимость в {base}"])
        warnings = []
        total = 0.0
        for code, wallet in sorted(wallets.items()):
            try:
                if wallet.balance == 0:
                    value = "0.00"
                else:
                    rate = usecases.get_rate(code, base)
                    valuation = wallet.balance * float(rate["rate"])
                    total += valuation
                    value = f"{valuation:.2f}"
            except (ValueError, RuntimeError, OSError) as error:
                value = "нет свежего курса"
                warnings.append(f"{code}: {error}")
            table.add_row([code, format_amount(wallet.balance), value])
        print(table)
        if warnings:
            print("Общая стоимость недоступна: для части кошельков нет свежего курса.")
        else:
            print(f"Общая стоимость: {total:.2f} {base}.")
        for warning in warnings:
            print(f"Предупреждение: {warning}")

    @staticmethod
    def show_rates(rates):
        """Выводит кеш курсов с датой, источником и признаком устаревания."""
        table = PrettyTable(["Пара", "Курс", "Обновлён", "Источник", "Состояние"])
        for rate in rates:
            table.add_row(
                [
                    rate["pair"],
                    f"{float(rate['rate']):.12g}",
                    rate["updated_at"],
                    rate["source"],
                    "устарел" if rate["stale"] else "свежий",
                ]
            )
        print(table)
        if not rates:
            print("Подходящих курсов нет. Выполните update-rates.")


def main(argv=None):
    """Запускает интерактивную сессию либо одну команду из argv."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    session = Session()
    if arguments:
        return session.execute(arguments)
    print("ValutaTrade Hub. Введите help для справки, exit для выхода.")
    while session.running:
        try:
            text = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            print("\nДо свидания!")
            break
        session.execute(text, interactive=True)
    return 0


def run():
    """Запускает интерактивную сессию из корневого main.py."""
    return main([])
