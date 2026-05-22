# ==============================
# VALORANT AI TELEGRAM BOT
# Production Core v2
# ==============================
# Stack:
# - aiogram 3
# - webhook mode
# - Henrik API
# - Groq API
# - SQLite
# - OCR screenshot analysis
# - structured analytics engine
# ==============================

import asyncio
import json
import logging
import os
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

import aiohttp
import cv2
import numpy as np
import pytesseract
from PIL import Image
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from openai import OpenAI

# =========================================
# ENV
# =========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "10000"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "@ailatumbot").lower()

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("Missing GROQ_API_KEY")

if not HENRIK_API_KEY:
    raise RuntimeError("Missing HENRIK_API_KEY")

if not WEBHOOK_URL:
    raise RuntimeError("Missing WEBHOOK_URL")

# =========================================
# LOGGING
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("valorant-bot")

# =========================================
# DATABASE
# =========================================

DB_PATH = "valorant_ai.sqlite3"


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                chat_id INTEGER,
                user_id INTEGER,
                name TEXT,
                role TEXT,
                rank TEXT,
                main_agent TEXT,
                tracker TEXT,
                notes TEXT,
                updated_at TEXT,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracker_cache (
                tracker TEXT PRIMARY KEY,
                data TEXT,
                updated_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                text TEXT,
                created_at TEXT
            )
            """
        )

# =========================================
# BOT
# =========================================

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

# =========================================
# OPENAI/GROQ
# =========================================

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

TEXT_MODELS = [
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct"
]

# =========================================
# SYSTEM PROMPT
# =========================================

SYSTEM_PROMPT = """
Ты профессиональный AI тренер по Valorant.

Правила:
- не выдумывай статистику;
- если данных нет — честно скажи;
- отвечай кратко;
- максимум 5 пунктов;
- анализируй только реальные цифры;
- не путай игроков;
- пиши живым русским языком;
- допускаются игровые термины;
- не используй длинные вступления;
- делай полезный анализ.
"""

# =========================================
# HELPERS
# =========================================


def now_iso():
    return datetime.now(timezone.utc).isoformat()


ROLE_ALIASES = {
    "смокер": "controller",
    "инициатор": "initiator",
    "дуэлянт": "duelist",
    "страж": "sentinel"
}

photo_waiting_users = {}

# =========================================
# TRACKER API
# =========================================


async def api_get(session, url):
    headers = {
        "Authorization": HENRIK_API_KEY
    }

    for attempt in range(3):
        try:
            async with session.get(url, headers=headers, timeout=20) as response:
                if response.status == 200:
                    return await response.json()

                text = await response.text()

                logger.warning(
                    f"Henrik API {response.status}: {text[:200]}"
                )

                if response.status == 429:
                    await asyncio.sleep(3)
                    continue

                return None

        except Exception as exc:
            logger.exception(exc)
            await asyncio.sleep(2)

    return None


async def fetch_player_data(tracker: str):
    if "#" not in tracker:
        return None

    name, tag = tracker.split("#", 1)

    async with aiohttp.ClientSession() as session:

        account_url = (
            f"https://api.henrikdev.xyz/valorant/v2/account/{name}/{tag}"
        )

        account = await api_get(session, account_url)

        if not account:
            return None

        region = (
            account.get("data", {})
            .get("region", "eu")
        )

        mmr_url = (
            f"https://api.henrikdev.xyz/valorant/v3/mmr/{region}/pc/{name}/{tag}"
        )

        matches_url = (
            f"https://api.henrikdev.xyz/valorant/v3/matches/{region}/{name}/{tag}?size=10"
        )

        mmr = await api_get(session, mmr_url)
        matches = await api_get(session, matches_url)

        return {
            "account": account,
            "mmr": mmr,
            "matches": matches
        }

# =========================================
# ANALYTICS ENGINE
# =========================================


def analyze_matches(data: dict):
    matches = data.get("matches", {}).get("data", [])

    if not matches:
        return {
            "summary": "Нет матчей",
            "stats": {}
        }

    kills = []
    deaths = []
    assists = []
    adr_values = []
    hs_values = []
    maps = []
    agents = []
    wins = 0

    first_death_matches = 0

    for match in matches:
        metadata = match.get("metadata", {})
        players = (
            match.get("players", {})
            .get("all_players", [])
        )

        if not players:
            continue

        player = players[0]

        stats = player.get("stats", {})

        k = stats.get("kills", 0)
        d = stats.get("deaths", 1)
        a = stats.get("assists", 0)

        kills.append(k)
        deaths.append(d)
        assists.append(a)

        damage = player.get("damage_made", 0)
        rounds = metadata.get("rounds_played", 1)

        adr = damage / max(rounds, 1)
        adr_values.append(adr)

        headshots = stats.get("headshots", 0)
        bodyshots = stats.get("bodyshots", 0)
        legshots = stats.get("legshots", 0)

        total_shots = headshots + bodyshots + legshots

        hs = 0

        if total_shots > 0:
            hs = (headshots / total_shots) * 100

        hs_values.append(hs)

        maps.append(metadata.get("map", "Unknown"))
        agents.append(player.get("character", "Unknown"))

        if player.get("team") == "Blue":
            blue = match.get("teams", {}).get("blue", {})
            if blue.get("has_won"):
                wins += 1

        if d >= 20:
            first_death_matches += 1

    avg_kd = sum(kills) / max(sum(deaths), 1)
    avg_adr = statistics.mean(adr_values)
    avg_hs = statistics.mean(hs_values)

    best_agent = Counter(agents).most_common(1)[0][0]
    best_map = Counter(maps).most_common(1)[0][0]

    problems = []

    if avg_hs < 18:
        problems.append("низкий процент попаданий в голову")

    if avg_adr < 120:
        problems.append("низкий средний урон")

    if avg_kd < 1:
        problems.append("слишком много смертей")

    if first_death_matches >= 4:
        problems.append("часто погибаешь слишком рано")

    return {
        "stats": {
            "kd": round(avg_kd, 2),
            "adr": round(avg_adr, 1),
            "hs": round(avg_hs, 1),
            "winrate": round((wins / max(len(matches), 1)) * 100, 1),
            "best_agent": best_agent,
            "best_map": best_map,
        },
        "problems": problems,
    }

# =========================================
# LLM
# =========================================


async def ask_llm(prompt: str):
    last_error = None

    for model in TEXT_MODELS:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.3,
                max_completion_tokens=700
            )

            return response.choices[0].message.content

        except Exception as exc:
            logger.exception(exc)
            last_error = exc

    return f"Ошибка модели: {last_error}"

# =========================================
# OCR
# =========================================


async def analyze_scoreboard_image(file_bytes: bytes):
    image = Image.open(BytesIO(file_bytes))

    image_np = np.array(image)

    gray = cv2.cvtColor(image_np, cv2.COLOR_BGR2GRAY)

    processed = cv2.threshold(
        gray,
        140,
        255,
        cv2.THRESH_BINARY
    )[1]

    text = pytesseract.image_to_string(
        processed,
        lang="eng"
    )

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    players = []

    for line in lines:
        if re.search(r"\d+/\d+/\d+", line):
            players.append(line)

    return {
        "raw": text[:4000],
        "players": players[:10]
    }

# =========================================
# COMMANDS
# =========================================


@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer(
        "AI тренер Valorant активен.\n\n"
        "Команды:\n"
        "/профиль\n"
        "/разбор\n"
        "/последняя\n"
        "/фото\n"
        "/состав\n"
    )


@dp.message(Command("фото"))
async def photo_mode(message: Message):
    photo_waiting_users[message.from_user.id] = True

    await message.answer(
        "Отправь скрин scoreboard или статистики."
    )


@dp.message(F.photo)
async def photo_handler(message: Message):

    waiting = photo_waiting_users.get(message.from_user.id)

    if not waiting:
        return

    photo_waiting_users.pop(message.from_user.id, None)

    await message.answer("Анализирую скрин...")

    try:
        photo = message.photo[-1]

        file = await bot.get_file(photo.file_id)

        downloaded = await bot.download_file(file.file_path)

        file_bytes = downloaded.read()

        result = await analyze_scoreboard_image(file_bytes)

        prompt = f"""
        Вот OCR со scoreboard Valorant.

        Игроки:
        {result['players']}

        Сырой текст:
        {result['raw']}

        Сделай краткий анализ.
        """

        answer = await ask_llm(prompt)

        await message.answer(answer)

    except Exception as exc:
        logger.exception(exc)
        await message.answer(f"Ошибка анализа фото: {exc}")


@dp.message(Command("разбор"))
async def analysis_command(message: Message):

    with db_connect() as conn:
        row = conn.execute(
            "SELECT tracker FROM players WHERE chat_id=? AND user_id=?",
            (message.chat.id, message.from_user.id)
        ).fetchone()

    if not row:
        await message.answer(
            "Сначала укажи трекер: бот трекер Name#TAG"
        )
        return

    tracker = row[0]

    await message.answer("Получаю матчи...")

    data = await fetch_player_data(tracker)

    if not data:
        await message.answer("Не удалось получить данные Henrik API")
        return

    analytics = analyze_matches(data)

    prompt = f"""
    Статистика игрока:

    {json.dumps(analytics, ensure_ascii=False, indent=2)}

    Сделай профессиональный разбор.
    """

    answer = await ask_llm(prompt)

    await message.answer(answer)


@dp.message(F.text)
async def text_handler(message: Message):

    text = message.text.lower()

    if not (
        BOT_USERNAME in text
        or text.startswith("бот")
        or text.startswith("вал")
    ):
        return

    cleaned = (
        text
        .replace(BOT_USERNAME, "")
        .replace("бот", "")
        .strip()
    )

    tracker_match = re.search(
        r"([A-Za-z0-9_.-]{2,24}#[A-Za-z0-9]{2,8})",
        cleaned
    )

    if tracker_match:
        tracker = tracker_match.group(1)

        with db_connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO players (
                    chat_id,
                    user_id,
                    name,
                    tracker,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.chat.id,
                    message.from_user.id,
                    message.from_user.full_name,
                    tracker,
                    now_iso()
                )
            )

        await message.answer(
            f"Запомнил трекер: {tracker}"
        )

        return

    if "последняя игра" in cleaned:

        with db_connect() as conn:
            row = conn.execute(
                "SELECT tracker FROM players WHERE chat_id=? AND user_id=?",
                (message.chat.id, message.from_user.id)
            ).fetchone()

        if not row:
            await message.answer("Сначала укажи трекер")
            return

        tracker = row[0]

        data = await fetch_player_data(tracker)

        if not data:
            await message.answer("Henrik API не ответил")
            return

        matches = data.get("matches", {}).get("data", [])

        if not matches:
            await message.answer("Матчи не найдены")
            return

        last_match = matches[0]

        prompt = f"""
        Последний матч:

        {json.dumps(last_match, ensure_ascii=False)[:6000]}

        Сделай разбор.
        """

        answer = await ask_llm(prompt)

        await message.answer(answer)

        return

    try:
        answer = await ask_llm(cleaned)
        await message.answer(answer)

    except Exception as exc:
        logger.exception(exc)
        await message.answer(f"Ошибка: {exc}")

# =========================================
# ERROR MIDDLEWARE
# =========================================


@dp.errors()
async def global_error_handler(event):
    logger.exception(event.exception)
    return True

# =========================================
# WEBHOOK
# =========================================


async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info("Webhook set")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logger.info("Webhook deleted")

# =========================================
# MAIN
# =========================================


async def main():

    init_db()

    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    ).register(app, path="/")

    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT
    )

    logger.info(f"Starting webhook server on {PORT}")

    await site.start()

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
