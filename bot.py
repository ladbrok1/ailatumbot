import asyncio
import os
import random
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

from aiohttp import ClientSession, web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import APIStatusError, RateLimitError, OpenAI


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TRACKER_API_KEY = os.getenv("TRACKER_API_KEY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@ailatumbot").lower()
DB_PATH = os.getenv("BOT_DB_PATH", "bot_memory.sqlite3")

TEXT_MODELS = [
    model.strip()
    for model in os.getenv(
        "GROQ_TEXT_MODELS",
        "meta-llama/llama-4-scout-17b-16e-instruct,qwen/qwen3-32b,llama-3.1-8b-instant",
    ).split(",")
    if model.strip()
]

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

if not GROQ_API_KEY:
    raise RuntimeError("Set GROQ_API_KEY environment variable")

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
dp = Dispatcher()

SYSTEM_PROMPT = """
Ты русскоязычный ИИ-тиммейт и тренер по Valorant для чата друзей.

Главные правила:
- отвечай конкретно, коротко и квалифицированно;
- можно шутить и подкалывать, но без токсичности и личных оскорблений;
- не путай игроков: профиль "Текущий автор" относится только к автору запроса;
- "Состав" - это справочник по людям в чате, не смешивай роли разных людей;
- "Недавний чат" - это только контекст разговора, а не подтвержденные факты;
- если данных нет, честно скажи, что их нет;
- запрещено выдумывать Tracker-цифры, матчи, winrate, K/D, ACS, ADR, HS%, KAST, агентов и карты;
- если Tracker API не вернул данные, попроси Riot ID/ссылку/цифры текстом;
- если пользователь просит "проанализируй профиль", используй только переданные Tracker API данные или текст от пользователя.

Формат хорошего ответа:
1. короткий вывод;
2. 2-4 конкретных действия;
3. если уместно - лёгкий дружеский подкол.
"""

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

AGENTS_BY_ROLE = {
    "дуэлянт": ["Jett", "Raze", "Reyna", "Phoenix", "Yoru", "Neon", "Iso"],
    "инициатор": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "смокер": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "страж": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
}

MAP_TIPS = {
    "ascent": "Ascent: мид - главный рычаг. Без контроля мида атака часто превращается в угадайку.",
    "bind": "Bind: играйте через телепорты, фейки и быстрые ротации. Showers/long бесплатно не отдавать.",
    "haven": "Haven: три сайта требуют инфы. На защите не сидите молча 1-1-3 без плана ретейка.",
    "split": "Split: мид решает темп. Смоки на vent/mail дают нормальные выходы и ретейки.",
    "lotus": "Lotus: ломайте двери, меняйте темп, ловите ротации. Карта любит наглую инфу.",
    "sunset": "Sunset: mid control открывает оба сайта. Следите за lurk через tiles.",
    "icebox": "Icebox: без дрона/флеша выходы превращаются в тир, где мишени - вы.",
    "breeze": "Breeze: дальние дуэли, halls и смоки. Без инфы там не игра, а экскурсия.",
}

AIM_TIPS = [
    "10 минут: 3 мин Sheriff easy bots, 4 мин Vandal medium bots, 3 мин DM только crosshair placement.",
    "15 минут: 5 мин tracking, 5 мин strafing bots, 5 мин DM без crouch spray. Да, без любимого приседа.",
    "Перед ranked: 50 ботов на точность, 1 DM на спокойствие, потом катка. Не наоборот.",
]

MENTAL_TIPS = [
    "Тильт-пауза: выдох, вода, следующий раунд играем простой план. Не надо доказывать миру, что ты главный герой.",
    "После лузстрика: перестань пикать первым без трейда. Красивый ник бессмертие не даёт.",
    "Один раунд - одна задача. Сейчас задача не спорить, а вместе забрать пространство.",
]


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
                addressed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
        if "addressed" not in columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN addressed INTEGER NOT NULL DEFAULT 0")


def normalize(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Игрок"
    return user.full_name or user.username or str(user.id)


def is_addressed_to_bot(text: str) -> bool:
    lowered = normalize(text)
    return BOT_USERNAME in lowered or lowered.startswith(("бот ", "валик ", "вал "))


def strip_bot_mention(text: str) -> str:
    cleaned = re.sub(re.escape(BOT_USERNAME), "", text, flags=re.IGNORECASE)
    lowered = normalize(cleaned)
    for prefix in ("бот", "валик", "вал"):
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + " "):
            return cleaned[len(prefix):].strip()
    return cleaned.strip()


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
    if field not in {"role", "agent", "rank", "tracker", "notes"}:
        return
    upsert_player(message)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE players SET {field} = ?, updated_at = ? WHERE chat_id = ? AND user_id = ?",
            (value.strip(), now_iso(), message.chat.id, message.from_user.id),
        )


def append_player_note(message: Message, note: str):
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


def store_message(message: Message):
    text = message.text or message.caption or ""
    if not text or not message.from_user:
        return
    upsert_player(message)
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (chat_id, user_id, display_name, text, addressed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message.chat.id,
                message.from_user.id,
                display_name(message),
                text[:1000],
                1 if is_addressed_to_bot(text) else 0,
                now_iso(),
            ),
        )
        conn.execute(
            """
            DELETE FROM chat_messages
            WHERE chat_id = ? AND id NOT IN (
                SELECT id FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT 60
            )
            """,
            (message.chat.id, message.chat.id),
        )


def get_player(chat_id: int, user_id: int):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM players WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()


def get_players(chat_id: int):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM players WHERE chat_id = ? ORDER BY display_name",
            (chat_id,),
        ).fetchall()


def get_recent_messages(chat_id: int, limit: int = 10):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT display_name, text, addressed FROM chat_messages
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
    current = get_player(message.chat.id, message.from_user.id) if message.from_user else None
    players = get_players(message.chat.id)
    recent = get_recent_messages(message.chat.id)

    lines = [
        "ПАМЯТЬ. Используй как справочник, не смешивай игроков.",
        f"Текущий автор запроса: {player_line(current) if current else display_name(message)}",
    ]
    if players:
        lines.append("Состав чата:")
        lines.extend(f"- {player_line(player)}" for player in players[:12])
    if recent:
        lines.append("Недавний чат. Это контекст обсуждения, не база фактов:")
        for row in recent:
            tag = "обращение к боту" if row["addressed"] else "обычное сообщение"
            lines.append(f"- {row['display_name']} ({tag}): {row['text']}")
    return "\n".join(lines)


def tracker_id_from_text(text: str) -> str | None:
    match = re.search(r"([A-Za-z0-9А-Яа-я_. -]{2,32}#[A-Za-z0-9А-Яа-я]{2,8})", text)
    if match:
        return match.group(1).strip()
    return None


def extract_tracker_stats(data: dict) -> str:
    lines = []
    profile = data.get("data", {})
    platform_info = profile.get("platformInfo") or {}
    user_info = profile.get("userInfo") or {}
    if platform_info.get("platformUserHandle"):
        lines.append(f"Профиль: {platform_info['platformUserHandle']}")
    if user_info.get("countryCode"):
        lines.append(f"Регион: {user_info['countryCode']}")

    for segment in profile.get("segments", [])[:8]:
        segment_name = segment.get("metadata", {}).get("name") or segment.get("type") or "segment"
        stats = segment.get("stats") or {}
        stat_lines = []
        for key, value in stats.items():
            display = value.get("displayName") or key
            stat_value = value.get("displayValue")
            if stat_value is not None:
                stat_lines.append(f"{display}: {stat_value}")
        if stat_lines:
            lines.append(f"{segment_name}: " + "; ".join(stat_lines[:14]))

    return "\n".join(lines[:30])


async def fetch_tracker_profile(tracker_id: str) -> tuple[str | None, str | None]:
    if not TRACKER_API_KEY:
        return None, "TRACKER_API_KEY не задан в Render env."

    encoded_id = quote(tracker_id, safe="")
    urls = [
        f"https://public-api.tracker.gg/api/v1/valorant/standard/profile/riot/{encoded_id}",
        f"https://api.tracker.gg/api/v2/valorant/standard/profile/riot/{encoded_id}",
    ]
    headers = {"TRN-Api-Key": TRACKER_API_KEY}

    async with ClientSession(headers=headers) as session:
        last_error = None
        for url in urls:
            try:
                async with session.get(url, timeout=12) as response:
                    if response.status == 200:
                        data = await response.json()
                        stats = extract_tracker_stats(data)
                        if stats:
                            return stats, None
                        return None, "Tracker API ответил, но статистику не удалось разобрать."
                    text = await response.text()
                    last_error = f"Tracker API {response.status}: {text[:180]}"
            except Exception as exc:
                last_error = f"Tracker API error: {exc}"
        return None, last_error


def groq_limit_message(error: Exception, tried_models: list[str]) -> str:
    models = ", ".join(tried_models)
    if isinstance(error, RateLimitError) or getattr(error, "status_code", None) == 429:
        return (
            "Groq упёрся в лимит по моделям: "
            f"{models}. Подожди минуту, если это RPM/TPM, или до сброса дневного лимита, если RPD/TPD. "
            "Да, бот тоже иногда уходит на eco."
        )
    if isinstance(error, APIStatusError):
        return f"Groq вернул ошибку {error.status_code}. Модели пробовал: {models}."
    return "Groq сейчас не ответил. Попробуй ещё раз через пару секунд."


async def ask_groq(text: str, message: Message | None = None, tracker_data: str | None = None) -> str:
    user_content = text
    if message:
        user_content = f"{memory_context(message)}\n\nЗапрос пользователя:\n{text}"
    if tracker_data:
        user_content += f"\n\nTRACKER_DATA. Только эти цифры можно считать реальными:\n{tracker_data}"

    tried_models = []
    last_error = None
    for model in TEXT_MODELS:
        tried_models.append(model)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.35,
                max_completion_tokens=650,
            )
            return response.choices[0].message.content
        except Exception as exc:
            last_error = exc
            if not isinstance(exc, RateLimitError) and getattr(exc, "status_code", None) != 429:
                break
    raise RuntimeError(groq_limit_message(last_error or Exception(), tried_models))


def parse_memory_update(message: Message) -> str | None:
    text = normalize(strip_bot_mention(message.text or ""))
    original = strip_bot_mention(message.text or "")

    if text in {"забудь меня", "удали мой профиль", "очисти мой профиль"}:
        with db_connect() as conn:
            conn.execute(
                "DELETE FROM players WHERE chat_id = ? AND user_id = ?",
                (message.chat.id, message.from_user.id),
            )
        return "Ок, забыл твой профиль в этом чате."

    role = None
    for alias, canonical in ROLE_ALIASES.items():
        if re.search(rf"(^|\s)(я\s+)?({re.escape(alias)})(\s|$)", text):
            role = canonical
            break
    if role:
        update_player_field(message, "role", role)
        return f"Запомнил: роль - {role}."

    agent_match = re.search(r"(?:я\s+)?(?:играю\s+на|играю|мейню|мой\s+агент|агент)\s+([a-zа-я0-9/' -]{2,24})", text)
    if agent_match:
        agent = agent_match.group(1).strip(" .,!?:;")
        update_player_field(message, "agent", agent.title())
        return f"Запомнил: агент - {agent.title()}."

    rank_match = re.search(r"(?:мой\s+ранг|ранг)\s+([a-zа-я]+(?:\s*\d{1,2})?)", text)
    if rank_match:
        rank = rank_match.group(1).strip(" .,!?:;")
        update_player_field(message, "rank", rank.title())
        return f"Запомнил ранг: {rank.title()}."

    tracker_match = re.search(r"(?:мой\s+)?(?:tracker|трекер)\s*[-: ]+\s*(.+)", original, re.IGNORECASE)
    if tracker_match:
        tracker = tracker_match.group(1).strip()
        update_player_field(message, "tracker", tracker)
        return "Запомнил Tracker/Riot ID."

    note_match = re.search(r"(?:запомни|помни)\s+(.+)", original, re.IGNORECASE)
    if note_match:
        append_player_note(message, note_match.group(1).strip())
        return "Запомнил заметку."

    return None


async def send_help(message: Message):
    await message.answer(
        "Как обращаться:\n"
        f"{BOT_USERNAME} команды\n"
        f"{BOT_USERNAME} я играю Omen\n"
        f"{BOT_USERNAME} я смокер\n"
        f"{BOT_USERNAME} мой ранг gold 2\n"
        f"{BOT_USERNAME} трекер Name#TAG\n"
        f"{BOT_USERNAME} профиль\n"
        f"{BOT_USERNAME} состав\n"
        f"{BOT_USERNAME} проанализируй мой Tracker\n"
        f"{BOT_USERNAME} дай план на Ascent за атаку\n"
        f"{BOT_USERNAME} как мне улучшить игру\n"
        f"{BOT_USERNAME} разберите наш прошлый раунд\n\n"
        "Самое полезное: профиль, состав, Tracker/Riot ID, разбор ошибок, план раунда, советы по роли."
    )


async def send_profile(message: Message):
    player = get_player(message.chat.id, message.from_user.id)
    if not player:
        await message.answer(f"Пока пусто. Напиши: {BOT_USERNAME} я играю Omen, {BOT_USERNAME} я смокер, {BOT_USERNAME} мой ранг gold 2.")
        return
    await message.answer(player_line(player))


async def send_roster(message: Message):
    players = get_players(message.chat.id)
    if not players:
        await message.answer("Состав пока пустой. Пусть каждый один раз напишет свою роль/агента/ранг.")
        return
    await message.answer("Состав:\n" + "\n".join(f"- {player_line(player)}" for player in players))


async def handle_tracker(message: Message, query: str):
    tracker_id = tracker_id_from_text(query)
    if not tracker_id and message.from_user:
        player = get_player(message.chat.id, message.from_user.id)
        tracker_id = player["tracker"] if player and player["tracker"] else None

    if not tracker_id:
        await message.answer(f"Дай Riot ID: {BOT_USERNAME} трекер Name#TAG")
        return

    update_player_field(message, "tracker", tracker_id)
    await message.answer(f"Пробую достать Tracker по {tracker_id}. Если API не пустит, скажу честно, без сказок про 57.4%.")
    tracker_data, tracker_error = await fetch_tracker_profile(tracker_id)
    if not tracker_data:
        await message.answer(
            "Tracker API не дал статистику. "
            f"Причина: {tracker_error or 'нет данных'}. "
            "Скинь основные цифры текстом: winrate, K/D, ADR, ACS, HS%, KAST, топ-агенты, карты."
        )
        return

    try:
        answer = await ask_groq(
            f"Проанализируй Tracker профиль {tracker_id}. Дай выводы по игре и 3 приоритета для улучшения.",
            message,
            tracker_data=tracker_data,
        )
    except RuntimeError as exc:
        answer = str(exc)
    await message.answer(answer)


async def handle_utility(message: Message, text: str) -> bool:
    lowered = normalize(text)
    if lowered in {"", "команды", "помощь", "хелп", "что ты умеешь"}:
        await send_help(message)
        return True
    if lowered in {"профиль", "мой профиль", "кто я", "что ты обо мне знаешь"}:
        await send_profile(message)
        return True
    if lowered in {"состав", "ростер", "кто на чем играет", "кто на чём играет"}:
        await send_roster(message)
        return True
    if lowered.startswith(("трекер", "tracker")) or tracker_id_from_text(text):
        await handle_tracker(message, text)
        return True
    if lowered.startswith("карта "):
        map_name = normalize(text.partition(" ")[2])
        await message.answer(MAP_TIPS.get(map_name, "По этой карте нет заготовки, но могу разобрать конкретный план через обычный запрос."))
        return True
    if lowered in {"аим", "aim", "тренировка"}:
        await message.answer(random.choice(AIM_TIPS))
        return True
    if lowered in {"тильт", "mental", "ментал", "успокой"}:
        await message.answer(random.choice(MENTAL_TIPS))
        return True
    if lowered.startswith(("пикни", "выбери агента", "агент")):
        role_text = lowered.split(maxsplit=2)[-1] if len(lowered.split()) > 1 else ""
        role = ROLE_ALIASES.get(role_text)
        if role:
            await message.answer(f"Пик: {random.choice(AGENTS_BY_ROLE[role])}. Только без 'я не умею', уже поздно.")
        else:
            await message.answer("Напиши роль: дуэлянт, инициатор, смокер, страж.")
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


@dp.message(F.photo)
async def photo_reply(message: Message):
    store_message(message)
    if is_addressed_to_bot(message.caption or ""):
        await message.answer("Фото-анализ отключил, чтобы не сжигать vision-лимиты. Скинь цифры/ситуацию текстом, разберу нормально.")


@dp.message(F.text)
async def text_reply(message: Message):
    store_message(message)
    text = message.text or ""
    if not is_addressed_to_bot(text):
        return

    body = strip_bot_mention(text)
    memory_reply = parse_memory_update(message)
    if memory_reply:
        await message.answer(memory_reply)
        return

    if await handle_utility(message, body):
        return

    tracker_data = None
    tracker_id = tracker_id_from_text(body)
    if tracker_id and any(word in normalize(body) for word in ("трекер", "tracker", "профиль", "стат")):
        tracker_data, _tracker_error = await fetch_tracker_profile(tracker_id)

    try:
        answer = await ask_groq(body, message, tracker_data=tracker_data)
    except RuntimeError as exc:
        answer = str(exc)
    await message.answer(answer)


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
