import os
import logging
import datetime
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional

import ffmpeg
import speech_recognition as sr
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

# ------------------------ БАЗОВЫЕ НАСТРОЙКИ ------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://bratik.onrender.com")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

# ID друга с безлимитом
FRIEND_ID = 1300323894

# ------------------------ ТАРИФЫ И ЛИМИТЫ ------------------------

# Ключи групп:
# free, a1, a2, b1, pro
DAILY_LIMITS: Dict[str, Optional[int]] = {
    "free": 15,   # DEMO – 15 переводов в день
    "a1": 30,
    "a2": 60,
    "b1": 100,
    "pro": None,  # безлимит
}

GROUP_TITLES = {
    "free": "Free (DEMO)",
    "a1": "A1",
    "a2": "A2",
    "b1": "B1",
    "pro": "PRO (безлимит)",
}

# Пароли для активации
PASSWORDS = {
    "karbofos-a1": "a1",
    "karbofos-a2": "a2",
    "karbofos-b1": "b1",
    "karbofos-vip": "pro",
}

# ------------------------ НАПРАВЛЕНИЯ ПЕРЕВОДА ------------------------

# code → настройки направления
DIRECTIONS = {
    # RU ↔ DE
    "ru_de": {
        "label": "🇷🇺 RU → 🇩🇪 DE",
        "src_locale": "ru-RU",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_ru": {
        "label": "🇩🇪 DE → 🇷🇺 RU",
        "src_locale": "de-DE",
        "target_lang": "ru",
        "tts_lang": "ru",
    },

    # EN ↔ DE
    "en_de": {
        "label": "🇬🇧 EN → 🇩🇪 DE",
        "src_locale": "en-US",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_en": {
        "label": "🇩🇪 DE → 🇬🇧 EN",
        "src_locale": "de-DE",
        "target_lang": "en",
        "tts_lang": "en",
    },

    # TR ↔ DE
    "tr_de": {
        "label": "🇹🇷 TR → 🇩🇪 DE",
        "src_locale": "tr-TR",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_tr": {
        "label": "🇩🇪 DE → 🇹🇷 TR",
        "src_locale": "de-DE",
        "target_lang": "tr",
        "tts_lang": "tr",
    },

    # RO ↔ DE
    "ro_de": {
        "label": "🇷🇴 RO → 🇩🇪 DE",
        "src_locale": "ro-RO",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_ro": {
        "label": "🇩🇪 DE → 🇷🇴 RO",
        "src_locale": "de-DE",
        "target_lang": "ro",
        "tts_lang": "ro",
    },

    # PL ↔ DE
    "pl_de": {
        "label": "🇵🇱 PL → 🇩🇪 DE",
        "src_locale": "pl-PL",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_pl": {
        "label": "🇩🇪 DE → 🇵🇱 PL",
        "src_locale": "de-DE",
        "target_lang": "pl",
        "tts_lang": "pl",
    },

    # AR(SY) ↔ DE
    "ar_de": {
        "label": "🇸🇾 AR(SY) → 🇩🇪 DE",
        "src_locale": "ar-SY",
        "target_lang": "de",
        "tts_lang": "de",
    },
    "de_ar": {
        "label": "🇩🇪 DE → 🇸🇾 AR(SY)",
        "src_locale": "de-DE",
        "target_lang": "ar",
        "tts_lang": "ar",
    },
}

# ------------------------ ЯЗЫКИ ИНТЕРФЕЙСА ------------------------

INTERFACE_LANGS = {
    "ru": "Русский",
    "de": "Deutsch",
    "en": "English",
    "tr": "Türkçe",
    "ro": "Română",
    "pl": "Polski",
    "ar": "العربية (SY)",
}

# Тексты сообщений по языкам интерфейса (минимальный набор).
# Если какой-то текст не найден для языка — используем русскую версию.
TEXTS = {
    "ru": {
        "start_title": "Привет! Я голосовой переводчик для курсов немецкого.",
        "start_body": (
            "🎧 Отправь мне голосовое сообщение — я распознаю речь и переведу её.\n"
            "🔊 Плюс озвучу перевод голосом.\n\n"
            "📌 Можешь также отправлять обычный текст — я сразу пришлю перевод и голос."
        ),
        "start_footer": (
            "🖱 Переключай направление перевода кнопками ниже.\n"
            "🔑 Чтобы снять ограничения, введи пароль командой /password\n"
            "💶 Тарифы и оплата: /pricing\n"
            "📊 Статус лимитов: /status\n"
            "ℹ️ Информация по группам: /groupinfo\n"
            "🌐 Язык интерфейса: /setlang\n"
            "❓ Помощь: /help"
        ),
        "limits_line": "Текущая группа: *{group_title}*\nЛимит: *{limit_text}*\nСегодня использовано: *{used}*\nОсталось на сегодня: *{left}*",
        "limit_reached": (
            "🚫 Лимит переводов на сегодня для твоего тарифа исчерпан.\n\n"
            "Посмотри тарифы и варианты доступа: /pricing\n"
            "Или спроси у преподавателя пароль для расширения доступа."
        ),
        "password_prompt": (
            "🔑 Отправь пароль вот так:\n"
            "`/password karbofos-a1`\n\n"
            "Доступные примеры:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`."
        ),
        "password_ok": "✅ Тариф успешно изменён на *{group_title}*.",
        "password_bad": "❌ Неверный или устаревший пароль. Проверь у преподавателя.",
        "status_header": "📊 Твой текущий статус:",
        "pricing": (
            "💶 *Тарифы и оплата:*\n"
            "• Free (DEMO) — 15 переводов в день\n"
            "• 🎓 A1 — 9.99€ / месяц\n"
            "• 🎓 A2 — 14.99€ / месяц\n"
            "• 🎓 B1 — 19.99€ / месяц\n"
            "• 🥇 PRO (безлимит) — 39.99€ / месяц\n\n"
            "🧩 *Дополнительно:*\n"
            "• Разовый доступ «1 неделя теста» — 4.99€\n\n"
            "Оплата обсуждается с преподавателем (наличные / перевод / PayPal / Revolut / SEPA и т.п.)."
        ),
        "groupinfo": (
            "👥 *Информация по группам:*\n"
            "• A1 — преподаватель: Юрий Николаевич\n"
            "• A2 — преподаватель: Преподаватель A2\n"
            "• B1 — преподаватель: Преподаватель B1\n"
            "• PRO — индивидуальный доступ (индивидуальный тариф)\n\n"
            "Чтобы активировать правильный уровень, возьми пароль у своего преподавателя "
            "и введи его через команду /password."
        ),
        "help_student": (
            "❓ *Как пользоваться ботом:*\n\n"
            "1️⃣ Нажми /start, выбери направление перевода.\n"
            "2️⃣ Отправь голосовое или текст — я распознаю, переведу и озвучу.\n"
            "3️⃣ Следи за лимитом через /status.\n"
            "4️⃣ Чтобы получить полный доступ, спроси пароль у преподавателя и введи его через /password.\n\n"
            "Если что-то не работает — покажи это сообщение преподавателю. 🙌"
        ),
        "help_admin": (
            "🛠 *Admin help:*\n\n"
            "• /pricing — показать студентам тарифы и стоимость.\n"
            "• /groupinfo — объяснение, какие группы и кому принадлежат.\n"
            "• /status — текущий тариф и остаток лимита студента.\n"
            "• /password karbofos-a1|a2|b1|vip — смена уровня доступа.\n"
            "• /setlang — смена языка интерфейса (для студентов).\n\n"
            "ID друга с безлимитом уже зашит в код: 1300323894 (PRO, без лимита)."
        ),
        "setlang_prompt": "🌐 Выбери язык интерфейса:",
        "setlang_ok": "✅ Язык интерфейса изменён на: *{lang_name}*.",
        "recognized": "🗣 *Оригинал:* {text}",
        "translated": "🌍 *Перевод:* {text}",
        "recognize_fail": "⚠️ Не удалось распознать речь. Попробуй ещё раз, говори чуть чётче и ближе к микрофону.",
    },

    # минимальная английская версия (fallback)
    "en": {
        "start_title": "Hi! I am a voice translator for German courses.",
        "start_body": (
            "🎧 Send me a voice message – I will recognize the speech and translate it.\n"
            "🔊 I will also send back a voice with the translation.\n\n"
            "You can also send plain text – I’ll translate and voice it."
        ),
        "start_footer": (
            "🖱 Change translation direction with the buttons below.\n"
            "🔑 To unlock higher limits, use /password\n"
            "💶 Pricing: /pricing\n"
            "📊 Status: /status\n"
            "ℹ️ Groups info: /groupinfo\n"
            "🌐 Interface language: /setlang\n"
            "❓ Help: /help"
        ),
        "limits_line": "Current group: *{group_title}*\nLimit: *{limit_text}*\nUsed today: *{used}*\nLeft today: *{left}*",
        "limit_reached": (
            "🚫 Your daily translations limit is reached.\n\n"
            "Check pricing: /pricing and ask your teacher for an access password."
        ),
        "password_prompt": (
            "🔑 Send password like this:\n"
            "`/password karbofos-a1`"
        ),
        "password_ok": "✅ Plan updated to *{group_title}*.",
        "password_bad": "❌ Wrong or expired password. Ask your teacher.",
        "status_header": "📊 Your status:",
        "pricing": (
            "💶 *Plans & pricing:*\n"
            "• Free (DEMO) — 15 translations/day\n"
            "• 🎓 A1 — 9.99€ / month\n"
            "• 🎓 A2 — 14.99€ / month\n"
            "• 🎓 B1 — 19.99€ / month\n"
            "• 🥇 PRO (unlimited) — 39.99€ / month\n\n"
            "🧩 *Extra:*\n"
            "• One-week test access — 4.99€\n\n"
            "Payment is arranged with your teacher."
        ),
        "groupinfo": (
            "👥 *Groups info:*\n"
            "• A1 — teacher: Yuriy Nikolaevich\n"
            "• A2 — teacher: A2 Teacher\n"
            "• B1 — teacher: B1 Teacher\n"
            "• PRO — individual access (custom plan)."
        ),
        "help_student": (
            "❓ *How to use the bot:*\n\n"
            "1️⃣ Tap /start and choose direction.\n"
            "2️⃣ Send a voice or text message – I’ll translate & voice it.\n"
            "3️⃣ Check your limit via /status.\n"
            "4️⃣ To upgrade your plan, ask your teacher for a password and use /password."
        ),
        "help_admin": (
            "🛠 *Admin help:*\n\n"
            "Use /pricing, /groupinfo, /status and /password to manage students.\n"
            "Friend ID with PRO unlimited is hard-coded: 1300323894."
        ),
        "setlang_prompt": "🌐 Choose interface language:",
        "setlang_ok": "✅ Interface language changed to: *{lang_name}*.",
        "recognized": "🗣 *Original:* {text}",
        "translated": "🌍 *Translation:* {text}",
        "recognize_fail": "⚠️ Could not recognize speech, please try again.",
    },
}


def t(lang: str, key: str) -> str:
    """Достаём текст по ключу и языку интерфейса, с fallback на ru."""
    if lang not in TEXTS:
        lang = "ru"
    if key in TEXTS[lang]:
        return TEXTS[lang][key]
    return TEXTS["ru"].get(key, key)


# ------------------------ СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ ------------------------

@dataclass
class UserState:
    direction_code: str = "ru_de"
    group: str = "free"
    used_today: int = 0
    last_date: datetime.date = field(default_factory=lambda: datetime.date.today())
    interface_lang: str = "ru"


user_states: Dict[int, UserState] = {}
recognizer = sr.Recognizer()


def get_user_state(user_id: int) -> UserState:
    state = user_states.get(user_id)
    if state is None:
        state = UserState()
        # друг сразу на PRO
        if user_id == FRIEND_ID:
            state.group = "pro"
        user_states[user_id] = state

    today = datetime.date.today()
    if state.last_date != today:
        state.used_today = 0
        state.last_date = today

    return state


def format_limit_text(group: str) -> str:
    limit = DAILY_LIMITS.get(group)
    if limit is None:
        return "без лимита"
    return f"{limit} переводов/день"


async def check_and_increase_quota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True — можно переводить, False — лимит исчерпан (ответ уже отправлен)."""
    if not update.effective_user:
        return False

    user_id = update.effective_user.id
    state = get_user_state(user_id)
    lang = state.interface_lang

    # другу лимиты не считаем
    if user_id == FRIEND_ID:
        return True

    limit = DAILY_LIMITS.get(state.group)
    if limit is None:
        return True

    if state.used_today >= limit:
        await update.effective_message.reply_text(
            t(lang, "limit_reached"),
            parse_mode="Markdown",
        )
        return False

    state.used_today += 1
    return True


# ------------------------ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ------------------------

def make_direction_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора направления, как на скрине."""
    def btn(code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(DIRECTIONS[code]["label"], callback_data=f"dir:{code}")

    keyboard = [
        [btn("ru_de"), btn("de_ru")],
        [btn("en_de"), btn("de_en")],
        [btn("tr_de"), btn("de_tr")],
        [btn("ro_de"), btn("de_ro")],
        [btn("pl_de"), btn("de_pl")],
        [btn("ar_de"), btn("de_ar")],
    ]
    return InlineKeyboardMarkup(keyboard)


def make_lang_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for code, name in INTERFACE_LANGS.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lang:{code}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def ogg_to_wav(ogg_path: str, wav_path: str) -> None:
    (
        ffmpeg
        .input(ogg_path)
        .output(wav_path, format="wav", acodec="pcm_s16le", ac=1, ar="16000")
        .overwrite_output()
        .run(quiet=True)
    )


def tts_to_ogg(text: str, lang: str) -> str:
    """Создаём OGG-файл с озвучкой текста и возвращаем путь к нему."""
    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(mp3_fd)
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(ogg_fd)

    tts = gTTS(text=text, lang=lang)
    tts.save(mp3_path)

    (
        ffmpeg
        .input(mp3_path)
        .output(ogg_path, format="ogg", acodec="libopus")
        .overwrite_output()
        .run(quiet=True)
    )

    os.remove(mp3_path)
    return ogg_path


def translate_text(text: str, target_lang: str) -> str:
    translator = GoogleTranslator(source="auto", target=target_lang)
    return translator.translate(text)


# ------------------------ ОБРАБОТЧИКИ КОМАНД ------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    state = get_user_state(user.id)
    lang = state.interface_lang

    direction = DIRECTIONS[state.direction_code]
    group_title = GROUP_TITLES.get(state.group, state.group)
    limit_text = format_limit_text(state.group)
    left = (
        "∞" if DAILY_LIMITS.get(state.group) is None
        else max(DAILY_LIMITS[state.group] - state.used_today, 0)
    )

    header = f"{t(lang, 'start_title')}\n\n{t(lang, 'start_body')}\n\n"
    current = (
        f"🎯 Текущее направление: *{direction['label']}*\n"
        + t(lang, "limits_line").format(
            group_title=group_title,
            limit_text=limit_text,
            used=state.used_today,
            left=left,
        )
        + "\n\n"
    )
    footer = t(lang, "start_footer")

    await update.message.reply_text(
        header + current + footer,
        parse_mode="Markdown",
        reply_markup=make_direction_keyboard(),
    )


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    await update.message.reply_text(
        t(lang, "pricing"),
        parse_mode="Markdown",
    )


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    await update.message.reply_text(
        t(lang, "groupinfo"),
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    group_title = GROUP_TITLES.get(state.group, state.group)
    limit_text = format_limit_text(state.group)
    left = (
        "∞" if DAILY_LIMITS.get(state.group) is None
        else max(DAILY_LIMITS[state.group] - state.used_today, 0)
    )

    header = t(lang, "status_header")
    body = t(lang, "limits_line").format(
        group_title=group_title,
        limit_text=limit_text,
        used=state.used_today,
        left=left,
    )

    await update.message.reply_text(
        f"{header}\n\n{body}",
        parse_mode="Markdown",
    )


async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        await update.message.reply_text(
            t(lang, "password_prompt"),
            parse_mode="Markdown",
        )
        return

    token = parts[1].strip()
    group = PASSWORDS.get(token)

    if not group:
        await update.message.reply_text(
            t(lang, "password_bad"),
            parse_mode="Markdown",
        )
        return

    state.group = group
    state.used_today = 0  # обнуляем счётчик
    group_title = GROUP_TITLES.get(group, group)

    await update.message.reply_text(
        t(lang, "password_ok").format(group_title=group_title),
        parse_mode="Markdown",
    )


async def help_student(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    await update.message.reply_text(
        t(lang, "help_student"),
        parse_mode="Markdown",
    )


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = get_user_state(user.id)
    lang = state.interface_lang

    await update.message.reply_text(
        t(lang, "help_admin"),
        parse_mode="Markdown",
    )


async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    state = get_user_state(user.id)
    lang = state.interface_lang

    await update.message.reply_text(
        t(lang, "setlang_prompt"),
        reply_markup=make_lang_keyboard(),
    )


# ------------------------ CALLBACK-ОБРАБОТЧИКИ ------------------------

async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    state = get_user_state(user.id)
    lang = state.interface_lang

    data = query.data  # dir:ru_de
    _, code = data.split(":", maxsplit=1)

    if code not in DIRECTIONS:
        return

    state.direction_code = code
    direction = DIRECTIONS[code]

    group_title = GROUP_TITLES.get(state.group, state.group)
    limit_text = format_limit_text(state.group)
    left = (
        "∞" if DAILY_LIMITS.get(state.group) is None
        else max(DAILY_LIMITS[state.group] - state.used_today, 0)
    )

    text = (
        f"✅ Направление перевода установлено: *{direction['label']}*\n\n"
        + t(lang, "limits_line").format(
            group_title=group_title,
            limit_text=limit_text,
            used=state.used_today,
            left=left,
        )
    )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=make_direction_keyboard(),
    )


async def lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    state = get_user_state(user.id)

    _, code = query.data.split(":", maxsplit=1)
    if code not in INTERFACE_LANGS:
        return

    state.interface_lang = code
    lang_name = INTERFACE_LANGS[code]

    await query.edit_message_text(
        t(code, "setlang_ok").format(lang_name=lang_name),
        parse_mode="Markdown",
    )


# ------------------------ ПЕРЕВОД ТЕКСТА ------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    # пропускаем команды
    if update.message.text and update.message.text.startswith("/"):
        return

    if not await check_and_increase_quota(update, context):
        return

    user_id = update.effective_user.id
    state = get_user_state(user_id)
    lang = state.interface_lang

    direction = DIRECTIONS[state.direction_code]
    target_lang = direction["target_lang"]
    tts_lang = direction["tts_lang"]

    original_text = update.message.text.strip()
    if not original_text:
        return

    try:
        translated = translate_text(original_text, target_lang)
    except Exception as e:
        logger.exception("Ошибка перевода текста: %s", e)
        await update.message.reply_text("Ошибка при переводе, попробуй ещё раз.")
        return

    # текстовый ответ
    await update.message.reply_text(
        f"{t(lang, 'recognized').format(text=original_text)}\n"
        f"{t(lang, 'translated').format(text=translated)}",
        parse_mode="Markdown",
    )

    # голос с переводом
    try:
        ogg_path = tts_to_ogg(translated, tts_lang)
        with open(ogg_path, "rb") as f:
            await update.message.reply_voice(f)
        os.remove(ogg_path)
    except Exception as e:
        logger.exception("Ошибка озвучки текста: %s", e)


# ------------------------ ПЕРЕВОД ГОЛОСОВЫХ ------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.voice:
        return

    if not await check_and_increase_quota(update, context):
        return

    user_id = update.effective_user.id
    state = get_user_state(user_id)
    lang = state.interface_lang

    direction = DIRECTIONS[state.direction_code]
    src_locale = direction["src_locale"]
    target_lang = direction["target_lang"]
    tts_lang = direction["tts_lang"]

    voice = update.message.voice

    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".oga")
    os.close(ogg_fd)
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(wav_fd)

    try:
        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(ogg_path)

        ogg_to_wav(ogg_path, wav_path)

        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        logger.info("Распознаю речь с locale=%s", src_locale)

        try:
            recognized_text = recognizer.recognize_google(audio, language=src_locale)
            logger.info("Распознано: %r", recognized_text)
        except sr.UnknownValueError:
            logger.warning("Не удалось распознать речь")
            await update.message.reply_text(t(lang, "recognize_fail"))
            return

        # Перевод
        try:
            translated = translate_text(recognized_text, target_lang)
            logger.info("Перевод: %r", translated)
        except Exception as e:
            logger.exception("Ошибка перевода голосового: %s", e)
            await update.message.reply_text("Ошибка при переводе, попробуй ещё раз.")
            return

        # Текстовый ответ
        await update.message.reply_text(
            f"{t(lang, 'recognized').format(text=recognized_text)}\n"
            f"{t(lang, 'translated').format(text=translated)}",
            parse_mode="Markdown",
        )

        # Озвучка перевода
        try:
            ogg_tts = tts_to_ogg(translated, tts_lang)
            with open(ogg_tts, "rb") as f:
                await update.message.reply_voice(f)
            os.remove(ogg_tts)
        except Exception as e:
            logger.exception("Ошибка озвучки перевода: %s", e)

    finally:
        for p in (ogg_path, wav_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


# ------------------------ MAIN ------------------------

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pricing", pricing))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("password", cmd_password))
    application.add_handler(CommandHandler("help", help_student))
    application.add_handler(CommandHandler("adminhelp", admin_help))
    application.add_handler(CommandHandler("setlang", setlang))

    # кнопки
    application.add_handler(CallbackQueryHandler(direction_button, pattern=r"^dir:"))
    application.add_handler(CallbackQueryHandler(lang_button, pattern=r"^lang:"))

    # сообщения
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Запускаю webhook на %s, порт %s", BASE_URL, PORT)

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="",          # пустой путь → вебхук = BASE_URL
        webhook_url=BASE_URL,
    )


if __name__ == "__main__":
    main()
