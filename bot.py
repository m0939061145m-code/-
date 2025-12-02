import logging
import os
import tempfile
from datetime import datetime, timezone
from io import BytesIO

import speech_recognition as sr
from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# ---------------------- КОНФИГ ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://bratik.onrender.com/webhook")
PORT = int(os.getenv("PORT", "10000"))

# Твои ID с безлимитом
UNLIMITED_USERS = {
    7707071177,   # ты
    1300323894,   # друг в Германии
}

# ---------------------- НАПРАВЛЕНИЯ ПЕРЕВОДА ----------------------
# Главное: любой язык -> немецкий, и немецкий -> другие
DIRECTIONS = {
    "auto_de": {
        "src": "auto",   # автоопределение языка переводчиком
        "dest": "de",
        "label": "🌐 → 🇩🇪 Любой язык → Немецкий",
    },
    "de_ru": {
        "src": "de",
        "dest": "ru",
        "label": "🇩🇪 → 🇷🇺 Немецкий → Русский",
    },
    "de_en": {
        "src": "de",
        "dest": "en",
        "label": "🇩🇪 → 🇬🇧 Немецкий → Английский",
    },
    "de_tr": {
        "src": "de",
        "dest": "tr",
        "label": "🇩🇪 → 🇹🇷 Немецкий → Турецкий",
    },
    "de_ro": {
        "src": "de",
        "dest": "ro",
        "label": "🇩🇪 → 🇷🇴 Немецкий → Румынский",
    },
    "de_pl": {
        "src": "de",
        "dest": "pl",
        "label": "🇩🇪 → 🇵🇱 Немецкий → Польский",
    },
    "de_ar": {
        "src": "de",
        "dest": "ar",
        "label": "🇩🇪 → 🇸🇾 Немецкий → Сирийский (арабский)",
    },
}

# Языки для распознавания речи (SpeechRecognition)
# Для режима "любой язык → нем" по умолчанию считаем, что студент говорит на русском
STT_LOCALES = {
    "auto": "ru-RU",  # авто → de: распознаём как русский (под твою аудиторию)
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "pl": "pl-PL",
    "ar": "ar-SY",
}

# Языки для озвучки (gTTS)
TTS_LANG_MAP = {
    "ru": "ru",
    "de": "de",
    "en": "en",
    "tr": "tr",
    "ro": "ro",
    "pl": "pl",
    "ar": "ar",
}

# ---------------------- ЛИМИТЫ И ГРУППЫ ----------------------
GROUP_LIMITS = {
    "FREE": 20,   # без пароля
    "A1": 100,
    "A2": 200,
    "B1": 300,
}

PASSWORDS = {
    "karbofosA1": "A1",
    "karbofosA2": "A2",
    "karbofosB1": "B1",
}

USER_GROUP: dict[int, str] = {}
USER_USAGE: dict[tuple[int, str], int] = {}
AWAITING_PASSWORD: dict[int, bool] = {}

# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------------


def today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_user_group(user_id: int) -> str:
    if user_id in UNLIMITED_USERS:
        return "UNLIMITED"
    return USER_GROUP.get(user_id, "FREE")


def get_user_limit(user_id: int) -> int | None:
    if user_id in UNLIMITED_USERS:
        return None
    group = USER_GROUP.get(user_id, "FREE")
    return GROUP_LIMITS.get(group, GROUP_LIMITS["FREE"])


def inc_usage_and_check_limit(user_id: int) -> bool:
    """Инкремент счётчика и проверка лимита. True — можно, False — лимит исчерпан."""
    limit = get_user_limit(user_id)
    if limit is None:
        return True

    key = (user_id, today_key())
    used = USER_USAGE.get(key, 0)

    if used >= limit:
        return False

    USER_USAGE[key] = used + 1
    return True


def get_usage_string(user_id: int) -> str:
    group = get_user_group(user_id)
    key = (user_id, today_key())
    used = USER_USAGE.get(key, 0)
    limit = get_user_limit(user_id)

    if limit is None:
        return f"Группа: {group} — у тебя безлимит ✅"

    return f"Группа: {group}\nСегодня переводов: {used}/{limit}"


def get_current_direction_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("direction_key", "auto_de")


def set_current_direction_key(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    context.user_data["direction_key"] = key


def make_direction_keyboard(current_key: str | None) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    # Первая строка — любой язык → немецкий
    order = [
        ("auto_de",),
        ("de_ru", "de_en"),
        ("de_tr", "de_ro"),
        ("de_pl", "de_ar"),
    ]

    for row_keys in order:
        row: list[InlineKeyboardButton] = []
        for key in row_keys:
            data = DIRECTIONS[key]
            text = data["label"]
            if key == current_key:
                text = "✅ " + text
            row.append(InlineKeyboardButton(text=text, callback_data=f"dir:{key}"))
        buttons.append(row)

    # Нижний ряд — помощь и лимиты
    buttons.append(
        [
            InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help"),
            InlineKeyboardButton("📊 Лимиты", callback_data="menu:limits"),
        ]
    )

    return InlineKeyboardMarkup(buttons)


async def tts_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    lang_code: str,
) -> None:
    """Создать озвучку перевода и отправить как голосовое сообщение."""
    if not text or not text.strip():
        return

    tts_lang = TTS_LANG_MAP.get(lang_code, "en")
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            tts = gTTS(text=text, lang=tts_lang)
            tts.save(tmp_path)

        with open(tmp_path, "rb") as f:
            await context.bot.send_voice(chat_id=chat_id, voice=f)
    except Exception as e:
        logger.error("Ошибка при озвучке: %s", e)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def translate_text(src: str, dest: str, text: str) -> str:
    """Перевод текста через deep_translator (Google)."""
    if src == "auto":
        translator = GoogleTranslator(source="auto", target=dest)
    else:
        translator = GoogleTranslator(source=src, target=dest)
    return translator.translate(text)


# ---------------------- ХЭНДЛЕРЫ КОМАНД ----------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id if user else 0

    logger.info("Пользователь %s (%s) запустил /start", user_id, user.username)

    current_key = get_current_direction_key(context)
    dir_label = DIRECTIONS[current_key]["label"]

    text = (
        "👋 Привет! Я голосовой переводчик для занятий немецким языком.\n\n"
        "1️⃣ Отправь *голосовое сообщение* — я распознаю текст.\n"
        "2️⃣ Переведу его по выбранному направлению.\n"
        "3️⃣ Озвучу перевод голосом.\n\n"
        f"Текущее направление: *{dir_label}*\n\n"
        "👉 Можешь переключить направление переводов кнопками ниже.\n\n"
        "ℹ️ Для подробной инструкции напиши /help\n"
        "📊 Чтобы посмотреть лимиты — /limits\n"
        "🔑 Чтобы ввести пароль от группы — /password"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=make_direction_keyboard(current_key),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Помощь для студентов."""
    text = (
        "📚 *Как пользоваться ботом (для студентов)*\n\n"
        "1️⃣ Выбери направление перевода:\n"
        "   • 🌐 → 🇩🇪 Любой язык → Немецкий — говоришь на своём языке, получаешь немецкий.\n"
        "   • 🇩🇪 → 🇷🇺 / 🇬🇧 / 🇹🇷 / 🇷🇴 / 🇵🇱 / 🇸🇾 — говоришь по-немецки, получаешь перевод.\n\n"
        "2️⃣ Запиши голосовое сообщение:\n"
        "   • Кнопка микрофона в Telegram.\n"
        "   • Говори чётко, без сильного шума.\n\n"
        "3️⃣ В ответ бот пришлёт:\n"
        "   • Текст того, что ты сказал.\n"
        "   • Перевод.\n"
        "   • Голосом озвучит перевод.\n\n"
        "4️⃣ Лимиты:\n"
        "   • Без пароля — ограниченное количество переводов в день.\n"
        "   • С паролем от группы (A1/A2/B1) — лимит больше.\n"
        "   • Пароль даёт преподаватель.\n\n"
        "Команды:\n"
        "   • /start — перезапустить меню.\n"
        "   • /help — эта подсказка.\n"
        "   • /limits — посмотреть оставшиеся переводы.\n"
        "   • /password — ввести пароль от курса.\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = "📊 Твои лимиты на сегодня:\n\n" + get_usage_string(user_id)
    await update.message.reply_text(text)


async def password_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    AWAITING_PASSWORD[user_id] = True
    await update.message.reply_text(
        "🔑 Введи пароль от своей группы (например: *karbofosA1*).\n"
        "Если передумал — просто отправь /start.",
        parse_mode="Markdown",
    )


# ---------------------- CALLBACK ДЛЯ КНОПОК ----------------------


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    # Переключение направления
    if data.startswith("dir:"):
        key = data.split(":", 1)[1]
        if key not in DIRECTIONS:
            return

        set_current_direction_key(context, key)
        dir_label = DIRECTIONS[key]["label"]

        msg = f"✅ Направление перевода сменено на:\n*{dir_label}*"

        try:
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=make_direction_keyboard(key),
            )
        except Exception as e:
            logger.warning("Не удалось отредактировать сообщение с клавиатурой: %s", e)
            await query.message.reply_text(
                msg,
                parse_mode="Markdown",
                reply_markup=make_direction_keyboard(key),
            )

    elif data == "menu:help":
        await help_command(update, context)

    elif data == "menu:limits":
        user_id = query.from_user.id
        text = "📊 Твои лимиты на сегодня:\n\n" + get_usage_string(user_id)
        await query.message.reply_text(text)


# ---------------------- ОБРАБОТКА ПАРОЛЕЙ ----------------------


async def handle_password_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True, если сообщение было обработано как пароль."""
    user_id = update.effective_user.id

    if not AWAITING_PASSWORD.get(user_id):
        return False

    text = (update.message.text or "").strip()

    # Если человек передумал
    if text.startswith("/"):
        AWAITING_PASSWORD[user_id] = False
        return False

    group = PASSWORDS.get(text)
    if not group:
        await update.message.reply_text(
            "❌ Неверный пароль.\n"
            "Попробуй ещё раз или спроси преподавателя.",
        )
        return True

    USER_GROUP[user_id] = group
    AWAITING_PASSWORD[user_id] = False

    await update.message.reply_text(
        f"✅ Пароль принят. Твоя группа: *{group}*.\n"
        "Теперь у тебя увеличенный дневной лимит переводов.\n\n"
        "Посмотреть лимиты: /limits",
        parse_mode="Markdown",
    )
    return True


# ---------------------- ОБРАБОТКА ГОЛОСОВЫХ ----------------------


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id

    # сначала пробуем распознать как пароль
    if await handle_password_text(update, context):
        return

    current_key = get_current_direction_key(context)
    direction = DIRECTIONS[current_key]
    src_lang = direction["src"]
    dest_lang = direction["dest"]

    stt_locale = STT_LOCALES.get(src_lang, "ru-RU")

    logger.info(
        "Получено voice от %s. Направление: %s (%s -> %s)",
        user_id,
        current_key,
        src_lang,
        dest_lang,
    )

    voice = update.message.voice
    if not voice:
        await update.message.reply_text("Не нашёл голосовое сообщение 😅 Попробуй ещё раз.")
        return

    # Скачиваем ogg-файл
    try:
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.error("Ошибка скачивания voice: %s", e)
        await update.message.reply_text("Не удалось скачать голосовое сообщение 😔")
        return

    # Конвертируем в WAV
    tmp_wav = None
    try:
        audio = AudioSegment.from_file(BytesIO(file_bytes), format="ogg")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name
            audio.export(tmp_wav, format="wav")
    except Exception as e:
        logger.error("Ошибка конвертации аудио: %s", e)
        await update.message.reply_text("Не удалось обработать аудио 😔 Попробуй ещё раз.")
        return

    recognizer = sr.Recognizer()

    # Распознаём речь
    try:
        with sr.AudioFile(tmp_wav) as source:
            audio_data = recognizer.record(source)

        logger.info("Распознаю речь с locale=%s", stt_locale)
        original_text = recognizer.recognize_google(audio_data, language=stt_locale)
        logger.info("Распознано: %r", original_text)
    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь (UnknownValueError)")
        await update.message.reply_text(
            "🧐 Не удалось распознать речь.\n"
            "Попробуй сказать чуть чётче или короче фразу.",
        )
        if tmp_wav and os.path.exists(tmp_wav):
            os.remove(tmp_wav)
        return
    except Exception as e:
        logger.error("Ошибка распознавания речи: %s", e)
        await update.message.reply_text("Произошла ошибка при распознавании речи 😔")
        if tmp_wav and os.path.exists(tmp_wav):
            os.remove(tmp_wav)
        return
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            os.remove(tmp_wav)

    # Проверка лимита (берём лимит только если распознали текст)
    if not inc_usage_and_check_limit(user_id):
        text = (
            "⚠️ Лимит переводов на сегодня исчерпан.\n\n"
            "Спроси у преподавателя пароль от своей группы (A1/A2/B1), "
            "чтобы увеличить количество переводов.\n\n"
            "Текущий статус:\n" + get_usage_string(user_id)
        )
        await update.message.reply_text(text)
        return

    # Перевод
    try:
        logger.info(
            "Перевожу текст. src=%s dest=%s text=%r", src_lang, dest_lang, original_text
        )
        translated_text = translate_text(src_lang, dest_lang, original_text)
    except Exception as e:
        logger.error("Ошибка перевода: %s", e)
        await update.message.reply_text("Произошла ошибка при переводе 😔 Попробуй ещё раз.")
        return

    # Ответ текстом
    src_label = "авто" if src_lang == "auto" else src_lang
    reply_text = (
        f"🗣 *Оригинал* ({src_label}):\n"
        f"{original_text}\n\n"
        f"🌐 *Перевод* ({dest_lang}):\n"
        f"{translated_text}\n\n"
        f"{get_usage_string(user_id)}"
    )

    await update.message.reply_text(reply_text, parse_mode="Markdown")

    # Озвучка перевода
    if dest_lang in TTS_LANG_MAP:
        await tts_and_send(context, chat.id, translated_text, dest_lang)


# ---------------------- ОБРАБОТКА ТЕКСТА (ПАРОЛИ / ПЕРЕВОД) ----------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # сначала проверяем, не вводит ли пользователь пароль
    if await handle_password_text(update, context):
        return

    text = (update.message.text or "").strip()
    # Если это команда — не трогаем здесь
    if text.startswith("/"):
        return

    user_id = update.effective_user.id

    current_key = get_current_direction_key(context)
    direction = DIRECTIONS[current_key]
    src_lang = direction["src"]
    dest_lang = direction["dest"]

    # Проверка лимита
    if not inc_usage_and_check_limit(user_id):
        msg = (
            "⚠️ Лимит переводов на сегодня исчерпан.\n\n"
            "Спроси у преподавателя пароль от своей группы (A1/A2/B1), "
            "чтобы увеличить количество переводов.\n\n"
            "Текущий статус:\n" + get_usage_string(user_id)
        )
        await update.message.reply_text(msg)
        return

    try:
        translated_text = translate_text(src_lang, dest_lang, text)
    except Exception as e:
        logger.error("Ошибка перевода текста: %s", e)
        await update.message.reply_text("Произошла ошибка при переводе 😔 Попробуй ещё раз.")
        return

    src_label = "авто" if src_lang == "auto" else src_lang
    reply_text = (
        f"📝 *Оригинал* ({src_label}):\n"
        f"{text}\n\n"
        f"🌐 *Перевод* ({dest_lang}):\n"
        f"{translated_text}\n\n"
        f"{get_usage_string(user_id)}"
    )
    await update.message.reply_text(reply_text, parse_mode="Markdown")

    # Озвучка перевода
    if dest_lang in TTS_LANG_MAP:
        chat_id = update.effective_chat.id
        await tts_and_send(context, chat_id, translated_text, dest_lang)


# ---------------------- MAIN / WEBHOOK ----------------------


def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Не задан BOT_TOKEN. Укажи токен бота в переменной окружения BOT_TOKEN.")

    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("limits", limits_command))
    application.add_handler(CommandHandler("password", password_command))

    # Кнопки
    application.add_handler(CallbackQueryHandler(button_callback))

    # Голосовые
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice))

    # Обычный текст (пароль + текстовый перевод)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Запускаю webhook на %s, порт %s", WEBHOOK_URL, PORT)

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
    )


if __name__ == "__main__":
    main()
