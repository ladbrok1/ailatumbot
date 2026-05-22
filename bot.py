import asyncio
import base64
import io
import os
import random
import re
import sqlite3
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import OpenAI


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@ailatumbot").lower()
DB_PATH = os.getenv("BOT_DB_PATH", "bot_memory.sqlite3")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

if not GROQ_API_KEY:
    raise RuntimeError("Set GROQ_API_KEY environment variable")

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
dp = Dispatcher()

SYSTEM_PROMPT = """
Ты русскоязычный ИИ-тиммейт для небольшой компании друзей, которые играют в Valorant.

Стиль:
- отвечай коротко, полезно и по делу;
- можно рафлить и подкалывать, но без токсичности, унижения и личных оскорблений;
- вайб опытного Immortal/Radiant игрока, который шарит, но не душнит;
- если человек тильтует, сначала стабилизируй, потом дай конкретный план;
- учитывай память о составе, ролях, агентах, рангах и последних сообщениях, если она передана.

Помогай с агентами, экономикой, стратами, клатчами, ретейками, аимом, разбором Tracker/Riot ID и скринов.
Если вопрос не про Valorant, мягко возвращай разговор к игре.
"""

VISION_PROMPT = """
Разбери изображение как тренер по Valorant.
Если это скрин таба, статистики, карты, позиции, настроек или Tracker, дай:
1. что видно;
2. главная проблема;
3. 3 конкретных действия для улучшения;
4. короткий дружеский подкол, без токсичности.
Если на фото не Valorant, скажи это и попроси прислать нужный скрин.
"""

AGENTS = {
    "duelist": ["Jett", "Raze", "Reyna", "Phoenix", "Yoru", "Neon", "Iso"],
    "дуэлянт": ["Jett", "Raze", "Reyna", "Phoenix", "Yoru", "Neon", "Iso"],
    "дуелянт": ["Jett", "Raze", "Reyna", "Phoenix", "Yoru", "Neon", "Iso"],
    "initiator": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "инициатор": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "controller": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "контроллер": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "смокер": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "sentinel": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
    "страж": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
}

ROLE_ALIASES = {
    "дуэлянт": "дуэлянт",
    "дуелянт": "дуэлянт",
    "duelist": "дуэлянт",
    "инициатор": "инициатор",
    "initiator": "инициатор",
    "смокер": "смокер",
    "контроллер": "смокер",
    "controller": "смокер",
    "страж": "страж",
    "sentinel": "страж",
}

MAP_TIPS = {
    "ascent": "Ascent: заберите мид, иначе будете угадывать сайт как на контрольной без подготовки.",
    "bind": "Bind: душите телепортами и фейками. Showers/long бесплатно отдавать нельзя.",
    "haven": "Haven: три сайта = инфа дороже скина. На защите не спите по одному в коробках.",
    "split": "Split: мид решает. Smokes на vent/mail и нормальный выход, а не парад по одному.",
    "lotus": "Lotus: ломайте двери, меняйте темп, ловите ротации. Карта любит наглых.",
    "sunset": "Sunset: mid control открывает всё. Следите за lurk, иначе получите привет в спину.",
    "icebox": "Icebox: без дронов/флешей выходы превращаются в тир, где мишени - вы.",
    "breeze": "Breeze: дальние дуэли, halls и смоки. Без инфы там не игра, а экскурсия.",
}

ECO_TIPS = [
    "Eco: сыграйте стаком и заберите один вандал. Умирать по одному - это донат врагу.",
    "Force: форсите только с планом: быстрый выход, шортганы, ульты или стак.",
    "Bonus: Spectre не продаём за понты. Играйте ближе и ломайте экономику врага.",
    "Full buy: проверьте смоки, флеши и defuse. Скин на вандале раунд сам не выиграет.",
]

AIM_ROUTINES = [
    "10 минут: 3 мин easy bots Sheriff, 4 мин medium Vandal, 3 deathmatch только crosshair placement.",
    "15 минут: 5 мин tracking, 5 мин strafing bots, 5 мин DM без crouch spray. Да, без любимого приседа.",
    "Перед каткой: 50 ботов на точность, 1 DM на спокойствие, потом ranked. Не наоборот.",
]

TILT_LINES = [
    "Тильт-пауза: выдох, вода, следующий раунд играем простой план. Геройствовать будешь в хайлайтах.",
    "Если лузстрик: перестань пикать первым без трейда. Ты не бессмертный, просто ник красивый.",
    "Менталка: один раунд = одна задача. Сейчас задача не доказать миру, а сыграть вместе.",
]

CLUTCH_TIPS = {
    "1v1": "1v1: не шуми без причины, изолируй дуэль, играй от таймера. Не отдавай честный пик, если можно быть мерзким.",
    "1v2": "1v2: разведи на разные дуэли. Шум в одну сторону, позиция в другую, потом забирай по одному.",
    "1v3": "1v3: нужен хаос: смок, флеш, тайминг, фейк. Честно перестреливать троих - это контент для врага.",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                username TEXT,
                role TEXT,
                agent TEXT,
                rank TEXT,
                tracker TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Игрок"
    return user.full_name or user.username or str(user.id)


def upsert_player(message: Message):
    if not message.from_user:
        return
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO players (chat_id, user_id, display_name, username, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            (
                message.chat.id,
                message.from_user.id,
                display_name(message),
                message.from_user.username,
                now_iso(),
            ),
        )


def update_player_field(message: Message, field: str, value: str):
    if not message.from_user:
        return
    upsert_player(message)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE players SET {field} = ?, updated_at = ? WHERE chat_id = ? AND user_id = ?",
            (value.strip(), now_iso(), message.chat.id, message.from_user.id),
        )


def append_player_note(message: Message, note: str):
    if not message.from_user:
        return
    upsert_player(message)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT notes FROM players WHERE chat_id = ? AND user_id = ?",
            (message.chat.id, message.from_user.id),
        ).fetchone()
        old_notes = row[0] if row and row[0] else ""
        notes = f"{old_notes}; {note}".strip("; ")
        conn.execute(
            "UPDATE players SET notes = ?, updated_at = ? WHERE chat_id = ? AND user_id = ?",
            (notes[-700:], now_iso(), message.chat.id, message.from_user.id),
        )


def delete_player(message: Message):
    if not message.from_user:
        return
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM players WHERE chat_id = ? AND user_id = ?",
            (message.chat.id, message.from_user.id),
        )


def store_message(message: Message):
    text = message.text or message.caption or ""
    if not text or not message.from_user:
        return
    upsert_player(message)
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (chat_id, user_id, display_name, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message.chat.id, message.from_user.id, display_name(message), text[:1000], now_iso()),
        )
        conn.execute(
            """
            DELETE FROM chat_messages
            WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT 40
            )
            """,
            (message.chat.id, message.chat.id),
        )


def get_players(chat_id: int):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT * FROM players
            WHERE chat_id = ?
            ORDER BY display_name
            """,
            (chat_id,),
        ).fetchall()


def get_player(chat_id: int, user_id: int):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM players WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()


def get_recent_messages(chat_id: int, limit: int = 12):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT display_name, text FROM chat_messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()[::-1]


def player_line(player) -> str:
    parts = [player["display_name"]]
    if player["role"]:
        parts.append(f"роль: {player['role']}")
    if player["agent"]:
        parts.append(f"агент: {player['agent']}")
    if player["rank"]:
        parts.append(f"ранг: {player['rank']}")
    if player["tracker"]:
        parts.append(f"tracker: {player['tracker']}")
    if player["notes"]:
        parts.append(f"заметки: {player['notes']}")
    return " | ".join(parts)


def memory_context(message: Message) -> str:
    players = get_players(message.chat.id)
    recent = get_recent_messages(message.chat.id)
    current = None
    if message.from_user:
        current = get_player(message.chat.id, message.from_user.id)

    lines = ["Контекст чата:"]
    if current:
        lines.append(f"Текущий автор: {player_line(current)}")
    if players:
        lines.append("Состав:")
        lines.extend(f"- {player_line(player)}" for player in players[:15])
    if recent:
        lines.append("Последние сообщения:")
        lines.extend(f"- {row['display_name']}: {row['text']}" for row in recent)
    return "\n".join(lines)


def normalize_arg(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def strip_bot_mention(text: str) -> str:
    cleaned = re.sub(re.escape(BOT_USERNAME), "", text, flags=re.IGNORECASE)
    return cleaned.strip()


def is_addressed_to_bot(text: str) -> bool:
    lowered = normalize_arg(text)
    starters = ("бот ", "валик ", "вал ", "чатгпт ")
    return BOT_USERNAME in lowered or lowered.startswith(starters)


def command_body(text: str) -> str:
    text = strip_bot_mention(text)
    lowered = normalize_arg(text)
    for prefix in ("бот", "валик", "вал", "чатгпт"):
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + " "):
            return text[len(prefix) :].strip()
    return text.strip()


def parse_memory_update(message: Message) -> str | None:
    text = normalize_arg(command_body(message.text or ""))
    original = command_body(message.text or "")

    if text in {"забудь меня", "удали мой профиль", "очисти мой профиль"}:
        delete_player(message)
        return "Ок, забыл твой профиль в этом чате. Чистый лист, как после 0/13."

    role_match = re.search(r"(?:я|мой профиль)?\s*(?:играю\s+)?(?:роль|я)\s*[-: ]+\s*(дуэлянт|дуелянт|duelist|инициатор|initiator|смокер|контроллер|controller|страж|sentinel)", text)
    simple_role = re.search(r"^я\s+(дуэлянт|дуелянт|duelist|инициатор|initiator|смокер|контроллер|controller|страж|sentinel)$", text)
    if role_match or simple_role:
        role = ROLE_ALIASES[(role_match or simple_role).group(1)]
        update_player_field(message, "role", role)
        return f"Запомнил: твоя роль - {role}. Теперь отмазка 'я не знал что пикать' не работает."

    agent_match = re.search(r"(?:я\s+)?(?:играю\s+на|играю|мейню|мой\s+агент|агент)\s+([a-zа-я0-9/' -]{2,24})", text)
    if agent_match:
        agent = agent_match.group(1).strip(" .,!?:;")
        update_player_field(message, "agent", agent.title())
        return f"Запомнил: твой агент - {agent.title()}."

    rank_match = re.search(r"(?:мой\s+ранг|ранг|я)\s+([a-zа-я]+(?:\s*\d{1,2})?|ascendant|immortal|radiant|iron|bronze|silver|gold|platinum|diamond)", text)
    if rank_match and any(word in text for word in ("ранг", "желез", "бронз", "сильвер", "голд", "плат", "даймонд", "асцен", "иммо", "радиант", "gold", "silver", "diamond")):
        rank = rank_match.group(1).strip(" .,!?:;")
        update_player_field(message, "rank", rank.title())
        return f"Запомнил ранг: {rank.title()}. Будем апать, а не коллекционировать -18."

    tracker_match = re.search(r"(?:мой\s+)?(?:tracker|трекер)\s*[-: ]+\s*(.+)", original, re.IGNORECASE)
    if tracker_match:
        tracker = tracker_match.group(1).strip()
        update_player_field(message, "tracker", tracker)
        return "Запомнил трекер. Теперь можно будет разбирать стату без археологии в чате."

    note_match = re.search(r"(?:запомни|помни)\s+(.+)", original, re.IGNORECASE)
    if note_match:
        note = note_match.group(1).strip()
        append_player_note(message, note)
        return "Запомнил. Добавил в досье, почти как Cypher, только без камеры в душе."

    return None


async def ask_groq(text: str, message: Message | None = None) -> str:
    user_content = text
    if message:
        user_content = f"{memory_context(message)}\n\nЗапрос пользователя:\n{text}"
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=700,
    )
    return response.choices[0].message.content


async def ask_groq_vision(image_bytes: bytes, prompt: str, message: Message | None = None) -> str:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    text_prompt = prompt
    if message:
        text_prompt = f"{memory_context(message)}\n\n{prompt}"
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                ],
            }
        ],
        max_completion_tokens=700,
    )
    return response.choices[0].message.content


async def send_help(message: Message):
    await message.answer(
        "Можно без слэшей:\n"
        "бот команды\n"
        "бот я играю Omen\n"
        "бот я смокер\n"
        "бот мой ранг gold 2\n"
        "бот запомни Саня любит пушить мид\n"
        "бот мой профиль\n"
        "бот состав\n"
        "бот выбери агента смокер\n"
        "бот карта Ascent\n"
        "бот страта Ascent атака\n"
        "бот эко / аим / тильт / клатч 1v2\n"
        "бот трекер Name#TAG или ссылка\n\n"
        "Фото: отправь скрин с подписью 'бот разбери' или '/фото'.\n"
        "Слэши тоже работают: /команды, /агент, /карта, /страта, /эко, /аим, /клатч, /тильт, /трекер."
    )


async def send_profile(message: Message):
    if not message.from_user:
        return
    player = get_player(message.chat.id, message.from_user.id)
    if not player:
        await message.answer("Я про тебя пока ничего не помню. Напиши: бот я играю Omen, бот я смокер, бот мой ранг gold 2.")
        return
    await message.answer(player_line(player))


async def send_roster(message: Message):
    players = get_players(message.chat.id)
    if not players:
        await message.answer("Состав пока пустой. Пусть каждый напишет: бот я играю Omen, бот я дуэлянт, бот мой ранг gold.")
        return
    await message.answer("Состав:\n" + "\n".join(f"- {player_line(player)}" for player in players))


async def send_agent(message: Message, role_query: str = ""):
    role = normalize_arg(role_query) if role_query else random.choice(list(AGENTS))
    if role not in AGENTS:
        await message.answer("Роли: дуэлянт, инициатор, смокер, страж. Английские duelist/initiator/controller/sentinel тоже можно.")
        return
    await message.answer(f"Сегодня играешь {random.choice(AGENTS[role])}. Без нытья, делай impact.")


async def send_map_tip(message: Message, map_query: str):
    if not map_query:
        await message.answer("Напиши карту: бот карта Ascent")
        return
    map_name = normalize_arg(map_query)
    await message.answer(MAP_TIPS.get(map_name, "По этой карте нет заготовки. Спроси обычным текстом, разберу через Groq."))


async def send_clutch(message: Message, clutch_type: str = "1v2"):
    key = normalize_arg(clutch_type or "1v2")
    await message.answer(CLUTCH_TIPS.get(key, "Форматы: бот клатч 1v1, бот клатч 1v2, бот клатч 1v3"))


async def send_strat(message: Message, query: str):
    if not query:
        await message.answer("Пример: бот страта Ascent атака")
        return
    try:
        answer = await ask_groq(f"Дай короткую страту для Valorant: {query}. Формат: план, роли, ключевой риск.", message)
    except Exception:
        answer = "Groq не ответил. План Б: играйте дефолт, заберите инфу, потом взрывайте слабый сайт."
    await message.answer(answer)


async def send_tracker_analysis(message: Message, query: str):
    if not query:
        await message.answer("Кинь ссылку Tracker или Riot ID: бот трекер Name#TAG. Если есть скрин статистики, отправь фото.")
        return
    try:
        answer = await ask_groq(
            "Пользователь дал Tracker/Riot ID/статистику. Если данных мало, скажи что нужно прислать. "
            f"Разбери по Valorant и дай полезные выводы: {query}",
            message,
        )
    except Exception:
        answer = "Не смог достучаться до Groq. Кинь скрин Tracker, так будет проще разобрать."
    await message.answer(answer)


async def handle_natural_command(message: Message) -> bool:
    text = normalize_arg(command_body(message.text or ""))
    original = command_body(message.text or "")

    memory_reply = parse_memory_update(message)
    if memory_reply:
        await message.answer(memory_reply)
        return True

    if text in {"", "команды", "помощь", "хелп", "что ты умеешь"}:
        await send_help(message)
        return True
    if text in {"мой профиль", "профиль", "кто я", "что ты обо мне знаешь"}:
        await send_profile(message)
        return True
    if text in {"состав", "ростер", "кто на чем играет", "кто на чём играет", "тиммейты"}:
        await send_roster(message)
        return True
    if text.startswith(("выбери агента", "агент", "пикни")):
        role = re.sub(r"^(выбери агента|агент|пикни)\s*", "", text).strip()
        await send_agent(message, role)
        return True
    if text.startswith("карта"):
        await send_map_tip(message, original.partition(" ")[2])
        return True
    if text in {"эко", "eco", "экономика"}:
        await message.answer(random.choice(ECO_TIPS))
        return True
    if text in {"аим", "aim", "тренировка"}:
        await message.answer(random.choice(AIM_ROUTINES))
        return True
    if text in {"тильт", "tilt", "успокой", "минус мораль"}:
        await message.answer(random.choice(TILT_LINES))
        return True
    if text.startswith("клатч"):
        await send_clutch(message, original.partition(" ")[2] or "1v2")
        return True
    if text.startswith(("страта", "стратегия", "план")):
        await send_strat(message, original.partition(" ")[2])
        return True
    if text.startswith(("трекер", "tracker")):
        await send_tracker_analysis(message, original.partition(" ")[2])
        return True
    return False


@dp.message(Command("start", "help", "commands"))
async def help_reply(message: Message):
    store_message(message)
    await send_help(message)


@dp.message(F.text.regexp(r"^/(команды|помощь|хелп)(\s|$)"))
async def ru_help_reply(message: Message):
    store_message(message)
    await send_help(message)


@dp.message(Command("agent"))
async def agent_reply(message: Message):
    store_message(message)
    await send_agent(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/агент(\s|$)"))
async def ru_agent_reply(message: Message):
    store_message(message)
    await send_agent(message, (message.text or "").partition(" ")[2])


@dp.message(Command("map"))
async def map_reply(message: Message):
    store_message(message)
    await send_map_tip(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/карта(\s|$)"))
async def ru_map_reply(message: Message):
    store_message(message)
    await send_map_tip(message, (message.text or "").partition(" ")[2])


@dp.message(Command("eco"))
async def eco_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(ECO_TIPS))


@dp.message(F.text.regexp(r"^/эко(\s|$)"))
async def ru_eco_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(ECO_TIPS))


@dp.message(Command("aim"))
async def aim_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(AIM_ROUTINES))


@dp.message(F.text.regexp(r"^/аим(\s|$)"))
async def ru_aim_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(AIM_ROUTINES))


@dp.message(Command("tilt"))
async def tilt_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(TILT_LINES))


@dp.message(F.text.regexp(r"^/тильт(\s|$)"))
async def ru_tilt_reply(message: Message):
    store_message(message)
    await message.answer(random.choice(TILT_LINES))


@dp.message(Command("clutch"))
async def clutch_reply(message: Message):
    store_message(message)
    await send_clutch(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/клатч(\s|$)"))
async def ru_clutch_reply(message: Message):
    store_message(message)
    await send_clutch(message, (message.text or "").partition(" ")[2])


@dp.message(Command("strat"))
async def strat_reply(message: Message):
    store_message(message)
    await send_strat(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/страта(\s|$)"))
async def ru_strat_reply(message: Message):
    store_message(message)
    await send_strat(message, (message.text or "").partition(" ")[2])


@dp.message(Command("tracker"))
async def tracker_reply(message: Message):
    store_message(message)
    await send_tracker_analysis(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/трекер(\s|$)"))
async def ru_tracker_reply(message: Message):
    store_message(message)
    await send_tracker_analysis(message, (message.text or "").partition(" ")[2])


@dp.message(F.photo)
async def photo_reply(message: Message, bot: Bot):
    store_message(message)
    caption = message.caption or ""
    if not is_addressed_to_bot(caption) and not normalize_arg(caption).startswith(("/фото", "/photo", "/анализ")):
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > 3_000_000:
        await message.answer("Фото тяжеловато для base64-лимита Groq. Сожми скрин или отправь меньшую версию.")
        return

    image = io.BytesIO()
    await bot.download(photo, destination=image)
    image_bytes = image.getvalue()

    if len(base64.b64encode(image_bytes)) > 4_000_000:
        await message.answer("Фото после кодирования больше 4 MB. Сожми скрин и кинь ещё раз.")
        return

    user_prompt = strip_bot_mention(caption)
    prompt = f"{VISION_PROMPT}\nДополнительный запрос пользователя: {user_prompt or 'разбери этот скрин'}"

    try:
        answer = await ask_groq_vision(image_bytes, prompt, message)
    except Exception:
        answer = "Vision-модель Groq не ответила. Проверь лимиты/доступ к vision-модели или кинь скрин позже."

    await message.answer(answer)


@dp.message(F.text)
async def ai_reply(message: Message):
    store_message(message)
    text = message.text or ""

    if not is_addressed_to_bot(text):
        return

    if await handle_natural_command(message):
        return

    try:
        reply = await ask_groq(command_body(text), message)
    except Exception:
        await message.answer("Groq сейчас не ответил. Попробуй ещё раз через пару секунд.")
        return

    await message.answer(reply)


async def health_check(_request):
    return web.Response(text="ok")


async def start_health_server():
    port = os.getenv("PORT")
    if not port:
        return None

    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    return runner


async def main():
    init_db()
    bot = Bot(token=TELEGRAM_TOKEN)
    health_runner = await start_health_server()
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
