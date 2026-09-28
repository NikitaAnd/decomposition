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
    line = line.strip()
    if not line or line == "data: [DONE]":
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if line == "[DONE]":
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    data = obj.get("data")
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"]
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            return delta["content"]
        if isinstance(choice.get("text"), str):
            return choice["text"]
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

    # One AI exposes an SSE endpoint. We consume it internally and expose only
    # a normal OpenAI-compatible JSON response to clients.
    payload = make_payload(request)
    has_image = payload_has_image(payload)
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    parts: list[str] = []
    upstream_completed = False
    last_error: Exception | None = None

    log.info(
        "chat request: image=%s requested_stream=%s payload_bytes=%d",
        has_image,
        request.stream,
        len(json.dumps(payload, ensure_ascii=False).encode()),
    )

    timeout = httpx.Timeout(connect=20.0, read=75.0, write=30.0, pool=30.0)

    # The upstream occasionally kills an otherwise valid SSE connection at
    # about 60 seconds before sending any data. Retry the complete request
    # with a fresh JWT instead of immediately returning 502.
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
                            continue
                        raise HTTPException(
                            502,
                            f"Upstream HTTP {response.status_code}: {body_text}",
                        )

                    async for line in response.aiter_lines():
                        stripped = line.strip()
                        if stripped in ("data: [DONE]", "[DONE]"):
                            upstream_completed = True
                            continue

                        text = parse_upstream(line)
                        if text:
                            parts.append(text)

                    upstream_completed = True
                    break

        except HTTPException:
            raise
        except Exception as exc:
            last_error = exc
            content_so_far = "".join(parts)

            # If content was received, preserve it. If nothing was received,
            # retry because this is the exact failure mode seen from the
            # upstream: incomplete chunked read after ~60 seconds.
            if content_so_far:
                log.warning(
                    "upstream ended early after %d chars on attempt %d; returning partial response: %s",
                    len(content_so_far),
                    attempt,
                    exc,
                )
                break

            log.warning(
                "upstream attempt %d/%d failed with no content: %s",
                attempt,
                3,
                exc,
            )
            if attempt < 3:
                await __import__("asyncio").sleep(1.0)
                continue

    content = "".join(parts)
    if not content:
        error_text = f"{type(last_error).__name__}: {last_error}" if last_error else "empty upstream response"
        log.error("upstream returned no assistant content after retries: %s", error_text)
        raise HTTPException(502, f"Upstream request failed: {error_text}")

    finish_reason = "stop" if upstream_completed else "length"
    log.info(
        "chat completed: image=%s chars=%d upstream_completed=%s",
        has_image,
        len(content),
        upstream_completed,
    )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
