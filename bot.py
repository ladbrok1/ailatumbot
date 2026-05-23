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

        c.execute("""
        CREATE TABLE IF NOT EXISTS player_profiles (
            riot_id TEXT PRIMARY KEY,
            name TEXT,
            tag TEXT,
            region TEXT,
            rank TEXT,
            mmr INTEGER,
            last_updated TEXT,
            raw_data TEXT
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS match_history (
            id TEXT PRIMARY KEY,
            riot_id TEXT,
            map TEXT,
            agent TEXT,
            kills INTEGER,
            deaths INTEGER,
            assists INTEGER,
            damage_dealt INTEGER,
            damage_taken INTEGER,
            result TEXT,
            date TEXT,
            FOREIGN KEY(riot_id) REFERENCES player_profiles(riot_id)
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS player_analytics (
            riot_id TEXT PRIMARY KEY,
            total_matches INTEGER,
            win_rate REAL,
            avg_kills REAL,
            avg_deaths REAL,
            avg_kda REAL,
            main_agent TEXT,
            main_map TEXT,
            recent_form TEXT,
            last_computed TEXT,
            FOREIGN KEY(riot_id) REFERENCES player_profiles(riot_id)
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS player_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            riot_id TEXT,
            win_rate REAL,
            avg_kda REAL,
            total_matches INTEGER,
            snapshot_date TEXT,
            FOREIGN KEY(riot_id) REFERENCES player_profiles(riot_id)
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS player_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            riot_id TEXT,
            alert_type TEXT,
            threshold REAL,
            enabled INTEGER DEFAULT 1,
            created_date TEXT,
            UNIQUE(chat_id, riot_id, alert_type)
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
# SMART ANALYZER (парсинг и анализ данных вместо слепого LLM)
# =========================================================

def parse_matches(matches_payload):
    """Парсим матчи из Henrik API в структурированные данные"""
    if not matches_payload or not isinstance(matches_payload, dict):
        return []

    data_list = matches_payload.get("data", [])
    if not isinstance(data_list, list):
        return []

    parsed = []
    for m in data_list:
        try:
            stats = m.get("stats", {})
            team = m.get("team", {})
            metadata = m.get("metadata", {})

            agent = m.get("character", {})
            agent_name = agent.get("name") if isinstance(agent, dict) else str(agent)

            kills = stats.get("kills", 0) if isinstance(stats, dict) else 0
            deaths = stats.get("deaths", 0) if isinstance(stats, dict) else 0
            assists = stats.get("assists", 0) if isinstance(stats, dict) else 0
            damage = stats.get("damage", {}) if isinstance(stats, dict) else {}
            damage_dealt = damage.get("dealt", 0) if isinstance(damage, dict) else 0
            damage_taken = damage.get("taken", 0) if isinstance(damage, dict) else 0

            map_name = metadata.get("map", {})
            map_name = map_name.get("name") if isinstance(map_name, dict) else str(map_name)

            result = team.get("result", "unknown") if isinstance(team, dict) else "unknown"
            result = result.lower()

            date_str = m.get("started_at", now_iso())

            parsed.append({
                "agent": agent_name,
                "map": map_name,
                "kills": int(kills) if kills else 0,
                "deaths": int(deaths) if deaths else 0,
                "assists": int(assists) if assists else 0,
                "damage_dealt": int(damage_dealt) if damage_dealt else 0,
                "damage_taken": int(damage_taken) if damage_taken else 0,
                "result": result,
                "date": date_str
            })
        except Exception as e:
            log.warning("Failed to parse match: %s", e)
            continue

    return parsed


def compute_analytics(riot_id, parsed_matches):
    """Вычисляем аналитику на основе последних матчей"""
    if not parsed_matches:
        return None

    total = len(parsed_matches)
    wins = sum(1 for m in parsed_matches if m["result"] in {"win", "victory"})
    total_kills = sum(m["kills"] for m in parsed_matches)
    total_deaths = sum(m["deaths"] for m in parsed_matches)
    total_assists = sum(m["assists"] for m in parsed_matches)
    total_damage = sum(m["damage_dealt"] for m in parsed_matches)

    avg_kills = total_kills / total if total else 0
    avg_deaths = total_deaths / total if total else 0
    avg_kda = (total_kills + total_assists) / max(total_deaths, 1)
    win_rate = (wins / total * 100) if total else 0

    # Main agent and map
    agent_counts = {}
    map_counts = {}
    for m in parsed_matches:
        agent_counts[m["agent"]] = agent_counts.get(m["agent"], 0) + 1
        map_counts[m["map"]] = map_counts.get(m["map"], 0) + 1

    main_agent = max(agent_counts.items(), key=lambda x: x[1])[0] if agent_counts else "unknown"
    main_map = max(map_counts.items(), key=lambda x: x[1])[0] if map_counts else "unknown"

    # Recent form (last 5 matches)
    recent = parsed_matches[:5]
    recent_wins = sum(1 for m in recent if m["result"] in {"win", "victory"})
    recent_form = f"{recent_wins}/5W" if len(recent) == 5 else f"{recent_wins}/{len(recent)}W"

    return {
        "total_matches": total,
        "win_rate": round(win_rate, 1),
        "avg_kills": round(avg_kills, 2),
        "avg_deaths": round(avg_deaths, 2),
        "avg_kda": round(avg_kda, 2),
        "main_agent": main_agent,
        "main_map": main_map,
        "recent_form": recent_form,
    }


def save_parsed_data(riot_id, profile, analytics, parsed_matches):
    """Сохраняем распарсенные данные в БД"""
    def _save(riot_id, profile, analytics, parsed_matches):
        with db() as c:
            name, tag = riot_id.split("#", 1)

            c.execute("""
                INSERT OR REPLACE INTO player_profiles
                (riot_id, name, tag, region, rank, mmr, last_updated, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (riot_id, name, tag, profile.get("region"), profile.get("rank", "unknown"),
                  profile.get("mmr", 0), now_iso(), json.dumps(profile)))

            if analytics:
                c.execute("""
                    INSERT OR REPLACE INTO player_analytics
                    (riot_id, total_matches, win_rate, avg_kills, avg_deaths, avg_kda,
                     main_agent, main_map, recent_form, last_computed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (riot_id, analytics["total_matches"], analytics["win_rate"],
                      analytics["avg_kills"], analytics["avg_deaths"], analytics["avg_kda"],
                      analytics["main_agent"], analytics["main_map"],
                      analytics["recent_form"], now_iso()))

            # Save match history (last 20)
            for i, m in enumerate(parsed_matches[:20]):
                match_id = f"{riot_id}_{i}_{m['date']}"
                c.execute("""
                    INSERT OR IGNORE INTO match_history
                    (id, riot_id, map, agent, kills, deaths, assists,
                     damage_dealt, damage_taken, result, date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (match_id, riot_id, m["map"], m["agent"], m["kills"],
                      m["deaths"], m["assists"], m["damage_dealt"],
                      m["damage_taken"], m["result"], m["date"]))

    return await asyncio.to_thread(_save, riot_id, profile, analytics, parsed_matches)


async def get_analytics(riot_id):
    """Получить сохранённую аналитику из БД"""
    def _get(riot_id):
        with db() as c:
            c.row_factory = sqlite3.Row
            return c.execute(
                "SELECT * FROM player_analytics WHERE riot_id=?",
                (riot_id,)
            ).fetchone()

    return await asyncio.to_thread(_get, riot_id)


def format_analysis(riot_id, profile, analytics):
    """Генерируем читаемый анализ на основе реальных данных"""
    if not analytics:
        return "Недостаточно данных для анализа."

    lines = []
    lines.append(f"📊 **{profile.get('name')}#{profile.get('tag')}** [{profile.get('rank', '?')}]")
    lines.append(f"🌍 Region: {profile.get('region', 'unknown')}")
    lines.append("")

    lines.append("**СТАТИСТИКА (последние матчи):**")
    lines.append(f"• Матчей: {analytics['total_matches']}")
    lines.append(f"• Winrate: {analytics['win_rate']}%")
    lines.append(f"• Avg K/D/A: {analytics['avg_kills']}/{analytics['avg_deaths']}/{0}  (KDA: {analytics['avg_kda']})")
    lines.append(f"• Main Agent: {analytics['main_agent']}")
    lines.append(f"• Main Map: {analytics['main_map']}")
    lines.append(f"• Recent Form: {analytics['recent_form']}")
    lines.append("")

    lines.append("**РЕКОМЕНДАЦИИ:**")
    if analytics['win_rate'] < 45:
        lines.append("⚠️ Низкий винрейт. Анализируй сыгранные матчи, меняй стратегию.")
    elif analytics['win_rate'] > 55:
        lines.append("✅ Хороший винрейт! Продолжай в том же духе.")

    if analytics['avg_kda'] < 1.0:
        lines.append("⚠️ KDA ниже 1.0 - работай над позиционированием и умением читать карту.")
    elif analytics['avg_kda'] > 1.5:
        lines.append("✅ KDA выше 1.5 - сильный фраг. Стабилизируй перформанс.")

    lines.append(f"📌 Сосредоточься на {analytics['main_agent']} - это твой сильнейший агент.")
    lines.append(f"🗺️ На {analytics['main_map']} у тебя лучше всего получается - анализируй свои выигрыши там.")

    return "\n".join(lines)


async def save_snapshot(riot_id, analytics):
    """Сохраняем снимок аналитики для отслеживания прогресса"""
    def _save(riot_id, analytics):
        with db() as c:
            c.execute("""
                INSERT INTO player_snapshots (riot_id, win_rate, avg_kda, total_matches, snapshot_date)
                VALUES (?, ?, ?, ?, ?)
            """, (riot_id, analytics['win_rate'], analytics['avg_kda'],
                  analytics['total_matches'], now_iso()))
    return await asyncio.to_thread(_save, riot_id, analytics)


async def get_progress(riot_id):
    """Получить прогресс игрока за последние 30 дней"""
    def _get(riot_id):
        with db() as c:
            c.row_factory = sqlite3.Row
            cutoff = (now_utc() - timedelta(days=30)).isoformat()
            rows = c.execute("""
                SELECT snapshot_date, win_rate, avg_kda, total_matches
                FROM player_snapshots
                WHERE riot_id=? AND snapshot_date >= ?
                ORDER BY snapshot_date ASC
            """, (riot_id, cutoff)).fetchall()
            return rows
    return await asyncio.to_thread(_get, riot_id)


def format_progress(riot_id, snapshots):
    """Форматируем прогресс в читаемый вид"""
    if not snapshots:
        return f"Нет истории для {riot_id}. Используй трекер несколько раз."

    lines = []
    lines.append(f"📈 **ПРОГРЕСС {riot_id} (последние 30 дней)**")
    lines.append("")

    first = snapshots[0]
    last = snapshots[-1]

    wr_change = last['win_rate'] - first['win_rate']
    kda_change = last['avg_kda'] - first['avg_kda']
    matches_change = last['total_matches'] - first['total_matches']

    lines.append(f"**WINRATE:**")
    lines.append(f"  • Было: {first['win_rate']}% | Стало: {last['win_rate']}%")
    if wr_change > 0:
        lines.append(f"  • 📈 +{wr_change:.1f}% (идёшь вверх!)")
    elif wr_change < 0:
        lines.append(f"  • 📉 {wr_change:.1f}% (упал винрейт)")
    else:
        lines.append(f"  • ➡️ Без изменений")

    lines.append(f"")
    lines.append(f"**KDA:**")
    lines.append(f"  • Было: {first['avg_kda']} | Стало: {last['avg_kda']}")
    if kda_change > 0:
        lines.append(f"  • 📈 +{kda_change:.2f} (улучшаешься!)")
    elif kda_change < 0:
        lines.append(f"  • 📉 {kda_change:.2f} (упал KDA)")
    else:
        lines.append(f"  • ➡️ Стабильно")

    lines.append(f"")
    lines.append(f"**МАТЧИ:**")
    lines.append(f"  • Сыграно: +{matches_change} матчей")

    lines.append(f"")
    if len(snapshots) >= 5:
        mid_idx = len(snapshots) // 2
        mid = snapshots[mid_idx]
        lines.append(f"**СЕРЕДИНА ПЕРИОДА (прогресс из {len(snapshots)} снимков):**")
        lines.append(f"  • WR: {mid['win_rate']}%")
        lines.append(f"  • KDA: {mid['avg_kda']}")

    return "\n".join(lines)


async def get_vs_data(rid1, rid2):
    """Получить данные двух игроков для сравнения"""
    def _get(rid1, rid2):
        with db() as c:
            c.row_factory = sqlite3.Row
            profile1 = c.execute("SELECT * FROM player_analytics WHERE riot_id=?", (rid1,)).fetchone()
            profile2 = c.execute("SELECT * FROM player_analytics WHERE riot_id=?", (rid2,)).fetchone()
            return profile1, profile2
    return await asyncio.to_thread(_get, rid1, rid2)


def format_vs(rid1, data1, rid2, data2):
    """Сравнение двух игроков"""
    if not data1 or not data2:
        return "Недостаточно данных для сравнения. Проверь оба RiotID."

    lines = []
    lines.append(f"⚔️ **СРАВНЕНИЕ: {rid1} vs {rid2}**")
    lines.append("")

    # WinRate
    lines.append(f"**WINRATE:**")
    lines.append(f"  {rid1}: {data1['win_rate']}%")
    lines.append(f"  {rid2}: {data2['win_rate']}%")
    if data1['win_rate'] > data2['win_rate']:
        lines.append(f"  ✅ {rid1} лучше на {data1['win_rate'] - data2['win_rate']:.1f}%")
    else:
        lines.append(f"  ✅ {rid2} лучше на {data2['win_rate'] - data1['win_rate']:.1f}%")

    lines.append("")

    # KDA
    lines.append(f"**KDA:**")
    lines.append(f"  {rid1}: {data1['avg_kda']}")
    lines.append(f"  {rid2}: {data2['avg_kda']}")
    if data1['avg_kda'] > data2['avg_kda']:
        lines.append(f"  ✅ {rid1} лучше на {data1['avg_kda'] - data2['avg_kda']:.2f}")
    else:
        lines.append(f"  ✅ {rid2} лучше на {data2['avg_kda'] - data1['avg_kda']:.2f}")

    lines.append("")

    # Фраги
    lines.append(f"**AVG KILLS:**")
    lines.append(f"  {rid1}: {data1['avg_kills']}")
    lines.append(f"  {rid2}: {data2['avg_kills']}")

    lines.append("")

    # Смерти
    lines.append(f"**AVG DEATHS:**")
    lines.append(f"  {rid1}: {data1['avg_deaths']}")
    lines.append(f"  {rid2}: {data2['avg_deaths']}")

    lines.append("")

    # Матчи
    lines.append(f"**МАТЧЕЙ ВСЕГО:**")
    lines.append(f"  {rid1}: {data1['total_matches']}")
    lines.append(f"  {rid2}: {data2['total_matches']}")

    lines.append("")

    # Вывод
    lines.append(f"**ВЫВОД:**")
    if data1['win_rate'] > data2['win_rate'] and data1['avg_kda'] > data2['avg_kda']:
        lines.append(f"🏆 {rid1} явно сильнее")
    elif data2['win_rate'] > data1['win_rate'] and data2['avg_kda'] > data1['avg_kda']:
        lines.append(f"🏆 {rid2} явно сильнее")
    else:
        lines.append(f"⚖️ Близко! Зависит от текущей формы")

    return "\n".join(lines)


async def set_alert(chat_id, riot_id, alert_type: str, threshold: float):
    """Установить оповещение для игрока"""
    def _set(chat_id, riot_id, alert_type, threshold):
        with db() as c:
            c.execute("""
                INSERT OR REPLACE INTO player_alerts
                (chat_id, riot_id, alert_type, threshold, enabled, created_date)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (chat_id, riot_id, alert_type, threshold, now_iso()))
    return await asyncio.to_thread(_set, chat_id, riot_id, alert_type, threshold)


async def check_alerts(chat_id, riot_id, analytics):
    """Проверить оповещения для игрока"""
    def _check(chat_id, riot_id, analytics):
        with db() as c:
            c.row_factory = sqlite3.Row
            alerts = c.execute("""
                SELECT * FROM player_alerts
                WHERE chat_id=? AND riot_id=? AND enabled=1
            """, (chat_id, riot_id)).fetchall()
        
        triggered = []
        for alert in alerts:
            alert_type = alert['alert_type']
            threshold = alert['threshold']
            
            if alert_type == 'wr_below':
                if analytics['win_rate'] < threshold:
                    triggered.append(f"⚠️ WR упал ниже {threshold}%! Сейчас: {analytics['win_rate']}%")
            elif alert_type == 'wr_above':
                if analytics['win_rate'] > threshold:
                    triggered.append(f"🎉 WR поднялся выше {threshold}%! Сейчас: {analytics['win_rate']}%")
            elif alert_type == 'kda_below':
                if analytics['avg_kda'] < threshold:
                    triggered.append(f"⚠️ KDA упал ниже {threshold}! Сейчас: {analytics['avg_kda']}")
        
        return triggered
    
    return await asyncio.to_thread(_check, chat_id, riot_id, analytics)


async def get_user_alerts(chat_id):
    """Получить все оповещения пользователя"""
    def _get(chat_id):
        with db() as c:
            c.row_factory = sqlite3.Row
            return c.execute("""
                SELECT * FROM player_alerts
                WHERE chat_id=? AND enabled=1
                ORDER BY riot_id
            """, (chat_id,)).fetchall()
    
    return await asyncio.to_thread(_get, chat_id)


def format_alerts(alerts):
    """Форматировать список оповещений"""
    if not alerts:
        return "У тебя нет активных оповещений."
    
    lines = ["🔔 **ТВОИ ОПОВЕЩЕНИЯ:**", ""]
    for alert in alerts:
        if alert['alert_type'] == 'wr_below':
            lines.append(f"• {alert['riot_id']}: оповещение если WR < {alert['threshold']}%")
        elif alert['alert_type'] == 'wr_above':
            lines.append(f"• {alert['riot_id']}: оповещение если WR > {alert['threshold']}%")
        elif alert['alert_type'] == 'kda_below':
            lines.append(f"• {alert['riot_id']}: оповещение если KDA < {alert['threshold']}")
    
    return "\n".join(lines)





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


cache = {}


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
                match_url = f"https://api.henrikdev.xyz/valorant/v3/matches/{region}/{quote(name)}/{quote(tag)}?mode=competitive&size=20"

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


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await save_chat(m.chat.id, user_name(m), "/help")
    text = """
📌 **Команды Latumbot:**

**ТРЕКЕР И АНАЛИЗ**
/tracker name#tag — анализ профиля (или просто: трекер name#tag)
/progress name#tag — прогресс за 30 дней (WR, KDA, тренд)
/vs name1#tag1 vs name2#tag2 — сравнение двух игроков
  Пример: /vs aisokuro#ako vs другой#игрок

**ОПОВЕЩЕНИЯ**
/alert name#tag wr < 40 — оповещение если WR упадёт ниже 40%
/alert name#tag kda < 1.0 — оповещение если KDA упадёт ниже 1.0
/alerts — список твоих оповещений

**ОБЩЕЕ**
/start — проверить статус
/help — эта справка

**ОСОБЕННОСТИ**
• В личном чате — просто напиши сообщение, бот ответит
• В группе — упомяни бота: @latumbot трекер aisokuro#ako
• Follow-up запросы: после трекера напиши "скинь все" — бот найдёт последний ID

**ПРИМЕРЫ**
@ailatumbot трекер aisokuro#ako
скинь все
@ailatumbot progress aisokuro#ako
@ailatumbot vs player1#tag vs player2#tag
/alert aisokuro#ako wr < 45
/alerts
"""
    await m.answer(text)


@dp.message(Command("progress"))
async def progress_cmd(m: Message):
    text = m.text or ""
    await save_chat(m.chat.id, user_name(m), text)

    # Extract RiotID from /progress command or use last mentioned
    rid = extract_riot_id(text)
    if not rid:
        rid_str = await find_last_rid(m.chat.id)
        if not rid_str:
            return await m.answer("Укажи RiotID: /progress name#tag")
    else:
        rid_str = f"{rid[0]}#{rid[1]}"

    snapshots = await get_progress(rid_str)
    if not snapshots:
        return await m.answer(f"Нет истории для {rid_str}. Используй /трекер несколько раз.")

    result = format_progress(rid_str, snapshots)
    await m.answer(result)
    log.info("Sent progress for %s to chat %s", rid_str, m.chat.id)


@dp.message(Command("alert"))
async def alert_cmd(m: Message):
    text = m.text or ""
    await save_chat(m.chat.id, user_name(m), text)

    # Parse: /alert aisokuro#ako wr < 40
    # or: /alert aisokuro#ako kda < 1.0
    parts = text.split()
    if len(parts) < 4:
        await m.answer("Используй: /alert name#tag wr < 40 или /alert name#tag kda < 1.0")
        return

    rid = extract_riot_id(parts[1])
    if not rid:
        return await m.answer("RiotID не распознан. Используй формат: name#tag")

    rid_str = f"{rid[0]}#{rid[1]}"
    alert_type = parts[2].lower()
    operator = parts[3].lower()
    try:
        threshold = float(parts[4])
    except (ValueError, IndexError):
        return await m.answer("Порог должен быть числом, например: 40 или 1.5")

    if alert_type == 'wr':
        if operator == '<':
            alert_key = 'wr_below'
        elif operator == '>':
            alert_key = 'wr_above'
        else:
            return await m.answer("Оператор должен быть < или >")
    elif alert_type == 'kda':
        if operator != '<':
            return await m.answer("Для KDA поддерживается только <")
        alert_key = 'kda_below'
    else:
        return await m.answer("Тип должен быть: wr или kda")

    await set_alert(m.chat.id, rid_str, alert_key, threshold)
    await m.answer(f"✅ Оповещение установлено для {rid_str}")
    log.info("Alert set for %s in chat %s", rid_str, m.chat.id)


@dp.message(Command("alerts"))
async def alerts_cmd(m: Message):
    await save_chat(m.chat.id, user_name(m), "/alerts")
    
    alerts = await get_user_alerts(m.chat.id)
    result = format_alerts(alerts)
    await m.answer(result)
    log.info("Sent alerts list to chat %s", m.chat.id)

    text = m.text or ""
    await save_chat(m.chat.id, user_name(m), text)

    # Parse: /vs name1#tag1 vs name2#tag2
    parts = re.split(r'\s+vs\s+', text.lower())
    if len(parts) < 2:
        return await m.answer("Используй: /vs name1#tag1 vs name2#tag2")

    rid1 = extract_riot_id(parts[0])
    rid2 = extract_riot_id(parts[1])

    if not rid1 or not rid2:
        return await m.answer("Не смог распарсить RiotID. Используй: /vs aisokuro#ako vs другой#игрок")

    rid1_str = f"{rid1[0]}#{rid1[1]}"
    rid2_str = f"{rid2[0]}#{rid2[1]}"

    # Fetch if needed
    for rid_str in [rid1_str, rid2_str]:
        data = await fetch_tracker(rid_str)
        if data:
            profile = data.get("profile", {})
            matches_raw = data.get("matches", {})
            parsed_matches = parse_matches(matches_raw)
            if parsed_matches:
                analytics = compute_analytics(rid_str, parsed_matches)
                await save_parsed_data(rid_str, profile, analytics, parsed_matches)
                log.info("Fetched and saved %s for vs comparison", rid_str)

    data1, data2 = await get_vs_data(rid1_str, rid2_str)
    result = format_vs(rid1_str, data1, rid2_str, data2)
    await m.answer(result)
    log.info("Sent vs comparison for %s vs %s to chat %s", rid1_str, rid2_str, m.chat.id)



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
    lower = clean.lower()

    await upsert_player(m.chat.id, m.from_user.id, user_name(m))

    # roles
    if "смокер" in lower:
        await set_field(m.chat.id, m.from_user.id, "role", "controller")
        return await m.answer("по памяти: записал — смокер")

    if "дуэлянт" in lower:
        await set_field(m.chat.id, m.from_user.id, "role", "duelist")
        return await m.answer("по памяти: записал — дуэлянт")

    # tracker
    if "трекер" in lower:
        rid = extract_riot_id(clean)

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

        # Parse matches and compute analytics
        profile_data = data.get("profile", {})
        matches_raw = data.get("matches", {})
        parsed_matches = parse_matches(matches_raw)
        
        if parsed_matches:
            analytics = compute_analytics(rid_str, parsed_matches)
            await save_parsed_data(rid_str, profile_data, analytics, parsed_matches)
            await save_snapshot(rid_str, analytics)
            
            # Check and trigger alerts
            triggered = await check_alerts(m.chat.id, rid_str, analytics)
            if triggered:
                for alert_msg in triggered:
                    await m.answer(alert_msg)
            
            log.info("Parsed and saved analytics for %s: %d matches", rid_str, len(parsed_matches))
        else:
            analytics = await get_analytics(rid_str)
            log.warning("Could not parse new matches for %s, using cached analytics", rid_str)

        # If user explicitly asked to "скинь все" or "все показатели", provide detailed analysis
        if any(k in lower for k in ("скинь", "все", "всё", "показатель", "все показатели")):
            if analytics:
                text = format_analysis(rid_str, profile_data, analytics)
                sent = await m.answer(text)
                log.info("Sent detailed analysis to chat %s for %s", m.chat.id, rid_str)
                return sent
            else:
                return await m.answer("Недостаточно данных для анализа. Попробуй позже.")

        # Default behavior: smart analysis instead of LLM hallucination
        if analytics:
            text = format_analysis(rid_str, profile_data, analytics)
            sent = await m.answer(text)
            log.info("Sent analysis to chat %s for %s", m.chat.id, rid_str)
            return sent
        else:
            return await m.answer("Нет данных матчей. Henrik API может быть недоступен.")

    # progress command
    if "progress" in lower or "прогресс" in lower:
        rid = extract_riot_id(clean)
        if not rid:
            rid_str = await find_last_rid(m.chat.id)
            if not rid_str:
                return await m.answer("Укажи RiotID: progress name#tag")
        else:
            rid_str = f"{rid[0]}#{rid[1]}"

        snapshots = await get_progress(rid_str)
        if not snapshots:
            return await m.answer(f"Нет истории для {rid_str}. Используй трекер несколько раз.")

        result = format_progress(rid_str, snapshots)
        await m.answer(result)
        log.info("Sent progress for %s to chat %s", rid_str, m.chat.id)
        return

    # vs comparison
    if " vs " in lower:
        parts = re.split(r'\s+vs\s+', clean, flags=re.IGNORECASE)
        if len(parts) >= 2:
            rid1 = extract_riot_id(parts[0])
            rid2 = extract_riot_id(parts[1])

            if rid1 and rid2:
                rid1_str = f"{rid1[0]}#{rid1[1]}"
                rid2_str = f"{rid2[0]}#{rid2[1]}"

                # Fetch if needed
                for rid_str in [rid1_str, rid2_str]:
                    data = await fetch_tracker(rid_str)
                    if data:
                        profile = data.get("profile", {})
                        matches_raw = data.get("matches", {})
                        parsed_matches = parse_matches(matches_raw)
                        if parsed_matches:
                            analytics = compute_analytics(rid_str, parsed_matches)
                            await save_parsed_data(rid_str, profile, analytics, parsed_matches)
                            log.info("Fetched and saved %s for vs comparison", rid_str)

                data1, data2 = await get_vs_data(rid1_str, rid2_str)
                result = format_vs(rid1_str, data1, rid2_str, data2)
                await m.answer(result)
                log.info("Sent vs comparison for %s vs %s to chat %s", rid1_str, rid2_str, m.chat.id)
                return

    # Default: general LLM chat
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
