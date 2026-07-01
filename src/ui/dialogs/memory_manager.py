"""
Web-based Memory Manager dialog for Cortex IDE.

Hosts a QWebEngineView that renders the memory manager UI from
src/ui/html/memory_manager/memory_management.html and exposes a small
QWebChannel bridge for memory CRUD actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QMessageBox, QVBoxLayout
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

from src.utils.logger import get_logger

log = get_logger("memory_manager")

# Import cross-project memory manager
try:
    from src.agent.src.memdir.crossProjectMemory import get_cross_project_manager
    HAS_CROSS_PROJECT = True
except ImportError:
    HAS_CROSS_PROJECT = False


def _parse_frontmatter(content: str):
    """Return (frontmatter_dict, body_text)."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4 :].strip()
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        fm[key] = value
    return fm, body


def _compute_memory_dir(project_root: str) -> str:
    """Return the memory directory INSIDE the project at <project>/.cortex/memory/"""
    return os.path.join(project_root, ".cortex", "memory")

def _compute_global_memory_dir() -> str:
    try:
        from src.agent.src.memdir.paths import getGlobalMemPath
        return getGlobalMemPath().rstrip("/\\")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".cortex", "global", "memory")


def _compute_project_rules_dir(project_root: str) -> str:
    return os.path.join(project_root, ".cortex", "rules")


def _compute_global_rules_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".cortex", "rules")


def _age_label(mtime: float) -> str:
    days = int((time.time() - mtime) / 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _load_memories(memory_dir: str) -> List[dict]:
    memories: List[dict] = []
    if not os.path.isdir(memory_dir):
        return memories

    for root, _dirs, files in os.walk(memory_dir):
        for fname in files:
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
                with open(fpath, encoding="utf-8") as handle:
                    raw = handle.read()
                fm, body = _parse_frontmatter(raw)
                mem_type = fm.get("type", "").strip()
                description = fm.get("description", "").strip()
                memories.append(
                    {
                        "path": fpath,
                        "filename": os.path.relpath(fpath, memory_dir).replace("\\", "/"),
                        "name": fm.get("name") or os.path.splitext(fname)[0],
                        "description": description,
                        "type": mem_type,
                        "body": body,
                        "mtime": mtime,
                        "age": _age_label(mtime),
                        "stale": int((time.time() - mtime) / 86400) > 7,
                        "keywords": [part.strip() for part in description.split(",") if part.strip()],
                    }
                )
            except Exception as exc:
                log.warning(f"Failed to parse memory file {fpath}: {exc}")

    memories.sort(key=lambda item: item["mtime"], reverse=True)
    return memories


class _MemoryPage(QWebEnginePage):
    """Capture JS console messages for easier debugging."""

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        level_value = level.value if hasattr(level, "value") else int(level)
        # Route by level: INFO(0)→debug, WARN(1)→warning, ERROR(2+)→error
        # JS info messages too verbose for file — only warn/error go to file
        if level_value == 1:
            log.warning(f"[MEMORY_JS-WARN] {message}")
        elif level_value >= 2:
            log.error(f"[MEMORY_JS-ERROR] {message}")
        else:
            log.debug(f"[MEMORY_JS] {message}")

    def createWindow(self, window_type):
        """CAPSULE-FIX-R11: Prevent WebEngine from spawning native top-level
        windows (with title bar [−][□][X]). Return self to handle in-page."""
        log.warning("[CAPSULE-FIX-R11] MemoryManager.createWindow() intercepted — "
                    "preventing native window spawn")
        return self


class MemoryManagerBridge(QObject):
    data_changed = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)
    model_changed = pyqtSignal(str, str)  # (model_id, model_label)
    login_complete = pyqtSignal(object)   # Emits user_info dict or None

    def __init__(self, project_root: str, settings=None, parent=None):
        super().__init__(parent)
        self._project_root = project_root or os.getcwd()
        self._settings = settings
        self._project_memory_dir = _compute_memory_dir(self._project_root)
        self._global_memory_dir = _compute_global_memory_dir()
        self._project_rules_dir = _compute_project_rules_dir(self._project_root)
        self._global_rules_dir = _compute_global_rules_dir()
        if self._settings:
            self._enabled = bool(self._settings.get("memory", "enabled", default=True))
            self._active_scope = str(self._settings.get("memory", "ui_scope", default="project") or "project")
        else:
            self._enabled = True
            self._active_scope = "project"

        if self._active_scope not in ("project", "global"):
            self._active_scope = "project"
        
        # Register login callback with auth manager (persists across dialog recreations)
        try:
            from src.core.auth_manager import get_auth_manager
            auth = get_auth_manager()
            auth.set_on_login_callback(self._on_login_complete)
        except Exception:
            pass
        
        # Initialize semantic search
        self._semantic_searcher = None
        self._init_semantic_search()
    
    def _init_semantic_search(self):
        """Initialize semantic search for current project."""
        try:
            from src.agent.src.memdir.semanticSearch import get_semantic_searcher
            self._semantic_searcher = get_semantic_searcher(self._project_memory_dir)
            log.info("[MemoryManager] Semantic search initialized")
        except Exception as e:
            log.warning(f"[MemoryManager] Semantic search unavailable: {e}")
            self._semantic_searcher = None

    def _get_scope_dir(self, scope: str) -> str:
        return self._global_memory_dir if scope == "global" else self._project_memory_dir

    def _get_rules_dir(self, scope: str) -> str:
        return self._global_rules_dir if scope == "global" else self._project_rules_dir

    def _get_rules_file(self, scope: str) -> str:
        return os.path.join(self._get_rules_dir(scope), "ide_rules.md")

    def _serialize_state(self) -> str:
        payload = {
            "enabled": self._enabled,
            "activeScope": self._active_scope,
            "scopes": {
                "project": {
                    "name": "Current Project",
                    "projectRoot": self._project_root,
                    "memoryDir": self._project_memory_dir,
                    "rulesDir": self._project_rules_dir,
                    "memories": _load_memories(self._project_memory_dir),
                },
                "global": {
                    "name": "Global",
                    "memoryDir": self._global_memory_dir,
                    "rulesDir": self._global_rules_dir,
                    "memories": _load_memories(self._global_memory_dir),
                },
            },
        }
        return json.dumps(payload)

    def _emit_refresh(self):
        payload = self._serialize_state()
        self.data_changed.emit(payload)
        return payload

    def _remove_from_index(self, memory_dir: str, filename: str):
        index_path = os.path.join(memory_dir, "MEMORY.md")
        if not os.path.exists(index_path):
            return
        try:
            with open(index_path, encoding="utf-8") as handle:
                lines = handle.readlines()
            stem = os.path.splitext(filename)[0]
            new_lines = [line for line in lines if stem not in line and filename not in line]
            with open(index_path, "w", encoding="utf-8") as handle:
                handle.writelines(new_lines)
        except Exception as exc:
            log.warning(f"Failed to update memory index {index_path}: {exc}")

    @pyqtSlot(result=str)
    def loadInitialData(self):
        return self._serialize_state()

    @pyqtSlot(result=str)
    def refresh(self):
        return self._emit_refresh()

    @pyqtSlot(str, result=str)
    def setActiveScope(self, scope: str):
        scope = (scope or "").strip().lower()
        if scope not in ("project", "global"):
            return self._emit_refresh()
        self._active_scope = scope
        if self._settings:
            self._settings.set("memory", "ui_scope", scope)
        return self._emit_refresh()

    @pyqtSlot(str, result=str)
    def openRulesDir(self, scope: str):
        scope = (scope or "").strip().lower()
        if scope == "shared":
            scope = "global"
        if scope not in ("project", "global"):
            scope = self._active_scope

        rules_dir = self._global_rules_dir if scope == "global" else self._project_rules_dir
        try:
            os.makedirs(rules_dir, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(rules_dir))
            if not opened:
                raise RuntimeError("Could not open rules folder in file explorer")
            return json.dumps({"success": True, "scope": scope, "rulesDir": rules_dir})
        except Exception as exc:
            log.error(f"[MemoryManager] Failed to open rules dir '{rules_dir}': {exc}")
            return json.dumps({"error": str(exc), "scope": scope, "rulesDir": rules_dir})

    @pyqtSlot(str, result=str)
    def loadRules(self, scope: str):
        scope = (scope or "").strip().lower()
        if scope == "shared":
            scope = "global"
        if scope not in ("project", "global"):
            scope = self._active_scope
        try:
            rules_dir = self._get_rules_dir(scope)
            os.makedirs(rules_dir, exist_ok=True)
            target = self._get_rules_file(scope)
            content = ""
            if os.path.exists(target):
                with open(target, encoding="utf-8") as handle:
                    content = handle.read()
            return json.dumps(
                {
                    "success": True,
                    "scope": scope,
                    "rulesDir": rules_dir,
                    "filePath": target,
                    "content": content,
                }
            )
        except Exception as exc:
            log.error(f"[MemoryManager] Failed to load rules for scope '{scope}': {exc}")
            return json.dumps({"error": str(exc), "scope": scope})

    @pyqtSlot(str)
    def openExternal(self, url: str):
        """Open URL in system browser (external)."""
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(url))
            log.info(f"[MemoryManager] Opened external URL: {url}")
        except Exception as e:
            log.warning(f"[MemoryManager] Failed to open URL: {e}")

    @pyqtSlot(str, str, result=str)
    def saveRules(self, scope: str, content: str):
        scope = (scope or "").strip().lower()
        if scope == "shared":
            scope = "global"
        if scope not in ("project", "global"):
            scope = self._active_scope
        try:
            rules_dir = self._get_rules_dir(scope)
            os.makedirs(rules_dir, exist_ok=True)
            target = self._get_rules_file(scope)
            normalized = (content or "").rstrip() + "\n"
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(normalized)
            self.toast_requested.emit("success", f"Saved {scope} rules")
            return json.dumps(
                {
                    "success": True,
                    "scope": scope,
                    "rulesDir": rules_dir,
                    "filePath": target,
                }
            )
        except Exception as exc:
            log.error(f"[MemoryManager] Failed to save rules for scope '{scope}': {exc}")
            return json.dumps({"error": str(exc), "scope": scope})

    @pyqtSlot(object, result=str)
    def setMemoryEnabled(self, checked):
        self._enabled = bool(checked)
        if self._settings:
            self._settings.set("memory", "enabled", self._enabled)
        log.info(f"Memory enabled set to {self._enabled}")
        self.toast_requested.emit("success", "Memory generation updated")
        return self._emit_refresh()

    @pyqtSlot(str, str, result=str)
    def deleteMemory(self, scope: str, path: str):
        scope = (scope or "").strip().lower()
        if scope not in ("project", "global"):
            scope = self._active_scope
        memory_dir = self._get_scope_dir(scope)
        name = os.path.basename(path or "")
        try:
            from src.utils.safe_delete import safe_delete
            result = safe_delete(path)
            if not result.get("success"):
                raise OSError(result.get("message", "Delete failed"))
            self._remove_from_index(memory_dir, name)
            self.toast_requested.emit("success", f"Deleted {name}")
        except OSError as exc:
            self.toast_requested.emit("error", f"Delete failed: {exc}")
        return self._emit_refresh()

    @pyqtSlot(str, result=str)
    def clearAll(self, scope: str):
        scope = (scope or "").strip().lower()
        if scope not in ("project", "global"):
            scope = self._active_scope
        memory_dir = self._get_scope_dir(scope)
        errors = []
        from src.utils.safe_delete import safe_delete
        for memory in _load_memories(memory_dir):
            try:
                result = safe_delete(memory["path"])
                if not result.get("success"):
                    raise OSError(result.get("message", "Delete failed"))
            except OSError as exc:
                errors.append(str(exc))

        index_path = os.path.join(memory_dir, "MEMORY.md")
        try:
            if os.path.exists(index_path):
                result = safe_delete(index_path)
                if not result.get("success"):
                    raise OSError(result.get("message", "Delete failed"))
        except OSError as exc:
            errors.append(str(exc))

        if errors:
            self.toast_requested.emit("error", errors[0])
        else:
            self.toast_requested.emit("success", f"Cleared {scope} memories")
        return self._emit_refresh()
    
    @pyqtSlot(str, result=str)
    def semanticSearch(self, query: str):
        """Perform semantic search on memories."""
        query = (query or "").strip()
        if not query:
            return self._emit_refresh()
        
        if not self._semantic_searcher:
            self.toast_requested.emit("error", "Semantic search not available")
            return self._emit_refresh()
        
        try:
            # Perform semantic search
            results = self._semantic_searcher.search_memories(
                query, 
                self._project_memory_dir, 
                top_k=20
            )
            
            # Convert results to dict format for JSON
            search_results = [
                {
                    "path": r.file_path,
                    "filename": r.filename,
                    "name": r.title,
                    "description": r.description,
                    "type": r.memory_type,
                    "similarity_score": r.similarity_score,
                    "content_preview": r.content_preview,
                    "mtime": r.mtime,
                    "age": _age_label(r.mtime),
                    "stale": int((time.time() - r.mtime) / 86400) > 7,
                    "keywords": [part.strip() for part in r.description.split(",") if part.strip()],
                    "body": self._load_file_body(r.file_path),
                }
                for r in results
            ]
            
            log.info(f"[MemoryManager] Semantic search found {len(search_results)} results for '{query[:50]}...'")
            
            # Update scope with search results
            payload = {
                "enabled": self._enabled,
                "activeScope": self._active_scope,
                "searchQuery": query,
                "isSearchMode": True,
                "scopes": {
                    "project": {
                        "name": f"Search: {query[:30]}...",
                        "memoryDir": self._project_memory_dir,
                        "rulesDir": self._project_rules_dir,
                        "memories": search_results,
                    },
                    "global": {
                        "name": "Global",
                        "memoryDir": self._global_memory_dir,
                        "rulesDir": self._global_rules_dir,
                        "memories": [],
                    },
                },
            }
            
            return json.dumps(payload)
            
        except Exception as e:
            log.error(f"[MemoryManager] Semantic search failed: {e}", exc_info=True)
            self.toast_requested.emit("error", f"Semantic search failed: {e}")
            return self._emit_refresh()
    
    def _load_file_body(self, file_path: str) -> str:
        """Load file body content."""
        try:
            with open(file_path, encoding="utf-8") as handle:
                raw = handle.read()
            _, body = _parse_frontmatter(raw)
            return body[:500]  # Limit preview to 500 chars
        except Exception:
            return ""
    
    @pyqtSlot(result=str)
    def exitSearchMode(self):
        """Exit search mode and return to normal view."""
        return self._emit_refresh()
    
    @pyqtSlot(str, result=str)
    def getMemoryStats(self, scope: str):
        """Get memory statistics for dashboard."""
        scope = (scope or "").strip().lower()
        if scope not in ("project", "global"):
            scope = self._active_scope
        memory_dir = self._get_scope_dir(scope)
        
        memories = _load_memories(memory_dir)
        
        # Calculate stats
        type_counts = {}
        total_size = 0
        oldest_mtime = time.time()
        newest_mtime = 0
        
        for mem in memories:
            mem_type = mem.get("type", "unknown")
            type_counts[mem_type] = type_counts.get(mem_type, 0) + 1
            
            try:
                file_size = os.path.getsize(mem["path"])
                total_size += file_size
            except Exception:
                pass
            
            mtime = mem.get("mtime", 0)
            if mtime < oldest_mtime:
                oldest_mtime = mtime
            if mtime > newest_mtime:
                newest_mtime = mtime
        
        stats = {
            "total": len(memories),
            "type_counts": type_counts,
            "total_size_kb": round(total_size / 1024, 2),
            "oldest_age": _age_label(oldest_mtime) if oldest_mtime < time.time() else "N/A",
            "newest_age": _age_label(newest_mtime) if newest_mtime > 0 else "N/A",
            "stale_count": sum(1 for m in memories if m.get("stale", False)),
            "fresh_count": sum(1 for m in memories if not m.get("stale", False)),
        }
        
        return json.dumps(stats)
    
    @pyqtSlot(str, bool, result=str)
    def runConsolidation(self, scope: str, auto_merge: bool = False):
        """Run memory consolidation and deduplication."""
        scope = (scope or "").strip().lower()
        if scope not in ("project", "global"):
            scope = self._active_scope
        memory_dir = self._get_scope_dir(scope)
        
        if not os.path.isdir(memory_dir):
            self.toast_requested.emit("error", "Memory directory not found")
            return json.dumps({"error": "Memory directory not found"})
        
        try:
            log.info(f"[MemoryManager] Running consolidation on {memory_dir} (auto_merge={auto_merge})")
            self.toast_requested.emit("info", "Starting memory consolidation...")
            
            from src.agent.src.memdir.memoryConsolidation import MemoryConsolidator
            
            consolidator = MemoryConsolidator(memory_dir)
            report = consolidator.run_consolidation(auto_merge=auto_merge)
            
            # Convert report to JSON-serializable dict
            report_dict = {
                "total_memories_scanned": report.total_memories_scanned,
                "duplicates_found": report.duplicates_found,
                "memories_merged": report.memories_merged,
                "memories_deleted": report.memories_deleted,
                "space_saved_bytes": report.space_saved_bytes,
                "space_saved_kb": round(report.space_saved_bytes / 1024, 2),
                "timestamp": report.timestamp,
                "clusters": [
                    {
                        "cluster_id": c.cluster_id,
                        "memory_count": len(c.memories),
                        "recommended_action": c.recommended_action,
                        "memories": [
                            {
                                "filename": m.get("filename", ""),
                                "title": m.get("title", ""),
                                "file_path": m.get("file_path", ""),
                            }
                            for m in c.memories
                        ],
                    }
                    for c in report.clusters
                ],
            }
            
            log.info(f"[MemoryManager] Consolidation complete: {report.duplicates_found} duplicates found, {report.memories_merged} merged")
            
            if report.duplicates_found > 0:
                action_msg = f"Found {report.duplicates_found} duplicate groups"
                if auto_merge:
                    action_msg += f", merged {report.memories_merged} memories"
                self.toast_requested.emit("success", action_msg)
            else:
                self.toast_requested.emit("success", "No duplicates found - memories are clean!")
            
            # Refresh memory list after consolidation
            return json.dumps(report_dict)
            
        except Exception as e:
            log.error(f"[MemoryManager] Consolidation failed: {e}", exc_info=True)
            self.toast_requested.emit("error", f"Consolidation failed: {e}")
            return json.dumps({"error": str(e)})
    
    @pyqtSlot(result=str)
    def getGlobalMemories(self):
        """Get all global (cross-project) memories."""
        if not HAS_CROSS_PROJECT:
            return json.dumps({"error": "Cross-project memory not available"})
        
        try:
            manager = get_cross_project_manager()
            memories = manager.load_global_memories()
            
            memories_list = [
                {
                    "filename": m.filename,
                    "title": m.title,
                    "description": m.description,
                    "memory_type": m.memory_type,
                    "mtime": m.mtime,
                    "age": _age_label(m.mtime),
                    "content_preview": m.content[:300],
                }
                for m in memories
            ]
            
            return json.dumps({"memories": memories_list})
            
        except Exception as e:
            log.error(f"[MemoryManager] Failed to load global memories: {e}")
            return json.dumps({"error": str(e)})
    
    @pyqtSlot(str, str, str, result=str)
    def saveGlobalMemory(self, filename: str, title: str, content: str):
        """Save a memory to global (cross-project) scope."""
        if not HAS_CROSS_PROJECT:
            return json.dumps({"error": "Cross-project memory not available"})
        
        try:
            manager = get_cross_project_manager()
            
            metadata = {
                "title": title,
                "type": "user_preference",
                "created": datetime.now().isoformat(),
            }
            
            file_path = manager.save_global_memory(filename, content, metadata)
            
            log.info(f"[MemoryManager] Saved global memory: {filename}")
            self.toast_requested.emit("success", f"Saved global memory: {title}")
            
            return json.dumps({"success": True, "file_path": file_path})
            
        except Exception as e:
            log.error(f"[MemoryManager] Failed to save global memory: {e}")
            return json.dumps({"error": str(e)})
    
    @pyqtSlot(str, result=str)
    def deleteGlobalMemory(self, filename: str):
        """Delete a global memory."""
        if not HAS_CROSS_PROJECT:
            return json.dumps({"error": "Cross-project memory not available"})
        
        try:
            manager = get_cross_project_manager()
            success = manager.delete_global_memory(filename)
            
            if success:
                self.toast_requested.emit("success", f"Deleted global memory: {filename}")
                return json.dumps({"success": True})
            else:
                return json.dumps({"error": "Memory not found"})
            
        except Exception as e:
            log.error(f"[MemoryManager] Failed to delete global memory: {e}")
            return json.dumps({"error": str(e)})
    
    @pyqtSlot(str, bool, result=str)
    def syncGlobalMemoriesToProject(self, project_root: str, auto_merge: bool = True):
        """Sync global memories to a project."""
        if not HAS_CROSS_PROJECT:
            return json.dumps({"error": "Cross-project memory not available"})
        
        try:
            manager = get_cross_project_manager()
            report = manager.sync_memories_to_project(project_root, auto_merge)
            
            report_dict = {
                "global_memories_loaded": report.global_memories_loaded,
                "project_memories_loaded": report.project_memories_loaded,
                "conflicts_resolved": report.conflicts_resolved,
                "merged_memory_path": report.merged_memory_path,
            }
            
            log.info(f"[MemoryManager] Synced global memories to project: {project_root}")
            self.toast_requested.emit(
                "success",
                f"Synced {report.global_memories_loaded} global memories"
            )
            
            return json.dumps(report_dict)
            
        except Exception as e:
            log.error(f"[MemoryManager] Failed to sync global memories: {e}")
            return json.dumps({"error": str(e)})
    
    @pyqtSlot(str, result=str)
    def promoteToGlobal(self, memory_path: str):
        """Promote a project memory to global scope."""
        if not HAS_CROSS_PROJECT:
            return json.dumps({"error": "Cross-project memory not available"})
        
        try:
            manager = get_cross_project_manager()
            
            # Extract filename from path
            filename = os.path.basename(memory_path)
            project_root = self._project_memory_dir
            
            # Find project root from memory dir
            if project_root.endswith("/memory") or project_root.endswith("\\memory"):
                project_root = project_root.rsplit(os.sep + "memory", 1)[0]
            
            result = manager.share_project_memory(project_root, filename, promote_to_global=True)
            
            if result:
                self.toast_requested.emit("success", f"Promoted {filename} to global memory")
                return json.dumps({"success": True, "global_path": result})
            else:
                return json.dumps({"error": "Failed to promote memory"})
            
        except Exception as e:
            log.error(f"[MemoryManager] Failed to promote memory: {e}")
            return json.dumps({"error": str(e)})

    # ═══════════════════════════════════════════════════════════════
    # SETTINGS BRIDGE (JS calls these for settings persistence)
    # ═══════════════════════════════════════════════════════════════

    @pyqtSlot(result=str)
    def getState(self) -> str:
        """Alias for loadInitialData — JS calls bridge.getState()."""
        return self.loadInitialData()

    # API key fields that should be stored in KeyManager (encrypted)
    _API_KEY_FIELDS = {
        "ai.openai_key":       "openai",
        "ai.deepseek_key":     "deepseek",
        "ai.mistral_key":      "mistral",
        "ai.mimo_key":         "mimo",
        "ai.openrouter_key":   "openrouter",
        "ai.alibaba_key":      "alibaba",
        "ai.kimi_key":         "kimi",
        "ai.siliconflow_key":  "siliconflow",
    }

    @pyqtSlot(str, str)
    def setSetting(self, key: str, value: str):
        """Persist a single setting by dotted path (e.g. 'editor.font_size').

        API key fields (ai.*_key) are stored encrypted via KeyManager instead
        of plain JSON.  All other settings go to ~/.cortex/settings.json.
        """
        try:
            if self._settings:
                # Parse dotted path into section / setting_key
                if "." in key:
                    section, setting_key = key.split(".", 1)
                else:
                    section, setting_key = "ui", key

                # ── API Key: store encrypted via KeyManager ──
                km_provider = self._API_KEY_FIELDS.get(key)
                if km_provider is not None:
                    self._store_api_key(km_provider, value)
                    # Also store a placeholder in settings (so UI knows a key exists)
                    self._settings.set(section, setting_key, "***")
                    log.info(f"[MemoryManager] API key for {km_provider} stored in KeyManager")
                    return

                # Coerce value to the right Python type
                low = value.lower()
                if low in ("true", "false"):
                    coerced: object = low == "true"
                else:
                    try:
                        coerced = int(value)
                    except (ValueError, TypeError):
                        try:
                            coerced = float(value)
                        except (ValueError, TypeError):
                            coerced = value

                self._settings.set(section, setting_key, coerced)
                log.info(f"[MemoryManager] setSetting({section}.{setting_key} = {coerced!r})")
        except Exception as e:
            log.warning(f"[MemoryManager] setSetting failed: {e}")

    @pyqtSlot(str, str)
    def setDefaultModel(self, model_id: str, model_label: str):
        """Update the default model and notify the chat panel to sync its button."""
        try:
            if self._settings:
                self._settings.set("ai", "model", model_id)
                self._settings.set("ai", "model_label", model_label)
            self.model_changed.emit(model_id, model_label)
            log.info(f"[MemoryManager] Default model changed to: {model_id} ({model_label})")
        except Exception as e:
            log.warning(f"[MemoryManager] setDefaultModel failed: {e}")

    # ── Profile & Usage Bridge Methods ────────────────────────────

    @pyqtSlot(result=str)
    def getProfile(self) -> str:
        """Return profile data as JSON string. Merges server + local data."""
        try:
            from src.ai.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            local_profile = tracker.get_profile()
            
            # Try to get server profile
            try:
                from src.core.cortex_api import get_api_client
                api = get_api_client()
                if api.is_logged_in():
                    server_profile = api.get_profile()
                    if server_profile:
                        # Merge server data into local
                        local_profile["profile"]["display_name"] = server_profile.get("display_name", local_profile["profile"].get("display_name"))
                        local_profile["profile"]["email"] = server_profile.get("email", local_profile["profile"].get("email"))
                        local_profile["auth"]["logged_in"] = True
                        local_profile["auth"]["email"] = server_profile.get("email")
                        local_profile["auth"]["has_subscription"] = server_profile.get("has_subscription", False)
                        local_profile["auth"]["plan"] = server_profile.get("plan")
                        local_profile["auth"]["plan_display"] = server_profile.get("plan_display")
                        local_profile["auth"]["subscription_status"] = server_profile.get("subscription_status")
            except Exception as e:
                log.debug(f"[MemoryManager] Server profile fetch failed (offline?): {e}")
            
            return json.dumps(local_profile)
        except Exception as e:
            log.warning(f"[MemoryManager] getProfile failed: {e}")
            return "{}"

    @pyqtSlot(result=str)
    def getUsageStats(self) -> str:
        """Return usage stats as JSON string. Merges server + local data."""
        try:
            from src.ai.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            local_stats = tracker.get_usage_stats()
            
            # Only fetch server data if logged in
            from src.core.cortex_api import get_api_client
            api = get_api_client()
            if api.is_logged_in():
                try:
                    server_usage = api.get_usage_summary()
                    if server_usage:
                        local_stats["server"] = {
                            "subscription": server_usage.get("subscription", {}),
                            "credits": server_usage.get("credits", {}),
                            "usage": server_usage.get("usage", {}),
                        }
                except Exception as e:
                    log.debug(f"[MemoryManager] Server usage fetch failed: {e}")
            else:
                # Not logged in - clear any cached server data
                local_stats.pop("server", None)
            
            return json.dumps(local_stats)
        except Exception as e:
            log.warning(f"[MemoryManager] getUsageStats failed: {e}")
            return "{}"

    @pyqtSlot(str)
    def setProfile(self, data_json: str):
        """Update profile fields from JSON. Syncs to server if logged in."""
        try:
            from src.ai.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            data = json.loads(data_json)
            tracker.update_profile_bulk(data)
            log.info(f"[MemoryManager] Profile updated: {list(data.keys())}")
            
            # Sync to server if logged in
            try:
                from src.core.cortex_api import get_api_client
                api = get_api_client()
                if api.is_logged_in():
                    server_data = {}
                    if "display_name" in data:
                        server_data["display_name"] = data["display_name"]
                    if "email" in data:
                        server_data["email"] = data["email"]
                    if server_data:
                        api.update_profile(server_data)
            except Exception as e:
                log.debug(f"[MemoryManager] Server profile sync failed: {e}")
        except Exception as e:
            log.warning(f"[MemoryManager] setProfile failed: {e}")

    # ── Auth Bridge Methods ───────────────────────────────────────

    @pyqtSlot(result=str)
    def getAuthStatus(self) -> str:
        """Return auth status as JSON string. Fetches fresh user data from server."""
        try:
            from src.core.cortex_api import get_api_client
            api = get_api_client()
            
            user_data = api.user_info or {}
            
            # If logged in, fetch fresh profile from server to get plan details
            if api.is_logged_in():
                try:
                    server_profile = api.get_profile()
                    if server_profile:
                        # Merge server data into user_data (server has plan info)
                        user_data.update(server_profile)
                except Exception:
                    pass
            
            return json.dumps({
                "logged_in": api.is_logged_in(),
                "user": user_data,
                "server_url": api.base_url,
            })
        except Exception as e:
            log.warning(f"[MemoryManager] getAuthStatus failed: {e}")
            return json.dumps({"logged_in": False, "error": str(e)})

    @pyqtSlot(result=bool)
    def startLogin(self) -> bool:
        """Start OAuth2 login flow. Opens browser."""
        try:
            from src.core.auth_manager import get_auth_manager
            auth = get_auth_manager()
            auth.set_on_login_callback(self._on_login_complete)
            return auth.start_login(use_browser=True)
        except Exception as e:
            log.error(f"[MemoryManager] startLogin failed: {e}")
            return False

    @pyqtSlot(str, str, result=bool)
    def loginWithCredentials(self, email: str, password: str) -> bool:
        """Direct login with email + password."""
        try:
            from src.core.auth_manager import get_auth_manager
            auth = get_auth_manager()
            auth.set_on_login_callback(self._on_login_complete)
            return auth.login_with_credentials(email, password)
        except Exception as e:
            log.error(f"[MemoryManager] loginWithCredentials failed: {e}")
            return False

    @pyqtSlot(result=bool)
    def logout(self) -> bool:
        """Logout and clear tokens."""
        try:
            from src.core.auth_manager import get_auth_manager
            auth = get_auth_manager()
            auth.logout()
            return True
        except Exception as e:
            log.error(f"[MemoryManager] logout failed: {e}")
            return False

    def _on_login_complete(self, user_info):
        """Callback when login completes — emit signal for main thread UI update."""
        log.info(f"[MemoryManager] Login complete: {user_info}")
        # Emit signal to safely update UI on main thread
        self.login_complete.emit(user_info)

    @pyqtSlot(str, str, str, result=str)
    def getUsageForRange(self, start_date: str, end_date: str, granularity: str) -> str:
        """Return usage data for a date range (daily/weekly/cumulative)."""
        try:
            from src.ai.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            data = tracker.get_usage_for_range(start_date, end_date, granularity)
            return json.dumps(data)
        except Exception as e:
            log.warning(f"[MemoryManager] getUsageForRange failed: {e}")
            return json.dumps({"error": str(e)})

    def _store_api_key(self, provider: str, api_key: str):
        """Store an API key in KeyManager (encrypted) and hot-reload the provider."""
        try:
            # Sanitize the key before storing (strip null bytes, spaces, quotes)
            if api_key:
                api_key = api_key.replace('\x00', '').replace('\u0000', '').replace(' ', '').replace('\n', '').replace('\r', '').strip().strip("'\"")
            from src.core.key_manager import get_key_manager
            km = get_key_manager()
            success = km.store_key(provider, api_key)
            if success:
                log.info(f"[MemoryManager] Encrypted key stored for {provider}")
                # Hot-reload: update the live provider instance
                self._reload_provider_key(provider, api_key)
            else:
                log.warning(f"[MemoryManager] Failed to store key for {provider}")
        except Exception as e:
            log.warning(f"[MemoryManager] _store_api_key error: {e}")

    def _reload_provider_key(self, provider: str, api_key: str):
        """Push new API key into the live provider instance (no restart needed)."""
        try:
            from src.ai.providers import get_provider_registry, ProviderType
            registry = get_provider_registry()
            provider_map = {
                "openai":     ProviderType.OPENAI,
                "deepseek":   ProviderType.DEEPSEEK,
                "mistral":    ProviderType.MISTRAL,
                "mimo":       ProviderType.MIMO,
                "openrouter": ProviderType.OPENROUTER,
                "alibaba":    ProviderType.ALIBABA,
                "siliconflow":ProviderType.SILICONFLOW,
            }
            pt = provider_map.get(provider)
            if pt:
                p = registry.get_provider(pt)
                if p and hasattr(p, 'set_api_key'):
                    p.set_api_key(api_key)
                    log.info(f"[MemoryManager] Hot-reloaded key for {provider}")
        except Exception as e:
            log.debug(f"[MemoryManager] Hot-reload skipped: {e}")

    @pyqtSlot(str, result=str)
    def getApiKey(self, provider: str) -> str:
        """Get a stored API key (masked for display). Returns empty string if not found."""
        try:
            from src.core.key_manager import get_key_manager
            km = get_key_manager()
            key = km.get_key(provider)
            if key:
                return key
            return ""
        except Exception as e:
            log.warning(f"[MemoryManager] getApiKey error: {e}")
            return ""

    @pyqtSlot(str, str, result=bool)
    def setApiKey(self, provider: str, api_key: str) -> bool:
        """Store an API key securely."""
        try:
            self._store_api_key(provider, api_key)
            return True
        except Exception as e:
            log.warning(f"[MemoryManager] setApiKey error: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def removeApiKey(self, provider: str) -> bool:
        """Remove a stored API key."""
        log.info(f"[MemoryManager] removeApiKey called for {provider}")
        try:
            from src.core.key_manager import get_key_manager
            km = get_key_manager()
            
            # Check if key exists first
            existing_key = km.get_key(provider)
            log.info(f"[MemoryManager] Key exists for {provider}: {bool(existing_key)}")
            
            success = km.delete_key(provider)
            log.info(f"[MemoryManager] delete_key result for {provider}: {success}")
            
            if success:
                # Also clear from live provider
                self._reload_provider_key(provider, "")
                
                # Clear settings placeholder
                settings_map = {
                    "mimo": ("ai", "mimo_key"),
                    "deepseek": ("ai", "deepseek_key"),
                    "openai": ("ai", "openai_key"),
                    "openrouter": ("ai", "openrouter_key"),
                    "alibaba": ("ai", "alibaba_key"),
                    "siliconflow": ("ai", "siliconflow_key"),
                    "mistral": ("ai", "mistral_key"),
                }
                if provider in settings_map and self._settings:
                    section, key = settings_map[provider]
                    self._settings.set(section, key, "")
                    log.info(f"[MemoryManager] Cleared settings for {provider}")
                
                log.info(f"[MemoryManager] Successfully removed key for {provider}")
            else:
                log.warning(f"[MemoryManager] delete_key returned False for {provider}")
            
            return success
        except Exception as e:
            log.error(f"[MemoryManager] removeApiKey error for {provider}: {e}")
            return False

    @pyqtSlot(str, result=str)
    def testApiKey(self, provider: str) -> str:
        """Test if a stored API key works. Returns JSON {success: bool, error: str}."""
        log.info(f"[MemoryManager] testApiKey called for {provider}")
        try:
            from src.core.key_manager import get_key_manager
            from src.ai.providers import get_provider_registry, ProviderType
            
            km = get_key_manager()
            key = km.get_key(provider)
            if not key:
                log.info(f"[MemoryManager] testApiKey: no key for {provider}")
                return json.dumps({"success": False, "error": "No key stored"})
            
            # Sanitize the key (strip null bytes, spaces, quotes)
            if key:
                key = key.replace('\x00', '').replace('\u0000', '').replace(' ', '').replace('\n', '').replace('\r', '').strip().strip("'\"")
            
            log.info(f"[MemoryManager] testApiKey: key prefix={repr(key[:8])}, len={len(key)}")
            
            # Try to validate with the provider
            registry = get_provider_registry()
            provider_map = {
                "openai":     ProviderType.OPENAI,
                "deepseek":   ProviderType.DEEPSEEK,
                "mistral":    ProviderType.MISTRAL,
                "mimo":       ProviderType.MIMO,
                "openrouter": ProviderType.OPENROUTER,
                "alibaba":    ProviderType.ALIBABA,
                "siliconflow":ProviderType.SILICONFLOW,
            }
            pt = provider_map.get(provider)
            if pt:
                p = registry.get_provider(pt)
                if p and hasattr(p, 'validate_api_key'):
                    # Use set_api_key() to properly re-detect endpoints (important for MiMo tp-/sk- routing)
                    old_key = getattr(p, '_api_key', None)
                    try:
                        if hasattr(p, 'set_api_key'):
                            p.set_api_key(key)
                        else:
                            p._api_key = key
                        valid = p.validate_api_key()
                        log.info(f"[MemoryManager] testApiKey: validate_api_key returned {valid} for {provider}")
                        if valid:
                            return json.dumps({"success": True, "error": ""})
                        else:
                            return json.dumps({"success": False, "error": "Key validation failed"})
                    finally:
                        # Restore old key
                        if hasattr(p, 'set_api_key'):
                            p.set_api_key(old_key or '')
                        else:
                            p._api_key = old_key
            
            # If no validate method, just check key length
            if len(key) > 8:
                return json.dumps({"success": True, "error": ""})
            return json.dumps({"success": False, "error": "Key too short"})
            
        except Exception as e:
            log.warning(f"[MemoryManager] testApiKey error: {e}")
            return json.dumps({"success": False, "error": str(e)})

    @pyqtSlot(str, result=str)
    def getSetting(self, key: str) -> str:
        """Read a single setting value by dotted path (e.g. 'editor.font_size')."""
        try:
            if self._settings:
                if "." in key:
                    parts = key.split(".")
                    val = self._settings.get(*parts, default="")
                else:
                    val = self._settings.get("ui", key, default="")
                return str(val) if val is not None else ""
        except Exception:
            pass
        return ""

    @pyqtSlot(result=str)
    def getSettings(self) -> str:
        """Return ALL settings from ALL sections as nested JSON dict.

        API keys are loaded from KeyManager (decrypted) and injected into
        the ai section so the settings page can display them.
        """
        try:
            if self._settings:
                data = self._settings.all()
                # Inject API keys from KeyManager into the ai section
                ai = data.get("ai", {})
                if isinstance(ai, dict):
                    for settings_key, km_name in self._API_KEY_FIELDS.items():
                        _, field = settings_key.split(".", 1)
                        key = self._load_api_key(km_name)
                        if key:
                            ai[field] = key
                        else:
                            # Clear placeholder if no real key exists
                            if ai.get(field) == "***":
                                ai[field] = ""
                    data["ai"] = ai
                return json.dumps(data)
        except Exception:
            pass
        return "{}"

    def _load_api_key(self, provider: str) -> str:
        """Load an API key from KeyManager (decrypted)."""
        try:
            from src.core.key_manager import get_key_manager
            km = get_key_manager()
            key = km.get_key(provider)
            return key or ""
        except Exception:
            return ""

    @pyqtSlot()
    def onSettingsClosed(self):
        """Called when user clicks 'Back to app' — close the dialog."""
        log.info("[MemoryManager] onSettingsClosed — closing dialog")
        try:
            # Close the dialog (self is the bridge, need to find the dialog)
            dialog = self.parent()
            if dialog and hasattr(dialog, 'close'):
                dialog.close()
        except Exception as e:
            log.warning(f"[MemoryManager] onSettingsClosed error: {e}")


class MemoryManagerDialog(QDialog):
    """WebEngine-backed memory manager dialog."""

    def __init__(self, project_root: str, settings=None, parent=None):
        super().__init__(parent)
        self._bridge = MemoryManagerBridge(project_root, settings=settings, parent=self)
        self._page_loaded = False

        self.setWindowTitle("Memory Manager - Cortex IDE")
        self.setMinimumSize(980, 720)
        self.resize(1120, 780)

        self._build_ui()

    def _build_ui(self):
        log.debug("[MemoryManager] _build_ui START")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._view = QWebEngineView(self)
        self._page = _MemoryPage(self._view)
        self._view.setPage(self._page)
        log.debug("[MemoryManager] QWebEngineView + _MemoryPage created")

        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)

        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", self._bridge)
        self._channel.registerObject("memoryBridge", self._bridge)
        self._bind_web_channel()

        self._view.loadFinished.connect(self._on_page_loaded)
        self._bridge.data_changed.connect(self._push_state_to_page)
        self._bridge.toast_requested.connect(self._show_toast)
        self._bridge.login_complete.connect(self._handle_login_complete)

        layout.addWidget(self._view)
        self._load_page()

        # Safety net: if page doesn't load within 3s, force HTML fallback
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(3000, self._safety_fallback)

    def _safety_fallback(self):
        """If page never loaded, inject HTML directly."""
        if self._page_loaded:
            return
        log.warning("[MemoryManager] Safety fallback triggered — page did not load in 3s")
        self._load_html_fallback()

    def _handle_login_complete(self, user_info):
        """Handle login complete on main thread — bring window to focus and refresh UI."""
        log.info(f"[MemoryManager] _handle_login_complete called on main thread")
        try:
            from PyQt6.QtWidgets import QApplication
            # Get the main window
            main_window = None
            for w in QApplication.topLevelWidgets():
                if w.__class__.__name__ == "MainWindow":
                    main_window = w
                    break
            if main_window is None:
                main_window = self.window()

            if main_window:
                main_window.showNormal()
                main_window.raise_()
                main_window.activateWindow()
                # Windows-specific: force foreground
                try:
                    import ctypes
                    hwnd = int(main_window.winId())
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

            # Refresh the webview to show logged-in state
            if self._view and self._page_loaded:
                self._view.page().runJavaScript("if(typeof loadProfile==='function') loadProfile();")
                self._view.page().runJavaScript("if(typeof loadUsageStats==='function') loadUsageStats();")
                log.info("[MemoryManager] Triggered UI refresh after login")
            else:
                log.warning(f"[MemoryManager] Cannot refresh UI: _view={self._view}, _page_loaded={self._page_loaded}")
        except Exception as e:
            log.error(f"[MemoryManager] _handle_login_complete error: {e}")

    def _load_page(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "html" / "memory_manager" / "memory_management.html"
        )
        log.debug(f"[MemoryManager] _load_page: html_path={html_path} exists={html_path.exists()}")
        if not html_path.exists():
            QMessageBox.critical(self, "Memory Manager", f"Missing UI file:\n{html_path}")
            return

        try:
            # Read HTML content for fallback
            with open(html_path, "r", encoding="utf-8") as f:
                self._html_content = f.read()

            url = QUrl.fromLocalFile(str(html_path))
            url.setQuery(f"v={int(time.time())}")
            log.debug(f"[MemoryManager] Loading URL: {url.toString()}")
            self._view.setUrl(url)
        except Exception as e:
            log.error(f"[MemoryManager] _load_page error: {e}")
            # Fallback: load HTML directly
            self._load_html_fallback()

    def _load_html_fallback(self):
        """Fallback: load HTML content directly via setHtml."""
        log.debug("[MemoryManager] Using setHtml fallback")
        html_path = (
            Path(__file__).resolve().parent.parent / "html" / "memory_manager" / "memory_management.html"
        )
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        # Inject base tag so relative paths (CSS/JS) resolve
        base_dir = html_path.parent
        base_url = QUrl.fromLocalFile(str(base_dir)).toString()
        if "<head>" in html:
            html = html.replace("<head>", f'<head>\n  <base href="{base_url}/">', 1)
        self._view.setHtml(html)

    def _on_page_loaded(self, ok: bool):
        self._page_loaded = ok
        log.debug(f"[MemoryManager] Page loaded ok={ok}")
        if not ok:
            QMessageBox.warning(self, "Memory Manager", "Failed to load memory management page.")
            return
        # Re-apply channel binding after load to avoid intermittent transport injection races.
        self._bind_web_channel()
        initial = self._bridge.loadInitialData()
        log.debug(f"[MemoryManager] Initial data length={len(initial)} first200={initial[:200]}")

        # Use a delayed push to ensure JS has fully initialized.
        # Multiple retries ensure the page renders even if scripts load slowly.
        from PyQt6.QtCore import QTimer
        for delay_ms in (200, 600, 1500, 3000):
            QTimer.singleShot(delay_ms, lambda d=delay_ms: self._try_push_state(initial, d))

    def _bind_web_channel(self):
        try:
            # Use the same stable pattern as the working AI chat webview.
            self._page.setWebChannel(self._channel)
        except TypeError:
            # Fallback for builds that only accept setWebChannel(channel).
            self._page.setWebChannel(self._channel)
        except Exception as exc:
            log.warning(f"[MemoryManager] Failed to bind web channel: {exc}")

    def _try_push_state(self, payload: str, delay_ms: int):
        """Push state to page — always call _push_state_to_page which handles fallback."""
        if not self._page_loaded:
            return
        def _check_and_push(func_type):
            log.debug(f"[MemoryManager] _try_push_state(delay={delay_ms}ms) receiveMemoryState type={func_type}")
            # Always push — _push_state_to_page has its own fallback renderer
            self._push_state_to_page(payload)
        self._view.page().runJavaScript(
            "typeof window.receiveMemoryState",
            _check_and_push,
        )

    def _push_state_to_page(self, payload: str):
        log.debug(f"[MemoryManager] _push_state_to_page called, page_loaded={self._page_loaded}, payload_len={len(payload)}")
        if not self._page_loaded:
            log.warning("[MemoryManager] _push_state_to_page skipped: page not loaded")
            return
        # Build JS that:
        # 1. Tries the real receiveMemoryState render first
        # 2. ALWAYS injects a visible fallback container as safety net
        # The JSON payload is injected directly (it's already valid JS object notation).
        js_code = (
            "(function(){\n"
            f"  var _s = {payload};\n"
            "  var _rendered = false;\n"
            "  /* Step 1: try the real renderer */\n"
            "  try {\n"
            "    if (typeof window.receiveMemoryState === 'function') {\n"
            "      window.receiveMemoryState(_s);\n"
            "      /* Check if real renderer actually populated the list */\n"
            "      var lv = document.getElementById('listView');\n"
            "      if (lv && lv.children.length > 0) { _rendered = true; }\n"
            "    }\n"
            "  } catch(e) { console.error('[MEMORY] render error:', e); }\n"
            "  /* Step 2: always inject fallback into a safety container */\n"
            "  var _scope = (_s.scopes || {})[_s.activeScope] || (_s.scopes || {}).project || {};\n"
            "  var _mems = Array.isArray(_scope.memories) ? _scope.memories : [];\n"
            "  var _fb = document.getElementById('mm-python-fallback');\n"
            "  if (!_fb) {\n"
            "    _fb = document.createElement('div');\n"
            "    _fb.id = 'mm-python-fallback';\n"
            "    _fb.style.cssText = 'padding:24px;font-family:sans-serif;color:#ccc;min-height:200px;';\n"
            "    document.body.appendChild(_fb);\n"
            "  }\n"
            "  var _h = '<div style=\"padding:16px 0;\">';\n"
            "  _h += '<p style=\"color:#888;margin-bottom:12px;\">Scope: <strong style=\"color:#fff;\">' + (_scope.name || 'N/A') + '</strong> \\u2022 ' + _mems.length + ' memor' + (_mems.length === 1 ? 'y' : 'ies') + '</p>';\n"
            "  if (_scope.memoryDir) _h += '<p style=\"color:#666;font-size:12px;margin-bottom:16px;\"><code>' + _scope.memoryDir + '</code></p>';\n"
            "  _mems.forEach(function(m){\n"
            "    _h += '<div style=\"border:1px solid #444;border-radius:6px;padding:12px;margin:8px 0;background:#2d2d2d;\">';\n"
            "    _h += '<strong style=\"color:#fff;\">' + (m.name || m.filename || 'unnamed') + '</strong>';\n"
            "    _h += ' <span style=\"color:#888;\">(' + (m.type || 'general') + ')</span>';\n"
            "    if (m.age) _h += ' <span style=\"color:#666;font-size:11px;\">\\u2022 ' + m.age + '</span>';\n"
            "    if (m.description) _h += '<p style=\"color:#aaa;margin:4px 0 0;\">' + m.description + '</p>';\n"
            "    _h += '</div>';\n"
            "  });\n"
            "  if (_mems.length === 0) _h += '<p style=\"color:#888;\">No memories saved yet. The agent will populate this space as it learns.</p>';\n"
            "  _h += '</div>';\n"
            "  _fb.innerHTML = _h;\n"
            "  /* Hide fallback if real renderer worked */\n"
            "  if (_rendered) { _fb.style.display = 'none'; }\n"
            "  return _rendered ? 'ok' : 'fallback';\n"
            "})()"
        )
        log.debug(f"[MemoryManager] Running JS code length={len(js_code)}")
        self._view.page().runJavaScript(
            js_code,
            lambda result: log.debug(f"[MemoryManager] JS result: {result}"),
        )

    def _show_toast(self, level: str, message: str):
        if not self._page_loaded:
            return
        safe_level = json.dumps(level)
        safe_message = json.dumps(message)
        self._view.page().runJavaScript(
            f"window.showToast && window.showToast({safe_level}, {safe_message});"
        )
