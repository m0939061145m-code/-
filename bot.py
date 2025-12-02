import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, Tuple, Optional

import speech_recognition as sr
from deep_translator import GoogleTranslator
from pydub import AudioSegment

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------- ЛОГИРОВАНИЕ ----------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------- КОНФИГ ---------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN / TELEGRAM_TOKEN в переменных окружения")

# Render обычно прокидывает RENDER_EXTERNAL_URL
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "users.db"

# Направления перевода: code -> (src_lang, dest_lang, speech_locale, human_readable)
DIRECTIONS: Dict[str, Tuple[str, str, str, str]] = {
    "ru_de": ("ru", "de", "ru-RU", "Русский → Немецкий"),
    "de_ru": ("de", "ru", "de-DE", "Немецкий → Русский"),

    "ru_en": ("ru", "en", "ru-RU", "Русский → Английский"),
    "en_ru": ("en", "ru", "en-US", "Английский → Русский"),

    "ru_tr": ("ru", "tr", "ru-RU", "Русский → Турецкий"),
    "tr_ru": ("tr", "ru", "tr-TR", "Турецкий → Русский"),

    "ru_ro": ("ru", "ro", "ru-RU", "Русский → Румынский"),
    "ro_ru": ("ro", "ru", "ro-RO", "Румынский → Русский"),

    # Сирийский арабский
    "ru_ar": ("ru", "ar", "ru-RU", "Русский → Арабский (сирийский)"),
    "ar_ru": ("ar", "ru", "ar-SY", "Арабский (сирийский) → Русский"),

    "ru_pl": ("ru", "pl", "ru-RU", "Русский → Польский"),
    "pl_ru": ("pl", "ru", "pl-PL", "Польский → Русский"),
}
DEFAULT_DIRECTION = "ru_de"

# Пароли и группы
PASSWORDS: Dict[str, str] = {
    "DEMO123": "DEMO",
    "A1PASS": "A1",
    "A2PASS": "A2",
    "B1PASS": "B1",
    # ты — админ
    "ADMIN777": "ADMIN",
    # друг в Германии — преподаватель
    "GERMANYPRO": "TEACHER",
}

# Лимиты по группам (в день)
GROUP_LIMITS: Dict[str, int] = {
    "DEMO": 10,        # демо-доступ
    "A1": 30,
    "A2": 40,
    "B1": 60,
    "TEACHER": 10_000, # преподаватель — условный безлимит
    "ADMIN": 10_000,   # ты — безлимит
}


# ---------------------- БАЗА ДАННЫХ ----------------------


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            group_name  TEXT NOT NULL,
            used_today  INTEGER NOT NULL,
            date        TEXT NOT NULL,
            direction   TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def today_str() -> str:
    # Можно оставить UTC — для лимитов по суткам достаточно
    return datetime.utcnow().strftime("%Y-%m-%d")


def get_user(user_id: int) -> Dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, group_name, used_today, date, direction FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        # создаём по умолчанию DEMO
        row = (user_id, "DEMO", 0, today_str(), DEFAULT_DIRECTION)
        cur.execute(
            "INSERT INTO users (user_id, group_name, used_today, date, direction) VALUES (?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()

    user = {
        "user_id": row[0],
        "group": row[1],
        "used_today": row[2],
        "date": row[3],
        "direction": row[4],
    }
    conn.close()

    # если дата устарела — обнуляем счётчик
    if user["date"] != today_str():
        reset_daily_usage(user_id, keep_group=user["group"], direction=user["direction"])
        user["used_today"] = 0
        user["date"] = today_str()
    return user


def reset_daily_usage(user_id: int, keep_group: Optional[str] = None, direction: Optional[str] = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if keep_group is None or direction is None:
        cur.execute("SELECT group_name, direction FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            keep_group = keep_group or row[0]
            direction = direction or row[1]
        else:
            keep_group = keep_group or "DEMO"
            direction = direction or DEFAULT_DIRECTION

    cur.execute(
        """
        UPDATE users
        SET used_today = 0,
            date = ?,
            group_name = ?,
            direction = ?
        WHERE user_id = ?
        """,
        (today_str(), keep_group, direction, user_id),
    )
    conn.commit()
    conn.close()


def set_user_group(user_id: int, group_name: str) -> None:
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET group_name = ?, used_today = ?
        WHERE user_id = ?
        """,
        (group_name, user["used_today"], user_id),
    )
    conn.commit()
    conn.close()


def set_user_direction(user_id: int, direction: str) -> None:
    user = get_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE users
        SET direction = ?
        WHERE user_id = ?
        """,
        (direction, user_id),
    )
    conn.commit()
    conn.close()


def increment_usage(user_id: int) -> int:
    user = get_user(user_id)
    new_used = user["used_today"] + 1
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET used_today = ?, date = ? WHERE user_id = ?",
        (new_used, today_str(), user_id),
    )
    conn.commit()
    conn.close()
    return new_used


def check_and_increment_limit(user_id: int) -> Tuple[bool, int, int, bool]:
    """
    Возвращает:
      allowed: можно ли выполнить запрос
      used: сколько уже использовано сегодня
      limit: лимит для группы
      reset: был ли today-сброс
    """
    user = get_user(user_id)
    group = user["group"]

    # ADMIN / TEACHER — безлимит
    if group in ("ADMIN", "TEACHER"):
        return True, 0, 10_000, False

    limit = GROUP_LIMITS.get(group, GROUP_LIMITS["DEMO"])
    used = user["used_today"]

    if used >= limit:
        return False, used, limit, False

    new_used = increment_usage(user_id)
    return True, new_used, limit, False


# ---------------------- КЛАВИАТУРА ЯЗЫКОВ ----------------


def make_direction_keyboard(current_direction: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("RU → DE", callback_data="dir:ru_de"),
            InlineKeyboardButton("DE → RU", callback_data="dir:de_ru"),
        ],
        [
            InlineKeyboardButton("RU → EN", callback_data="dir:ru_en"),
            InlineKeyboardButton("EN → RU", callback_data="dir:en_ru"),
        ],
        [
            InlineKeyboardButton("RU → TR", callback_data="dir:ru_tr"),
            InlineKeyboardButton("TR → RU", callback_data="dir:tr_ru"),
        ],
        [
            InlineKeyboardButton("RU → RO", callback_data="dir:ru_ro"),
            InlineKeyboardButton("RO → RU", callback_data="dir:ro_ru"),
        ],
        [
            InlineKeyboardButton("RU → AR", callback_data="dir:ru_ar"),
            InlineKeyboardButton("AR → RU", callback_data="dir:ar_ru"),
        ],
        [
            InlineKeyboardButton("RU → PL", callback_data="dir:ru_pl"),
            InlineKeyboardButton("PL → RU", callback_data="dir:pl_ru"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


# ---------------------- ОБРАБОТЧИКИ КОМАНД ----------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)

    group = user["group"]
    direction_code = user["direction"]
    src, dest, locale, human = DIRECTIONS.get(direction_code, DIRECTIONS[DEFAULT_DIRECTION])

    logger.info("Пользователь %s запустил /start", user_id)

    text_lines = [
        "👋 Привет! Я бот-переводчик голосовых сообщений.",
        "",
        "🎧 Отправь мне голосовое — я распознаю текст и переведу его.",
        "",
        f"Текущее направление перевода: *{human}*",
        "Поменять язык можно кнопкой ниже.",
        "",
    ]

    if group == "ADMIN":
        text_lines.append("🔐 Ты отмечен как *ADMIN* — для тебя лимитов нет 💼")
    elif group == "TEACHER":
        text_lines.append("👨‍🏫 Ты отмечен как *TEACHER* — для тебя лимитов нет.")
        text_lines.append("Можешь выдавать студентам пароли для уровней A1/A2/B1.")
    else:
        limit = GROUP_LIMITS.get(group, GROUP_LIMITS["DEMO"])
        used = user["used_today"]
        remaining = max(limit - used, 0)

        if group == "DEMO":
            text_lines.append(
                f"🧪 У тебя *DEMO* доступ: {limit} переводов в день.\n"
                f"Осталось на сегодня: *{remaining}*."
            )
            text_lines.append(
                "Чтобы получить полный доступ, введи пароль от преподавателя:\n"
                "`/password ПАРОЛЬ`"
            )
        else:
            text_lines.append(
                f"📚 Твой уровень: *{group}*\n"
                f"Дневной лимит: *{limit}* переводов.\n"
                f"Уже использовано сегодня: *{used}*, осталось: *{remaining}*."
            )

    text_lines.append("")
    text_lines.append("💳 Команда `/pricing` — тарифы и способы оплаты.")
    text_lines.append("ℹ `/status` — твой текущий лимит.\n👥 `/groupinfo` — статус доступа.")

    await update.message.reply_text(
        "\n".join(text_lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_direction_keyboard(direction_code),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🆘 Помощь\n\n"
        "1️⃣ Отправь голосовое сообщение — я распознаю и переведу.\n"
        "2️⃣ Направление перевода выбирается кнопками под сообщением.\n"
        "3️⃣ `/password ПАРОЛЬ` — ввести пароль от преподавателя и поднять уровень.\n"
        "4️⃣ `/status` — посмотреть, сколько переводов осталось сегодня.\n"
        "5️⃣ `/groupinfo` — твой уровень доступа.\n"
        "6️⃣ `/pricing` — тарифы и способы оплаты.\n"
    )
    await update.message.reply_text(text)


async def password_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text("Отправь команду в формате:\n`/password ПАРОЛЬ`", parse_mode=ParseMode.MARKDOWN)
        return

    code = args[0].strip().upper()
    group = PASSWORDS.get(code)

    if not group:
        await update.message.reply_text("❌ Неверный пароль. Проверь и попробуй ещё раз.")
        return

    set_user_group(user_id, group)
    limit = GROUP_LIMITS.get(group, GROUP_LIMITS["DEMO"])

    msg = [f"✅ Пароль принят. Твоя новая группа: *{group}*."]
    if group == "ADMIN":
        msg.append("Ты теперь *ADMIN* — для тебя лимитов нет 💼.")
    elif group == "TEACHER":
        msg.append("Ты теперь *TEACHER* — для тебя лимитов нет 👨‍🏫.")
    else:
        msg.append(f"Твой дневной лимит: *{limit}* переводов.")

    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)
    group = user["group"]

    if group in ("ADMIN", "TEACHER"):
        text = (
            f"👤 Твой статус: *{group}*\n"
            "Лимитов на количество переводов в день нет.\n"
            "Можешь пользоваться сколько угодно 😊"
        )
    else:
        limit = GROUP_LIMITS.get(group, GROUP_LIMITS["DEMO"])
        used = user["used_today"]
        remaining = max(limit - used, 0)
        text = (
            f"👤 Твой статус: *{group}*\n"
            f"Сегодня уже использовано: *{used}* переводов.\n"
            f"Дневной лимит: *{limit}*.\n"
            f"Осталось на сегодня: *{remaining}*."
        )

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def groupinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id)
    group = user["group"]

    lines = [f"👥 Твой уровень доступа: *{group}*"]

    if group == "ADMIN":
        lines.append("Ты — ADMIN. У тебя полный безлимит и ты можешь управлять всеми паролями.")
    elif group == "TEACHER":
        lines.append(
            "Ты — TEACHER. У тебя безлимит.\n"
            "Ты можешь выдавать студентам пароли для уровней *DEMO / A1 / A2 / B1*."
        )
    else:
        limit = GROUP_LIMITS.get(group, GROUP_LIMITS["DEMO"])
        used = user["used_today"]
        remaining = max(limit - used, 0)
        lines.append(f"Твой дневной лимит: *{limit}* переводов (осталось сегодня: *{remaining}*).")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def pricing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Просто текстовое описание тарифов и способов оплаты.
    Тут дальше можно будет прикрутить реальные ссылки/инвойсы.
    """
    text = (
        "💳 *Тарифы и оплата*\n\n"
        "Мы используем модель помесячной подписки:\n\n"
        "• *DEMO* — бесплатно, до 10 переводов в день (для теста).\n"
        "• *A1* — базовый уровень, до 30 переводов в день.\n"
        "• *A2* — до 40 переводов в день.\n"
        "• *B1* — до 60 переводов в день (интенсивная практика).\n\n"
        "👨‍🏫 Для преподавателей (TEACHER) доступ без лимитов по отдельной договорённости.\n\n"
        "🔐 Доступ выдаётся через пароль `/password ПАРОЛЬ`.\n"
        "Пароль ты получаешь у преподавателя или администратора.\n\n"
        "💰 *Популярные способы оплаты в Европе и Германии:*\n"
        "• Банковский перевод (SEPA)\n"
        "• Кредитная / дебетовая карта (Visa / MasterCard)\n"
        "• PayPal\n"
        "• Revolut / Wise\n"
        "• Наличные (если вы занимаетесь офлайн на курсах)\n\n"
        "📩 Для подключения платного доступа напиши преподавателю или админу в Telegram.\n"
        "_(В коде бота ты можешь вписать свой @username, чтобы студентам было проще связываться.)_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------- КЛАВИАТУРА / CALLBACK ----------------


async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = get_user(user_id)

    data = query.data
    if not data.startswith("dir:"):
        return

    new_direction = data.split(":", 1)[1]
    if new_direction not in DIRECTIONS:
        return

    # если направление не изменилось — не трогаем сообщение
    if user["direction"] == new_direction:
        return

    set_user_direction(user_id, new_direction)
    src, dest, locale, human = DIRECTIONS[new_direction]
    logger.info("Пользователь %s сменил направление на %s", user_id, new_direction)

    msg = f"🔁 Направление перевода изменено на: *{human}*"
    try:
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_direction_keyboard(new_direction),
        )
    except BadRequest as e:
        # если сообщение не изменилось — просто игнорируем
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error("Ошибка при редактировании сообщения: %s", e)


# ---------------------- ОБРАБОТКА ГОЛОСОВЫХ ----------------


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = update.effective_user.id
    user = get_user(user_id)

    allowed, used, limit, _ = check_and_increment_limit(user_id)
    if not allowed and user["group"] not in ("ADMIN", "TEACHER"):
        await message.reply_text(
            f"⚠ Ты достиг дневного лимита переводов.\n"
            f"Группа: *{user['group']}*, лимит: *{limit}*.\n"
            f"Попроси преподавателя выдать новый пароль или повысить уровень.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    voice = message.voice
    if not voice:
        await message.reply_text("Отправь именно голосовое сообщение, не аудио-файл.")
        return

    direction_code = user["direction"]
    src_lang, dest_lang, locale, human = DIRECTIONS.get(direction_code, DIRECTIONS[DEFAULT_DIRECTION])

    # шаг 1 — скачиваем файл
    file = await context.bot.get_file(voice.file_id)

    ogg_path = f"/tmp/{voice.file_id}.oga"
    wav_path = f"/tmp/{voice.file_id}.wav"

    await file.download_to_drive(ogg_path)

    # шаг 2 — конвертация в wav через pydub (ffmpeg)
    try:
        audio = AudioSegment.from_file(ogg_path)
        audio.export(wav_path, format="wav")
    except Exception as e:
        logger.exception("Ошибка при конвертации аудио: %s", e)
        await message.reply_text("❌ Не удалось обработать аудио-файл.")
        return

    # шаг 3 — распознавание речи
    recognizer = sr.Recognizer()

    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)

    logger.info("Распознаю речь с locale=%s", locale)
    try:
        recognized_text = recognizer.recognize_google(audio_data, language=locale)
        logger.info("Распознано: %r", recognized_text)
    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь")
        await message.reply_text("😔 Не удалось распознать речь. Попробуй сказать чуть чётче или ближе к микрофону.")
        return
    except sr.RequestError as e:
        logger.error("Ошибка сервиса распознавания: %s", e)
        await message.reply_text("⚠ Сервис распознавания сейчас недоступен. Попробуй чуть позже.")
        return

    # шаг 4 — перевод
    logger.info("Перевожу текст. src=%s dest=%s text=%r", src_lang, dest_lang, recognized_text)
    try:
        translated = GoogleTranslator(source=src_lang, target=dest_lang).translate(recognized_text)
    except Exception as e:
        logger.exception("Ошибка при переводе: %s", e)
        await message.reply_text("⚠ Не удалось перевести текст. Попробуй ещё раз.")
        return

    reply_lines = [
        f"🎯 Направление: *{human}*",
        "",
        f"🗣 *Исходный текст:*",
        recognized_text,
        "",
        f"🌍 *Перевод:*",
        translated,
    ]

    await message.reply_text("\n".join(reply_lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------- MAIN / WEBHOOK --------------------


def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("password", password_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("groupinfo", groupinfo_command))
    application.add_handler(CommandHandler("pricing", pricing_command))

    # кнопки выбора языка
    application.add_handler(CallbackQueryHandler(direction_button, pattern=r"^dir:"))

    # голосовые сообщения
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    if not BASE_WEBHOOK_URL:
        raise RuntimeError("Не задан BASE_WEBHOOK_URL или RENDER_EXTERNAL_URL для webhook")

    webhook_url = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
    logger.info("Запускаю webhook на %s, порт %s", webhook_url, PORT)

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()
