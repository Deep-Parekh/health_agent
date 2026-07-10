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


def create_app(config: HealthAgentConfig) -> gr.Blocks:
    logger.info("Initializing HealthVA (backend=%s, model=%s)", config.backend, config.model_id)
    agent = HealthAgent(config)
    tool_logger = ToolLoggingHandler(TOOL_LOG_PATH)

    with gr.Blocks(title="HealthVA — Diet & Workout Assistant", theme=gr.themes.Soft()) as demo:
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

        def respond(user: str, message: str, history: list):
            if not (user or "").strip():
                return history + [[message, "Please enter a username first so I can load or create your profile."]], "**Route**: —", ""
            if not (message or "").strip():
                return history, "**Route**: —", ""
            if is_prompt_leak_request(message):
                return history + [[message, "I can't share my internal instructions, but I'm happy to help with diet or workout planning."]], "**Route**: blocked (prompt-leak filter)", ""
            if is_medical_request(message):
                return history + [[message, "I'm not able to provide medical advice, diagnosis, or medication/PED guidance. Please consult a licensed professional."]], "**Route**: blocked (medical filter)", ""
            if is_confidential(message):
                return history + [[message, "For your privacy, please remove sensitive identifiers like emails, SSNs, or phone numbers."]], "**Route**: blocked (privacy filter)", ""

            tool_logger.clear_thought_process()
            try:
                reply, route = agent.chat(user, message, history, tool_logger)
                badge = f"**Route**: {ROUTE_BADGES.get(route, route)}"
            except Exception as exc:
                logger.exception("Agent error")
                reply, badge = f"Error: {exc}", "**Route**: error"
            return history + [[message, reply]], badge, tool_logger.get_thought_process()

        msg.submit(respond, [username, msg, chatbot], [chatbot, route_display, thought_display])
        msg.submit(lambda: "", None, msg)
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
    demo.launch(server_name="0.0.0.0", server_port=7860)
