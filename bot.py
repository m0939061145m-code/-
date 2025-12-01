import os
import tempfile
from pydub import AudioSegment
import speech_recognition as sr
from deep_translator import GoogleTranslator
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing!")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🇩🇪 → 🇷🇺", callback_data="de-ru"),
            InlineKeyboardButton("🇷🇺 → 🇩🇪", callback_data="ru-de"),
        ]
    ]

    await update.message.reply_text(
        "Выбери направление перевода:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def set_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    direction = query.data
    context.user_data["direction"] = direction

    src_lang, dest_lang = direction.split("-")
    await query.edit_message_text(f"Направление установлено: {src_lang.upper()} → {dest_lang.upper()}")
    await query.message.reply_text("Теперь отправь голосовое или текст.")


async def recognize_speech_from_ogg(file_path: str, lang: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_temporary_file:
        wav_path = wav_temporary_file.name

    audio = AudioSegment.from_file(file_path, format="ogg")
    audio.export(wav_path, format="wav")

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        try:
            recognized = recognizer.recognize_google(audio_data, language=lang)
            return recognized
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            return f"Ошибка Google Speech API: {e}"


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "direction" not in context.user_data:
        await update.message.reply_text("Выбери направление через /start")
        return

    direction = context.user_data["direction"]
    src_lang_code, dest_lang_code = direction.split("-")

    speech_lang = "de-DE" if src_lang_code == "de" else "ru-RU"

    voice_file = await update.message.voice.get_file()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_ogg_file:
        temp_ogg_path = temp_ogg_file.name
        await voice_file.download_to_drive(temp_ogg_path)

    recognized = await recognize_speech_from_ogg(temp_ogg_path, speech_lang)

    if not recognized:
        await update.message.reply_text("Не удалось распознать речь.")
        return

    translation = GoogleTranslator(source=src_lang_code, target=dest_lang_code).translate(recognized)

    await update.message.reply_text(
        f"🔹 Распознано:\n{recognized}\n\n"
        f"🔹 Перевод:\n{translation}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "direction" not in context.user_data:
        await update.message.reply_text("Выбери направление через /start")
        return

    direction = context.user_data["direction"]
    src_lang_code, dest_lang_code = direction.split("-")

    user_text = update.message.text
    translation = GoogleTranslator(source=src_lang_code, target=dest_lang_code).translate(user_text)

    await update.message.reply_text(
        f"🔹 Исходный текст:\n{user_text}\n\n"
        f"🔹 Перевод:\n{translation}"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(set_direction))
    app.add_handler(MessageHandler(filters.Voice(), handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Бот запущен через polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
