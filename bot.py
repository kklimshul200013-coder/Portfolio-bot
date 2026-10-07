import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# НАСТРОЙКИ КАЛЬКУЛЯТОРА
# =========================
FUND_SHARE: Final[float] = 0.20
CBR_URL: Final[str] = "https://www.cbr.ru/hd_base/zcyc_params/"
CACHE_HOURS: Final[int] = 6

# Точки КБД ОФЗ, публикуемые Банком России
CBR_TERMS: Final[tuple[float, ...]] = (
    0.25, 0.50, 0.75, 1.00, 2.00, 3.00,
    5.00, 7.00, 10.00, 15.00, 20.00, 30.00,
)

# Резервная кривая на случай временной недоступности сайта ЦБ.
# Данные Банка России на 06.10.2026.
FALLBACK_CURVE_DATE: Final[str] = "06.10.2026"
FALLBACK_CURVE: Final[dict[float, float]] = {
    0.25: 0.1119,
    0.50: 0.1217,
    0.75: 0.1295,
    1.00: 0.1357,
    2.00: 0.1511,
    3.00: 0.1582,
    5.00: 0.1640,
    7.00: 0.1663,
    10.00: 0.1681,
    15.00: 0.1694,
    20.00: 0.1701,
    30.00: 0.1707,
}

_curve_cache = {
    "fetched_at": None,
    "date": FALLBACK_CURVE_DATE,
    "curve": FALLBACK_CURVE.copy(),
    "is_fallback": True,
}

AMOUNT, TERM = range(2)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class TableParser(HTMLParser):
    """Собирает текст ячеек HTML-таблиц в строки."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data):
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell_parts is not None:
            cell = " ".join("".join(self._cell_parts).replace("\xa0", " ").split())
            if self._row is not None:
                self._row.append(cell)
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell_parts = None


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def parse_number(value: str) -> float:
    cleaned = (
        value.replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
        .replace("%", "")
        .strip()
    )
    return float(cleaned)


def fetch_cbr_curve() -> tuple[str, dict[float, float]]:
    """
    Загружает последние доступные значения КБД ОФЗ с сайта Банка России.
    Запрашивается окно последних 14 дней, чтобы корректно переживать выходные
    и праздничные дни.
    """
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=14)

    params = {
        "UniDbQuery.Posted": "True",
        "UniDbQuery.From": date_from.strftime("%d.%m.%Y"),
        "UniDbQuery.To": today.strftime("%d.%m.%Y"),
    }
    url = f"{CBR_URL}?{urlencode(params)}"

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; PortfolioCalculatorBot/2.0; "
                "+https://www.cbr.ru/)"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )

    with urlopen(request, timeout=12) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(html)

    date_pattern = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

    for row in parser.rows:
        if len(row) < 13 or not date_pattern.fullmatch(row[0]):
            continue

        try:
            values = [parse_number(x) / 100 for x in row[1:13]]
        except ValueError:
            continue

        if len(values) != len(CBR_TERMS):
            continue

        curve = dict(zip(CBR_TERMS, values))
        return row[0], curve

    raise ValueError("На странице Банка России не найдена строка с данными КБД.")


def get_cbr_curve() -> tuple[str, dict[float, float], bool]:
    """
    Возвращает актуальную кривую.
    True в третьем значении означает, что используются резервные/сохранённые данные.
    """
    now = datetime.now(timezone.utc)
    fetched_at = _curve_cache["fetched_at"]

    if (
        fetched_at is not None
        and now - fetched_at < timedelta(hours=CACHE_HOURS)
    ):
        return (
            _curve_cache["date"],
            _curve_cache["curve"],
            _curve_cache["is_fallback"],
        )

    try:
        curve_date, curve = fetch_cbr_curve()
        _curve_cache.update(
            {
                "fetched_at": now,
                "date": curve_date,
                "curve": curve,
                "is_fallback": False,
            }
        )
        logger.info("CBR curve updated: %s", curve_date)
    except Exception:
        logger.exception("Failed to update CBR curve; using cached/fallback data")
        _curve_cache["fetched_at"] = now

    return (
        _curve_cache["date"],
        _curve_cache["curve"],
        _curve_cache["is_fallback"],
    )


def rate_for_term(years: float, curve: dict[float, float]) -> float:
    """Линейная интерполяция КБД для произвольного срока от 1 до 30 лет."""
    points = sorted(curve)

    if years <= points[0]:
        return curve[points[0]]
    if years >= points[-1]:
        return curve[points[-1]]

    for left, right in zip(points, points[1:]):
        if left <= years <= right:
            if years == left:
                return curve[left]
            if years == right:
                return curve[right]

            weight = (years - left) / (right - left)
            return curve[left] + weight * (curve[right] - curve[left])

    raise ValueError("Не удалось определить ставку для выбранного срока.")


def calculate_portfolio(amount: float, years: float, bond_rate: float):
    """
    Сохраняем текущую утверждённую модель:
    20% — фонды, оставшиеся 80% делятся между облигациями и акциями.
    Меняется только ставка — теперь она зависит от срока по КБД ОФЗ.
    """
    funds = amount * FUND_SHARE
    base = amount - funds

    bonds = base / (1 + bond_rate * years)
    stocks = base - bonds

    return {
        "funds": funds,
        "bonds": bonds,
        "stocks": stocks,
        "funds_pct": funds / amount,
        "bonds_pct": bonds / amount,
        "stocks_pct": stocks / amount,
    }


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Рассчитать портфель", callback_data="calculate")]]
    )


def term_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 год", callback_data="term:1"),
            InlineKeyboardButton("3 года", callback_data="term:3"),
            InlineKeyboardButton("5 лет", callback_data="term:5"),
        ],
        [
            InlineKeyboardButton("10 лет", callback_data="term:10"),
            InlineKeyboardButton("15 лет", callback_data="term:15"),
            InlineKeyboardButton("20 лет", callback_data="term:20"),
        ],
        [InlineKeyboardButton("✏️ Другой срок", callback_data="term:custom")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! 👋\n\n"
        "Я помогу рассчитать структуру портфеля по нашей модели:\n"
        "20% — фонды, а оставшаяся часть распределяется между "
        "облигациями и акциями с учётом срока инвестирования.\n\n"
        "Доходность для расчёта определяется автоматически по актуальной "
        "кривой бескупонной доходности ОФЗ Банка России.\n\n"
        "Нажмите кнопку ниже, чтобы начать."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_keyboard())
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=main_keyboard())


async def begin_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите сумму портфеля в рублях.\n\n"
        "Например: 300000"
    )
    return AMOUNT


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        amount = float(raw)
    except ValueError:
        await update.message.reply_text("Введите сумму числом. Например: 300000")
        return AMOUNT

    if amount <= 0 or amount > 1_000_000_000_000:
        await update.message.reply_text("Введите корректную сумму больше 0.")
        return AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text(
        f"Сумма: {money(amount)}\n\nТеперь выберите срок:",
        reply_markup=term_keyboard(),
    )
    return TERM


async def receive_term_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]

    if value == "custom":
        await query.edit_message_text(
            "Введите срок в годах от 1 до 30.\n\nНапример: 7"
        )
        return TERM

    years = float(value)
    await show_result(query, context, years)
    return ConversationHandler.END


async def receive_custom_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.replace(",", ".").strip()
    try:
        years = float(raw)
    except ValueError:
        await update.message.reply_text("Введите срок числом. Например: 7")
        return TERM

    if years < 1 or years > 30:
        await update.message.reply_text("Введите срок от 1 до 30 лет.")
        return TERM

    await show_result(update.message, context, years, is_message=True)
    return ConversationHandler.END


async def show_result(
    target,
    context: ContextTypes.DEFAULT_TYPE,
    years: float,
    is_message: bool = False,
):
    amount = context.user_data["amount"]

    curve_date, curve, is_fallback = get_cbr_curve()
    bond_rate = rate_for_term(years, curve)
    result = calculate_portfolio(amount, years, bond_rate)

    years_text = f"{years:g}".replace(".", ",")
    rate_text = pct(bond_rate)

    data_note = (
        f"Данные Банка России на {curve_date}."
        if not is_fallback
        else f"Использованы последние сохранённые данные Банка России на {curve_date}."
    )

    text = (
        "📊 СТРУКТУРА ПОРТФЕЛЯ\n\n"
        f"Сумма: {money(amount)}\n"
        f"Срок: {years_text} лет\n"
        f"Расчётная доходность на выбранный срок: {rate_text} годовых\n\n"
        f"💲 Фонды — {pct(result['funds_pct'])} · {money(result['funds'])}\n"
        f"📈 Облигации — {pct(result['bonds_pct'])} · {money(result['bonds'])}\n"
        f"🚀 Акции — {pct(result['stocks_pct'])} · {money(result['stocks'])}\n\n"
        "ℹ️ Доходность определяется автоматически по кривой бескупонной "
        "доходности ОФЗ Банка России. Для промежуточных сроков используется "
        "линейная интерполяция.\n"
        f"{data_note}\n\n"
        "Расчёт носит модельный характер и не является индивидуальной "
        "инвестиционной рекомендацией."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Рассчитать заново", callback_data="calculate")],
        [InlineKeyboardButton("🏠 В начало", callback_data="home")],
    ])

    if is_message:
        await target.reply_text(text, reply_markup=keyboard)
    else:
        await target.edit_message_text(text, reply_markup=keyboard)


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Расчёт отменён.", reply_markup=main_keyboard())
    return ConversationHandler.END


def run():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Создайте переменную окружения с токеном от BotFather."
        )

    app = Application.builder().token(token).build()

    conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_calculation, pattern=r"^calculate$")],
        states={
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)
            ],
            TERM: [
                CallbackQueryHandler(receive_term_button, pattern=r"^term:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_term),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(home, pattern=r"^home$"))
    app.add_handler(conversation)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
