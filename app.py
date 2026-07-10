"""HealthVA — Gradio entry point (HF Spaces runs this file).

UI: username field keys the persistent profile; the side panel shows the
router's domain decision and the agent's tool calls for each turn.
"""

from __future__ import annotations

import os

import gradio as gr

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

ROUTE_BADGES = {
    "diet": "🥗 diet tools loaded",
    "workout": "🏋️ workout tools loaded",
    "both": "🥗+🏋️ all tools loaded",
    "general": "💬 profile tools only",
}


def _message_text(content: object) -> str:
    """Extract plain text from a Gradio message content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _history_pairs(history: list) -> list[tuple[str, str]]:
    """Convert Gradio's role/content messages to the agent's turn-pair format."""
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = _message_text(item.get("content"))
        if role == "user":
            if pending_user is not None:
                pairs.append((pending_user, ""))
            pending_user = content
        elif role == "assistant" and pending_user is not None:
            pairs.append((pending_user, content))
            pending_user = None
    if pending_user is not None:
        pairs.append((pending_user, ""))
    return pairs


def _append_assistant(history: list, reply: str) -> list[dict]:
    """Append an assistant message in Gradio's messages format."""
    return [*(history or []), {"role": "assistant", "content": reply}]


def create_app(config: HealthAgentConfig) -> gr.Blocks:
    logger.info("Initializing HealthVA (backend=%s, model=%s)", config.backend, config.model_id)
    agent = HealthAgent(config)
    tool_logger = ToolLoggingHandler(TOOL_LOG_PATH)

    with gr.Blocks(title="HealthVA — Diet & Workout Assistant") as demo:
        gr.Markdown("# 🧬 HealthVA — Diet & Workout Assistant")
        gr.Markdown(
            "One assistant for nutrition and fitness. Each question is routed to a "
            "domain specialist that loads only the tools it needs, and your profile "
            "is remembered across sessions. I'm NOT a medical professional."
        )

        with gr.Row():
            with gr.Column(scale=4):
                username = gr.Textbox(
                    label="Username",
                    placeholder="Pick any name — it keys your saved profile (demo login, not real auth)",
                    value="",
                )
                chatbot = gr.Chatbot(height=460)
                msg = gr.Textbox(placeholder="Ask about meals, workouts, or both…", label="Message")
                clear = gr.Button("Clear conversation")
            with gr.Column(scale=2):
                route_display = gr.Markdown("**Route**: —")
                with gr.Accordion("🧠 Agent activity", open=True):
                    thought_display = gr.Markdown("Routing decisions and tool calls will appear here.")

        def queue_message(message: str, history: list):
            """Stage 1 (instant): echo the user's message into the chat and
            clear the textbox, before any LLM work starts."""
            if not (message or "").strip():
                return history, ""
            return [*(history or []), {"role": "user", "content": message}], ""

        def generate_reply(user: str, history: list):
            """Stage 2: guardrails + agent for the last unanswered user message."""
            if not history or history[-1].get("role") != "user":
                return history, gr.update(), gr.update()
            message = _message_text(history[-1].get("content"))

            if not (user or "").strip():
                return _append_assistant(
                    history,
                    "Please enter a username first so I can load or create your profile.",
                ), "**Route**: —", ""
            if is_prompt_leak_request(message):
                return _append_assistant(
                    history,
                    "I can't share my internal instructions, but I'm happy to help "
                    "with diet or workout planning.",
                ), "**Route**: blocked (prompt-leak filter)", ""
            if is_medical_request(message):
                return _append_assistant(
                    history,
                    "I'm not able to provide medical advice, diagnosis, or medication/PED "
                    "guidance. Please consult a licensed professional.",
                ), "**Route**: blocked (medical filter)", ""
            if is_confidential(message):
                return _append_assistant(
                    history,
                    "For your privacy, please remove sensitive identifiers like emails, "
                    "SSNs, or phone numbers.",
                ), "**Route**: blocked (privacy filter)", ""

            tool_logger.clear_thought_process()
            try:
                # history[:-1]: agent.chat appends the current message itself
                reply, route = agent.chat(
                    user, message, _history_pairs(history[:-1]), tool_logger
                )
                badge = f"**Route**: {ROUTE_BADGES.get(route, route)}"
            except Exception as exc:
                logger.exception("Agent error")
                reply, badge = f"Error: {exc}", "**Route**: error"
            return (
                _append_assistant(history, reply),
                badge,
                tool_logger.get_thought_process(),
            )

        msg.submit(
            queue_message, [msg, chatbot], [chatbot, msg]
        ).then(
            generate_reply, [username, chatbot], [chatbot, route_display, thought_display]
        )
        clear.click(
            lambda: ([], "**Route**: —", "Routing decisions and tool calls will appear here."),
            None,
            [chatbot, route_display, thought_display],
        )

    return demo


DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "qwen2.5:3b",
}

if __name__ == "__main__":
    # Backend resolution: explicit env var wins; otherwise pick by available key.
    # Deployment plan is an OpenAI key in Space secrets; Groq/Ollama remain
    # supported so local dev and the old setup keep working unchanged.
    backend = os.getenv("AGENT_BACKEND")
    if not backend or backend not in ("openai", "groq", "ollama"):
        if os.getenv("OPENAI_API_KEY"):
            backend = "openai"
        elif os.getenv("GROQ_API_KEY"):
            backend = "groq"
        else:
            backend = "ollama"
    model_id = os.getenv("AGENT_MODEL_ID") or DEFAULT_MODELS.get(backend, "gpt-4o-mini")
    config = HealthAgentConfig(backend=backend, model_id=model_id)
    demo = create_app(config)
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
    )
