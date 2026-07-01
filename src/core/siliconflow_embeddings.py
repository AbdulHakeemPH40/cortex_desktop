"""
SiliconFlow Embeddings - Cloud-based semantic embeddings using Qwen models
No heavy local dependencies - calls SiliconFlow API
"""

import os
import hashlib
import threading
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from src.utils.logger import get_logger

log = get_logger("siliconflow_embeddings")

# Try to import requests
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    log.warning("requests not installed. Install with: pip install requests")


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
    Generate embeddings using SiliconFlow API (Qwen models).
    No local model needed - calls cloud API.
    True semantic understanding.
    """
    
    # SiliconFlow API endpoint (OpenAI-compatible)
    API_URL = "https://api.siliconflow.com/v1/embeddings"
    
    # Available models
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
            api_key: SiliconFlow API key (or set SILICONFLOW_API_KEY env var)
        """
        self.model_name = model_name or self.DEFAULT_MODEL
        self.api_key = api_key or self._get_api_key()
        self._lock = threading.Lock()
        self._initialized = bool(self.api_key)
        
        if self._initialized:
            model_info = self.MODELS.get(self.model_name, {})
            log.info(f"SiliconFlow embeddings initialized: {self.model_name} ({model_info.get('quality', 'unknown')} quality)")
        else:
            log.warning("SILICONFLOW_API_KEY not set. Using hash-based fallback.")
    
    def _get_api_key(self) -> Optional[str]:
        """Get API key from KeyManager ONLY (Windows Credential Manager)."""
        # Try key manager (Windows Credential Manager - encrypted)
        try:
            from src.core.key_manager import KeyManager
            km = KeyManager()
            api_key = km.get_key("siliconflow")
            if api_key:
                # Sanitize: strip null bytes, spaces, newlines (critical for subprocess argv)
                api_key = api_key.replace('\x00', '').replace('\u0000', '').replace('\n', '').replace('\r', '').strip()
                return api_key
        except Exception:
            pass
        
        # NO .env fallback - subscription required for embeddings
        return None
    
    def generate_embedding(self, text: str) -> EmbeddingResult:
        """
        Generate an embedding for a single text.
        
        Args:
            text: Text to embed
        
        Returns:
            EmbeddingResult with embedding vector
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
        
        # Try SiliconFlow API
        if self.api_key and HAS_REQUESTS:
            try:
                api_result = self._call_api(text)
                if api_result.success:
                    return api_result
                log.warning(f"SiliconFlow API returned an error: {api_result.error}. Using hash fallback.")
            except Exception as e:
                log.warning(f"SiliconFlow API failed: {e}. Using hash fallback.")

        # Fallback: hash-based embedding (match model dimensions to avoid mixed-size vectors)
        return self._hash_embedding(text, dimensions=self._target_dimensions())

    def _target_dimensions(self) -> int:
        """Return the expected embedding dimensionality for the configured model."""
        model_info = self.MODELS.get(self.model_name, {})
        return int(model_info.get("dimensions", self.FALLBACK_DIMENSIONS))
    
    def _call_api(self, text: str) -> EmbeddingResult:
        """Call SiliconFlow embedding API.

        On Windows, urllib3's socket.create_connection() can trigger a fatal
        C-level access violation (0xC0000005) that Python cannot catch.
        We isolate the network call in a subprocess so that if it crashes,
        only the subprocess dies — the main IDE process survives.
        """
        import subprocess as _sp
        import sys as _sys
        import json as _json

        payload = {
            "model": self.model_name,
            "input": text,
            "encoding_format": "float"
        }

        # Inline Python script that runs in an isolated process
        # Parse JSON inside subprocess to avoid truncating mid-string
        script = (
            "import sys, json\n"
            "try:\n"
            "    import requests\n"
            "except ImportError:\n"
            "    print(json.dumps({'s': 0, 'e': 'requests not installed'}))\n"
            "    sys.exit(0)\n"
            "try:\n"
            "    headers = {'Authorization': 'Bearer ' + sys.argv[1], 'Content-Type': 'application/json'}\n"
            "    payload = json.loads(sys.argv[2])\n"
            "    resp = requests.post(sys.argv[3], headers=headers, json=payload, timeout=25)\n"
            "    body = resp.json()\n"
            "    data = body.get('data', [{}])[0].get('embedding', [])\n"
            "    usage = body.get('usage', {})\n"
            "    print(json.dumps({'s': resp.status_code, 'd': data, 'u': usage}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'s': 0, 'e': str(e)}))\n"
        )

        try:
            # Final safety: strip any null bytes from api_key before subprocess (Windows argv rejects nulls)
            safe_key = self.api_key.replace('\x00', '').replace('\u0000', '').strip() if self.api_key else ''
            proc = _sp.run(
                [_sys.executable, "-c", script, safe_key, _json.dumps(payload), self.API_URL],
                capture_output=True, text=True, timeout=30,
                creationflags=0x08000000 if _sys.platform == 'win32' else 0,
            )
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or "").strip()[:200]
                log.warning(f"SiliconFlow subprocess crashed (rc={proc.returncode}): {stderr_tail}")
                return EmbeddingResult(success=False, error=f"Subprocess crashed: rc={proc.returncode}")

            stdout = proc.stdout.strip()
            if not stdout:
                stderr_tail = (proc.stderr or "").strip()[:200]
                log.warning(f"SiliconFlow subprocess returned empty stdout. stderr: {stderr_tail}")
                return EmbeddingResult(success=False, error=f"Empty subprocess output: {stderr_tail}")

            result = _json.loads(stdout)
            if not isinstance(result, dict):
                error_msg = f"SiliconFlow API returned non-dict: {type(result).__name__} = {str(result)[:200]}"
                log.warning(error_msg)
                return EmbeddingResult(success=False, error=error_msg)
            status = result.get('s', 0)

            if status == 200:
                embedding = result.get('d', [])
                usage = result.get('u', {})
                if not embedding:
                    return EmbeddingResult(success=False, error="No embedding in response")
                return EmbeddingResult(
                    success=True, embedding=embedding,
                    model_name=self.model_name,
                    dimensions=len(embedding),
                    tokens_used=usage.get("total_tokens", 0),
                )
            else:
                error_msg = f"API error {status}: {result.get('e', '')[:200]}"
                log.error(error_msg)
                return EmbeddingResult(success=False, error=error_msg)

        except _sp.TimeoutExpired:
            log.warning("SiliconFlow subprocess timed out (30s)")
            return EmbeddingResult(success=False, error="Subprocess timeout")
        except _json.JSONDecodeError as e:
            log.warning(f"SiliconFlow subprocess returned invalid JSON: {e}")
            return EmbeddingResult(success=False, error=f"Invalid JSON: {e}")
        except Exception as e:
            log.warning(f"SiliconFlow subprocess error: {e}")
            return EmbeddingResult(success=False, error=f"Subprocess error: {e}")
    
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
        
        return {
            'model_name': self.model_name,
            'dimensions': model_info.get('dimensions', self.FALLBACK_DIMENSIONS),
            'quality': model_info.get('quality', 'unknown'),
            'initialized': self._initialized,
            'has_api_key': bool(self.api_key),
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
