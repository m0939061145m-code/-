import asyncio
import json
import logging
import os
import secrets
import string
from datetime import date
from pathlib import Path
import tempfile

import speech_recognition as sr
from deep_translator import GoogleTranslator
from pydub import AudioSegment

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# --------------------------------------------------
# ЛОГИ
# --------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# НАСТРОЙКИ ДОСТУПА И ЛИМИТОВ
# --------------------------------------------------

DATA_FILE = Path("users_data.json")

# Лимиты по группам
GROUP_LIMITS = {
    "DEMO": 10,
    "A1": 40,
    "A2": 60,
    "B1": 100,
    "UNLIMITED": 0,  # 0 = безлимит
}

# Статические пароли → группа
PASSWORD_GROUPS = {
    "karbofosA1": "A1",
    "karbofosA2": "A2",
    "karbofosB1": "B1",
    "karbofosTEACHER": "UNLIMITED",
}

# Друг в Германии — всегда безлимит
UNLIMITED_USER_IDS = {
    1300323894,
}

# Препод/админ, кто видит статистику и может генерить промокоды
ADMIN_IDS = {
    1300323894,
}

# Тарифы для /pay и Telegram Payments
TARIFFS = {
    "A1": {
        "title": "Доступ A1 (40 переводов/день)",
        "description": "Месяц доступа к боту-переводчику, уровень A1.",
        "payload": "tariff_A1",
        "price_eur": 15,
        "group": "A1",
    },
    "A2": {
        "title": "Доступ A2 (60 переводов/день)",
        "description": "Месяц доступа к боту-переводчику, уровень A2.",
        "payload": "tariff_A2",
        "price_eur": 20,
        "group": "A2",
    },
    "B1": {
        "title": "Доступ B1 (100 переводов/день)",
        "description": "Месяц доступа к боту-переводчику, уровень B1.",
        "payload": "tariff_B1",
        "price_eur": 25,
        "group": "B1",
    },
    "UNLIMITED": {
        "title": "UNLIMITED (безлимит)",
        "description": "Полный безлимит переводов на месяц.",
        "payload": "tariff_UNLIMITED",
        "price_eur": 49,
        "group": "UNLIMITED",
    },
}

# --------------------------------------------------
# ЯЗЫКИ И НАПРАВЛЕНИЯ
# --------------------------------------------------

LANG_INFO = {
    "ru": {"name": "Русский", "flag": "🇷🇺", "locale": "ru-RU"},
    "de": {"name": "Немецкий", "flag": "🇩🇪", "locale": "de-DE"},
    "en": {"name": "Английский", "flag": "🇬🇧", "locale": "en-US"},
    "tr": {"name": "Турецкий", "flag": "🇹🇷", "locale": "tr-TR"},
    "ro": {"name": "Румынский", "flag": "🇷🇴", "locale": "ro-RO"},
    "ar": {"name": "Сирийский (араб.)", "flag": "🇸🇾", "locale": "ar-SY"},
    "pl": {"name": "Польский", "flag": "🇵🇱", "locale": "pl-PL"},
}

DIRECTIONS = {
    "ru_de": ("ru", "de"),
    "de_ru": ("de", "ru"),
    "ru_en": ("ru", "en"),
    "en_ru": ("en", "ru"),
    "ru_tr": ("ru", "tr"),
    "tr_ru": ("tr", "ru"),
    "ru_ro": ("ru", "ro"),
    "ro_ru": ("ro", "ru"),
    "ru_ar": ("ru", "ar"),
    "ar_ru": ("ar", "ru"),
    "ru_pl": ("ru", "pl"),
    "pl_ru": ("pl", "ru"),
}

# --------------------------------------------------
# ХРАНЕНИЕ ДАННЫХ
# --------------------------------------------------

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("users", {})
    data.setdefault("promo_codes", {})  # код -> группа
    return data


def save_data(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DATA = load_data()


def get_today_str() -> str:
    return date.today().isoformat()


def get_user_record(user_id: int) -> dict:
    users = DATA.setdefault("users", {})
    rec = users.get(str(user_id))
    if rec is None:
        rec = {
            "group": "DEMO",
            "date": get_today_str(),
            "used_today": 0,
            "total_used": 0,
        }
        users[str(user_id)] = rec
        save_data(DATA)
    else:
        # Обновляем день + инициализируем total_used
        if rec.get("date") != get_today_str():
            rec["date"] = get_today_str()
            rec["used_today"] = 0
        rec.setdefault("total_used", 0)
        save_data(DATA)
    return rec


def set_user_group(user_id: int, group: str) -> None:
    rec = get_user_record(user_id)
    rec["group"] = group
    save_data(DATA)
    logger.info("User %s moved to group %s", user_id, group)


def can_use_translation(user_id: int):
    rec = get_user_record(user_id)
    group = rec["group"]

    # Жёсткий безлимит по ID
    if user_id in UNLIMITED_USER_IDS:
        return True, rec["used_today"], 0, "UNLIMITED"

    limit = GROUP_LIMITS[group]

    if limit == 0:
        return True, rec["used_today"], 0, group

    if rec["used_today"] >= limit:
        return False, rec["used_today"], limit, group

    return True, rec["used_today"], limit, group


def increment_usage(user_id: int):
    rec = get_user_record(user_id)
    rec["used_today"] += 1
    rec["total_used"] = rec.get("total_used", 0) + 1
    save_data(DATA)


# --------------------------------------------------
# ПРОМОКОДЫ
# --------------------------------------------------

def generate_promo_codes(group: str, count: int) -> list[str]:
    """Генерация одноразовых промокодов для группы."""
    codes = []
    for _ in range(count):
        suffix = "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8)
        )
        code = f"{group}-{suffix}"
        DATA["promo_codes"][code] = group
        codes.append(code)
    save_data(DATA)
    return codes


# --------------------------------------------------
# КНОПКИ
# --------------------------------------------------

def get_direction_label(direction_key: str) -> str:
    src, dest = DIRECTIONS[direction_key]
    return f"{LANG_INFO[src]['flag']} {src.upper()} → {LANG_INFO[dest]['flag']} {dest.upper()}"


def make_direction_keyboard(current_direction: str):
    pairs = [
        ("ru_de", "de_ru"),
        ("ru_en", "en_ru"),
        ("ru_tr", "tr_ru"),
        ("ru_ro", "ro_ru"),
        ("ru_ar", "ar_ru"),
        ("ru_pl", "pl_ru"),
    ]

    keyboard = []
    for left, right in pairs:
        row = []
        for key in (left, right):
            label = get_direction_label(key)
            if key == current_direction:
                label = "✅ " + label
            row.append(InlineKeyboardButton(label, callback_data=f"dir:{key}"))
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def commands_keyboard():
    return ReplyKeyboardMarkup(
        [["/status", "/pricing"], ["/groupinfo", "/help"]],
        resize_keyboard=True,
    )


# --------------------------------------------------
# ПЕРЕВОД И РАСПОЗНАВАНИЕ
# --------------------------------------------------

async def translate_text(text: str, src: str, dest: str) -> str:
    return GoogleTranslator(source=src, target=dest).translate(text)


def recognize_speech(oga_path: Path, locale: str):
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    wav_path = Path(wav_path)

    try:
        AudioSegment.from_file(oga_path, format="ogg").export(wav_path, format="wav")
        recog = sr.Recognizer()
        with sr.AudioFile(str(wav_path)) as src_audio:
            audio = recog.record(src_audio)
        text = recog.recognize_google(audio, language=locale)
        return text
    except sr.UnknownValueError:
        return None
    except Exception as e:
        logger.exception("Speech recognition failed: %s", e)
        return None
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass


def get_current_direction(context: ContextTypes.DEFAULT_TYPE) -> str:
    d = context.user_data.get("direction", "ru_de")
    context.user_data["direction"] = d
    return d


# --------------------------------------------------
# КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rec = get_user_record(user_id)
    direction = get_current_direction(context)

    can_use, used, limit, group = can_use_translation(user_id)

    if limit == 0:
        limit_text = "У тебя БЕЗЛИМИТ 🚀"
    else:
        limit_text = f"{limit} переводов в день. Осталось сегодня: {max(limit - used, 0)}"

    text = (
        "👋 Привет! Я бот-переводчик голосовых сообщений.\n\n"
        "🎧 Отправь мне голосовое — я распознаю текст и переведу его.\n\n"
        f"Текущее направление перевода: {get_direction_label(direction)}\n"
        "Поменять язык можно кнопкой ниже.\n\n"
        f"✏️ Твой доступ: *{group}*\n"
        f"Лимит: {limit_text}\n\n"
        "Чтобы получить полный доступ, используй `/password ПАРОЛЬ` от преподавателя.\n\n"
        "Команда /pricing — тарифы и способы оплаты.\n"
        "Команда /help — краткая помощь."
    )

    await update.message.reply_text(
        text,
        reply_markup=make_direction_keyboard(direction),
        parse_mode="Markdown",
    )
    await update.message.reply_text("Команды снизу 👇", reply_markup=commands_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *Помощь по боту*\n\n"
        "1️⃣ Отправь голосовое — я распознаю и переведу.\n"
        "2️⃣ Можно отправлять обычный текст — тоже переведу.\n\n"
        "*Основные команды:*\n"
        "/start — перезапустить бота\n"
        "/help — это сообщение\n"
        "/status — твой лимит и группа\n"
        "/pricing — тарифы и способы оплаты\n"
        "/groupinfo — описание групп доступа\n"
        "/password ПАРОЛЬ — ввести пароль от преподавателя или промокод\n"
        "/pay — оплата через Telegram (если настроено)\n\n"
        "/stats — общая статистика (только для преподавателя)\n"
        "/user — информация по конкретному ученику (только для преподавателя)\n"
        "/genpromo — сгенерировать промокоды (только для преподавателя)\n\n"
        "Направление перевода выбирается кнопками под сообщением."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💰 *Тарифы и оплата*\n\n"
        "• A1 — 40 переводов/день — *15 € / месяц*\n"
        "• A2 — 60 переводов/день — *20 € / месяц*\n"
        "• B1 — 100 переводов/день — *25 € / месяц*\n"
        "• UNLIMITED — безлимит — *49 € / месяц*\n\n"
        "*Способы оплаты:*\n"
        "• Оплата прямо в боте через Telegram (если доступна) — команда /pay\n"
        "• PayPal / SEPA (IBAN) / Revolut / Wise\n"
        "• Наличными преподавателю\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Уровни доступа:*\n\n"
        "• DEMO — 10 переводов/день (для пробного доступа)\n"
        "• A1 — 40 переводов/день\n"
        "• A2 — 60 переводов/день\n"
        "• B1 — 100 переводов/день\n"
        "• UNLIMITED — без ограничений\n\n"
        "Повышение доступа — по паролю от преподавателя, промокоду или через оплату.",
        parse_mode="Markdown",
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    can_use, used, limit, group = can_use_translation(user_id)
    remaining = "∞" if limit == 0 else max(limit - used, 0)

    await update.message.reply_text(
        f"📈 *Твой статус:*\n\n"
        f"Группа: *{group}*\n"
        f"Лимит: {'Безлимит' if limit == 0 else f'{limit}/день'}\n"
        f"Использовано сегодня: {used}\n"
        f"Осталось сегодня: {remaining}",
        parse_mode="Markdown",
    )


async def password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Использование: `/password ПАРОЛЬ`",
            parse_mode="Markdown",
        )
        return

    code = context.args[0].strip()

    # 1) статические пароли
    group = PASSWORD_GROUPS.get(code)

    # 2) промокоды из файла
    if not group:
        promo_group = DATA["promo_codes"].get(code)
        if promo_group:
            group = promo_group
            # одноразовый промокод — удаляем после активации
            del DATA["promo_codes"][code]
            save_data(DATA)

    if not group:
        await update.message.reply_text("❌ Неверный пароль или промокод. Проверь у преподавателя.")
        return

    set_user_group(user_id, group)

    await update.message.reply_text(
        f"✅ Доступ обновлён.\nТвоя новая группа: *{group}*",
        parse_mode="Markdown",
    )


# --------------------------------------------------
# АДМИН-КОМАНДЫ
# --------------------------------------------------

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для преподавателя.")
        return

    today = get_today_str()
    users = DATA.get("users", {})
    total_today = 0
    total_all = 0
    groups_count = {}

    for uid_str, _ in users.items():
        uid = int(uid_str)
        rec = get_user_record(uid)
        group = rec["group"]
        used_today = rec.get("used_today", 0)
        total = rec.get("total_used", 0)

        total_today += used_today
        total_all += total
        groups_count[group] = groups_count.get(group, 0) + 1

    promo_count = len(DATA.get("promo_codes", {}))

    lines = [
        f"📊 *Статистика на {today}*",
        "",
        f"Всего переводов сегодня: *{total_today}*",
        f"Всего переводов за всё время: *{total_all}*",
        "",
        "Пользователи по группам:",
    ]

    for grp in ["DEMO", "A1", "A2", "B1", "UNLIMITED"]:
        count = groups_count.get(grp, 0)
        lines.append(f"• {grp}: {count}")

    lines.append("")
    lines.append(f"Активных промокодов: *{promo_count}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: /user <id> или ответом на сообщение ученика."""
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для преподавателя.")
        return

    target_id = None

    # 1) Если есть аргумент — пробуем его как ID
    if context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID должен быть числом. Пример: `/user 7707071177`", parse_mode="Markdown")
            return

    # 2) Если нет аргумента, но есть reply
    if not target_id and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id

    if not target_id:
        await update.message.reply_text(
            "Использование:\n"
            "• `/user 7707071177`\n"
            "• или ответь этой командой на сообщение ученика",
            parse_mode="Markdown",
        )
        return

    rec = get_user_record(target_id)
    can_use, used, limit, group = can_use_translation(target_id)
    remaining = "∞" if limit == 0 else max(limit - used, 0)

    lines = [
        f"👤 *Ученик ID:* `{target_id}`",
        f"Группа: *{group}*",
        f"Лимит: {'Безлимит' if limit == 0 else f'{limit}/день'}",
        f"Использовано сегодня: {used}",
        f"Осталось сегодня: {remaining}",
        f"Всего переводов за всё время: {rec.get('total_used', 0)}",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def genpromo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: /genpromo <group> [count]"""
    admin_id = update.effective_user.id
    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("Эта команда только для преподавателя.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: `/genpromo A1 5`\n"
            "Где A1/A2/B1/UNLIMITED — группа, 5 — сколько кодов сгенерировать (по умолчанию 1).",
            parse_mode="Markdown",
        )
        return

    group = context.args[0].upper()
    if group not in GROUP_LIMITS:
        await update.message.reply_text(
            "Неизвестная группа. Допустимо: DEMO, A1, A2, B1, UNLIMITED.",
        )
        return

    count = 1
    if len(context.args) >= 2:
        try:
            count = int(context.args[1])
        except ValueError:
            pass

    if count < 1:
        count = 1
    if count > 50:
        count = 50

    codes = generate_promo_codes(group, count)

    lines = [
        f"🎟 Сгенерировано промокодов для группы *{group}*: {len(codes)}",
        "",
        "Коды (одноразовые):",
    ]
    for c in codes:
        lines.append(f"`{c}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --------------------------------------------------
# ОПЛАТА /pay ЧЕРЕЗ TELEGRAM
# --------------------------------------------------

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider_token = context.application.bot_data.get("PAYMENT_PROVIDER_TOKEN")
    if not provider_token:
        await update.message.reply_text(
            "Платежи через Telegram пока не настроены.\n"
            "Используй PayPal / SEPA / Revolut / Wise или свяжись с преподавателем."
        )
        return

    keyboard = [
        [InlineKeyboardButton("A1 — 15 €", callback_data="pay:A1")],
        [InlineKeyboardButton("A2 — 20 €", callback_data="pay:A2")],
        [InlineKeyboardButton("B1 — 25 €", callback_data="pay:B1")],
        [InlineKeyboardButton("UNLIMITED — 49 €", callback_data="pay:UNLIMITED")],
    ]
    await update.message.reply_text(
        "Выбери тариф, который хочешь оплатить:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pay_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    provider_token = context.application.bot_data.get("PAYMENT_PROVIDER_TOKEN")
    if not provider_token:
        await query.edit_message_text(
            "Платежи через Telegram пока не настроены.\n"
            "Используй PayPal / SEPA / Revolut / Wise."
        )
        return

    _, tariff_code = query.data.split(":", 1)
    tariff = TARIFFS.get(tariff_code)
    if not tariff:
        await query.edit_message_text("Неизвестный тариф. Попробуй ещё раз.")
        return

    prices = [
        LabeledPrice(
            label=tariff["title"],
            amount=tariff["price_eur"] * 100,  # в центах
        )
    ]

    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=tariff["title"],
        description=tariff["description"],
        payload=tariff["payload"],
        provider_token=provider_token,
        currency="EUR",
        prices=prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )

    await query.edit_message_text(
        "Счёт отправлен. Открой окно оплаты в Telegram и завершите платёж."
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    payload = payment.invoice_payload

    payload_to_group = {
        "tariff_A1": "A1",
        "tariff_A2": "A2",
        "tariff_B1": "B1",
        "tariff_UNLIMITED": "UNLIMITED",
    }

    group = payload_to_group.get(payload)
    if group:
        set_user_group(user_id, group)
        await update.message.reply_text(
            f"🎉 Оплата принята!\nТвой новый уровень доступа: *{group}*.\n"
            "Теперь просто отправляй голосовые — лимиты обновлены.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Платёж получен, но тариф не распознан. Напиши преподавателю, пожалуйста."
        )


# --------------------------------------------------
# ИЗМЕНЕНИЕ НАПРАВЛЕНИЯ
# --------------------------------------------------

async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    new_dir = query.data.split(":", 1)[1]
    context.user_data["direction"] = new_dir

    await query.edit_message_text(
        f"Направление изменено на {get_direction_label(new_dir)}.\n"
        "Теперь отправь голосовое или текст.",
        reply_markup=make_direction_keyboard(new_dir),
    )


# --------------------------------------------------
# ОБРАБОТКА ТЕКСТА И ГОЛОСА
# --------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    can_use, used, limit, group = can_use_translation(user_id)
    if not can_use:
        await update.message.reply_text(
            "⚠️ Ты исчерпал лимит переводов на сегодня.\n"
            "Обнови тариф через преподавателя или команду /pay."
        )
        return

    direction = get_current_direction(context)
    src, dest = DIRECTIONS[direction]

    translated = await translate_text(text, src, dest)
    increment_usage(user_id)

    await update.message.reply_text(
        f"🗣 *Оригинал*:\n{text}\n\n"
        f"🌍 *Перевод*:\n{translated}",
        parse_mode="Markdown",
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    voice = update.message.voice

    can_use, used, limit, group = can_use_translation(user_id)
    if not can_use:
        await update.message.reply_text(
            "⚠️ Ты исчерпал лимит переводов на сегодня.\n"
            "Обнови тариф через преподавателя или команду /pay."
        )
        return

    file = await context.bot.get_file(voice.file_id)
    fd, path = tempfile.mkstemp(suffix=".oga")
    os.close(fd)
    path = Path(path)

    await file.download_to_drive(str(path))

    src, dest = DIRECTIONS[get_current_direction(context)]
    locale = LANG_INFO[src]["locale"]

    logger.info("Распознаю речь с locale=%s", locale)
    text = recognize_speech(path, locale)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

    if not text:
        logger.warning("Не удалось распознать речь")
        await update.message.reply_text("Не удалось распознать речь. Попробуй ещё раз.")
        return

    logger.info("Распознано: %r", text)

    translated = await translate_text(text, src, dest)
    increment_usage(user_id)

    await update.message.reply_text(
        f"🗣 *Оригинал*:\n{text}\n\n"
        f"🌍 *Перевод*:\n{translated}",
        parse_mode="Markdown",
    )


# --------------------------------------------------
# ЗАПУСК
# --------------------------------------------------

async def main():
    bot_token = os.getenv("BOT_TOKEN")
    base_url = os.getenv("BASE_URL")
    port = int(os.getenv("PORT", 10000))
    payment_provider_token = os.getenv("PAYMENT_PROVIDER_TOKEN")

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

    application = Application.builder().token(bot_token).concurrent_updates(True).build()

    # сохраняем токен для /pay
    if payment_provider_token:
        application.bot_data["PAYMENT_PROVIDER_TOKEN"] = payment_provider_token
        logger.info("PAYMENT_PROVIDER_TOKEN загружен, платежи в боте включены.")
    else:
        logger.warning("PAYMENT_PROVIDER_TOKEN не задан. Платежи в боте будут отключены.")

    # Пользовательские команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("pricing", pricing))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CommandHandler("password", password_cmd))

    # Админ-команды
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("user", user_cmd))
    application.add_handler(CommandHandler("genpromo", genpromo_cmd))

    # Кнопки направления и оплаты
    application.add_handler(CallbackQueryHandler(direction_button, pattern=r"^dir:"))
    application.add_handler(CallbackQueryHandler(pay_button, pattern=r"^pay:"))

    # Платежные хендлеры
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Голос и текст
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запуск вебхука или long polling
    if base_url:
        webhook_url = f"{base_url}/webhook"
        logger.info("Запускаю webhook на %s, порт %s", webhook_url, port)
        await application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=webhook_url,
        )
    else:
        logger.info("BASE_URL не задан, запускаю long polling")
        await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
