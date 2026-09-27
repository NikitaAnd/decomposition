import os, asyncio, logging, base64, re
from io import BytesIO
from PIL import Image
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


def escape_plain(value: str) -> str:
    reserved = r"_*[]()~`>#+-=|{}.!\\"
    return "".join("\\" + ch if ch in reserved else ch for ch in str(value))


def markdown_to_telegram(text: str) -> str:
    """Convert common Markdown produced by GPT to Telegram MarkdownV2."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    protected = []

    def protect_code(match):
        protected.append(("code", match.group(1)))
        return "@@TGPROTECT{}@@".format(len(protected) - 1)

    text = re.sub(r"```(?:[^\n]*)\n([\s\S]*?)```", protect_code, text)
    text = re.sub(r"`([^`\n]+)`", protect_code, text)

    def protect_link(match):
        protected.append(("link", match.group(1), match.group(2)))
        return "@@TGPROTECT{}@@".format(len(protected) - 1)

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", protect_link, text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", r"*\1*", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    reserved = r"_*[]()~`>#+-=|{}.!\\"
    out = []
    i = 0
    while i < len(text):
        if text.startswith("@@TGPROTECT", i):
            m = re.match(r"@@TGPROTECT(\d+)@@", text[i:])
            if m:
                value = protected[int(m.group(1))]
                if value[0] == "link":
                    label = escape_plain(value[1])
                    url = value[2].replace("\\", "\\\\").replace(")", "\\)")
                    out.append("[{}]({})".format(label, url))
                else:
                    code = value[1].replace("\\", "\\\\").replace("`", "\\`")
                    out.append("`{}`".format(code))
                i += len(m.group(0))
                continue
        ch = text[i]
        if ch in reserved:
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1

    result = "".join(out)
    result = result.replace(r"\*", "*").replace(r"\~", "~").replace(r"\_", "_")
    return result


async def send_ai_answer(message: Message, answer: str):
    """Convert GPT Markdown to Telegram MarkdownV2 and safely send long answers."""
    converted = markdown_to_telegram(answer)
    chunks = []
    while len(converted) > 4096:
        cut = converted.rfind("\n", 0, 4096)
        if cut < 2048:
            cut = converted.rfind(" ", 0, 4096)
        if cut < 2048:
            cut = 4096
        chunks.append(converted[:cut].rstrip())
        converted = converted[cut:].lstrip()
    if converted:
        chunks.append(converted)
    for chunk in chunks or [""]:
        try:
            await message.answer(chunk, parse_mode="MarkdownV2", reply_markup=keyboard())
        except Exception:
            logging.exception("MarkdownV2 failed; sending plain text")
            await message.answer(re.sub(r"[*_~`]", "", chunk), reply_markup=keyboard())
def telegram_image_to_data_url(data: bytes) -> str:
    source = BytesIO(data)
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + encoded


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
