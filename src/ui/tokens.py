"""
tokens.py — Cortex IDE Design Tokens
=====================================

Single source of truth for all chat UI theming.
Based on OpenCode OC-2 dark theme design tokens.

Every color in the UI flows from here. No hardcoded hex values outside this file.
"""

# ── Dark theme tokens (default) — OpenCode OC-2 exact ──
DARK = {
    # Backgrounds — matched to editor.html color scheme
    "bg":                "#1e1e1e",     # editor #app background
    "bg_card":           "#252526",     # editor tab-bar / elevated panel bg
    "bg_secondary":      "#2d2d2d",     # editor inactive tab bg
    "bg_tertiary":       "#18181c",     # editor file-path-bar bg
    "bg_hover":          "#2a2a2a",     # editor tab hover
    "bg_input":          "#161616",     # chat input — UNCHANGED
    "bg_elevated":       "#252526",     # elevated panels
    "bg_raised":         "#252526",     # raised panels

    # Borders — matched to editor.html
    "border":            "#3c3c3c",     # editor tab-bar border-bottom
    "border_dim":        "#2a2a2e",     # editor file-path-bar border-bottom
    "border_color":      "#3c3c3c",     # consistent with editor borders
    "border_active":     "#3b82f6",

    # Text — demo: text .85, text_secondary .55, text_tertiary .35
    "text":              "rgba(255,255,255,0.85)",
    "text_dim":          "rgba(255,255,255,0.55)",
    "text_secondary":    "rgba(255,255,255,0.55)",
    "text_primary":      "rgba(255,255,255,0.85)",
    "muted":             "rgba(255,255,255,0.35)",
    "mono_muted":        "#8b949e",

    # Accent
    "accent":            "#06b6d4",
    "accent_primary":    "#7c3aed",
    "accent_secondary":  "#6c5ce7",

    # Semantic
    "think":             "#7c6ce7",
    "think_label":       "#a89df0",
    "user_bubble":       "#212529",
    "green":             "#3fb950",
    "red":               "#f85149",
    "orange":            "#ff8c00",
    "warning":           "#e5c07b",
    "blue":              "#06b6d4",
    "info":              "#9d7cd8",
    "streaming_cursor":  "#06b6d4",

    # Tool card specific
    "tool_header_bg":    "rgba(255,255,255,0.03)",
    "tool_body_bg":      "rgba(255,255,255,0.01)",
    "diff_add_bg":       "rgba(63,185,80,0.12)",
    "diff_del_bg":       "rgba(248,81,73,0.12)",
    "diff_hunk_bg":      "rgba(110,118,129,0.06)",
    "diff_add_line":     "rgba(63,185,80,0.22)",
    "diff_del_line":     "rgba(248,81,73,0.22)",

    # Syntax — OC-2 exact values from TUI_DISPLAY_DESIGN.md
    "syntax_string":     "#00ceb9",    # teal — strings
    "syntax_property":   "#ff9ae2",    # pink — properties
    "syntax_keyword":    "#c678dd",    # purple — keywords (was muted, now proper purple)
    "syntax_variable":   "rgba(255,255,255,0.936)",  # strong — variables
    "syntax_function":   "#61afef",    # blue — functions/methods
    "syntax_number":     "#ffba92",    # peach — numbers/booleans
    "syntax_type":       "#ecf58c",    # yellow-green — types/classes
    "syntax_builtin":    "#f85149",    # RED — builtins (OpenCode: builtins are red!)
    "syntax_comment":    "rgba(255,255,255,0.422)",  # muted italic — comments
    "syntax_operator":   "#abb2bf",    # light-gray — operators/punctuation
    "syntax_constant":   "#d19a66",    # orange — constants
    "syntax_decorator":  "#ecf58c",    # yellow-green — decorators
    "syntax_tag":        "#e06c75",    # coral — HTML tags
    "syntax_attribute":  "#d19a66",    # orange — HTML attributes
    "syntax_namespace":  "#ecf58c",    # yellow-green — namespaces

    # Markdown colors — matches chat_panel_design_demo.html
    "md_heading":        "#f0f6fc",    # white — all heading levels (demo)
    "md_text":           "rgba(255,255,255,0.85)",  # white — body text (demo)
    "md_link":           "#58a6ff",    # blue — link URLs (demo)
    "md_link_text":      "#58a6ff",    # blue — link label text
    "md_code":           "#7fd88f",    # green — inline code (demo)
    "md_code_bg":        "rgba(110,118,129,0.15)",  # inline code background (demo)
    "md_blockquote":     "rgba(255,255,255,0.55)",  # secondary — blockquote text (demo)
    "md_blockquote_border": "#3b82f6", # blue — blockquote border (demo)
    "md_strong":         "#f5a742",    # orange — bold/strong (demo)
    "md_emph":           "#e5c07b",    # gold — italic/emphasis (OC-2 exact)
    "md_hr":             "#282828",    # subtle — horizontal rule
    "md_list_marker":    "#61afef",    # blue — bullet/numbers
    "md_table_header_bg": "rgba(255,255,255,0.04)",  # th background
    "md_table_border":   "#282828",    # subtle — table borders
    "md_strikethrough":  "rgba(255,255,255,0.4)",    # strikethrough
    "md_mark_bg":        "rgba(229,192,123,0.25)",   # mark highlight bg
    "md_filename":       "#3fb950",    # green — file names/paths

    # Syntax highlight palette (used by tool_cards.py)
    "tool_read":      "#58a6ff",   # blue   — reading / inspecting
    "tool_edit":      "#3fb950",   # green  — mutating files
    "tool_write":     "#3fb950",   # green  — creating files
    "tool_search":    "#bc8cff",   # purple — grep / glob / semantic search
    "tool_terminal":  "#f0883e",   # orange — shell / bash / powershell
    "tool_web":       "#56d4dd",   # cyan   — web_search / web_fetch
    "tool_task":      "#d2a8ff",   # lilac  — task / planning
    "tool_team":      "#ff7b72",   # coral  — multi-agent / delegation
    "tool_thought":   "#7c6ce7",   # violet — thinking
    "tool_generic":   "#8b949e",   # gray   — fallback

    # Status colors
    "status_running": "#56d4dd",   # spinner default tint
    "status_ok":      "#3fb950",
    "status_error":   "#f85149",

    # Fonts
    "font_ui":           "Geist, 'Segoe UI', system-ui, -apple-system, sans-serif",
    "font_mono":         "'JetBrains Mono','Fira Code',Consolas,'Courier New',monospace",
    "font_size":         "14px",
    "font_size_sm":      "13px",
    "font_size_xs":      "12px",
    "font_size_xxs":     "11px",

    # Line heights
    "line_height":       "1.45",
    "line_height_code":  "1.4",

    # Spacing & Radius
    "radius_xs":         "4px",
    "radius_sm":         "6px",
    "radius_md":         "8px",
    "radius_lg":         "10px",
    "radius_xl":         "14px",

    # Input / Menu / Button tokens (used by InputArea, context menus, etc.)
    "input_border":      "#262626",
    "input_hover":       "#2A2A2A",
    "separator":         "#3c3c3c",     # match editor borders
    "btn_bg":            "#242424",
    "btn_hover":         "#2D2D2D",
    "btn_text":          "#aaaaaa",
    "btn_text_hover":    "#ffffff",
    "menu_bg":           "#1F1F1F",
    "menu_text":         "#cccccc",
    "menu_selected":     "#2A2A2A",
    "divider":           "#3c3c3c",     # match editor borders
    "white":             "#ffffff",
    "card_border_subtle": "rgba(255,255,255,0.08)",
    "card_bg_subtle":    "rgba(255,255,255,0.015)",
    "stop_btn":          "#f85149",
    "stop_btn_hover_bg": "rgba(248,81,73,0.15)",
    "spell_error":       "#ff4040",
    "spell_input_bg":    "#1a1a1a",
    "context_menu_bg":   "#252526",
    "context_menu_border": "#3c3c3c",
    "context_menu_sel":  "#094771",
    "ring_purple":       "#7c3aed",
    "ring_purple_light": "#8b5cf6",
    "ring_cyan":         "#06b6d4",
    "ring_cyan_light":   "#22d3ee",
    "edited_row_bg":     "#18181c",     # match bg_tertiary
    "edited_row_hover":  "#2a2a2a",     # match bg_hover
    "edited_row_text":   "#e3e4e6",
    "edited_row_badge":  "#ffb300",

    # Code block decoration
    "code_header_bg":    "#2d2d2d",     # match bg_secondary (editor inactive tab)
    "code_header_border": "#3c3c3c",    # match editor borders
    "code_copy_color":   "#8b949e",
    "code_copy_hover":   "#f0f6fc",
    "code_copy_bg":      "transparent",
    "code_copy_bg_hover": "#30363d",
    "code_lang_color":   "#8b949e",
    "code_line_number":  "rgba(255,255,255,0.35)",
    "code_scrollbar":    "rgba(255,255,255,0.10)",
}

# ── Light theme tokens — OpenCode OC-2 light mode ──
LIGHT = {
    "bg":                "#ffffff",
    "bg_card":           "#f6f8fa",
    "bg_secondary":      "#f0f2f5",
    "bg_tertiary":       "#e8eaed",
    "bg_hover":          "#dfe2e8",
    "bg_input":          "#ffffff",
    "bg_elevated":       "#ffffff",
    "bg_raised":         "#ffffff",

    "border":            "#d0d7de",
    "border_dim":        "#e1e4e8",
    "border_color":      "#d0d7de",
    "border_active":     "#0969da",

    "text":              "#1f2328",
    "text_dim":          "#656d76",
    "text_secondary":    "#57606a",
    "text_primary":      "#1f2328",
    "muted":             "#8b949e",
    "mono_muted":        "#656d76",

    "accent":            "#0969da",
    "accent_primary":    "#7c3aed",
    "accent_secondary":  "#6c5ce7",

    "think":             "#57606a",
    "think_label":       "#57606a",
    "user_bubble":       "#f6f8fa",
    "green":             "#1a7f37",
    "red":               "#cf222e",
    "orange":            "#bf8700",
    "warning":           "#9a6700",
    "blue":              "#0969da",
    "info":              "#8250df",
    "streaming_cursor":  "#0969da",

    "tool_header_bg":    "rgba(0,0,0,0.03)",
    "tool_body_bg":      "rgba(0,0,0,0.01)",
    "diff_add_bg":       "rgba(26,127,55,0.12)",
    "diff_del_bg":       "rgba(207,34,46,0.12)",
    "diff_hunk_bg":      "rgba(110,118,129,0.06)",
    "diff_add_line":     "rgba(26,127,55,0.15)",
    "diff_del_line":     "rgba(207,34,46,0.15)",

    # Syntax — OC-2 light mode
    "syntax_string":     "#006656",    # dark-teal
    "syntax_property":   "#ed6dc8",    # pink
    "syntax_keyword":    "#a626a4",    # purple
    "syntax_variable":   "#1a1a1a",    # near-black
    "syntax_function":   "#4078f2",    # blue
    "syntax_number":     "#fb4804",    # orange
    "syntax_type":       "#596600",    # olive
    "syntax_builtin":    "#cf222e",    # red (OpenCode: builtins are red!)
    "syntax_comment":    "#8b949e",    # gray
    "syntax_operator":   "#383a42",    # dark-gray
    "syntax_constant":   "#e36209",    # orange
    "syntax_decorator":  "#596600",    # olive
    "syntax_tag":        "#116329",    # green
    "syntax_attribute":  "#e36209",    # orange
    "syntax_namespace":  "#596600",    # olive

    # Markdown — OC-2 light mode
    "md_heading":        "#d68c27",    # gold-orange
    "md_text":           "#1a1a1a",    # near-black
    "md_link":           "#3b7dd8",    # blue
    "md_link_text":      "#318795",    # cyan
    "md_code":           "#3d9a57",    # green
    "md_code_bg":        "rgba(0,0,0,0.05)",
    "md_blockquote":     "#b0851f",    # gold
    "md_blockquote_border": "#d0d7de",
    "md_strong":         "#d68c27",    # gold-orange
    "md_emph":           "#b0851f",    # dark-gold
    "md_hr":             "#e5e5e5",    # light gray
    "md_list_marker":    "#3b7dd8",    # blue
    "md_table_header_bg": "rgba(0,0,0,0.05)",
    "md_table_border":   "#d0d7de",
    "md_strikethrough":  "#8b949e",
    "md_mark_bg":        "rgba(214,140,39,0.2)",

    # Tool colors
    "tool_read":      "#0969da",
    "tool_edit":      "#1a7f37",
    "tool_write":     "#1a7f37",
    "tool_search":    "#8250df",
    "tool_terminal":  "#bc4c00",
    "tool_web":       "#0e8a7e",
    "tool_task":      "#8250df",
    "tool_team":      "#cf222e",
    "tool_thought":   "#6c5ce7",
    "tool_generic":   "#57606a",
    "status_running": "#0e8a7e",
    "status_ok":      "#1a7f37",
    "status_error":   "#cf222e",

    "font_ui":           "Geist, 'Segoe UI', system-ui, -apple-system, sans-serif",
    "font_mono":         "'JetBrains Mono','Fira Code',Consolas,'Courier New',monospace",
    "font_size":         "14px",
    "font_size_sm":      "13px",
    "font_size_xs":      "12px",
    "font_size_xxs":     "11px",

    "line_height":       "1.45",
    "line_height_code":  "1.4",

    "radius_xs":         "4px",
    "radius_sm":         "6px",
    "radius_md":         "8px",
    "radius_lg":         "10px",
    "radius_xl":         "14px",

    # Input / Menu / Button tokens
    "input_border":      "#d0d7de",
    "input_hover":       "#e8eaed",
    "separator":         "#d0d7de",
    "btn_bg":            "#f0f2f5",
    "btn_hover":         "#e8eaed",
    "btn_text":          "#57606a",
    "btn_text_hover":    "#1f2328",
    "menu_bg":           "#ffffff",
    "menu_text":         "#1f2328",
    "menu_selected":     "#f0f2f5",
    "divider":           "#d0d7de",
    "white":             "#ffffff",
    "card_border_subtle": "rgba(0,0,0,0.08)",
    "card_bg_subtle":    "rgba(0,0,0,0.015)",
    "stop_btn":          "#cf222e",
    "stop_btn_hover_bg": "rgba(207,34,46,0.15)",
    "spell_error":       "#cf222e",
    "spell_input_bg":    "#f6f8fa",
    "context_menu_bg":   "#ffffff",
    "context_menu_border": "#d0d7de",
    "context_menu_sel":  "#0969da",
    "ring_purple":       "#7c3aed",
    "ring_purple_light": "#8b5cf6",
    "ring_cyan":         "#0969da",
    "ring_cyan_light":   "#58a6ff",
    "edited_row_bg":     "#f6f8fa",
    "edited_row_hover":  "#e8eaed",
    "edited_row_text":   "#1f2328",
    "edited_row_badge":  "#bf8700",

    "code_header_bg":    "#f0f2f5",
    "code_header_border": "#e1e4e8",
    "code_copy_color":   "#57606a",
    "code_copy_hover":   "#1f2328",
    "code_copy_bg":      "rgba(0,0,0,0.03)",
    "code_copy_bg_hover": "rgba(0,0,0,0.08)",
    "code_lang_color":   "#8b949e",
    "code_line_number":  "#c7c7c7",
    "code_scrollbar":    "rgba(0,0,0,0.10)",
}

# ── Theme state ──
_current_theme = DARK


def get_theme(mode: str = "dark") -> dict:
    """Return the token dict for the given theme mode."""
    return LIGHT if mode == "light" else DARK


def set_theme(mode: str = "dark"):
    """Switch the active theme. All T() calls will return the new theme."""
    global _current_theme
    _current_theme = LIGHT if mode == "light" else DARK


def T() -> dict:
    """Get current theme tokens. Usage: T()['key'] — allows runtime switching."""
    return _current_theme


def build_markdown_css(t: dict | None = None) -> str:
    """Generate unified markdown CSS from design tokens. Single source of truth.

    OpenCode OC-2 styling:
    - Headings: purple, bold
    - Strong/bold: orange
    - Emphasis/italic: gold
    - Inline code: green with subtle bg
    - Code blocks: dark bg, subtle border, no padding (handled by wrapper)
    - Blockquotes: gold, italic, left border
    - Links: peach underline
    - Lists: blue markers
    - Tables: subtle borders, header bg
    """
    if t is None:
        t = _current_theme
    return f"""<style>
  body {{
    color: {t['md_text']} !important; font-size: {t['font_size']};
    line-height: {t['line_height']}; font-family: {t['font_ui']};
    overflow-wrap: break-word; word-break: break-word;
    max-width: 100%; box-sizing: border-box;
  }}

  /* Universal overflow prevention — NO element can cause horizontal scroll */
  * {{ overflow-wrap: break-word; word-wrap: break-word; box-sizing: border-box; }}

  /* Headings — OpenCode: purple (#9d7cd8), bold */
  h1, h2, h3, h4, h5, h6 {{
    color: {t['md_heading']} !important; font-weight: 600; margin: 8px 0 4px 0;
    overflow-wrap: break-word; word-wrap: break-word; max-width: 100%;
  }}
  h1 {{ font-size: 1.5em; border-bottom: 1px solid {t['border_dim']}; padding-bottom: 4px; margin-top: 12px; }}
  h2 {{ font-size: 1.35em; margin-top: 10px; }}
  h3 {{ font-size: 1.2em; margin-top: 8px; }}
  h4 {{ font-size: 1.05em; }}
  h5, h6 {{ font-size: 1em; color: {t['text_dim']} !important; }}

  /* Paragraphs */
  p {{ color: {t['md_text']} !important; margin: 3px 0; line-height: {t['line_height']};
      overflow-wrap: break-word; word-wrap: break-word; word-break: break-word;
      max-width: 100%; box-sizing: border-box; }}

  /* Strong — OpenCode: orange (#f5a742) */
  strong, b {{ color: {t['md_strong']} !important; font-weight: 600; }}

  /* Emphasis — OpenCode: gold (#e5c07b) */
  em, i {{ color: {t['md_emph']} !important; font-style: italic; }}

  /* Inline code — OpenCode: green (#7fd88f) with subtle bg */
  code {{
    color: {t['md_code']} !important; background: {t['md_code_bg']};
    font-family: {t['font_mono']}; font-size: 0.9em;
    padding: 2px 6px; border-radius: 4px;
  }}

  /* Code blocks — dark bg, subtle border */
  pre {{
    max-width: 100%; overflow-x: auto; box-sizing: border-box;
    background: {t['bg']}; border: 1px solid {t['border_dim']};
    border-radius: {t['radius_md']}; padding: 10px 14px; margin: 8px 0;
  }}
  pre code {{
    color: {t['text']} !important; background: transparent;
    padding: 0; border-radius: 0;
    font-size: 0.88em; line-height: {t['line_height_code']};
    white-space: pre-wrap;
  }}

  /* Links — OpenCode: peach (#fab283) */
  a {{ color: {t['md_link']} !important; text-decoration: underline; }}
  a:visited {{ color: {t['md_link']} !important; }}

  /* Blockquotes — demo: blue left border, normal text */
  blockquote {{
    border-left: 3px solid {t['md_blockquote_border']};
    color: {t['md_blockquote']} !important;
    padding: 4px 0 4px 12px; margin: 8px 0;
    background: transparent;
    border-radius: 0;
    overflow-wrap: break-word; word-wrap: break-word; word-break: break-word;
  }}

  /* Horizontal rules — subtle */
  hr {{ border: none; border-top: 1px solid {t['md_hr']}; margin: 10px 0; }}

  /* Lists — pure white text, NOT purple */
  ul, ol {{ padding-left: 20px; margin: 4px 0; overflow-wrap: break-word; word-wrap: break-word; }}
  li {{ margin: 2px 0; color: {t['md_text']} !important; line-height: {t['line_height']};
       overflow-wrap: break-word; word-wrap: break-word; word-break: break-word; }}
  li > ul, li > ol {{ margin: 2px 0 2px 0; }}

  /* Tables — purple header, white body, file names green */
  table {{ border-collapse: collapse; margin: 6px 0; width: 100%; font-size: 13px;
           table-layout: fixed; max-width: 100%; overflow-wrap: break-word; word-wrap: break-word; }}
  th, td {{
    border-bottom: 1px solid {t['md_table_border']};
    padding: 8px 12px; text-align: left; vertical-align: top;
    overflow-wrap: break-word; word-wrap: break-word; word-break: break-word;
  }}
  th {{
    color: {t['md_heading']} !important; font-weight: 600;
    border-bottom: 1px solid {t['border']};
    background: {t['md_table_header_bg']};
  }}
  /* Qt setMarkdown() uses <td> for headers \u2014 style first row like th */
  tr:first-child td {{
    color: {t['md_heading']} !important; font-weight: 600;
    border-bottom: 1px solid {t['border']};
    background: {t['md_table_header_bg']};
  }}
  td {{ color: {t['md_text']} !important; }}
  /* Remove outer borders */
  table tr:first-child th, table tr:first-child td {{ border-top: none; }}
  table tr:last-child td {{ border-bottom: none; }}
  table th:first-child, table td:first-child {{ border-left: none; }}
  table th:last-child, table td:last-child {{ border-right: none; }}

  /* Strikethrough */
  del, s {{ color: {t['md_strikethrough']} !important; text-decoration: line-through; }}

  /* Mark highlighting */
  mark {{ background: {t['md_mark_bg']}; color: {t['text']} !important; border-radius: 2px; padding: 0 3px; }}

  /* Images */
  img {{ max-width: 100%; border-radius: {t['radius_sm']}; margin: 8px 0; }}

  /* Definition lists */
  dt {{ font-weight: 600; color: {t['md_heading']} !important; margin-top: 12px; }}
  dd {{ margin-left: 20px; color: {t['text_dim']} !important; }}

  /* Task lists (checkboxes) */
  input[type="checkbox"] {{
    margin-right: 6px;
  }}

  /* Keyboard shortcuts */
  kbd {{
    background: {t['bg_secondary']}; border: 1px solid {t['border_dim']};
    border-radius: 3px; padding: 1px 5px;
    font-family: {t['font_mono']}; font-size: 0.88em;
    color: {t['text_dim']} !important;
  }}

  /* File names/paths — GREEN highlight */
  code.filename, code.filepath, span.filename {{
    color: {t['md_filename']} !important;
    background: rgba(63,185,80,0.08) !important;
    padding: 1px 4px !important;
    font-weight: 500;
    font-family: {t['font_mono']};
  }}
  /* Any code element that looks like a path (contains / or \\) — green */
  code[class*=\"path\"], code[class*=\"file\"] {{
    color: {t['md_filename']} !important;
    background: rgba(63,185,80,0.08) !important;
    padding: 1px 4px !important;
  }}
</style>"""


def build_code_block_css(t: dict | None = None) -> str:
    """CSS for code block decorations (header bar, copy button, language label)."""
    if t is None:
        t = _current_theme
    return f"""<style>
    /* Code block wrapper */
    .cb-wrapper {{
      margin: 12px 0; border-radius: {t['radius_md']};
      border: 1px solid {t['code_header_border']};
    }}
    /* Code block header bar */
    .cb-header {{
      background: {t['code_header_bg']};
      border-bottom: 1px solid {t['code_header_border']};
      padding: 6px 14px; min-height: 32px;
    }}
    /* Language label */
    .cb-lang {{
      color: {t['code_lang_color']}; font-family: {t['font_mono']};
      font-size: 11px; text-transform: uppercase;
      font-weight: 500;
    }}
    /* Copy button */
    .cb-copy {{
      color: {t['code_copy_color']}; font-size: 11px;
      padding: 3px 10px; border-radius: {t['radius_xs']};
      border: 1px solid {t['border_dim']}; background: {t['code_copy_bg']};
      font-family: {t['font_ui']};
    }}
    /* Code body */
    .cb-body {{
      background: {t['bg']};
      border-top: none;
    }}
    .cb-body pre {{
      margin: 0; border: none; border-radius: 0;
    }}
    </style>"""


def build_qss(t: dict | None = None, mode: str = "dark") -> str:
    """Build the full QSS stylesheet from tokens."""
    if t is None:
        t = get_theme(mode)
    return f"""
    QWidget {{
        background: {t['bg']};
        color: {t['text']};
        font-family: {t['font_ui']};
        font-size: {t['font_size']};
    }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {t['border']}; border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t['border_active']};
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

    #userBubble {{
        background: {t['bg_card']};
        border: none;
        border-right: 3px solid #ff8c00;
        border-radius: 0px;
        padding: 12px 18px;
        font-size: 14px;
        line-height: 1.65;
        color: rgba(255,255,255,0.92);
        font-family: {t['font_mono']};
    }}

    #aiCard {{
        background: transparent;
        border: none;
    }}

    QTextBrowser {{
        background: transparent;
        border: none;
        font-size: {t['font_size']};
    }}

    #cardFrame {{
        border: 1px solid {t['border']};
        border-radius: 0px;
    }}
    #cardHeader {{ background: transparent; }}
    #thoughtHeader {{
        background: rgba(124,108,231,0.06);
        border-radius: {t['radius_md']};
    }}
    #cardHeaderLabel {{
        color: {t['text_secondary']};
        font-size: {t['font_size_xs']};
        font-weight: 400;
    }}
    #thoughtLabel {{
        color: {t['think_label']};
        font-size: {t['font_size_sm']};
        font-weight: 500;
    }}
    #thoughtBody {{
        color: {t['text_secondary']};
        font-size: {t['font_size_sm']};
        font-style: italic;
    }}

    #toolName {{
        color: {t['text_secondary']};
        font-size: {t['font_size_xs']};
    }}
    #toolArg {{
        color: {t['muted']};
        font-family: {t['font_mono']};
        font-size: {t['font_size_xxs']};
    }}

    #chatInput {{
        background: {t['bg_card']};
        border: 1px solid {t['border']};
        border-radius: {t['radius_lg']};
        padding: 10px 12px;
        font-size: {t['font_size']};
    }}
    #chatInput:focus {{
        border-color: {t['border_active']};
    }}
    #sendBtn {{
        background: {t['accent']};
        border: none;
        border-radius: {t['radius_md']};
        padding: 8px 14px;
        color: #04222a;
        font-weight: 600;
    }}
    #sendBtn:hover {{
        background: {t['accent']};
        opacity: 0.9;
    }}
    #stopBtn {{
        background: transparent;
        border: 1px solid {t['red']};
        border-radius: {t['radius_md']};
        padding: 8px 14px;
        color: {t['red']};
        font-weight: 600;
    }}
    #stopBtn:hover {{
        background: rgba(248,81,73,0.10);
    }}
    #toolbarBtn {{
        background: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: {t['radius_sm']};
        padding: 4px 10px;
        color: {t['text_dim']};
        font-size: {t['font_size_xs']};
    }}
    #toolbarBtn:hover {{ background: rgba(255,255,255,0.08); }}
    #toolbarBtn::menu-indicator {{ image: none; width: 0; }}
    QMenu {{
        background: {t['bg_card']};
        border: 1px solid {t['border']};
        border-radius: {t['radius_md']};
        padding: 6px;
        color: {t['text']};
    }}
    QMenu::item {{
        padding: 6px 12px;
        border-radius: {t['radius_sm']};
        font-size: {t['font_size_xs']};
    }}
    QMenu::item:selected {{ background: rgba(255,255,255,0.08); }}
    QMenu::item:disabled {{ color: {t['muted']}; font-size: 10px; }}
    QMenu::separator {{
        height: 1px;
        background: {t['border']};
        margin: 6px 4px;
    }}
    #chev {{
        color: {t['muted']};
        border: none;
        background: transparent;
        font-size: 14px;
        width: 20px;
    }}
    """


# ── Context Window Compact Thresholds ──────────────────────────────────────
# These are UI design tokens for display purposes.
# The actual runtime threshold is calculated by
# agent.src.services.compact.autoCompact.getAutoCompactThreshold()
# which uses model-aware buffer tokens (13K for auto, 3K for manual).

COMPACT_EARLY_THRESHOLD = 0.85   # 85% — visual warning in token bar
COMPACT_URGENT_THRESHOLD = 0.95  # 95% — visual urgent state in token bar


def get_model_context_limit(model_id: str) -> int:
    """
    Get the context window size (tokens) for a given model ID.

    Used by UI components to determine when to show context pressure
    warnings and trigger early compaction.

    Delegates to model_limits.py registry. Falls back to 200K tokens.
    """
    try:
        from src.ai.model_limits import get_model_limits
        limits = get_model_limits(model_id)
        return limits.context_window
    except Exception:
        return 200_000


def get_auto_compact_threshold(model_id: str) -> int:
    """
    Get the model-aware auto-compact threshold (tokens).

    Delegates to the existing autoCompact system which calculates:
        effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS(13,000)

    This is more precise than a flat 85% multiplier and matches
    the production compaction trigger used by the agent runtime.
    """
    try:
        from agent.src.services.compact.autoCompact import getAutoCompactThreshold
        return getAutoCompactThreshold(model_id)
    except ImportError:
        # Fallback: 85% of context window
        ctx = get_model_context_limit(model_id)
        return int(ctx * 0.85)