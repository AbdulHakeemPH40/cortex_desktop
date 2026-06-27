"""
CursorSplitHandle + CursorSplitter - Custom splitter with Cursor-style
<-> resize arrows on hover, matching the Cursor IDE split handle look.

Drop-in replacement for QSplitter: use CursorSplitter instead.
"""

from PyQt6.QtWidgets import QSplitter, QSplitterHandle
from PyQt6.QtGui import QPainter, QColor, QPen, QPainterPath
from PyQt6.QtCore import Qt, QRectF


class CursorSplitter(QSplitter):
    """QSplitter subclass that creates CursorSplitHandle for all handles."""

    def createHandle(self):
        return CursorSplitHandle(self.orientation(), self)


class CursorSplitHandle(QSplitterHandle):
    """A splitter handle that paints <-> (or up/down) resize arrows on hover."""

    # Theme colors - Cursor Anysphere dark
    # Base: visible groove divider between panels
    _COL_BASE = QColor("#2d2d2d")         # slightly lighter than panels - visible groove
    _COL_LINE = QColor("#555555")         # center divider line (always visible)
    _COL_HOVER_BG = QColor("#2a2a2a")
    _COL_HOVER_LINE = QColor("#4d78cc")   # blue accent line on hover
    _COL_HOVER_ARROW = QColor("#8ab4f8")
    _COL_PRESSED_BG = QColor("#4d78cc")
    _COL_PRESSED_LINE = QColor("#5a8ad6")

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._is_hovered = False
        self._is_pressed = False
        self.setMouseTracking(True)
        # Default handle size - 6px wide for comfortable drag target
        if orientation == Qt.Orientation.Horizontal:
            self.setFixedWidth(6)
        else:
            self.setFixedHeight(6)
        # Explicit cursor shape - must set manually for custom QSplitterHandle
        if orientation == Qt.Orientation.Horizontal:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.SplitVCursor)

    # -- hover tracking ---------------------------------------------------

    def showEvent(self, event):
        """Force repaint when handle first becomes visible."""
        super().showEvent(event)
        self.update()

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._is_hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._is_pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._is_pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    # -- painting ---------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        is_horiz = self.orientation() == Qt.Orientation.Horizontal

        # Pick colors based on state
        if self._is_pressed:
            bg = self._COL_PRESSED_BG
            line_col = self._COL_PRESSED_LINE
            arrow_col = QColor("#ffffff")
        elif self._is_hovered:
            bg = self._COL_HOVER_BG
            line_col = self._COL_HOVER_LINE
            arrow_col = QColor("#8ab4f8")
        else:
            bg = self._COL_BASE
            line_col = self._COL_LINE
            arrow_col = QColor("#555555")  # dim when not hovered

        # Fill entire handle with groove background
        p.fillRect(0, 0, w, h, bg)

        # Draw TWO edge lines - creates a visible groove/border between panels
        if is_horiz:
            # Vertical handle -> draw left and right edge lines
            left_col = QColor("#1a1a1a")   # dark edge (shadow)
            right_col = QColor("#444444")  # light edge (highlight)
            p.setPen(QPen(left_col, 1))
            p.drawLine(0, 0, 0, h)
            p.setPen(QPen(right_col, 1))
            p.drawLine(w - 1, 0, w - 1, h)
            # Center accent line - ALWAYS visible (not just on hover)
            cx = w // 2
            p.setPen(QPen(line_col, 1))
            p.drawLine(cx, 0, cx, h)
        else:
            # Horizontal handle -> draw top and bottom edge lines
            top_col = QColor("#1a1a1a")    # dark edge (shadow)
            bot_col = QColor("#444444")    # light edge (highlight)
            p.setPen(QPen(top_col, 1))
            p.drawLine(0, 0, w, 0)
            p.setPen(QPen(bot_col, 1))
            p.drawLine(0, h - 1, w, h - 1)
            # Center accent line - ALWAYS visible (not just on hover)
            cy = h // 2
            p.setPen(QPen(line_col, 1))
            p.drawLine(0, cy, w, cy)

        # -- arrows (only when hovered or pressed) --
        if self._is_hovered or self._is_pressed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(arrow_col)

            cx = w / 2.0
            cy = h / 2.0

            if is_horiz:
                # Vertical handle (horizontal splitter) <-> arrows
                self._draw_h_arrows(p, cx, cy, w, h)
            else:
                # Horizontal handle (vertical splitter) up/down arrows
                self._draw_v_arrows(p, cx, cy, w, h)

        p.end()

    def _draw_h_arrows(self, p: QPainter, cx: float, cy: float, w: float, h: float):
        """Draw left and right arrows for a vertical handle (horizontal splitter)."""
        body = 4
        head = 3
        gap = 1

        # Left arrow
        left_cx = cx - gap
        path_l = QPainterPath()
        path_l.moveTo(left_cx - body / 2, cy)              # tip (left)
        path_l.lineTo(left_cx + body / 2, cy - head)        # top-right
        path_l.lineTo(left_cx + body / 2, cy + head)        # bottom-right
        path_l.closeSubpath()
        p.drawPath(path_l)

        # Right arrow
        right_cx = cx + gap
        path_r = QPainterPath()
        path_r.moveTo(right_cx + body / 2, cy)              # tip (right)
        path_r.lineTo(right_cx - body / 2, cy - head)       # top-left
        path_r.lineTo(right_cx - body / 2, cy + head)       # bottom-left
        path_r.closeSubpath()
        p.drawPath(path_r)

    def _draw_v_arrows(self, p: QPainter, cx: float, cy: float, w: float, h: float):
        """Draw up and down arrows for a horizontal handle (vertical splitter)."""
        body = 4
        head = 3
        gap = 1

        # Top arrow
        top_cy = cy - gap
        path_t = QPainterPath()
        path_t.moveTo(cx, top_cy - body / 2)               # tip (top)
        path_t.lineTo(cx - head, top_cy + body / 2)        # bottom-left
        path_t.lineTo(cx + head, top_cy + body / 2)        # bottom-right
        path_t.closeSubpath()
        p.drawPath(path_t)

        # Bottom arrow
        bot_cy = cy + gap
        path_b = QPainterPath()
        path_b.moveTo(cx, bot_cy + body / 2)               # tip (bottom)
        path_b.lineTo(cx - head, bot_cy - body / 2)        # top-left
        path_b.lineTo(cx + head, bot_cy - body / 2)        # top-right
        path_b.closeSubpath()
        p.drawPath(path_b)

    # -- size hints -------------------------------------------------------

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        size = self._resize_cursor_size(6)
        if self.orientation() == Qt.Orientation.Horizontal:
            return QSize(size, 0)
        else:
            return QSize(0, size)

    def _resize_cursor_size(self, base: int):
        """Return size hint - 6px base, slightly wider when hovered for better UX."""
        if self._is_hovered:
            return base + 4  # 10px on hover for better grab target
        return base
