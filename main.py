import os, json, time, uuid, hmac, hashlib, base64, logging
from datetime import datetime
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("one-ai-proxy")

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "https://chat-ai.begamob.com").rstrip("/")
UPSTREAM_ENDPOINT = "/api/v3/chat/stream"
UPSTREAM_SECRET = os.getenv("UPSTREAM_SECRET", "stteam-ikameglobal-chatapiopenai")
APP_ID = os.getenv("APP_ID", "com.chat.chatai.chatbot.aichatbot")
APP_VERSION = os.getenv("APP_VERSION", "1.5.3")
ANDROID_VERSION = os.getenv("ANDROID_VERSION", "16")
SERVER_API_KEY = os.getenv("SERVER_API_KEY", "")
DEVICE_ID = os.getenv("DEVICE_ID", uuid.uuid4().hex.upper())
UPSTREAM_USER_ID = os.getenv("UPSTREAM_USER_ID", "")
TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "120"))

app = FastAPI(title="OpenAI Compatible One AI Proxy", version="1.0.0")

class Message(BaseModel):
    role: str
    content: Any

class ChatRequest(BaseModel):
    model: str = "gpt-5"
    messages: list[Message]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def make_jwt() -> str:
    now_ms = int(time.time() * 1000)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iat": now_ms,
        "exp": (now_ms + 1800000) // 1000,
        "bundleId": APP_ID,
        "os": "Android",
        "versionApp": APP_VERSION,
        "timezone": datetime.now().astimezone().tzinfo.tzname(None),
    }
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(UPSTREAM_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"

def make_user_header() -> str:
    return (
        f"bundleId:{APP_ID}/versionApp:{APP_VERSION}/OS:Android/"
        f"osVersion:{ANDROID_VERSION}/userId:{UPSTREAM_USER_ID}/deviceId:{DEVICE_ID}"
    )

def client_auth(authorization: str | None):
    if not SERVER_API_KEY:
        return
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, SERVER_API_KEY):
        raise HTTPException(401, "Invalid API key")

def normalize_content(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    result = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                result.append({"type": "text", "text": text})
        elif part_type in ("image_url", "image"):
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                detail = image_url.get("detail", "auto")
            else:
                url = image_url
                detail = "auto"
            if isinstance(url, str) and url:
                # Native One AI uses "image_url" as the content discriminator.
                # Its Android client also wraps JPEG base64 as data:image/jpeg;base64,{...}.
                if isinstance(url, str) and url.startswith("data:image/jpeg;base64,"):
                    prefix, encoded = url.split(",", 1)
                    if not (encoded.startswith("{") and encoded.endswith("}")):
                        url = prefix + ",{" + encoded + "}"
                result.append({
                    "type": "image_url",
                    "text": None,
                    "image_url": {
                        "detail": detail if isinstance(detail, str) else "auto",
                        "url": url,
                    },
                })
    return result or [{"type": "text", "text": ""}]

def make_payload(req: ChatRequest) -> dict:
    return {
        "max_tokens": req.max_tokens,
        "messages": [{"role": m.role, "content": normalize_content(m.content)} for m in req.messages],
        "model": req.model,
        "response_length": "",
        "response_tone": "default",
        "topic_type": "",
        "image_and_analytic": True,
        "tools": [],
        "is_image_vip": False,
    }

def payload_has_image(payload: dict) -> bool:
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in payload.get("messages", [])
    )

def parse_upstream(line: str) -> str | None:
    """Extract only actual assistant text from one upstream SSE line."""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # Never treat arbitrary SSE text/events as assistant content.
        return None

    data = obj.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, str):
            return content

    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                return content
        content = choice.get("text")
        if isinstance(content, str):
            return content
    return None

def sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

@app.get("/")
async def root():
    return {"status": "ok", "api": "OpenAI-compatible", "endpoint": "/v1/chat/completions"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    client_auth(authorization)
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": int(time.time()), "owned_by": "one-ai-proxy"}
        for m in ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o-mini", "deepseek"]
    ]}

@app.post("/debug/upstream")
async def debug_upstream(request: ChatRequest, authorization: str | None = Header(default=None)):
    client_auth(authorization)
    if not request.messages:
        raise HTTPException(400, "messages cannot be empty")
    payload = make_payload(request)
    has_image = payload_has_image(payload)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {make_jwt()}",
        "user-header": make_user_header(),
    }
    log.info("DEBUG upstream request: image=%s payload_bytes=%d", has_image, len(json.dumps(payload, ensure_ascii=False).encode()))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(UPSTREAM_BASE + UPSTREAM_ENDPOINT, json=payload, headers=headers)
            body = response.text[:20000]
            log.info("DEBUG upstream response: image=%s status=%s content_type=%s body=%s", has_image, response.status_code, response.headers.get("content-type"), body[:4000])
            return {
                "upstream_status": response.status_code,
                "upstream_content_type": response.headers.get("content-type"),
                "upstream_body": body,
                "request_has_image": has_image,
                "request_payload_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
                "request_payload": payload,
                "request_headers": {**headers, "Authorization": "Bearer [redacted]"},
            }
    except Exception as exc:
        log.exception("DEBUG upstream exception: image=%s", has_image)
        raise HTTPException(502, f"Upstream request failed: {type(exc).__name__}: {exc}")

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, authorization: str | None = Header(default=None)):
    client_auth(authorization)
    if not request.messages:
        raise HTTPException(400, "messages cannot be empty")

    payload = make_payload(request)
    has_image = payload_has_image(payload)
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=30.0)

    log.info(
        "chat request: image=%s requested_stream=%s payload_bytes=%d",
        has_image,
        request.stream,
        len(json.dumps(payload, ensure_ascii=False).encode()),
    )

    async def upstream_stream() -> AsyncIterator[str]:
        """Forward upstream One AI SSE as OpenAI-compatible SSE."""
        for attempt in range(1, 4):
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "Authorization": f"Bearer {make_jwt()}",
                "user-header": make_user_header(),
            }

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST",
                        UPSTREAM_BASE + UPSTREAM_ENDPOINT,
                        json=payload,
                        headers=headers,
                    ) as response:
                        log.info(
                            "upstream opened: attempt=%d image=%s status=%s content_type=%s",
                            attempt,
                            has_image,
                            response.status_code,
                            response.headers.get("content-type"),
                        )

                        if response.status_code >= 400:
                            body = await response.aread()
                            body_text = body.decode(errors="replace")[:4000]
                            log.error(
                                "upstream HTTP error: attempt=%d status=%s body=%s",
                                attempt,
                                response.status_code,
                                body_text,
                            )
                            if attempt < 3:
                                await __import__("asyncio").sleep(1.0)
                                continue
                            raise HTTPException(
                                502,
                                f"Upstream HTTP {response.status_code}: {body_text}",
                            )

                        chunk_count = 0
                        stream_started = False
                        stream_started_at = time.monotonic()

                        async for line in response.aiter_lines():
                            stripped = line.strip()
                            if not stripped:
                                continue

                            if stripped in ("data: [DONE]", "[DONE]"):
                                yield "data: [DONE]\n\n"
                                log.info("upstream completed: image=%s", has_image)
                                return

                            text = parse_upstream(line)
                            if text:
                                chunk_count += 1
                                if not stream_started:
                                    stream_started = True
                                    log.info(
                                        "first upstream content chunk: image=%s elapsed=%.3fs",
                                        has_image,
                                        time.monotonic() - stream_started_at,
                                    )
                                yield (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "id": completion_id,
                                            "object": "chat.completion.chunk",
                                            "created": created,
                                            "model": request.model,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {
                                                        "role": "assistant",
                                                        "content": text,
                                                    },
                                                    "finish_reason": None,
                                                }
                                            ],
                                        },
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    + "\n\n"
                                )

                        log.info(
                            "upstream stream closed: image=%s chunks=%d elapsed=%.3fs",
                            has_image,
                            chunk_count,
                            time.monotonic() - stream_started_at,
                        )

                        # Some upstream responses may close without an explicit
                        # [DONE]. Signal completion to OpenAI-compatible clients.
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": request.model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {},
                                            "finish_reason": "stop",
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n\n"
                        )
                        yield "data: [DONE]\n\n"
                        return

            except HTTPException:
                raise
            except Exception as exc:
                log.warning(
                    "upstream attempt %d/%d failed: %s",
                    attempt,
                    3,
                    exc,
                )
                # Never replay a partial answer: that would duplicate text
                # already delivered to the Telegram bot.
                if "stream_started" in locals() and stream_started:
                    log.error("stream failed after content started; not retrying")
                    return
                if attempt < 3:
                    await __import__("asyncio").sleep(1.0)
                    continue
                raise HTTPException(
                    502,
                    f"Upstream request failed: {type(exc).__name__}: {exc}",
                )

    if request.stream:
        return StreamingResponse(
            upstream_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming OpenAI-compatible response.
    parts: list[str] = []

    async for event in upstream_stream():
        if event.startswith("data: ") and event.strip() != "data: [DONE]":
            try:
                obj = json.loads(event[6:])
                choices = obj.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    text = delta.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            except json.JSONDecodeError:
                continue

    content = "".join(parts)
    if not content:
        raise HTTPException(502, "Upstream returned no assistant content")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
