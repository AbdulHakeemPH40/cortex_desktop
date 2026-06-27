# utils/thinking.py
# Extended Thinking/Reasoning Configuration for Multi-LLM Cortex IDE
# Supports: OpenAI (o1/o3), Anthropic (Cortex), Google (Gemini), DeepSeek, Qwen (budget-capped)

"""
Extended thinking/reasoning configuration for multi-LLM models.

Different LLM providers call this feature by different names:
- OpenAI: "Extended Thinking" (o1, o3 models)
- Anthropic: "Extended Thinking" (Cortex 4+)
- Google: "Thinking Budget" (Gemini 2.0 Flash Thinking)
- Alibaba: "Thinking" (Qwen3 / QwQ models — budget-capped via DashScope API)
- DeepSeek: "Reasoning" (DeepSeek-R1)
- Qwen: "Thinking" (QwQ reasoning model)

This module provides unified configuration across all providers.
"""

from dataclasses import dataclass
from typing import Dict, Literal, Optional


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class ThinkingConfig:
    """
    Unified thinking/reasoning configuration for all LLM providers.
    
    Attributes:
        enabled: Whether thinking is enabled
        budget_tokens: Maximum tokens for thinking process (provider-specific limits apply)
        type: Thinking mode
            - 'adaptive': AI decides when to think (recommended)
            - 'enabled': Always think (uses more tokens)
            - 'disabled': Never think (fastest, cheapest)
    """
    enabled: bool = True
    budget_tokens: int = 4000
    type: Literal["adaptive", "enabled", "disabled"] = "adaptive"
    
    def to_api_params(self, provider: str) -> dict:
        """
        Convert to provider-specific API request parameters.
        
        Args:
            provider: LLM provider name (openai, anthropic, google, deepseek, qwen)
        
        Returns:
            Dict with provider-specific thinking parameters
        """
        if self.type == "disabled":
            return {}
        
        # OpenAI o1/o3 models
        if provider == "openai":
            return {
                "reasoning_effort": "high" if self.type == "enabled" else "medium"
            }
        
        # Anthropic Cortex 4+
        elif provider == "anthropic":
            return {
                "thinking": {
                    "type": self.type,
                    "budget_tokens": self.budget_tokens
                }
            }
        
        # Google Gemini 2.0 Flash Thinking
        elif provider == "google":
            return {
                "thinking_config": {
                    "include_thoughts": True,
                    "thinking_budget": self.budget_tokens
                }
            }
        
        # DeepSeek-R1
        elif provider == "deepseek":
            # DeepSeek-R1 always reasons, but we can control verbosity
            return {}
        
        # Alibaba QwQ
        elif provider == "qwen":
            # QwQ reasoning model
            return {}
        
        return {}


# ============================================================================
# Model Support Detection
# ============================================================================

# Models that support extended thinking/reasoning
THINKING_SUPPORTED_MODELS = {
    # OpenAI - o-series models with extended thinking
    "openai": {
        "o1", "o1-mini", "o1-preview",
        "o3", "o3-mini",
        "o4-mini",
    },
    
    # Anthropic - Cortex 4+ models
    "anthropic": {
        "cortex-sonnet-4", "cortex-sonnet-4-20250514",
        "cortex-opus-4", "cortex-opus-4-20250514",
        "cortex-haiku-4-5", "cortex-haiku-4-5-20250514",
        # Cortex 4.6+ with adaptive thinking
        "cortex-sonnet-4-6", "cortex-opus-4-6",
    },
    
    # Google - Gemini 2.0 Flash Thinking
    "google": {
        "gemini-2.0-flash-thinking",
        "gemini-2.0-flash-thinking-exp",
    },
    
    # DeepSeek - R1 reasoning model
    "deepseek": {
        "deepseek-reasoner",
        "deepseek-r1",
    },
    
    # Alibaba - QwQ reasoning model
    "qwen": {
        "qwq-32b",
        "qwq-plus",
        "qwen-reasoning",
    },
}

# Models that support ADAPTIVE thinking (AI decides when to think)
ADAPTIVE_THINKING_MODELS = {
    # OpenAI - o3+ models
    "openai": {"o3", "o3-mini", "o4-mini"},
    
    # Anthropic - Cortex 4.6+
    "anthropic": {
        "cortex-sonnet-4-6",
        "cortex-opus-4-6",
    },
    
    # Google - Gemini 2.0 Flash Thinking (adaptive by default)
    "google": {
        "gemini-2.0-flash-thinking",
        "gemini-2.0-flash-thinking-exp",
    },
    
    # DeepSeek - R1 (always reasons, adaptive)
    "deepseek": {
        "deepseek-reasoner",
        "deepseek-r1",
    },
    
    # Alibaba - QwQ (always reasons, adaptive)
    "qwen": {
        "qwq-32b",
        "qwq-plus",
        "qwen-reasoning",
    },
}


def model_supports_thinking(provider: str, model_name: str) -> bool:
    """
    Check if a specific model supports extended thinking/reasoning.
    
    Args:
        provider: LLM provider (openai, anthropic, google, deepseek, qwen)
        model_name: Model identifier (e.g., "gpt-4", "cortex-sonnet-4")
    
    Returns:
        True if model supports extended thinking
    
    Examples:
        >>> model_supports_thinking("openai", "o3-mini")
        True
        >>> model_supports_thinking("anthropic", "cortex-sonnet-4")
        True
        >>> model_supports_thinking("deepseek", "deepseek-chat")
        False
    """
    provider = provider.lower()
    model_name = model_name.lower()
    
    # Check if provider is in our database
    if provider not in THINKING_SUPPORTED_MODELS:
        return False
    
    supported_models = THINKING_SUPPORTED_MODELS[provider]
    
    # Check for exact match or substring match
    for supported_model in supported_models:
        if model_name == supported_model or supported_model in model_name:
            return True
    
    return False


def model_supports_adaptive_thinking(provider: str, model_name: str) -> bool:
    """
    Check if a model supports ADAPTIVE thinking (AI decides when to think).
    
    Adaptive thinking is more advanced - the model automatically decides
    when reasoning is needed, saving tokens on simple questions.
    
    Args:
        provider: LLM provider
        model_name: Model identifier
    
    Returns:
        True if model supports adaptive thinking
    
    Examples:
        >>> model_supports_adaptive_thinking("anthropic", "cortex-opus-4-6")
        True
        >>> model_supports_adaptive_thinking("anthropic", "cortex-sonnet-4")
        False  # Cortex 4.0 requires explicit thinking enable/disable
    """
    provider = provider.lower()
    model_name = model_name.lower()
    
    if provider not in ADAPTIVE_THINKING_MODELS:
        return False
    
    adaptive_models = ADAPTIVE_THINKING_MODELS[provider]
    
    # Check for exact match or substring match
    for adaptive_model in adaptive_models:
        if model_name == adaptive_model or adaptive_model in model_name:
            return True
    
    return False


def get_recommended_thinking_config(provider: str, model_name: str) -> Optional[ThinkingConfig]:
    """
    Get recommended thinking configuration for a model.
    
    Args:
        provider: LLM provider
        model_name: Model identifier
    
    Returns:
        Recommended ThinkingConfig or None if model doesn't support thinking
    
    Examples:
        >>> config = get_recommended_thinking_config("openai", "o3-mini")
        >>> config.type
        'adaptive'
        >>> config.budget_tokens
        4000
    """
    if not model_supports_thinking(provider, model_name):
        return None
    
    # Check if model supports adaptive thinking
    if model_supports_adaptive_thinking(provider, model_name):
        return ThinkingConfig(
            enabled=True,
            budget_tokens=4000,
            type="adaptive"
        )
    
    # Model supports thinking but not adaptive - default to enabled
    return ThinkingConfig(
        enabled=True,
        budget_tokens=4000,
        type="enabled"
    )


# ============================================================================
# Provider Information
# ============================================================================

def get_provider_thinking_info(provider: str) -> dict:
    """
    Get thinking capability information for a provider.
    
    Args:
        provider: LLM provider name
    
    Returns:
        Dict with thinking support details
    
    Examples:
        >>> info = get_provider_thinking_info("openai")
        >>> info["feature_name"]
        'Extended Thinking'
        >>> info["supports_thinking"]
        True
    """
    provider_info = {
        "openai": {
            "feature_name": "Extended Thinking",
            "supports_thinking": True,
            "supports_adaptive": True,
            "models": ["o1", "o1-mini", "o3", "o3-mini", "o4-mini"],
            "config_param": "reasoning_effort",
            "description": "OpenAI o-series models with extended reasoning",
        },
        "anthropic": {
            "feature_name": "Extended Thinking",
            "supports_thinking": True,
            "supports_adaptive": True,
            "models": ["cortex-sonnet-4", "cortex-opus-4", "cortex-haiku-4-5"],
            "config_param": "thinking.budget_tokens",
            "description": "Cortex 4+ models with chain-of-thought reasoning",
        },
        "google": {
            "feature_name": "Thinking Budget",
            "supports_thinking": True,
            "supports_adaptive": True,
            "models": ["gemini-2.0-flash-thinking"],
            "config_param": "thinking_config.thinking_budget",
            "description": "Gemini 2.0 Flash Thinking Edition",
        },
        "qwen": {
            "feature_name": "Thinking",
            "supports_thinking": True,
            "supports_adaptive": True,
            "models": ["qwq-32b", "qwq-plus"],
            "config_param": None,
            "description": "Alibaba QwQ reasoning model (always reasons)",
        },
    }
    
    return provider_info.get(provider.lower(), {
        "feature_name": "Unknown",
        "supports_thinking": False,
        "supports_adaptive": False,
        "models": [],
        "config_param": None,
        "description": f"Unknown provider: {provider}",
    })


def list_thinking_supported_models() -> Dict[str, list]:
    """
    List all models that support extended thinking across all providers.
    
    Returns:
        Dict mapping providers to lists of supported models
    
    Examples:
        >>> models = list_thinking_supported_models()
        >>> "o3-mini" in models["openai"]
        True
        >>> "cortex-sonnet-4" in models["anthropic"]
        True
    """
    return {
        provider: sorted(list(models))
        for provider, models in THINKING_SUPPORTED_MODELS.items()
        if models  # Only include providers with supported models
    }


# ============================================================================
# Convenience Functions
# ============================================================================

def should_enable_thinking_by_default(provider: str, model_name: str) -> bool:
    """
    Check if thinking should be enabled by default for a model.
    
    Args:
        provider: LLM provider
        model_name: Model identifier
    
    Returns:
        True if thinking should be enabled by default
    """
    # Only enable by default if model supports thinking
    return model_supports_thinking(provider, model_name)


def get_thinking_summary(provider: str, model_name: str) -> str:
    """
    Get human-readable summary of thinking support for a model.
    
    Args:
        provider: LLM provider
        model_name: Model identifier
    
    Returns:
        Human-readable description
    
    Examples:
        >>> print(get_thinking_summary("openai", "o3-mini"))
        "OpenAI o3-mini: Supports Extended Thinking (adaptive mode)"
    """
    provider_info = get_provider_thinking_info(provider)
    
    if not provider_info["supports_thinking"]:
        return f"{provider} {model_name}: Does not support extended thinking"
    
    supports_adaptive = model_supports_adaptive_thinking(provider, model_name)
    mode = "adaptive" if supports_adaptive else "manual"
    
    return (
        f"{provider_info['feature_name']}: {model_name} "
        f"(supports thinking in {mode} mode)"
    )


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Data classes
    "ThinkingConfig",
    
    # Core functions
    "model_supports_thinking",
    "model_supports_adaptive_thinking",
    "get_recommended_thinking_config",
    "should_enable_thinking_by_default",
    
    # Provider info
    "get_provider_thinking_info",
    "list_thinking_supported_models",
    "get_thinking_summary",
    
    # Constants
    "THINKING_SUPPORTED_MODELS",
    "ADAPTIVE_THINKING_MODELS",
]
