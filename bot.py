import asyncio
import io
import logging
import os
from typing import Literal

from deep_translator import GoogleTranslator
from pydub import AudioSegment
import speech_recognition as sr
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------
# ЛОГИ
# -------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# НАСТРОЙКИ / ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# -------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Токен бота из BotFather
BASE_URL = os.getenv("BASE_URL")    # https://bratik.onrender.com (или твой URL на Render)
PORT = int(os.getenv("PORT", "8443"))

# варианты направления перевода
Direction = Literal["de_ru", "ru_de"]


# -------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------------------------
def get_user_direction(context: ContextTypes.DEFAULT_TYPE) -> Direction:
    """Получаем направление перевода для пользователя из user_data."""
    direction: Direction = context.user_data.get("direction", "de_ru")
    return direction


def set_user_direction(context: ContextTypes.DEFAULT_TYPE, direction: Direction) -> None:
    context.user_data["direction"] = direction


def get_langs_for_direction(direction: Direction) -> tuple[str, str]:
    """
    Возвращает (source_lang, target_lang) для Google Speech + GoogleTranslator.
    """
    if direction == "de_ru":
        return "de-DE", "ru"
    else:
        return "ru-RU", "de"


async def translate_text(text: str, direction: Direction) -> str:
    """Перевод текста через deep-translator в отдельном потоке, чтобы не блочить asyncio."""
    _, target = get_langs_for_direction(direction)

    def _do_translate() -> str:
        translator = GoogleTranslator(source="auto", target=target)
        return translator.translate(text)

    return await asyncio.to_thread(_do_translate)


async def recognize_voice(voice_bytes: bytes, direction: Direction) -> str:
    """
    Распознаём речь из голосового сообщения.
    1. OGG (Telegram) -> WAV (через pydub/ffmpeg)
    2. SpeechRecognition -> текст
    """
    source_lang, _ = get_langs_for_direction(direction)

    def _do_recognize() -> str:
        audio = AudioSegment.from_file(io.BytesIO(voice_bytes), format="ogg")
        wav_io = io.BytesIO()
        audio.export(wav_io, format="wav")
        wav_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            audio_data = recognizer.record(source)

        # google speech
        text = recognizer.recognize_google(audio_data, language=source_lang)
        return text

    return await asyncio.to_thread(_do_recognize)


# -------------------------------------------------
# ХЭНДЛЕРЫ
# -------------------------------------------------
START_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🇩🇪 DE → 🇷🇺 RU"],
        ["🇷🇺 RU → 🇩🇪 DE"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_user_direction(context, "de_ru")
    await update.message.reply_text(
        "Привет! Я голосовой переводчик.\n\n"
        "Выбери направление перевода:\n"
        "🇩🇪 DE → 🇷🇺 RU или 🇷🇺 RU → 🇩🇪 DE.\n"
        "Можешь отправлять мне текст или голосовые.",
        reply_markup=START_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я перевожу между немецким и русским.\n\n"
        "Команды:\n"
        "/start — выбрать направление\n"
        "/help — помощь\n\n"
        "Можно присылать текст или голосовые сообщения.",
        reply_markup=START_KEYBOARD,
    )


async def handle_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор направления через кнопки."""
    text = (update.message.text or "").strip()

    if "DE → RU" in text:
        set_user_direction(context, "de_ru")
        await update.message.reply_text("Направление: 🇩🇪 немецкий → 🇷🇺 русский")
    elif "RU → DE" in text:
        set_user_direction(context, "ru_de")
        await update.message.reply_text("Направление: 🇷🇺 русский → 🇩🇪 немецкий")
    else:
        # если вдруг сюда попал другой текст — просто ничего не делаем
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    direction = get_user_direction(context)
    original = update.message.text

    try:
        translated = await translate_text(original, direction)
    except Exception as e:
        logger.exception("Ошибка перевода текста: %s", e)
        await update.message.reply_text("😔 Не удалось перевести текст. Попробуй ещё раз.")
        return

    await update.message.reply_text(
        f"🗣 Оригинал:\n{original}\n\n"
        f"🌐 Перевод:\n{translated}",
        reply_markup=START_KEYBOARD,
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return

    direction = get_user_direction(context)
    voice = update.message.voice

    try:
        file = await context.bot.get_file(voice.file_id)
        voice_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.exception("Не смог скачать голосовое: %s", e)
        await update.message.reply_text("😔 Не получилось скачать голосовое сообщение.")
        return

    try:
        recognized_text = await recognize_voice(voice_bytes, direction)
    except sr.UnknownValueError:
        await update.message.reply_text("😔 Не понял, что было сказано. Скажи, пожалуйста, ещё раз.")
        return
    except Exception as e:
        logger.exception("Ошибка распознавания речи: %s", e)
        await update.message.reply_text("😔 Не удалось распознать речь.")
        return

    try:
        translated = await translate_text(recognized_text, direction)
    except Exception as e:
        logger.exception("Ошибка перевода речи: %s", e)
        await update.message.reply_text(
            f"🗣 Распознал: {recognized_text}\n"
            "Но не получилось перевести. Попробуй ещё раз."
        )
        return

    await update.message.reply_text(
        f"🗣 Распознал:\n{recognized_text}\n\n"
        f"🌐 Перевод:\n{translated}",
        reply_markup=START_KEYBOARD,
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я понимаю команды /start и /help, а также текст и голосовые сообщения."
    )


# -------------------------------------------------
# ЗАПУСК ПРИЛОЖЕНИЯ
# -------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

    application = Application.builder().token(BOT_TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # выбор направления (через кнопки)
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("DE → RU|RU → DE"),
            handle_direction,
        )
    )

    # голосовые
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # обычный текст
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.Regex("DE → RU|RU → DE")),
            handle_text,
        )
    )

    # всё остальное
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    # -----------------------------
    # Локально: polling
    # На Render: webhook
    # -----------------------------
    if BASE_URL:
        webhook_path = "/webhook"
        webhook_url = BASE_URL.rstrip("/") + webhook_path

        logger.info("Запускаю webhook на %s, порт %d", webhook_url, PORT)

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path.lstrip("/"),
            webhook_url=webhook_url,
        )
    else:
        logger.info("BASE_URL не задан. Запускаю обычный polling.")
        application.run_polling()


if __name__ == "__main__":
    main()
