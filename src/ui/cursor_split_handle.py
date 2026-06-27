"""
CursorSplitHandle + CursorSplitter - Ultra-lightweight custom splitter handle.

Drop-in replacement for QSplitter: use CursorSplitter instead.

PERFORMANCE CRITICAL:
  - sizeHint() is CONSTANT (never changes on hover) -- prevents relayout storms
  - NO enterEvent/leaveEvent/mousePress/mouseRelease -- zero repaints on hover
  - NO setMouseTracking -- no mouse event processing at all
  - No Antialiasing -- straight line drawing only, no QPainterPath
  - No objects allocated in paintEvent -- all pre-created at module level
  - Paint event draws only static lines -- nothing changes on hover/press
"""

from PyQt6.QtWidgets import QSplitter, QSplitterHandle
from PyQt6.QtGui import QPainter, QColor, QPen
from PyQt6.QtCore import Qt, QSize


# Single pre-created pen -- zero allocation during paint
_PEN_LINE = QPen(QColor("#3c3c3c"), 1)

_HANDLE_WIDTH = 6


class CursorSplitter(QSplitter):
    """QSplitter subclass that creates CursorSplitHandle for all handles."""

    def createHandle(self):
        return CursorSplitHandle(self.orientation(), self)


class CursorSplitHandle(QSplitterHandle):
    """A splitter handle with a single visible groove line.

    ULTRA-LIGHTWEIGHT -- designed for panels that repaint slowly (QWebEngineView):
      - ZERO repaints on hover (no enterEvent/leaveEvent that call update())
      - ZERO repaints on click (no mousePressEvent/mouseReleaseEvent)
      - NO setMouseTracking -- no mouse events processed at all
      - sizeHint() NEVER changes -> no relayout storms
      - No QPainterPath -> zero heap allocation in paintEvent
      - No Antialiasing -> fast straight-line blitting
      - All QPen objects are module-level constants -> zero allocation
    """

    __slots__ = ()

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        # Fixed size -- NEVER changes (prevents relayout storms)
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedWidth(_HANDLE_WIDTH)
        else:
            self.setFixedHeight(_HANDLE_WIDTH)
        # Explicit cursor shape -- tells Qt to handle resize cursor natively
        if orientation == Qt.Orientation.Horizontal:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.SplitVCursor)

    # -- NO enterEvent, leaveEvent, mousePressEvent, mouseReleaseEvent --
    # This is intentional. Any event that calls self.update() forces a
    # repaint of ALL sibling widgets (sidebar, chat, editor QWebEngineViews).
    # The handle only needs to be drawn once and stay static.

    # -- painting (STATIC -- nothing changes on hover/press) -------------

    def paintEvent(self, event):
        p = QPainter(self)
        # NO Antialiasing -- straight lines only, much faster
        w = self.width()
        h = self.height()
        is_horiz = self.orientation() == Qt.Orientation.Horizontal

        # Single center line -- draws once, never repaints on hover
        p.setPen(_PEN_LINE)
        if is_horiz:
            # Vertical center line for horizontal splitter
            p.drawLine(w // 2, 0, w // 2, h - 1)
        else:
            # Horizontal center line for vertical splitter
            p.drawLine(0, h // 2, w - 1, h // 2)

        p.end()

    # -- size hints (CONSTANT -- never changes on hover) -----------------

    def sizeHint(self):
        if self.orientation() == Qt.Orientation.Horizontal:
            return QSize(_HANDLE_WIDTH, 0)
        else:
            return QSize(0, _HANDLE_WIDTH)
