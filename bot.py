import logging
import os
import tempfile
from typing import Tuple

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

import speech_recognition as sr
from pydub import AudioSegment
from deep_translator import GoogleTranslator

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")  # например: https://bratik.onrender.com
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/webhook"

# Направления перевода
DIR_RU_DE = "ru_de"
DIR_DE_RU = "de_ru"

# По-умолчанию будем считать, что переводим RU -> DE
DEFAULT_DIRECTION = DIR_RU_DE

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

def get_lang_pair(direction: str) -> Tuple[str, str]:
    """
    Возвращает (src_lang, dest_lang) для deep-translator и SpeechRecognition.
    """
    if direction == DIR_DE_RU:
        return "de", "ru"
    # по умолчанию
    return "ru", "de"


def get_recognition_locale(lang_code: str) -> str:
    """
    Преобразуем 'ru' / 'de' в локали для Google Speech Recognition.
    """
    if lang_code == "de":
        return "de-DE"
    return "ru-RU"


def detect_direction_from_text(text: str) -> str | None:
    """
    Смотрим, нажата ли кнопка с направлением перевода.
    """
    t = text.strip()
    if "DE → RU" in t or "DE -> RU" in t:
        return DIR_DE_RU
    if "RU → DE" in t or "RU -> DE" in t:
        return DIR_RU_DE
    return None


async def translate_text(text: str, src: str, dest: str) -> str:
    logger.info("Перевожу текст. src=%s dest=%s text=%r", src, dest, text)
    translator = GoogleTranslator(source=src, target=dest)
    return translator.translate(text)


async def recognize_voice(ogg_bytes: bytes, src_lang: str) -> str | None:
    """
    Преобразуем OGG в WAV и распознаём через SpeechRecognition.
    """
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg_file:
        ogg_file.write(ogg_bytes)
        ogg_path = ogg_file.name

    wav_path = ogg_path.replace(".ogg", ".wav")

    try:
        # Конвертация через pydub
        audio = AudioSegment.from_file(ogg_path)
        audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        locale = get_recognition_locale(src_lang)
        logger.info("Распознаю речь с locale=%s", locale)
        text = recognizer.recognize_google(audio_data, language=locale)
        logger.info("Распознано: %r", text)
        return text

    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь")
        return None
    except Exception as e:
        logger.exception("Ошибка при распознавании: %s", e)
        return None
    finally:
        try:
            os.remove(ogg_path)
        except OSError:
            pass
        try:
            os.remove(wav_path)
        except OSError:
            pass


def make_direction_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🇩🇪 DE → 🇷🇺 RU", callback_data=DIR_DE_RU),
        ],
        [
            InlineKeyboardButton("🇷🇺 RU → 🇩🇪 DE", callback_data=DIR_RU_DE),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ================== ХЕНДЛЕРЫ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    context.user_data["direction"] = DEFAULT_DIRECTION

    text = (
        "Привет! Я голосовой переводчик.\n\n"
        "Выбери направление перевода:\n"
        "🇩🇪 DE → 🇷🇺 RU или 🇷🇺 RU → 🇩🇪 DE.\n\n"
        "После выбора можешь отправлять мне текст или голосовые сообщения."
    )

    await update.message.reply_text(text, reply_markup=make_direction_keyboard())
    logger.info("Пользователь %s запустил /start", user.id if user else "unknown")


async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    direction = query.data
    context.user_data["direction"] = direction

    if direction == DIR_DE_RU:
        msg = "Теперь перевожу: 🇩🇪 DE → 🇷🇺 RU"
    else:
        msg = "Теперь перевожу: 🇷🇺 RU → 🇩🇪 DE"

    await query.edit_message_text(msg, reply_markup=make_direction_keyboard())
    logger.info("Пользователь %s сменил направление на %s", query.from_user.id, direction)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text

    # Может пользователь нажал кнопку, а не callback (через mobile-клаву)
    detected = detect_direction_from_text(text)
    if detected:
        context.user_data["direction"] = detected
        if detected == DIR_DE_RU:
            msg = "Теперь перевожу: 🇩🇪 DE → 🇷🇺 RU"
        else:
            msg = "Теперь перевожу: 🇷🇺 RU → 🇩🇪 DE"
        await update.message.reply_text(msg)
        return

    direction = context.user_data.get("direction", DEFAULT_DIRECTION)
    src, dest = get_lang_pair(direction)

    try:
        translation = await translate_text(text, src, dest)
        await update.message.reply_text(
            f"🗣 Оригинал:\n{text}\n\n"
            f"🌐 Перевод ({src.upper()} → {dest.upper()}):\n{translation}"
        )
    except Exception as e:
        logger.exception("Ошибка перевода текста: %s", e)
        await update.message.reply_text("⚠️ Не удалось перевести текст, попробуй ещё раз.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice
    if not voice:
        return

    direction = context.user_data.get("direction", DEFAULT_DIRECTION)
    src, dest = get_lang_pair(direction)

    # Скачиваем файл с сервера Telegram
    try:
        file = await context.bot.get_file(voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.exception("Не удалось скачать голосовое: %s", e)
        await update.message.reply_text("⚠️ Не удалось скачать голосовое сообщение.")
        return

    # Распознаём речь
    text = await recognize_voice(ogg_bytes, src_lang=src)
    if not text:
        await update.message.reply_text("⚠️ Не удалось распознать речь. Попробуй ещё раз.")
        return

    # Переводим распознанный текст
    try:
        translation = await translate_text(text, src, dest)
    except Exception as e:
        logger.exception("Ошибка перевода голоса: %s", e)
        await update.message.reply_text(
            f"🗣 Оригинал:\n{text}\n\n⚠️ Не удалось перевести, попробуй ещё раз."
        )
        return

    await update.message.reply_text(
        f"🗣 Оригинал:\n{text}\n\n"
        f"🌐 Перевод ({src.upper()} → {dest.upper()}):\n{translation}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Произошла ошибка при обработке апдейта: %s", context.error)


# ================== ЗАПУСК БОТА ==================

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(direction_button))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(error_handler)

    if BASE_WEBHOOK_URL:
        webhook_url = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        logger.info(
            "Запускаю webhook на %s, порт %d",
            webhook_url,
            PORT,
        )
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=webhook_url,
        )
    else:
        logger.warning(
            "BASE_WEBHOOK_URL не задан, запускаю long polling (на Render так работать не будет)."
        )
        application.run_polling()


if __name__ == "__main__":
    main()
