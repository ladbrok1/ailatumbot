import asyncio
import os
import re
import sqlite3
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramConflictError
from openai import OpenAI, RateLimitError, APIStatusError


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# =========================================================
# ENV
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY") or os.getenv("HDEV_API_KEY")

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "@ailatumbot").lower()
if not BOT_USERNAME.startswith("@"):
    BOT_USERNAME = "@" + BOT_USERNAME

DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = os.getenv("PORT")

# Models and cooldowns
TEXT_MODELS = [
    m.strip()
    for m in os.getenv(
        "GROQ_MODELS",
        "llama-3.1-8b-instant,qwen/qwen3-32b,llama-3.3-70b-versatile"
    ).split(",")
    if m.strip()
]
MODEL_COOLDOWN_SECONDS = int(os.getenv("MODEL_COOLDOWN_SECONDS", "90"))
MODEL_COOLDOWNS: dict[str, datetime] = {}

# Require critical envs early
if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN is not set")
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

if not GROQ_API_KEY:
    log.error("GROQ_API_KEY is not set")
    raise RuntimeError("Set GROQ_API_KEY environment variable")

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

dp = Dispatcher()


# =========================================================
# Small helpers
# =========================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


# =========================================================
# DB helpers (sync) and async wrappers
# =========================================================

def db():
    # per-operation connection with pragmas to reduce locking
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


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


# synchronous DB operations
def _get_player(chat_id, user_id):
    with db() as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM players WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()


def _upsert_player(chat_id, user_id, name):
    with db() as c:
        c.execute("""
        INSERT OR IGNORE INTO players(chat_id,user_id,name,updated)
        VALUES(?,?,?,?)
        """, (chat_id, user_id, name, now_iso()))


def _set_field(chat_id, user_id, field, value):
    if field not in {"role", "agent", "rank", "tracker", "notes"}:
        return
    with db() as c:
        c.execute(f"""
        UPDATE players SET {field}=?, updated=?
        WHERE chat_id=? AND user_id=?
        """, (value, now_iso(), chat_id, user_id))


def _save_chat(chat_id, user, text):
    with db() as c:
        c.execute("""
        INSERT INTO chat(chat_id,user,text,ts)
        VALUES(?,?,?,?)
        """, (chat_id, user, text[:800], now_iso()))

        c.execute("""
        DELETE FROM chat WHERE id NOT IN (
            SELECT id FROM chat ORDER BY id DESC LIMIT 60
        )
        """)


def _get_chat(chat_id):
    with db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT user,text FROM chat WHERE chat_id=? ORDER BY id DESC LIMIT 10",
            (chat_id,)
        ).fetchall()

    return "\n".join([f"{r['user']}: {r['text']}" for r in rows[::-1]])


# async wrappers to avoid blocking the event loop
async def get_player(chat_id, user_id):
    return await asyncio.to_thread(_get_player, chat_id, user_id)


async def upsert_player(chat_id, user_id, name):
    return await asyncio.to_thread(_upsert_player, chat_id, user_id, name)


async def set_field(chat_id, user_id, field, value):
    return await asyncio.to_thread(_set_field, chat_id, user_id, field, value)


async def save_chat(chat_id, user, text):
    return await asyncio.to_thread(_save_chat, chat_id, user, text)


async def get_chat(chat_id):
    return await asyncio.to_thread(_get_chat, chat_id)


# =========================================================
# UTILS
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
        return msg.from_user.full_name or msg.from_user.username or str(msg.from_user.id)
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
# Model helpers
# =========================================================

def active_models() -> list[str]:
    now = now_utc()
    return [m for m in TEXT_MODELS if MODEL_COOLDOWNS.get(m, now - timedelta(seconds=1)) <= now]


def groq_limit_message(error: Exception, tried_models: list[str]) -> str:
    models = ", ".join(tried_models) or "нет доступных моделей"
    if isinstance(error, RateLimitError) or getattr(error, "status_code", None) == 429:
        return (
            f"Groq лимит. Пробовал: {models}. Подожди {MODEL_COOLDOWN_SECONDS} секунд или снизь частоту запросов."
        )
    if isinstance(error, APIStatusError):
        return f"Groq вернул ошибку {error.status_code}. Модели: {models}."
    return "Groq не ответил. Попробуй ещё раз позже."


# =========================================================
# LLM
# =========================================================

async def ask_llm(prompt, msg=None, max_tokens: int = 700):
    content = prompt

    if msg:
        hist = await get_chat(msg.chat.id)
        content = hist + "\n\n" + prompt

    tried = []
    last_exc = None
    models = active_models() or TEXT_MODELS
    for model in models:
        tried.append(model)
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content}
                ],
                temperature=0.2,
                max_completion_tokens=max_tokens,
                timeout=30
            )
            return r.choices[0].message.content

        except Exception as exc:
            last_exc = exc
            # Rate limit or 429 -> set cooldown for this model
            if isinstance(exc, RateLimitError) or getattr(exc, "status_code", None) == 429:
                MODEL_COOLDOWNS[model] = now_utc() + timedelta(seconds=MODEL_COOLDOWN_SECONDS)
                log_msg = f"Model {model} rate-limited; cooling down until {MODEL_COOLDOWNS[model].isoformat()}"
                log.warning(log_msg)
                continue
            if isinstance(exc, APIStatusError):
                log.warning("Model %s returned APIStatusError: %s", model, exc)
                continue
            log.exception("LLM call failed for model %s", model)
            continue

    # all failed
    raise RuntimeError(groq_limit_message(last_exc or Exception(), tried))


# =========================================================
# SAFE HENRIK PARSER
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


async def fetch_tracker(rid, retries: int = 2, timeout_seconds: int = 10):
    if "#" not in rid:
        return None

    # normalize
    rid = rid.strip()

    if rid in cache and cache[rid]["time"] > now_utc() - timedelta(minutes=20):
        return cache[rid]["data"]

    if not HENRIK_API_KEY:
        return None

    name, tag = rid.split("#", 1)
    headers = {"Authorization": HENRIK_API_KEY}
    timeout = ClientTimeout(total=timeout_seconds)

    attempt = 0
    while attempt <= retries:
        attempt += 1
        try:
            async with ClientSession(headers=headers, timeout=timeout) as s:
                acc_url = f"https://api.henrikdev.xyz/valorant/v2/account/{quote(name)}/{quote(tag)}"
                async with s.get(acc_url) as r:
                    if r.status != 200:
                        log.warning("Henrik account API returned %s for %s", r.status, rid)
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

                cache[rid] = {"data": parsed, "time": now_utc()}
                return parsed

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Henrik fetch attempt %s failed for %s: %s", attempt, rid, exc)
            if attempt > retries:
                return None
            await asyncio.sleep(1 + attempt)


# =========================================================
# HANDLERS
# =========================================================


# Helper to find last Riot ID mentioned in recent chat history
async def find_last_rid(chat_id: int):
    def _find(chat_id):
        with db() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT text FROM chat WHERE chat_id=? ORDER BY id DESC LIMIT 60",
                (chat_id,)
            ).fetchall()
            for r in rows:
                txt = r["text"] or ""
                match = re.findall(r"([^\s#]+)#([^\s#]+)", txt)
                if match:
                    return f"{match[0][0]}#{match[0][1]}"
            return None
    return await asyncio.to_thread(_find, chat_id)


# Try to summarize basic metrics from Henrik matches payload defensively
def summarize_matches(matches):
    # matches may be a dict or list; try common keys
    lst = None
    if not matches:
        return None
    if isinstance(matches, list):
        lst = matches
    elif isinstance(matches, dict):
        lst = matches.get("data") or matches.get("matches") or matches.get("results")
    if not lst or not isinstance(lst, list):
        return None

    total = len(lst)
    wins = 0
    losses = 0
    kills = 0
    deaths = 0
    scores = 0

    for m in lst:
        # try several possible shapes
        stats = m.get("stats") or m.get("player_stats") or m
        k = stats.get("kills") if isinstance(stats, dict) else None
        d = stats.get("deaths") if isinstance(stats, dict) else None
        s = stats.get("score") if isinstance(stats, dict) else None
        # fallback top-level
        k = k or m.get("kills") or m.get("kills_total")
        d = d or m.get("deaths") or m.get("deaths_total")
        s = s or m.get("score") or m.get("points")

        try:
            kills += int(k or 0)
        except Exception:
            pass
        try:
            deaths += int(d or 0)
        except Exception:
            pass
        try:
            scores += int(s or 0)
        except Exception:
            pass

        # win detection
        result = (m.get("team" ) or {}).get("result") if isinstance(m.get("team"), dict) else m.get("result")
        if isinstance(result, str) and result.lower() in {"win", "w", "victory"}:
            wins += 1
        elif isinstance(result, str) and result.lower() in {"loss", "l", "defeat"}:
            losses += 1
        else:
            # try booleans
            if m.get("won") is True or m.get("is_win") is True:
                wins += 1
            elif m.get("won") is False or m.get("is_win") is False:
                losses += 1

    avg_k = kills / total if total else 0
    avg_d = deaths / total if total else 0
    avg_s = scores / total if total else 0

    return {
        "matches": total,
        "wins": wins,
        "losses": losses,
        "kills": kills,
        "deaths": deaths,
        "avg_kills": round(avg_k, 2),
        "avg_deaths": round(avg_d, 2),
        "avg_score": round(avg_s, 2),
    }


def format_metrics(profile, mmr, matches):
    parts = []
    parts.append(f"PLAYER: {profile.get('name')}#{profile.get('tag')}")
    parts.append(f"REGION: {profile.get('region')}")
    if isinstance(mmr, dict):
        mmr_val = mmr.get('mmr') or mmr.get('rating') or mmr.get('current') or str(mmr)
        parts.append(f"MMR summary: {mmr_val}")
    else:
        parts.append(f"MMR: {mmr}")

    s = summarize_matches(matches)
    if s:
        parts.append("\nMATCHES SUMMARY:")
        parts.append(f"Total matches: {s['matches']}")
        parts.append(f"Wins: {s['wins']}, Losses: {s['losses']}")
        parts.append(f"Kills/Deaths: {s['kills']}/{s['deaths']}")
        parts.append(f"Avg Kills: {s['avg_kills']}, Avg Deaths: {s['avg_deaths']}, Avg Score: {s['avg_score']}")
    else:
        parts.append("Matches: нет подробных данных")

    return "\n".join(parts)

@dp.message(Command("start"))
async def start(m: Message):
    await save_chat(m.chat.id, user_name(m), "/start")
    await m.answer("Latumbot: активен")


@dp.message(F.text)
async def handler(m: Message):
    text = m.text or ""

    # non-blocking DB writes
    await save_chat(m.chat.id, user_name(m), text)

    # Log incoming message briefly for debugging
    log.info("Handling message from %s (%s) in chat %s: %s", user_name(m), getattr(m.from_user, 'id', None), m.chat.id, (text or '')[:120])

    # Respond when: bot is mentioned, message is a command, or in private chat
    if not is_bot(text) and getattr(m.chat, 'type', None) != 'private' and not (text or '').strip().startswith("/"):
        return

    clean = strip_bot(text)

    await upsert_player(m.chat.id, m.from_user.id, user_name(m))

    # roles
    if "смокер" in clean:
        await set_field(m.chat.id, m.from_user.id, "role", "controller")
        return await m.answer("по памяти: записал — смокер")

    if "дуэлянт" in clean:
        await set_field(m.chat.id, m.from_user.id, "role", "duelist")
        return await m.answer("по памяти: записал — дуэлянт")

    # tracker
    if "трекер" in clean:
        rid = extract_riot_id(clean)
        lower = (clean or "").lower()

        # If Riot ID provided inline, use it. Otherwise try to resolve from recent chat history when user asks "скинь все" or similar follow-up
        if not rid:
            # follow-up requests like "по трекеру скажи какие показатели" or "скинь все"
            if any(k in lower for k in ("скинь", "все", "показатель", "какие показатели", "дай все")):
                rid_str = await find_last_rid(m.chat.id)
                if not rid_str:
                    return await m.answer("Не нашёл последнего упоминания RiotID в чате. Пожалуйста, укажи RiotID в формате name#tag.")
            else:
                return await m.answer("Пожалуйста, укажи RiotID в формате name#tag, например: aisokuro#ako")
        else:
            rid_str = f"{rid[0]}#{rid[1]}"

        data = await fetch_tracker(rid_str)
        if not data:
            return await m.answer("По Tracker: нет данных или Henrik API не доступен.")

        # If user explicitly asked to "скинь все" or "все показатели", provide a full metrics dump
        if any(k in lower for k in ("скинь", "все", "всё", "показатель", "все показатели")):
            try:
                profile = data.get("profile", {})
                mmr = data.get("mmr", {})
                matches = data.get("matches", {})
                text = format_metrics(profile, mmr, matches)
                sent = await m.answer(text)
                log.info("Sent full tracker metrics to chat %s for %s", m.chat.id, rid_str)
                return sent
            except Exception as exc:
                log.exception("Failed to format full metrics: %s", exc)
                return await m.answer("Не удалось собрать полные показатели. Попробуйте позже.")

        # Default behavior: short compact analysis via LLM
        prompt = f"""
АНАЛИЗ ИГРОКА:
{compact_tracker(data)}

Дай короткий вывод и 3 конкретных действия. Без выдумок.
"""

        try:
            ans = await ask_llm(prompt, m)
        except RuntimeError as exc:
            ans = str(exc)
        except Exception as exc:
            log.exception("LLM failed: %s", exc)
            ans = "Ошибка LLM. Попробуйте позже."
        sent = await m.answer(ans)
        log.info("Sent tracker reply to chat %s", m.chat.id)
        return sent

    try:
        ans = await ask_llm(clean, m)
    except RuntimeError as exc:
        ans = str(exc)
    except Exception as exc:
        log.exception("LLM failed: %s", exc)
        ans = "Ошибка LLM. Попробуйте позже."

    sent = await m.answer(ans)
    log.info("Sent reply to chat %s", m.chat.id)


# =========================================================
# HEALTH SERVER (optional)
# =========================================================

async def health_check(_request):
    return web.Response(text="ok")


async def start_health_server():
    if not PORT or not PORT.isdigit():
        log.info("PORT not set or invalid; skipping health server")
        return None

    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(PORT))
    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)
    return runner


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    health_runner = None
    bot = None
    try:
        health_runner = await start_health_server()

        bot = Bot(TELEGRAM_TOKEN)

        # ensure webhook is cleared to avoid TelegramConflictError
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except TelegramConflictError:
            log.warning("Webhook conflict when deleting webhook; continuing")
        except Exception:
            log.exception("Failed to delete webhook; continuing")

        # resilient polling loop: restart on unexpected crashes with backoff
        attempts = 0
        while True:
            try:
                await dp.start_polling(bot)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                log.exception("Polling crashed (attempt %s): %s", attempts, exc)
                if attempts >= 5:
                    log.error("Too many polling failures; exiting")
                    raise
                backoff = min(30, attempts * 5)
                await asyncio.sleep(backoff)

    finally:
        # cleanup
        try:
            if health_runner:
                await health_runner.cleanup()
        except Exception:
            log.exception("Failed to cleanup health server")
        try:
            if bot is not None:
                await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested")
