import os
import logging
import tempfile
import datetime
from typing import Dict, Any

import asyncio
import speech_recognition as sr
from pydub import AudioSegment
from gtts import gTTS
from deep_translator import GoogleTranslator

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =======================
#  НАСТРОЙКИ ЛОГГЕРА
# =======================
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =======================
#  ТОКЕНЫ / WEHBOOK
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN или TELEGRAM_BOT_TOKEN в переменных окружения")

BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", "https://bratik.onrender.com").strip()
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))

# =======================
#  ТАРИФЫ / ПАРОЛИ
# =======================
DEFAULT_FREE_LIMIT = 5  # бесплатный триал

TARIFF_LIMITS: Dict[str, int | None] = {
    "FREE": DEFAULT_FREE_LIMIT,
    "A1": 30,
    "A2": 60,
    "B1": 100,
    "VIP": None,  # None = безлимит
}

TARIFF_PASSWORDS: Dict[str, str] = {
    "karbofosA1": "A1",
    "karbofosA2": "A2",
    "karbofosB1": "B1",
    "karbofosVIP": "VIP",
}

# VIP-друг (безлимит независимо от тарифа)
VIP_USER_IDS = {1300323894}

# =======================
#  НАПРАВЛЕНИЯ ПЕРЕВОДА
# =======================
# stt_locale — для SpeechRecognition
DIRECTIONS: Dict[str, Dict[str, Any]] = {
    "ru_de": {
        "label": "🇷🇺 RU → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "ru-RU",
    },
    "de_ru": {
        "label": "🇩🇪 DE → 🇷🇺 RU",
        "src_lang_code": "de",
        "dest_lang_code": "ru",
        "stt_locale": "de-DE",
    },
    "en_de": {
        "label": "🇬🇧 EN → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "en-US",
    },
    "de_en": {
        "label": "🇩🇪 DE → 🇬🇧 EN",
        "src_lang_code": "de",
        "dest_lang_code": "en",
        "stt_locale": "de-DE",
    },
    "tr_de": {
        "label": "🇹🇷 TR → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "tr-TR",
    },
    "de_tr": {
        "label": "🇩🇪 DE → 🇹🇷 TR",
        "src_lang_code": "de",
        "dest_lang_code": "tr",
        "stt_locale": "de-DE",
    },
    "ro_de": {
        "label": "🇷🇴 RO → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "ro-RO",
    },
    "de_ro": {
        "label": "🇩🇪 DE → 🇷🇴 RO",
        "src_lang_code": "de",
        "dest_lang_code": "ro",
        "stt_locale": "de-DE",
    },
    "pl_de": {
        "label": "🇵🇱 PL → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "pl-PL",
    },
    "de_pl": {
        "label": "🇩🇪 DE → 🇵🇱 PL",
        "src_lang_code": "de",
        "dest_lang_code": "pl",
        "stt_locale": "de-DE",
    },
    "ar_de": {
        "label": "🇸🇾 AR(SY) → 🇩🇪 DE",
        "src_lang_code": "auto",
        "dest_lang_code": "de",
        "stt_locale": "ar-SY",
    },
    "de_ar": {
        "label": "🇩🇪 DE → 🇸🇾 AR(SY)",
        "src_lang_code": "de",
        "dest_lang_code": "ar",
        "stt_locale": "de-DE",
    },
}

# =======================
#  ЯЗЫК ИНТЕРФЕЙСА
# =======================
SUPPORTED_UI_LANGS = ["ru", "de"]

# Хранилище состояния пользователей: в памяти
user_state: Dict[int, Dict[str, Any]] = {}
state_lock = asyncio.Lock()


def get_today_str() -> str:
    return datetime.date.today().isoformat()


def get_user_state(user_id: int) -> Dict[str, Any]:
    if user_id not in user_state:
        user_state[user_id] = {
            "ui_lang": "ru",              # язык интерфейса
            "direction": "ru_de",         # направление по умолчанию
            "tariff": "FREE",             # тариф
            "usage": {"date": get_today_str(), "count": 0},
            "awaiting_password": False,
        }
    return user_state[user_id]


def is_vip(user_id: int, state: Dict[str, Any]) -> bool:
    return user_id in VIP_USER_IDS or state.get("tariff") == "VIP"


async def check_and_increment_usage(user_id: int) -> tuple[bool, int | None, str, int | None]:
    """
    Возвращает:
        allowed: bool
        remaining: int | None  (оставшиеся запросы, None для VIP)
        tariff: str
        limit: int | None
    """
    async with state_lock:
        st = get_user_state(user_id)
        if is_vip(user_id, st):
            return True, None, "VIP", None

        tariff = st.get("tariff", "FREE")
        limit = TARIFF_LIMITS.get(tariff, DEFAULT_FREE_LIMIT)
        today = get_today_str()

        usage = st.setdefault("usage", {"date": today, "count": 0})
        if usage["date"] != today:
            usage["date"] = today
            usage["count"] = 0

        if limit is not None and usage["count"] >= limit:
            return False, 0, tariff, limit

        usage["count"] += 1
        remaining = None
        if limit is not None:
            remaining = limit - usage["count"]

        return True, remaining, tariff, limit


def build_direction_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(DIRECTIONS["ru_de"]["label"], callback_data="dir:ru_de"),
            InlineKeyboardButton(DIRECTIONS["de_ru"]["label"], callback_data="dir:de_ru"),
        ],
        [
            InlineKeyboardButton(DIRECTIONS["en_de"]["label"], callback_data="dir:en_de"),
            InlineKeyboardButton(DIRECTIONS["de_en"]["label"], callback_data="dir:de_en"),
        ],
        [
            InlineKeyboardButton(DIRECTIONS["tr_de"]["label"], callback_data="dir:tr_de"),
            InlineKeyboardButton(DIRECTIONS["de_tr"]["label"], callback_data="dir:de_tr"),
        ],
        [
            InlineKeyboardButton(DIRECTIONS["ro_de"]["label"], callback_data="dir:ro_de"),
            InlineKeyboardButton(DIRECTIONS["de_ro"]["label"], callback_data="dir:de_ro"),
        ],
        [
            InlineKeyboardButton(DIRECTIONS["pl_de"]["label"], callback_data="dir:pl_de"),
            InlineKeyboardButton(DIRECTIONS["de_pl"]["label"], callback_data="dir:de_pl"),
        ],
        [
            InlineKeyboardButton(DIRECTIONS["ar_de"]["label"], callback_data="dir:ar_de"),
            InlineKeyboardButton(DIRECTIONS["de_ar"]["label"], callback_data="dir:de_ar"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_lang_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
            InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang:de"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def ui_text(key: str, lang: str = "ru") -> str:
    """Простые тексты для интерфейса (ru/de)."""
    texts = {
        "start_ru": (
            "👋 Привет! Я *голосовой переводчик* для курсов немецкого.\n\n"
            "🎧 Отправь мне голосовое сообщение — я распознаю речь, переведу и пришлю текст.\n"
            "🔊 Плюс озвучу перевод голосом.\n\n"
            "📚 Выбери направление перевода кнопкой ниже.\n"
            "🔑 Чтобы снять ограничения, введи пароль командой /password"
        ),
        "start_de": (
            "👋 Hallo! Ich bin ein *Sprachübersetzer* für Deutschkurse.\n\n"
            "🎧 Schick mir eine Sprachnachricht – ich erkenne die Sprache, übersetze und sende dir den Text.\n"
            "🔊 Außerdem lese ich die Übersetzung vor.\n\n"
            "📚 Wähle die Übersetzungsrichtung über die Schaltflächen unten.\n"
            "🔑 Um Limits zu entfernen, gib dein Passwort mit /password ein."
        ),
        "help_students_ru": (
            "📖 *Как пользоваться ботом:*\n\n"
            "1️⃣ Нажми /start и выбери направление перевода (например, 🇷🇺→🇩🇪 или 🇩🇪→🇷🇺).\n"
            "2️⃣ Удерживай кнопку микрофона и запиши фразу.\n"
            "3️⃣ Отправь голосовое сообщение боту.\n"
            "4️⃣ Бот пришлёт:\n"
            "   • распознанный текст,\n"
            "   • перевод,\n"
            "   • аудио с произношением перевода.\n\n"
            "❗ Если лимит исчерпан — попроси у преподавателя новый пароль для тарифа A1/A2/B1/VIP."
        ),
        "help_students_de": (
            "📖 *So benutzt du den Bot:*\n\n"
            "1️⃣ Tippe /start und wähle die Übersetzungsrichtung (z. B. 🇩🇪→🇷🇺 oder 🇷🇺→🇩🇪).\n"
            "2️⃣ Halte die Mikrofon-Taste gedrückt und sprich deinen Satz.\n"
            "3️⃣ Sende die Sprachnachricht an den Bot.\n"
            "4️⃣ Der Bot sendet dir:\n"
            "   • den erkannten Text,\n"
            "   • die Übersetzung,\n"
            "   • eine Audio-Datei mit der Aussprache.\n\n"
            "❗ Wenn dein Limit erreicht ist, bitte deinen Lehrer um ein neues Passwort (A1/A2/B1/VIP)."
        ),
        "ask_password_ru": "🔑 Отправь пароль тарифa (A1/A2/B1/VIP) одним сообщением.",
        "ask_password_de": "🔑 Sende dein Tarif-Passwort (A1/A2/B1/VIP) in einer Nachricht.",
        "password_ok_ru": "✅ Тариф *{tariff}* активирован!\nСегодняшний лимит: {limit}",
        "password_ok_de": "✅ Tarif *{tariff}* ist aktiviert!\nHeutiges Limit: {limit}",
        "password_bad_ru": "❌ Неверный пароль. Проверь и попробуй ещё раз.",
        "password_bad_de": "❌ Falsches Passwort. Bitte überprüfe es und versuche es erneut.",
        "no_tariff_ru": (
            "ℹ Сейчас у тебя *бесплатный триал* — {limit} запросов в день.\n"
            "Чтобы увеличить лимит, возьми пароль у преподавателя и отправь команду /password."
        ),
        "no_tariff_de": (
            "ℹ Du hast aktuell ein *kostenloses Trial* – {limit} Anfragen pro Tag.\n"
            "Für mehr Anfragen, hol dir ein Passwort von deinem Lehrer und nutze /password."
        ),
        "limit_reached_ru": (
            "⛔ Лимит на сегодня исчерпан по тарифу *{tariff}*.\n"
            "Доступно в день: {limit} запросов.\n"
            "Попроси у преподавателя новый пароль или подожди до завтра."
        ),
        "limit_reached_de": (
            "⛔ Dein Tageslimit für Tarif *{tariff}* ist erreicht.\n"
            "Erlaubt pro Tag: {limit} Anfragen.\n"
            "Frag deinen Lehrer nach einem neuen Passwort oder warte bis morgen."
        ),
        "direction_changed_ru": "✅ Направление перевода установлено: {label}",
        "direction_changed_de": "✅ Übersetzungsrichtung gesetzt: {label}",
        "lang_changed_ru": "✅ Язык интерфейса: Русский 🇷🇺",
        "lang_changed_de": "✅ Sprache des Interfaces: Deutsch 🇩🇪",
        "tts_error_ru": "Перевод выполнен, но озвучку сейчас не удалось сгенерировать.",
        "tts_error_de": "Übersetzung erfolgreich, aber die Audio-Ausgabe konnte nicht erzeugt werden.",
        "stt_fail_ru": "❗ Не удалось распознать речь. Попробуй записать ещё раз, чуть чётче и громче.",
        "stt_fail_de": "❗ Sprache konnte nicht erkannt werden. Bitte sprich etwas deutlicher und versuche es erneut.",
    }
    return texts.get(f"{key}_{lang}", texts.get(f"{key}_ru", ""))


# =======================
#  ПЕРЕВОД ТЕКСТА
# =======================
def translate_text(text: str, direction_key: str) -> str:
    cfg = DIRECTIONS[direction_key]
    src = cfg["src_lang_code"]
    dest = cfg["dest_lang_code"]
    try:
        translator = GoogleTranslator(source=src, target=dest)
        return translator.translate(text)
    except Exception as e:
        logger.exception("Ошибка перевода: %s", e)
        return "[Ошибка перевода]"


# =======================
#  TTS (gTTS → audio)
# =======================
def map_lang_to_gtts(lang: str) -> str:
    # gTTS поддерживает: 'de', 'en', 'ru', 'tr', 'ro', 'pl', 'ar'
    if lang in {"de", "en", "ru", "tr", "ro", "pl", "ar"}:
        return lang
    return "de"


async def send_tts_audio(
    chat_id: int,
    text: str,
    dest_lang: str,
    context: ContextTypes.DEFAULT_TYPE,
    ui_lang: str,
) -> None:
    try:
        gtts_lang = map_lang_to_gtts(dest_lang)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_name = tmp.name
        tts = gTTS(text=text, lang=gtts_lang)
        tts.save(tmp_name)

        with open(tmp_name, "rb") as f:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=f,
                title="Pronunciation",
            )
        os.remove(tmp_name)
    except Exception as e:
        logger.exception("Ошибка озвучки: %s", e)
        # Сообщение пользователю мягко, без краша
        await context.bot.send_message(
            chat_id=chat_id,
            text=ui_text("tts_error", ui_lang),
        )


# =======================
#  STT (SpeechRecognition)
# =======================
def ogg_to_wav(ogg_path: str) -> str:
    sound = AudioSegment.from_file(ogg_path)
    wav_path = ogg_path + ".wav"
    sound.export(wav_path, format="wav")
    return wav_path


def recognize_speech_from_file(path: str, locale: str) -> str | None:
    r = sr.Recognizer()
    with sr.AudioFile(path) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio, language=locale)
    except sr.UnknownValueError:
        return None
    except Exception as e:
        logger.exception("Ошибка распознавания речи: %s", e)
        return None


# =======================
#  ХЕНДЛЕРЫ КОМАНД
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st["ui_lang"]

    await update.message.reply_markdown(
        ui_text("start", lang),
        reply_markup=build_direction_keyboard(),
    )


async def help_students(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st["ui_lang"]

    await update.message.reply_markdown(ui_text("help_students", lang))


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Выбери язык интерфейса / Wähle die Interface-Sprache:",
        reply_markup=build_lang_keyboard(),
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st["ui_lang"]

    tariff = st.get("tariff", "FREE")
    usage = st.get("usage", {})
    count = usage.get("count", 0)
    today = usage.get("date", get_today_str())

    if is_vip(user.id, st):
        text = (
            f"👤 Тариф: *VIP* (безлимит)\n"
            f"📅 Сегодня: {today}\n"
            f"🔢 Использовано запросов: {count}\n"
        )
    else:
        limit = TARIFF_LIMITS.get(tariff, DEFAULT_FREE_LIMIT)
        remaining = max(limit - count, 0) if limit is not None else "∞"
        text = (
            f"👤 Тариф: *{tariff}*\n"
            f"📅 Сегодня: {today}\n"
            f"🔢 Использовано: {count} / {limit}\n"
            f"✅ Осталось на сегодня: {remaining}"
        )

    await update.message.reply_markdown(text)


async def password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    st["awaiting_password"] = True
    lang = st["ui_lang"]
    await update.message.reply_text(ui_text("ask_password", lang))


# =======================
#  CALLBACK-DATA (кнопки)
# =======================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    st = get_user_state(user.id)
    lang = st["ui_lang"]

    data = query.data or ""

    # смена направления
    if data.startswith("dir:"):
        dir_key = data.split(":", 1)[1]
        if dir_key in DIRECTIONS:
            st["direction"] = dir_key
            label = DIRECTIONS[dir_key]["label"]
            await query.message.reply_text(
                ui_text("direction_changed", lang).format(label=label)
            )
        return

    # смена языка интерфейса
    if data.startswith("lang:"):
        ui = data.split(":", 1)[1]
        if ui in SUPPORTED_UI_LANGS:
            st["ui_lang"] = ui
            await query.message.reply_text(ui_text("lang_changed", ui))
        return


# =======================
#  ОБРАБОТКА ТЕКСТА
# =======================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    st = get_user_state(user.id)
    lang = st["ui_lang"]
    text = update.message.text.strip()

    # Если ждём пароль
    if st.get("awaiting_password"):
        st["awaiting_password"] = False
        tariff = TARIFF_PASSWORDS.get(text)
        if not tariff:
            await update.message.reply_text(ui_text("password_bad", lang))
            return

        async with state_lock:
            st["tariff"] = tariff
            st["usage"] = {"date": get_today_str(), "count": 0}

        limit = TARIFF_LIMITS[tariff]
        limit_str = "∞" if limit is None else str(limit)
        await update.message.reply_markdown(
            ui_text("password_ok", lang).format(tariff=tariff, limit=limit_str)
        )
        return

    # обычный текст → перевод
    allowed, remaining, tariff, limit = await check_and_increment_usage(user.id)
    if not allowed:
        msg = ui_text("limit_reached", lang).format(tariff=tariff, limit=limit)
        await update.message.reply_text(msg)
        return

    direction_key = st.get("direction", "ru_de")
    translated = translate_text(text, direction_key)
    cfg = DIRECTIONS[direction_key]
    dest_lang = cfg["dest_lang_code"]

    reply_text = (
        f"📝 *Исходный текст:*\n{text}\n\n"
        f"🌐 *Перевод ({cfg['label']}):*\n{translated}"
    )
    await update.message.reply_markdown(reply_text)

    # Озвучка
    await send_tts_audio(chat_id, translated, dest_lang, context, lang)


# =======================
#  ОБРАБОТКА ГОЛОСОВЫХ
# =======================
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    st = get_user_state(user.id)
    lang = st["ui_lang"]

    direction_key = st.get("direction", "ru_de")
    cfg = DIRECTIONS[direction_key]
    stt_locale = cfg["stt_locale"]

    allowed, remaining, tariff, limit = await check_and_increment_usage(user.id)
    if not allowed:
        msg = ui_text("limit_reached", lang).format(tariff=tariff, limit=limit)
        await update.message.reply_text(msg)
        return

    # скачиваем файл
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp_ogg:
        ogg_path = tmp_ogg.name
    await file.download_to_drive(ogg_path)

    try:
        wav_path = ogg_to_wav(ogg_path)
        recognized = recognize_speech_from_file(wav_path, stt_locale)

        if not recognized:
            await update.message.reply_text(ui_text("stt_fail", lang))
            return

        logger.info("Распознано: %r", recognized)

        translated = translate_text(recognized, direction_key)
        dest_lang = cfg["dest_lang_code"]

        reply_text = (
            f"👂 *Распознано:*\n{recognized}\n\n"
            f"🌐 *Перевод ({cfg['label']}):*\n{translated}"
        )
        await update.message.reply_markdown(reply_text)

        # Озвучка перевода
        await send_tts_audio(chat_id, translated, dest_lang, context, lang)

    finally:
        # подчистить временные файлы
        for p in (ogg_path, ogg_path + ".wav"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# =======================
#  MAIN / WEBHOOK
# =======================
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_students))
    application.add_handler(CommandHandler("lang", lang_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("password", password_cmd))

    # Callback-кнопки
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Сообщения
    application.add_handler(
        MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # Webhook URL
    webhook_url = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
    logger.info("Запускаю webhook на %s, порт %d", webhook_url, PORT)

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
