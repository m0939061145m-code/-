import os
import io
import asyncio
import logging
from datetime import datetime, date

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from deep_translator import GoogleTranslator
import speech_recognition as sr
from pydub import AudioSegment
from gtts import gTTS

import httpx

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("BASE_URL", "https://bratik.onrender.com")
PORT = int(os.environ.get("PORT", "10000"))

# --- Состояние пользователей в памяти ---------------------------------------

# user_id -> dict
USER_STATE: dict[int, dict] = {}

# id друга с безлимитом
FRIEND_ID = 1300323894

# --- Тарифы и лимиты ---------------------------------------------------------

TIER_LIMITS_PER_DAY = {
    "demo": 15,     # бесплатный демо
    "a1": 30,
    "a2": 60,
    "b1": 100,
    "vip": 10_000,  # фактически безлимит
    "testweek": 50, # примерный лимит на тестовую неделю
}

TIER_NAMES = {
    "demo": "Free (DEMO)",
    "a1": "A1",
    "a2": "A2",
    "b1": "B1",
    "vip": "PRO",
    "testweek": "Test week",
}

# Пароли (их не показываем пользователю)
PASSWORDS = {
    "karbofos-a1": "a1",
    "karbofos-a2": "a2",
    "karbofos-b1": "b1",
    "karbofos-vip": "vip",
    "karbofos-test": "testweek",
}

# --- Языки перевода ----------------------------------------------------------

# пары: код → locale для распознавания речи
LANG_LOCALES = {
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "pl": "pl-PL",
    "ar": "ar-SY",
}

# какие пары перевода доступны
DIRECTIONS = {
    "ru_de": ("ru", "de"),
    "de_ru": ("de", "ru"),
    "en_de": ("en", "de"),
    "de_en": ("de", "en"),
    "tr_de": ("tr", "de"),
    "de_tr": ("de", "tr"),
    "ro_de": ("ro", "de"),
    "de_ro": ("de", "ro"),
    "pl_de": ("pl", "de"),
    "de_pl": ("de", "pl"),
    "ar_de": ("ar", "de"),
    "de_ar": ("de", "ar"),
}

# подписи для кнопок направлений
DIRECTION_LABELS = {
    "ru_de": "🇷🇺 RU → 🇩🇪 DE",
    "de_ru": "🇩🇪 DE → 🇷🇺 RU",
    "en_de": "🇬🇧 EN → 🇩🇪 DE",
    "de_en": "🇩🇪 DE → 🇬🇧 EN",
    "tr_de": "🇹🇷 TR → 🇩🇪 DE",
    "de_tr": "🇩🇪 DE → 🇹🇷 TR",
    "ro_de": "🇷🇴 RO → 🇩🇪 DE",
    "de_ro": "🇩🇪 DE → 🇷🇴 RO",
    "pl_de": "🇵🇱 PL → 🇩🇪 DE",
    "de_pl": "🇩🇪 DE → 🇵🇱 PL",
    "ar_de": "🇸🇾 AR(SY) → 🇩🇪 DE",
    "de_ar": "🇩🇪 DE → 🇸🇾 AR(SY)",
}

# --- Язык интерфейса ---------------------------------------------------------

# ui_lang: 'ru', 'de', 'en', 'tr', 'ro', 'pl', 'ar'
SUPPORTED_UI_LANGS = ["ru", "de", "en", "tr", "ro", "pl", "ar"]

# простые тексты для интерфейса
TEXTS = {
    "ru": {
        "start_title": "Привет! Я голосовой переводчик для курсов немецкого.",
        "start_howto": "Отправь мне голосовое сообщение — я распознаю речь, переведу и озвучу ответ.",
        "start_dir": "Текущее направление перевода:",
        "start_group": "Текущая группа",
        "start_limit": "Лимит на сегодня",
        "start_used": "Сегодня использовано",
        "start_left": "Осталось переводов",
        "start_password": "🔑 Чтобы снять ограничения, введи пароль командой /password",
        "start_lang_hint": "🌐 Язык интерфейса можно сменить командой /lang",
        "lang_choose": "Выбери язык интерфейса:",
        "lang_set": "Язык интерфейса переключен на: {lang}",
        "unknown_lang": "Этот язык интерфейса пока не поддерживается.",
        "direction_set": "Направление перевода установлено: {label}",
        "direction_same": "Это направление уже выбрано 👍",
        "send_password_usage": "🔑 Отправь пароль вот так:\n\n`/password ТВОЙ_КОД`\n\nКод выдает только преподаватель.",
        "password_ok": "Пароль принят ✅\nТвой уровень: {tier_name}\nЛимит в день: {limit}",
        "password_bad": "Пароль не подошёл 😔\nПроверь код или обратись к преподавателю.",
        "status_title": "Статус:",
        "status_tier": "Группа: {tier}\nЛимит в день: {limit}\nСегодня использовано: {used}\nОсталось: {left}",
        "no_limit": "Группа: {tier}\nЛимит: без ограничений\nСегодня использовано: {used}",
        "limit_reached": "На сегодня лимит переводов исчерпан для твоего тарифа. Обратись к преподавателю за повышением уровня.",
        "pricing": (
            "📊 *Тарифы и лимиты в день:*\n"
            "• Free (DEMO) — 15 переводов\n"
            "• A1 — 30 переводов\n"
            "• A2 — 60 переводов\n"
            "• B1 — 100 переводов\n"
            "• PRO — безлимит\n\n"
            "💳 *Стоимость (в евро / месяц):*\n"
            "🎓 A1 — 9.99€\n"
            "🎓 A2 — 14.99€\n"
            "🎓 B1 — 19.99€\n"
            "🥇 PRO (безлимит) — 39.99€\n\n"
            "🧩 Дополнительно:\n"
            "🔹 Разовый доступ «1 неделя теста» — 4.99€\n\n"
            "Оплата обсуждается с преподавателем (наличные / перевод / PayPal / Revolut / SEPA и т.п.)."
        ),
        "groupinfo": (
            "👥 *Информация по группам:*\n"
            "• A1 — преподаватель: Юрий Николаевич\n"
            "• A2 — преподаватель: преподаватель A2\n"
            "• B1 — преподаватель: преподаватель B1\n"
            "• PRO — индивидуальный доступ\n\n"
            "Чтобы активировать уровень, возьми пароль у своего преподавателя и введи его через /password."
        ),
        "help_students": (
            "📚 *Как пользоваться ботом:*\n"
            "1. Нажми /start — выбери направление перевода.\n"
            "2. Отправь голосовое сообщение или текст.\n"
            "3. Я распознаю, переведу и озвучу ответ.\n\n"
            "🔑 Чтобы убрать демо-ограничение, возьми у преподавателя пароль и введи его командой:\n"
            "`/password ТВОЙ_КОД`"
        ),
        "help_admin": (
            "🛠 *Админ-help*\n\n"
            "Пока все админ-действия делаются вручную через пароли уровней.\n"
            "Планы: добавить отдельные команды для сброса лимитов и выдачи уровней.\n"
        ),
        "speech_fail": "Не удалось распознать речь. Попробуй ещё раз, говори ближе к микрофону.",
        "translating": "Перевожу...",
        "original": "Оригинал",
        "translation": "Перевод",
    },
    "en": {
        "start_title": "Hi! I’m a voice translator for German courses.",
        "start_howto": "Send me a voice message — I’ll recognize, translate and reply with voice.",
        "start_dir": "Current translation direction:",
        "start_group": "Current group",
        "start_limit": "Daily limit",
        "start_used": "Used today",
        "start_left": "Left today",
        "start_password": "🔑 To unlock full access, use /password and enter your code from teacher.",
        "start_lang_hint": "🌐 You can change interface language with /lang",
        "lang_choose": "Choose interface language:",
        "lang_set": "Interface language set to: {lang}",
        "unknown_lang": "This language is not supported yet.",
        "direction_set": "Translation direction set to: {label}",
        "direction_same": "This direction is already selected 👍",
        "send_password_usage": "🔑 Send your password like this:\n\n`/password YOUR_CODE`\n\nYou receive the code only from your teacher.",
        "password_ok": "Password accepted ✅\nYour level: {tier_name}\nDaily limit: {limit}",
        "password_bad": "Password is not valid. Please check it with your teacher.",
        "status_title": "Status:",
        "status_tier": "Group: {tier}\nDaily limit: {limit}\nUsed today: {used}\nLeft: {left}",
        "no_limit": "Group: {tier}\nLimit: unlimited\nUsed today: {used}",
        "limit_reached": "Your daily translation limit is reached. Ask your teacher to upgrade your level.",
        "pricing": (
            "📊 *Plans & daily limits:*\n"
            "• Free (DEMO) — 15 translations\n"
            "• A1 — 30 translations\n"
            "• A2 — 60 translations\n"
            "• B1 — 100 translations\n"
            "• PRO — unlimited\n\n"
            "💳 *Prices (EUR / month):*\n"
            "🎓 A1 — 9.99€\n"
            "🎓 A2 — 14.99€\n"
            "🎓 B1 — 19.99€\n"
            "🥇 PRO (unlimited) — 39.99€\n\n"
            "🧩 Extra:\n"
            "🔹 One-week test access — 4.99€\n\n"
            "Payment is discussed with your teacher (cash / bank transfer / PayPal / Revolut / SEPA, etc.)."
        ),
        "groupinfo": (
            "👥 *Groups:*\n"
            "• A1 — teacher: Yuriy Nikolaevich\n"
            "• A2 — teacher: A2 teacher\n"
            "• B1 — teacher: B1 teacher\n"
            "• PRO — individual access\n\n"
            "To activate level, get a password from your teacher and send it via /password."
        ),
        "help_students": (
            "📚 *How to use the bot:*\n"
            "1. Tap /start — pick translation direction.\n"
            "2. Send a voice message or text.\n"
            "3. I’ll recognize, translate and answer with voice.\n\n"
            "🔑 To remove demo limit, ask your teacher for a password and send:\n"
            "`/password YOUR_CODE`"
        ),
        "help_admin": (
            "🛠 *Admin help*\n\n"
            "For now, access levels are controlled via passwords only.\n"
            "More admin commands may be added later."
        ),
        "speech_fail": "Couldn’t recognize speech. Please try again.",
        "translating": "Translating...",
        "original": "Original",
        "translation": "Translation",
    },
}

# Для остальных языков интерфейса просто используем английский как fallback
for l in ["de", "tr", "ro", "pl", "ar"]:
    if l not in TEXTS:
        TEXTS[l] = TEXTS["en"]


def get_user_state(user_id: int) -> dict:
    st = USER_STATE.get(user_id)
    if not st:
        st = {
            "tier": "demo",
            "used_today": 0,
            "date": date.today().isoformat(),
            "direction": "ru_de",
            "ui_lang": "ru",
        }
        # другу сразу даём vip
        if user_id == FRIEND_ID:
            st["tier"] = "vip"
        USER_STATE[user_id] = st
    # обнуление по дате
    today = date.today().isoformat()
    if st["date"] != today:
        st["date"] = today
        st["used_today"] = 0
    return st


def t(user_id: int, key: str, **kwargs) -> str:
    st = get_user_state(user_id)
    lang = st.get("ui_lang", "ru")
    base = TEXTS.get(lang, TEXTS["en"])
    txt = base.get(key, TEXTS["en"].get(key, key))
    if kwargs:
        txt = txt.format(**kwargs)
    return txt


def make_direction_keyboard(current: str) -> InlineKeyboardMarkup:
    # 6 рядов по 2 кнопки
    rows = [
        ["ru_de", "de_ru"],
        ["en_de", "de_en"],
        ["tr_de", "de_tr"],
        ["ro_de", "de_ro"],
        ["pl_de", "de_pl"],
        ["ar_de", "de_ar"],
    ]
    keyboard = []
    for a, b in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    ("✅ " if current == a else "") + DIRECTION_LABELS[a],
                    callback_data=f"dir:{a}",
                ),
                InlineKeyboardButton(
                    ("✅ " if current == b else "") + DIRECTION_LABELS[b],
                    callback_data=f"dir:{b}",
                ),
            ]
        )
    return InlineKeyboardMarkup(keyboard)


def make_lang_keyboard(current: str) -> InlineKeyboardMarkup:
    labels = {
        "ru": "🇷🇺 Русский",
        "de": "🇩🇪 Deutsch",
        "en": "🇬🇧 English",
        "tr": "🇹🇷 Türkçe",
        "ro": "🇷🇴 Română",
        "pl": "🇵🇱 Polski",
        "ar": "🇸🇾 عربي",
    }
    keyboard = [
        [
            InlineKeyboardButton(
                ("✅ " if current == code else "") + labels[code],
                callback_data=f"lang:{code}",
            )
        ]
        for code in SUPPORTED_UI_LANGS
    ]
    return InlineKeyboardMarkup(keyboard)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)

    src, dst = DIRECTIONS[st["direction"]]
    dir_label = DIRECTION_LABELS[st["direction"]]

    tier = st["tier"]
    daily = TIER_LIMITS_PER_DAY.get(tier)
    used = st["used_today"]

    if daily:
        left = max(daily - used, 0)
        limit_line = (
            f"{t(user.id,'start_limit')}: {daily}\n"
            f"{t(user.id,'start_used')}: {used}\n"
            f"{t(user.id,'start_left')}: {left}"
        )
    else:
        limit_line = t(user.id, "no_limit", tier=TIER_NAMES.get(tier, tier), used=used)

    text = (
        f"{t(user.id,'start_title')}\n\n"
        f"🎧 {t(user.id,'start_howto')}\n\n"
        f"{t(user.id,'start_dir')} {dir_label}\n\n"
        f"{t(user.id,'start_group')}: {TIER_NAMES.get(tier, tier)}\n"
        f"{limit_line}\n\n"
        f"{t(user.id,'start_password')}\n"
        f"{t(user.id,'start_lang_hint')}"
    )

    await update.effective_message.reply_text(
        text,
        reply_markup=make_direction_keyboard(st["direction"]),
    )


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    await update.effective_message.reply_text(
        t(user.id, "lang_choose"),
        reply_markup=make_lang_keyboard(st["ui_lang"]),
    )


async def cmd_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)
    args = context.args

    if not args:
        await update.effective_message.reply_text(
            t(user.id, "send_password_usage"), parse_mode="Markdown"
        )
        return

    code = args[0].strip()
    tier = PASSWORDS.get(code)
    if not tier:
        await update.effective_message.reply_text(t(user.id, "password_bad"))
        return

    st["tier"] = tier
    st["used_today"] = 0
    USER_STATE[user.id] = st

    limit = TIER_LIMITS_PER_DAY.get(tier)
    if limit:
        msg = t(
            user.id,
            "password_ok",
            tier_name=TIER_NAMES.get(tier, tier),
            limit=limit,
        )
    else:
        msg = t(
            user.id,
            "password_ok",
            tier_name=TIER_NAMES.get(tier, tier),
            limit="∞",
        )
    await update.effective_message.reply_text(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)

    tier = st["tier"]
    used = st["used_today"]
    daily = TIER_LIMITS_PER_DAY.get(tier)

    if daily:
        left = max(daily - used, 0)
        msg = t(
            user.id,
            "status_tier",
            tier=TIER_NAMES.get(tier, tier),
            limit=daily,
            used=used,
            left=left,
        )
    else:
        msg = t(
            user.id,
            "no_limit",
            tier=TIER_NAMES.get(tier, tier),
            used=used,
        )

    await update.effective_message.reply_text(f"{t(user.id,'status_title')}\n\n{msg}")


async def cmd_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(
        t(user.id, "pricing"),
        parse_mode="Markdown",
    )


async def cmd_groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(
        t(user.id, "groupinfo"),
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(
        t(user.id, "help_students"),
        parse_mode="Markdown",
    )


async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(
        t(user.id, "help_admin"),
        parse_mode="Markdown",
    )


# --- Callback-кнопки ---------------------------------------------------------


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    await query.answer()
    data = query.data

    st = get_user_state(user.id)

    if data.startswith("dir:"):
        new_dir = data.split(":", 1)[1]
        if new_dir not in DIRECTIONS:
            return
        if st["direction"] == new_dir:
            await query.answer(t(user.id, "direction_same"), show_alert=False)
            return
        st["direction"] = new_dir
        USER_STATE[user.id] = st
        label = DIRECTION_LABELS[new_dir]
        try:
            await query.edit_message_text(
                t(user.id, "start_title")
                + "\n\n"
                + t(user.id, "direction_set", label=label),
                reply_markup=make_direction_keyboard(new_dir),
            )
        except Exception as e:
            logger.warning("edit_message_text failed: %s", e)
        return

    if data.startswith("lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang not in SUPPORTED_UI_LANGS:
            await query.answer(t(user.id, "unknown_lang"), show_alert=True)
            return
        st["ui_lang"] = new_lang
        USER_STATE[user.id] = st
        await query.edit_message_text(
            t(user.id, "lang_set", lang=new_lang.upper()),
            reply_markup=make_lang_keyboard(new_lang),
        )
        return


# --- Перевод сообщений -------------------------------------------------------


def increment_and_check_limit(user_id: int) -> bool:
    """True если можно делать перевод, False если лимит кончился."""
    st = get_user_state(user_id)
    if st["tier"] == "vip" or user_id == FRIEND_ID:
        return True

    tier = st["tier"]
    daily = TIER_LIMITS_PER_DAY.get(tier)
    if not daily:
        return True

    if st["used_today"] >= daily:
        return False

    st["used_today"] += 1
    USER_STATE[user_id] = st
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)

    if not increment_and_check_limit(user.id):
        await update.effective_message.reply_text(t(user.id, "limit_reached"))
        return

    src, dst = DIRECTIONS[st["direction"]]
    text = update.effective_message.text

    logger.info(
        "Translating text for %s: %s (%s→%s)", user.id, text, src, dst
    )

    await update.effective_message.chat.send_action("typing")

    try:
        translated = GoogleTranslator(source=src, target=dst).translate(text)
    except Exception as e:
        logger.exception("translate error: %s", e)
        await update.effective_message.reply_text("Ошибка перевода.")
        return

    # озвучка
    try:
        tts = gTTS(translated, lang=dst)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        await update.effective_message.reply_voice(
            voice=buf,
            caption=f"🗣 *{t(user.id,'original')}:*\n{text}\n\n"
                    f"✅ *{t(user.id,'translation')}:*\n{translated}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("TTS error: %s", e)
        await update.effective_message.reply_text(translated)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    st = get_user_state(user.id)

    if not increment_and_check_limit(user.id):
        await update.effective_message.reply_text(t(user.id, "limit_reached"))
        return

    src, dst = DIRECTIONS[st["direction"]]
    locale = LANG_LOCALES.get(src, "ru-RU")

    logger.info("Got voice from %s, locale=%s", user.id, locale)

    file = await context.bot.get_file(update.message.voice.file_id)
    ogg_bytes = await file.download_as_bytearray()

    # конвертация ogg -> wav
    audio = AudioSegment.from_file(io.BytesIO(ogg_bytes), format="ogg")
    wav_buf = io.BytesIO()
    audio.export(wav_buf, format="wav")
    wav_buf.seek(0)

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_buf) as source:
        audio_data = recognizer.record(source)

    try:
        logger.info("Recognizing with locale=%s", locale)
        text = recognizer.recognize_google(audio_data, language=locale)
        logger.info("Recognized: %r", text)
    except Exception:
        logger.warning("Speech recognition failed", exc_info=True)
        await update.effective_message.reply_text(t(user.id, "speech_fail"))
        return

    if not text:
        await update.effective_message.reply_text(t(user.id, "speech_fail"))
        return

    await update.effective_message.chat.send_action("record_voice")

    try:
        translated = GoogleTranslator(source=src, target=dst).translate(text)
    except Exception as e:
        logger.exception("translate error: %s", e)
        await update.effective_message.reply_text("Ошибка перевода.")
        return

    # озвучка
    try:
        tts = gTTS(translated, lang=dst)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)

        await update.effective_message.reply_voice(
            voice=buf,
            caption=f"🗣 *{t(user.id,'original')}:*\n{text}\n\n"
                    f"✅ *{t(user.id,'translation')}:*\n{translated}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("TTS error: %s", e)
        await update.effective_message.reply_text(translated)


# --- main --------------------------------------------------------------------


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env is not set")

    logger.info(
        "Запускаю webhook на %s, порт %s",
        f"{BASE_URL}",
        PORT,
    )

    # небольшая пауза чтобы Render успел открыть порт
    await asyncio.sleep(1.0)

    async with httpx.AsyncClient(timeout=10) as client:
        # просто проверка токена
        r = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        )
        r.raise_for_status()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CommandHandler("password", cmd_password))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pricing", cmd_pricing))
    application.add_handler(CommandHandler("groupinfo", cmd_groupinfo))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("adminhelp", cmd_adminhelp))

    application.add_handler(CallbackQueryHandler(on_callback))

    # текст (кроме команд)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    # голос
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{BASE_URL}/webhook",
    )


if __name__ == "__main__":
    asyncio.run(main())
