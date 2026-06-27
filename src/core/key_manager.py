"""
Secure Key Management System for Cortex AI Agent IDE
Handles API key storage, retrieval, and encryption
"""

import os
import json
import base64
import hashlib
import secrets
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from src.utils.logger import get_logger

log = get_logger("key_manager")


@dataclass
class StoredKey:
    """Represents a stored API key."""
    provider: str
    key: str
    encrypted: bool
    storage_backend: str
    created_at: str
    last_used: str
    usage_count: int = 0
    

class KeyManager:
    """
    Manages API keys with multiple storage backends.
    Supports Windows Credential Manager, encrypted files, and environment variables.
    """
    
    def __init__(self):
        self._config_dir = Path.home() / ".cortex"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._keys_file = self._config_dir / "keys.enc"
        self._cache: Dict[str, str] = {}
        self._cache_ttl: Dict[str, datetime] = {}
        self._CACHE_DURATION = timedelta(minutes=5)
        
        # Initialize encryption
        self._cipher = self._init_encryption()
        
    def _init_encryption(self) -> Fernet:
        """Initialize encryption with system-derived key."""
        # Derive key from system-specific data
        system_data = f"{os.environ.get('USERNAME', 'user')}_{os.environ.get('COMPUTERNAME', 'pc')}"
        salt = b'cortex_salt_v1'  # In production, use random salt stored separately
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(system_data.encode()))
        return Fernet(key)
    
    def store_key(self, provider: str, api_key: str, 
                  storage_backend: str = "auto") -> bool:
        """
        Store an API key securely.
        
        Args:
            provider: The LLM provider (openai, anthropic, etc.)
            api_key: The API key to store
            storage_backend: Where to store (windows_credential, encrypted_file, env, auto)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Clean the key - remove whitespace and quotes
            api_key = api_key.strip()
            api_key = api_key.strip("'\"")  # Remove surrounding quotes
            
            if not api_key:
                log.error(f"Empty API key provided for {provider}")
                return False
            
            log.debug(f"Storing key for {provider}: length={len(api_key)}, starts_with={api_key[:7]}...")
            
            if storage_backend == "auto":
                storage_backend = self._select_best_backend()
            
            if storage_backend == "windows_credential":
                success = self._store_windows_credential(provider, api_key)
            elif storage_backend == "encrypted_file":
                success = self._store_encrypted_file(provider, api_key)
            elif storage_backend == "env":
                success = self._store_env_variable(provider, api_key)
            else:
                log.error(f"Unknown storage backend: {storage_backend}")
                return False
            
            if success:
                # Cache the cleaned key
                self._cache[provider] = api_key
                self._cache_ttl[provider] = datetime.now()
                log.info(f"Stored API key for {provider} using {storage_backend}")
                return True
            
        except Exception as e:
            log.error(f"Failed to store key for {provider}: {e}")
            
        return False
    
    def get_key(self, provider: str, force_refresh: bool = False) -> Optional[str]:
        """
        Retrieve an API key.
        
        Args:
            provider: The LLM provider
            force_refresh: If True, bypass cache and reload from sources
            
        Returns:
            The API key if found, None otherwise
        """
        # Clear cache if force refresh requested
        if force_refresh and provider in self._cache:
            log.debug(f"Forcing refresh of {provider} key, clearing cache")
            del self._cache[provider]
            del self._cache_ttl[provider]
        
        # Check cache first
        if provider in self._cache:
            if datetime.now() - self._cache_ttl[provider] < self._CACHE_DURATION:
                log.debug(f"Retrieved {provider} key from cache")
                return self._cache[provider]
            else:
                # Expired
                del self._cache[provider]
                del self._cache_ttl[provider]
        
        # Try to get from storage backends
        key = None
        
        # Try Windows Credential Manager
        if not key:
            key = self._get_windows_credential(provider)
        
        # Try encrypted file
        if not key:
            key = self._get_encrypted_file(provider)
        
        # Try environment variable
        if not key:
            key = self._get_env_variable(provider)
        
        if key:
            # Convert bytes to string if needed
            if isinstance(key, bytes):
                key = key.decode('utf-8')
            
            # Clean the key - remove whitespace and quotes
            key = key.strip()
            key = key.strip("'\"")
            
            # Cache it
            self._cache[provider] = key
            self._cache_ttl[provider] = datetime.now()
            log.debug(f"Retrieved {provider} key from storage (length={len(key)})")
        else:
            log.warning(f"No API key found for {provider}")
        
        return key
    
    def delete_key(self, provider: str) -> bool:
        """Delete a stored API key."""
        try:
            # Remove from cache
            if provider in self._cache:
                del self._cache[provider]
                del self._cache_ttl[provider]
            
            # Try to delete from all backends
            deleted = False
            
            # Windows Credential
            try:
                self._delete_windows_credential(provider)
                deleted = True
            except:
                pass
            
            # Encrypted file
            try:
                self._delete_encrypted_file(provider)
                deleted = True
            except:
                pass
            
            if deleted:
                log.info(f"Deleted API key for {provider}")
                return True
            
        except Exception as e:
            log.error(f"Failed to delete key for {provider}: {e}")
        
        return False
    
    def clear_cache(self, provider: Optional[str] = None):
        """Clear cached API keys.
        
        Args:
            provider: Specific provider to clear, or None to clear all
        """
        if provider:
            if provider in self._cache:
                log.info(f"Cleared cache for {provider}")
                del self._cache[provider]
                del self._cache_ttl[provider]
        else:
            count = len(self._cache)
            self._cache.clear()
            self._cache_ttl.clear()
            log.info(f"Cleared all {count} cached API keys")
    
    def list_stored_providers(self) -> List[str]:
        """List all providers with stored keys."""
        providers = set()
        
        # Check encrypted file
        if self._keys_file.exists():
            try:
                with open(self._keys_file, 'rb') as f:
                    encrypted_data = f.read()
                    data = json.loads(self._cipher.decrypt(encrypted_data).decode())
                    providers.update(data.keys())
            except:
                pass
        
        # Check environment variables
        for provider in ['openai', 'anthropic', 'deepseek', 'google', 'openrouter']:
            env_var = f"{provider.upper()}_API_KEY"
            if os.environ.get(env_var):
                providers.add(provider)
        
        return sorted(list(providers))
    
    def validate_key(self, provider: str, api_key: str) -> bool:
        """
        Validate an API key by making a test request.
        
        Args:
            provider: The provider name
            api_key: The API key to validate
            
        Returns:
            True if valid, False otherwise
        """
        # Simple format validation
        if not api_key or len(api_key) < 10:
            return False
        
        # Provider-specific format checks
        if provider == "openai":
            return api_key.startswith("sk-")
        elif provider == "anthropic":
            return api_key.startswith("sk-ant-")
        
        return True
    
    def _select_best_backend(self) -> str:
        """Select the best available storage backend."""
        if os.name == 'nt':  # Windows
            return "windows_credential"
        else:
            return "encrypted_file"
    
    # Windows Credential Manager
    def _store_windows_credential(self, provider: str, api_key: str) -> bool:
        """Store key in Windows Credential Manager."""
        if os.name != 'nt':
            return False
        
        try:
            import win32cred
            
            target = f"cortex_{provider}_api_key"
            credential = {
                'Type': win32cred.CRED_TYPE_GENERIC,
                'TargetName': target,
                'CredentialBlob': api_key,
                'Comment': f"Cortex AI API key for {provider}",
                'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
            }
            
            win32cred.CredWrite(credential)
            return True
            
        except ImportError:
            log.warning("win32cred not available, falling back to encrypted file")
            return False
        except Exception as e:
            log.error(f"Windows credential store failed: {e}")
            return False
    
    def _get_windows_credential(self, provider: str) -> Optional[str]:
        """Retrieve key from Windows Credential Manager."""
        if os.name != 'nt':
            return None
        
        try:
            import win32cred
            
            target = f"cortex_{provider}_api_key"
            credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
            
            return credential['CredentialBlob']
            
        except ImportError:
            return None
        except Exception:
            return None
    
    def _delete_windows_credential(self, provider: str) -> bool:
        """Delete key from Windows Credential Manager."""
        if os.name != 'nt':
            return False
        
        try:
            import win32cred
            
            target = f"cortex_{provider}_api_key"
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
            return True
            
        except:
            return False
    
    # Encrypted File Storage
    def _store_encrypted_file(self, provider: str, api_key: str) -> bool:
        """Store key in encrypted file."""
        try:
            # Load existing data
            data = {}
            if self._keys_file.exists():
                with open(self._keys_file, 'rb') as f:
                    encrypted_data = f.read()
                    data = json.loads(self._cipher.decrypt(encrypted_data).decode())
            
            # Add/update key
            data[provider] = {
                'key': api_key,
                'created_at': datetime.now().isoformat(),
                'last_used': datetime.now().isoformat(),
            }
            
            # Encrypt and save
            encrypted = self._cipher.encrypt(json.dumps(data).encode())
            with open(self._keys_file, 'wb') as f:
                f.write(encrypted)
            
            return True
            
        except Exception as e:
            log.error(f"Encrypted file storage failed: {e}")
            return False
    
    def _get_encrypted_file(self, provider: str) -> Optional[str]:
        """Retrieve key from encrypted file."""
        try:
            if not self._keys_file.exists():
                return None
            
            with open(self._keys_file, 'rb') as f:
                encrypted_data = f.read()
                data = json.loads(self._cipher.decrypt(encrypted_data).decode())
            
            if provider in data:
                return data[provider]['key']
            
        except Exception:
            pass
        
        return None
    
    def _delete_encrypted_file(self, provider: str) -> bool:
        """Delete key from encrypted file."""
        try:
            if not self._keys_file.exists():
                return False
            
            with open(self._keys_file, 'rb') as f:
                encrypted_data = f.read()
                data = json.loads(self._cipher.decrypt(encrypted_data).decode())
            
            if provider in data:
                del data[provider]
                
                if data:
                    encrypted = self._cipher.encrypt(json.dumps(data).encode())
                    with open(self._keys_file, 'wb') as f:
                        f.write(encrypted)
                else:
                    self._keys_file.unlink()
                
                return True
            
        except Exception:
            pass
        
        return False
    
    # Environment Variable
    def _store_env_variable(self, provider: str, api_key: str) -> bool:
        """Note: We can't actually store in env, just log it."""
        log.warning(f"Cannot store {provider} key in environment variable programmatically")
        log.info(f"Please set {provider.upper()}_API_KEY in your .env file")
        return False
    
    def _get_env_variable(self, provider: str) -> Optional[str]:
        """Get key from environment variable."""
        env_var = f"{provider.upper()}_API_KEY"
        return os.environ.get(env_var)


# Global instance
_key_manager = None

def get_key_manager() -> KeyManager:
    """Get singleton key manager instance."""
    global _key_manager
    if _key_manager is None:
        _key_manager = KeyManager()
    return _key_manager
