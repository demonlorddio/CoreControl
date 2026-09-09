"""
Message Logger — records conversation turns and renders an HTML log viewable in any browser.
Logs are reset (cleared) every time the bot starts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "messages.jsonl"
_HTML_FILE = _LOG_DIR / "messages.html"


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class MessageLogger:
    """Thread-safe conversation logger. Writes JSONL and generates an HTML view."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict] = []

    def reset(self) -> None:
        """Clear all entries and start fresh. Call on bot startup."""
        with self._lock:
            self._entries.clear()
        self._write_jsonl()
        self._render_html()
        logger.info("Message logger reset.")

    def log_user(self, text: str) -> None:
        """Record a user prompt."""
        entry = {"ts": _timestamp(), "role": "user", "text": text}
        with self._lock:
            self._entries.append(entry)
        self._write_jsonl()
        self._render_html()

    def log_assistant(self, text: str) -> None:
        """Record a bot response."""
        entry = {"ts": _timestamp(), "role": "assistant", "text": text}
        with self._lock:
            self._entries.append(entry)
        self._write_jsonl()
        self._render_html()

    def _write_jsonl(self) -> None:
        with self._lock:
            lines = [json.dumps(e, ensure_ascii=False) for e in self._entries]
        _LOG_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _render_html(self) -> None:
        with self._lock:
            entries = list(self._entries)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(entries)

        rows_html = ""
        for e in entries:
            role = e.get("role", "?")
            ts = e.get("ts", "")
            text = e.get("text", "").replace("\n", "<br>").replace("<", "&lt;").replace(">", "&gt;")
            cls = "user" if role == "user" else "assistant"
            label = "You" if role == "user" else "Sage"
            rows_html += f'<tr class="{cls}"><td class="ts">{ts}</td><td class="label">{label}</td><td class="msg">{text}</td></tr>\n'

        tbody = rows_html.strip() if rows_html.strip() else '<tr><td colspan="3" class="empty">No messages yet.</td></tr>'
        msg_word = "messages" if total != 1 else "message"

        html = TEMPLATE.format(
            now=now,
            total=total,
            msg_word=msg_word,
            tbody=tbody,
        )
        _HTML_FILE.write_text(html, encoding="utf-8")
        logger.debug("HTML log rendered: %d entries", total)


# ── HTML template (static, no Python expressions inside) ──────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CoreControl — Message Log</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0a0e1a;
    color: #c8d6e5;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 14px;
    padding: 24px;
    min-height: 100vh;
  }}
  h1 {{
    color: #50aaff;
    font-size: 18px;
    margin-bottom: 4px;
    letter-spacing: 1px;
  }}
  .meta {{
    color: #576574;
    font-size: 12px;
    margin-bottom: 20px;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left;
    color: #50aaff;
    border-bottom: 1px solid #1e2a4a;
    padding: 8px 12px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }}
  td {{
    padding: 8px 12px;
    border-bottom: 1px solid #0d1525;
    vertical-align: top;
  }}
  tr.user td.msg {{ color: #f5a623; }}
  tr.assistant td.msg {{ color: #7ed6df; }}
  tr.user td.label {{ color: #f5a623; font-weight: bold; }}
  tr.assistant td.label {{ color: #7ed6df; font-weight: bold; }}
  .ts {{ color: #576574; white-space: nowrap; }}
  .label {{ white-space: nowrap; width: 80px; }}
  .msg {{ max-width: 900px; word-wrap: break-word; line-height: 1.5; }}
  .empty {{ color: #576574; padding: 40px; text-align: center; }}
</style>
</head>
<body>
  <h1>&#9776; CoreControl Message Log</h1>
  <div class="meta">Generated {now} &middot; {total} {msg_word} &middot; resets on bot restart</div>
  <table>
    <thead><tr><th>Time</th><th>From</th><th>Message</th></tr></thead>
    <tbody>
      {tbody}
    </tbody>
  </table>
<script>
(function() {{
  var last = '';
  var check = function() {{
    fetch(location.href + '?t=' + Date.now())
      .then(function(r) {{ return r.text(); }})
      .then(function(txt) {{
        if (txt !== last) {{ last = txt; location.reload(); }}
      }})
      .catch(function() {{}});
  }};
  setTimeout(check, 2000);
  setInterval(check, 3000);
}})();
</script>
</body>
</html>"""


# Module-level singleton
_logger = MessageLogger()


def get_logger() -> MessageLogger:
    return _logger


def get_html_path() -> Path:
    """Return the path to the HTML log file."""
    return _HTML_FILE
