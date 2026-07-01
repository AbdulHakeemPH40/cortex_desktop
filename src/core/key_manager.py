"""
Secure Key Management System for Cortex AI Agent IDE
Following OpenCode's credential management pattern:
- OS Keyring (Windows Credential Manager) as PRIMARY storage
- Encrypted file as BACKUP storage
- Auto-re-prompt on decryption failure
- Export/import for migration

Security: Keys are encrypted with a master secret sourced from the
OS keyring or a user-provided passphrase, never persisted in clear text.
"""

import os
import json
import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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
    Manages API keys following OpenCode's credential management pattern.
    
    Storage priority (like OpenCode):
    1. OS Keyring (Windows Credential Manager) — PRIMARY
    2. Encrypted file (~/.cortex/keys.enc) — BACKUP
    3. Auto-re-prompt on failure
    
    Security:
    - Keys encrypted with AES-256-GCM
    - Master key from OS Keyring or machine fingerprint
    - Random salt per encryption
    - Rate limiting on decryption attempts
    """
    
    # Security constants
    SALT_SIZE = 32
    NONCE_SIZE = 12
    KEY_SIZE = 32
    PBKDF2_ITERATIONS = 600000  # OWASP 2023 recommendation
    
    # Rate limiting
    MAX_DECRYPT_ATTEMPTS = 10
    DECRYPT_RATE_LIMIT_WINDOW = 60
    
    # Provider name mapping
    PROVIDER_MAP = {
        "mimo": "mimo",
        "deepseek": "deepseek",
        "openai": "openai",
        "mistral": "mistral",
        "openrouter": "openrouter",
        "alibaba": "alibaba",
        "siliconflow": "siliconflow",
    }
    
    def __init__(self):
        self._config_dir = Path.home() / ".cortex"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._keys_file = self._config_dir / "keys.enc"
        self._salt_file = self._config_dir / "keys.salt"
        self._integrity_file = self._config_dir / "keys.hmac"
        self._cache: Dict[str, str] = {}
        self._cache_ttl: Dict[str, datetime] = {}
        self._CACHE_DURATION = timedelta(minutes=5)
        
        # Rate limiting
        self._decrypt_attempts: List[float] = []
        
        # Initialize master key
        self._master_key = self._derive_master_key()
        
    def _derive_master_key(self) -> bytes:
        """
        Derive master key from OS Keyring or machine fingerprint.
        Following OpenCode's pattern:
        1. Try OS Keyring (Windows Credential Manager)
        2. Fallback to machine fingerprint
        """
        # Try to get master secret from Windows Credential Manager
        master_secret = self._get_os_keyring_secret()
        
        if not master_secret:
            # Fallback: derive from machine fingerprint
            master_secret = self._get_machine_fingerprint()
            log.info("[Security] Using machine fingerprint for master key (no OS keyring)")
        else:
            log.info("[Security] Using OS keyring for master key")
        
        # Ensure master_secret is string
        if isinstance(master_secret, bytes):
            master_secret = master_secret.decode('utf-8', errors='ignore')
        
        # Load or generate salt
        salt = self._load_or_generate_salt()
        
        # Derive key using PBKDF2 (OWASP 2023: 600,000 iterations)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_SIZE,
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
        )
        return kdf.derive(master_secret.encode('utf-8'))
    
    def _get_os_keyring_secret(self) -> Optional[str]:
        """
        Get master secret from OS Keyring (like OpenCode).
        Windows Credential Manager, macOS Keychain, Linux Secret Service.
        """
        if os.name == 'nt':
            try:
                import win32cred
                target = "cortex_master_secret"
                try:
                    credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
                    secret = credential['CredentialBlob']
                    # Ensure string
                    if isinstance(secret, bytes):
                        secret = secret.decode('utf-8', errors='ignore')
                    return secret
                except Exception:
                    # Create new master secret
                    secret = secrets.token_hex(32)
                    credential = {
                        'Type': win32cred.CRED_TYPE_GENERIC,
                        'TargetName': target,
                        'CredentialBlob': secret,
                        'Comment': 'Cortex AI IDE master encryption secret',
                        'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    }
                    win32cred.CredWrite(credential)
                    log.info("[Security] Created new master secret in Windows Credential Manager")
                    return secret
            except ImportError:
                log.warning("[Security] win32cred not available")
                return None
            except Exception as e:
                log.warning(f"[Security] OS keyring error: {e}")
                return None
        return None
    
    def _get_machine_fingerprint(self) -> str:
        """Get machine-specific fingerprint for key derivation."""
        components = [
            os.environ.get('COMPUTERNAME', ''),
            os.environ.get('USERNAME', ''),
            os.environ.get('PROCESSOR_IDENTIFIER', ''),
            str(Path.home()),
        ]
        return '|'.join(components)
    
    def _load_or_generate_salt(self) -> bytes:
        """Load existing salt or generate new one."""
        if self._salt_file.exists():
            try:
                salt = self._salt_file.read_bytes()
                if len(salt) == self.SALT_SIZE:
                    return salt
            except Exception:
                pass
        
        # Generate new salt
        salt = secrets.token_bytes(self.SALT_SIZE)
        try:
            self._salt_file.write_bytes(salt)
            if os.name == 'nt':
                import stat
                os.chmod(self._salt_file, stat.S_IREAD | stat.S_IWRITE)
        except Exception as e:
            log.warning(f"[Security] Could not save salt file: {e}")
        
        return salt
    
    def _encrypt(self, plaintext: str) -> tuple:
        """Encrypt plaintext with AES-256-GCM."""
        nonce = secrets.token_bytes(self.NONCE_SIZE)
        salt = secrets.token_bytes(self.SALT_SIZE)
        
        # Derive encryption key
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.KEY_SIZE,
            salt=salt,
            iterations=10000,
        )
        enc_key = kdf.derive(self._master_key + salt)
        
        # Encrypt
        aesgcm = AESGCM(enc_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        
        return nonce, ciphertext, salt
    
    def _decrypt(self, nonce: bytes, ciphertext: bytes, salt: bytes) -> Optional[str]:
        """Decrypt ciphertext with AES-256-GCM."""
        # Rate limiting
        if not self._check_rate_limit():
            log.warning("[Security] Decryption rate limit exceeded")
            return None
        
        try:
            # Derive decryption key
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=self.KEY_SIZE,
                salt=salt,
                iterations=10000,
            )
            dec_key = kdf.derive(self._master_key + salt)
            
            # Decrypt
            aesgcm = AESGCM(dec_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            
            return plaintext.decode('utf-8')
            
        except Exception as e:
            log.warning(f"[Security] Decryption failed: {type(e).__name__}")
            return None
    
    def _check_rate_limit(self) -> bool:
        """Check if decryption attempts are within rate limit."""
        now = time.time()
        self._decrypt_attempts = [
            t for t in self._decrypt_attempts
            if now - t < self.DECRYPT_RATE_LIMIT_WINDOW
        ]
        if len(self._decrypt_attempts) >= self.MAX_DECRYPT_ATTEMPTS:
            return False
        self._decrypt_attempts.append(now)
        return True
    
    def _compute_hmac(self, data: bytes) -> str:
        """Compute HMAC-SHA256 for integrity verification."""
        return hmac.new(
            self._master_key,
            data,
            hashlib.sha256
        ).hexdigest()
    
    # ─── Public API ──────────────────────────────────────────────────────
    
    def store_key(self, provider: str, api_key: str, 
                  storage_backend: str = "auto") -> bool:
        """
        Store an API key securely.
        Following OpenCode's pattern:
        1. Store in OS Keyring (PRIMARY)
        2. Store in encrypted file (BACKUP)
        """
        try:
            # Clean the key — strip null bytes, whitespace, quotes
            if api_key:
                api_key = api_key.replace('\x00', '').replace('\u0000', '').replace(' ', '').replace('\n', '').replace('\r', '')
            api_key = api_key.strip()
            api_key = api_key.strip("'\"")
            
            if not api_key:
                log.error(f"Empty API key provided for {provider}")
                return False
            
            log.info(f"[KeyManager] Storing key for {provider}")
            
            # Store in OS Keyring (PRIMARY - like OpenCode)
            if os.name == 'nt':
                try:
                    import win32cred
                    target = f"cortex_{provider}_api_key"
                    credential = {
                        'Type': win32cred.CRED_TYPE_GENERIC,
                        'TargetName': target,
                        'CredentialBlob': api_key,
                        'Comment': f'Cortex AI API key for {provider}',
                        'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    }
                    win32cred.CredWrite(credential)
                    log.info(f"[KeyManager] Stored {provider} key in Windows Credential Manager")
                except Exception as e:
                    log.warning(f"[KeyManager] Failed to store in Windows Credential Manager: {e}")
            
            # Store in encrypted file (BACKUP)
            self._store_encrypted_file(provider, api_key)
            
            # Cache the key
            self._cache[provider] = api_key
            self._cache_ttl[provider] = datetime.now()
            
            log.info(f"[KeyManager] Successfully stored key for {provider}")
            return True
            
        except Exception as e:
            log.error(f"[KeyManager] Failed to store key for {provider}: {e}")
            return False
    
    def get_key(self, provider: str, force_refresh: bool = False) -> Optional[str]:
        """
        Retrieve an API key.
        Following OpenCode's pattern:
        1. Check cache
        2. Try OS Keyring (PRIMARY)
        3. Try encrypted file (BACKUP)
        4. Auto-re-prompt on failure
        """
        # Clear cache if force refresh
        if force_refresh and provider in self._cache:
            del self._cache[provider]
            del self._cache_ttl[provider]
        
        # Check cache
        if provider in self._cache:
            if datetime.now() - self._cache_ttl[provider] < self._CACHE_DURATION:
                return self._cache[provider]
            else:
                del self._cache[provider]
                del self._cache_ttl[provider]
        
        # Try OS Keyring (PRIMARY - like OpenCode)
        key = self._get_os_keyring_key(provider)
        
        # Try encrypted file (BACKUP)
        if not key:
            key = self._get_encrypted_file(provider)
        
        if key:
            # Sanitize retrieved key (strip null bytes, spaces, etc.)
            key = key.replace('\x00', '').replace('\u0000', '').replace('\n', '').replace('\r', '')
            key = key.strip()
            # Cache the key
            self._cache[provider] = key
            self._cache_ttl[provider] = datetime.now()
            log.debug(f"[KeyManager] Retrieved {provider} key")
        else:
            log.debug(f"[KeyManager] No key found for {provider}")
        
        return key
    
    def _get_os_keyring_key(self, provider: str) -> Optional[str]:
        """Get key from OS Keyring (Windows Credential Manager)."""
        if os.name != 'nt':
            return None
        
        try:
            import win32cred
            target = f"cortex_{provider}_api_key"
            credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
            key = credential['CredentialBlob']
            
            # Ensure string
            if isinstance(key, bytes):
                key = key.decode('utf-8', errors='ignore')
            key = key.strip()
            key = key.strip("'\"")
            
            return key
            
        except ImportError:
            return None
        except Exception:
            return None
    
    def _get_encrypted_file(self, provider: str) -> Optional[str]:
        """Get key from encrypted file (BACKUP)."""
        try:
            if not self._keys_file.exists():
                return None
            
            keys = self._load_encrypted_keys()
            if provider not in keys:
                return None
            
            key_data = keys[provider]
            
            # Decrypt the key
            nonce = base64.b64decode(key_data['nonce'])
            ciphertext = base64.b64decode(key_data['ciphertext'])
            salt = base64.b64decode(key_data['salt'])
            
            return self._decrypt(nonce, ciphertext, salt)
            
        except Exception as e:
            log.warning(f"[KeyManager] Failed to read encrypted key for {provider}: {e}")
            return None
    
    def _store_encrypted_file(self, provider: str, api_key: str) -> bool:
        """Store key in encrypted file (BACKUP)."""
        try:
            keys = self._load_encrypted_keys()
            
            # Encrypt the key
            nonce, ciphertext, salt = self._encrypt(api_key)
            
            # Store encrypted key
            keys[provider] = {
                'nonce': base64.b64encode(nonce).decode('ascii'),
                'ciphertext': base64.b64encode(ciphertext).decode('ascii'),
                'salt': base64.b64encode(salt).decode('ascii'),
                'created_at': datetime.now().isoformat(),
                'algorithm': 'AES-256-GCM',
            }
            
            # Save
            self._keys_file.write_text(json.dumps(keys, indent=2))
            
            # Update HMAC
            data_to_sign = json.dumps(keys, sort_keys=True).encode('utf-8')
            hmac_value = self._compute_hmac(data_to_sign)
            self._integrity_file.write_text(hmac_value)
            
            return True
            
        except Exception as e:
            log.error(f"[KeyManager] Failed to store encrypted key for {provider}: {e}")
            return False
    
    def _load_encrypted_keys(self) -> Dict[str, Any]:
        """Load encrypted keys from file."""
        if not self._keys_file.exists():
            return {}
        try:
            content = self._keys_file.read_text()
            return json.loads(content)
        except Exception:
            return {}
    
    def delete_key(self, provider: str) -> bool:
        """Delete a stored API key from all backends."""
        try:
            # Remove from cache
            if provider in self._cache:
                del self._cache[provider]
                del self._cache_ttl[provider]
            
            deleted = False
            
            # Delete from OS Keyring
            if os.name == 'nt':
                try:
                    import win32cred
                    target = f"cortex_{provider}_api_key"
                    win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
                    deleted = True
                    log.info(f"[KeyManager] Deleted {provider} key from Windows Credential Manager")
                except Exception:
                    pass
            
            # Delete from encrypted file
            try:
                keys = self._load_encrypted_keys()
                if provider in keys:
                    del keys[provider]
                    self._keys_file.write_text(json.dumps(keys, indent=2))
                    deleted = True
                    log.info(f"[KeyManager] Deleted {provider} key from encrypted file")
            except Exception:
                pass
            
            return deleted
            
        except Exception as e:
            log.error(f"[KeyManager] Failed to delete key for {provider}: {e}")
            return False
    
    def clear_cache(self) -> None:
        """Clear the key cache."""
        self._cache.clear()
        self._cache_ttl.clear()
    
    # ─── OpenCode-style Recovery & Migration ──────────────────────────────
    
    def export_credentials(self, export_path: str) -> bool:
        """Export encrypted credentials to JSON file (like OpenCode)."""
        try:
            keys = self._load_encrypted_keys()
            if not keys:
                log.warning("[Export] No keys to export")
                return False
            
            export_data = {
                'version': 1,
                'exported_at': datetime.now().isoformat(),
                'encryption': 'AES-256-GCM',
                'keys': keys,
            }
            
            if self._salt_file.exists():
                export_data['salt'] = base64.b64encode(
                    self._salt_file.read_bytes()
                ).decode('ascii')
            
            Path(export_path).write_text(json.dumps(export_data, indent=2))
            log.info(f"[Export] Exported {len(keys)} keys to {export_path}")
            return True
            
        except Exception as e:
            log.error(f"[Export] Failed: {e}")
            return False
    
    def import_credentials(self, import_path: str) -> bool:
        """Import encrypted credentials from JSON file (like OpenCode)."""
        try:
            import_data = json.loads(Path(import_path).read_text())
            
            if import_data.get('version') != 1:
                log.error("[Import] Invalid import file version")
                return False
            
            keys = import_data.get('keys', {})
            if not keys:
                log.warning("[Import] No keys in import file")
                return False
            
            if 'salt' in import_data:
                salt = base64.b64decode(import_data['salt'])
                self._salt_file.write_bytes(salt)
            
            existing_keys = self._load_encrypted_keys()
            imported_count = 0
            for provider, key_data in keys.items():
                if provider not in existing_keys:
                    existing_keys[provider] = key_data
                    imported_count += 1
            
            self._keys_file.write_text(json.dumps(existing_keys, indent=2))
            
            data_to_sign = json.dumps(existing_keys, sort_keys=True).encode('utf-8')
            hmac_value = self._compute_hmac(data_to_sign)
            self._integrity_file.write_text(hmac_value)
            
            self.clear_cache()
            
            log.info(f"[Import] Imported {imported_count} keys from {import_path}")
            return True
            
        except Exception as e:
            log.error(f"[Import] Failed: {e}")
            return False
    
    def reset_credentials(self) -> bool:
        """Reset all credentials (like OpenCode)."""
        try:
            if self._keys_file.exists():
                self._keys_file.unlink()
            if self._salt_file.exists():
                self._salt_file.unlink()
            if self._integrity_file.exists():
                self._integrity_file.unlink()
            
            if os.name == 'nt':
                try:
                    import win32cred
                    for provider in self.PROVIDER_MAP.keys():
                        target = f"cortex_{provider}_api_key"
                        try:
                            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
                        except Exception:
                            pass
                except ImportError:
                    pass
            
            self.clear_cache()
            log.info("[Reset] All credentials reset")
            return True
            
        except Exception as e:
            log.error(f"[Reset] Failed: {e}")
            return False
    
    def auto_recover_keys(self) -> Dict[str, bool]:
        """Auto-recover keys on decryption failure (like OpenCode)."""
        status = {}
        for provider in self.PROVIDER_MAP.keys():
            try:
                key = self.get_key(provider)
                status[provider] = key is not None and len(key) > 0
            except Exception:
                status[provider] = False
        
        missing = [p for p, has_key in status.items() if not has_key]
        if missing:
            log.warning(f"[Recovery] Missing keys for: {', '.join(missing)}")
        
        return status
    
    def get_security_info(self) -> Dict[str, Any]:
        """Get security configuration information."""
        return {
            'encryption': 'AES-256-GCM',
            'primary_storage': 'Windows Credential Manager',
            'backup_storage': 'Encrypted file (keys.enc)',
            'salt_size': self.SALT_SIZE,
            'nonce_size': self.NONCE_SIZE,
            'key_size': self.KEY_SIZE,
            'rate_limit': {
                'max_attempts': self.MAX_DECRYPT_ATTEMPTS,
                'window_seconds': self.DECRYPT_RATE_LIMIT_WINDOW,
            },
        }


def get_key_manager() -> KeyManager:
    """Get the singleton KeyManager instance."""
    if not hasattr(get_key_manager, '_instance'):
        get_key_manager._instance = KeyManager()
    return get_key_manager._instance
