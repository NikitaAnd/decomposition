import os, asyncio, logging, base64, re, json
from io import BytesIO
from PIL import Image
from collections import defaultdict
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, KeyboardButton, ReplyKeyboardMarkup, InputRichMessage,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
)
import database as db
import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_URL = os.getenv("AI_URL", "https://one-ai-openai-proxy-production.up.railway.app/v1/chat/completions")
AI_KEY = os.environ["AI_KEY"]
MODEL = os.getenv("MODEL", "gpt-5")
ADMIN_ID = 8434278373
admin_broadcast_mode = set()
MAX_CONTEXT = int(os.getenv("MAX_CONTEXT", "20"))

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
contexts = defaultdict(list)
user_stats = defaultdict(lambda: {"messages": 0, "images": 0, "searches": 0})

def keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="💬 Новый чат", style="primary"),
                KeyboardButton(text="🧠 Контекст", style="primary"),
            ],
            [
                KeyboardButton(text="👤 Профиль", style="success"),
                KeyboardButton(text="ℹ️ Помощь", style="danger"),
            ],
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


async def stream_ai_answer(user_id, content):
    """Stream OpenAI-compatible SSE, with JSON fallback for non-streaming proxies."""
    messages = contexts[user_id] + [{"role": "user", "content": content}]
    headers = {
        "Authorization": f"Bearer {AI_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }
    payload = {"model": MODEL, "messages": messages, "stream": True}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=30.0)
    ) as client:
        async with client.stream(
            "POST", AI_URL, headers=headers, json=payload
        ) as r:
            r.raise_for_status()

            content_type = (r.headers.get("content-type") or "").lower()

            # Some OpenAI-compatible proxies ignore stream=true and return
            # one normal JSON response. Handle that case instead of silently
            # producing an empty answer.
            if "text/event-stream" not in content_type:
                raw = await r.aread()
                if not raw:
                    return

                try:
                    data = json.loads(raw)
                except Exception:
                    logging.error(
                        "AI proxy returned non-SSE response: %s",
                        raw[:1000].decode("utf-8", errors="replace"),
                    )
                    return

                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    choice = choices[0]
                    message = choice.get("message", {})
                    answer = (
                        message.get("content")
                        if isinstance(message, dict)
                        else None
                    )
                    if answer is None:
                        answer = choice.get("text")

                    if isinstance(answer, str) and answer:
                        yield answer
                return

            async for line in r.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue

                if line.startswith("data:"):
                    line = line[5:].strip()

                if line == "[DONE]":
                    break

                try:
                    data = json.loads(line)
                except Exception:
                    continue

                choices = data.get("choices")
                if isinstance(choices, list) and choices:
                    delta = choices[0].get("delta", {})
                    chunk = (
                        delta.get("content")
                        if isinstance(delta, dict)
                        else None
                    )

                    if chunk is None:
                        chunk = choices[0].get("text")

                    if isinstance(chunk, str) and chunk:
                        yield chunk

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


async def stream_to_telegram(message: Message, user_id, content):
    """Stream directly into the first real Telegram message, then finish with Rich formatting."""
    full_text = ""
    last_sent = ""
    last_update = 0.0
    interval = 0.3

    async def edit_stream_text(text):
        return await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=stream_message.message_id,
            text=text,
        )

    stream_message = None

    try:
        async for chunk in stream_ai_answer(user_id, content):
            full_text += chunk
            now = asyncio.get_running_loop().time()

            # Send the first real chunk immediately — no placeholder like "▌".
            if stream_message is None:
                display = full_text
                if display and len(display) <= 4096:
                    stream_message = await bot.send_message(
                        chat_id=message.chat.id,
                        text=display,
                    )
                    last_sent = display
                    last_update = now
                continue

            # While generating, update the same plain-text message every 0.3s.
            if (
                now - last_update >= interval
                and full_text != last_sent
                and len(full_text) <= 4096
            ):
                try:
                    await edit_stream_text(full_text)
                    last_sent = full_text
                    last_update = now
                except Exception:
                    logging.exception("Telegram stream edit failed")

        final_text = full_text.strip() or "Не удалось получить ответ."

        # If the response was so fast that no chunk was sent yet, send it now.
        if stream_message is None:
            if len(final_text) <= 4096:
                stream_message = await bot.send_message(
                    chat_id=message.chat.id,
                    text=final_text,
                )
            else:
                await send_ai_answer(message, final_text)
        elif len(final_text) <= 4096:
            # Final plain-text flush, then replace it with the Rich Message.
            if final_text != last_sent:
                try:
                    await edit_stream_text(final_text)
                except Exception:
                    logging.warning("Final Telegram stream edit failed")

            # Rich Messages cannot be edited with edit_message_text.
            # Replace the temporary streamed text with the fully formatted answer.
            try:
                await bot.delete_message(
                    chat_id=stream_message.chat.id,
                    message_id=stream_message.message_id,
                )
            except Exception:
                logging.warning("Could not delete streamed message before Rich Message")

            await send_ai_answer(message, final_text)
        else:
            try:
                await bot.delete_message(
                    chat_id=stream_message.chat.id,
                    message_id=stream_message.message_id,
                )
            except Exception:
                logging.warning("Could not delete streamed message")
            await send_ai_answer(message, final_text)

        await db.add_message(user_id, "user", content)
        await db.add_message(user_id, "assistant", final_text)
        contexts[user_id].extend([
            {"role": "user", "content": content},
            {"role": "assistant", "content": final_text},
        ])
        await db.increment_stats(user_id, messages=1)
        user_stats[user_id]["messages"] += 1
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in content
        ):
            await db.increment_stats(user_id, images=1)
            user_stats[user_id]["images"] += 1
        clean_context(user_id)
        return final_text
    except Exception:
        logging.exception("AI streaming request failed")
        if stream_message is not None:
            try:
                await bot.delete_message(
                    chat_id=stream_message.chat.id,
                    message_id=stream_message.message_id,
                )
            except Exception:
                pass
        raise


async def send_ai_answer(message: Message, answer: str):
    """Send GPT replies using Telegram Rich Messages, with MarkdownV2 fallback."""
    text = str(answer).replace("\r\n", "\n").replace("\r", "\n").strip()

    # Rich Messages support GitHub-style Markdown plus headings, lists,
    # tables, blockquotes, spoilers, sub/superscript and LaTeX.
    chunks = []
    while len(text) > 4096:
        cut = text.rfind("\n", 0, 4096)
        if cut < 2048:
            cut = text.rfind(" ", 0, 4096)
        if cut < 2048:
            cut = 4096
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)

    for chunk in chunks or [""]:
        try:
            await bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=InputRichMessage(markdown=chunk),
                reply_markup=keyboard(),
            )
        except Exception:
            logging.exception("Rich Message failed; falling back to MarkdownV2")
            converted = markdown_to_telegram(chunk)
            try:
                await message.answer(converted, parse_mode="MarkdownV2", reply_markup=keyboard())
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
    await db.upsert_user(message.from_user)
    if await db.is_banned(user_id):
        return
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
        await db.upsert_user(message.from_user)
        if await db.is_banned(message.from_user.id):
            return
        await bot.send_chat_action(message.chat.id, "typing")
        image_url = await download_photo(message)
        caption = message.caption or "Проанализируй это изображение."
        content = [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
        ]
        await stream_to_telegram(message, message.from_user.id, content)
    except Exception as e:
        logging.exception("Image AI request failed")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=keyboard())


@dp.message(F.text)
async def text_message(message: Message):
    try:
        await db.upsert_user(message.from_user)
        if await db.is_banned(message.from_user.id):
            await message.answer("🚫 Доступ к боту ограничен.")
            return
        if message.from_user.id in admin_broadcast_mode:
            if message.text == "/cancel":
                admin_broadcast_mode.discard(message.from_user.id)
                await message.answer("Рассылка отменена.")
                return
            admin_broadcast_mode.discard(message.from_user.id)
            ids = await db.get_all_user_ids()
            sent = failed = 0
            for uid in ids:
                try:
                    await bot.send_message(uid, message.text)
                    sent += 1
                except Exception:
                    failed += 1
            await message.answer(
                f"📢 Рассылка завершена.\n\n✅ Доставлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
                parse_mode="HTML"
            )
            return
        await bot.send_chat_action(message.chat.id, "typing")
        user_id = message.from_user.id
        query = message.text.strip()
        do_search = search_trigger(query)

        if do_search:
            results = await web_search(query)
            user_stats[user_id]["searches"] += 1
            await db.increment_stats(user_id, searches=1)
            search_context = format_search_context(query, results)
            await stream_to_telegram(
                message, user_id,
                search_context + "\n\nОтветь на исходный запрос пользователя: " + query
            )

            # Search results are already passed into GPT context; no extra system message is sent.
            return

        await stream_to_telegram(message, user_id, query)
    except Exception as e:
        logging.exception("AI/search request failed")
        await message.answer(f"❌ Ошибка: {e}", reply_markup=keyboard())


# ---------------- ADMIN PANEL ----------------

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:home")],
    ])

def admin_user_keyboard(user_id, banned):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔓 Разбанить" if banned else "🚫 Забанить",
            callback_data=f"adm:ban:{user_id}:{0 if banned else 1}"
        )],
        [InlineKeyboardButton(text="◀️ К пользователям", callback_data="adm:users:0")],
    ])

def is_admin(user_id):
    return user_id == ADMIN_ID

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await db.upsert_user(message.from_user)
    await db.set_admin(ADMIN_ID, True)
    s = await db.stats()
    await message.answer(
        "🛠 <b>One AI — Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n"
        f"🟢 Активных за 24ч: <b>{s['active_24h']}</b>\n"
        f"💬 Сообщений: <b>{s['messages']}</b>\n"
        f"🖼 Изображений: <b>{s['images']}</b>\n"
        f"🔎 Поисков: <b>{s['searches']}</b>\n"
        f"🚫 Заблокировано: <b>{s['banned']}</b>",
        parse_mode="HTML", reply_markup=admin_keyboard()
    )

@dp.callback_query(F.data == "adm:home")
async def admin_home(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    s = await db.stats()
    await call.message.edit_text(
        "🛠 <b>One AI — Админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b>\n"
        f"🟢 Активных за 24ч: <b>{s['active_24h']}</b>\n"
        f"💬 Сообщений: <b>{s['messages']}</b>\n"
        f"🖼 Изображений: <b>{s['images']}</b>\n"
        f"🔎 Поисков: <b>{s['searches']}</b>\n"
        f"🚫 Заблокировано: <b>{s['banned']}</b>",
        parse_mode="HTML", reply_markup=admin_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "adm:stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    s = await db.stats()
    await call.answer(
        f"Пользователи: {s['users']}\nАктивные 24ч: {s['active_24h']}\n"
        f"Сообщения: {s['messages']}\nИзображения: {s['images']}\n"
        f"Поиски: {s['searches']}\nБаны: {s['banned']}",
        show_alert=True
    )

@dp.callback_query(F.data.startswith("adm:users:"))
async def admin_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    offset = int(call.data.rsplit(":", 1)[1])
    users = await db.list_users(offset, 8)
    total = await db.user_count()
    lines = ["👥 <b>Пользователи</b>", ""]
    buttons = []
    for u in users:
        name = (u["first_name"] or "Без имени")[:24]
        status = "🚫" if u["is_banned"] else "🟢"
        lines.append(f"{status} <b>{name}</b> — <code>{u['user_id']}</code> — {u['messages']} сообщ.")
        buttons.append([InlineKeyboardButton(text=f"{status} {name}", callback_data=f"adm:user:{u['user_id']}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:users:{max(0, offset-8)}"))
    if offset + 8 < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:users:{offset+8}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm:home")])
    await call.message.edit_text(
        "\n".join(lines) + f"\n\nСтраница {offset//8+1} • всего {total}",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("adm:user:"))
async def admin_user(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    uid = int(call.data.rsplit(":", 1)[1])
    u = await db.get_user(uid)
    if not u:
        await call.answer("Пользователь не найден", show_alert=True)
        return
    name = " ".join(x for x in [u["first_name"], u["last_name"]] if x) or "—"
    username = f"@{u['username']}" if u["username"] else "—"
    text = (
        "👤 <b>Пользователь</b>\n\n"
        f"Имя: <b>{name}</b>\nUsername: <b>{username}</b>\nID: <code>{uid}</code>\n"
        f"Статус: <b>{'ЗАБЛОКИРОВАН' if u['is_banned'] else 'активен'}</b>\n\n"
        f"💬 Сообщений: <b>{u['messages']}</b>\n"
        f"🖼 Изображений: <b>{u['images']}</b>\n"
        f"🔎 Поисков: <b>{u['searches']}</b>\n"
        f"Последняя активность: <code>{u['last_seen'][:19]}</code>"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_user_keyboard(uid, u["is_banned"]))
    await call.answer()

@dp.callback_query(F.data.startswith("adm:ban:"))
async def admin_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, _, uid, value = call.data.split(":")
    uid, value = int(uid), bool(int(value))
    if uid == ADMIN_ID:
        await call.answer("Себя заблокировать нельзя.", show_alert=True)
        return
    await db.set_banned(uid, value)
    await call.answer("Пользователь заблокирован." if value else "Пользователь разблокирован.")
    await admin_user(call)

@dp.callback_query(F.data == "adm:broadcast")
async def admin_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    admin_broadcast_mode.add(call.from_user.id)
    await call.message.answer(
        "📢 <b>Режим рассылки</b>\n\n"
        "Отправь следующим сообщением текст, который получат все незаблокированные пользователи.\n"
        "Для отмены отправь /cancel.",
        parse_mode="HTML"
    )
    await call.answer()

async def main():
    await db.init_db()
    await db.set_admin(ADMIN_ID, True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# Railway deployment marker
