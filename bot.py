import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

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
CBR_SOAP_URL: Final[str] = "https://www.cbr.ru/secinfo/secinfo.asmx"
CBR_SOAP_ACTION: Final[str] = "http://web.cbr.ru/zcyc_paramsXML"
CACHE_HOURS: Final[int] = 6
WELCOME_IMAGE: Final[Path] = Path(__file__).with_name("welcome.jpg")

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


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def parse_number(value: str) -> float:
    cleaned = (
        str(value)
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
        .replace("%", "")
        .strip()
    )
    return float(cleaned)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].strip()


def _parse_date(value: str):
    value = str(value).strip()
    candidates = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
        "%d.%m.%Y",
    )
    clean = value.replace("Z", "")
    if "+" in clean[10:]:
        clean = clean.split("+", 1)[0]

    for fmt in candidates:
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    return None


def _yield_from_params(
    years: float,
    b1: float,
    b2: float,
    b3: float,
    t1: float,
    g_values: list[float],
) -> float:
    """
    Расчёт КБД из параметров G-кривой.
    Результат возвращается долей: например 0.135 = 13,5%.
    """
    t = float(years)
    tau = float(t1)
    if t <= 0 or tau <= 0:
        raise ValueError("Некорректные параметры КБД.")

    exp_part = math.exp(-t / tau)
    term1 = b1 + b2 * tau * (1 - exp_part) / t
    term2 = b3 * ((1 - exp_part) * tau / t - exp_part)

    # Корректирующие члены G-кривой.
    a_values = [0.0] * 9
    b_values = [0.0] * 9
    a_values[0] = 0.0
    a_values[1] = 0.6
    b_values[0] = 0.6
    k = 1.6

    for i in range(2, 9):
        a_values[i] = a_values[i - 1] + k ** (i - 1)
        b_values[i - 1] = b_values[i - 2] * k
    b_values[8] = b_values[7] * k

    correction = 0.0
    for i, g in enumerate(g_values[:9]):
        width = b_values[i]
        if width > 0:
            correction += g * math.exp(-((t - a_values[i]) ** 2) / (width ** 2))

    continuously_compounded = (term1 + term2 + correction) / 10000.0
    annual_effective_pct = 100.0 * (math.exp(continuously_compounded) - 1.0)
    return annual_effective_pct / 100.0


def _extract_rows(root: ET.Element) -> list[dict[str, str]]:
    """
    Извлекает табличные строки из SOAP/DataSet независимо от namespace.
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for elem in root.iter():
        children = list(elem)
        if not children:
            continue

        row: dict[str, str] = {}
        for child in children:
            if list(child):
                continue
            value = (child.text or "").strip()
            if value:
                row[_local_name(child.tag).upper()] = value

        if len(row) < 4:
            continue

        marker = tuple(row.items())
        if marker not in seen:
            seen.add(marker)
            rows.append(row)

    return rows


def _curve_from_row(row: dict[str, str]):
    """
    Поддерживает два варианта ответа Банка России:
    1) готовые значения КБД по стандартным срокам;
    2) параметры B1/B2/B3/T1/G1...G9, из которых строится КБД.
    """
    row_date = None
    for value in row.values():
        parsed = _parse_date(value)
        if parsed is not None:
            row_date = parsed
            break

    if row_date is None:
        return None

    # Вариант с параметрами G-кривой.
    parameter_keys = ["B1", "B2", "B3", "T1"] + [f"G{i}" for i in range(1, 10)]
    if all(key in row for key in parameter_keys):
        try:
            b1 = parse_number(row["B1"])
            b2 = parse_number(row["B2"])
            b3 = parse_number(row["B3"])
            t1 = parse_number(row["T1"])
            g_values = [parse_number(row[f"G{i}"]) for i in range(1, 10)]
            curve = {
                term: _yield_from_params(term, b1, b2, b3, t1, g_values)
                for term in CBR_TERMS
            }
            if all(0.0 < value < 1.0 for value in curve.values()):
                return row_date, curve
        except (ValueError, OverflowError):
            pass

    # Вариант, когда сервис вернул сразу 12 значений доходности.
    numbers: list[float] = []
    for value in row.values():
        if _parse_date(value) is not None:
            continue
        try:
            number = parse_number(value)
        except ValueError:
            continue
        numbers.append(number)

    if len(numbers) == len(CBR_TERMS) and all(0 < n < 100 for n in numbers):
        curve = {
            term: value / 100.0
            for term, value in zip(CBR_TERMS, numbers)
        }
        return row_date, curve

    return None


def fetch_cbr_curve() -> tuple[str, dict[float, float]]:
    """
    Получает КБД через официальный SOAP-веб-сервис Банка России SecInfo.
    Запрашиваем последние 14 дней, чтобы переживать выходные и праздники.
    """
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=14)

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <zcyc_paramsXML xmlns="http://web.cbr.ru/">
      <OnDate>{date_from.isoformat()}T00:00:00</OnDate>
      <ToDate>{today.isoformat()}T23:59:59</ToDate>
    </zcyc_paramsXML>
  </soap:Body>
</soap:Envelope>""".encode("utf-8")

    request = Request(
        CBR_SOAP_URL,
        data=soap_body,
        method="POST",
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{CBR_SOAP_ACTION}"',
            "User-Agent": "PortfolioCalculatorBot/3.0",
            "Accept": "text/xml, application/xml",
        },
    )

    with urlopen(request, timeout=15) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    candidates = []

    for row in _extract_rows(root):
        parsed = _curve_from_row(row)
        if parsed is not None:
            candidates.append(parsed)

    if not candidates:
        raise ValueError(
            "В ответе SecInfo Банка России не найдены данные КБД."
        )

    curve_date, curve = max(candidates, key=lambda item: item[0])

    # Дополнительная проверка здравого диапазона.
    if not all(0.001 < value < 0.60 for value in curve.values()):
        raise ValueError("Получены некорректные значения КБД.")

    return curve_date.strftime("%d.%m.%Y"), curve

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
        logger.info("CBR SecInfo curve updated: %s", curve_date)
    except Exception:
        logger.exception("Failed to update CBR SecInfo curve; using cached/fallback data")
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
        "Я помогу рассчитать доли в модельном инвестиционном портфеле "
        "с учётом суммы и срока инвестирования.\n\n"
        "В основе модели — защитная облигационная часть: её расчётная "
        "доходность учитывается при распределении капитала между "
        "облигациями, акциями и фондами.\n\n"
        "Доходность определяется автоматически по актуальной кривой "
        "бескупонной доходности ОФЗ Банка России.\n\n"
        "Нажмите кнопку ниже, чтобы начать."
    )
    if update.message:
        with WELCOME_IMAGE.open("rb") as photo:
            await update.message.reply_photo(photo=photo, caption=text, reply_markup=main_keyboard())
    else:
        query = update.callback_query
        await query.answer()
        with WELCOME_IMAGE.open("rb") as photo:
            if query.message.photo:
                from telegram import InputMediaPhoto
                await query.edit_message_media(media=InputMediaPhoto(media=photo, caption=text), reply_markup=main_keyboard())
            else:
                await query.message.delete()
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo, caption=text, reply_markup=main_keyboard())


async def begin_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prompt = "Введите сумму портфеля в рублях.\n\nНапример: 300000"
    if query.message.photo:
        await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text=prompt)
    else:
        await query.edit_message_text(prompt)
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

    data_note = f"КБД ОФЗ: данные Банка России на {curve_date}."

    text = (
        "📊 СТРУКТУРА ПОРТФЕЛЯ\n\n"
        f"Сумма: {money(amount)}\n"
        f"Срок: {years_text} лет\n"
        f"Расчётная доходность на выбранный срок: {rate_text} годовых\n\n"
        f"🏆 Золото — {pct(result['funds_pct'])} · {money(result['funds'])}\n"
        f"📈 Облигации — {pct(result['bonds_pct'])} · {money(result['bonds'])}\n"
        f"🚀 Акции — {pct(result['stocks_pct'])} · {money(result['stocks'])}\n\n"
        "ℹ️ Расчётная доходность определяется автоматически на основе "
        "кривой бескупонной доходности ОФЗ Банка России.\n"
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
