import asyncio
import os
import re
import sqlite3
import json
from datetime import datetime, timedelta
from urllib.parse import quote

from aiohttp import ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import OpenAI, RateLimitError, APIStatusError


# =========================================================
# ENV
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY") or os.getenv("HDEV_API_KEY")

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "@latumbot").lower()
DB_PATH = os.getenv("DB_PATH", "bot.db")

TEXT_MODELS = [
    m.strip()
    for m in os.getenv(
        "GROQ_MODELS",
        "llama-3.1-8b-instant,qwen/qwen3-32b,llama-3.3-70b-versatile"
    ).split(",")
    if m.strip()
]

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

dp = Dispatcher()


# =========================================================
# DB
# =========================================================

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS players(
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            role TEXT,
            agent TEXT,
            rank TEXT,
            tracker TEXT,
            notes TEXT,
            updated TEXT,
            PRIMARY KEY(chat_id,user_id)
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS chat(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user TEXT,
            text TEXT,
            ts TEXT
        )
        """)


def now():
    return datetime.utcnow().isoformat()


# =========================================================
# MEMORY
# =========================================================

def get_player(chat_id, user_id):
    with db() as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM players WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()


def upsert_player(chat_id, user_id, name):
    with db() as c:
        c.execute("""
        INSERT OR IGNORE INTO players(chat_id,user_id,name,updated)
        VALUES(?,?,?,?)
        """, (chat_id, user_id, name, now()))


def set_field(chat_id, user_id, field, value):
    if field not in {"role", "agent", "rank", "tracker", "notes"}:
        return
    with db() as c:
        c.execute(f"""
        UPDATE players SET {field}=?, updated=?
        WHERE chat_id=? AND user_id=?
        """, (value, now(), chat_id, user_id))


def save_chat(chat_id, user, text):
    with db() as c:
        c.execute("""
        INSERT INTO chat(chat_id,user,text,ts)
        VALUES(?,?,?,?)
        """, (chat_id, user, text[:800], now()))

        c.execute("""
        DELETE FROM chat WHERE id NOT IN (
            SELECT id FROM chat ORDER BY id DESC LIMIT 60
        )
        """)


def get_chat(chat_id):
    with db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT user,text FROM chat WHERE chat_id=? ORDER BY id DESC LIMIT 10",
            (chat_id,)
        ).fetchall()

    return "\n".join([f"{r['user']}: {r['text']}" for r in rows[::-1]])


# =========================================================
# UTILS FIXED
# =========================================================

def norm(t):
    return (t or "").lower().replace("ё", "е").strip()


def is_bot(text):
    t = norm(text)
    return BOT_USERNAME in t or t.startswith("бот ")


def strip_bot(text):
    return re.sub(re.escape(BOT_USERNAME), "", text, flags=re.I).strip()


def user_name(msg):
    if msg.from_user:
        return msg.from_user.full_name
    return "player"


def extract_riot_id(text):
    match = re.findall(r"([^\s#]+)#([^\s#]+)", text)
    return match[0] if match else None


# =========================================================
# SYSTEM
# =========================================================

SYSTEM = """
Ты Valorant AI тренер.

ПРАВИЛА:
- НИКОГДА не выдумывай
- если данных нет → "нет данных"
- анализ только по входным данным
- кратко и структурно
"""


# =========================================================
# LLM
# =========================================================

async def ask_llm(prompt, msg=None):
    content = prompt

    if msg:
        content = get_chat(msg.chat.id) + "\n\n" + prompt

    for model in TEXT_MODELS:
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content}
                ],
                temperature=0.2,
                max_completion_tokens=700
            )
            return r.choices[0].message.content

        except (RateLimitError, APIStatusError):
            continue

    return "Нет ответа"


# =========================================================
# SAFE HENRIK PARSER (FIXED)
# =========================================================

cache = {}


def compact_tracker(data):
    """УБИРАЕМ МУСОР JSON -> читаемый текст"""

    if not data:
        return "нет данных"

    profile = data.get("profile", {})
    mmr = data.get("mmr", {})
    matches = data.get("matches", {})

    text = []
    text.append(f"PLAYER: {profile.get('name')}#{profile.get('tag')}")
    text.append(f"REGION: {profile.get('region')}")

    text.append("\nMMR:")
    text.append(json.dumps(mmr, ensure_ascii=False)[:500])

    text.append("\nMATCHES:")
    text.append(json.dumps(matches, ensure_ascii=False)[:800])

    return "\n".join(text)


async def fetch_tracker(rid):
    if "#" not in rid:
        return None

    if rid in cache and cache[rid]["time"] > datetime.utcnow() - timedelta(minutes=20):
        return cache[rid]["data"]

    if not HENRIK_API_KEY:
        return None

    name, tag = rid.split("#")

    headers = {"Authorization": HENRIK_API_KEY}

    async with ClientSession(headers=headers) as s:
        try:
            acc_url = f"https://api.henrikdev.xyz/valorant/v2/account/{quote(name)}/{quote(tag)}"
            async with s.get(acc_url) as r:
                if r.status != 200:
                    return None
                acc = await r.json()

            region = (acc.get("data") or {}).get("region", "eu")

            mmr_url = f"https://api.henrikdev.xyz/valorant/v3/mmr/{region}/pc/{quote(name)}/{quote(tag)}"
            match_url = f"https://api.henrikdev.xyz/valorant/v3/matches/{region}/{quote(name)}/{quote(tag)}?mode=competitive&size=5"

            async with s.get(mmr_url) as r1:
                mmr = await r1.json() if r1.status == 200 else {}

            async with s.get(match_url) as r2:
                matches = await r2.json() if r2.status == 200 else {}

            parsed = {
                "profile": {
                    "name": name,
                    "tag": tag,
                    "region": region
                },
                "mmr": mmr,
                "matches": matches
            }

            cache[rid] = {"data": parsed, "time": datetime.utcnow()}
            return parsed

        except Exception:
            return None


# =========================================================
# HANDLER FIXED
# =========================================================

@dp.message(Command("start"))
async def start(m: Message):
    save_chat(m.chat.id, user_name(m), "/start")
    await m.answer("Latumbot V5.1 активен")


@dp.message(F.text)
async def handler(m: Message):
    text = m.text or ""

    save_chat(m.chat.id, user_name(m), text)

    if not is_bot(text):
        return

    clean = strip_bot(text)

    upsert_player(m.chat.id, m.from_user.id, user_name(m))

    # roles
    if "смокер" in clean:
        set_field(m.chat.id, m.from_user.id, "role", "controller")
        return await m.answer("ок, смокер")

    if "дуэлянт" in clean:
        set_field(m.chat.id, m.from_user.id, "role", "duelist")
        return await m.answer("ок, дуэлянт")

    # tracker FIXED
    if "трекер" in clean:
        rid = extract_riot_id(clean)

        if rid:
            data = await fetch_tracker(f"{rid[0]}#{rid[1]}")
            if not data:
                return await m.answer("нет данных")

            prompt = f"""
АНАЛИЗ ИГРОКА:
{compact_tracker(data)}

1 вывод
2 ошибки
3 улучшения
"""

            ans = await ask_llm(prompt, m)
            return await m.answer(ans)

    ans = await ask_llm(clean, m)
    await m.answer(ans)


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()
    bot = Bot(TELEGRAM_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())