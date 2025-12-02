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

# ==========================
#  БАЗОВЫЕ НАСТРОЙКИ
# ==========================

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")  # ОБЯЗАТЕЛЬНО в Render → Environment
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://bratik.onrender.com/webhook")
PORT = int(os.getenv("PORT", "10000"))

# Ты + друг в Германии — без лимитов
UNLIMITED_USERS = {
    7707071177,   # ты (судя по логам)
    1300323894,   # друг в Германии
}

# ==========================
#  ЯЗЫКИ И НАПРАВЛЕНИЯ
# ==========================

# Направления перевода для клавиатуры
DIRECTIONS = {
    "ru_de": {
        "src": "ru",
        "dest": "de",
        "label": "🇷🇺 → 🇩🇪 Русский → Немецкий",
    },
    "de_ru": {
        "src": "de",
        "dest": "ru",
        "label": "🇩🇪 → 🇷🇺 Немецкий → Русский",
    },
    "ru_en": {
        "src": "ru",
        "dest": "en",
        "label": "🇷🇺 → 🇬🇧 Русский → Английский",
    },
    "en_ru": {
        "src": "en",
        "dest": "ru",
        "label": "🇬🇧 → 🇷🇺 Английский → Русский",
    },
    "ru_tr": {
        "src": "ru",
        "dest": "tr",
        "label": "🇷🇺 → 🇹🇷 Русский → Турецкий",
    },
    "tr_ru": {
        "src": "tr",
        "dest": "ru",
        "label": "🇹🇷 → 🇷🇺 Турецкий → Русский",
    },
    "ru_ro": {
        "src": "ru",
        "dest": "ro",
        "label": "🇷🇺 → 🇷🇴 Русский → Румынский",
    },
    "ro_ru": {
        "src": "ro",
        "dest": "ru",
        "label": "🇷🇴 → 🇷🇺 Румынский → Русский",
    },
    "ru_pl": {
        "src": "ru",
        "dest": "pl",
        "label": "🇷🇺 → 🇵🇱 Русский → Польский",
    },
    "pl_ru": {
        "src": "pl",
        "dest": "ru",
        "label": "🇵🇱 → 🇷🇺 Польский → Русский",
    },
    "ru_ar": {
        "src": "ru",
        "dest": "ar",
        "label": "🇷🇺 → 🇸🇾 Русский → Сирийский (арабский)",
    },
    "ar_ru": {
        "src": "ar",
        "dest": "ru",
        "label": "🇸🇾 → 🇷🇺 Сирийский (арабский) → Русский",
    },
}

# Для распознавания речи (SpeechRecognition)
STT_LOCALES = {
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "pl": "pl-PL",
    "ar": "ar-SY",  # грубо, но для наших задач ок
}

# Для озвучки (gTTS)
TTS_LANG_MAP = {
    "ru": "ru",
    "de": "de",
    "en": "en",
    "tr": "tr",
    "ro": "ro",
    "pl": "pl",
    "ar": "ar",
}

# ==========================
#  ГРУППЫ, ПАРОЛИ И ЛИМИТЫ
# ==========================

GROUP_LIMITS = {
    "FREE": 20,   # демо для всех без пароля
    "A1": 100,
    "A2": 200,
    "B1": 300,
}

PASSWORDS = {
    "karbofosA1": "A1",
    "karbofosA2": "A2",
    "karbofosB1": "B1",
}

# user_id -> "FREE" | "A1" | "A2" | "B1"
USER_GROUP: dict[int, str] = {}

# (user_id, "YYYY-MM-DD") -> count
USER_USAGE: dict[tuple[int, str], int] = {}

# user_id -> bool (ждём пароль текстом)
AWAITING_PASSWORD: dict[int, bool] = {}

# ==========================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================

def today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_user_group(user_id: int) -> str:
    if user_id in UNLIMITED_USERS:
        return "UNLIMITED"
    return USER_GROUP.get(user_id, "FREE")


def get_user_limit(user_id: int) -> int | None:
    """None = безлимит"""
    if user_id in UNLIMITED_USERS:
        return None
    group = USER_GROUP.get(user_id, "FREE")
    return GROUP_LIMITS.get(group, GROUP_LIMITS["FREE"])


def inc_usage_and_check_limit(user_id: int) -> bool:
    """
    Увеличить счётчик и вернуть True, если лимит НЕ превышен.
    False, если лимит уже был исчерпан.
    """
    limit = get_user_limit(user_id)
    if limit is None:
        return True  # безлимит

    key = (user_id, today_key())
    used = USER_USAGE.get(key, 0)

    if used >= limit:
        return False

    USER_USAGE[key] = used + 1
    return True


def make_direction_keyboard(current_key: str | None) -> InlineKeyboardMarkup:
    buttons = []

    # Сгруппуем по парам строк
    order = [
        ("ru_de", "de_ru"),
        ("ru_en", "en_ru"),
        ("ru_tr", "tr_ru"),
        ("ru_ro", "ro_ru"),
        ("ru_pl", "pl_ru"),
        ("ru_ar", "ar_ru"),
    ]

    for left, right in order:
        row = []
        for key in (left, right):
            data = DIRECTIONS[key]
            text = data["label"]
            if key == current_key:
                text = "✅ " + text
            row.append(InlineKeyboardButton(text=text, callback_data=f"dir:{key}"))
        buttons.append(row)

    # Отдельная строка для служебных кнопок
    buttons.append(
        [
            InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help"),
            InlineKeyboardButton("📊 Лимиты", callback_data="menu:limits"),
        ]
    )

    return InlineKeyboardMarkup(buttons)


async def tts_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    lang_code: str,
) -> None:
    """
    Озвучка текста через gTTS и отправка как аудио-файл.
    """
    if not text or not text.strip():
        return

    tts_lang = TTS_LANG_MAP.get(lang_code, "en")
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            tts = gTTS(text=text, lang=tts_lang)
            tts.save(tmp_path)

        logger.info("Сгенерирован TTS-файл: %s", tmp_path)

        with open(tmp_path, "rb") as f:
            await update.effective_chat.send_audio(audio=f)
    except Exception as e:
        logger.exception("Ошибка озвучки через gTTS: %s", e)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def translate_text(src: str, dest: str, text: str) -> str:
    translator = GoogleTranslator(source=src, target=dest)
    return translator.translate(text)


# ==========================
#  ХЕНДЛЕРЫ КОМАНД
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id

    # Направление по умолчанию — ru_de
    current_dir = context.user_data.get("direction_key", "ru_de")
    context.user_data["direction_key"] = current_dir

    group = get_user_group(user_id)
    limit = get_user_limit(user_id)

    if group == "UNLIMITED":
        limit_text = "♾️ У вас безлимитный доступ."
    else:
        limit_text = f"📊 Ваша группа: *{group}*, дневной лимит: *{limit}* переводов."

    text = (
        "Привет! Я голосовой переводчик для занятий.\n\n"
        "🎙 Просто отправь мне *голосовое сообщение* — я распознаю речь, переведу и пришлю:\n"
        "• текст перевода\n"
        "• и озвучку перевода голосом.\n\n"
        "👇 Выбери направление перевода (флажки):\n\n"
        f"{limit_text}\n\n"
        "🔐 Если ты студент группы A1/A2/B1 — попроси у преподавателя пароль и введи команду /password."
    )

    await update.message.reply_text(
        text,
        reply_markup=make_direction_keyboard(current_dir),
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /help — отдельная инструкция для студентов
    """
    user = update.effective_user
    user_id = user.id
    group = get_user_group(user_id)
    limit = get_user_limit(user_id)

    if group == "UNLIMITED":
        limit_text = "У вас безлимитный доступ, лимиты не действуют."
    else:
        limit_text = (
            f"Ваша текущая группа: *{group}*.\n"
            f"Дневной лимит переводов: *{limit}* голосовых в сутки."
        )

    text = (
        "🧑‍🎓 *Инструкция для студентов*\n\n"
        "1️⃣ Нажмите /start и выберите направление перевода по флажкам.\n"
        "   Например: 🇷🇺 → 🇩🇪 или 🇩🇪 → 🇷🇺.\n\n"
        "2️⃣ Зажмите кнопку микрофона и запишите *короткую фразу* (1–2 предложения).\n"
        "   Старайтесь говорить чётко, без сильного шума.\n\n"
        "3️⃣ Отправьте голосовое боту.\n"
        "   Я отвечу:\n"
        "   • текстом перевода\n"
        "   • озвучкой перевода (аудио-файл).\n\n"
        "4️⃣ Для учеников уровней A1, A2, B1:\n"
        "   • преподаватель выдаёт вам пароль от вашей группы\n"
        "   • введите команду /password и отправьте пароль\n"
        "   • после этого ваш дневной лимит будет выше.\n\n"
        f"{limit_text}\n\n"
        "💡 Если бот пишет «Не удалось распознать речь» — попробуйте ещё раз, "
        "говорите ближе к микрофону и без посторонних шумов."
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    AWAITING_PASSWORD[user_id] = True
    await update.message.reply_text(
        "🔐 Введите *пароль*, который дал преподаватель.\n\n"
        "Просто отправьте его одним сообщением.",
        parse_mode="Markdown",
    )


async def limits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    group = get_user_group(user_id)
    limit = get_user_limit(user_id)
    today = today_key()
    used = USER_USAGE.get((user_id, today), 0)

    if group == "UNLIMITED":
        txt = (
            "📊 *Ваши лимиты*\n\n"
            "Группа: *UNLIMITED*\n"
            "Сегодня использовано: без ограничений.\n"
        )
    else:
        txt = (
            "📊 *Ваши лимиты*\n\n"
            f"Группа: *{group}*\n"
            f"Дневной лимит: *{limit}*\n"
            f"Сегодня уже использовано: *{used}* переводов."
        )

    await update.message.reply_text(txt, parse_mode="Markdown")


async def pricing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Просто инфо для оплаты (без интеграции).
    """
    text = (
        "💳 *Варианты оплаты* (обсуждаются с преподавателем):\n\n"
        "• Наличные / банковский перевод внутри Германии\n"
        "• SEPA-перевод (EU)\n"
        "• PayPal / Revolut / Wise\n\n"
        "Точный способ и стоимость доступа к боту зависят от курса и "
        "оговариваются с преподавателем индивидуально."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ==========================
#  КОЛБЭК-КНОПКИ
# ==========================

async def direction_or_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    data = query.data or ""

    # Смена направления
    if data.startswith("dir:"):
        new_key = data.split(":", 1)[1]
        current_key = context.user_data.get("direction_key", "ru_de")

        if new_key == current_key:
            # Не меняем текст, чтобы не ловить ошибку "Message is not modified"
            await query.answer("Это направление уже выбрано ✅", show_alert=False)
            return

        if new_key not in DIRECTIONS:
            await query.answer("Неизвестное направление", show_alert=True)
            return

        context.user_data["direction_key"] = new_key
        dir_data = DIRECTIONS[new_key]
        msg = (
            "Направление перевода обновлено:\n\n"
            f"*{dir_data['label']}*\n\n"
            "Теперь отправьте голосовое сообщение."
        )
        await query.edit_message_text(
            msg,
            reply_markup=make_direction_keyboard(new_key),
            parse_mode="Markdown",
        )
        logger.info("Пользователь %s сменил направление на %s", user_id, new_key)
        return

    # Меню: помощь
    if data == "menu:help":
        fake_update = Update(
            update.update_id,
            message=query.message
        )
        # просто отправим отдельным сообщением
        await query.message.reply_text(
            "Открываю справку… Используй /help для подробной инструкции.",
        )
        return

    # Меню: лимиты
    if data == "menu:limits":
        group = get_user_group(user_id)
        limit = get_user_limit(user_id)
        today = today_key()
        used = USER_USAGE.get((user_id, today), 0)

        if group == "UNLIMITED":
            txt = (
                "📊 *Ваши лимиты*\n\n"
                "Группа: *UNLIMITED*\n"
                "Сегодня использовано: без ограничений."
            )
        else:
            txt = (
                "📊 *Ваши лимиты*\n\n"
                f"Группа: *{group}*\n"
                f"Дневной лимит: *{limit}*\n"
                f"Сегодня уже использовано: *{used}*."
            )
        await query.message.reply_text(txt, parse_mode="Markdown")
        return


# ==========================
#  ОБРАБОТКА ПАРОЛЯ (ТЕКСТ)
# ==========================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # Если ждём пароль — проверяем его
    if AWAITING_PASSWORD.get(user_id):
        AWAITING_PASSWORD[user_id] = False
        group = PASSWORDS.get(text)

        if not group:
            await update.message.reply_text(
                "❌ Неверный пароль.\n"
                "Проверьте у преподавателя правильность написания."
            )
            return

        USER_GROUP[user_id] = group
        limit = GROUP_LIMITS[group]
        await update.message.reply_text(
            f"✅ Пароль принят. Ваша группа: *{group}*.\n"
            f"Новый дневной лимит: *{limit}* переводов.",
            parse_mode="Markdown",
        )
        logger.info("Пользователь %s вошёл в группу %s по паролю", user_id, group)
        return

    # Обычный текст — просто подсказка
    await update.message.reply_text(
        "Я работаю с голосовыми сообщениями 🎙\n"
        "Отправьте мне voice, и я переведу его.",
    )


# ==========================
#  ОБРАБОТКА ГОЛОСОВЫХ
# ==========================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id

    # Проверка лимитов
    if not inc_usage_and_check_limit(user_id):
        limit = get_user_limit(user_id)
        await chat.send_message(
            f"⛔ Вы достигли дневного лимита переводов ({limit}).\n"
            "Попробуйте завтра или попросите преподавателя повысить доступ.",
        )
        return

    # Направление перевода для пользователя
    direction_key = context.user_data.get("direction_key", "ru_de")
    if direction_key not in DIRECTIONS:
        direction_key = "ru_de"
        context.user_data["direction_key"] = direction_key

    dir_data = DIRECTIONS[direction_key]
    src_lang = dir_data["src"]
    dest_lang = dir_data["dest"]

    stt_locale = STT_LOCALES.get(src_lang, "ru-RU")

    voice = update.message.voice
    if not voice:
        await chat.send_message("Не вижу голосовое сообщение 🤔 Попробуйте ещё раз.")
        return

    try:
        file = await context.bot.get_file(voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
        logger.info(
            "Скачан voice от user=%s, размер=%d байт", user_id, len(ogg_bytes)
        )

        # Конвертация OGG → WAV через pydub
        audio = AudioSegment.from_file(BytesIO(ogg_bytes), format="ogg")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
            audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        logger.info("Распознаю речь с locale=%s", stt_locale)

        try:
            recognized_text = recognizer.recognize_google(
                audio_data,
                language=stt_locale,
            )
        except sr.UnknownValueError:
            logger.warning("Не удалось распознать речь")
            await chat.send_message(
                "🤔 Не удалось распознать речь.\n"
                "Попробуйте ещё раз и говорите чуть медленнее и чётче."
            )
            return

        logger.info("Распознано: '%s'", recognized_text)

        # Перевод
        logger.info(
            "Перевожу текст. src=%s dest=%s text='%s'",
            src_lang,
            dest_lang,
            recognized_text,
        )
        translated = translate_text(src_lang, dest_lang, recognized_text)

        await chat.send_message(
            f"📝 Оригинал ({src_lang}): {recognized_text}\n\n"
            f"🔊 Перевод ({dest_lang}): {translated}"
        )

        # Озвучка перевода
        await tts_and_send(update, context, translated, dest_lang)

    except Exception as e:
        logger.exception("Ошибка при обработке голосового: %s", e)
        await chat.send_message(
            "Произошла ошибка при обработке голосового. Попробуйте ещё раз чуть позже."
        )


# ==========================
#  MAIN + WEBHOOK
# ==========================

def main() -> None:
    logger.info("Запускаю webhook на %s, порт %s", WEBHOOK_URL, PORT)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("password", password_cmd))
    application.add_handler(CommandHandler("limits", limits_cmd))
    application.add_handler(CommandHandler("pricing", pricing_cmd))

    # Кнопки
    application.add_handler(
        CallbackQueryHandler(direction_or_menu_callback)
    )

    # Текст (для паролей)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # Голосовые
    application.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # Запуск webhook (синхронный, без asyncio.run, чтобы не ломать event loop)
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
