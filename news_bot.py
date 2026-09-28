import os, asyncio, hashlib, html, json, logging, re, sqlite3
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote_plus, urljoin
import httpx
from PIL import Image, ImageOps
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("news-bot")
BOT_TOKEN=os.environ["BOT_TOKEN"]; CHANNEL_ID=os.getenv("CHANNEL_ID","-1003884967892")
AI_URL=os.getenv("AI_URL","https://one-ai-openai-proxy-production.up.railway.app/v1/chat/completions")
AI_KEY=os.getenv("AI_KEY",""); MODEL=os.getenv("MODEL","gpt-5"); NEWS_LIMIT=int(os.getenv("NEWS_LIMIT","12"))
NEWS_QUERY=os.getenv("NEWS_QUERY","технологии OR искусственный интеллект OR Россия OR мир OR Minecraft OR игры")
DB_PATH=os.getenv("DB_PATH","/tmp/newsbot.db"); bot=Bot(BOT_TOKEN)
HEAD={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
SCHEMA="CREATE TABLE IF NOT EXISTS posted (key TEXT PRIMARY KEY,url TEXT,title TEXT,posted_at TEXT)"

def db():
    c=sqlite3.connect(DB_PATH); c.execute(SCHEMA); c.commit(); return c
def posted(k):
    with db() as c: return c.execute("SELECT 1 FROM posted WHERE key=?",(k,)).fetchone() is not None
def mark(x):
    with db() as c: c.execute("INSERT OR IGNORE INTO posted VALUES(?,?,?,?)",(x["key"],x["url"],x["title"],datetime.now(timezone.utc).isoformat())); c.commit()
def clean(s):
    s=re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>"," ",s or "",flags=re.I)
    return html.unescape(re.sub(r"\s+"," ",s)).strip()

async def fetch_news():
    import xml.etree.ElementTree as ET
    u="https://news.google.com/rss/search?q="+quote_plus(NEWS_QUERY)+"&hl=ru&gl=RU&ceid=RU:ru"
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=HEAD) as c:
        r=await c.get(u); r.raise_for_status(); root=ET.fromstring(r.text)
    out=[]
    for x in root.findall(".//item"):
        title=clean(x.findtext("title")); link=(x.findtext("link") or "").strip(); desc_raw=x.findtext("description") or ""
        if not title or not link: continue
        k=hashlib.sha256(link.encode()).hexdigest()[:32]
        if posted(k): continue
        imgs=re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)',desc_raw,re.I)
        out.append({"key":k,"title":title,"url":link,"description":clean(desc_raw)[:1800],"date":x.findtext("pubDate") or "","image_urls":[html.unescape(u) for u in imgs],"images":[]})
        if len(out)>=max(40,NEWS_LIMIT): break
    return out

def image_urls(page,base):
    found=[]
    pats=[
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|og:image:url|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|og:image:url|twitter:image(?::src)?)["\']',
        r'<link[^>]+rel=["\'][^"\']*image_src[^"\']*["\'][^>]+href=["\']([^"\']+)'
    ]
    for p in pats:
        found += [urljoin(base,html.unescape(m.group(1))) for m in re.finditer(p,page,re.I)]
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',page,re.I):
        try:
            d=json.loads(html.unescape(m.group(1))); stack=d if isinstance(d,list) else [d]
            for o in stack:
                if not isinstance(o,dict): continue
                for key in ("image","thumbnailUrl","contentUrl"):
                    v=o.get(key); vals=v if isinstance(v,list) else [v]
                    for z in vals:
                        if isinstance(z,dict): z=z.get("url") or z.get("contentUrl")
                        if isinstance(z,str): found.append(urljoin(base,z))
        except Exception: pass
    bad=("logo","favicon","avatar","icon","sprite","emoji","placeholder","default-image")
    return list(dict.fromkeys(u for u in found if u.startswith("http") and not any(b in u.lower() for b in bad)))[:20]

async def get_images(client,item):
    urls=list(item["image_urls"])
    try:
        r=await client.get(item["url"])
        if r.status_code<400: urls += image_urls(r.text[:2500000],str(r.url))
    except Exception as e: log.warning("article fetch failed: %s",e)
    seen=set()
    for u in urls:
        if u in seen: continue
        seen.add(u)
        try:
            r=await client.get(u)
            ct=r.headers.get("content-type","").lower()
            if r.status_code>=400 or not ct.startswith("image/") or len(r.content)<20000: continue
            im=Image.open(BytesIO(r.content)); w,h=im.size
            if w<200 or h<150 or not .45<=w/h<=3.5: continue
            item["images"].append({"bytes":r.content,"w":w,"h":h,"url":u})
            if len(item["images"])>=10: break
        except Exception: pass

async def ai(items):
    candidates=[{"id":i,"title":x["title"],"description":x["description"][:1200],"date":x["date"],
                 "images":[{"id":j,"width":z["w"],"height":z["h"]} for j,z in enumerate(x["images"])]}
                for i,x in enumerate(items)]
    prompt="""Ты редактор популярного Telegram-канала с новостями. Выбери одну новость и напиши пост ПРОСТЫМИ СЛОВАМИ, понятными любому человеку.

Стиль:
— Не сухая статья и не пресс-релиз.
— Живой разговорный русский без канцелярита.
— Заголовок должен цеплять и сразу говорить, что произошло.
— Можно использовать 1 подходящий эмодзи в начале заголовка: ‼️ ⚡️ 🔥 🗿 😳 и т.п., но не ставь эмодзи случайно.
— В заголовке выделяй <b>самое важное</b>: событие, человека, сумму, цифру.
— 2–4 коротких абзаца. Каждый абзац — 1–3 предложения.
— Не начинай абзацы словами «согласно данным», «стало известно», «эксперты отмечают», если без этого можно обойтись.
— Пиши так, будто объясняешь новость другу.
— Если есть важный итог, отдельный абзац начинай с «➖ » и выдели ключевую часть <b>жирным</b>.
— Не делай списки и blockquote без реальной необходимости.
— В конце дай 2 короткие реакции, естественные для этой новости. Можно использовать 💩, ❤️, 😡, 🔥 и т.п.
— Не задавай вопрос «Как вам...».
— Не выдумывай факты, причины, цитаты, диагнозы, травмы, суммы или детали.
— Не упоминай источник, сайт, URL, хэштеги или название СМИ.
— Не копируй формулировки из примера буквально.

Верни ТОЛЬКО JSON:
{"id":0,"headline":"‼️ <b>живой заголовок</b>","paragraphs":["абзац","абзац","➖ <b>главный итог</b>"],"reactions":["💩 *— реакция*","*❤️* *— реакция*"],"image_id":0}

Картинка:
— image_id должен указывать на реальную фотографию из списка images выбранной новости.
— Выбирай фотографию, которая максимально связана с событием, а не логотип или баннер.
— Если у новости нет images, image_id поставь 0; бот сам найдёт фото отдельным поиском.
— Никогда не выбирай несуществующий image_id.

КАНДИДАТЫ:
"""+json.dumps(candidates,ensure_ascii=False)
    h={"Content-Type":"application/json"}
    if AI_KEY: h["Authorization"]="Bearer "+AI_KEY
    payload={"model":MODEL,"messages":[{"role":"system","content":"Ты профессиональный Telegram-редактор. Возвращай только валидный JSON без markdown-обёртки."},{"role":"user","content":prompt}],"stream":True}
    parts=[]
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20,read=180,write=30,pool=30)) as c:
        async with c.stream("POST",AI_URL,headers=h,json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                raw=line[5:].strip()
                if raw=="[DONE]": break
                try:
                    ch=json.loads(raw).get("choices") or []
                    if ch:
                        p=(ch[0].get("delta") or {}).get("content")
                        if isinstance(p,str): parts.append(p)
                except json.JSONDecodeError: pass
    m=re.search(r"\{[\s\S]*\}","".join(parts))
    if not m: raise RuntimeError("GPT returned non-JSON")
    return json.loads(m.group())


async def ai_image_query(title, description):
    prompt = "Дай короткий поисковый запрос для реальной фотографии к этой новости. Верни только JSON {\"query\":\"...\"}. Новость: " + title + " " + description[:700]
    h={"Content-Type":"application/json"}
    if AI_KEY: h["Authorization"]="Bearer "+AI_KEY
    payload={"model":MODEL,"messages":[{"role":"user","content":prompt}],"stream":True}
    parts=[]
    async with httpx.AsyncClient(timeout=120) as cc:
        async with cc.stream("POST",AI_URL,headers=h,json=payload) as rr:
            rr.raise_for_status()
            async for line in rr.aiter_lines():
                if not line.startswith("data:"): continue
                raw=line[5:].strip()
                if raw=="[DONE]": break
                try:
                    ch=json.loads(raw).get("choices") or []
                    if ch:
                        p=(ch[0].get("delta") or {}).get("content")
                        if isinstance(p,str): parts.append(p)
                except json.JSONDecodeError: pass
    m=re.search(r"\{[\s\S]*?\}","".join(parts))
    if m:
        try: return str(json.loads(m.group()).get("query") or title)
        except Exception: pass
    return title

async def search_real_photo(item):
    query=await ai_image_query(item["title"],item["description"])
    urls=[]
    try:
        u="https://www.google.com/search?tbm=isch&q="+quote_plus(query)+"&hl=ru"
        async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=HEAD) as cc:
            rr=await cc.get(u)
        for m in re.finditer(r'https?://[^" ]+',rr.text,re.I):
            u=html.unescape(m.group(0)).replace("\\/","/")
            if any(x in u.lower() for x in ("google.com","gstatic.com","googleusercontent.com","favicon","logo")): continue
            urls.append(u)
            if len(urls)>=30: break
    except Exception as e:
        log.warning("image search failed: %s",e)
    for u in urls:
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=HEAD) as cc:
                rr=await cc.get(u)
            if not rr.headers.get("content-type","").startswith("image/") or len(rr.content)<20000: continue
            im=Image.open(BytesIO(rr.content)); w,h=im.size
            if w>=400 and h>=250 and .45<=w/h<=3.5:
                return {"bytes":rr.content,"w":w,"h":h,"url":u}
        except Exception: pass
    return None

def safe(s):
    return re.sub(r"<(?!/?(?:b|i)\b)[^>]*>","",str(s or ""),flags=re.I).strip()

def format_post(r):
    p=[safe(r.get("headline"))]
    p += [safe(x) for x in r.get("paragraphs",[]) if str(x).strip()][:4]
    opts=[safe(x) for x in r.get("reactions",[]) if str(x).strip()][:2]
    if opts: p.append("\n".join(opts))
    return "\n\n".join(p)

def prepare_image(raw):
    if not raw: return None
    try:
        im=ImageOps.fit(Image.open(BytesIO(raw)).convert("RGB"),(1280,720),method=Image.Resampling.LANCZOS)
        o=BytesIO(); im.save(o,"JPEG",quality=94,optimize=True,progressive=True); return o.getvalue()
    except Exception: return None

async def publish():
    items=await fetch_news()
    if not items: log.info("No new news candidates"); return False
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=HEAD) as c:
        for x in items: await get_images(c,x)
    r=await ai(items)
    sid=r.get("id",0); sid=sid if isinstance(sid,int) and 0<=sid<len(items) else 0
    selected=items[sid]
    iid=r.get("image_id",0); iid=iid if isinstance(iid,int) and 0<=iid<len(selected["images"]) else 0
    image=prepare_image(selected["images"][iid]["bytes"]) if selected["images"] else None
    if image is None:
        found=await search_real_photo(selected)
        if found:
            image=prepare_image(found["bytes"])
            selected["images"].append(found)
            iid=len(selected["images"])-1
    if image is None:
        for alt in items[:8]:
            if alt is selected: continue
            found=await search_real_photo(alt)
            if found:
                selected=alt
                image=prepare_image(found["bytes"])
                selected["images"].append(found)
                iid=len(selected["images"])-1
                break
    if image is None:
        log.warning("No real photo found; nothing published")
        return False
    text=format_post(r)
    try:
        if image:
            await bot.send_photo(CHANNEL_ID,BufferedInputFile(image,filename="news.jpg"),caption=text,parse_mode=ParseMode.HTML)
        else:
            log.error("Selected image could not be prepared; skipping post")
            return False
        mark(selected); log.info("Published: %s | image=%s",r.get("headline"),selected["images"][iid]["url"]); return True
    except Exception: log.exception("Telegram publish failed"); return False

async def main():
    db(); log.info("News bot job started: channel=%s model=%s",CHANNEL_ID,MODEL)
    try: await publish()
    except Exception: log.exception("News cycle failed")

if __name__=="__main__": asyncio.run(main())
