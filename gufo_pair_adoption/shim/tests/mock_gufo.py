#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Mock GUFO v0.20.0 for shim testing.

Mirrors the *observed* behaviour of the real handlers in
src/cli/serve/http_server.cpp so the shim is tested against the upstream it
will actually meet, not against an idealisation:

  * /health            -> {"status":"ok"}
  * /props             -> model, model_alias, template, model_info,
                         model_path, build_info, total_slots,
                         default_generation_settings, modalities
                         *** NO `role` KEY ***   <-- gap G1
  * /models            -> OpenAI list; the loaded model only; NO `status`
  * /v1/chat/completions, /completion, /v1/completions -> streaming + sync
  * Bearer auth enforced on everything (IsAuthorized)
"""

from __future__ import annotations

import json
import os
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

API_KEY = os.environ.get("MOCK_GUFO_API_KEY", "test-key-123")
MODEL_ID = os.environ.get("MOCK_GUFO_MODEL", "Qwen3.8-Flash-Next-UD-Q4_K_XL")
PORT = int(os.environ.get("MOCK_GUFO_PORT", "18901"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def unauthorized() -> JSONResponse:
    resp = JSONResponse(
        status_code=401,
        content={
            "error": {
                "type": "authentication_error",
                "code": "invalid_api_key",
                "message": "missing or invalid API key",
            }
        },
    )
    resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


def authed(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    return header == f"Bearer {API_KEY}"


@app.get("/health")
async def health(request: Request):
    if not authed(request):
        return unauthorized()
    return {"status": "ok"}


@app.get("/props")
async def props(request: Request):
    """Faithful to LlamaProps(): note the ABSENCE of `role`."""
    if not authed(request):
        return unauthorized()
    body = {
        "model": MODEL_ID,
        "model_alias": MODEL_ID,
        "template": "",
        "model_info": {},
        "model_path": "/models/nemotron-3.5-lightning-optiq",
        "build_info": {"build_compiler": "gcc-13", "build-type": "Release", "version": "0.20.0"},
        "total_slots": 4,
        "default_generation_settings": {
            "params": {
                "temperature": 0.7,
                "top_k": 40,
                "top_p": 0.95,
                "min_p": 0.0,
                "seed": -1,
                "repeat_penalty": 1.0,
                "repeat_last_n": 64,
            }
        },
        "modalities": {"input": ["text"], "output": ["text"]},
    }
    # GUFO echoes the ?model= query but never re-serves it.
    want = request.query_params.get("model")
    if want:
        body["model"] = want
    return body


@app.get("/models")
@app.get("/v1/models")
async def models(request: Request):
    """Faithful to ListModels(): no `status` field on any row."""
    if not authed(request):
        return unauthorized()
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": 1780000000,
                "owned_by": "gufo",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            }
        ],
    }


@app.get("/slots")
@app.get("/v1/slots")
async def slots(request: Request):
    if not authed(request):
        return unauthorized()
    return [{"id": i, "state": "idle", "model": MODEL_ID} for i in range(4)]


def _chat_chunks(prompt: str, n: int = 4):
    for i in range(n):
        piece = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "delta": ({"role": "assistant"} if i == 0 else {"content": f"tok{i} "}),
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(piece)}\n\n".encode()
        time.sleep(0.02)
    yield b"data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if not authed(request):
        return unauthorized()
    payload = await request.json()
    if payload.get("stream"):
        return StreamingResponse(_chat_chunks(str(payload)), media_type="text/event-stream")
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "mock answer"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


@app.post("/completion")
@app.post("/v1/completions")
async def completion(request: Request):
    if not authed(request):
        return unauthorized()
    payload = await request.json()
    if payload.get("stream"):
        return StreamingResponse(_chat_chunks(str(payload)), media_type="text/event-stream")
    return {
        "id": "cmpl-mock",
        "object": "text_completion",
        "model": MODEL_ID,
        "choices": [{"index": 0, "text": "mock completion", "finish_reason": "stop"}],
    }


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    if not authed(request):
        return unauthorized()
    return JSONResponse(
        status_code=501,
        content={"error": {"type": "not_implemented", "message": "embeddings unsupported"}},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
