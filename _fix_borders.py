"""One-shot fix: replace border:none with border:0px in tool_cards.py"""
import pathlib

p = pathlib.Path("src/ui/tool_cards.py")
content = p.read_text(encoding="utf-8")

# Fix cmd_lbl default style
content = content.replace(
    "padding:4px 8px;border:none;",
    "padding:4px 8px;border:0px solid transparent;"
)

# Fix output QTextBrowser border
old_out = 'border:1px solid {T[\'border_dim\']};border-radius:4px;padding:6px;'
new_out = 'border:0px solid transparent;border-radius:4px;padding:6px;'
content = content.replace(old_out, new_out)

p.write_text(content, encoding="utf-8")

# Verify
text = p.read_text(encoding="utf-8")
count = text.count("border:none")
count2 = text.count("border:0px solid transparent")
print(f"border:none remaining: {count}")
print(f"border:0px solid transparent: {count2}")
print("DONE")
