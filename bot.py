import os
import logging
import tempfile
from datetime import date

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import speech_recognition as sr
from pydub import AudioSegment
from deep_translator import GoogleTranslator

# -------------------------------------------------
# Логирование
# -------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# Конфиг из переменных окружения
# -------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_WEBHOOK_URL = os.environ.get("BASE_WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "10000"))

# id друга-препода с безлимитом
FRIEND_ID = 1300323894

# -------------------------------------------------
# Группы и лимиты
# -------------------------------------------------
GROUPS = {
    "free": {"title": "Free", "daily_limit": 30},
    "a1": {"title": "A1", "daily_limit": 120},
    "a2": {"title": "A2", "daily_limit": 200},
    "b1": {"title": "B1", "daily_limit": 300},
    "vip": {"title": "VIP", "daily_limit": None},  # None = безлимит
}
DEFAULT_GROUP = "free"

# -------------------------------------------------
# Направления перевода
# -------------------------------------------------
DIRECTIONS = {
    "ru_de": {"src": "ru", "dst": "de", "title": "🇷🇺 → 🇩🇪"},
    "de_ru": {"src": "de", "dst": "ru", "title": "🇩🇪 → 🇷🇺"},

    "ru_en": {"src": "ru", "dst": "en", "title": "🇷🇺 → 🇬🇧"},
    "en_ru": {"src": "en", "dst": "ru", "title": "🇬🇧 → 🇷🇺"},

    "ru_tr": {"src": "ru", "dst": "tr", "title": "🇷🇺 → 🇹🇷"},
    "tr_ru": {"src": "tr", "dst": "ru", "title": "🇹🇷 → 🇷🇺"},

    "ru_ro": {"src": "ru", "dst": "ro", "title": "🇷🇺 → 🇷🇴"},
    "ro_ru": {"src": "ro", "dst": "ru", "title": "🇷🇴 → 🇷🇺"},

    # "Сирийский" — берём арабский
    "ru_ar": {"src": "ru", "dst": "ar", "title": "🇷🇺 → 🇸🇾"},
    "ar_ru": {"src": "ar", "dst": "ru", "title": "🇸🇾 → 🇷🇺"},

    "ru_pl": {"src": "ru", "dst": "pl", "title": "🇷🇺 → 🇵🇱"},
    "pl_ru": {"src": "pl", "dst": "ru", "title": "🇵🇱 → 🇷🇺"},
}
DEFAULT_DIRECTION = "ru_de"

# Для распознавания речи
LOCALES = {
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "ar": "ar",      # общий арабский
    "pl": "pl-PL",
}

# -------------------------------------------------
# Память по пользователям (в RAM)
# -------------------------------------------------
user_state: dict[int, dict] = {}


def _get_state(user_id: int) -> dict:
    """Создаём/обновляем состояние пользователя + сброс по дате."""
    state = user_state.get(user_id)
    if not state:
        state = {
            "direction": DEFAULT_DIRECTION,
            "group": DEFAULT_GROUP,
            "used_today": 0,
            "last_date": date.today().isoformat(),
        }
        user_state[user_id] = state
    else:
        today = date.today().isoformat()
        if state.get("last_date") != today:
            state["last_date"] = today
            state["used_today"] = 0
    return state


def get_user_group(user_id: int) -> str:
    return _get_state(user_id)["group"]


def set_user_group(user_id: int, group: str) -> None:
    state = _get_state(user_id)
    if group not in GROUPS:
        group = DEFAULT_GROUP
    state["group"] = group


def get_user_direction(user_id: int) -> str:
    return _get_state(user_id)["direction"]


def set_user_direction(user_id: int, direction: str) -> None:
    state = _get_state(user_id)
    if direction not in DIRECTIONS:
        direction = DEFAULT_DIRECTION
    state["direction"] = direction


def _get_limits_text(user_id: int) -> tuple[str, str, str]:
    """Возвращает (название_группы, лимит_строкой, осталось_строкой)."""
    state = _get_state(user_id)
    group_key = state["group"]
    group = GROUPS.get(group_key, GROUPS[DEFAULT_GROUP])
    used = state["used_today"]
    limit = group["daily_limit"]

    if user_id == FRIEND_ID or limit is None:
        return group["title"], "∞", str(used)

    remaining = max(limit - used, 0)
    return group["title"], str(limit), str(remaining)


def _inc_usage(user_id: int) -> None:
    """Инкремент счётчика, кроме безлимитных."""
    if user_id == FRIEND_ID:
        return
    state = _get_state(user_id)
    group = GROUPS.get(state["group"], GROUPS[DEFAULT_GROUP])
    limit = group["daily_limit"]
    if limit is None:
        return
    state["used_today"] = min(state["used_today"] + 1, limit)


def _can_use(user_id: int) -> bool:
    """Проверка, не выбрали ли лимит на сегодня."""
    if user_id == FRIEND_ID:
        return True
    state = _get_state(user_id)
    group = GROUPS.get(state["group"], GROUPS[DEFAULT_GROUP])
    limit = group["daily_limit"]
    if limit is None:
        return True
    return state["used_today"] < limit


# -------------------------------------------------
# Клавиатура направлений
# -------------------------------------------------
def make_direction_keyboard(selected: str) -> InlineKeyboardMarkup:
    layout = [
        ("ru_de", "de_ru"),
        ("ru_en", "en_ru"),
        ("ru_tr", "tr_ru"),
        ("ru_ro", "ro_ru"),
        ("ru_ar", "ar_ru"),
        ("ru_pl", "pl_ru"),
    ]
    rows = []
    for left, right in layout:
        row = []
        for code in (left, right):
            info = DIRECTIONS[code]
            prefix = "✅ " if code == selected else ""
            row.append(
                InlineKeyboardButton(
                    text=prefix + info["title"],
                    callback_data=f"dir:{code}",
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_status_text(user_id: int) -> str:
    state = _get_state(user_id)
    dir_code = state["direction"]
    dir_info = DIRECTIONS[dir_code]
    group_title, limit_str, remaining_str = _get_limits_text(user_id)

    lines = [
        "Это *Bratik Voice Translator*.",
        "Просто пришли голосовое — я распознаю и переведу.",
        "",
        f"Текущее направление: *{dir_info['title']}*",
        "",
        f"Текущая группа: *{group_title}*",
    ]

    if user_id == FRIEND_ID:
        lines.append("Лимит: *безлимит для преподавателя* ✅")
    else:
        lines.append(f"Лимит: *{limit_str} переводов/день*")
        lines.append(f"Сегодня использовано: *{state['used_today']}*")
        lines.append(f"Осталось сегодня: *{remaining_str}*")

    lines.extend(
        [
            "",
            "🔁 Направление можно переключить кнопкой ниже.",
            "🔑 Пароль от преподавателя активирует полный доступ: /password",
            "",
            "ℹ️ /pricing — тарифы и способы оплаты.",
            "📊 /status — статус лимитов.",
            "📚 /help — краткая инструкция для студентов.",
        ]
    )
    return "\n".join(lines)


# -------------------------------------------------
# Команды
# -------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = build_status_text(user_id)
    keyboard = make_direction_keyboard(get_user_direction(user_id))
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = build_status_text(user_id)
    keyboard = make_direction_keyboard(get_user_direction(user_id))
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def cmd_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "💳 *Тарифы и лимиты:*\n"
        "\n"
        "• Free — 30 переводов/день (для теста).\n"
        "• A1 — 120 переводов/день.\n"
        "• A2 — 200 переводов/день.\n"
        "• B1 — 300 переводов/день.\n"
        "• VIP / Teacher — без лимита.\n"
        "\n"
        "Оплата обычно принимается через преподавателя:\n"
        "• PayPal / Revolut / Wise\n"
        "• Банковский перевод (SEPA / локальный)\n"
        "• По договорённости — криптовалюта (USDT / USDC / BTC).\n"
        "\n"
        "Точные цены и способ оплаты уточняй у своего преподавателя 📚"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отдельная помощь для студентов."""
    text = (
        "📚 *Инструкция для студентов:*\n"
        "\n"
        "1️⃣ Нажми /start — выбери направление перевода (например 🇷🇺→🇩🇪).\n"
        "2️⃣ Пришли *голосовое сообщение* или обычный текст.\n"
        "3️⃣ Я сначала распознаю речь, потом переведу и верну текст.\n"
        "\n"
        "💡 Советы по диктовке:\n"
        "• Говори чётко и не слишком быстро.\n"
        "• Минимизируй шумы вокруг.\n"
        "• Если бот не распознал речь — попробуй ещё раз, покороче.\n"
        "\n"
        "🔑 Если у тебя есть доступ к платному курсу, преподаватель выдаст пароль.\n"
        "Отправь его командой: `/password ваш_пароль`.\n"
        "\n"
        "📊 Команда /status покажет твою группу и лимит на сегодня.\n"
        "Если что-то не работает — напиши своему преподавателю."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "🔑 Отправь пароль так:\n"
            "/password <пароль>\n\n"
            "Доступные уровни: A1, A2, B1, VIP.\n"
            "Пароль выдаёт преподаватель на курсе.",
        )
        return

    code = args[0].strip().lower()

    PASSWORDS_TO_GROUP = {
        "karbofos-a1": "a1",
        "karbofos-a2": "a2",
        "karbofos-b1": "b1",
        "karbofos-vip": "vip",
    }

    group = PASSWORDS_TO_GROUP.get(code)
    if not group:
        await update.message.reply_text(
            "❌ Неверный пароль.\n"
            "Проверь код у преподавателя и попробуй ещё раз."
        )
        return

    prev_group = get_user_group(user_id)
    set_user_group(user_id, group)

    await update.message.reply_text(
        f"✅ Пароль принят!\n"
        f"Твоя группа: *{group.upper()}*.\n"
        f"Предыдущий статус: {prev_group.upper() if prev_group else 'FREE'}.",
        parse_mode="Markdown",
    )


# -------------------------------------------------
# Обработка нажатий по языкам
# -------------------------------------------------
async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data or ""

    await query.answer()

    if not data.startswith("dir:"):
        return
    code = data.split(":", 1)[1]
    current = get_user_direction(user_id)
    if code == current:
        await query.answer("Уже выбрано это направление ✅", show_alert=False)
        return
    if code not in DIRECTIONS:
        await query.answer("Неизвестное направление", show_alert=True)
        return

    set_user_direction(user_id, code)
    text = build_status_text(user_id)
    keyboard = make_direction_keyboard(code)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# -------------------------------------------------
# Перевод
# -------------------------------------------------
def translate_text(text: str, src: str, dst: str) -> str:
    translator = GoogleTranslator(source=src, target=dst)
    return translator.translate(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    user_id = message.from_user.id
    state = _get_state(user_id)
    dir_code = state["direction"]
    dir_info = DIRECTIONS[dir_code]
    src = dir_info["src"]
    dst = dir_info["dst"]

    if not _can_use(user_id):
        group_title, limit_str, _ = _get_limits_text(user_id)
        await message.reply_text(
            f"⛔ Лимит на сегодня исчерпан.\n"
            f"Текущая группа: {group_title}, лимит: {limit_str} переводов/день.\n"
            f"Попроси преподавателя повысить уровень или дождись завтра."
        )
        return

    original = message.text
    try:
        translated = translate_text(original, src, dst)
    except Exception as e:
        logger.exception("Ошибка перевода текста: %s", e)
        await message.reply_text("⚠️ Не удалось перевести текст, попробуй позже.")
        return

    _inc_usage(user_id)

    await message.reply_text(
        f"🎧 *Оригинал* ({src}):\n{original}\n\n"
        f"🌍 *Перевод* ({dst}):\n{translated}",
        parse_mode="Markdown",
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.voice:
        return
    user_id = message.from_user.id
    state = _get_state(user_id)
    dir_code = state["direction"]
    dir_info = DIRECTIONS[dir_code]
    src = dir_info["src"]
    dst = dir_info["dst"]

    if not _can_use(user_id):
        group_title, limit_str, _ = _get_limits_text(user_id)
        await message.reply_text(
            f"⛔ Лимит на сегодня исчерпан.\n"
            f"Текущая группа: {group_title}, лимит: {limit_str} переводов/день.\n"
            f"Попроси преподавателя повысить уровень или дождись завтра."
        )
        return

    try:
        # качаем файл
        file = await context.bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as ogg_f:
            ogg_path = ogg_f.name
        await file.download_to_drive(ogg_path)

        # конвертируем в wav
        wav_path = ogg_path + ".wav"
        AudioSegment.from_file(ogg_path).export(wav_path, format="wav")

        # распознаём речь
        recognizer = sr.Recognizer()
        locale = LOCALES.get(src, "ru-RU")
        logger.info("Распознаю речь с locale=%s", locale)
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        try:
            text = recognizer.recognize_google(audio, language=locale)
        except sr.UnknownValueError:
            logger.warning("Не удалось распознать речь")
            await message.reply_text(
                "🤔 Не удалось распознать речь. Попробуй ещё раз, чуть медленнее."
            )
            return
        except Exception as e:
            logger.exception("Ошибка распознавания речи: %s", e)
            await message.reply_text(
                "⚠️ Ошибка распознавания речи. Попробуй ещё раз позже."
            )
            return

        logger.info("Распознано: %r", text)

        # перевод
        try:
            translated = translate_text(text, src, dst)
        except Exception as e:
            logger.exception("Ошибка перевода речи: %s", e)
            await message.reply_text("⚠️ Не удалось перевести текст, попробуй позже.")
            return

        _inc_usage(user_id)

        await message.reply_text(
            f"🎙 *Оригинал* ({src}):\n{text}\n\n"
            f"🌍 *Перевод* ({dst}):\n{translated}",
            parse_mode="Markdown",
        )

    finally:
        # чистим временные файлы
        try:
            if "ogg_path" in locals() and os.path.exists(ogg_path):
                os.remove(ogg_path)
            if "wav_path" in locals() and os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass


# -------------------------------------------------
# Ошибки
# -------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке апдейта: %s", context.error)


# -------------------------------------------------
# Точка входа
# -------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    application = Application.builder().token(BOT_TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pricing", cmd_pricing))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("password", cmd_password))

    # кнопки выбора направления
    application.add_handler(CallbackQueryHandler(direction_button))

    # сообщения
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    application.add_error_handler(error_handler)

    if BASE_WEBHOOK_URL:
        webhook_url = f"{BASE_WEBHOOK_URL.rstrip('/')}/webhook"
        logger.info("Запускаю webhook на %s, порт %s", webhook_url, PORT)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=webhook_url,
        )
    else:
        logger.info("BASE_WEBHOOK_URL не задан, запускаю polling")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
