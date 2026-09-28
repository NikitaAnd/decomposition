import os, asyncio, logging, base64, re
from io import BytesIO
from html.parser import HTMLParser
from urllib.parse import quote_plus
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
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🔎 Поиск")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def clean_context(user_id):
    contexts[user_id] = contexts[user_id][-MAX_CONTEXT:]

class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.link = False
        self.snippet = False
        self.title = []
        self.text = []
        self.url = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "a" and "result__a" in cls:
            self.link = True
            self.title = []
            self.url = attrs.get("href", "")
        elif "result__snippet" in cls:
            self.snippet = True
            self.text = []

    def handle_data(self, data):
        if self.link:
            self.title.append(data)
        if self.snippet:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.link:
            title = " ".join("".join(self.title).split())
            if title and self.url:
                self.results.append({"title": title, "url": self.url, "snippet": ""})
            self.link = False
        elif self.snippet and tag in ("a", "div"):
            if self.results:
                self.results[-1]["snippet"] = " ".join("".join(self.text).split())
            self.snippet = False


async def web_search(query: str, limit: int = 5):
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    headers = {"User-Agent": "Mozilla/5.0 (Android 16; Mobile) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
    parser = DDGParser()
    parser.feed(response.text)
    out, seen = [], set()
    for item in parser.results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
        if len(out) >= limit:
            break
    return out


def search_trigger(text: str) -> bool:
    text = text.lower().strip()
    triggers = ("поищи ", "найди ", "гугли ", "загугли ", "что сейчас", "последние новости",
                "актуальная информация", "в интернете", "посмотри в интернете")
    return any(x in text for x in triggers)


def format_search_context(query, results):
    if not results:
        return f"Интернет-поиск по запросу «{query}» ничего не вернул. Сообщи это пользователю и попроси уточнить запрос."
    lines = [f"Результаты интернет-поиска по запросу: {query}",
             "Используй эти результаты как внешний контекст. Не выдумывай сведения, которых в них нет.", ""]
    for i, r in enumerate(results, 1):
        lines += [f"[{i}] {r['title']}", f"URL: {r['url']}", f"Описание: {r['snippet']}", ""]
    return "\n".join(lines)


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
    user_stats[user_id]["messages"] += 1
    if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
        user_stats[user_id]["images"] += 1
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
    # Native One AI wraps the JPEG base64 payload in braces.
    return "data:image/jpeg;base64,{" + encoded + "}"


async def download_photo(message: Message) -> str:
    photo = message.photo[-1]
    telegram_file = await bot.get_file(photo.file_id)
    buffer = BytesIO()
    await bot.download(telegram_file, destination=buffer)
    return telegram_image_to_data_url(buffer.getvalue())

@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    contexts[user_id].clear()
    user_stats[user_id] = {"messages": 0, "images": 0, "searches": 0}
    search_mode[user_id] = False
    name = message.from_user.first_name or "друг"
    await message.answer(
        f"🤖 <b>One AI</b>\n\nПривет, {name}! Я твой AI-помощник на GPT-5.\n\n"
        "Я умею:\n• помнить контекст диалога\n• анализировать изображения и помнить их в текущем чате\n"
        "• искать свежую информацию в интернете без отдельного API\n\nПросто напиши вопрос или отправь фотографию.",
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

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = message.from_user
    stats = user_stats[user.id]
    username = f"@{user.username}" if user.username else "не указан"
    await message.answer(
        f"👤 <b>Профиль</b>\n\nИмя: <b>{user.first_name or '—'}</b>\n"
        f"Username: <b>{username}</b>\nID: <code>{user.id}</code>\n\n"
        f"Сообщений: <b>{stats['messages']}</b>\nИзображений: <b>{stats['images']}</b>\n"
        f"Поисков: <b>{stats['searches']}</b>",
        parse_mode="HTML", reply_markup=keyboard()
    )

@dp.message(F.text == "🔎 Поиск")
async def search_button(message: Message):
    search_mode[message.from_user.id] = True
    await message.answer(
        "🔎 <b>Режим поиска включён.</b>\n\nОтправь ключевые слова или вопрос — "
        "сначала выполню веб-поиск, затем передам результаты GPT-5.",
        parse_mode="HTML", reply_markup=keyboard()
    )

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
