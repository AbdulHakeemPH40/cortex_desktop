"""
Theme Manager for Cortex AI Agent IDE
Handles dark QSS stylesheet loading — dark mode only.
"""

import logging
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger("cortex.theme_manager")


THEMES_DIR = Path(__file__).parent.parent / "ui" / "themes"


class ThemeManager(QObject):
    theme_changed = pyqtSignal(str)  # Always 'dark'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current = "dark"

    def apply(self, theme_name: str = "dark", app: QApplication = None):
        """Load and apply the dark QSS theme."""
        self._current = "dark"
        qss_file = THEMES_DIR / "dark.qss"

        if not qss_file.exists():
            log.error(f"[ThemeManager] Theme file not found: {qss_file}")
            return

        with open(qss_file, "r", encoding="utf-8") as f:
            stylesheet = f.read()

        target = app or QApplication.instance()
        if target:
            target.setStyleSheet(stylesheet)
            self.theme_changed.emit("dark")

    def toggle(self, app: QApplication = None):
        """Always stay dark — no toggle."""
        self.apply("dark", app)
        return "dark"

    @property
    def current(self) -> str:
        return "dark"

    @property
    def is_dark(self) -> bool:
        return True


# Singleton
_theme_manager = None


def get_theme_manager() -> ThemeManager:
    global _theme_manager
    if _theme_manager is None:
        _theme_manager = ThemeManager()
    return _theme_manager
