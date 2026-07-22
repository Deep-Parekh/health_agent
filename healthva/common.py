"""Shared infrastructure for HealthVA: paths, config, LLM backends, logging, input guardrails.

Ported from langchain_diet_agent/app.py with two changes:
- backends: OpenAI (deployed), Groq, and Ollama (offline dev), all importing lazily
- data paths cover the workout DB and the local user-store fallback
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseLanguageModel
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("health_agent")

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]  # repo root
REPO_DATA_DIR = BASE_DIR / "data"

# HF Spaces has a read-only repo filesystem; write under /tmp there.
if os.getenv("SPACE_ID"):
    DATA_DIR = Path("/tmp/data")
    LOG_DIR = Path("/tmp/logs")
else:
    DATA_DIR = REPO_DATA_DIR
    LOG_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

if os.getenv("SPACE_ID") and REPO_DATA_DIR.exists():
    import shutil

    for f in ["fdc_subset.json", "recipes.db", "workouts.db"]:
        src = REPO_DATA_DIR / f
        dst = DATA_DIR / f
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

FDC_PATH = DATA_DIR / "fdc_subset.json"
RECIPES_DB_PATH = DATA_DIR / "recipes.db"
WORKOUTS_DB_PATH = DATA_DIR / "workouts.db"
USERS_DB_PATH = DATA_DIR / "users.db"  # SQLite fallback when DATABASE_URL is unset
TOOL_LOG_PATH = LOG_DIR / "tool_calls.jsonl"


@dataclass
class HealthAgentConfig:
    """Configuration for HealthVA with pluggable LLM backends."""

    backend: Literal["openai", "groq", "ollama"] = "openai"
    model_id: str = "gpt-4o-mini"
    max_new_tokens: int = 1024
    temperature: float = 0.4
    max_history_turns: int = 6  # user+assistant pairs kept per request
    use_llm_router: bool = True  # LLM classifies ambiguous turns; keywords handle clear ones


def build_llm(config: HealthAgentConfig) -> BaseLanguageModel:
    """Build an LLM from the config backend. Heavy deps import lazily."""
    if config.backend == "groq":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.environ.get("GROQ_API_KEY"),
            model=config.model_id,
            max_tokens=config.max_new_tokens,
            temperature=config.temperature,
        )

    if config.backend == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=config.model_id,
            max_tokens=config.max_new_tokens,
            temperature=config.temperature,
        )

    if config.backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=config.model_id, temperature=config.temperature)

    raise ValueError(f"Unsupported backend: {config.backend}")


# --- Input guardrails (checked before any LLM call) ---

MEDICAL_KEYWORDS = [
    "diagnose", "diagnosis", "prescribe", "prescription", "medication",
    "drug", "pill", "dose", "dosing",
    "disease", "cancer", "diabetes", "hypertension",
    "symptom", "symptoms", "chest pain",
    "emergency", "heart attack", "stroke",
    "steroid", "steroids", "sarms", "trt",
]

CONFIDENTIAL_PATTERNS = [
    r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN-like pattern
    r"\b\d{10}\b",             # 10-digit phone
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # email
]

PROMPT_LEAK_PHRASES = [
    "system prompt", "your prompt", "exact prompt",
    "instructions you were given", "hidden prompt",
    "what are your instructions", "show me your prompt",
]


def is_medical_request(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in MEDICAL_KEYWORDS)


def is_confidential(text: str) -> bool:
    return any(re.search(pat, text) for pat in CONFIDENTIAL_PATTERNS)


def is_prompt_leak_request(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in PROMPT_LEAK_PHRASES)


class ToolLoggingHandler(BaseCallbackHandler):
    """Logs all tool calls and agent steps to a JSONL file and to memory for the UI."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.thought_process = []
        self.events: List[Dict[str, Any]] = []  # structured trace for the web API

    def _write(self, record: Dict[str, Any]) -> None:
        record["timestamp"] = time.time()
        self.events.append(record)
        self.log_path.parent.mkdir(exist_ok=True, parents=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        logger.info(
            "Agent Event: %s - %s",
            record.get("event"),
            record.get("tool") or record.get("route") or "",
        )

    def log_route(self, route: str, method: str) -> None:
        """Record the router's domain decision so every turn is auditable."""
        self.thought_process.append(f"🧭 **Route**: `{route}` (via {method})")
        self._write({"event": "route", "route": route, "method": method})

    def on_chain_start(self, serialized, inputs, **kwargs):
        name = (serialized or {}).get("name") or "agent"
        self._write({"event": "chain_start", "name": name})

    def on_tool_start(self, serialized, input_str, run_id, parent_run_id=None, **kwargs):
        tool_name = (serialized or {}).get("name") or "unknown_tool"
        self.thought_process.append(f"🛠️ **Tool Call**: `{tool_name}`\nInput: `{input_str}`")
        self._write({
            "event": "tool_start",
            "tool": tool_name,
            "input": input_str,
            "run_id": str(run_id),
            "parent_run_id": str(parent_run_id),
        })

    def on_tool_end(self, output, run_id, parent_run_id=None, **kwargs):
        self.thought_process.append(f"✅ **Tool Output**: {str(output)[:500]}")
        self._write({
            "event": "tool_end",
            "run_id": str(run_id),
            "parent_run_id": str(parent_run_id),
            "output": str(output)[:1000],
        })

    def on_tool_error(self, error, run_id, parent_run_id=None, **kwargs):
        self.thought_process.append(f"❌ **Tool Error**: {error}")
        self._write({
            "event": "tool_error",
            "run_id": str(run_id),
            "parent_run_id": str(parent_run_id),
            "error": str(error),
        })

    def get_thought_process(self) -> str:
        return "\n\n".join(self.thought_process)

    def clear_thought_process(self):
        self.thought_process = []
