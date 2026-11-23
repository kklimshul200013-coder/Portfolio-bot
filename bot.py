import asyncio
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
from telegram.error import RetryAfter, TelegramError
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
BROADCAST_TEXT, BROADCAST_CONFIRM = range(2, 4)
BROADCAST_MAX_LENGTH: Final[int] = 3500

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


def money_kopecks(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%".replace(".", ",")


def years_word(value: float) -> str:
    """Корректное отображение лет в Telegram (1 год, 2 года, 5 лет)."""
    number = float(value)
    if not number.is_integer():
        return f"{number:g}".replace(".", ",") + " года"
    years = int(number)
    if years % 10 == 1 and years % 100 != 11:
        word = "год"
    elif 2 <= years % 10 <= 4 and not 12 <= years % 100 <= 14:
        word = "года"
    else:
        word = "лет"
    return f"{years} {word}"


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
        _curve_cache["is_fallback"] = True

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


def calculate_portfolio(amount: float, years: float, coupon_rate: float):
    """Утверждённая модель: 20% золото, 80% — акции/облигации,
    облигации с учётом НДФЛ на расчётные купоны без реинвестирования.
    """
    return calculate_portfolio_net(amount, years, coupon_rate)


# === SVETINVEST: единое математическое ядро, версия 4 ===
from calendar import isleap, monthrange
from dataclasses import dataclass
from datetime import date

# Ставки из прогноза Банка России от 23.10.2026. Проценты за год,
# midpoint диапазонов, НО НЕ ПРОГНОЗ ДОХОДНОСТИ ОБЛИГАЦИЙ.
# Источник: https://cbr.ru/content/document/file/198897/forecast_261023.pdf
# Обновлять после выхода нового прогноза ЦБ. Значения можно изменить через
# переменную окружения SVETINVEST_MACRO_JSON, пример:
# {"date":"2026-10-23","rates":{"2027":13,"2028":9.5,"2029":8},
#  "inflation":{"2026":7.35,"2027":4,"2028":4,"2029":4},"long_run_rate":8,"long_run_inflation":4}
MACRO_DEFAULT = {
    "date": "2026-10-23",
    "rates": {2027: 13.0, 2028: 9.5, 2029: 8.0},
    "inflation": {2026: 7.35, 2027: 4.0, 2028: 4.0, 2029: 4.0},
    "long_run_rate": 8.0,
    "long_run_inflation": 4.0,
}


def load_macro():
    import json
    result = {
        "date": MACRO_DEFAULT["date"],
        "rates": MACRO_DEFAULT["rates"].copy(),
        "inflation": MACRO_DEFAULT["inflation"].copy(),
        "long_run_rate": MACRO_DEFAULT["long_run_rate"],
        "long_run_inflation": MACRO_DEFAULT["long_run_inflation"],
    }
    raw = os.getenv("SVETINVEST_MACRO_JSON", "").strip()
    if not raw:
        return result
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "date" not in parsed:
            raise ValueError("Нужна дата прогноза")
        result["date"] = str(parsed["date"])
        for name in ("rates", "inflation"):
            if name in parsed:
                result[name] = {int(year): float(value) for year, value in parsed[name].items()}
        for name in ("long_run_rate", "long_run_inflation"):
            if name in parsed:
                result[name] = float(parsed[name])
        rates = list(result["rates"].values()) + list(result["inflation"].values())
        rates += [result["long_run_rate"], result["long_run_inflation"]]
        if not all(0 <= x <= 60 for x in rates):
            raise ValueError("Ставки вне диапазона 0–60%")
    except (ValueError, TypeError, KeyError):
        logger.exception("Некорректная SVETINVEST_MACRO_JSON, используем проверенный прогноз")
        return MACRO_DEFAULT.copy()
    return result


MACRO = load_macro()
TAX_THRESHOLD = 2_400_000.0
TAX_LOW = 0.13
TAX_HIGH = 0.15
MAX_YEARS = 30  # Предел сроков КБД и большинства калькуляторов
MAX_COMPARISON_YEARS = 50  # Общий срок сравнения «Сейчас или позже»
CUSTOM_RATE_MAX_PCT = 200.0


def securities_tax(gross_income: float) -> float:
    """Расчётная шкала 13/15% по доходу; не учитывает другие источники дохода."""
    value = max(0.0, gross_income)
    return TAX_LOW * min(value, TAX_THRESHOLD) + TAX_HIGH * max(0.0, value - TAX_THRESHOLD)


def discount_factor(years: float, curve: dict[float, float]) -> float:
    """Коэффициент дисконтирования с лог-линейной интерполяцией D(t).

    Узлы: D(t_i)=(1+z_i)^(-t_i), промежутки интерполируются по ln(D).
    Для сроков короче минимального узла — ставка первого узла.
    """
    points = sorted(curve)
    if years <= points[0]:
        return (1 + curve[points[0]]) ** (-years)
    if years >= points[-1]:
        return (1 + curve[points[-1]]) ** (-years)
    for left, right in zip(points, points[1:]):
        if left <= years <= right:
            ln_left = -left * math.log1p(curve[left])
            ln_right = -right * math.log1p(curve[right])
            part = (years - left) / (right - left)
            return math.exp(ln_left + part * (ln_right - ln_left))
    raise ValueError("Не удалось интерполировать КБД")


def par_coupon_rate(years: float, curve: dict[float, float]) -> float:
    """Купон условной облигации по номиналу с полугодовыми платежами.

    Для нецелого числа полугодий последний купон пропорционален длине
    оставшегося интервала. Дисконтирование — по КБД ОФЗ.
    """
    if not (1 <= years <= MAX_YEARS):
        raise ValueError("Срок должен быть от 1 до 30 лет")
    payment_dates = []
    t = 0.0
    while t + 0.5 < years - 1e-9:
        t += 0.5
        payment_dates.append((t, 0.5))
    payment_dates.append((years, years - t))
    annuity = sum(period * discount_factor(pay_date, curve)
                  for pay_date, period in payment_dates)
    if annuity <= 0:
        raise ValueError("Невозможно построить купонную ставку")
    return max(0.0, (1.0 - discount_factor(years, curve)) / annuity)


def calculate_portfolio_net(amount: float, years: float, coupon_rate: float) -> dict:
    """B = 0.8P/[1+C(1-N)*T], N — эффективный НДФЛ на купоны.

    Прогрессивность налога делает N зависимой от B. Решение — бисекция:
      B + T*(B*C - securities_tax(B*C)) = 0.8P
    Не реинвестирует купоны, не учитывает налоги других инструментов.
    """
    base = amount * (1 - FUND_SHARE)
    left, right = 0.0, base
    for _ in range(65):
        bonds = (left + right) / 2.0
        gross_coupon = bonds * coupon_rate
        net_coupon = gross_coupon - securities_tax(gross_coupon)
        if bonds + years * net_coupon > base:
            right = bonds
        else:
            left = bonds
    bonds = (left + right) / 2.0
    gross_coupon = bonds * coupon_rate
    effective_tax = securities_tax(gross_coupon) / gross_coupon if gross_coupon else 0.0
    return {
        "funds": amount * FUND_SHARE,
        "bonds": bonds,
        "stocks": base - bonds,
        "funds_pct": FUND_SHARE,
        "bonds_pct": bonds / amount,
        "stocks_pct": (base - bonds) / amount,
        "coupon_rate": coupon_rate,
        "effective_tax": effective_tax,
        "net_coupons": years * (gross_coupon - securities_tax(gross_coupon)),
    }


def add_months(d: date, count: int) -> date:
    year = d.year + (d.month - 1 + count) // 12
    month = (d.month - 1 + count) % 12 + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def add_years(d: date, count: int) -> date:
    return date(d.year + count, d.month, min(d.day, monthrange(d.year + count, d.month)[1]))


def payment_dates(start: date, end: date, frequency: str) -> set[date]:
    """Взносы в конце периода, календарные месяцы — с якорем на начальную дату."""
    if frequency not in ("daily", "weekly", "biweekly", "monthly"):
        raise ValueError("Некорректная периодичность")
    dates = set()
    if frequency == "monthly":
        n = 1
        while True:
            due = add_months(start, n)
            if due > end:
                break
            dates.add(due)
            n += 1
    else:
        delta = {"daily": 1, "weekly": 7, "biweekly": 14}[frequency]
        due = start + timedelta(days=delta)
        while due <= end:
            dates.add(due)
            due += timedelta(days=delta)
    return dates


@dataclass
class Simulation:
    final: float
    invested: float
    tax: float
    inflation_factor: float
    date: date
    reached: bool = False

    @property
    def real(self):
        return self.final / self.inflation_factor

    @property
    def net_profit(self):
        return self.final - self.invested


def bond_proxy_rate(day: date, started: date, curve: dict[float, float],
                    own_rate: float | None) -> float:
    """Оценка доходности одногодичных перекладок, не обещание дохода.

    КБД ОФЗ 1 год — рыночный ориентир текущего периода;
    будущие годы — середины прогноза СТАВКИ ЦБ как сценарные приближения,
    после горизонта ЦБ — допущение о нейтральном уровне (по умолчанию 8%).
    Ставка ЦБ и доходность облигаций не тождественны: эту разницу явно
    поясняем пользователю. Здесь нет попытки выдать макропрогноз за котировку.
    """
    if own_rate is not None:
        return own_rate
    if day.year == started.year:
        return rate_for_term(1.0, curve)
    if day.year in MACRO["rates"]:
        return MACRO["rates"][day.year] / 100.0
    return MACRO["long_run_rate"] / 100.0


def modeled_inflation(year: int) -> float:
    return MACRO["inflation"].get(year, MACRO["long_run_inflation"]) / 100.0


def simulate_savings(start: date, end: date, initial: float, contribution: float,
                     frequency: str, curve: dict[float, float],
                     own_rate: float | None = None, delay_years: int = 0,
                     target: float | None = None, target_today: bool = True) -> Simulation:
    """Календарный расчёт с условным ежедневным начислением после модельного НДФЛ.

    Допущения: доход условного облигационного портфеля полностью является
    облагаемым купонным доходом, налог учитывается по календарным годам,
    нет комиссий и персональных льгот. Реальные выплаты купонов могут быть
    полугодовыми; это приближённый, а не налоговый расчёт.
    """
    if end <= start:
        raise ValueError("Конечная дата должна быть позже начала")
    investment_start = add_years(start, delay_years)
    if investment_start > end:
        raise ValueError("Отложенный старт позже окончания")
    balance = float(initial)
    paid = float(initial)
    taxed = 0.0
    gross_tax_basis = 0.0
    current_tax_year = start.year
    inflation_factor = 1.0
    due_dates = payment_dates(investment_start, end, frequency)
    if target is not None and balance >= target:
        return Simulation(balance, paid, taxed, inflation_factor, start, True)
    day = start
    while day < end:
        day += timedelta(days=1)
        year_days = 366 if isleap(day.year) else 365
        inflation_factor *= (1.0 + modeled_inflation(day.year)) ** (1.0 / year_days)
        if day.year != current_tax_year:
            gross_tax_basis = 0.0
            current_tax_year = day.year
        if day > investment_start:
            annual_rate = bond_proxy_rate(day, start, curve, own_rate)
            growth = (1.0 + annual_rate) ** (1.0 / year_days) - 1.0
            interest = balance * growth
            next_basis = gross_tax_basis + interest
            tax = securities_tax(next_basis) - securities_tax(gross_tax_basis)
            gross_tax_basis = next_basis
            balance += interest - tax
            taxed += tax
        if day in due_dates:
            balance += contribution
            paid += contribution
        if target is not None:
            goal = target * (inflation_factor if target_today else 1.0)
            if balance + 1e-8 >= goal:
                return Simulation(balance, paid, taxed, inflation_factor, day, True)
    return Simulation(balance, paid, taxed, inflation_factor, end, False)


def required_contribution(start: date, end: date, initial: float, goal: float,
                          frequency: str, curve: dict[float, float],
                          own_rate: float | None, target_today: bool) -> tuple[float, Simulation]:
    """Бисекция для динамической доходности и реальных дат. Взнос вверх до 1 коп."""
    kwargs = dict(start=start, end=end, initial=initial, frequency=frequency,
                  curve=curve, own_rate=own_rate)
    zero = simulate_savings(contribution=0.0, **kwargs)
    target_nominal = goal * (zero.inflation_factor if target_today else 1.0)
    if zero.final >= target_nominal:
        return 0.0, zero
    left, right = 0.0, max(1.0, goal)
    for _ in range(50):
        probe = simulate_savings(contribution=right, **kwargs)
        if probe.final >= target_nominal:
            break
        right *= 2.0
        if right > 1e12:
            raise ValueError("Слишком большая целевая сумма")
    for _ in range(36):
        middle = (left + right) / 2.0
        if simulate_savings(contribution=middle, **kwargs).final >= target_nominal:
            right = middle
        else:
            left = middle
    result = math.ceil((right + 1e-9) * 100) / 100
    # Проверка, что округлённый взнос действительно достигает цели.
    simulation = simulate_savings(contribution=result, **kwargs)
    if simulation.final + 1e-7 < target_nominal:
        result += 0.01
        simulation = simulate_savings(contribution=result, **kwargs)
    return result, simulation


def frequency_name(freq: str) -> str:
    return {"daily": "ежедневно", "weekly": "еженедельно",
            "biweekly": "раз в 2 недели", "monthly": "ежемесячно"}[freq]


def format_result(sim: Simulation) -> str:
    """Короткая расшифровка для подписчика — без технических параметров."""
    return (f"Собственные вложения: {money(sim.invested)}\n"
            f"Инвестиционный доход: {money(sim.net_profit)}\n"
            f"С учётом инфляции: {money(sim.real)}")


# === конец математического ядра ===


def welcome_keyboard() -> InlineKeyboardMarkup:
    """Отдельная кнопка для перехода с приветствия к калькуляторам."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Начать", callback_data="menu")],
    ])


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Портфель", callback_data="calculate")],
        [InlineKeyboardButton("📈 Сложный процент", callback_data="tool:compound")],
        [InlineKeyboardButton("🎯 Финансовая цель", callback_data="tool:goal")],
        [InlineKeyboardButton("⏳ Сейчас или позже", callback_data="tool:delay")],
    ])


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


WELCOME_TEXT: Final[str] = (
    "Привет! 👋\n\n"
    "Я помогу рассчитать инвестиционный портфель, будущие накопления "
    "и путь к финансовой цели, а также сравнить разные сроки начала инвестирования.\n\n"
    "📊 Портфель — распределение капитала.\n"
    "📈 Сложный процент — рост накоплений с пополнениями.\n"
    "🎯 Финансовая цель — сколько откладывать и когда накоплю.\n"
    "⏳ Сейчас или позже — сравнение результатов инвестирования.\n\n"
    "Нажмите «🚀 Начать», чтобы открыть калькуляторы."
)

MENU_TEXT: Final[str] = (
    "📊 SVETINVEST | КАЛЬКУЛЯТОРЫ\n\n"
    "Выберите, что хотите рассчитать:"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие с единственной кнопкой «Начать» по команде /start."""
    if update.effective_user:
        save_user(update.effective_user)
    if update.message:
        with WELCOME_IMAGE.open("rb") as photo:
            await update.message.reply_photo(
                photo=photo, caption=WELCOME_TEXT, reply_markup=welcome_keyboard()
            )
    else:
        # Запасной вариант для вызова из callback, если появится такой переход.
        query = update.callback_query
        await query.answer()
        if query.message.photo:
            await query.edit_message_caption(
                caption=WELCOME_TEXT, reply_markup=welcome_keyboard()
            )
        else:
            await query.edit_message_text(
                WELCOME_TEXT, reply_markup=welcome_keyboard()
            )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню из четырёх калькуляторов. Приветствие повторно не показываем."""
    if update.effective_user:
        save_user(update.effective_user)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.message.photo:
            await query.edit_message_caption(
                caption=MENU_TEXT, reply_markup=main_keyboard()
            )
        else:
            await query.edit_message_text(
                MENU_TEXT, reply_markup=main_keyboard()
            )
    else:
        await update.message.reply_text(MENU_TEXT, reply_markup=main_keyboard())


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
    coupon_rate = par_coupon_rate(years, curve)
    result = calculate_portfolio(amount, years, coupon_rate)
    save_user(user)
    record_calculation(user, amount, years, bond_rate)
    record_event(user, "portfolio", "completed")

    text = (
        "📊 ВАШ ИНВЕСТИЦИОННЫЙ ПОРТФЕЛЬ\n\n"
        f"Сумма инвестиций: {money(amount)}\n"
        f"Срок: {years_word(years)}\n\n"
        "РАСПРЕДЕЛЕНИЕ КАПИТАЛА\n\n"
        f"🥇 Золото — {pct(result['funds_pct'])} · {money(result['funds'])}\n"
        f"🏦 Облигации — {pct(result['bonds_pct'])} · {money(result['bonds'])}\n"
        f"📈 Акции — {pct(result['stocks_pct'])} · {money(result['stocks'])}\n\n"
        "Расчёт ориентировочный и не является индивидуальной инвестиционной рекомендацией."
    )
    if is_fallback:
        text += "\n\n⚠️ Использованы ранее сохранённые данные. Результат может быть менее точным."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Рассчитать заново", callback_data="calculate")],
        [InlineKeyboardButton("🏠 В начало", callback_data="home")],
    ])

    if is_message:
        await target.reply_text(text, reply_markup=keyboard)
    else:
        await target.edit_message_text(text, reply_markup=keyboard)


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Расчёт отменён.", reply_markup=main_keyboard())
    return ConversationHandler.END

# === SVETINVEST: диалоги новых калькуляторов ===
NEW_CALC_STATE = 20
FIN_STEPS = {
    "compound": ["initial", "contribution", "frequency", "years", "rate_mode"],
    "goal_save": ["goal", "basis", "initial", "years", "frequency", "rate_mode"],
    "goal_when": ["goal", "basis", "initial", "contribution", "frequency", "rate_mode"],
    "goal_future": ["initial", "contribution", "frequency", "years", "rate_mode"],
    "delay": ["initial", "contribution", "frequency", "years", "delay", "rate_mode"],
}
FIN_TITLES = {
    "compound": "📈 СЛОЖНЫЙ ПРОЦЕНТ",
    "goal_save": "🎯 СКОЛЬКО ОТКЛАДЫВАТЬ",
    "goal_when": "🎯 КОГДА НАКОПЛЮ",
    "goal_future": "🎯 СКОЛЬКО НАКОПЛЮ",
    "delay": "⏳ СЕЙЧАС ИЛИ ПОЗЖЕ",
}


def fin_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить расчёт", callback_data="calc:cancel")],
    ])


def fin_result_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="home")],
    ])


async def fin_reply(update, context, text, markup=None):
    if update.callback_query:
        query = update.callback_query
        if query.message.photo:
            await query.message.delete()
            await context.bot.send_message(chat_id=query.message.chat_id, text=text,
                                           reply_markup=markup)
        else:
            await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def fin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return ConversationHandler.END
    save_user(update.effective_user)
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    if kind == "goal":
        context.user_data.pop("fin_work", None)
        await fin_reply(update, context,
            "🎯 ФИНАНСОВАЯ ЦЕЛЬ\n\nЧто хотите рассчитать?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Сколько откладывать?", callback_data="calc:kind:goal_save")],
                [InlineKeyboardButton("📅 Когда накоплю?", callback_data="calc:kind:goal_when")],
                [InlineKeyboardButton("📈 Сколько накоплю?", callback_data="calc:kind:goal_future")],
                [InlineKeyboardButton("❌ Отменить", callback_data="calc:cancel")],
            ]))
        return NEW_CALC_STATE
    if kind not in ("compound", "delay"):
        return ConversationHandler.END
    context.user_data["fin_work"] = {"kind": kind, "index": 0, "values": {}}
    record_event(update.effective_user, kind, "opened")
    await fin_ask(update, context)
    return NEW_CALC_STATE


async def fin_ask(update, context):
    work = context.user_data.get("fin_work")
    if not work:
        return
    title = FIN_TITLES[work["kind"]]
    if work.get("custom_pending"):
        await fin_reply(update, context,
            f"{title}\n\nУкажите ожидаемую доходность в % годовых.\n"
            "Например: 10,5\n\nДопустимый диапазон: 0–200%.", fin_cancel_keyboard())
        return
    steps = FIN_STEPS[work["kind"]]
    if work["index"] >= len(steps):
        await fin_finish(update, context)
        return
    step = steps[work["index"]]
    if step == "frequency":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ежедневно", callback_data="calc:freq:daily"),
             InlineKeyboardButton("Еженедельно", callback_data="calc:freq:weekly")],
            [InlineKeyboardButton("Раз в 2 недели", callback_data="calc:freq:biweekly"),
             InlineKeyboardButton("Ежемесячно", callback_data="calc:freq:monthly")],
            [InlineKeyboardButton("❌ Отменить", callback_data="calc:cancel")],
        ])
        if work["kind"] == "delay":
            amount = money(work["values"]["contribution"])
            question = (f"{title}\n\nШаг 3 из 6\n\n"
                        f"Как часто будете вносить {amount}?")
        else:
            question = f"{title}\n\nКак часто будете пополнять счёт?"
        await fin_reply(update, context, question, markup)
        return
    if step == "basis":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Учесть рост цен", callback_data="calc:basis:today")],
            [InlineKeyboardButton("Накопить указанную сумму", callback_data="calc:basis:fixed")],
            [InlineKeyboardButton("❌ Отменить", callback_data="calc:cancel")],
        ])
        await fin_reply(update, context,
            f"{title}\n\nНужно ли учитывать рост цен при расчёте цели?",
            markup)
        return
    if step == "rate_mode":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Прогнозная доходность", callback_data="calc:rate:auto")],
            [InlineKeyboardButton("✏️ Указать свою доходность", callback_data="calc:rate:custom")],
            [InlineKeyboardButton("❌ Отменить", callback_data="calc:cancel")],
        ])
        question = (f"{title}\n\nШаг 6 из 6\n\n"
                    "Как рассчитать ожидаемый рост капитала?") if work["kind"] == "delay" else (
                    f"{title}\n\nКакую доходность учитывать?")
        await fin_reply(update, context, question, markup)
        return
    prompts = {
        "initial": "Введите стартовый капитал в рублях (можно 0).\nНапример: 100000",
        "contribution": "Введите сумму одного регулярного пополнения в рублях (можно 0).\nНапример: 10000",
        "goal": "Введите целевую сумму в рублях.\nНапример: 3000000",
        "years": "Введите срок инвестирования от 1 до 30 лет (целое число).\nНапример: 10",
        "delay": "Через сколько лет начнёте инвестировать во втором варианте?",
    }
    if work["kind"] == "delay":
        if step == "initial":
            prompts[step] = (
                "Сравним, сколько можно накопить к одной дате, если начать "
                "инвестировать сейчас или спустя несколько лет.\n\n"
                "Шаг 1 из 6\n\n"
                "С какой суммы начнёте?\n\n"
                "Сумма одинакова для обоих вариантов. "
                "Если начального капитала нет, введите 0."
            )
        elif step == "contribution":
            prompts[step] = (
                "Шаг 2 из 6\n\n"
                "Сколько будете регулярно инвестировать?\n\n"
                "Введите сумму одного пополнения в рублях. Например: 10000"
            )
        elif step == "years":
            prompts[step] = (
                "Шаг 4 из 6\n\n"
                "Через сколько лет хотите узнать результат?\n\n"
                "Например, сейчас вам 20 лет и вы хотите узнать капитал к 60 — "
                "введите 40.\n\n"
                f"Введите целое число от 2 до {MAX_COMPARISON_YEARS}."
            )
        elif step == "delay":
            total = int(work["values"]["years"])
            prompts[step] = (
                "Шаг 5 из 6\n\n"
                "На сколько лет позже начнёте инвестировать во втором варианте?\n\n"
                "Например: 10.\n"
                f"Тогда сравним результаты через {total} лет: первый вариант "
                f"— {total} лет инвестиций, второй — {total - 10} лет.\n\n"
                f"Введите целое число от 1 до {total - 1}."
            ) if total > 10 else (
                "Шаг 5 из 6\n\n"
                "На сколько лет позже начнёте инвестировать во втором варианте?\n\n"
                f"Оба результата сравним через {total} лет.\n\n"
                f"Введите целое число от 1 до {total - 1}."
            )
    await fin_reply(update, context, f"{title}\n\n{prompts[step]}", fin_cancel_keyboard())


async def fin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    work = context.user_data.get("fin_work")
    if not work:
        await update.message.reply_text("Начните новый расчёт из главного меню.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        value = parse_number(update.message.text)
        if not math.isfinite(value):
            raise ValueError
        if work.get("custom_pending"):
            if not 0 <= value <= CUSTOM_RATE_MAX_PCT:
                raise ValueError
            work["values"]["own_rate"] = value / 100.0
            work["custom_pending"] = False
            await fin_finish(update, context)
            return ConversationHandler.END
        step = FIN_STEPS[work["kind"]][work["index"]]
        if step in ("goal", "initial", "contribution"):
            if value < 0 or value > 1_000_000_000_000 or (step == "goal" and value <= 0):
                raise ValueError
        elif step == "years":
            max_years = MAX_COMPARISON_YEARS if work["kind"] == "delay" else MAX_YEARS
            min_years = 2 if work["kind"] == "delay" else 1
            if int(value) != value or not min_years <= value <= max_years:
                raise ValueError
            value = int(value)
        elif step == "delay":
            if int(value) != value or not 1 <= value < work["values"]["years"]:
                raise ValueError
            value = int(value)
        else:
            raise ValueError
    except (ValueError, OverflowError):
        await update.message.reply_text("Введите корректное число в указанном диапазоне.")
        return NEW_CALC_STATE

    work["values"][step] = value
    if work["index"] == 0:
        record_event(update.effective_user, work["kind"], "started")
    work["index"] += 1
    await fin_ask(update, context)
    return NEW_CALC_STATE if context.user_data.get("fin_work") else ConversationHandler.END


async def fin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    payload = query.data.split(":")
    if payload[1] == "cancel":
        context.user_data.pop("fin_work", None)
        await fin_reply(update, context, "Расчёт отменён.", main_keyboard())
        return ConversationHandler.END
    if payload[1] == "kind":
        kind = payload[2]
        if kind not in FIN_STEPS or not kind.startswith("goal_"):
            return ConversationHandler.END
        context.user_data["fin_work"] = {"kind": kind, "index": 0, "values": {}}
        record_event(update.effective_user, kind, "opened")
        await fin_ask(update, context)
        return NEW_CALC_STATE
    work = context.user_data.get("fin_work")
    if not work:
        await fin_reply(update, context, "Начните новый расчёт.", main_keyboard())
        return ConversationHandler.END
    expected = FIN_STEPS[work["kind"]][work["index"]]
    if payload[1] == "freq" and expected == "frequency" and payload[2] in ("daily", "weekly", "biweekly", "monthly"):
        work["values"]["frequency"] = payload[2]
    elif payload[1] == "basis" and expected == "basis" and payload[2] in ("today", "fixed"):
        work["values"]["basis"] = payload[2]
    elif payload[1] == "rate" and expected == "rate_mode" and payload[2] in ("auto", "custom"):
        work["values"]["own_rate"] = None
        if payload[2] == "custom":
            work["custom_pending"] = True
    else:
        await fin_reply(update, context,
                        "Параметр не соответствует текущему шагу. Продолжите расчёт.",
                        fin_cancel_keyboard())
        return NEW_CALC_STATE
    work["index"] += 1
    await fin_ask(update, context)
    return NEW_CALC_STATE if context.user_data.get("fin_work") else ConversationHandler.END


async def fin_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    work = context.user_data.get("fin_work")
    if not work:
        return
    kind, v = work["kind"], work["values"]
    try:
        if v.get("own_rate") is None:
            curve_date, curve, is_fallback = await asyncio.to_thread(get_cbr_curve)
        else:
            curve_date, curve, is_fallback = "", FALLBACK_CURVE, False
        text = await asyncio.to_thread(fin_calculate_text, kind, v, curve)
    except Exception:
        logger.exception("Ошибка расчёта %s", kind)
        await fin_reply(update, context, "Не удалось выполнить расчёт. Проверьте исходные данные и попробуйте снова.", main_keyboard())
        context.user_data.pop("fin_work", None)
        return
    # Методику и технические показатели не выводим. Предупреждение об
    # использовании резервных данных остаётся видимым для пользователя.
    suffix = "\n\nСуммы ориентировочные и учитывают расчётные налоги."
    if is_fallback:
        suffix += "\n⚠️ Использованы ранее сохранённые данные. Результат может быть менее точным."
    await fin_reply(update, context, text + suffix, fin_result_keyboard())
    record_event(update.effective_user, kind, "completed")
    context.user_data.pop("fin_work", None)


def fin_calculate_text(kind: str, v: dict, curve: dict[float, float],
                       start: date | None = None) -> str:
    """Краткие результаты без технических параметров; математика не меняется."""
    today = start or date.today()
    own_rate = v.get("own_rate")
    if kind in ("compound", "goal_future"):
        end = add_years(today, int(v["years"]))
        sim = simulate_savings(today, end, v["initial"], v["contribution"],
                               v["frequency"], curve, own_rate)
        title = "📈 РАСЧЁТ НАКОПЛЕНИЙ" if kind == "compound" else "🎯 СКОЛЬКО НАКОПЛЮ"
        return (
            f"{title}\n\n"
            f"Начальный капитал: {money(v['initial'])}\n"
            f"Пополнение: {money(v['contribution'])} {frequency_name(v['frequency'])}\n"
            f"Срок: {years_word(v['years'])}\n\n"
            f"💰 КАПИТАЛ ЧЕРЕЗ {years_word(v['years']).upper()}\n"
            f"{money(sim.final)}\n\n"
            f"{format_result(sim)}"
        )
    if kind == "goal_save":
        end = add_years(today, int(v["years"]))
        amount, sim = required_contribution(today, end, v["initial"], v["goal"],
                                            v["frequency"], curve, own_rate,
                                            v["basis"] == "today")
        text = (
            "🎯 ВАША ФИНАНСОВАЯ ЦЕЛЬ\n\n"
            f"Цель: {money(v['goal'])}\n"
            f"Начальный капитал: {money(v['initial'])}\n"
            f"Срок: {years_word(v['years'])}\n\n"
            "✅ НУЖНО ОТКЛАДЫВАТЬ\n"
            f"{money_kopecks(amount)} {frequency_name(v['frequency'])}"
        )
        if v["basis"] == "today":
            text += f"\n\nОриентировочная стоимость цели к этому сроку: {money(v['goal'] * sim.inflation_factor)}"
        return text
    if kind == "goal_when":
        end = add_years(today, MAX_YEARS)
        sim = simulate_savings(today, end, v["initial"], v["contribution"],
                               v["frequency"], curve, own_rate,
                               target=v["goal"], target_today=v["basis"] == "today")
        prefix = (
            "🎯 КОГДА ДОСТИГНУ ЦЕЛИ?\n\n"
            f"Цель: {money(v['goal'])}"
            + (" с учётом роста цен" if v["basis"] == "today" else "") + "\n"
            f"Начальный капитал: {money(v['initial'])}\n"
            f"Пополнение: {money(v['contribution'])} {frequency_name(v['frequency'])}\n\n"
        )
        if sim.reached:
            return prefix + ("📅 ОЖИДАЕМАЯ ДАТА\n"
                             f"{sim.date:%d.%m.%Y}\n\n"
                             f"Капитал к этой дате: {money(sim.final)}")
        return prefix + ("⚠️ При выбранных взносах цель не достигается за 30 лет.\n\n"
                         f"Ожидаемый капитал через 30 лет: {money(sim.final)}")
    if kind == "delay":
        # Оба варианта оцениваются на ОДНУ дату: например, к 60 годам.
        # Первый пополняется весь период, второй — только после задержки.
        # Поэтому его период взносов короче, а не сдвигается конечная дата.
        total_years = int(v["years"])
        delay_years = int(v["delay"])
        if not (2 <= total_years <= MAX_COMPARISON_YEARS and
                1 <= delay_years < total_years):
            raise ValueError("Некорректные сроки сравнения")
        end = add_years(today, total_years)
        now = simulate_savings(today, end, v["initial"], v["contribution"],
                               v["frequency"], curve, own_rate)
        later = simulate_savings(today, end, v["initial"], v["contribution"],
                                 v["frequency"], curve, own_rate,
                                 delay_years=delay_years)
        later_years = total_years - delay_years
        return (
            "⏳ СЕЙЧАС ИЛИ ПОЗЖЕ\n\n"
            f"Начальный капитал: {money(v['initial'])}\n"
            f"Пополнение: {money(v['contribution'])} {frequency_name(v['frequency'])}\n"
            f"Сравниваем через: {years_word(total_years)}\n\n"
            "🚀 ВАРИАНТ 1. НАЧАТЬ СЕЙЧАС\n"
            f"Инвестировать: {years_word(total_years)}\n"
            f"Собственные вложения: {money(now.invested)}\n"
            f"Инвестиционный доход: {money(now.net_profit)}\n"
            f"💰 Итоговый капитал: {money(now.final)}\n\n"
            f"⏳ ВАРИАНТ 2. НАЧАТЬ ЧЕРЕЗ {years_word(delay_years).upper()}\n"
            f"Инвестировать: {years_word(later_years)}\n"
            f"Собственные вложения: {money(later.invested)}\n"
            f"Инвестиционный доход: {money(later.net_profit)}\n"
            f"💰 Итоговый капитал: {money(later.final)}\n\n"
            f"📊 РАЗНИЦА: {money(now.final - later.final)}"
        )
    raise ValueError("Неизвестный калькулятор")


async def fin_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("fin_work", None)
    await update.message.reply_text("Расчёт отменён.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def fin_cancel_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("fin_work", None)
    await home(update, context)
    return ConversationHandler.END


# === конец диалогов ===


ADMIN_USER_ID: Final[int] = 393436657


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
         InlineKeyboardButton("👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton("📈 Расчёты", callback_data="admin:calculations"),
         InlineKeyboardButton("🧮 По инструментам", callback_data="admin:tools")],
        [InlineKeyboardButton("🔀 Воронка", callback_data="admin:funnel"),
         InlineKeyboardButton("📅 По дням", callback_data="admin:daily")],
        [InlineKeyboardButton("📨 Рассылки", callback_data="broadcast:menu")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="admin:menu")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:menu")]
    ])


def broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Создать рассылку", callback_data="broadcast:create")],
        [InlineKeyboardButton("⬅️ В админ-панель", callback_data="admin:menu")],
    ])


def _broadcast_allowed(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_USER_ID
                and update.effective_chat and update.effective_chat.type == "private")


async def broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _broadcast_allowed(update):
        await query.answer("Доступ запрещён", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "📨 SVETINVEST | РАССЫЛКИ\n\n"
        "Создайте текстовое сообщение для пользователей бота. "
        "Перед отправкой будет предварительный просмотр и подтверждение.",
        reply_markup=broadcast_keyboard(),
    )


async def broadcast_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _broadcast_allowed(update):
        await query.answer("Доступ запрещён", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.pop("broadcast_draft", None)
    await query.edit_message_text(
        "✍️ Отправьте текст рассылки одним сообщением "
        f"(не более {BROADCAST_MAX_LENGTH} символов).\n\n"
        "Для отмены отправьте /cancel или нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отменить", callback_data="broadcast:cancel")]
        ]),
    )
    return BROADCAST_TEXT


async def broadcast_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _broadcast_allowed(update):
        return ConversationHandler.END
    draft = (update.message.text or "").strip()
    if not draft or len(draft) > BROADCAST_MAX_LENGTH:
        await update.message.reply_text(
            f"Текст должен содержать от 1 до {BROADCAST_MAX_LENGTH} символов. "
            "Отправьте другой текст или /cancel."
        )
        return BROADCAST_TEXT
    context.user_data["broadcast_draft"] = draft
    await update.message.reply_text(
        "👀 ПРЕДПРОСМОТР РАССЫЛКИ\n\n" + draft +
        "\n\nОтправить это сообщение всем пользователям?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Отправить всем", callback_data="broadcast:send")],
            [InlineKeyboardButton("✏️ Изменить текст", callback_data="broadcast:create")],
            [InlineKeyboardButton("❌ Отменить", callback_data="broadcast:cancel")],
        ]),
    )
    return BROADCAST_CONFIRM


def _broadcast_recipients() -> list[int]:
    if not DATABASE_URL:
        raise RuntimeError("База данных не подключена")
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM bot_users ORDER BY user_id")
            return [int(row[0]) for row in cur.fetchall()]


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _broadcast_allowed(update):
        await query.answer("Доступ запрещён", show_alert=True)
        return ConversationHandler.END
    if context.application.bot_data.get("broadcast_running"):
        await query.answer("Рассылка уже выполняется", show_alert=True)
        return BROADCAST_CONFIRM
    draft = context.user_data.get("broadcast_draft")
    if not draft:
        await query.answer("Текст не найден. Создайте рассылку заново.", show_alert=True)
        return ConversationHandler.END

    # Устанавливаем блокировку до первого await, чтобы исключить повторную отправку.
    context.application.bot_data["broadcast_running"] = True
    try:
        await query.answer("Рассылка запущена")
        await query.edit_message_text("📨 Отправка сообщений началась. Дождитесь отчёта…")
        recipients = await asyncio.to_thread(_broadcast_recipients)
        sent = 0
        failed = 0
        for user_id in recipients:
            for attempt in range(2):
                try:
                    await context.bot.send_message(chat_id=user_id, text=draft)
                    sent += 1
                    break
                except RetryAfter as exc:
                    if attempt == 0:
                        await asyncio.sleep(min(float(exc.retry_after) + 1, 60))
                        continue
                    failed += 1
                    logger.warning("Broadcast rate limited for user %s", user_id)
                except TelegramError as exc:
                    failed += 1
                    logger.warning("Broadcast failed for user %s: %s", user_id, exc)
                    break
            await asyncio.sleep(0.07)
        await query.edit_message_text(
            "📨 SVETINVEST | РАССЫЛКА ЗАВЕРШЕНА\n\n"
            f"👥 Получателей в базе: {len(recipients)}\n"
            f"✅ Успешно отправлено: {sent}\n"
            f"❌ Ошибок отправки: {failed}\n\n"
            "Ошибки возможны, если пользователь заблокировал бота или удалил чат.",
            reply_markup=admin_back_keyboard(),
        )
    except Exception:
        logger.exception("Ошибка выполнения рассылки")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="❌ Рассылка прервана из-за ошибки. Проверьте логи Railway. "
                 "Перед повторной отправкой проверьте, кому сообщения уже пришли.",
        )
    finally:
        context.user_data.pop("broadcast_draft", None)
        context.application.bot_data["broadcast_running"] = False
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _broadcast_allowed(update):
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещён", show_alert=True)
        return ConversationHandler.END
    context.user_data.pop("broadcast_draft", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Рассылка отменена.", reply_markup=admin_back_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ Рассылка отменена.", reply_markup=admin_back_keyboard()
        )
    return ConversationHandler.END


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
                elif action == "tools":
                    cur.execute("""
                        SELECT calculator,
                               COUNT(DISTINCT user_id) FILTER (WHERE event_type='opened'),
                               COUNT(*) FILTER (WHERE event_type='completed'),
                               COUNT(DISTINCT user_id) FILTER (WHERE event_type='completed')
                        FROM bot_events
                        GROUP BY calculator ORDER BY calculator
                    """)
                    rows = cur.fetchall()
                    lines = ["🧮 SVETINVEST | КАЛЬКУЛЯТОРЫ", "",
                             "Открытия (уник.) / Расчёты / Пользователи"]
                    for name, opened, completed, people in rows:
                        lines.append(f"{name}: {opened} / {completed} / {people}")
                    if not rows:
                        lines.append("Расчётов пока нет.")
                    text = "\n".join(lines)
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

    broadcast_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(broadcast_create, pattern=r"^broadcast:create$")],
        states={
            BROADCAST_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_receive_text),
                CallbackQueryHandler(broadcast_cancel, pattern=r"^broadcast:cancel$"),
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(broadcast_send, pattern=r"^broadcast:send$"),
                CallbackQueryHandler(broadcast_create, pattern=r"^broadcast:create$"),
                CallbackQueryHandler(broadcast_cancel, pattern=r"^broadcast:cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_menu, pattern=r"^menu$"))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(admin_action, pattern=r"^admin:(?:menu|stats|users|calculations|funnel|daily|tools)$"))
    app.add_handler(CallbackQueryHandler(broadcast_menu, pattern=r"^broadcast:menu$"))
    app.add_handler(broadcast_conversation)
    app.add_handler(CallbackQueryHandler(home, pattern=r"^home$"))
    app.add_handler(conversation)
    financial_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(fin_entry, pattern=r"^tool:(?:compound|goal|delay)$")],
        states={
            NEW_CALC_STATE: [
                CallbackQueryHandler(fin_choice, pattern=r"^calc:(?:kind|freq|basis|rate|cancel)(?::.*)?$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fin_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", fin_cancel_command),
            CallbackQueryHandler(fin_cancel_home, pattern=r"^home$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(financial_conversation)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


  
if __name__ == "__main__":
    init_database()
    run()
  
