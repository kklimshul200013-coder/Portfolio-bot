import logging
import os
from typing import Final

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
FUND_SHARE: Final[float] = 0.20       # 20% портфеля сразу в фонды
DEFAULT_BOND_RATE: Final[float] = 0.10  # 10% средняя доходность облигаций

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


def calculate_portfolio(amount: float, years: float, bond_rate: float = DEFAULT_BOND_RATE):
    """Логика полностью повторяет утвержденную модель Excel."""
    funds = amount * FUND_SHARE
    base = amount - funds

    # Облигации и акции считаются от оставшихся 80%.
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
        "облигациями и акциями с учетом срока инвестирования.\n\n"
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
            "Введите срок в годах.\n\nНапример: 7"
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

    if years <= 0 or years > 100:
        await update.message.reply_text("Введите срок от 1 до 100 лет.")
        return TERM

    await show_result(update.message, context, years, is_message=True)
    return ConversationHandler.END


async def show_result(target, context: ContextTypes.DEFAULT_TYPE, years: float, is_message=False):
    amount = context.user_data["amount"]
    result = calculate_portfolio(amount, years)

    years_text = f"{years:g}".replace(".", ",")
    rate_text = pct(DEFAULT_BOND_RATE)

    text = (
        "📊 СТРУКТУРА ПОРТФЕЛЯ\n\n"
        f"Сумма: {money(amount)}\n"
        f"Срок: {years_text} лет\n"
        f"Средняя доходность облигаций: {rate_text}\n\n"
        f"🟡 Фонды — {pct(result['funds_pct'])} · {money(result['funds'])}\n"
        f"🔵 Облигации — {pct(result['bonds_pct'])} · {money(result['bonds'])}\n"
        f"🟢 Акции — {pct(result['stocks_pct'])} · {money(result['stocks'])}\n\n"
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
    token = os.getenv("TELEGRAM_BOT_TOKEN")
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
