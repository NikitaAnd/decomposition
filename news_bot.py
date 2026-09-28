import os
import asyncio
import hashlib
import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote_plus, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "-1003884967892")
AI_URL = os.getenv("AI_URL", "https://one-ai-openai-proxy-production.up.railway.app/v1/chat/completions")
AI_KEY = os.getenv("AI_KEY", "")
MODEL = os.getenv("MODEL", "gpt-5")
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "30"))
NEWS_LIMIT = int(os.getenv("NEWS_LIMIT", "12"))
NEWS_QUERY = os.getenv(
    "NEWS_QUERY",
    "технологии OR искусственный интеллект OR Россия OR мир OR Minecraft OR игры",
)
DB_PATH = os.getenv("DB_PATH", "/tmp/newsbot.db")

bot = Bot(BOT_TOKEN)
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TelegramNewsBot/1.0)"}

SCHEMA = """CREATE TABLE IF NOT EXISTS posted (
    key TEXT PRIMARY KEY,
    url TEXT,
    title TEXT,
    posted_at TEXT
)"""

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    conn.commit()
    return conn

def already_posted(key: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM posted WHERE key=?", (key,)).fetchone() is not None

def mark_posted(item):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted(key,url,title,posted_at) VALUES(?,?,?,?)",
            (item["key"], item["url"], item["title"], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

def strip_html(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()

async def fetch_news():
    import xml.etree.ElementTree as ET
    url = "https://news.google.com/rss/search?q=" + quote_plus(NEWS_QUERY) + "&hl=ru&gl=RU&ceid=RU:ru"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HTTP_HEADERS) as client:
        r = await client.get(url)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    items = []
    for x in root.findall(".//item"):
        title = strip_html(x.findtext("title") or "")
        link = (x.findtext("link") or "").strip()
        desc_raw = x.findtext("description") or ""
        desc = strip_html(desc_raw)
        pub = (x.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        key = hashlib.sha256(link.encode()).hexdigest()[:32]
        if already_posted(key):
            continue
        img = None
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', desc_raw, re.I)
        if m:
            img = html.unescape(m.group(1))
        items.append({"key": key, "title": title, "url": link, "description": desc[:1800], "date": pub, "image": img})
        if len(items) >= NEWS_LIMIT:
            break
    return items

async def article_image(client, url):
    if url and url.startswith("http"):
        try:
            r = await client.get(url)
            if r.status_code < 400 and r.headers.get("content-type","").startswith("image/"):
                return r.content
        except Exception:
            pass
    try:
        r = await client.get(url)
        if r.status_code >= 400:
            return None
        text = r.text[:800000]
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        ]:
            m = re.search(pat, text, re.I)
            if m:
                ir = m.group(1).replace("&amp;", "&")
                try:
                    img = await client.get(ir)
                    if img.status_code < 400 and img.headers.get("content-type","").startswith("image/"):
                        return img.content
                except Exception:
                    pass
    except Exception:
        pass
    return None

async def choose_and_write(items):
    compact = []
    for i, x in enumerate(items):
        compact.append({
            "id": i,
            "title": x["title"],
            "description": x["description"][:700],
            "date": x["date"],
            "source_url": x["url"],
            "has_image": bool(x["image"]),
        })
    prompt = """Ты редактор Telegram-новостного канала. Из списка выбери ОДНУ наиболее актуальную и содержательно полезную новость.
Не выдумывай факты. Если данных мало, формулируй осторожно. Верни СТРОГО JSON:
{"id": number, "headline": "короткий заголовок", "text": "2-4 коротких абзаца новости", "why": "одно предложение о значимости", "image_id": number}
image_id — индекс новости, чью картинку лучше использовать. Если у выбранной новости нет изображения, выбери image_id с подходящей картинкой из списка.
Стиль: живой, нейтральный, без кликбейта. Не называй источники внутри текста — ссылка будет добавлена отдельно.

КАНДИДАТЫ:
""" + json.dumps(compact, ensure_ascii=False)
    headers = {"Content-Type": "application/json"}
    if AI_KEY:
        headers["Authorization"] = f"Bearer {AI_KEY}"
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": "Ты редактор новостей. Отвечай только валидным JSON."},
        {"role": "user", "content": prompt},
    ], "stream": True}
    parts = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20, read=180, write=30, pool=30)) as client:
        async with client.stream("POST", AI_URL, headers=headers, json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    obj = json.loads(raw)
                    choices = obj.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str):
                            parts.append(piece)
                except json.JSONDecodeError:
                    continue
    content = "".join(parts)
    if not content:
        raise RuntimeError("GPT returned empty content")
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        raise RuntimeError("GPT returned non-JSON")
    result = json.loads(m.group(0))
    if not isinstance(result.get("id"), int):
        raise RuntimeError("Invalid GPT selection")
    return result

def font(size):
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

def edit_image(raw, headline):
    if not raw:
        return None
    try:
        im = Image.open(BytesIO(raw)).convert("RGB")
        im = ImageOps.fit(im, (1280, 720), method=Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(im, "RGBA")
        # Readable dark lower panel; no destructive modification of the original.
        panel_h = 205
        draw.rectangle((0, 515, 1280, 720), fill=(0, 0, 0, 175))
        f = font(42)
        words = headline.split()
        lines, line = [], ""
        for w in words:
            test = (line + " " + w).strip()
            if draw.textbbox((0,0), test, font=f)[2] <= 1160:
                line = test
            else:
                if line: lines.append(line)
                line = w
        if line: lines.append(line)
        lines = lines[:4]
        y = 540
        for line in lines:
            draw.text((60, y), line, font=f, fill="white", stroke_width=1, stroke_fill="black")
            y += 45
        out = BytesIO()
        im.save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception:
        log.exception("Image editing failed")
        return None

async def publish():
    items = await fetch_news()
    if not items:
        log.info("No new news candidates")
        return False
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HTTP_HEADERS) as client:
        for x in items:
            if not x["image"]:
                x["image"] = await article_image(client, x["url"])
            elif isinstance(x["image"], str):
                try:
                    ir = await client.get(x["image"])
                    x["image"] = ir.content if ir.status_code < 400 else None
                except Exception:
                    x["image"] = None
    result = await choose_and_write(items)
    selected_id = result["id"]
    if selected_id < 0 or selected_id >= len(items):
        selected_id = 0
    selected = items[selected_id]
    image_id = result.get("image_id", selected_id)
    if not isinstance(image_id, int) or image_id < 0 or image_id >= len(items):
        image_id = selected_id
    image = edit_image(items[image_id].get("image"), result.get("headline", selected["title"]))
    source_url = selected["url"]
    text = (
        f"<b>{html.escape(result.get('headline', selected['title']))}</b>\n\n"
        f"{html.escape(result.get('text', selected['description']))}\n\n"
        f"<i>{html.escape(result.get('why', ''))}</i>\n\n"
        f"🔗 <a href=\"{html.escape(source_url, quote=True)}\">Источник</a>"
    )
    try:
        if image:
            await bot.send_photo(CHANNEL_ID, BufferedInputFile(image, filename="news.jpg"), caption=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        mark_posted(selected)
        log.info("Published: %s", result.get("headline"))
        return True
    except Exception:
        log.exception("Telegram publish failed")
        return False

async def main():
    db()
    log.info("News bot started: channel=%s interval=%sm model=%s", CHANNEL_ID, INTERVAL_MINUTES, MODEL)
    while True:
        try:
            await publish()
        except Exception:
            log.exception("News cycle failed")
        await asyncio.sleep(INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    asyncio.run(main())
