import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, Any

import speech_recognition as sr
from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputFile,
)
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------------------------
# БАЗОВЫЕ НАСТРОЙКИ
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "ТОТ_САМЫЙ_ТОКЕН")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://bratik.onrender.com/webhook")
PORT = int(os.getenv("PORT", "10000"))

# Твой друг в Германии — безлимит
FRIEND_ID = 1300323894

# Пароли для уровней
LEVEL_PASSWORDS = {
    "A1": "karbofosA1",
    "A2": "karbofosA2",
    "B1": "karbofosB1",
}

# Лимиты по уровням (в голосовых переводах в день)
LEVEL_LIMITS = {
    "A1": 30,
    "A2": 60,
    "B1": 120,
}

# Отдельный "премиум" уровень для друга (без лимитов)
PREMIUM_USERS = {FRIEND_ID}

# Память по использованиям: {user_id: {"date": "YYYY-MM-DD", "count": N, "level": "A1"/...}}
user_usage: Dict[int, Dict[str, Any]] = {}

# Локали для распознавания речи по "исходному" языку
STT_LOCALES = {
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "ar": "ar-SY",
    "pl": "pl-PL",
    "auto": "ru-RU",  # если авто, пробуем ru-RU как наиболее частый
}

# -------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# НАСТРОЙКА ЯЗЫКА ИНТЕРФЕЙСА
# -------------------------------------------------------------------

UI_LANGS = ["ru", "de", "en"]  # доступные языки интерфейса


def get_ui_lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("ui_lang", "ru")


def set_ui_lang(context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    if lang in UI_LANGS:
        context.user_data["ui_lang"] = lang


def t(ui: str, key: str, **kwargs) -> str:
    """
    Очень простой "словичок" для основных текстов интерфейса.
    ui: "ru" / "de" / "en"
    key: ключ строки.
    """
    texts = {
        "ru": {
            "start": (
                "👋 Привет! Я бот-переводчик для языковых курсов.\n\n"
                "🔊 Отправь мне голосовое сообщение — я распознаю текст и переведу.\n"
                "🌍 Текущее направление: {direction}\n\n"
                "📌 Команды:\n"
                "/start – главное меню\n"
                "/help – помощь для студентов\n"
                "/limits – ваш текущий лимит\n"
                "/password – ввести пароль уровня (A1/A2/B1)\n"
                "/ui – язык интерфейса"
            ),
            "help_students": (
                "📚 Помощь для студентов\n\n"
                "1️⃣ Отправляй голосовые сообщения боту. Он:\n"
                " • распознаёт твою речь,\n"
                " • переводит в выбранное направление,\n"
                " • отправляет текстом и голосом.\n\n"
                "2️⃣ Кнопка «Направление перевода» позволяет выбрать пары:\n"
                " • любой язык → немецкий\n"
                " • русский ↔ немецкий\n"
                " • английский ↔ немецкий\n"
                " • турецкий ↔ немецкий\n"
                " • румынский ↔ немецкий\n"
                " • арабский ↔ немецкий\n"
                " • польский ↔ немецкий\n\n"
                "3️⃣ Лимиты:\n"
                " • A1: 30 переводов в день\n"
                " • A2: 60 переводов в день\n"
                " • B1: 120 переводов в день\n\n"
                "4️⃣ Введите пароль уровня через /password, чтобы активировать свой тариф.\n"
                "5️⃣ Если что-то не работает — скажите преподавателю 😊"
            ),
            "limits": "📊 Ваш уровень: {level}\nСегодня вы использовали {count} из {limit} переводов.",
            "limits_unset": "У вас пока не выбран уровень. Введите пароль через /password.",
            "limits_premium": "✨ У вас безлимитный доступ. Можно пользоваться сколько угодно.",
            "password_request": "🔐 Введите пароль уровня (A1/A2/B1) одним сообщением.",
            "password_ok": "✅ Уровень {level} активирован. Дневной лимит: {limit} переводов.",
            "password_bad": "❌ Неверный пароль. Попросите преподавателя выдать правильный.",
            "limit_reached": (
                "⛔ Вы достигли дневного лимита.\n"
                "Обратитесь к преподавателю, если нужен больший объём."
            ),
            "stt_fail": "😔 Не удалось распознать речь. Попробуйте записать ещё раз, чуть чётче.",
            "translate_error": "⚠️ Ошибка при переводе. Попробуйте ещё раз позже.",
            "audio_error": "⚠️ Ошибка при подготовке голосового ответа.",
            "ui_choose": "🌐 Выберите язык интерфейса:",
            "ui_set": "✅ Язык интерфейса переключен на: {lang_name}",
        },
        "de": {
            "start": (
                "👋 Hallo! Ich bin ein Übersetzungs-Bot für Sprachkurse.\n\n"
                "🔊 Sende mir eine Sprachnachricht – ich erkenne den Text und übersetze ihn.\n"
                "🌍 Aktuelle Richtung: {direction}\n\n"
                "📌 Befehle:\n"
                "/start – Hauptmenü\n"
                "/help – Hilfe für Studierende\n"
                "/limits – dein tägliches Limit\n"
                "/password – Kurs-Passwort eingeben (A1/A2/B1)\n"
                "/ui – Sprache der Benutzeroberfläche"
            ),
            "help_students": (
                "📚 Hilfe für Studierende\n\n"
                "1️⃣ Sende Sprachnachrichten an den Bot. Er:\n"
                " • erkennt deine Sprache,\n"
                " • übersetzt in die gewählte Richtung,\n"
                " • sendet Text + Sprachantwort.\n\n"
                "2️⃣ Über die Schaltfläche „Übersetzungsrichtung“ kannst du wählen:\n"
                " • beliebige Sprache → Deutsch\n"
                " • Russisch ↔ Deutsch\n"
                " • Englisch ↔ Deutsch\n"
                " • Türkisch ↔ Deutsch\n"
                " • Rumänisch ↔ Deutsch\n"
                " • Arabisch ↔ Deutsch\n"
                " • Polnisch ↔ Deutsch\n\n"
                "3️⃣ Limits:\n"
                " • A1: 30 Übersetzungen/Tag\n"
                " • A2: 60 Übersetzungen/Tag\n"
                " • B1: 120 Übersetzungen/Tag\n\n"
                "4️⃣ Aktiviere deinen Kurs mit /password.\n"
                "5️⃣ Wenn etwas nicht funktioniert – sprich mit deinem Lehrer 😊"
            ),
            "limits": "📊 Dein Level: {level}\nHeute verwendet: {count} von {limit} Übersetzungen.",
            "limits_unset": "Dein Level ist noch nicht gesetzt. Bitte Passwort mit /password eingeben.",
            "limits_premium": "✨ Du hast unbegrenzten Zugang.",
            "password_request": "🔐 Bitte Kurs-Passwort (A1/A2/B1) in einer Nachricht senden.",
            "password_ok": "✅ Level {level} aktiviert. Tageslimit: {limit} Übersetzungen.",
            "password_bad": "❌ Falsches Passwort. Bitte Lehrer nach dem richtigen fragen.",
            "limit_reached": (
                "⛔ Dein Tageslimit ist erreicht.\n"
                "Wende dich an deinen Lehrer, wenn du mehr brauchst."
            ),
            "stt_fail": "😔 Sprache konnte nicht erkannt werden. Bitte noch einmal deutlicher sprechen.",
            "translate_error": "⚠️ Fehler bei der Übersetzung. Bitte später erneut versuchen.",
            "audio_error": "⚠️ Fehler bei der Sprachausgabe.",
            "ui_choose": "🌐 Wähle die Sprache der Benutzeroberfläche:",
            "ui_set": "✅ Sprache der Benutzeroberfläche geändert zu: {lang_name}",
        },
        "en": {
            "start": (
                "👋 Hi! I’m a translation bot for language courses.\n\n"
                "🔊 Send me a voice message – I’ll recognize it and translate.\n"
                "🌍 Current direction: {direction}\n\n"
                "📌 Commands:\n"
                "/start – main menu\n"
                "/help – help for students\n"
                "/limits – your daily limit\n"
                "/password – enter level password (A1/A2/B1)\n"
                "/ui – interface language"
            ),
            "help_students": (
                "📚 Help for students\n\n"
                "1️⃣ Send voice messages to the bot. It will:\n"
                " • recognize your speech,\n"
                " • translate to the selected direction,\n"
                " • reply with text and voice.\n\n"
                "2️⃣ Use the “Direction” button to choose:\n"
                " • any language → German\n"
                " • Russian ↔ German\n"
                " • English ↔ German\n"
                " • Turkish ↔ German\n"
                " • Romanian ↔ German\n"
                " • Arabic ↔ German\n"
                " • Polish ↔ German\n\n"
                "3️⃣ Limits:\n"
                " • A1: 30 translations/day\n"
                " • A2: 60 translations/day\n"
                " • B1: 120 translations/day\n\n"
                "4️⃣ Enter your level password via /password to activate.\n"
                "5️⃣ If something doesn’t work – talk to your teacher 😊"
            ),
            "limits": "📊 Your level: {level}\nToday you used {count} of {limit} translations.",
            "limits_unset": "Your level isn’t set yet. Enter a password via /password.",
            "limits_premium": "✨ You have unlimited access.",
            "password_request": "🔐 Send your level password (A1/A2/B1) in a single message.",
            "password_ok": "✅ Level {level} activated. Daily limit: {limit} translations.",
            "password_bad": "❌ Wrong password. Ask your teacher for the correct one.",
            "limit_reached": (
                "⛔ You’ve reached your daily limit.\n"
                "Contact your teacher if you need more."
            ),
            "stt_fail": "😔 Couldn’t recognize speech. Please try again and speak more clearly.",
            "translate_error": "⚠️ Translation error. Please try again later.",
            "audio_error": "⚠️ Error generating voice reply.",
            "ui_choose": "🌐 Choose interface language:",
            "ui_set": "✅ Interface language set to: {lang_name}",
        },
    }

    lang_dict = texts.get(ui, texts["ru"])
    template = lang_dict.get(key, texts["ru"].get(key, ""))
    return template.format(**kwargs)


def ui_lang_name(lang: str) -> str:
    return {
        "ru": "Русский",
        "de": "Deutsch",
        "en": "English",
    }.get(lang, lang)


# -------------------------------------------------------------------
# НАПРАВЛЕНИЯ ПЕРЕВОДА
# -------------------------------------------------------------------

DIRECTIONS = {
    "auto_de": {
        "src": "auto",
        "dest": "de",
        "labels": {
            "ru": "🌐 → 🇩🇪 De (любой язык)",
            "de": "🌐 → 🇩🇪 De (beliebige Sprache)",
            "en": "🌐 → 🇩🇪 De (any language)",
        },
    },
    "ru_de": {
        "src": "ru",
        "dest": "de",
        "labels": {
            "ru": "🇷🇺 Ru-De",
            "de": "🇷🇺 Ru-De",
            "en": "🇷🇺 Ru-De",
        },
    },
    "de_ru": {
        "src": "de",
        "dest": "ru",
        "labels": {
            "ru": "🇩🇪 De-Русский",
            "de": "🇩🇪 De-Russisch",
            "en": "🇩🇪 De-Russian",
        },
    },
    "en_de": {
        "src": "en",
        "dest": "de",
        "labels": {
            "ru": "🇬🇧 En-De",
            "de": "🇬🇧 En-De",
            "en": "🇬🇧 En-De",
        },
    },
    "de_en": {
        "src": "de",
        "dest": "en",
        "labels": {
            "ru": "🇩🇪 De-English",
            "de": "🇩🇪 De-Englisch",
            "en": "🇩🇪 De-English",
        },
    },
    "tr_de": {
        "src": "tr",
        "dest": "de",
        "labels": {
            "ru": "🇹🇷 Tr-De",
            "de": "🇹🇷 Tr-De",
            "en": "🇹🇷 Tr-De",
        },
    },
    "de_tr": {
        "src": "de",
        "dest": "tr",
        "labels": {
            "ru": "🇩🇪 De-Türkçe",
            "de": "🇩🇪 De-Türkçe",
            "en": "🇩🇪 De-Turkish",
        },
    },
    "ro_de": {
        "src": "ro",
        "dest": "de",
        "labels": {
            "ru": "🇷🇴 Ro-De",
            "de": "🇷🇴 Ro-De",
            "en": "🇷🇴 Ro-De",
        },
    },
    "de_ro": {
        "src": "de",
        "dest": "ro",
        "labels": {
            "ru": "🇩🇪 De-Română",
            "de": "🇩🇪 De-Rumänisch",
            "en": "🇩🇪 De-Romanian",
        },
    },
    "ar_de": {
        "src": "ar",
        "dest": "de",
        "labels": {
            "ru": "🇸🇾 Ar-De",
            "de": "🇸🇾 Ar-De",
            "en": "🇸🇾 Ar-De",
        },
    },
    "de_ar": {
        "src": "de",
        "dest": "ar",
        "labels": {
            "ru": "🇩🇪 De-Arabic",
            "de": "🇩🇪 De-Arabisch",
            "en": "🇩🇪 De-Arabic",
        },
    },
    "pl_de": {
        "src": "pl",
        "dest": "de",
        "labels": {
            "ru": "🇵🇱 Pl-De",
            "de": "🇵🇱 Pl-De",
            "en": "🇵🇱 Pl-De",
        },
    },
    "de_pl": {
        "src": "de",
        "dest": "pl",
        "labels": {
            "ru": "🇩🇪 De-Polski",
            "de": "🇩🇪 De-Polnisch",
            "en": "🇩🇪 De-Polish",
        },
    },
}


def get_dir_label(key: str, ui_lang: str) -> str:
    data = DIRECTIONS[key]
    return data["labels"].get(ui_lang, data["labels"]["en"])


def get_current_direction(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("direction", "auto_de")


def set_current_direction(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    if key in DIRECTIONS:
        context.user_data["direction"] = key


def make_direction_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    ui = get_ui_lang(context)
    current = get_current_direction(context)

    order = [
        ["auto_de"],
        ["ru_de", "de_ru"],
        ["en_de", "de_en"],
        ["tr_de", "de_tr"],
        ["ro_de", "de_ro"],
        ["ar_de", "de_ar"],
        ["pl_de", "de_pl"],
    ]

    rows: list[list[InlineKeyboardButton]] = []

    for row_keys in order:
        row: list[InlineKeyboardButton] = []
        for key in row_keys:
            label = get_dir_label(key, ui)
            if key == current:
                label = "✅ " + label
            row.append(InlineKeyboardButton(label, callback_data=f"dir:{key}"))
        rows.append(row)

    # Нижний ряд – меню
    if ui == "ru":
        rows.append(
            [
                InlineKeyboardButton("📚 Помощь", callback_data="menu:help"),
                InlineKeyboardButton("🌐 UI язык", callback_data="menu:ui"),
            ]
        )
    elif ui == "de":
        rows.append(
            [
                InlineKeyboardButton("📚 Hilfe", callback_data="menu:help"),
                InlineKeyboardButton("🌐 UI-Sprache", callback_data="menu:ui"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton("📚 Help", callback_data="menu:help"),
                InlineKeyboardButton("🌐 UI lang", callback_data="menu:ui"),
            ]
        )

    return InlineKeyboardMarkup(rows)


def make_ui_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇷🇺 Русский", callback_data="ui:ru"),
                InlineKeyboardButton("🇩🇪 Deutsch", callback_data="ui:de"),
                InlineKeyboardButton("🇬🇧 English", callback_data="ui:en"),
            ]
        ]
    )


# -------------------------------------------------------------------
# ПОДСЧЁТ ЛИМИТОВ
# -------------------------------------------------------------------


def get_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_user_level(user_id: int) -> str | None:
    info = user_usage.get(user_id)
    if info:
        return info.get("level")
    return None


def set_user_level(user_id: int, level: str) -> None:
    today = get_today_str()
    if user_id not in user_usage:
        user_usage[user_id] = {"date": today, "count": 0, "level": level}
    else:
        user_usage[user_id]["level"] = level
        # дату и счётчик не трогаем, чтобы не обнулять лимит сразу


def check_and_increment_usage(user_id: int) -> bool:
    """
    Возвращает True, если можно использовать ещё один перевод (и увеличивает счётчик).
    False — если лимит исчерпан.
    """
    if user_id in PREMIUM_USERS:
        return True

    today = get_today_str()
    info = user_usage.get(user_id)

    if not info:
        # уровень ещё не выбран — запрещаем, пока не введёт пароль
        return False

    level = info.get("level")
    if level not in LEVEL_LIMITS:
        return False

    limit = LEVEL_LIMITS[level]

    if info["date"] != today:
        info["date"] = today
        info["count"] = 0

    if info["count"] >= limit:
        return False

    info["count"] += 1
    return True


def get_usage_status_text(user_id: int, ui_lang: str) -> str:
    if user_id in PREMIUM_USERS:
        return t(ui_lang, "limits_premium")

    today = get_today_str()
    info = user_usage.get(user_id)

    if not info or "level" not in info:
        return t(ui_lang, "limits_unset")

    level = info["level"]
    if info["date"] != today:
        count = 0
    else:
        count = info.get("count", 0)

    limit = LEVEL_LIMITS.get(level, 0)
    return t(ui_lang, "limits", level=level, count=count, limit=limit)


# -------------------------------------------------------------------
# ПЕРЕВОД + ОЗВУЧКА
# -------------------------------------------------------------------


def translate_text(src_lang: str, dest_lang: str, text: str) -> str:
    """Перевод текста через deep-translator (GoogleTranslator)."""
    if src_lang == "auto":
        translator = GoogleTranslator(source="auto", target=dest_lang)
    else:
        translator = GoogleTranslator(source=src_lang, target=dest_lang)
    return translator.translate(text)


def tts_to_ogg(lang: str, text: str) -> BytesIO:
    """
    Генерация голосового ответа:
    gTTS -> mp3 -> ogg (opus) через pydub для отправки как voice.
    """
    mp3_buf = BytesIO()
    tts = gTTS(text=text, lang=lang)
    tts.write_to_fp(mp3_buf)
    mp3_buf.seek(0)

    # конвертируем в ogg/opus
    audio = AudioSegment.from_file(mp3_buf, format="mp3")
    ogg_buf = BytesIO()
    audio.export(ogg_buf, format="ogg", codec="libopus")
    ogg_buf.seek(0)
    return ogg_buf


def recognize_speech_from_ogg(ogg_bytes: bytes, src_lang: str) -> str | None:
    """
    Распознаём речь из OGG (voice) через SpeechRecognition.
    """
    locale = STT_LOCALES.get(src_lang, "ru-RU")

    tmp_ogg = "temp.ogg"
    tmp_wav = "temp.wav"

    with open(tmp_ogg, "wb") as f:
        f.write(ogg_bytes)

    # конвертация через ffmpeg (pydub)
    audio = AudioSegment.from_file(tmp_ogg, format="ogg")
    audio.export(tmp_wav, format="wav")

    recognizer = sr.Recognizer()
    with sr.AudioFile(tmp_wav) as source:
        audio_data = recognizer.record(source)

    try:
        logger.info("Распознаю речь с locale=%s", locale)
        text = recognizer.recognize_google(audio_data, language=locale)
        logger.info("Распознано: %r", text)
        return text
    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь")
        return None
    except sr.RequestError as e:
        logger.error("Ошибка запроса к Google STT: %s", e)
        return None


# -------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# -------------------------------------------------------------------


async def start(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    if "direction" not in context.user_data:
        context.user_data["direction"] = "auto_de"

    dir_key = get_current_direction(context)
    dir_label = get_dir_label(dir_key, ui)

    text = t(ui, "start", direction=dir_label)
    await update.message.reply_text(
        text,
        reply_markup=make_direction_keyboard(context),
    )


async def help_students(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    await update.message.reply_text(t(ui, "help_students"))


async def limits(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    user_id = update.effective_user.id
    await update.message.reply_text(get_usage_status_text(user_id, ui))


async def password(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    await update.message.reply_text(t(ui, "password_request"))
    context.user_data["awaiting_password"] = True


async def ui_command(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    await update.message.reply_text(
        t(ui, "ui_choose"),
        reply_markup=make_ui_keyboard(),
    )


# -------------------------------------------------------------------
# CALLBACK-КНОПКИ
# -------------------------------------------------------------------


async def direction_or_menu_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    ui = get_ui_lang(context)

    # выбор направления перевода
    if data.startswith("dir:"):
        key = data.split(":", 1)[1]
        if key in DIRECTIONS:
            set_current_direction(context, key)
        dir_label = get_dir_label(get_current_direction(context), ui)
        msg = t(ui, "start", direction=dir_label)
        await query.edit_message_text(
            msg,
            reply_markup=make_direction_keyboard(context),
        )
        logger.info(
            "Пользователь %s сменил направление на %s", query.from_user.id, key
        )
        return

    # меню Help
    if data == "menu:help":
        await query.message.reply_text(t(ui, "help_students"))
        return

    # меню смены UI
    if data == "menu:ui":
        await query.message.reply_text(
            t(ui, "ui_choose"),
            reply_markup=make_ui_keyboard(),
        )
        return

    # переключение языка интерфейса
    if data.startswith("ui:"):
        lang = data.split(":", 1)[1]
        set_ui_lang(context, lang)
        await query.edit_message_text(
            t(lang, "ui_set", lang_name=ui_lang_name(lang))
        )
        return


# -------------------------------------------------------------------
# TEКСТОВЫЕ СООБЩЕНИЯ (для пароля и т.п.)
# -------------------------------------------------------------------


async def text_message(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # ожидаем пароль
    if context.user_data.get("awaiting_password"):
        context.user_data["awaiting_password"] = False

        matched_level = None
        for level, pwd in LEVEL_PASSWORDS.items():
            if text == pwd:
                matched_level = level
                break

        if matched_level:
            set_user_level(user_id, matched_level)
            await update.message.reply_text(
                t(
                    ui,
                    "password_ok",
                    level=matched_level,
                    limit=LEVEL_LIMITS[matched_level],
                )
            )
        else:
            await update.message.reply_text(t(ui, "password_bad"))
        return

    # любое другое текстовое сообщение можно игнорировать или добавить подсказку
    await update.message.reply_text("Используйте /start, /help, /limits или отправьте голосовое сообщение.")


# -------------------------------------------------------------------
# ОБРАБОТКА VOICE
# -------------------------------------------------------------------


async def voice_message(update: Update, context: CallbackContext) -> None:
    ui = get_ui_lang(context)
    user = update.effective_user
    user_id = user.id

    if user_id not in PREMIUM_USERS:
        # проверяем уровень
        level = get_user_level(user_id)
        if not level:
            await update.message.reply_text(t(ui, "limits_unset"))
            return

        if not check_and_increment_usage(user_id):
            await update.message.reply_text(t(ui, "limit_reached"))
            return

    # получаем направление
    dir_key = get_current_direction(context)
    dir_data = DIRECTIONS[dir_key]
    src_lang = dir_data["src"]
    dest_lang = dir_data["dest"]

    voice = update.message.voice
    if not voice:
        return

    try:
        file = await context.bot.get_file(voice.file_id)
        ogg_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.error("Ошибка загрузки файла: %s", e)
        await update.message.reply_text("Ошибка загрузки голосового сообщения.")
        return

    # распознаём речь
    recognized = recognize_speech_from_ogg(bytes(ogg_bytes), src_lang)
    if not recognized:
        await update.message.reply_text(t(ui, "stt_fail"))
        return

    # переводим
    try:
        logger.info(
            "Перевожу текст. src=%s dest=%s text=%r", src_lang, dest_lang, recognized
        )
        translated = translate_text(src_lang, dest_lang, recognized)
    except Exception as e:
        logger.error("Ошибка перевода: %s", e)
        await update.message.reply_text(t(ui, "translate_error"))
        return

    # отправляем текст
    await update.message.reply_text(
        f"🗣 {recognized}\n\n🔁 ➜ {translated}"
    )

    # озвучиваем на языке назначения (если поддерживается gTTS)
    tts_lang_map = {
        "ru": "ru",
        "de": "de",
        "en": "en",
        "tr": "tr",
        "ro": "ro",
        "ar": "ar",
        "pl": "pl",
    }

    if dest_lang in tts_lang_map:
        try:
            ogg_buf = tts_to_ogg(tts_lang_map[dest_lang], translated)
            await update.message.reply_voice(
                voice=InputFile(ogg_buf, filename="answer.ogg")
            )
        except Exception as e:
            logger.error("Ошибка генерации аудио: %s", e)
            await update.message.reply_text(t(ui, "audio_error"))


# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------


def main() -> None:
    logger.info("Запускаю webhook на %s, порт %s", WEBHOOK_URL, PORT)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Хендлеры команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_students))
    application.add_handler(CommandHandler("limits", limits))
    application.add_handler(CommandHandler("password", password))
    application.add_handler(CommandHandler("ui", ui_command))

    # Callback-кнопки
    application.add_handler(CallbackQueryHandler(direction_or_menu_callback))

    # Текст и голос
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message)
    )
    application.add_handler(MessageHandler(filters.VOICE, voice_message))

    # Запуск как webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
