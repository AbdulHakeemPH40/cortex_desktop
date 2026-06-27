"""
AI Provider Registry and Base Classes for Cortex AI Agent IDE
Provides unified interface for multiple LLM providers
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Generator
from dataclasses import dataclass
from enum import Enum
from src.utils.logger import get_logger

log = get_logger("provider_registry")


class ProviderType(Enum):
    """Supported LLM providers."""
    MISTRAL = "mistral"     # Mistral — OCR/vision only (fallback)
    SILICONFLOW = "siliconflow"  # Vision models
    DEEPSEEK = "deepseek"   # DeepSeek V4 — primary coding/reasoning
    KIMI = "kimi"           # Kimi/Moonshot AI (K2.6 multimodal)
    MIMO = "mimo"           # Xiaomi MiMo (V2.5 family — 1M ctx agentic)
    OPENAI = "openai"       # OpenAI — GPT-4o/4.1 series
    OPENROUTER = "openrouter"  # OpenRouter — 300+ models via single API key
    ALIBABA = "alibaba"     # Alibaba Cloud Model Studio (DashScope) — Qwen family


@dataclass
class ModelInfo:
    """Information about an LLM model."""
    id: str
    name: str
    provider: str
    context_length: int
    max_tokens: int
    supports_streaming: bool = True
    supports_vision: bool = False


@dataclass
class ChatMessage:
    """Represents a chat message."""
    role: str  # 'system', 'user', 'assistant', 'tool'
    content: str
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None


@dataclass
class ChatResponse:
    """Response from an LLM provider."""
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: Optional[str] = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class BaseProvider(ABC):
    """Abstract base class for all LLM providers."""
    
    def __init__(self, provider_type: ProviderType):
        self.provider_type = provider_type
        self._api_key: Optional[str] = None
        self._base_url: Optional[str] = None
        self._last_error: Optional[str] = None
        
        # Ensure TLS CA bundle is configured (critical for frozen builds)
        self._ensure_ca_bundle()
        
    @staticmethod
    def _ensure_ca_bundle():
        """Ensure REQUESTS_CA_BUNDLE is set for SSL verification in frozen builds."""
        import os
        if os.environ.get('REQUESTS_CA_BUNDLE'):
            return  # Already configured
        try:
            import certifi
            ca_path = certifi.where()
            if os.path.isfile(ca_path):
                os.environ['REQUESTS_CA_BUNDLE'] = ca_path
        except ImportError:
            pass
        except Exception:
            pass
        
    @property
    @abstractmethod
    def available_models(self) -> List[ModelInfo]:
        """Return list of available models for this provider."""
        pass
    
    @abstractmethod
    def chat(self, 
             messages: List[ChatMessage], 
             model: str,
             temperature: float = 0.7,
             max_tokens: int = 2000,
             stream: bool = False,
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: Optional[str] = None) -> ChatResponse:
        """
        Send a chat completion request.
        """
        pass
    
    def chat_stream(self,
                   messages: List[ChatMessage],
                   model: str,
                   temperature: float = 0.7,
                   max_tokens: int = 2000,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   **kwargs: Any) -> Generator[str, None, None]:
        """
        Stream chat completion response.
        """
        response = self.chat(messages, model, temperature, max_tokens, stream=True, tools=tools)
        yield response.content
    
    @abstractmethod
    def validate_api_key(self) -> bool:
        """Validate the current API key."""
        pass
    
    def set_api_key(self, api_key: str):
        """Set the API key for this provider."""
        self._api_key = api_key
        
    def get_last_error(self) -> Optional[str]:
        """Get the last error message."""
        return self._last_error
    
    def _format_messages_for_provider(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        """Convert internal messages to provider-specific format.
        
        Handles both ChatMessage dataclass objects AND plain dicts
        (e.g. from sanitizer which returns dicts to avoid mutation issues).
        """
        formatted: List[Dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                # Already a dict — pass through (sanitizer may return dicts)
                m: Dict[str, Any] = dict(msg)
            else:
                # ChatMessage dataclass object
                m: Dict[str, Any] = {"role": msg.role, "content": msg.content}
                if msg.name:
                    m["name"] = msg.name
                if msg.tool_calls:
                    m["tool_calls"] = msg.tool_calls
                    # When assistant has tool_calls, content can be null
                    if not msg.content:
                        m["content"] = None
                if msg.tool_call_id:
                    m["tool_call_id"] = msg.tool_call_id
                if hasattr(msg, "reasoning_content") and getattr(msg, "reasoning_content"):
                    m["reasoning_content"] = getattr(msg, "reasoning_content")
            formatted.append(m)
        return formatted


class ProviderRegistry:
    """Registry for managing multiple AI providers."""
    
    def __init__(self):
        self._providers: Dict[ProviderType, BaseProvider] = {}
        self._current_provider: ProviderType = ProviderType.MISTRAL
        
        # Register Mistral provider (primary provider for ALL work)
        try:
            from src.ai.providers.mistral_provider import MistralProvider
            self._register_provider(ProviderType.MISTRAL, MistralProvider())
            log.info("MistralProvider registered")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register MistralProvider: {e}")
        

        
        # Lazily register other providers if their modules are available
        try:
            from src.ai.providers.siliconflow_provider import SiliconFlowProvider
            self._register_provider(ProviderType.SILICONFLOW, SiliconFlowProvider())
            log.info("SiliconFlowProvider registered")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register SiliconFlowProvider: {e}")
        
        # Register DeepSeek provider (V4 models with 1M context)
        try:
            from src.ai.providers.deepseek_provider import DeepSeekProvider
            self._register_provider(ProviderType.DEEPSEEK, DeepSeekProvider())
            log.info("DeepSeekProvider registered with V4 models")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register DeepSeekProvider: {e}")
        
        # Register Kimi/Moonshot AI provider (K2.6 multimodal model)
        try:
            from src.ai.providers.kimi_provider import KimiProvider
            self._register_provider(ProviderType.KIMI, KimiProvider())
            log.info("KimiProvider registered with K2.6 model")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register KimiProvider: {e}")

        # Register Xiaomi MiMo provider (V2.5 family — 1M ctx agentic)
        try:
            from src.ai.providers.mimo_provider import MimoProvider
            self._register_provider(ProviderType.MIMO, MimoProvider())
            log.info("MimoProvider registered with V2.5 models")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register MimoProvider: {e}")

        # Register OpenAI provider (GPT-4o / 4.1 series — budget-friendly tier)
        try:
            from src.ai.providers.openai_provider import OpenAIProvider
            self._register_provider(ProviderType.OPENAI, OpenAIProvider())
            log.info("OpenAIProvider registered")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register OpenAIProvider: {e}")

        # Register OpenRouter provider (300+ models via single API key)
        try:
            from src.ai.providers.openrouter_provider import OpenRouterProvider
            self._register_provider(ProviderType.OPENROUTER, OpenRouterProvider())
            log.info("OpenRouterProvider registered with 20+ models")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register OpenRouterProvider: {e}")

        # Register Alibaba Model Studio provider (DashScope — Qwen family)
        try:
            from src.ai.providers.alibaba_provider import AlibabaProvider
            self._register_provider(ProviderType.ALIBABA, AlibabaProvider())
            log.info("AlibabaProvider registered with Qwen models")
        except (ImportError, Exception) as e:
            log.warning(f"Could not register AlibabaProvider: {e}")

            
    def _register_provider(self, provider_type: ProviderType, provider: BaseProvider):
        self._providers[provider_type] = provider
    

        
    def get_provider(self, provider_type: Optional[ProviderType] = None) -> BaseProvider:
        if provider_type is None:
            provider_type = self._current_provider
        
        provider = self._providers.get(provider_type)
        if not provider:
            log.warning(f"Provider {provider_type} not found, falling back to MISTRAL")
            return self._providers[ProviderType.MISTRAL]
        return provider
        
    def set_provider(self, provider_type: ProviderType):
        if provider_type in self._providers:
            self._current_provider = provider_type
            
    def list_providers(self) -> List[ProviderType]:
        return list(self._providers.keys())
        
    def get_all_models(self) -> List[ModelInfo]:
        models: List[ModelInfo] = []
        for provider in self._providers.values():
            models.extend(provider.available_models)
        return models
        
    def validate_all_keys(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for provider_type, provider in self._providers.items():
            results[provider_type.value] = provider.validate_api_key()
        return results


_registry = None

def get_provider_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
