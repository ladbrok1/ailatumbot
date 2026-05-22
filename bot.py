import asyncio
import os
import random

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from openai import OpenAI


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

if not GROQ_API_KEY:
    raise RuntimeError("Set GROQ_API_KEY environment variable")

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
dp = Dispatcher()

SYSTEM_PROMPT = """
Ты ИИ-ассистент только по Valorant.

Ты:
- знаешь мету
- умеешь анализировать ошибки
- разбираешься в ролях
- помогаешь с аимом
- говоришь как опытный игрок Immortal/Radiant

Отвечай кратко, по делу, дружелюбно и с лёгким командным вайбом.
Если вопрос не про Valorant, мягко возвращай разговор к игре.
"""

AGENTS = {
    "duelist": ["Jett", "Raze", "Reyna", "Phoenix", "Yoru", "Neon", "Iso"],
    "initiator": ["Sova", "Skye", "KAY/O", "Fade", "Breach", "Gekko", "Tejo"],
    "controller": ["Omen", "Astra", "Viper", "Brimstone", "Harbor", "Clove"],
    "sentinel": ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
}

MAP_TIPS = {
    "ascent": "Ascent: контролируйте мид. Без мида атака часто превращается в угадайку.",
    "bind": "Bind: играйте через телепорты и фейки. Не отдавайте showers/long бесплатно.",
    "haven": "Haven: важен быстрый сбор инфы. На защите не сидите 5 человек по сайтам пассивно.",
    "split": "Split: мид решает темп. Smokes на vent/mail дают команде пространство.",
    "lotus": "Lotus: ломайте двери и давите ротации. Не играйте каждый раунд в один темп.",
    "sunset": "Sunset: mid control открывает оба сайта. Следите за lurk через tiles.",
    "icebox": "Icebox: используйте вертикаль и дроны/флеши перед выходом, иначе вас снимут с углов.",
    "breeze": "Breeze: без дальних дуэлей и контроля halls будет больно. Нужны смоки и инфа.",
}

ECO_TIPS = [
    "Eco: если денег мало, лучше сыграть стаком и забрать один вандал, чем умереть по одному.",
    "Force: форсите только если есть понятный план: быстрый выход, шортганы, ульты или стак.",
    "Bonus: не выбрасывайте Spectre просто так. Играйте ближе, заберите экономику врага.",
    "Full buy: проверьте, что у команды есть смоки, флеши и defuse, а не только красивые скины.",
]


@dp.message(Command("start", "help"))
async def help_reply(message: Message):
    await message.answer(
        "Команды:\n"
        "/agent роль - рандомный агент: duelist, initiator, controller, sentinel\n"
        "/map карта - быстрый совет по карте\n"
        "/eco - совет по экономике\n\n"
        "Для анализа просто упомяни @ailatumbot и напиши ситуацию по Valorant."
    )


@dp.message(Command("agent"))
async def agent_reply(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    role = parts[1].strip().lower() if len(parts) > 1 else random.choice(list(AGENTS))

    if role not in AGENTS:
        await message.answer("Роли: duelist, initiator, controller, sentinel")
        return

    await message.answer(f"Сегодня играешь {random.choice(AGENTS[role])}. Без нытья, делай impact.")


@dp.message(Command("map"))
async def map_reply(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напиши карту: /map Ascent")
        return

    map_name = parts[1].strip().lower()
    await message.answer(MAP_TIPS.get(map_name, "Пока нет заготовки по этой карте. Спроси через @ailatumbot подробнее."))


@dp.message(Command("eco"))
async def eco_reply(message: Message):
    await message.answer(random.choice(ECO_TIPS))


@dp.message(F.text)
async def ai_reply(message: Message):
    text = message.text or ""

    if "@ailatumbot" not in text.lower():
        return

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
    except Exception:
        await message.answer("Groq сейчас не ответил. Попробуй ещё раз через пару секунд.")
        return

    reply = response.choices[0].message.content
    await message.answer(reply)


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
