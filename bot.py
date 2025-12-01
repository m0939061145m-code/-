import os
import logging
import tempfile
from pathlib import Path
from typing import Tuple, Optional

import speech_recognition as sr
from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------ ЛОГИ ------------------ #

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------ ENV ------------------ #

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", BASE_URL).rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    logger.error("Переменная окружения BOT_TOKEN не задана!")
    raise SystemExit("BOT_TOKEN is required")

# ------------------ УТИЛИТЫ ------------------ #

recognizer = sr.Recognizer()


def get_lang_pair(direction: str) -> Tuple[str, str]:
    """
    direction: 'de_ru' или 'ru_de'
    Возвращает (src, dest)
    """
    if direction == "de_ru":
        return "de", "ru"
    return "ru", "de"


def make_direction_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("🇩🇪 DE → 🇷🇺 RU", callback_data="de_ru"),
            InlineKeyboardButton("🇷🇺 RU → 🇩🇪 DE", callback_data="ru_de"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def translate_text(text: str, src: str, dest: str) -> str:
    logger.info("Перевожу текст. src=%s dest=%s text=%r", src, dest, text)
    try:
        return GoogleTranslator(source=src, target=dest).translate(text)
    except Exception as e:
        logger.error("Ошибка перевода: %s", e)
        return text


def recognize_speech_from_file(path: Path, language: str) -> Optional[str]:
    logger.info("Распознаю речь с locale=%s", language)
    with sr.AudioFile(str(path)) as source:
        audio = recognizer.record(source)
    try:
        text = recognizer.recognize_google(audio, language=language)
        logger.info("Распознано: %r", text)
        return text
    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь")
        return None
    except Exception as e:
        logger.error("Ошибка распознавания речи: %s", e)
        return None


def convert_ogg_to_wav(ogg_path: Path) -> Path:
    wav_fd, wav_name = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)
    wav_path = Path(wav_name)
    audio = AudioSegment.from_file(ogg_path)
    audio.export(wav_path, format="wav")
    return wav_path


def synthesize_speech(text: str, lang: str) -> Path:
    """
    Синтез речи из текста (gTTS).
    lang: 'ru' или 'de'
    Возвращает путь к mp3-файлу.
    """
    tts = gTTS(text=text, lang=lang)
    fd, tmp_name = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    tmp_path = Path(tmp_name)
    tts.save(tmp_path.as_posix())
    return tmp_path


# ------------------ HANDLERS ------------------ #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    context.user_data.setdefault("direction", "de_ru")  # по умолчанию DE → RU
    logger.info("Пользователь %s запустил /start", user.id)

    text = (
        "Привет! Я голосовой переводчик.\n\n"
        "Выбери направление перевода:\n"
        "🇩🇪 DE → 🇷🇺 RU или 🇷🇺 RU → 🇩🇪 DE.\n\n"
        "Можешь отправлять мне текст или голосовые."
    )

    await update.message.reply_text(
        text,
        reply_markup=make_direction_keyboard(),
    )


async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    new_direction = query.data
    old_direction = context.user_data.get("direction")

    if new_direction == old_direction:
        # Чтобы не ловить ошибку "Message is not modified"
        logger.info(
            "Пользователь %s нажал то же самое направление %s",
            user_id,
            new_direction,
        )
        return

    context.user_data["direction"] = new_direction
    logger.info("Пользователь %s сменил направление на %s", user_id, new_direction)

    msg = (
        "Направление перевода обновлено.\n"
        "Теперь работаем: 🇩🇪 DE → 🇷🇺 RU" if new_direction == "de_ru"
        else "Направление перевода обновлено.\nТеперь работаем: 🇷🇺 RU → 🇩🇪 DE"
    )

    try:
        await query.edit_message_text(
            msg,
            reply_markup=make_direction_keyboard(),
        )
    except Exception as e:
        # Если вдруг опять будет BadRequest — просто логируем и идём дальше
        logger.error("Ошибка при обновлении сообщения с кнопками: %s", e)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_text = update.message.text
    direction = context.user_data.get("direction", "de_ru")
    src, dest = get_lang_pair(direction)

    translated = translate_text(user_text, src, dest)

    # Текстовый ответ
    await update.message.reply_text(
        f"🧠 *Оригинал:*\n{user_text}\n\n🌍 *Перевод:*\n{translated}",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Голосовой ответ
    try:
        tts_lang = dest  # 'ru' или 'de'
        voice_path = synthesize_speech(translated, tts_lang)
        with voice_path.open("rb") as f:
            await update.message.reply_voice(f)
        voice_path.unlink(missing_ok=True)
    except Exception as e:
        logger.error("Ошибка при озвучке текста (text handler): %s", e)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return

    voice = update.message.voice
    direction = context.user_data.get("direction", "de_ru")
    src, dest = get_lang_pair(direction)

    # язык распознавания
    if src == "ru":
        locale = "ru-RU"
    else:
        locale = "de-DE"

    # Скачиваем voice во временный файл
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".oga")
    os.close(tmp_fd)
    ogg_path = Path(tmp_name)

    try:
        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(ogg_path.as_posix())

        wav_path = convert_ogg_to_wav(ogg_path)
        recognized = recognize_speech_from_file(wav_path, locale)

        # чистим wav
        wav_path.unlink(missing_ok=True)

        if not recognized:
            await update.message.reply_text(
                "😢 Не удалось распознать речь. "
                "Попробуй ещё раз чуть громче и чётче."
            )
            return

        translated = translate_text(recognized, src, dest)

        # Текстовый ответ
        await update.message.reply_text(
            f"🎙 *Оригинал:*\n{recognized}\n\n🌍 *Перевод:*\n{translated}",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Голосовой ответ
        try:
            tts_lang = dest  # 'ru' или 'de'
            voice_path = synthesize_speech(translated, tts_lang)
            with voice_path.open("rb") as f:
                await update.message.reply_voice(f)
            voice_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error("Ошибка при озвучке текста (voice handler): %s", e)

    finally:
        ogg_path.unlink(missing_ok=True)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Не знаю такую команду. Напиши текст или отправь голосовое 🙂"
    )


# ------------------ MAIN ------------------ #

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(direction_button))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    if BASE_WEBHOOK_URL:
        webhook_url = f"{BASE_WEBHOOK_URL}/webhook"
        logger.info(
            "Запускаю webhook на %s, порт %s",
            webhook_url,
            PORT,
        )
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=webhook_url,
        )
    else:
        logger.warning(
            "BASE_WEBHOOK_URL не задан, запускаю long polling "
            "(на Render это обычно не подходит)."
        )
        application.run_polling()


if __name__ == "__main__":
    main()
