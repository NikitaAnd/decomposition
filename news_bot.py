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
AI_KEY=os.getenv("AI_KEY",""); MODEL=os.getenv("MODEL","gpt-5-mini"); NEWS_LIMIT=int(os.getenv("NEWS_LIMIT","8"))
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
        for tag in list(x):
            if tag.tag.lower().endswith(("content","thumbnail","enclosure")):
                u=tag.attrib.get("url") or tag.attrib.get("href")
                if u: imgs.append(u)
        out.append({"key":k,"title":title,"url":link,"description":clean(desc_raw)[:1800],"date":x.findtext("pubDate") or "","image_urls":[html.unescape(u) for u in imgs],"images":[]})
        if len(out)>=NEWS_LIMIT: break
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
            if len(item["images"])>=4: break
        except Exception: pass

async def search_web_images(client, item):
    query=clean(item["title"])
    query=re.sub(r"\b(?:19|20)\d{2}\b"," ",query)
    query=re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b"," ",query)
    query=clean(query)
    url="https://www.bing.com/images/search?q="+quote_plus(query)+"&form=HDRSC2&first=1"
    try:
        r=await client.get(url)
        if r.status_code>=400: log.warning("Bing image search HTTP %s",r.status_code); return []
        found=[]
        for m in re.finditer(r'class=["']iusc["'][^>]+m=["']([^"']+)["']',r.text,re.I):
            try:
                meta=json.loads(html.unescape(m.group(1)))
                u=meta.get("murl") or meta.get("turl")
                if u and u.startswith("http"): result_title=clean(meta.get("t",""))
                title_tokens={x for x in re.findall(r"[a-zа-яё0-9]+",query.lower()) if len(x)>=4 and x not in {"новый","новые","новых","для","работы","работе","повседневных","дел","дела","сервис","сервисы","сервисов","искусственный","интеллект","ии","запустил","запустили","доступны","доступен"}}
                result_tokens=set(re.findall(r"[a-zа-яё0-9]+",result_title.lower()))
                if title_tokens and not (title_tokens & result_tokens):
                    continue
                found.append({"url":u,"context":meta.get("purl",""),"title":result_title})
            except Exception: pass
        out=[]; seen=set()
        for cand in found:
            u=cand["url"]
            if u in seen: continue
            seen.add(u)
            try:
                rr=await client.get(u)
                ct=rr.headers.get("content-type","").lower()
                if rr.status_code>=400 or not ct.startswith("image/") or len(rr.content)<12000: continue
                im=Image.open(BytesIO(rr.content)); w,h=im.size
                if w<500 or h<300 or not .5<=w/h<=2.5: continue
                out.append({"bytes":rr.content,"w":w,"h":h,"url":u,"context":cand["context"]})
                if len(out)>=8: break
            except Exception: pass
        return out
    except Exception as e:
        log.warning("Bing image search failed: %s",e); return []

async def ai(items):
    candidates=[{"id":i,"title":x["title"],"description":x["description"][:1200],"date":x["date"]}
                for i,x in enumerate(items)]
    prompt="""Ты редактор популярного Telegram-канала. Пиши ДОКУМЕНТАЛЬНО ТОЧНЫЕ новости простыми словами.

ПРАВИЛА ФАКТОВ:
— Используй только сведения, которые прямо есть в title, description и date кандидата.
— Ничего не выдумывай и не усиливай ради кликабельности.
— Не придумывай реакции людей, общественный резонанс, «споры», «шок», причины, последствия, цитаты, цифры или детали, которых нет в исходных данных.
— Если речь об игре, приложении, фильме или другом вымышленном произведении, не называй его сюжет реальным событием. Новость должна быть о самом произведении.
— Если известно, что произведение уже существует/доступно, не пиши «появилась», если это не подтверждено данными.
— Не добавляй дисклеймеры вроде «это не новости» или «воспринимайте как художественный сюжет», если они не нужны для точности.
— Если данных мало, напиши только то, что действительно известно.
— Не выдавай придуманные реакции за реальные комментарии пользователей.

СТИЛЬ:
— Простые слова, понятные всем.
— Живой разговорный русский, без канцелярита и без выдуманных эмоций.
— Заголовок сразу говорит, что произошло.
— В начале заголовка можно поставить 1 подходящий эмодзи.
— В заголовке выделяй <b>самое важное</b>.
— 2–4 коротких абзаца.
— Если есть подтверждённый главный факт, отдельный абзац начинай с «➖ » и выдели его <b>жирным</b>.
— Не упоминай источник, URL, название СМИ или хэштеги.
— В конце дай 2 короткие реакции как мнение/эмоцию, но не выдавай их за фактические комментарии пользователей.
— Не пиши «реакции смешанные», «споры гарантированы», «тема на нервах» и подобные утверждения без подтверждения.

Пример логики: если кандидат сообщает, что существует игра про побег из военкомата и даёт подтверждённые сведения об игре, сообщай именно о существовании игры, её сюжете и этих сведениях. Не превращай сюжет игры в реальную новость.

Верни ТОЛЬКО JSON:
{"id":0,"headline":"⚡️ <b>живой точный заголовок</b>","paragraphs":["конкретный факт с деталями","ещё один конкретный факт","➖ <b>главный подтверждённый итог</b>"],"reactions":["🤖 — короткая живая реакция","🔥 — ещё одна живая реакция"]}


Картинка:
— image_id должен указывать на реальную фотографию из списка images выбранной новости.
— Выбирай фотографию, которая максимально связана с событием, а не логотип или баннер.
— Выбирай только новости, у которых есть хотя бы одна реальная фотография в images.
— Никогда не выбирай несуществующий image_id.

КАНДИДАТЫ:
"""+json.dumps(candidates,ensure_ascii=False)
    h={"Content-Type":"application/json"}
    if AI_KEY: h["Authorization"]="Bearer "+AI_KEY
    payload={"model":MODEL,"messages":[{"role":"system","content":"Ты профессиональный редактор Telegram-новостей. Возвращай только валидный JSON без markdown-обёртки."},{"role":"user","content":prompt}],"max_tokens":700,"stream":True}
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
    raw_text="".join(parts).strip()
    if not raw_text:
        raise RuntimeError("GPT returned empty response")
    # GPT иногда оборачивает JSON в markdown или добавляет пояснение.
    # Надёжно достаём первый валидный JSON-объект, не ломаясь на фигурных скобках внутри строк.
    cleaned=re.sub(r"^\\s*\\`\\`\\`(?:json)?\\s*", "", raw_text, flags=re.I)
    cleaned=re.sub(r"\\s*\\`\\`\\`\\s*$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder=json.JSONDecoder()
        for pos,ch in enumerate(cleaned):
            if ch != "{": continue
            try:
                obj,_=decoder.raw_decode(cleaned[pos:])
                if isinstance(obj,dict): return obj
            except json.JSONDecodeError:
                continue
    log.error("GPT raw response (first 4000 chars): %s", raw_text[:4000])
    raise RuntimeError("GPT returned non-JSON")


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
    if not items:
        log.info("No new news candidates")
        return False

    # Сначала выбираем новость. Не тратим время на загрузку картинок для всех кандидатов.
    try:
        r=await ai(items)
    except Exception:
        log.exception("AI selection failed; using first RSS candidate as fallback")
        first=items[0]
        r={
            "id":0,
            "headline":"📰 <b>"+html.escape(first["title"])+"</b>",
            "paragraphs":[html.escape(first["description"][:700])],
            "reactions":[]
        }

    sid=r.get("id",0)
    sid=sid if isinstance(sid,int) and 0<=sid<len(items) else 0
    selected=items[sid]

    # Картинку ищем ТОЛЬКО для уже выбранной новости.
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=HEAD) as client:
        await get_images(client,selected)
        if not selected["images"]:
            selected["images"]=await search_web_images(client,selected)

    text=format_post(r)

    try:
        if selected["images"]:
            image=prepare_image(selected["images"][0]["bytes"])
            if image:
                await bot.send_photo(
                    CHANNEL_ID,
                    BufferedInputFile(image,filename="news.jpg"),
                    caption=text,
                    parse_mode=ParseMode.HTML
                )
                log.info("Published with image: %s",r.get("headline"))
            else:
                await bot.send_message(CHANNEL_ID,text,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
                log.info("Published text-only: image prepare failed")
        else:
            await bot.send_message(CHANNEL_ID,text,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
            log.info("Published text-only: no image")

        mark(selected)
        return True
    except Exception:
        log.exception("Telegram publish failed")
        return False

async def main():
    db(); log.info("News bot job started: channel=%s model=%s",CHANNEL_ID,MODEL)
    try: await publish()
    except Exception: log.exception("News cycle failed")

if __name__=="__main__": asyncio.run(main())
