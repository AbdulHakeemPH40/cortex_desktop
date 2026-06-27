"""One-shot fix: add showEvent to cursor_split_handle.py"""
import pathlib

p = pathlib.Path("src/ui/cursor_split_handle.py")
lines = p.read_text(encoding="utf-8").splitlines(True)

# Insert showEvent method after line 47 (0-indexed: 46 = setCursor SplitVCursor)
insert_lines = [
    "\n",
    "    def showEvent(self, event):\n",
    '        """Force repaint when handle first becomes visible."""\n',
    "        super().showEvent(event)\n",
    "        self.update()\n",
]

new_lines = lines[:47] + insert_lines + lines[47:]
p.write_text("".join(new_lines), encoding="utf-8")

# Verify
text = p.read_text(encoding="utf-8")
assert "def showEvent" in text, "showEvent not found!"
print("OK: showEvent added and verified")
