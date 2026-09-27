import os, json, time, uuid, hmac, hashlib, base64
from datetime import datetime
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
                result.append({
                    "type": "image",
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
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {make_jwt()}",
        "user-header": make_user_header(),
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(UPSTREAM_BASE + UPSTREAM_ENDPOINT, json=payload, headers=headers)
            return {
                "upstream_status": response.status_code,
                "upstream_headers": dict(response.headers),
                "upstream_body": response.text[:20000],
                "request_payload": payload,
                "request_headers": {**headers, "Authorization": "Bearer [redacted]"},
            }
    except Exception as exc:
        raise HTTPException(502, f"Upstream request failed: {exc}")

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, authorization: str | None = Header(default=None)):
    client_auth(authorization)
    if not request.messages:
        raise HTTPException(400, "messages cannot be empty")
    payload = make_payload(request)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {make_jwt()}",
        "user-header": make_user_header(),
    }
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    if request.stream:
        return StreamingResponse(
            stream_upstream(request, payload, headers, completion_id, created),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    parts = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", UPSTREAM_BASE + UPSTREAM_ENDPOINT, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise HTTPException(502, f"Upstream HTTP {response.status_code}: {body.decode(errors='replace')[:2000]}")
                async for line in response.aiter_lines():
                    text = parse_upstream(line)
                    if text:
                        parts.append(text)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Upstream request failed: {exc}")

    return {
        "id": completion_id, "object": "chat.completion", "created": created, "model": request.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
    }

async def stream_upstream(request, payload, headers, completion_id, created) -> AsyncIterator[bytes]:
    yield sse({
        "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": request.model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", UPSTREAM_BASE + UPSTREAM_ENDPOINT, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    yield sse({"error": {"message": f"Upstream HTTP {response.status_code}: {body.decode(errors='replace')[:2000]}", "type": "upstream_error"}})
                    yield b"data: [DONE]\n\n"
                    return
                async for line in response.aiter_lines():
                    text = parse_upstream(line)
                    if text:
                        yield sse({
                            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                        })
    except Exception as exc:
        yield sse({"error": {"message": str(exc), "type": "proxy_error"}})
    yield sse({
        "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": request.model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield b"data: [DONE]\n\n"

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
