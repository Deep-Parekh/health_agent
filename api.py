"""FastAPI wrapper around HealthAgent — the web app's backend.

Host-agnostic: runs locally under uvicorn (`uvicorn api:app`) and unchanged on
Modal (see modal_app.py). Reuses all agent logic verbatim; this file only
translates HTTP <-> HealthAgent.chat and returns a structured trace for the UI.

Contract (mirrored in the portfolio repo and docs/WEBAPP_PLAN.md):
  GET  /health            -> {status, contract_version, backend, model}
  POST /chat  {username, message, history:[[user,assistant],...]}
              -> {reply, route, tool_trace:[...], profile:{...}}

Auth: every /chat request must send `X-API-Key` matching AGENT_API_SECRET
(unset -> open, for local dev only).
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from healthva.common import (
    HealthAgentConfig,
    TOOL_LOG_PATH,
    ToolLoggingHandler,
    is_confidential,
    is_medical_request,
    is_prompt_leak_request,
    logger,
)
from healthva.agents import HealthAgent

CONTRACT_VERSION = "1.0"

# Backend resolution mirrors app.py: OpenAI in production, Groq/Ollama fallbacks.
_backend = os.getenv("AGENT_BACKEND")
if not _backend or _backend not in ("openai", "groq", "ollama"):
    _backend = "openai" if os.getenv("OPENAI_API_KEY") else "groq" if os.getenv("GROQ_API_KEY") else "ollama"
_model = os.getenv("AGENT_MODEL_ID") or {
    "openai": "gpt-4o-mini", "groq": "llama-3.3-70b-versatile", "ollama": "qwen2.5:3b",
}.get(_backend, "gpt-4o-mini")

config = HealthAgentConfig(backend=_backend, model_id=_model)
agent = HealthAgent(config)  # builds the LLM + UserStore once at startup

app = FastAPI(title="HealthVA API", version=CONTRACT_VERSION)


class ChatRequest(BaseModel):
    username: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    history: List[Tuple[str, str]] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str
    route: str
    tool_trace: list
    profile: dict


def _check_auth(x_api_key: Optional[str]) -> None:
    expected = os.getenv("AGENT_API_SECRET")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _blocked_reply(message: str) -> Optional[Tuple[str, str]]:
    """Input guardrails, mirrored from app.py, run before any LLM work."""
    if is_prompt_leak_request(message):
        return ("I can't share my internal instructions, but I'm happy to help with "
                "diet or workout planning.", "blocked:prompt-leak")
    if is_medical_request(message):
        return ("I'm not able to provide medical advice, diagnosis, or medication/PED "
                "guidance. Please consult a licensed professional.", "blocked:medical")
    if is_confidential(message):
        return ("For your privacy, please remove sensitive identifiers like emails, "
                "SSNs, or phone numbers.", "blocked:privacy")
    return None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "backend": config.backend,
        "model": config.model_id,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, x_api_key: Optional[str] = Header(default=None)) -> ChatResponse:
    _check_auth(x_api_key)

    blocked = _blocked_reply(req.message)
    if blocked:
        reply, route = blocked
        return ChatResponse(reply=reply, route=route, tool_trace=[], profile={})

    tool_logger = ToolLoggingHandler(TOOL_LOG_PATH)
    try:
        reply, route = agent.chat(req.username, req.message, req.history, tool_logger)
    except Exception as exc:
        logger.exception("Agent error")
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}")

    profile = agent.store.scope(req.username).get_profile()
    return ChatResponse(
        reply=reply, route=route, tool_trace=tool_logger.events, profile=profile
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
