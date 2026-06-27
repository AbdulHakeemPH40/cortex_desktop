"""
model_registry.py — Single source of truth for available LLM models.

Used by chat_panel.py InputArea model selector. Each group is:
  (group_label_or_None, [(model_id, display_name, description, accent_color), ...], tier)

Tier is one of:
  "subscription" — Cortex-hosted models (MiMO, DeepSeek) — shown with highlighted border
  "byok"         — Bring Your Own Key (OpenRouter, OpenAI, Alibaba) — shown with smaller header
"""

MODEL_GROUPS = [
    # ── Auto (always first) ──
    (None, [("auto", "Auto", "Smart routing", "#2196f3")], "subscription"),

    # ── Cortex Subscription models ──
    ("DeepSeek V4", [
        ("deepseek-v4-pro", "DeepSeek V4 Pro", "1.6T params · 1M ctx", "#a78bfa"),
    ], "subscription"),
    ("Xiaomi MiMo", [
        ("mimo-v2.5-pro", "MiMo V2.5 Pro", "1M · 42B MoE agentic", "#ff6900"),
        ("mimo-v2.5", "MiMo V2.5", "1M · full-modal", "#ff6900"),
    ], "subscription"),

    # ── BYOK (Bring Your Own Key) models ──
    ("OpenAI GPT", [
        ("gpt-5.5", "GPT-5.5", "1.05M ctx · newest frontier", "#10a37f"),
        ("gpt-5.4", "GPT-5.4", "1.05M ctx · frontier", "#10a37f"),
    ], "byok"),
    ("OpenRouter — Anthropic", [
        ("anthropic/claude-opus-4-8", "Claude Opus 4.8", "1M ctx · 64k out · flagship", "#d77b4a"),
        ("anthropic/claude-opus-4-5", "Claude Opus 4.5", "1M ctx · 64k out", "#d77b4a"),
        ("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5", "1M ctx · 64k out", "#d77b4a"),
        ("anthropic/claude-haiku-4-5", "Claude Haiku 4.5", "1M ctx · 64k out · fast", "#d77b4a"),
    ], "byok"),
    ("OpenRouter — Google", [
        ("google/gemini-2.5-pro", "Gemini 2.5 Pro", "1M ctx · 65k out", "#4285f4"),
        ("google/gemini-2.5-flash", "Gemini 2.5 Flash", "1M ctx · fast", "#4285f4"),
    ], "byok"),
    ("OpenRouter — NVIDIA", [
        ("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra", "1M ctx · MoE", "#76b900"),
    ], "byok"),
    ("OpenRouter — Z.ai (GLM)", [
        ("z-ai/glm-5.2", "GLM 5.2", "1M ctx · 744B MoE · coding-first", "#00bcd4"),
    ], "byok"),
    ("Alibaba — Qwen (Model Studio)", [
        ("qwen3.7-plus", "Qwen 3.7 Plus", "1M ctx · agentic flagship", "#f59e0b"),
        ("qwen3.6-plus", "Qwen 3.6 Plus", "1M ctx · agentic", "#f59e0b"),
        ("qwen3-coder-plus", "Qwen3 Coder Plus", "1M ctx · best for code", "#f59e0b"),
        ("qwen-flash", "Qwen Flash", "1M ctx · fast · free quota", "#f59e0b"),
        ("qwen-turbo", "Qwen Turbo", "1M ctx · low cost", "#f59e0b"),
    ], "byok"),

    # ── Coming Soon ──
    ("Coming Soon", [
        ("mistral-soon", "Mistral AI", "Mistral Large, Codestral", "#6b7280"),
        ("kimi-soon", "Kimi (Moonshot)", "Kimi K2.6", "#6b7280"),
    ], "coming_soon"),
]
