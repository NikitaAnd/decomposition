import os, asyncio, hashlib, html, json, logging, re, sqlite3
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote_plus, urljoin
import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
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
HEAD={"User-Agent":"Mozilla/5.0 (compatible; TelegramNewsBot/2.0)"}
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
        m=re.search(r'<img[^>]+src=["\']([^"\']+)',desc_raw,re.I)
        out.append({"key":k,"title":title,"url":link,"description":clean(desc_raw)[:1800],"date":x.findtext("pubDate") or "","image_urls":[html.unescape(m.group(1))] if m else [],"images":[]})
        if len(out)>=NEWS_LIMIT: break
    return out

def image_urls(page,base):
    found=[]
    pats=[r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)',
          r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image(?::src)?)["\']']
    for p in pats:
        found += [urljoin(base,html.unescape(m.group(1))) for m in re.finditer(p,page,re.I)]
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',page,re.I):
        try:
            d=json.loads(html.unescape(m.group(1))); stack=d if isinstance(d,list) else [d]
            for o in stack:
                if not isinstance(o,dict): continue
                v=o.get("image") or o.get("thumbnailUrl")
                vals=v if isinstance(v,list) else [v]
                for z in vals:
                    if isinstance(z,dict): z=z.get("url")
                    if isinstance(z,str): found.append(urljoin(base,z))
        except Exception: pass
    bad=("logo","favicon","avatar","icon","sprite","emoji")
    return list(dict.fromkeys(u for u in found if u.startswith("http") and not any(b in u.lower() for b in bad)))[:12]

async def get_images(client,item):
    urls=list(item["image_urls"])
    try:
        r=await client.get(item["url"])
        if r.status_code<400: urls += image_urls(r.text[:1200000],str(r.url))
    except Exception: pass
    for u in dict.fromkeys(urls):
        try:
            r=await client.get(u)
            if r.status_code>=400 or not r.headers.get("content-type","").startswith("image/") or len(r.content)<15000: continue
            im=Image.open(BytesIO(r.content)); w,h=im.size
            if w>=500 and h>=300 and .65<=w/h<=3.2: item["images"].append({"bytes":r.content,"w":w,"h":h})
        except Exception: pass
    item["images"]=item["images"][:8]

async def ai(items):
    candidates=[]
    for i,x in enumerate(items):
        candidates.append({"id":i,"title":x["title"],"description":x["description"][:900],"date":x["date"],
                           "images":[{"id":j,"width":z["w"],"height":z["h"]} for j,z in enumerate(x["images"]) ]})
    prompt="""Ты редактор Telegram-новостей. Выбери одну самую свежую и содержательно важную новость. Не выдумывай факты.
Верни только JSON:
{"id":0,"headline":"⚡️ короткий живой заголовок","lead":"1-2 предложения с <b>акцентами</b>","body":["абзац с <b>акцентами</b>"],"quote_lines":["🧡 Факт — <b>значение</b>"],"closing":"🧡 Короткий вывод с <b>акцентом</b>","poll_options":["❤️ — вариант","💩 — вариант","💩 — вариант"],"image_id":0}
Правила: это должен быть живой Telegram-пост, не сухая статья. Жирным выделяй цифры, даты, суммы и ключевые детали. Если есть несколько условий/цифр — используй quote_lines. Всегда дай 3 poll_options. НЕ добавляй источник, URL, ссылки, название сайта или хэштеги. Только HTML-теги <b> и <i>. image_id — номер реальной картинки выбранной новости, начиная с 0.
КАНДИДАТЫ:
"""+json.dumps(candidates,ensure_ascii=False)
    h={"Content-Type":"application/json"}; 
    if AI_KEY: h["Authorization"]="Bearer "+AI_KEY
    payload={"model":MODEL,"messages":[{"role":"system","content":"Ты профессиональный Telegram-редактор. Только валидный JSON."},{"role":"user","content":prompt}],"stream":True}
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

def safe(s):
    return re.sub(r"<(?!/?(?:b|i|blockquote)\b)[^>]*>","",str(s or ""),flags=re.I).strip()

def format_post(r):
    p=[f"<b>{safe(r.get('headline'))}</b>"]
    if r.get("lead"): p.append(safe(r["lead"]))
    p += [safe(x) for x in r.get("body",[]) if str(x).strip()][:4]
    q=[safe(x) for x in r.get("quote_lines",[]) if str(x).strip()]
    if q: p.append("<blockquote>"+"\n".join(q[:6])+"</blockquote>")
    if r.get("closing"): p.append(safe(r["closing"]))
    opts=[safe(x) for x in r.get("poll_options",[]) if str(x).strip()][:3]
    if opts: p.append("\n".join("<i>"+x+"</i>" for x in opts))
    return "\n\n".join(p)

def font(n):
    try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",n)
    except Exception: return ImageFont.load_default()

def edit(raw,title):
    if not raw:return None
    try:
        im=ImageOps.fit(Image.open(BytesIO(raw)).convert("RGB"),(1280,720),method=Image.Resampling.LANCZOS); d=ImageDraw.Draw(im,"RGBA")
        for y in range(470,720): d.line((0,y,1280,y),fill=(5,8,12,int(30+190*(y-470)/250)))
        d.rectangle((0,470,10,720),fill=(178,250,114,255)); f=font(40); words=re.sub(r"<[^>]+>","",title or "").split(); lines=[]; line=""
        for w in words:
            t=(line+" "+w).strip()
            if d.textbbox((0,0),t,font=f)[2]<=1120: line=t
            else:
                if line:lines.append(line)
                line=w
        if line:lines.append(line)
        for i,l in enumerate(lines[:4]): d.text((48,515+i*46),l,font=f,fill="white",stroke_width=1,stroke_fill="black")
        o=BytesIO(); im.save(o,"JPEG",quality=92,optimize=True); return o.getvalue()
    except Exception: return None

async def publish():
    items=await fetch_news()
    if not items: log.info("No new news candidates"); return False
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=HEAD) as c:
        for x in items: await get_images(c,x)
    r=await ai(items); sid=r.get("id",0); sid=sid if 0<=sid<len(items) else 0; selected=items[sid]
    iid=r.get("image_id",0); images=selected["images"]; iid=iid if isinstance(iid,int) and 0<=iid<len(images) else 0
    image=edit(images[iid]["bytes"],r.get("headline")) if images else None
    text=format_post(r)
    try:
        if image: await bot.send_photo(CHANNEL_ID,BufferedInputFile(image,filename="news.jpg"),caption=text,parse_mode=ParseMode.HTML)
        else: await bot.send_message(CHANNEL_ID,text,parse_mode=ParseMode.HTML)
        mark(selected); log.info("Published: %s",r.get("headline")); return True
    except Exception: log.exception("Telegram publish failed"); return False

async def main():
    db(); log.info("News bot job started: channel=%s model=%s",CHANNEL_ID,MODEL)
    try: await publish()
    except Exception: log.exception("News cycle failed")

if __name__=="__main__": asyncio.run(main())
