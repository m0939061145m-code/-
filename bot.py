# app.py / bot.py
# Telegram voice translator with groups, limits, passwords and TTS

import asyncio
import datetime
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from deep_translator import GoogleTranslator
from gtts import gTTS
from pydub import AudioSegment
import speech_recognition as sr

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

# ---------------------------------------
# Конфиг
# ---------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://bratik.onrender.com")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")  # если есть – используем
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан в переменных окружения")
    raise SystemExit("BOT_TOKEN env is required")

# Админы (могут видеть /help_admin)
ADMIN_IDS = {7707071177, 1300323894}

# Друг с безлимитом
FRIEND_UNLIMITED_ID = 1300323894

# Лимиты по группам (в день)
GROUP_LIMITS = {
    "free": 30,
    "a1": 30,
    "a2": 60,
    "b1": 100,
    "pro": None,  # None = безлимит
}

# Пароли для групп
PASSWORDS = {
    "karbofos-a1": "a1",
    "karbofos-a2": "a2",
    "karbofos-b1": "b1",
    "karbofos-vip": "pro",
}

# Поддерживаемые языки интерфейса
SUPPORTED_UI_LANGS = ["ru", "en", "de", "tr", "ro", "pl", "ar"]

# ---------------------------------------
# Направления перевода
# ---------------------------------------

@dataclass
class Direction:
    code: str
    src_lang: str
    dest_lang: str
    label: str
    speech_locale: str
    tts_lang: str


DIRECTIONS: Dict[str, Direction] = {
    # RU <-> DE
    "ru_de": Direction("ru_de", "ru", "de", "🇷🇺 RU → 🇩🇪 DE", "ru-RU", "de"),
    "de_ru": Direction("de_ru", "de", "ru", "🇩🇪 DE → 🇷🇺 RU", "de-DE", "ru"),
    # EN <-> DE
    "en_de": Direction("en_de", "en", "de", "🇬🇧 EN → 🇩🇪 DE", "en-US", "de"),
    "de_en": Direction("de_en", "de", "en", "🇩🇪 DE → 🇬🇧 EN", "de-DE", "en"),
    # TR <-> DE
    "tr_de": Direction("tr_de", "tr", "de", "🇹🇷 TR → 🇩🇪 DE", "tr-TR", "de"),
    "de_tr": Direction("de_tr", "de", "tr", "🇩🇪 DE → 🇹🇷 TR", "de-DE", "tr"),
    # RO <-> DE
    "ro_de": Direction("ro_de", "ro", "de", "🇷🇴 RO → 🇩🇪 DE", "ro-RO", "de"),
    "de_ro": Direction("de_ro", "de", "ro", "🇩🇪 DE → 🇷🇴 RO", "de-DE", "ro"),
    # PL <-> DE
    "pl_de": Direction("pl_de", "pl", "de", "🇵🇱 PL → 🇩🇪 DE", "pl-PL", "de"),
    "de_pl": Direction("de_pl", "de", "pl", "🇩🇪 DE → 🇵🇱 PL", "de-DE", "pl"),
    # AR(SY) <-> DE
    "ar_de": Direction("ar_de", "ar", "de", "🇸🇾 AR(SY) → 🇩🇪 DE", "ar-SY", "de"),
    "de_ar": Direction("de_ar", "de", "ar", "🇩🇪 DE → 🇸🇾 AR(SY)", "de-DE", "ar"),
}

# Порядок кнопок на клавиатуре
DIRECTION_ROWS = [
    ("ru_de", "de_ru"),
    ("en_de", "de_en"),
    ("tr_de", "de_tr"),
    ("ro_de", "de_ro"),
    ("pl_de", "de_pl"),
    ("ar_de", "de_ar"),
]

DEFAULT_DIRECTION = "ru_de"
DEFAULT_UI_LANG = "ru"

# ---------------------------------------
# Тексты (RU / EN). Остальные языки интерфейса
# по факту падают на EN как fallback.
# ---------------------------------------

TEXTS = {
    "ru": {
        "start": (
            "Привет! Я голосовой переводчик для курсов немецкого. 👋\n\n"
            "🎙 Отправь мне голосовое — я распознаю речь, переведу и пришлю текст.\n"
            "🔊 Плюс озвучу перевод голосом.\n\n"
            "Текущее направление: {direction_label}\n"
            "Текущая группа: {group_name}\n"
            "Лимит: {limit_text}\n"
            "Сегодня использовано: {used_today}\n"
            "Сегодня осталось: {left_today}\n\n"
            "📚 Выбери направление перевода кнопкой ниже.\n"
            "🔑 Чтобы снять ограничения, введи пароль командой /password\n"
            "ℹ /pricing — тарифы и способы оплаты\n"
            "ℹ /status — твои лимиты\n"
            "ℹ /groupinfo — статус доступа\n"
            "🌐 /lang — язык интерфейса\n"
        ),
        "help": (
            "❓ Как пользоваться ботом:\n\n"
            "1️⃣ Нажми /start.\n"
            "2️⃣ Выбери направление перевода (RU→DE, EN→DE и т.д.).\n"
            "3️⃣ Отправь голосовое или текст.\n"
            "4️⃣ Я распознаю, переведу и озвучу перевод.\n\n"
            "🔑 Чтобы получить больше переводов, используй /password\n"
            "💳 /pricing — тарифы\n"
            "📊 /status — остаток лимита на сегодня\n"
            "👥 /groupinfo — какая у тебя группа (A1, A2, B1, PRO)\n"
        ),
        "help_admin": (
            "🛠 Админ-help\n\n"
            "• /status — посмотреть лимиты любого юзера (реплаем на его сообщение)\n"
            "• Пароли групп:\n"
            "  karbofos-a1 → A1 (30/день)\n"
            "  karbofos-a2 → A2 (60/день)\n"
            "  karbofos-b1 → B1 (100/день)\n"
            "  karbofos-vip → PRO (безлимит)\n\n"
            "Друг 1300323894 — всегда безлимит, независимо от группы."
        ),
        "pricing": (
            "💳 Тарифы и лимиты в день:\n"
            "• Free (DEMO) — 30 переводов/день, 0€\n"
            "• 🎓 A1 — 9.99€ / месяц, 30 переводов/день\n"
            "• 🎓 A2 — 14.99€ / месяц, 60 переводов/день\n"
            "• 🎓 B1 — 19.99€ / месяц, 100 переводов/день\n"
            "• 🥇 PRO (безлимит) — 39.99€ / месяц, безлимит\n\n"
            "🧩 Дополнительно:\n"
            "• Разовый доступ «1 неделя теста» — 4.99€\n\n"
            "Оплата обсуждается с преподавателем "
            "(наличные / банковский перевод / PayPal / Revolut / SEPA и т.п.)."
        ),
        "groupinfo": (
            "👥 Информация по группам:\n\n"
            "• A1 — преподаватель: Юрий Николаевич\n"
            "• A2 — преподаватель: Преподаватель A2\n"
            "• B1 — преподаватель: Преподаватель B1\n"
            "• PRO — индивидуальный доступ (индивидуальные занятия / VIP)\n\n"
            "Чтобы активировать правильный уровень, возьми пароль у своего преподавателя "
            "и введи его через /password."
        ),
        "status": (
            "📊 Твой статус:\n"
            "Группа: {group_name}\n"
            "Лимит в день: {limit_text}\n"
            "Сегодня использовано: {used_today}\n"
            "Сегодня осталось: {left_today}\n"
        ),
        "limit_reached": (
            "⛔ Лимит переводов на сегодня исчерпан.\n"
            "Текущая группа: {group_name}, лимит: {limit_text}\n\n"
            "💳 Посмотреть тарифы: /pricing\n"
            "🔑 Ввести пароль от группы: /password"
        ),
        "password_howto": (
            "🔑 Отправь пароль так:\n"
            "`/password karbofos-a1`\n\n"
            "Доступные примеры:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`."
        ),
        "password_ok": (
            "✅ Группа успешно изменена на: {group_name}\n"
            "Новый лимит: {limit_text}"
        ),
        "password_bad": "❌ Неверный пароль. Проверь у преподавателя.",
        "send_password": "🔑 Введи пароль в формате: `/password karbofos-a1`",
        "direction_set": "✅ Направление перевода установлено: {direction_label}",
        "recognizing": "🎧 Распознаю речь…",
        "nothing_recognized": "😔 Не удалось распознать речь. Попробуй записать ещё раз.",
        "translation_reply": (
            "💬 *Оригинал* ({src}):\n"
            "`{original}`\n\n"
            "🌍 *Перевод* ({dest}):\n"
            "`{translated}`"
        ),
        "text_limit_warn": (
            "ℹ Текстовые сообщения тоже учитываются в лимит переводов."
        ),
        "lang_choose": "🌐 Выбери язык интерфейса:",
        "lang_set": "✅ Язык интерфейса установлен: {lang_code}",
        "only_for_admin": "Эта команда только для администратора.",
    },
    "en": {
        "start": (
            "Hi! I’m a voice translator bot for German courses. 👋\n\n"
            "🎙 Send me a voice message — I’ll recognize speech, translate it and send the text back.\n"
            "🔊 I will also voice the translation.\n\n"
            "Current direction: {direction_label}\n"
            "Current group: {group_name}\n"
            "Daily limit: {limit_text}\n"
            "Used today: {used_today}\n"
            "Left today: {left_today}\n\n"
            "📚 Choose translation direction with the buttons below.\n"
            "🔑 To unlock higher limits, use /password\n"
            "ℹ /pricing — pricing & payment methods\n"
            "ℹ /status — your current limit\n"
            "ℹ /groupinfo — access status\n"
            "🌐 /lang — interface language\n"
        ),
        "help": (
            "❓ How to use the bot:\n\n"
            "1️⃣ Press /start.\n"
            "2️⃣ Choose translation direction (EN→DE, RU→DE, etc.).\n"
            "3️⃣ Send a voice message or text.\n"
            "4️⃣ I will recognize, translate and voice the result.\n\n"
            "🔑 Use /password to upgrade your plan.\n"
            "💳 /pricing — plans\n"
            "📊 /status — today’s remaining limit\n"
            "👥 /groupinfo — your level (A1, A2, B1, PRO)\n"
        ),
        "help_admin": (
            "🛠 Admin help\n\n"
            "• /status — check limits (by replying to a user’s message)\n"
            "• Group passwords:\n"
            "  karbofos-a1 → A1 (30/day)\n"
            "  karbofos-a2 → A2 (60/day)\n"
            "  karbofos-b1 → B1 (100/day)\n"
            "  karbofos-vip → PRO (unlimited)\n\n"
            "Friend 1300323894 has unlimited access regardless of group."
        ),
        "pricing": (
            "💳 Plans and daily limits:\n"
            "• Free (DEMO) — 30 translations/day, 0€\n"
            "• 🎓 A1 — 9.99€ / month, 30 translations/day\n"
            "• 🎓 A2 — 14.99€ / month, 60 translations/day\n"
            "• 🎓 B1 — 19.99€ / month, 100 translations/day\n"
            "• 🥇 PRO (unlimited) — 39.99€ / month, unlimited\n\n"
            "🧩 Additionally:\n"
            "• One-week trial access — 4.99€\n\n"
            "Payment is arranged with your teacher "
            "(cash / bank transfer / PayPal / Revolut / SEPA, etc.)."
        ),
        "groupinfo": (
            "👥 Groups info:\n\n"
            "• A1 — teacher: Yuriy Nikolaevich\n"
            "• A2 — teacher: A2 Teacher\n"
            "• B1 — teacher: B1 Teacher\n"
            "• PRO — individual access (1:1 / VIP)\n\n"
            "To activate the correct level, get a password from your teacher "
            "and enter it via /password."
        ),
        "status": (
            "📊 Your status:\n"
            "Group: {group_name}\n"
            "Daily limit: {limit_text}\n"
            "Used today: {used_today}\n"
            "Left today: {left_today}\n"
        ),
        "limit_reached": (
            "⛔ Your daily translation limit is reached.\n"
            "Current group: {group_name}, limit: {limit_text}\n\n"
            "💳 Check plans: /pricing\n"
            "🔑 Enter group password: /password"
        ),
        "password_howto": (
            "🔑 Send password like this:\n"
            "`/password karbofos-a1`\n\n"
            "Available examples:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`."
        ),
        "password_ok": (
            "✅ Group changed to: {group_name}\n"
            "New limit: {limit_text}"
        ),
        "password_bad": "❌ Wrong password. Ask your teacher.",
        "send_password": "🔑 Send password in format: `/password karbofos-a1`",
        "direction_set": "✅ Translation direction set: {direction_label}",
        "recognizing": "🎧 Recognizing speech…",
        "nothing_recognized": "😔 Couldn’t recognize speech. Please try again.",
        "translation_reply": (
            "💬 *Original* ({src}):\n"
            "`{original}`\n\n"
            "🌍 *Translation* ({dest}):\n"
            "`{translated}`"
        ),
        "text_limit_warn": "ℹ Text messages also count towards your limit.",
        "lang_choose": "🌐 Choose interface language:",
        "lang_set": "✅ Interface language set: {lang_code}",
        "only_for_admin": "This command is for admins only.",
    },
}


def get_text(lang: str, key: str) -> str:
    """Получить текст с учётом fallback."""
    if lang in TEXTS and key in TEXTS[lang]:
        return TEXTS[lang][key]
    # fallback на EN
    if "en" in TEXTS and key in TEXTS["en"]:
        return TEXTS["en"][key]
    # fallback на RU
    return TEXTS["ru"].get(key, "")


# ---------------------------------------
# User state
# ---------------------------------------

@dataclass
class UserState:
    ui_lang: str = DEFAULT_UI_LANG
    direction_code: str = DEFAULT_DIRECTION
    group: str = "free"  # free, a1, a2, b1, pro
    used_today: int = 0
    last_date: datetime.date = field(default_factory=lambda: datetime.date.today())


USER_STATE: Dict[int, UserState] = {}


def get_user_state(user_id: int) -> UserState:
    if user_id not in USER_STATE:
        USER_STATE[user_id] = UserState()
    st = USER_STATE[user_id]

    # если новый день — обнуляем счётчик
    today = datetime.date.today()
    if st.last_date != today:
        st.last_date = today
        st.used_today = 0
    return st


def group_name_rus(group: str) -> str:
    mapping = {
        "free": "Free (DEMO)",
        "a1": "A1",
        "a2": "A2",
        "b1": "B1",
        "pro": "PRO",
    }
    return mapping.get(group, group)


def group_name_en(group: str) -> str:
    mapping = {
        "free": "Free (DEMO)",
        "a1": "A1",
        "a2": "A2",
        "b1": "B1",
        "pro": "PRO",
    }
    return mapping.get(group, group)


def get_limit_for_user(user_id: int, group: str) -> Optional[int]:
    # друг — всегда безлимит
    if user_id == FRIEND_UNLIMITED_ID:
        return None
    return GROUP_LIMITS.get(group, GROUP_LIMITS["free"])


def format_limit_text(limit: Optional[int], lang: str) -> str:
    if limit is None:
        return "безлимит" if lang == "ru" else "unlimited"
    if lang == "ru":
        return f"{limit} переводов/день"
    return f"{limit} translations/day"


def check_and_consume(user_id: int) -> Tuple[bool, int, Optional[int]]:
    """
    Возвращает (ok, used_today, limit).
    ok=False, если лимит превышен.
    """
    st = get_user_state(user_id)
    limit = get_limit_for_user(user_id, st.group)
    if limit is None:
        return True, st.used_today, limit
    if st.used_today >= limit:
        return False, st.used_today, limit
    st.used_today += 1
    return True, st.used_today, limit


def build_direction_keyboard(current_code: str) -> InlineKeyboardMarkup:
    rows = []
    for left_code, right_code in DIRECTION_ROWS:
        row = []
        for code in (left_code, right_code):
            dir_obj = DIRECTIONS[code]
            prefix = "✅ " if code == current_code else ""
            row.append(
                InlineKeyboardButton(
                    text=prefix + dir_obj.label,
                    callback_data=f"dir:{code}",
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_lang_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    rows = []
    for code in SUPPORTED_UI_LANGS:
        prefix = "✅ " if code == current_lang else ""
        flag = {
            "ru": "🇷🇺",
            "en": "🇬🇧",
            "de": "🇩🇪",
            "tr": "🇹🇷",
            "ro": "🇷🇴",
            "pl": "🇵🇱",
            "ar": "🇸🇾",
        }.get(code, "🌐")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{flag} {code.upper()}",
                    callback_data=f"lang:{code}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


# ---------------------------------------
# Перевод и TTS
# ---------------------------------------

def translate_text(text: str, src: str, dest: str) -> str:
    if not text.strip():
        return ""
    if src == "auto":
        translator = GoogleTranslator(source="auto", target=dest)
    else:
        translator = GoogleTranslator(source=src, target=dest)
    return translator.translate(text)


def make_tts_file(text: str, lang: str) -> str:
    tts = gTTS(text=text, lang=lang)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(tmp_fd)
    tts.save(tmp_path)
    return tmp_path


def convert_ogg_to_wav(input_path: str) -> str:
    # pydub + ffmpeg
    audio = AudioSegment.from_file(input_path)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(tmp_fd)
    audio.export(tmp_path, format="wav")
    return tmp_path


# ---------------------------------------
# Handlers
# ---------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st.ui_lang
    dir_obj = DIRECTIONS.get(st.direction_code, DIRECTIONS[DEFAULT_DIRECTION])

    if lang == "ru":
        group_name = group_name_rus(st.group)
    else:
        group_name = group_name_en(st.group)

    limit = get_limit_for_user(user.id, st.group)
    limit_text = format_limit_text(limit, lang)
    left = "∞" if limit is None else max(limit - st.used_today, 0)

    text = get_text(lang, "start").format(
        direction_label=dir_obj.label,
        group_name=group_name,
        limit_text=limit_text,
        used_today=st.used_today,
        left_today=left,
    )

    await update.effective_message.reply_text(
        text,
        reply_markup=build_direction_keyboard(st.direction_code),
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_user_state(update.effective_user.id)
    lang = st.ui_lang
    await update.effective_message.reply_text(get_text(lang, "help"))


async def cmd_help_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = get_user_state(user_id)
    lang = st.ui_lang

    if user_id not in ADMIN_IDS:
        await update.effective_message.reply_text(get_text(lang, "only_for_admin"))
        return

    await update.effective_message.reply_text(
        get_text(lang, "help_admin"), parse_mode="Markdown"
    )


async def cmd_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_user_state(update.effective_user.id)
    lang = st.ui_lang
    await update.effective_message.reply_text(
        get_text(lang, "pricing"), parse_mode="Markdown"
    )


async def cmd_groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_user_state(update.effective_user.id)
    lang = st.ui_lang
    await update.effective_message.reply_text(
        get_text(lang, "groupinfo"), parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st.ui_lang

    if lang == "ru":
        group_name = group_name_rus(st.group)
    else:
        group_name = group_name_en(st.group)

    limit = get_limit_for_user(user.id, st.group)
    limit_text = format_limit_text(limit, lang)
    left = "∞" if limit is None else max(limit - st.used_today, 0)

    text = get_text(lang, "status").format(
        group_name=group_name,
        limit_text=limit_text,
        used_today=st.used_today,
        left_today=left,
    )
    await update.effective_message.reply_text(text)


async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st.ui_lang

    if not context.args:
        await update.effective_message.reply_text(
            get_text(lang, "password_howto"), parse_mode="Markdown"
        )
        return

    pwd = context.args[0].strip()
    group = PASSWORDS.get(pwd)
    if not group:
        await update.effective_message.reply_text(get_text(lang, "password_bad"))
        return

    st.group = group
    limit = get_limit_for_user(user.id, st.group)
    limit_text = format_limit_text(limit, lang)

    if lang == "ru":
        group_name = group_name_rus(st.group)
    else:
        group_name = group_name_en(st.group)

    await update.effective_message.reply_text(
        get_text(lang, "password_ok").format(
            group_name=group_name, limit_text=limit_text
        )
    )


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_user_state(update.effective_user.id)
    lang = st.ui_lang
    await update.effective_message.reply_text(
        get_text(lang, "lang_choose"),
        reply_markup=build_lang_keyboard(lang),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    st = get_user_state(user_id)
    lang = st.ui_lang

    data = query.data or ""
    if data.startswith("dir:"):
        code = data.split(":", 1)[1]
        if code in DIRECTIONS:
            st.direction_code = code
            dir_obj = DIRECTIONS[code]
            await query.edit_message_reply_markup(
                reply_markup=build_direction_keyboard(code)
            )
            await query.message.reply_text(
                get_text(lang, "direction_set").format(
                    direction_label=dir_obj.label
                )
            )
    elif data.startswith("lang:"):
        code = data.split(":", 1)[1]
        if code in SUPPORTED_UI_LANGS:
            st.ui_lang = code
            lang = code
            await query.edit_message_reply_markup(
                reply_markup=build_lang_keyboard(code)
            )
            await query.message.reply_text(
                get_text(lang, "lang_set").format(lang_code=code.upper())
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st.ui_lang
    text = update.effective_message.text or ""

    ok, used, limit = check_and_consume(user.id)
    if not ok:
        limit_text = format_limit_text(limit, lang)
        if lang == "ru":
            group_name = group_name_rus(st.group)
        else:
            group_name = group_name_en(st.group)
        await update.effective_message.reply_text(
            get_text(lang, "limit_reached").format(
                group_name=group_name, limit_text=limit_text
            )
        )
        return

    dir_obj = DIRECTIONS.get(st.direction_code, DIRECTIONS[DEFAULT_DIRECTION])
    translated = translate_text(text, dir_obj.src_lang, dir_obj.dest_lang)

    reply = get_text(lang, "translation_reply").format(
        src=dir_obj.src_lang.upper(),
        dest=dir_obj.dest_lang.upper(),
        original=text,
        translated=translated,
    )

    await update.effective_message.reply_text(reply, parse_mode="Markdown")
    await update.effective_message.reply_text(get_text(lang, "text_limit_warn"))

    # TTS
    try:
        tts_path = make_tts_file(translated, dir_obj.tts_lang)
        with open(tts_path, "rb") as f:
            await update.effective_message.reply_voice(
                voice=InputFile(f),
                caption=f"{dir_obj.dest_lang.upper()} TTS",
            )
    finally:
        if "tts_path" in locals() and os.path.exists(tts_path):
            os.remove(tts_path)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    lang = st.ui_lang

    ok, used, limit = check_and_consume(user.id)
    if not ok:
        limit_text = format_limit_text(limit, lang)
        if lang == "ru":
            group_name = group_name_rus(st.group)
        else:
            group_name = group_name_en(st.group)
        await update.effective_message.reply_text(
            get_text(lang, "limit_reached").format(
                group_name=group_name, limit_text=limit_text
            )
        )
        return

    dir_obj = DIRECTIONS.get(st.direction_code, DIRECTIONS[DEFAULT_DIRECTION])

    msg = update.effective_message
    voice = msg.voice or msg.audio
    if not voice:
        return

    note = await msg.reply_text(get_text(lang, "recognizing"))

    # Скачиваем файл
    with tempfile.TemporaryDirectory() as tmpdir:
        ogg_path = os.path.join(tmpdir, "voice.ogg")
        wav_path = os.path.join(tmpdir, "voice.wav")

        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(ogg_path)

        try:
            wav_path = convert_ogg_to_wav(ogg_path)
        except Exception as e:
            logger.exception("Ошибка при конвертации аудио: %s", e)
            await note.edit_text(get_text(lang, "nothing_recognized"))
            return

        recognizer = sr.Recognizer()
        try:
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            logger.info(
                "Распознаю речь с locale=%s",
                dir_obj.speech_locale,
            )
            text = recognizer.recognize_google(
                audio, language=dir_obj.speech_locale
            )
            logger.info("Распознано: %r", text)
        except sr.UnknownValueError:
            logger.warning("Не удалось распознать речь")
            await note.edit_text(get_text(lang, "nothing_recognized"))
            return
        except Exception as e:
            logger.exception("Ошибка распознавания: %s", e)
            await note.edit_text(get_text(lang, "nothing_recognized"))
            return

    # Перевод
    translated = translate_text(text, dir_obj.src_lang, dir_obj.dest_lang)
    await note.delete()

    reply = get_text(lang, "translation_reply").format(
        src=dir_obj.src_lang.upper(),
        dest=dir_obj.dest_lang.upper(),
        original=text,
        translated=translated,
    )
    await msg.reply_text(reply, parse_mode="Markdown")

    # TTS
    try:
        tts_path = make_tts_file(translated, dir_obj.tts_lang)
        with open(tts_path, "rb") as f:
            await msg.reply_voice(
                voice=InputFile(f),
                caption=f"{dir_obj.dest_lang.upper()} TTS",
            )
    finally:
        if "tts_path" in locals() and os.path.exists(tts_path):
            os.remove(tts_path)


# ---------------------------------------
# main / webhook
# ---------------------------------------

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("help_admin", cmd_help_admin))
    application.add_handler(CommandHandler("pricing", cmd_pricing))
    application.add_handler(CommandHandler("groupinfo", cmd_groupinfo))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("password", cmd_password))
    application.add_handler(CommandHandler("lang", cmd_lang))

    application.add_handler(CallbackQueryHandler(on_callback))

    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # Webhook config for Render
    webhook_url = BASE_WEBHOOK_URL or f"{BASE_URL.rstrip('/')}/webhook"
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


if __name__ == "__main__":
    main()
