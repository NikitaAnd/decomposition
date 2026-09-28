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
user_stats = defaultdict(lambda: {"messages": 0, "images": 0, "searches": 0})

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

async def web_search(query: str, limit: int = 8):
    """Keyless web/news search using public RSS feeds.

    Google News and Bing News RSS do not require a user API key. We merge,
    deduplicate and normalize their results before sending them to GPT.
    """
    import xml.etree.ElementTree as ET

    feeds = [
        (
            "Google News",
            "https://news.google.com/rss/search",
            {
                "q": query,
                "hl": "ru",
                "gl": "RU",
                "ceid": "RU:ru",
            },
        ),
        (
            "Bing News",
            "https://www.bing.com/news/search",
            {"q": query, "format": "rss", "setlang": "ru-RU"},
        ),
    ]

    headers = {"User-Agent": "Mozilla/5.0 OneAI-Telegram-Bot/1.0"}
    results = []

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers=headers,
    ) as client:
        for source, url, params in feeds:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                root = ET.fromstring(response.text)
            except Exception:
                logging.exception("Search feed failed: %s", source)
                continue

            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                description = (item.findtext("description") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()

                if not title or not link:
                    continue

                # RSS descriptions may contain HTML. Strip it before GPT sees it.
                description = re.sub(r"<[^>]+>", " ", description)
                description = re.sub(r"\\s+", " ", description).strip()

                results.append({
                    "title": title,
                    "url": link,
                    "snippet": description[:1000],
                    "date": pub_date,
                    "source": source,
                })

    unique = []
    seen = set()
    for item in results:
        key = item["url"].split("&utm_", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break

    return unique


def search_trigger(text: str) -> bool:
    text = text.lower().strip()
    triggers = (
        "поищи", "найди", "гугли", "загугли", "поищем",
        "новости", "новость", "последние новости", "свежие новости",
        "что сейчас", "что нового", "актуальная информация",
        "актуальные новости", "в интернете", "посмотри в интернете",
        "найди в интернете", "поищи в интернете",
    )
    return any(x in text for x in triggers)

def format_search_context(query, results):
    if not results:
        return f"Интернет-поиск по запросу «{query}» ничего не вернул. Сообщи это пользователю и попроси уточнить запрос."
    lines = [f"Результаты интернет-поиска по запросу: {query}",
             "Используй эти результаты как внешний контекст. Не выдумывай сведения, которых в них нет.", ""]
    for i, r in enumerate(results, 1):
        date = f"Дата: {r['date']}" if r.get("date") else ""
        source = f"Источник: {r['source']}" if r.get("source") else ""
        lines += [
            f"[{i}] {r['title']}",
            f"URL: {r['url']}",
            date,
            source,
            f"Описание: {r['snippet']}",
            "",
        ]
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
    images = user_stats[message.from_user.id]["images"]
    await message.answer(
        f"🧠 <b>Контекст</b>\n\nСообщений: <b>{n}</b>\nИзображений: <b>{images}</b>\nЛимит: <b>{MAX_CONTEXT}</b>",
        parse_mode="HTML", reply_markup=keyboard()
    )

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

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "💡 <b>Команды</b>\n\n"
        "Просто отправь текст — получишь ответ GPT-5.\n"
        "💬 Новый чат — очистить память.\n"
        "🧠 Контекст — посмотреть размер памяти.\n👤 Профиль — статистика.\n🔎 Поиск запускается автоматически, если в сообщении есть поисковый запрос или слова вроде «новости», «найди», «поищи».",
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
        user_id = message.from_user.id
        query = message.text.strip()
        do_search = search_trigger(query)

        if do_search:
            results = await web_search(query)
            user_stats[user_id]["searches"] += 1
            search_context = format_search_context(query, results)
            answer = await ask_ai(
                user_id,
                search_context + "\n\nОтветь на исходный запрос пользователя: " + query
            )
            await send_ai_answer(message, answer)

            # Search results are already passed into GPT context; no extra system message is sent.
            return

        answer = await ask_ai(user_id, query)
        await send_ai_answer(message, answer)
    except Exception as e:
        logging.exception("AI/search request failed")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=keyboard())

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# Railway deployment marker
