import logging
import os
import tempfile
from datetime import date
from typing import Dict, Any, Optional, Tuple

import ffmpeg
import speech_recognition as sr
from deep_translator import GoogleTranslator
from pydub import AudioSegment
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

# --------------------------------------------------
# ЛОГИ
# --------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

recognizer = sr.Recognizer()

# --------------------------------------------------
# НАСТРОЙКИ ЛИМИТОВ И ГРУПП
# --------------------------------------------------

# Твой айди (из логов /start)
OWNER_ID = 7707071177

# Друг в Германии — всегда безлимит
FRIENDS_UNLIMITED = {1300323894}

# Админы (могут смотреть /stats, /user и т.п.)
ADMIN_IDS = {OWNER_ID}

# Бейсик лимит для бесплатных
DEFAULT_FREE_LIMIT = 30

# Группы + лимиты
GROUPS: Dict[str, Dict[str, Any]] = {
    "free": {
        "title": "Free",
        "limit": DEFAULT_FREE_LIMIT,
        "desc": "Бесплатный базовый доступ.",
    },
    "A1": {
        "title": "A1",
        "limit": 60,
        "desc": "Начальный уровень, до 60 переводов в день.",
    },
    "A2": {
        "title": "A2",
        "limit": 120,
        "desc": "Продвинутый новичок, до 120 переводов в день.",
    },
    "B1": {
        "title": "B1",
        "limit": 250,
        "desc": "Уверенный уровень, до 250 переводов в день.",
    },
    "vip": {
        "title": "VIP",
        "limit": None,  # None = безлимит
        "desc": "VIP-доступ, без ограничений.",
    },
}

# Пароли → группы
PASSWORDS: Dict[str, str] = {
    # как просил — везде используется слово karbofos
    "karbofos-a1": "A1",
    "karbofos-a2": "A2",
    "karbofos-b1": "B1",
    # отдельный VIP-пароль, если захочешь кому-то дать безлимит
    "karbofos-vip": "vip",
}

# --------------------------------------------------
# НАПРАВЛЕНИЯ ПЕРЕВОДА
# --------------------------------------------------

DIRECTIONS: Dict[str, Dict[str, str]] = {
    "ru_de": {"label": "🇷🇺 → 🇩🇪", "src": "ru", "dest": "de"},
    "de_ru": {"label": "🇩🇪 → 🇷🇺", "src": "de", "dest": "ru"},
    "ru_en": {"label": "🇷🇺 → 🇬🇧", "src": "ru", "dest": "en"},
    "en_ru": {"label": "🇬🇧 → 🇷🇺", "src": "en", "dest": "ru"},
    "ru_tr": {"label": "🇷🇺 → 🇹🇷", "src": "ru", "dest": "tr"},
    "tr_ru": {"label": "🇹🇷 → 🇷🇺", "src": "tr", "dest": "ru"},
    "ru_ro": {"label": "🇷🇺 → 🇷🇴", "src": "ru", "dest": "ro"},
    "ro_ru": {"label": "🇷🇴 → 🇷🇺", "src": "ro", "dest": "ru"},
    "ru_ar": {"label": "🇷🇺 → 🇸🇾", "src": "ru", "dest": "ar"},  # сирийский арабский
    "ar_ru": {"label": "🇸🇾 → 🇷🇺", "src": "ar", "dest": "ru"},
    "ru_pl": {"label": "🇷🇺 → 🇵🇱", "src": "ru", "dest": "pl"},
    "pl_ru": {"label": "🇵🇱 → 🇷🇺", "src": "pl", "dest": "ru"},
}

# Локали для распознавания речи
SPEECH_LOCALES: Dict[str, str] = {
    "ru": "ru-RU",
    "de": "de-DE",
    "en": "en-US",
    "tr": "tr-TR",
    "ro": "ro-RO",
    "ar": "ar-SY",
    "pl": "pl-PL",
}

# --------------------------------------------------
# СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ В ПАМЯТИ
# --------------------------------------------------

# user_id -> состояние
USERS: Dict[int, Dict[str, Any]] = {}


def get_today_str() -> str:
    return date.today().isoformat()


def get_user_state(user_id: int) -> Dict[str, Any]:
    """Гарантированно возвращает состояние пользователя."""
    if user_id not in USERS:
        # дефолты
        group = "free"
        if user_id in FRIENDS_UNLIMITED:
            group = "vip"

        USERS[user_id] = {
            "direction": "ru_de",
            "group": group,
            "used_today": 0,
            "total_used": 0,
            "last_date": get_today_str(),
        }
    state = USERS[user_id]

    # Если наступил новый день — обнуляем used_today
    today = get_today_str()
    if state.get("last_date") != today:
        state["last_date"] = today
        state["used_today"] = 0

    return state


def get_user_limit(user_id: int, state: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Возвращает лимит переводов в день, None = безлимит."""
    if state is None:
        state = get_user_state(user_id)

    # Друг из Германии и, при желании, сам владелец — безлимит
    if user_id in FRIENDS_UNLIMITED:
        return None

    group_key = state.get("group", "free")
    group = GROUPS.get(group_key, GROUPS["free"])
    return group["limit"]


def inc_usage_and_check(user_id: int) -> Tuple[bool, int, Optional[int]]:
    """
    Инкрементируем счётчик пользователя.
    Возвращает:
      (можно_использовать, использовано_сегодня, лимит)
    """
    state = get_user_state(user_id)
    limit = get_user_limit(user_id, state)

    if limit is not None and state["used_today"] >= limit:
        return False, state["used_today"], limit

    state["used_today"] += 1
    state["total_used"] += 1
    return True, state["used_today"], limit


def remaining_today(user_id: int) -> Optional[int]:
    state = get_user_state(user_id)
    limit = get_user_limit(user_id, state)
    if limit is None:
        return None
    return max(limit - state["used_today"], 0)


# --------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# --------------------------------------------------

def make_direction_keyboard(current: str) -> InlineKeyboardMarkup:
    """
    Собираем клавиатуру с языковыми направлениями + выделяем текущее.
    Сделаем сеткой по 2 в строке.
    """
    buttons = []
    row = []
    for key, cfg in DIRECTIONS.items():
        label = cfg["label"]
        if key == current:
            label = f"✅ {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"dir:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def convert_oga_to_wav(oga_path: str, wav_path: str) -> None:
    """Конвертируем .oga → .wav через ffmpeg."""
    (
        ffmpeg
        .input(oga_path)
        .output(wav_path, format="wav")
        .overwrite_output()
        .run(quiet=True)
    )


def recognize_speech_from_file(path: str, lang_code: str) -> Optional[str]:
    """Распознаём речь из wav-файла."""
    locale = SPEECH_LOCALES.get(lang_code, "ru-RU")
    logger.info("Распознаю речь с locale=%s", locale)

    try:
        with sr.AudioFile(path) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio, language=locale)
        text = text.strip()
        logger.info("Распознано: %r", text)
        return text
    except sr.UnknownValueError:
        logger.warning("Не удалось распознать речь")
        return None
    except sr.RequestError as e:
        logger.warning("Ошибка сервиса распознавания: %s", e)
        return None


def translate_text(text: str, src: str, dest: str) -> str:
    """Перевод текста deep_translator'ом."""
    logger.info("Перевожу текст. src=%s dest=%s text=%r", src, dest, text)
    # deep_translator ожидает коды языков 'ru', 'de', 'en', 'tr', ...
    translated = GoogleTranslator(source=src, target=dest).translate(text)
    return translated.strip()


def format_group_status(user_id: int) -> str:
    state = get_user_state(user_id)
    group_key = state.get("group", "free")
    group = GROUPS.get(group_key, GROUPS["free"])
    limit = get_user_limit(user_id, state)
    used = state["used_today"]
    rem = remaining_today(user_id)

    if limit is None:
        limit_str = "безлимит 🚀"
    else:
        limit_str = f"{limit} переводов/день"

    if rem is None:
        rem_str = "без ограничений"
    else:
        rem_str = f"осталось {rem} сегодня"

    return (
        f"Текущая группа: *{group['title']}*\n"
        f"Лимит: *{limit_str}*\n"
        f"Сегодня использовано: *{used}*\n"
        f"Сегодня {rem_str}."
    )


# --------------------------------------------------
# ХЕНДЛЕРЫ КОМАНД
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    state = get_user_state(user_id)

    # если друг из Германии — всегда VIP
    if user_id in FRIENDS_UNLIMITED:
        state["group"] = "vip"

    direction_key = state["direction"]
    dir_cfg = DIRECTIONS[direction_key]

    text = (
        f"Привет, {user.first_name or 'друг'}! 👋\n\n"
        f"Это *Bratik Voice Translator*.\n"
        f"Просто пришли голосовое — я распознаю и переведу.\n\n"
        f"Текущее направление: *{dir_cfg['label']}*\n\n"
        + format_group_status(user_id)
        + "\n\n"
        "🔁 Можешь переключить направление, нажав на кнопку ниже.\n"
        "🔑 Для активации группы курса используй команду /password\n"
        "ℹ️ Посмотреть тарифы: /pricing\n"
        "📊 Статус лимитов: /status"
    )

    await update.effective_chat.send_message(
        text,
        reply_markup=make_direction_keyboard(direction_key),
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🧾 *Команды бота:*\n\n"
        "/start – перезапустить бота и показать меню\n"
        "/help – эта справка\n"
        "/pricing – тарифы и варианты оплаты\n"
        "/status – статус лимитов и группы\n"
        "/groupinfo – кратко о группах A1/A2/B1/VIP\n"
        "/password `<код>` – ввести код доступа к группе\n\n"
        "Просто присылай *голосовые сообщения* или *текст*, "
        "и я буду переводить по выбранному направлению."
    )
    await update.effective_chat.send_message(text, parse_mode="Markdown")


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "💰 *Тарифы Bratik Voice Translator (черновик)*\n\n"
        "• Free — 30 переводов/день\n"
        "• A1 — 60 переводов/день\n"
        "• A2 — 120 переводов/день\n"
        "• B1 — 250 переводов/день\n"
        "• VIP — безлимит\n\n"
        "Способы оплаты, которые обычно проще всего использовать:\n"
        "• PayPal\n"
        "• Revolut\n"
        "• Wise\n"
        "• SEPA/банковский перевод (для ЕС)\n"
        "• 💱 Крипта (USDT/USDC/ETH/BTC)\n\n"
        "Пока что оплата подключается *вручную* через друга/куратора курса.\n"
        "Например: он собирает оплату у студентов и выдаёт им пароли вида\n"
        "`karbofos-a1`, `karbofos-a2`, `karbofos-b1` или `karbofos-vip`.\n\n"
        "После оплаты студент вводит пароль командой:\n"
        "`/password karbofos-a1`\n\n"
        "В следующих версиях можно будет прикрутить встроенные платежи Telegram."
    )
    await update.effective_chat.send_message(text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = "📊 *Текущий статус:*\n\n" + format_group_status(user_id)
    await update.effective_chat.send_message(text, parse_mode="Markdown")


async def groupinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📚 *Группы и уровни:*", ""]
    for key, g in GROUPS.items():
        limit = g["limit"]
        if limit is None:
            limit_str = "безлимит"
        else:
            limit_str = f"{limit}/день"
        lines.append(f"*{g['title']}*: {limit_str}\n_{g['desc']}_\n")
    text = "\n".join(lines)
    await update.effective_chat.send_message(text, parse_mode="Markdown")


async def password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat = update.effective_chat

    if not context.args:
        await chat.send_message(
            "🔑 Отправь пароль так:\n`/password karbofos-a1`\n\n"
            "Доступные примеры:\n"
            "`karbofos-a1`, `karbofos-a2`, `karbofos-b1`, `karbofos-vip`.",
            parse_mode="Markdown",
        )
        return

    code = context.args[0].strip()
    if code not in PASSWORDS:
        await chat.send_message("❌ Неверный пароль. Проверь код или уточни у куратора.")
        return

    group_key = PASSWORDS[code]
    state = get_user_state(user_id)
    state["group"] = group_key

    group = GROUPS[group_key]
    await chat.send_message(
        f"✅ Пароль принят!\n"
        f"Теперь ты в группе *{group['title']}*.\n\n"
        + format_group_status(user_id),
        parse_mode="Markdown",
    )


# --------------------------------------------------
# АДМИН-КОМАНДЫ
# --------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    total_users = len(USERS)
    total_translations = sum(u.get("total_used", 0) for u in USERS.values())

    text = (
        "📊 *Статистика бота:*\n\n"
        f"Пользователей в памяти: *{total_users}*\n"
        f"Всего переводов (с начала запуска процесса): *{total_translations}*\n\n"
        "_Учти, что после перезапуска сервера эта статистика сбрасывается,_\n"
        "так как пока всё хранится только в оперативной памяти."
    )
    await update.effective_chat.send_message(text, parse_mode="Markdown")


async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    chat = update.effective_chat
    if not context.args:
        await chat.send_message(
            "Использование: `/user <telegram_id>`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await chat.send_message("ID должен быть числом.")
        return

    state = USERS.get(target_id)
    if not state:
        await chat.send_message("Пользователь не найден в памяти.")
        return

    group_key = state.get("group", "free")
    group = GROUPS.get(group_key, GROUPS["free"])
    limit = get_user_limit(target_id, state)
    rem = remaining_today(target_id)

    text = (
        f"👤 Пользователь: `{target_id}`\n"
        f"Группа: *{group['title']}*\n"
        f"direction: `{state.get('direction')}`\n"
        f"used_today: *{state.get('used_today', 0)}*\n"
        f"total_used: *{state.get('total_used', 0)}*\n"
    )
    if limit is None:
        text += "Лимит: безлимит\n"
    else:
        text += f"Лимит: {limit}/день, осталось сегодня: {rem}\n"

    await chat.send_message(text, parse_mode="Markdown")


# --------------------------------------------------
# КНОПКИ (LANG DIRECTION)
# --------------------------------------------------

async def direction_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    state = get_user_state(user_id)

    data = query.data
    _, direction_key = data.split(":", 1)

    if direction_key not in DIRECTIONS:
        await query.answer("Неизвестное направление.", show_alert=True)
        return

    old_dir = state["direction"]
    state["direction"] = direction_key
    cfg = DIRECTIONS[direction_key]

    if direction_key == old_dir:
        # попали в ту же кнопку — ничего не меняем, чтобы не ловить 400 "Message is not modified"
        await query.answer("Уже выбрано это направление ✅")
        return

    msg = (
        f"Направление перевода изменено на: *{cfg['label']}*.\n\n"
        + format_group_status(user_id)
    )
    try:
        await query.edit_message_text(
            msg,
            reply_markup=make_direction_keyboard(direction_key),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Ошибка при редактировании сообщения: %s", e)
        # в крайнем случае просто ответим отдельным сообщением
        await query.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=make_direction_keyboard(direction_key),
        )


# --------------------------------------------------
# ОБРАБОТКА ГОЛОСОВЫХ
# --------------------------------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    user_id = user.id

    # Лимиты
    ok, used, limit = inc_usage_and_check(user_id)
    if not ok:
        if limit is None:
            # теоретически не должно случиться
            await chat.send_message("У тебя безлимит, но что-то пошло не так 🤯")
        else:
            await chat.send_message(
                f"🚫 Ты уже использовал свой дневной лимит ({limit} переводов)."
            )
        return

    state = get_user_state(user_id)
    direction_key = state["direction"]
    dir_cfg = DIRECTIONS[direction_key]

    src_lang = dir_cfg["src"]
    dest_lang = dir_cfg["dest"]

    voice = message.voice
    if not voice:
        await chat.send_message("Не вижу голосового сообщения 🤔")
        return

    # Скачиваем файл
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as f_oga:
        oga_path = f_oga.name
        await file.download_to_drive(oga_path)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_wav:
        wav_path = f_wav.name

    try:
        # конвертируем ogg/oga → wav
        convert_oga_to_wav(oga_path, wav_path)

        # распознаём
        text = recognize_speech_from_file(wav_path, src_lang)
        if not text:
            await chat.send_message(
                "😔 Не удалось распознать речь. Попробуй говорить чуть чётче или громче."
            )
            return

        # переводим
        translated = translate_text(text, src_lang, dest_lang)

        rem = remaining_today(user_id)
        if rem is None:
            rem_str = "без лимита 🚀"
        else:
            rem_str = f"осталось *{rem}* переводов сегодня"

        reply = (
            f"🗣 *Распознано ({dir_cfg['label']}):*\n"
            f"`{text}`\n\n"
            f"📥 *Перевод:*\n"
            f"`{translated}`\n\n"
            f"📊 {rem_str}"
        )
        await chat.send_message(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error("Ошибка обработки голосового: %s", e)
        await chat.send_message("❌ Произошла ошибка при обработке голосового.")
    finally:
        # чистим файлы
        try:
            os.remove(oga_path)
        except OSError:
            pass
        try:
            os.remove(wav_path)
        except OSError:
            pass


# --------------------------------------------------
# ОБРАБОТКА ТЕКСТА
# --------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    user_id = update.effective_user.id
    text = (message.text or "").strip()

    if not text:
        return

    # Лимиты
    ok, used, limit = inc_usage_and_check(user_id)
    if not ok:
        if limit is None:
            await chat.send_message("У тебя безлимит, но что-то пошло не так 🤯")
        else:
            await chat.send_message(
                f"🚫 Ты уже использовал свой дневной лимит ({limit} переводов)."
            )
        return

    state = get_user_state(user_id)
    direction_key = state["direction"]
    dir_cfg = DIRECTIONS[direction_key]
    src_lang = dir_cfg["src"]
    dest_lang = dir_cfg["dest"]

    try:
        translated = translate_text(text, src_lang, dest_lang)
        rem = remaining_today(user_id)
        if rem is None:
            rem_str = "без лимита 🚀"
        else:
            rem_str = f"осталось *{rem}* переводов сегодня"

        reply = (
            f"📥 *Исходный текст ({dir_cfg['label']}):*\n"
            f"`{text}`\n\n"
            f"📤 *Перевод:*\n"
            f"`{translated}`\n\n"
            f"📊 {rem_str}"
        )
        await chat.send_message(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка перевода текста: %s", e)
        await chat.send_message("❌ Произошла ошибка при переводе текста.")


# --------------------------------------------------
# ЗАПУСК
# --------------------------------------------------

def main() -> None:
    bot_token = os.getenv("BOT_TOKEN")
    base_url = os.getenv("BASE_URL")
    port = int(os.getenv("PORT", "10000"))

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

    application = Application.builder().token(bot_token).concurrent_updates(True).build()

    # Команды пользователя
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("pricing", pricing))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("groupinfo", groupinfo))
    application.add_handler(CommandHandler("password", password_cmd))

    # Админ-команды
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("user", user_cmd))

    # Кнопки (направление перевода)
    application.add_handler(CallbackQueryHandler(direction_button, pattern=r"^dir:"))

    # Голос + текст
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if base_url:
        webhook_url = f"{base_url.rstrip('/')}/webhook"
        logger.info("Запускаю webhook на %s, порт %s", webhook_url, port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=webhook_url,
        )
    else:
        logger.info("BASE_URL не задан, запускаю long polling")
        application.run_polling()


if __name__ == "__main__":
    main()
