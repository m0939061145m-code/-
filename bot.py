import os
import logging
from datetime import date
from pathlib import Path
from io import BytesIO
import tempfile

from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment
import speech_recognition as sr

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

# ---------------------------------------
# БАЗОВЫЙ КОНФИГ
# ---------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

# Админы (видят /adminhelp и всегда без лимита)
OWNER_ID = 7707071177
FRIEND_ID = 1300323894
ADMINS = {OWNER_ID, FRIEND_ID}
UNLIMITED_USERS = {OWNER_ID, FRIEND_ID}

# Группы и лимиты
GROUP_LIMITS = {
    "free": 30,   # DEMO / Free
    "a1": 30,
    "a2": 60,
    "b1": 100,
    "vip": None,  # None = безлимит
}

GROUP_LABELS_RU = {
    "free": "Free (DEMO)",
    "a1": "Группа A1",
    "a2": "Группа A2",
    "b1": "Группа B1",
    "vip": "VIP (безлимит)",
}

GROUP_TEACHERS_RU = {
    "free": "—",
    "a1": "Юрий Николаевич",
    "a2": "Преподаватель A2",
    "b1": "Преподаватель B1",
    "vip": "Индивидуальный доступ",
}

GROUP_PASSWORDS = {
    "karbofos-a1": "a1",
    "karbofos-a2": "a2",
    "karbofos-b1": "b1",
    "karbofos-vip": "vip",
}

# Направления перевода: любой ↔ DE
DIRECTIONS = {
    "ru_de": {"label": "🇷🇺 RU ➝ 🇩🇪 DE", "src": "ru", "dest": "de", "stt_locale": "ru-RU"},
    "de_ru": {"label": "🇩🇪 DE ➝ 🇷🇺 RU", "src": "de", "dest": "ru", "stt_locale": "de-DE"},
    "en_de": {"label": "🇬🇧 EN ➝ 🇩🇪 DE", "src": "en", "dest": "de", "stt_locale": "en-US"},
    "de_en": {"label": "🇩🇪 DE ➝ 🇬🇧 EN", "src": "de", "dest": "en", "stt_locale": "de-DE"},
    "tr_de": {"label": "🇹🇷 TR ➝ 🇩🇪 DE", "src": "tr", "dest": "de", "stt_locale": "tr-TR"},
    "de_tr": {"label": "🇩🇪 DE ➝ 🇹🇷 TR", "src": "de", "dest": "tr", "stt_locale": "de-DE"},
    "ro_de": {"label": "🇷🇴 RO ➝ 🇩🇪 DE", "src": "ro", "dest": "de", "stt_locale": "ro-RO"},
    "de_ro": {"label": "🇩🇪 DE ➝ 🇷🇴 RO", "src": "de", "dest": "ro", "stt_locale": "de-DE"},
    "pl_de": {"label": "🇵🇱 PL ➝ 🇩🇪 DE", "src": "pl", "dest": "de", "stt_locale": "pl-PL"},
    "de_pl": {"label": "🇩🇪 DE ➝ 🇵🇱 PL", "src": "de", "dest": "pl", "stt_locale": "de-DE"},
    "ar_de": {"label": "🇸🇾 AR(SY) ➝ 🇩🇪 DE", "src": "ar", "dest": "de", "stt_locale": "ar-SY"},
    "de_ar": {"label": "🇩🇪 DE ➝ 🇸🇾 AR(SY)", "src": "de", "dest": "ar", "stt_locale": "de-DE"},
}

# Язык озвучки (gTTS)
TTS_LANG = {
    "ru": "ru",
    "de": "de",
    "en": "en",
    "tr": "tr",
    "ro": "ro",
    "pl": "pl",
    "ar": "ar",
}

# Языки интерфейса
UI_LANGS = {
    "ru": {"flag": "🇷🇺", "label": "Русский"},
    "de": {"flag": "🇩🇪", "label": "Deutsch"},
    "en": {"flag": "🇬🇧", "label": "English"},
    "tr": {"flag": "🇹🇷", "label": "Türkçe"},
    "pl": {"flag": "🇵🇱", "label": "Polski"},
    "ro": {"flag": "🇷🇴", "label": "Română"},
    "ar": {"flag": "🇸🇾", "label": "العربية"},
}

# Тексты интерфейса (RU + EN; DE/TR/PL/RO/AR → используют EN как базу)
TEXTS = {
    "ru": {
        "start": (
            "Привет! Я голосовой переводчик для курсов немецкого.\n\n"
            "🎙 Просто пришли мне голосовое сообщение — я распознаю речь, переведу и пришлю текст.\n"
            "🔊 Плюс могу озвучить перевод голосом.\n\n"
            "📌 Текущее направление перевода: {direction}\n"
            "👥 Текущая группа: {group_name}\n"
            "📊 Лимит: {limit_line}\n"
            "Сегодня использовано: {used}\n"
            "Сегодня осталось: {left}\n\n"
            "🔄 Направление можно сменить кнопкой ниже.\n"
            "🔑 Чтобы снять ограничения, используй /password.\n"
            "🌐 Язык интерфейса: {ui_lang_flag} {ui_lang_label} (команда /lang).\n\n"
            "ℹ Команды: /help /pricing /status /groupinfo /lang"
        ),
        "limit_line_unlimited": "безлимит",
        "limit_reached": (
            "⛔ Лимит переводов на сегодня исчерпан.\n"
            "Твой тариф: {group_name}, лимит: {limit} в день.\n"
            "Попроси преподавателя выдать пароль, чтобы повысить уровень доступа."
        ),
        "translation_header": "📥 Оригинал:\n{original}\n\n📤 Перевод ({src} ➝ {dest}):\n{translated}",
        "no_speech": "😕 Не удалось распознать речь. Попробуй сказать чуть чётче или ближе к микрофону.",
        "error_generic": "❌ Что-то пошло не так, попробуй ещё раз чуть позже.",
        "password_usage": (
            "🔑 Отправь пароль так (в одно сообщение):\n"
            "`/password karbofos-a1`\n\n"
            "Доступные примеры:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`."
        ),
        "password_ok": (
            "✅ Пароль принят.\n"
            "Твоя новая группа: {group_name}.\n"
            "Лимит в день: {limit_line}."
        ),
        "password_bad": "❌ Неверный пароль. Проверь написание или уточни у преподавателя.",
        "status": (
            "📊 Статус доступа:\n"
            "Группа: {group_name}\n"
            "Лимит в день: {limit_line}\n"
            "Сегодня использовано: {used}\n"
            "Сегодня осталось: {left}"
        ),
        "pricing": (
            "💳 Тарифы и лимиты в день:\n"
            "• Free (DEMO) — 30 переводов\n"
            "• A1 — 30 переводов\n"
            "• A2 — 60 переводов\n"
            "• B1 — 100 переводов\n"
            "• VIP — безлимит\n\n"
            "Оплата: обсуждается с преподавателем (наличные / перевод / PayPal / Revolut / SEPA и т.п.)."
        ),
        "groupinfo": (
            "👥 Информация по группам:\n"
            "• A1 — преподаватель: {a1}\n"
            "• A2 — преподаватель: {a2}\n"
            "• B1 — преподаватель: {b1}\n"
            "• VIP — индивидуальный доступ ({vip})\n\n"
            "Чтобы активировать правильный уровень, возьми пароль у своего преподавателя и введи его через /password."
        ),
        "help_student": (
            "📚 Как пользоваться ботом:\n\n"
            "1️⃣ Нажми /start и выбери нужное направление перевода (RU↔DE, EN↔DE и т.д.).\n"
            "2️⃣ Отправь голосовое сообщение — я распознаю текст и переведу его.\n"
            "3️⃣ Я отправлю перевод текстом и, если возможно, озвучу его голосом.\n"
            "4️⃣ Следи за лимитом через /status. Чтобы расширить лимит, используй пароль от преподавателя через /password.\n"
            "5️⃣ /pricing — тарифы, /groupinfo — инфо по группам, /lang — смена языка интерфейса.\n\n"
            "Если что-то работает странно — скажи преподавателю или напиши моему создателю 😉"
        ),
        "lang_choose": "🌐 Выбери язык интерфейса:",
        "lang_set": "✅ Язык интерфейса установлен: {flag} {label}",
        "admin_denied": "⛔ Эта команда только для администратора.",
        "admin_header": "🛠 Админ-панель Bratik Voice Translator",
        "admin_body": (
            "Пользователей всего: {total_users}\n"
            "Группы:\n"
            "• Free: {free}\n"
            "• A1: {a1}\n"
            "• A2: {a2}\n"
            "• B1: {b1}\n"
            "• VIP: {vip}\n\n"
            "Сегодняшние использования (суммарно по всем): {total_used_today}"
        ),
    },
    "en": {
        "start": (
            "Hi! I'm a voice translator for German courses.\n\n"
            "🎙 Just send me a voice message – I’ll recognize the speech, translate it and send the text.\n"
            "🔊 I can also speak the translation aloud.\n\n"
            "📌 Current direction: {direction}\n"
            "👥 Current group: {group_name}\n"
            "📊 Limit: {limit_line}\n"
            "Used today: {used}\n"
            "Left today: {left}\n\n"
            "🔄 You can change the direction with the buttons below.\n"
            "🔑 To remove limits, use /password.\n"
            "🌐 Interface language: {ui_lang_flag} {ui_lang_label} (/lang).\n\n"
            "ℹ Commands: /help /pricing /status /groupinfo /lang"
        ),
        "limit_line_unlimited": "unlimited",
        "limit_reached": (
            "⛔ You’ve reached today’s translation limit.\n"
            "Your plan: {group_name}, daily limit: {limit}.\n"
            "Ask your teacher for a password to upgrade your access."
        ),
        "translation_header": "📥 Original:\n{original}\n\n📤 Translation ({src} ➝ {dest}):\n{translated}",
        "no_speech": "😕 I couldn't recognize your voice. Please speak a bit clearer or closer to the microphone.",
        "error_generic": "❌ Something went wrong, please try again later.",
        "password_usage": (
            "🔑 Send the password like this (one message):\n"
            "`/password karbofos-a1`\n\n"
            "Available examples:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`."
        ),
        "password_ok": (
            "✅ Password accepted.\n"
            "Your new group: {group_name}.\n"
            "Daily limit: {limit_line}."
        ),
        "password_bad": "❌ Wrong password. Please check the spelling or ask your teacher.",
        "status": (
            "📊 Access status:\n"
            "Group: {group_name}\n"
            "Daily limit: {limit_line}\n"
            "Used today: {used}\n"
            "Left today: {left}"
        ),
        "pricing": (
            "💳 Plans and daily limits:\n"
            "• Free (DEMO) — 30 translations\n"
            "• A1 — 30 translations\n"
            "• A2 — 60 translations\n"
            "• B1 — 100 translations\n"
            "• VIP — unlimited\n\n"
            "Payment: arranged with your teacher (cash / bank transfer / PayPal / Revolut / SEPA etc.)."
        ),
        "groupinfo": (
            "👥 Groups info:\n"
            "• A1 — teacher: {a1}\n"
            "• A2 — teacher: {a2}\n"
            "• B1 — teacher: {b1}\n"
            "• VIP — individual access ({vip})\n\n"
            "To activate your level, get a password from your teacher and enter it via /password."
        ),
        "help_student": (
            "📚 How to use the bot:\n\n"
            "1️⃣ Tap /start and choose the direction (RU↔DE, EN↔DE, etc.).\n"
            "2️⃣ Send a voice message – I will recognize the speech and translate it.\n"
            "3️⃣ I’ll send the translation as text and, when possible, as voice.\n"
            "4️⃣ Check your limit via /status. To increase it, use a password from your teacher via /password.\n"
            "5️⃣ /pricing — plans, /groupinfo — group info, /lang — change interface language.\n\n"
            "If something looks weird, tell your teacher or my creator 😉"
        ),
        "lang_choose": "🌐 Choose interface language:",
        "lang_set": "✅ Interface language set to {flag} {label}",
        "admin_denied": "⛔ This command is for admin only.",
        "admin_header": "🛠 Bratik Voice Translator — admin panel",
        "admin_body": (
            "Total users: {total_users}\n"
            "Groups:\n"
            "• Free: {free}\n"
            "• A1: {a1}\n"
            "• A2: {a2}\n"
            "• B1: {b1}\n"
            "• VIP: {vip}\n\n"
            "Total translations used today: {total_used_today}"
        ),
    },
}

# ---------------------------------------
# Память по пользователям (в RAM)
# ---------------------------------------

# user_id -> state dict
USERS: dict[int, dict] = {}


def get_user_state(user_id: int) -> dict:
    """Гарантированно вернуть словарь состояния пользователя."""
    if user_id not in USERS:
        USERS[user_id] = {
            "direction": "ru_de",
            "group": "free",
            "ui_lang": "ru",
            "used_today": 0,
            "last_date": date.today().isoformat(),
        }
    # сброс счётчика, если новый день
    today_str = date.today().isoformat()
    if USERS[user_id]["last_date"] != today_str:
        USERS[user_id]["last_date"] = today_str
        USERS[user_id]["used_today"] = 0
    return USERS[user_id]


def get_ui_lang(user_id: int) -> str:
    state = get_user_state(user_id)
    lang = state.get("ui_lang", "ru")
    if lang not in UI_LANGS:
        lang = "ru"
    return lang


def set_ui_lang(user_id: int, lang: str) -> None:
    state = get_user_state(user_id)
    if lang in UI_LANGS:
        state["ui_lang"] = lang
    else:
        state["ui_lang"] = "ru"


def L(user_id: int, key: str) -> str:
    """Вернуть текст по ключу с учётом языка пользователя."""
    lang = get_ui_lang(user_id)
    if lang in ("ru",):
        base = TEXTS["ru"]
    elif lang in ("de", "tr", "pl", "ro", "ar"):
        base = TEXTS["en"]
    else:
        base = TEXTS.get(lang, TEXTS["ru"])
    # для некоторых ключей fallback на en/ru
    if key in base:
        return base[key]
    if key in TEXTS["en"]:
        return TEXTS["en"][key]
    return TEXTS["ru"].get(key, "")


def format_limit_line(user_id: int, limit: int | None) -> str:
    if limit is None:
        return L(user_id, "limit_line_unlimited")
    return str(limit)


def get_daily_limit(user_id: int, state: dict | None = None) -> int | None:
    if user_id in UNLIMITED_USERS:
        return None
    if state is None:
        state = get_user_state(user_id)
    group = state.get("group", "free")
    return GROUP_LIMITS.get(group, GROUP_LIMITS["free"])


def check_and_increment_usage(user_id: int) -> tuple[bool, int | None, int, int | None]:
    """
    Возвращает:
      (ok, limit, used_after, left)
    """
    state = get_user_state(user_id)
    limit = get_daily_limit(user_id, state)
    used = state["used_today"]

    if limit is not None and used >= limit:
        left = 0
        return False, limit, used, left

    state["used_today"] += 1
    used = state["used_today"]
    left = None if limit is None else max(limit - used, 0)
    return True, limit, used, left


# ---------------------------------------
# УТИЛИТЫ: перевод, STT, TTS
# ---------------------------------------

def translate_text(text: str, src: str, dest: str) -> str:
    if not text.strip():
        return ""
    translator = GoogleTranslator(source=src, target=dest)
    return translator.translate(text)


def ogg_to_wav(ogg_path: Path) -> Path:
    wav_path = ogg_path.with_suffix(".wav")
    audio = AudioSegment.from_file(ogg_path)
    audio.export(wav_path, format="wav")
    return wav_path


def recognize_speech(wav_path: Path, locale: str) -> str | None:
    r = sr.Recognizer()
    with sr.AudioFile(str(wav_path)) as source:
        audio_data = r.record(source)
    try:
        text = r.recognize_google(audio_data, language=locale)
        logger.info("Распознано (%s): %s", locale, text)
        return text
    except sr.UnknownValueError:
        logger.warning("Google STT не смог распознать речь")
        return None
    except sr.RequestError as e:
        logger.error("Ошибка STT: %s", e)
        return None


def synthesize_voice(text: str, lang_code: str) -> BytesIO | None:
    try:
        tts_lang = TTS_LANG.get(lang_code, "de")
        tts = gTTS(text=text, lang=tts_lang)
        bio = BytesIO()
        tts.write_to_fp(bio)
        bio.seek(0)
        return bio
    except Exception as e:
        logger.error("Ошибка gTTS: %s", e)
        return None


# ---------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------

def make_direction_keyboard() -> InlineKeyboardMarkup:
    rows = [
        ["ru_de", "de_ru"],
        ["en_de", "de_en"],
        ["tr_de", "de_tr"],
        ["ro_de", "de_ro"],
        ["pl_de", "de_pl"],
        ["ar_de", "de_ar"],
    ]
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        btn_row: list[InlineKeyboardButton] = []
        for key in row:
            info = DIRECTIONS[key]
            btn_row.append(InlineKeyboardButton(info["label"], callback_data=f"setdir_{key}"))
        keyboard.append(btn_row)
    return InlineKeyboardMarkup(keyboard)


def make_lang_keyboard() -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, info in UI_LANGS.items():
        row.append(
            InlineKeyboardButton(
                f"{info['flag']} {info['label']}",
                callback_data=f"uilang_{code}",
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------
# ХЕНДЛЕРЫ КОМАНД
# ---------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    direction_key = state.get("direction", "ru_de")
    direction_label = DIRECTIONS[direction_key]["label"]
    group = state.get("group", "free")
    group_name = GROUP_LABELS_RU.get(group, group)
    limit = get_daily_limit(user.id, state)
    used = state["used_today"]
    left = None if limit is None else max(limit - used, 0)

    ui_lang_code = get_ui_lang(user.id)
    ui_lang_flag = UI_LANGS[ui_lang_code]["flag"]
    ui_lang_label = UI_LANGS[ui_lang_code]["label"]

    txt_template = L(user.id, "start")
    text = txt_template.format(
        direction=direction_label,
        group_name=group_name,
        limit_line=format_limit_line(user.id, limit),
        used=used,
        left="∞" if left is None else left,
        ui_lang_flag=ui_lang_flag,
        ui_lang_label=ui_lang_label,
    )

    await update.message.reply_text(
        text,
        reply_markup=make_direction_keyboard(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await update.message.reply_text(L(user.id, "help_student"))


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await update.message.reply_text(L(user.id, "pricing"))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    state = get_user_state(user.id)
    group = state.get("group", "free")
    group_name = GROUP_LABELS_RU.get(group, group)
    limit = get_daily_limit(user.id, state)
    used = state["used_today"]
    left = None if limit is None else max(limit - used, 0)

    txt_template = L(user.id, "status")
    text = txt_template.format(
        group_name=group_name,
        limit_line=format_limit_line(user.id, limit),
        used=used,
        left="∞" if left is None else left,
    )
    await update.message.reply_text(text)


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    txt_template = L(user.id, "groupinfo")
    text = txt_template.format(
        a1=GROUP_TEACHERS_RU["a1"],
        a2=GROUP_TEACHERS_RU["a2"],
        b1=GROUP_TEACHERS_RU["b1"],
        vip=GROUP_TEACHERS_RU["vip"],
    )
    await update.message.reply_text(text)


async def password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    args = context.args
    if not args:
        await update.message.reply_markdown(L(user.id, "password_usage"))
        return

    pwd = args[0].strip()
    group = GROUP_PASSWORDS.get(pwd)
    if not group:
        await update.message.reply_text(L(user.id, "password_bad"))
        return

    state = get_user_state(user.id)
    state["group"] = group
    limit = get_daily_limit(user.id, state)
    txt_template = L(user.id, "password_ok")
    text = txt_template.format(
        group_name=GROUP_LABELS_RU.get(group, group),
        limit_line=format_limit_line(user.id, limit),
    )
    await update.message.reply_text(text)


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await update.message.reply_text(
        L(user.id, "lang_choose"),
        reply_markup=make_lang_keyboard(),
    )


async def adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    if user.id not in ADMINS:
        await update.message.reply_text(L(user.id, "admin_denied"))
        return

    # статистика по группам
    counts = {"free": 0, "a1": 0, "a2": 0, "b1": 0, "vip": 0}
    total_used_today = 0
    for st in USERS.values():
        g = st.get("group", "free")
        counts[g] = counts.get(g, 0) + 1
        total_used_today += st.get("used_today", 0)

    header = L(user.id, "admin_header")
    body_template = L(user.id, "admin_body")
    body = body_template.format(
        total_users=len(USERS),
        free=counts.get("free", 0),
        a1=counts.get("a1", 0),
        a2=counts.get("a2", 0),
        b1=counts.get("b1", 0),
        vip=counts.get("vip", 0),
        total_used_today=total_used_today,
    )
    await update.message.reply_text(f"{header}\n\n{body}")


# ---------------------------------------
# CALLBACK: смена направления / языка UI
# ---------------------------------------

async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user
    state = get_user_state(user.id)

    data = query.data or ""
    if not data.startswith("setdir_"):
        return
    key = data[len("setdir_") :]
    if key not in DIRECTIONS:
        return

    state["direction"] = key
    direction_label = DIRECTIONS[key]["label"]
    text = f"✅ Направление перевода установлено: {direction_label}"
    await query.edit_message_reply_markup(reply_markup=make_direction_keyboard())
    await query.message.reply_text(text)


async def lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = query.from_user

    data = query.data or ""
    if not data.startswith("uilang_"):
        return
    code = data[len("uilang_") :]
    if code not in UI_LANGS:
        return

    set_ui_lang(user.id, code)
    info = UI_LANGS[code]
    msg = L(user.id, "lang_set").format(flag=info["flag"], label=info["label"])
    await query.message.reply_text(msg)


# ---------------------------------------
# ОБРАБОТКА ТЕКСТА И ГОЛОСА
# ---------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    ok, limit, used, left = check_and_increment_usage(user.id)
    if not ok:
        txt_template = L(user.id, "limit_reached")
        await message.reply_text(
            txt_template.format(
                group_name=GROUP_LABELS_RU.get(get_user_state(user.id)["group"], "—"),
                limit=limit,
            )
        )
        return

    state = get_user_state(user.id)
    direction = DIRECTIONS[state["direction"]]
    src = direction["src"]
    dest = direction["dest"]

    try:
        translated = translate_text(message.text, src, dest)
    except Exception as e:
        logger.error("Ошибка перевода: %s", e)
        await message.reply_text(L(user.id, "error_generic"))
        return

    txt_template = L(user.id, "translation_header")
    answer_text = txt_template.format(
        original=message.text,
        src=src.upper(),
        dest=dest.upper(),
        translated=translated,
    )
    await message.reply_text(answer_text)

    # Попробуем озвучить результат
    voice_io = synthesize_voice(translated, dest)
    if voice_io is not None:
        await message.reply_voice(voice_io)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    ok, limit, used, left = check_and_increment_usage(user.id)
    if not ok:
        txt_template = L(user.id, "limit_reached")
        await message.reply_text(
            txt_template.format(
                group_name=GROUP_LABELS_RU.get(get_user_state(user.id)["group"], "—"),
                limit=limit,
            )
        )
        return

    state = get_user_state(user.id)
    direction = DIRECTIONS[state["direction"]]
    src = direction["src"]
    dest = direction["dest"]
    locale = direction["stt_locale"]

    # Скачиваем voice во временный файл
    file = await message.voice.get_file()
    fd, ogg_path_str = tempfile.mkstemp(suffix=".oga")
    os.close(fd)
    ogg_path = Path(ogg_path_str)
    await file.download_to_drive(custom_path=str(ogg_path))

    try:
        wav_path = ogg_to_wav(ogg_path)
        recognized = recognize_speech(wav_path, locale)
    finally:
        # Чистим временные файлы
        try:
            if ogg_path.exists():
                ogg_path.unlink()
            if "wav_path" in locals() and wav_path.exists():
                wav_path.unlink()
        except Exception as e:
            logger.warning("Не удалось удалить временный файл: %s", e)

    if not recognized:
        await message.reply_text(L(user.id, "no_speech"))
        return

    try:
        translated = translate_text(recognized, src, dest)
    except Exception as e:
        logger.error("Ошибка перевода: %s", e)
        await message.reply_text(L(user.id, "error_generic"))
        return

    txt_template = L(user.id, "translation_header")
    answer_text = txt_template.format(
        original=recognized,
        src=src.upper(),
        dest=dest.upper(),
        translated=translated,
    )
    await message.reply_text(answer_text)

    # Озвучиваем результат
    voice_io = synthesize_voice(translated, dest)
    if voice_io is not None:
        await message.reply_voice(voice_io)


# ---------------------------------------
# ERROR HANDLER
# ---------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)
    # Аккуратно пытаться уведомить пользователя
    try:
        if isinstance(update, Update):
            user = update.effective_user
            message = update.effective_message
            if user and message:
                await message.reply_text(L(user.id, "error_generic"))
    except Exception:
        pass


# ---------------------------------------
# MAIN
# ---------------------------------------

def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("pricing", pricing))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CommandHandler("password", password_cmd))
    application.add_handler(CommandHandler("lang", lang_cmd))
    application.add_handler(CommandHandler("adminhelp", adminhelp))

    application.add_handler(CallbackQueryHandler(direction_button, pattern=r"^setdir_"))
    application.add_handler(CallbackQueryHandler(lang_button, pattern=r"^uilang_"))

    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(error_handler)

    return application


def main() -> None:
    application = build_application()

    if BASE_WEBHOOK_URL:
        webhook_url = BASE_WEBHOOK_URL.rstrip("/") + "/webhook"
        logger.info("Запускаю webhook на %s, порт %s", webhook_url, PORT)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=webhook_url,
        )
    else:
        logger.warning(
            "BASE_WEBHOOK_URL не задан, запускаю long polling "
            "(на Render так работать не будет)."
        )
        application.run_polling()


if __name__ == "__main__":
    main()
