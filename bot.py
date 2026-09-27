import os, asyncio, logging, base64, re
from io import BytesIO
from collections import defaultdict
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_URL = os.getenv("AI_URL", "https://one-ai-openai-proxy-production.up.railway.app/v1/chat/completions")
AI_KEY = os.environ["AI_KEY"]
MODEL = os.getenv("MODEL", "gpt-5")
MAX_CONTEXT = int(os.getenv("MAX_CONTEXT", "20"))

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
contexts = defaultdict(list)

def keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Новый чат"), KeyboardButton(text="🧠 Контекст")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def clean_context(user_id):
    contexts[user_id] = contexts[user_id][-MAX_CONTEXT:]

async def ask_ai(user_id, content):
    messages = contexts[user_id] + [{"role": "user", "content": content}]
    headers = {"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "stream": False}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(AI_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    answer = data["choices"][0]["message"]["content"]
    contexts[user_id].extend([
        {"role": "user", "content": content},
        {"role": "assistant", "content": answer},
    ])
    clean_context(user_id)
    return answer


async def send_ai_answer(message: Message, answer: str):
    """Send AI MarkdownV2 with safe fallback and Telegram's 4096-char limit."""
    answer = str(answer).replace("\r\n", "\n").replace("\r", "\n")
    answer = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", answer)
    chunks = []
    while len(answer) > 4096:
        cut = answer.rfind("\n", 0, 4096)
        if cut < 2048:
            cut = answer.rfind(" ", 0, 4096)
        if cut < 2048:
            cut = 4096
        chunks.append(answer[:cut].rstrip())
        answer = answer[cut:].lstrip()
    if answer:
        chunks.append(answer)
    for chunk in chunks or [""]:
        try:
            await message.answer(chunk, parse_mode="MarkdownV2", reply_markup=keyboard())
        except Exception:
            logging.exception("MarkdownV2 failed; sending plain text")
            plain = re.sub(r"[*_~\` ]", "", chunk)
            await message.answer(plain, reply_markup=keyboard())


def telegram_image_to_data_url(data: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


async def download_photo(message: Message) -> str:
    photo = message.photo[-1]
    telegram_file = await bot.get_file(photo.file_id)
    buffer = BytesIO()
    await bot.download(telegram_file, destination=buffer)
    return telegram_image_to_data_url(buffer.getvalue())

@dp.message(CommandStart())
async def start(message: Message):
    contexts[message.from_user.id].clear()
    await message.answer(
        "🤖 <b>One AI</b>\n\nЯ готов. Пиши сообщение — я буду помнить контекст этого чата.",
        parse_mode="HTML", reply_markup=keyboard()
    )

@dp.message(F.text == "💬 Новый чат")
async def new_chat(message: Message):
    contexts[message.from_user.id].clear()
    await message.answer("🧹 Контекст очищен. Начинаем с чистого листа.", reply_markup=keyboard())

@dp.message(F.text == "🧠 Контекст")
async def context_info(message: Message):
    n = len(contexts[message.from_user.id])
    await message.answer(f"🧠 В памяти: <b>{n}</b> сообщений.", parse_mode="HTML", reply_markup=keyboard())

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "💡 <b>Команды</b>\n\n"
        "Просто отправь текст — получишь ответ GPT-5.\n"
        "💬 Новый чат — очистить память.\n"
        "🧠 Контекст — посмотреть размер памяти.",
        parse_mode="HTML", reply_markup=keyboard()
    )

@dp.message(F.photo)
async def photo_message(message: Message):
    try:
        await bot.send_chat_action(message.chat.id, "typing")
        image_url = await download_photo(message)
        caption = message.caption or "Проанализируй это изображение."
        content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
        ]
        answer = await ask_ai(message.from_user.id, content)
        await send_ai_answer(message, answer)
    except Exception as e:
        logging.exception("Image AI request failed")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=keyboard())


@dp.message(F.text)
async def text_message(message: Message):
    try:
        await bot.send_chat_action(message.chat.id, "typing")
        answer = await ask_ai(message.from_user.id, message.text)
        await send_ai_answer(message, answer)
    except Exception as e:
        logging.exception("AI request failed")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=keyboard())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# Railway deployment marker
