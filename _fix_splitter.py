"""One-shot helper to fix cursor_split_handle.py paintEvent."""
import sys

path = "src/ui/cursor_split_handle.py"
content = open(path, "r", encoding="utf-8").read()

old = """        # \u2500\u2500 background \u2500\u2500
        if self._is_pressed:
            bg = self._COL_PRESSED_BG
            border_col = self._COL_PRESSED_BORDER
            arrow_col = QColor("#ffffff")
        elif self._is_hovered:
            bg = self._COL_HOVER_BG
            border_col = self._COL_HOVER_BORDER
            arrow_col = self._COL_HOVER_ARROW
        else:
            bg = self._COL_BASE
            border_col = self._COL_BORDER
            arrow_col = QColor("#555555")  # dim when not hovered

        # Fill entire handle
        p.fillRect(0, 0, w, h, bg)

        # Border line(s)
        pen = QPen(border_col, 1)
        p.setPen(pen)
        if is_horiz:
            p.drawLine(0, 0, 0, h)       # left edge
            p.drawLine(w - 1, 0, w - 1, h)  # right edge
        else:
            p.drawLine(0, 0, w, 0)       # top edge
            p.drawLine(0, h - 1, w, h - 1)  # bottom edge"""

new = """        # Pick colors based on state
        if self._is_pressed:
            bg = self._COL_PRESSED_BG
            line_col = self._COL_PRESSED_LINE
            arrow_col = QColor("#ffffff")
        elif self._is_hovered:
            bg = self._COL_HOVER_BG
            line_col = self._COL_HOVER_LINE
            arrow_col = self._COL_HOVER_ARROW
        else:
            bg = self._COL_BASE
            line_col = self._COL_LINE
            arrow_col = QColor("#555555")  # dim when not hovered

        # Fill entire handle with panel-matching background
        p.fillRect(0, 0, w, h, bg)

        # Center divider line \u2014 the visible border between panels
        pen = QPen(line_col, 1)
        p.setPen(pen)
        if is_horiz:
            # Vertical handle \u2192 draw center vertical line
            cx = w // 2
            p.drawLine(cx, 0, cx, h)
        else:
            # Horizontal handle \u2192 draw center horizontal line
            cy = h // 2
            p.drawLine(0, cy, w, cy)"""

if old in content:
    content = content.replace(old, new)
    open(path, "w", encoding="utf-8").write(content)
    print("OK - paintEvent fixed")
else:
    print("ERROR - old paintEvent code not found in file")
    sys.exit(1)
