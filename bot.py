import asyncio
import os

from aiogram import Bot, Dispatcher, F
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

Отвечай кратко и по делу.
"""


@dp.message(F.text)
async def ai_reply(message: Message):
    text = message.text or ""

    if "@ailatumbot" not in text.lower():
        return

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )

    reply = response.choices[0].message.content

    await message.answer(reply)


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
