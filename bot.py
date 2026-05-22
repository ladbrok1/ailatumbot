import asyncio
import base64
import io
import os
import random

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

if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

if not GROQ_API_KEY:
    raise RuntimeError("Set GROQ_API_KEY environment variable")

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
dp = Dispatcher()

SYSTEM_PROMPT = """
Ты русскоязычный ИИ-тиммейт для небольшой компании друзей, которые играют в Valorant.

Твой стиль:
- полезный, короткий и по делу;
- можно подколоть, но без токсичности и личных оскорблений;
- вайб опытного игрока Immortal/Radiant, который шарит и иногда рафлит;
- если человек тильтует, сначала стабилизируй, потом дай конкретный план.

Ты помогаешь с:
- выбором агентов и ролей;
- экономикой;
- планом раунда;
- ошибками по скринам и описанию;
- клатчами, ретейками, атакой, защитой;
- тренировкой аима;
- разбором Tracker/Riot ID, если пользователь дал ссылку или статистику.

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
    "initiator": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "инициатор": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "controller": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "смокер": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "sentinel": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
    "страж": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
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


def normalize_arg(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def strip_bot_mention(text: str) -> str:
    return text.replace(BOT_USERNAME, "").replace(BOT_USERNAME.upper(), "").strip()


def is_addressed_to_bot(text: str) -> bool:
    lowered = text.lower()
    return BOT_USERNAME in lowered or lowered.startswith("бот ") or lowered.startswith("валик ")


async def ask_groq(text: str) -> str:
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_completion_tokens=700,
    )
    return response.choices[0].message.content


async def ask_groq_vision(image_bytes: bytes, prompt: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    },
                ],
            }
        ],
        max_completion_tokens=700,
    )
    return response.choices[0].message.content


@dp.message(Command("start", "help", "commands"))
async def help_reply(message: Message):
    await send_help(message)


@dp.message(F.text.regexp(r"^/(команды|помощь|хелп)(\s|$)"))
async def ru_help_reply(message: Message):
    await send_help(message)


async def send_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/agent или /агент роль - рандомный агент: duelist, initiator, controller, sentinel\n"
        "/map или /карта название - быстрый совет по карте\n"
        "/strat или /страта карта атака/защита - план раунда\n"
        "/eco или /эко - совет по экономике\n"
        "/aim или /аим - тренировка аима\n"
        "/clutch или /клатч 1v2 - совет по клатчу\n"
        "/tilt или /тильт - анти-тильт таблетка\n"
        "/tracker ссылка или RiotID#TAG - разбор статистики по данным, которые пришлёшь\n\n"
        "Фото: пришли скрин таба, Tracker, карты или момента с подписью /фото или просто упомяни бота.\n"
        "AI-разбор: упомяни @ailatumbot, либо начни сообщение с 'бот' или 'валик'."
    )


@dp.message(Command("agent"))
async def agent_reply(message: Message):
    await send_agent(message, (message.text or "").split(maxsplit=1))


@dp.message(F.text.regexp(r"^/агент(\s|$)"))
async def ru_agent_reply(message: Message):
    await send_agent(message, (message.text or "").split(maxsplit=1))


async def send_agent(message: Message, parts: list[str]):
    role = normalize_arg(parts[1]) if len(parts) > 1 else random.choice(list(AGENTS))
    if role not in AGENTS:
        await message.answer("Роли: duelist/дуэлянт, initiator/инициатор, controller/смокер, sentinel/страж")
        return
    await message.answer(f"Сегодня играешь {random.choice(AGENTS[role])}. Без нытья, делай impact.")


@dp.message(Command("map"))
async def map_reply(message: Message):
    await send_map_tip(message, (message.text or "").split(maxsplit=1))


@dp.message(F.text.regexp(r"^/карта(\s|$)"))
async def ru_map_reply(message: Message):
    await send_map_tip(message, (message.text or "").split(maxsplit=1))


async def send_map_tip(message: Message, parts: list[str]):
    if len(parts) < 2:
        await message.answer("Напиши карту: /карта Ascent")
        return
    map_name = normalize_arg(parts[1])
    await message.answer(MAP_TIPS.get(map_name, "По этой карте нет заготовки. Спроси через @ailatumbot подробнее."))


@dp.message(Command("eco"))
async def eco_reply(message: Message):
    await message.answer(random.choice(ECO_TIPS))


@dp.message(F.text.regexp(r"^/эко(\s|$)"))
async def ru_eco_reply(message: Message):
    await message.answer(random.choice(ECO_TIPS))


@dp.message(Command("aim"))
async def aim_reply(message: Message):
    await message.answer(random.choice(AIM_ROUTINES))


@dp.message(F.text.regexp(r"^/аим(\s|$)"))
async def ru_aim_reply(message: Message):
    await message.answer(random.choice(AIM_ROUTINES))


@dp.message(Command("tilt"))
async def tilt_reply(message: Message):
    await message.answer(random.choice(TILT_LINES))


@dp.message(F.text.regexp(r"^/тильт(\s|$)"))
async def ru_tilt_reply(message: Message):
    await message.answer(random.choice(TILT_LINES))


@dp.message(Command("clutch"))
async def clutch_reply(message: Message):
    await send_clutch(message, (message.text or "").split(maxsplit=1))


@dp.message(F.text.regexp(r"^/клатч(\s|$)"))
async def ru_clutch_reply(message: Message):
    await send_clutch(message, (message.text or "").split(maxsplit=1))


async def send_clutch(message: Message, parts: list[str]):
    clutch_type = normalize_arg(parts[1]) if len(parts) > 1 else "1v2"
    await message.answer(CLUTCH_TIPS.get(clutch_type, "Форматы: /клатч 1v1, /клатч 1v2, /клатч 1v3"))


@dp.message(Command("strat"))
async def strat_reply(message: Message):
    await send_strat(message, strip_bot_mention((message.text or "").partition(" ")[2]))


@dp.message(F.text.regexp(r"^/страта(\s|$)"))
async def ru_strat_reply(message: Message):
    await send_strat(message, strip_bot_mention((message.text or "").partition(" ")[2]))


async def send_strat(message: Message, query: str):
    if not query:
        await message.answer("Пример: /страта Ascent атака")
        return
    try:
        answer = await ask_groq(f"Дай короткую страту для Valorant: {query}. Формат: план, роли, ключевой риск.")
    except Exception:
        answer = "Groq не ответил. План Б: играйте дефолт, заберите инфу, потом взрывайте слабый сайт."
    await message.answer(answer)


@dp.message(Command("tracker"))
async def tracker_reply(message: Message):
    await send_tracker_analysis(message, (message.text or "").partition(" ")[2])


@dp.message(F.text.regexp(r"^/трекер(\s|$)"))
async def ru_tracker_reply(message: Message):
    await send_tracker_analysis(message, (message.text or "").partition(" ")[2])


async def send_tracker_analysis(message: Message, query: str):
    if not query:
        await message.answer("Кинь ссылку Tracker или Riot ID: /трекер Name#TAG. Если есть скрин статистики, отправь фото.")
        return
    prompt = (
        "Пользователь дал Tracker/Riot ID/статистику. "
        "Если данных мало, скажи что именно нужно прислать. "
        f"Разбери по Valorant и дай полезные выводы: {query}"
    )
    try:
        answer = await ask_groq(prompt)
    except Exception:
        answer = "Не смог достучаться до Groq. Кинь скрин Tracker, так будет проще разобрать."
    await message.answer(answer)


@dp.message(F.photo)
async def photo_reply(message: Message, bot: Bot):
    caption = message.caption or ""
    if not is_addressed_to_bot(caption) and not caption.startswith(("/фото", "/photo", "/анализ")):
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
        answer = await ask_groq_vision(image_bytes, prompt)
    except Exception:
        answer = "Vision-модель Groq не ответила. Проверь лимиты/доступ к vision-модели или кинь скрин позже."

    await message.answer(answer)


@dp.message(F.text)
async def ai_reply(message: Message):
    text = message.text or ""
    if not is_addressed_to_bot(text):
        return

    try:
        reply = await ask_groq(strip_bot_mention(text))
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
