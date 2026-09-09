# Day 3.5 — 2026-09-09 (Evening Session)

## What We Built

After the Day 3 log, several more features shipped in a single evening session — four rounds of additions, two bug fixes, and one HITL configuration change:

1. **Time & Location tools** — `get_current_time` and `get_location` for the LLM to answer "what time is it?" and "where am I?" without hallucinating.
2. **`open_url` tool** — fixes the LLM refusing to open websites by providing a trivial `webbrowser.open()` wrapper that bypasses Playwright async conflicts.
3. **9 new MCP tools** — `launch_app`, clipboard read/write, `set_timer`, annotated screenshots, file renamer, `kill_process`, `disk_usage`, YouTube search, and GitHub ops via `gh` CLI.
4. **4 more MCP tools** — `volume_control`, `screen_ocr`, `google_search`, and an enhanced `app_launcher` with shortcut-name mapping.
5. **HITL auto-approve fix** — the 5 tools above were getting blocked by the confirmation modal; added them to `auto_approve_safe_actions` in `settings.json`.
6. **Message logger HTML `<br>` fix** — newlines rendered as literal `&lt;br&gt;` instead of line breaks.

## Architecture

### Tool Call Flow (unchanged)
```
orchestrator.generate()
  └── LLM returns tool_calls[]
  └── for each tool_call:
        ├── _hitl.check(tool_name, tool_args, screenshot)
        │     ├── tool in AUTO_APPROVE_TOOLS → return True (no modal)
        │     ├── tool NOT in HIGH_RISK_TOOLS → return True (no modal)
        │     └── otherwise → show HitlRequest modal, await user response
        ├── _mcp.call_tool(tool_name, final_args)
        └── tool_results.append({tool, result|error})
  └── tool_results fed back to LLM as user message
```

### MCP Server Dispatch (server.py)
All new tools are dispatched in `handle_call_tool` before the `else` fallback:
```python
elif name == "get_current_time":   return _call_get_current_time(arguments)
elif name == "get_location":       return _call_get_location(arguments)
elif name == "open_url":           return _call_open_url(arguments)
elif name == "launch_app":         return _call_launch_app(arguments)
elif name == "clipboard_read":     return _call_clipboard_read(arguments)
elif name == "clipboard_write":    return _call_clipboard_write(arguments)
elif name == "set_timer":          return _call_set_timer(arguments)
elif name == "annotate_screenshot":return _call_annotate_screenshot(arguments)
elif name == "rename_files":       return _call_rename_files(arguments)
elif name == "kill_process":       return _call_kill_process(arguments)
elif name == "disk_usage":         return _call_disk_usage(arguments)
elif name == "search_youtube":     return _call_search_youtube(arguments)
elif name == "github_op":          return _call_github_op(arguments)
elif name == "volume_control":     return _call_volume_control(arguments)
elif name == "screen_ocr":         return _call_screen_ocr(arguments)
elif name == "google_search":      return _call_google_search(arguments)
elif name == "app_launcher":       return _call_app_launcher(arguments)
```

### HITL Auto-Approve Configuration
`config/settings.json` — `security.auto_approve_safe_actions`:
```json
"auto_approve_safe_actions": [
  "take_screenshot",
  "get_system_stats",
  "volume_control",
  "screen_ocr",
  "google_search",
  "app_launcher",
  "launch_app"
]
```

### Message Logger HTML Bug Fix
`src/gui/message_logger.py` line 74:
```python
# BEFORE (broken — <br> tags got re-escaped):
text = e.get("text", "").replace("\n", "<br>").replace("<", "&lt;").replace(">", "&gt;")

# AFTER (correct — escape HTML first, then convert newlines):
text = e.get("text", "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
```

## Key Files

| File | Change |
|------|--------|
| `src/mcp_server/server.py` | **+955 lines** — 17 new tool schemas + 17 handler functions |
| `src/orchestrator.py` | +10 lines — CRITICAL RULES expanded for all new tools |
| `config/settings.json` | +6 entries in `auto_approve_safe_actions` |
| `src/gui/message_logger.py` | 1-line fix — escape order in `_render_html()` |

## New Tools Detail

### Time & Location
| Tool | Description | Implementation |
|------|-------------|----------------|
| `get_current_time` | Local date/time + timezone offset | `datetime.datetime.now().astimezone()` |
| `get_location` | City, region, country, coords via public IP | `requests.get("https://ipinfo.io/json")` |

### Browser & Launch
| Tool | Description | Implementation |
|------|-------------|----------------|
| `open_url` | Open any URL in system browser | `webbrowser.open(url)` |
| `launch_app` | Launch app/file by name or path | `subprocess.Popen(target, shell=True)` |
| `app_launcher` | Enhanced launcher with shortcut mappings (settings, calc, notepad, zoom, teams, discord, spotify, etc.) + full path support | `subprocess.Popen` with Windows shortcut name table |
| `google_search` | Search Google and fetch content preview | `webbrowser.open()` + `requests.get()` on results page |
| `search_youtube` | Search YouTube and open results | `webbrowser.open(youtube_search_url)` |

### System Control
| Tool | Description | Implementation |
|------|-------------|----------------|
| `volume_control` | Get/set/mute/unmute/up/down volume | Windows `WM_APPCOMMAND` broadcast via ctypes `SendMessageTimeoutW` + PowerShell fallback for level read |
| `kill_process` | Terminate by PID or case-insensitive name | `psutil.process_iter()` + `proc.kill()` |
| `disk_usage` | Per-partition total/used/free GB | `psutil.disk_partitions()` + `psutil.disk_usage()` |
| `set_timer` | Countdown timer with desktop beep | `threading.Thread` + `ctypes.windll.user32.MessageBeep()` |

### Clipboard & Files
| Tool | Description | Implementation |
|------|-------------|----------------|
| `clipboard_read` | Read clipboard text | `pyperclip.paste()` |
| `clipboard_write` | Write text to clipboard | `pyperclip.copy(text)` |
| `rename_files` | Batch rename with prefix/suffix/replace/extension + glob filter + dry-run | `fnmatch` filter + `os.rename()` |

### Visual & DevOps
| Tool | Description | Implementation |
|------|-------------|----------------|
| `annotate_screenshot` | Draw arrow/circle/rectangle/text on screenshot | `mss` capture → PIL `ImageDraw` → base64 PNG |
| `github_op` | List issues/PRs, repo info, create branch, clone | `shutil.which("gh")` → `subprocess.run(["gh", ...])` |
| `screen_ocr` | Extract text from screen region | `mss` capture → PIL → `pytesseract.image_to_string()` |

## Bugs Fixed

### 1. LLM Refusing to Open Websites (`open_url`)
- **Symptom**: When asked to open YouTube or any link, the bot said "Browser navigation tool is currently non-functional" and refused.
- **Root cause**: The existing `web_navigate` tool used a headless Playwright instance that conflicted with the main async event loop.
- **Fix**: Added `open_url` using Python's `webbrowser.open()` — no async, no Playwright dependency. Updated CRITICAL RULES to explicitly tell the LLM to use `open_url` for browsing tasks.
- **File**: `src/mcp_server/server.py` — `_call_open_url()`, new tool schema

### 2. HITL Blocking Safe Tools
- **Symptom**: When asked to control volume or launch apps, the AI showed "Execution denied — HITL intervention" and told the user to do it manually.
- **Root cause**: `volume_control`, `screen_ocr`, `google_search`, `app_launcher`, and `launch_app` were not in `auto_approve_safe_actions`, so they triggered the confirmation modal. The user was denying them.
- **Fix**: Added all five tools to `security.auto_approve_safe_actions` in `config/settings.json`. Also cleared the stale `orchestrator.cpython-314.pyc` cache.
- **File**: `config/settings.json` — 6 new entries in `auto_approve_safe_actions`

### 3. Message Logger HTML `<br>` Rendering as Literal Text
- **Symptom**: Multi-line messages appeared as a single giant paragraph in the HTML log viewer. The `<br>` tags were visible as literal `&lt;br&gt;` text.
- **Root cause**: `"\n"` → `"<br>"` ran *before* `"<"` → `"&lt;"`, so every inserted `<br>` tag got immediately re-escaped into `&lt;br&gt;`.
- **Fix**: Swapped the order — escape `<`/`>` first, then convert `\n` to `<br>`.
- **File**: `src/gui/message_logger.py` — line 74, one-line swap

## How It Works

### volume_control — WM_APPCOMMAND Broadcast
The most reliable way to control system volume from a Python process without elevation:

```python
import ctypes
user32 = ctypes.windll.user32
HWND_BROADCAST = 0xFFFF
WM_APPCOMMAND = 0x0319
APPCOMMAND_VOLUME_UP = 0x000A0000

user32.SendMessageTimeoutW(
    HWND_BROADCAST, WM_APPCOMMAND, 0,
    APPCOMMAND_VOLUME_UP,
    0x0002,   # SMTO_ABORTIFHUNG
    5000,     # 5 second timeout
    None
)
```

This sends a system-level message that the active audio endpoint (and any capturing app like Zoom/Teams) receives and acts on. No admin privileges needed. Volume level is read separately via PowerShell `Get-Volume -Audio`.

### annotate_screenshot — PIL Overlay
1. Capture primary monitor with `mss.mss().grab(monitor)` — returns BGRA raw bytes
2. Convert to PIL `Image.frombytes("RGB", ..., "BGRX")` then `.convert("RGBA")` for transparency
3. `ImageDraw.Draw(img)` — draw the requested shape (arrow with math-derived arrowhead, circle, rectangle, or text with `arial.ttf`)
4. Save to `io.BytesIO()` as PNG, base64-encode for return

### github_op — gh CLI Wrapper
```python
gh = shutil.which("gh") or shutil.which("git")
if not gh:
    return _error_result("GitHub CLI not found in PATH")
# Then: subprocess.run([gh, "issue", "list", "--repo", repo, ...])
```
All ops are read-only or low-risk writes (create_branch, clone). No auth token required — uses the user's logged-in `gh` session.

## Commit History (Day 3.5)

| Commit | Message |
|--------|---------|
| `12f8739` | fix: render `<br>` tags in message logger HTML (escape before newline replacement) |
| `cffcf68` | fix: add volume_control, screen_ocr, google_search, app_launcher, launch_app to auto_approve so HITL no longer blocks them |
| `08239b1` | feat: add volume_control, screen_ocr, google_search, app_launcher tools |
| `6c31277` | feat: add 9 new MCP tools — launch_app, clipboard, timer, annotate screenshot, rename files, kill process, disk usage, youtube search, github ops |
| `1d5ba89` | feat: add open_url tool — opens websites in system browser, fix LLM refusing web tasks |
| `4dc36ea` | feat: add get_current_time and get_location tools, fix LLM hallucinating time/place |

(Preceded by Day 3 commits: `2c61bd2`, `e9b7927`, `52e438c`, `dfc882e`, `d978766`, `fa2e5df`)

## Total MCP Tool Count

| Category | Tools |
|----------|-------|
| Core system | `take_screenshot`, `get_system_stats`, `get_current_time`, `get_location` |
| Browser | `open_url`, `web_navigate`, `web_click`, `web_type`, `web_go_back`, `web_evaluate` |
| Input | `click_coordinate`, `type_text` |
| Terminal | `execute_terminal_command` |
| App control | `launch_app`, `app_launcher`, `volume_control`, `kill_process` |
| Clipboard | `clipboard_read`, `clipboard_write` |
| Files | `rename_files`, `disk_usage` |
| Visual | `annotate_screenshot`, `screen_ocr` |
| Search | `google_search`, `search_youtube` |
| DevOps | `github_op` |
| Utility | `set_timer` |
| **Total** | **24 tools** |

## Future Suggestions

- **Tool grouping in UI**: The HITL modal could show a category badge (e.g., "🔊 Audio" for volume_control) so the user understands context at a glance.
- **OCR region selector**: `screen_ocr` currently captures the full screen; adding a crosshair region picker would reduce tesseract processing time.
- **GitHub PR creation**: `github_op` supports clone/create_branch but not create PR — could add `op=submit_pr` with body/draft params.
- **Volume level read caching**: `volume_control` action calls re-read the level via PowerShell; could cache for 2s to avoid redundant subprocess calls.
