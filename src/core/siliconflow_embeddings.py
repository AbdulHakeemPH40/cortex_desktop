"""
SiliconFlow Embeddings - Cloud-based semantic embeddings using Qwen models

Subscription-only: routes through Django backend which holds the server-side
SiliconFlow API key. No BYOK (user API key) is used for this service.
"""

import os
import hashlib
import threading
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from src.utils.logger import get_logger

log = get_logger("siliconflow_embeddings")


@dataclass
class EmbeddingResult:
    """Result of embedding generation."""
    success: bool
    embedding: Optional[List[float]] = None
    error: Optional[str] = None
    model_name: str = ""
    dimensions: int = 0
    tokens_used: int = 0


class SiliconFlowEmbeddings:
    """
    Generate embeddings using SiliconFlow API (Qwen models) via Django proxy.
    Subscription-only — no direct API calls, no BYOK.
    Falls back to hash-based embedding for non-subscription users.
    """
    
    # Available models (dimensions must match what Django/Mistral expects)
    MODELS = {
        'Qwen/Qwen3-Embedding-0.6B': {'dimensions': 1024, 'quality': 'fast'},
        'Qwen/Qwen3-Embedding-4B': {'dimensions': 2560, 'quality': 'balanced'},
        'Qwen/Qwen3-Embedding-8B': {'dimensions': 4096, 'quality': 'best'},
    }
    
    # Default model - good balance of quality and cost
    DEFAULT_MODEL = 'Qwen/Qwen3-Embedding-4B'
    
    # Fallback dimensions (for hash-based embedding)
    FALLBACK_DIMENSIONS = 384
    
    def __init__(self, model_name: str = None, api_key: str = None):
        """
        Initialize SiliconFlow embeddings.
        
        Args:
            model_name: Model to use (default: Qwen/Qwen3-Embedding-4B)
            api_key: Ignored — subscription-only, key lives on Django server.
        """
        self.model_name = model_name or self.DEFAULT_MODEL
        self._lock = threading.Lock()
        self._initialized = True  # Always initialized (proxy or hash fallback)
        
        model_info = self.MODELS.get(self.model_name, {})
        log.info(f"SiliconFlow embeddings ready (subscription proxy mode): {self.model_name} ({model_info.get('quality', 'unknown')} quality)")
    
    def generate_embedding(self, text: str) -> EmbeddingResult:
        """
        Generate an embedding for a single text.
        
        Routes exclusively through Django backend:
        1. Subscription proxy — Django holds the SiliconFlow API key
        2. Hash fallback — for non-subscription users (lower quality)
        """
        if not text or not text.strip():
            return EmbeddingResult(
                success=False,
                error="Empty text provided"
            )
        
        # Truncate text if too long (most models have limits)
        max_chars = 8000  # ~2000 tokens, safe for most models
        if len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + "\n...[truncated]...\n" + text[-half:]
        
        # Tier 1: Subscription proxy — route through Django server
        try:
            from src.core.cortex_api import get_api_client
            api = get_api_client()
            if api.has_subscription():
                proxy_result = api.proxy_service(
                    "siliconflow_embeddings",
                    text=text,
                    model=self.model_name,
                )
                if proxy_result and proxy_result.get("status") == "success":
                    data = proxy_result.get("data", {})
                    embedding = data.get("data", [{}])[0].get("embedding", [])
                    usage = data.get("usage", {})
                    if embedding:
                        log.info(f"[SiliconFlow] Subscription proxy returned {len(embedding)}-dim embedding")
                        return EmbeddingResult(
                            success=True, embedding=embedding,
                            model_name=self.model_name,
                            dimensions=len(embedding),
                            tokens_used=usage.get("total_tokens", 0),
                        )
                else:
                    log.warning(f"[SiliconFlow] Subscription proxy failed: {proxy_result}")
        except Exception as e:
            log.debug(f"[SiliconFlow] Subscription proxy unavailable: {e}")

        # Tier 2: Hash-based fallback (always available, no API key needed)
        return self._hash_embedding(text, dimensions=self._target_dimensions())

    def _target_dimensions(self) -> int:
        """Return the expected embedding dimensionality for the configured model."""
        model_info = self.MODELS.get(self.model_name, {})
        return int(model_info.get("dimensions", self.FALLBACK_DIMENSIONS))
    
    def _hash_embedding(self, text: str, dimensions: int = None) -> EmbeddingResult:
        """
        Generate a deterministic embedding using hashing.
        Fallback when API is not available.
        """
        dimensions = int(dimensions or self.FALLBACK_DIMENSIONS)
        
        embedding = []
        for i in range(dimensions):
            hash_input = f"{text}:{i}"
            hash_value = hashlib.sha256(hash_input.encode()).hexdigest()
            value = int(hash_value[:8], 16) / (16**8) * 2 - 1
            embedding.append(value)
        
        return EmbeddingResult(
            success=True,
            embedding=embedding,
            model_name='hash-fallback',
            dimensions=dimensions
        )
    
    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 16) -> List[EmbeddingResult]:
        """
        Generate embeddings for multiple texts.
        
        Args:
            texts: List of texts to embed
            batch_size: Batch size for API calls
        
        Returns:
            List of EmbeddingResult objects
        """
        if not texts:
            return []
        
        # Process in batches
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_results = [self.generate_embedding(text) for text in batch]
            results.extend(batch_results)
        
        return results
    
    def cosine_similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
        """
        Calculate cosine similarity between two embeddings.
        """
        if len(embedding1) != len(embedding2):
            return 0.0
        
        # Calculate dot product and norms
        dot_product = sum(a * b for a, b in zip(embedding1, embedding2))
        norm1 = sum(a * a for a in embedding1) ** 0.5
        norm2 = sum(b * b for b in embedding2) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)
    
    def find_similar(self, query_embedding: List[float],
                     embeddings: List[tuple],
                     top_k: int = 10) -> List[tuple]:
        """
        Find most similar embeddings to a query.
        
        Args:
            query_embedding: Query embedding vector
            embeddings: List of (id, embedding) tuples
            top_k: Number of results
        
        Returns:
            List of (id, similarity) tuples sorted by similarity
        """
        similarities = []
        
        for id_, embedding in embeddings:
            similarity = self.cosine_similarity(query_embedding, embedding)
            similarities.append((id_, similarity))
        
        # Sort by similarity (descending)
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        return similarities[:top_k]
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        model_info = self.MODELS.get(self.model_name, {})
        
        # Check subscription status
        has_subscription = False
        try:
            from src.core.cortex_api import get_api_client
            api = get_api_client()
            has_subscription = api.has_subscription()
        except Exception:
            pass

        return {
            'model_name': self.model_name,
            'dimensions': model_info.get('dimensions', self.FALLBACK_DIMENSIONS),
            'quality': model_info.get('quality', 'unknown'),
            'initialized': self._initialized,
            'has_subscription': has_subscription,
            'provider': 'siliconflow'
        }


# Global instance
_siliconflow_embeddings: Optional[SiliconFlowEmbeddings] = None


def get_siliconflow_embeddings(model_name: str = None, api_key: str = None) -> SiliconFlowEmbeddings:
    """Get or create the global SiliconFlow embeddings instance."""
    global _siliconflow_embeddings
    if _siliconflow_embeddings is None:
        _siliconflow_embeddings = SiliconFlowEmbeddings(model_name, api_key)
    return _siliconflow_embeddings
