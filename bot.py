import logging
import math
import os
import psycopg
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
DATABASE_URL = os.getenv("DATABASE_URL")
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


def init_database():
    """Создаёт таблицу пользователей, если её ещё нет."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — база данных отключена")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        joined_at TIMESTAMPTZ DEFAULT NOW(),
                        last_active_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_calculations (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        amount NUMERIC(18,2) NOT NULL,
                        years NUMERIC(6,2) NOT NULL,
                        bond_rate NUMERIC(10,6) NOT NULL,
                        calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_portfolio_calculations_date
                    ON portfolio_calculations (calculated_at)
                """)
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_events (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        calculator TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT bot_events_event_type_check CHECK (
                            event_type IN ('opened', 'started', 'completed')
                        )
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_bot_events_type_date
                    ON bot_events (calculator, event_type, created_at)
                """)
        logger.info("Таблица bot_events готова")
        logger.info("Таблица portfolio_calculations готова")
        logger.info("Таблица bot_users готова")
    except Exception:
        logger.exception("Ошибка инициализации PostgreSQL")

def save_user(user):
    if not DATABASE_URL or user is None:
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_users (user_id, username, first_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_active_at = NOW()
                    """,
                    (user.id, user.username, user.first_name)
                )
    except Exception:
        logger.exception("Ошибка сохранения пользователя")

def record_calculation(user, amount: float, years: float, bond_rate: float):
    """Сохраняет только успешно выполненные расчёты."""
    if not DATABASE_URL or user is None:
        return
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO portfolio_calculations (user_id, amount, years, bond_rate)
                    VALUES (%s, %s, %s, %s)
                """, (user.id, amount, years, bond_rate))
    except Exception:
        logger.exception("Ошибка сохранения расчёта")


def record_event(user, calculator: str, event_type: str):
    """Универсальные события для нынешних и будущих калькуляторов."""
    if not DATABASE_URL or user is None:
        return
    if event_type not in ("opened", "started", "completed"):
        return
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_events (user_id, calculator, event_type)
                    VALUES (%s, %s, %s)
                """, (user.id, calculator, event_type))
    except Exception:
        logger.exception("Ошибка записи аналитического события")


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
    if update.effective_user:
        save_user(update.effective_user)
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
    save_user(update.effective_user)
    record_event(update.effective_user, "portfolio", "opened")
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

    save_user(update.effective_user)
    context.user_data["amount"] = amount
    record_event(update.effective_user, "portfolio", "started")
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
    await show_result(query, context, years, user=update.effective_user)
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

    await show_result(update.message, context, years, is_message=True, user=update.effective_user)
    return ConversationHandler.END


async def show_result(
    target,
    context: ContextTypes.DEFAULT_TYPE,
    years: float,
    is_message: bool = False,
    user=None,
):
    amount = context.user_data["amount"]

    curve_date, curve, is_fallback = get_cbr_curve()
    bond_rate = rate_for_term(years, curve)
    result = calculate_portfolio(amount, years, bond_rate)
    save_user(user)
    record_calculation(user, amount, years, bond_rate)
    record_event(user, "portfolio", "completed")

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

ADMIN_USER_ID: Final[int] = 393436657


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton("📈 Расчёты", callback_data="admin:calculations")],
        [InlineKeyboardButton("🔀 Воронка", callback_data="admin:funnel"),
         InlineKeyboardButton("📅 По дням", callback_data="admin:daily")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="admin:menu")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:menu")]
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_USER_ID:
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Доступ запрещён.")
        return

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "🛡 SVETINVEST | АДМИН-ПАНЕЛЬ\n\nВыберите раздел:",
            reply_markup=admin_keyboard(),
        )
    else:
        await update.message.reply_text(
            "🛡 SVETINVEST | АДМИН-ПАНЕЛЬ\n\nВыберите раздел:",
            reply_markup=admin_keyboard(),
        )


async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not update.effective_user or update.effective_user.id != ADMIN_USER_ID:
        await query.answer("Доступ запрещён", show_alert=True)
        return

    action = query.data.split(":", 1)[1]
    if action == "menu":
        await admin_panel(update, context)
        return

    await query.answer()
    if not DATABASE_URL:
        await query.edit_message_text("База данных не подключена.", reply_markup=admin_back_keyboard())
        return

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                if action == "stats":
                    cur.execute("""
                        SELECT COUNT(*),
                               COUNT(*) FILTER (WHERE joined_at >= CURRENT_DATE),
                               COUNT(*) FILTER (WHERE joined_at >= NOW() - INTERVAL '7 days'),
                               COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '7 days'),
                               MAX(joined_at)
                        FROM bot_users
                    """)
                    total, today, week_new, week_active, latest = cur.fetchone()
                    text = (
                        "📊 SVETINVEST | СТАТИСТИКА\n\n"
                        f"👥 Всего пользователей: {total}\n"
                        f"🆕 Новых сегодня: {today}\n"
                        f"📅 Новых за 7 дней: {week_new}\n"
                        f"📈 Активных за 7 дней: {week_active}\n\n"
                        "Последняя регистрация (UTC): "
                        f"{latest.strftime('%d.%m.%Y %H:%M') if latest else 'Нет данных'}"
                    )
                elif action == "calculations":
                    cur.execute("""
                        SELECT COUNT(*),
                               COUNT(*) FILTER (WHERE calculated_at >= NOW() - INTERVAL '7 days'),
                               COUNT(DISTINCT user_id),
                               AVG(amount)
                        FROM portfolio_calculations
                    """)
                    total, week, unique_users, avg_amount = cur.fetchone()
                    cur.execute("""
                        SELECT years, COUNT(*) AS n
                        FROM portfolio_calculations
                        GROUP BY years
                        ORDER BY n DESC, years ASC
                        LIMIT 5
                    """)
                    popular = cur.fetchall()
                    lines = [
                        "📈 SVETINVEST | АНАЛИТИКА РАСЧЁТОВ", "",
                        f"🧮 Всего расчётов: {total}",
                        f"📅 За последние 7 дней: {week}",
                        f"👥 Уникальных пользователей: {unique_users}",
                        f"💰 Средний портфель: {money(float(avg_amount)) if avg_amount is not None else 'Нет данных'}",
                        "", "⏳ Популярные сроки:",
                    ]
                    if popular:
                        for years_value, n in popular:
                            lines.append(f"• {float(years_value):g} лет — {n} расч.")
                    else:
                        lines.append("Пока нет завершённых расчётов.")
                    text = "\n".join(lines)
                elif action == "funnel":
                    cur.execute("""
                        SELECT event_type, COUNT(DISTINCT user_id)
                        FROM bot_events
                        WHERE calculator = 'portfolio'
                        GROUP BY event_type
                    """)
                    counts = dict(cur.fetchall())
                    opened = counts.get("opened", 0)
                    started = counts.get("started", 0)
                    completed = counts.get("completed", 0)
                    cur.execute("SELECT COUNT(*) FROM bot_users")
                    registered = cur.fetchone()[0]
                    def conversion(part, whole):
                        return f"{100 * part / whole:.1f}%" if whole else "—"
                    text = (
                        "🔀 SVETINVEST | ВОРОНКА ПОРТФЕЛЯ\n\n"
                        f"👥 Зарегистрировано: {registered}\n"
                        f"📲 Открыли калькулятор: {opened}\n"
                        f"✏️ Ввели сумму: {started}\n"
                        f"✅ Получили результат: {completed}\n\n"
                        f"Конверсия открытие → результат: {conversion(completed, opened)}\n\n"
                        "Учитываются уникальные пользователи на каждом этапе. "
                        "События записываются с момента установки обновления. "
                        "Этапы могут относиться к разным сессиям."
                    )
                elif action == "daily":
                    cur.execute("""
                        WITH days AS (
                            SELECT generate_series(
                                CURRENT_DATE - INTERVAL '6 days',
                                CURRENT_DATE, INTERVAL '1 day'
                            )::date AS day
                        ), registrations AS (
                            SELECT (joined_at AT TIME ZONE 'UTC')::date AS day, COUNT(*) AS n
                            FROM bot_users
                            WHERE joined_at >= (CURRENT_DATE - INTERVAL '6 days')
                            GROUP BY 1
                        ), calculations AS (
                            SELECT (calculated_at AT TIME ZONE 'UTC')::date AS day, COUNT(*) AS n
                            FROM portfolio_calculations
                            WHERE calculated_at >= (CURRENT_DATE - INTERVAL '6 days')
                            GROUP BY 1
                        )
                        SELECT days.day, COALESCE(registrations.n, 0),
                               COALESCE(calculations.n, 0)
                        FROM days
                        LEFT JOIN registrations USING (day)
                        LEFT JOIN calculations USING (day)
                        ORDER BY days.day DESC
                    """)
                    rows = cur.fetchall()
                    lines = ["📅 SVETINVEST | ПО ДНЯМ (UTC)", "", "Дата | Новые | Расчёты"]
                    for day, new_users, calc_count in rows:
                        lines.append(f"{day:%d.%m} | {new_users} | {calc_count}")
                    lines.append("\nИстория расчётов доступна с момента включения их учёта.")
                    text = "\n".join(lines)
                elif action == "users":
                    cur.execute("""
                        SELECT username, first_name, joined_at
                        FROM bot_users ORDER BY joined_at DESC LIMIT 10
                    """)
                    rows = cur.fetchall()
                    lines = ["👥 SVETINVEST | ПОСЛЕДНИЕ 10 ПОЛЬЗОВАТЕЛЕЙ", ""]
                    if not rows:
                        lines.append("Пользователей пока нет.")
                    for idx, (username, first_name, joined_at) in enumerate(rows, 1):
                        display_name = first_name or "Без имени"
                        handle = f" (@{username})" if username else ""
                        date = joined_at.strftime("%d.%m.%Y") if joined_at else "—"
                        lines.append(f"{idx}. {display_name}{handle} — {date}")
                    text = "\n".join(lines)
                else:
                    text = "Неизвестный раздел."

        await query.edit_message_text(text, reply_markup=admin_back_keyboard())
    except Exception:
        logger.exception("Ошибка административной панели")
        await query.edit_message_text(
            "Не удалось получить данные. Проверьте логи Railway.",
            reply_markup=admin_back_keyboard(),
        )


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
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin:(?:menu|stats|users|calculations|funnel|daily)$"))
    app.add_handler(CallbackQueryHandler(home, pattern=r"^home$"))
    app.add_handler(conversation)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


  
if __name__ == "__main__":
    init_database()
    run()
  
