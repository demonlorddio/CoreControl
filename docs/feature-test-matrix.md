# CoreControl — Feature Test Matrix

**Generated:** 2026-09-08 (updated)  
**Version:** 1.1  
**Base:** Great Sage Desktop Companion

---

## Critical Prerequisites (Blocking Features)

These must be installed for full functionality:

```bash
# Install Playwright browsers (blocks all 6 web tools)
playwright install chromium

# Install Tesseract OCR binary (blocks offline OCR fallback)
# Windows: download from https://github.com/UB-Mannheim/tesseract/wiki
# or: winget install Gyan.TesseractOCR
```

---

## Legend

| Status | Meaning |
|--------|---------|
| ✅ **Perfect** | Fully functional, tested and working |
| ⚠️ **Possible but Lacking** | Works in principle but has gaps, limitations, or known issues |
| 🔲 **Not Yet Implemented** | Planned or referenced but not built |

---

## 1. Overlay & UI

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1.1 | Animated GIF avatar (Great Sage) | ✅ Perfect | `QMovie` plays `great-sage-transparent-clean.gif` with smooth frame caching |
| 1.2 | State indicator dot (color-coded) | ✅ Perfect | Blue=LISTENING, Amber=PROCESSING, Teal=SPEAKING, Dim Blue=IDLE |
| 1.3 | Blink animation (idle state) | ✅ Perfect | Random blink overlay with 120ms eyelid lines; disabled during PROCESSING |
| 1.4 | Processing state color shift | ✅ Perfect | Hue oscillates ±60° around 200° with 30ms timer |
| 1.5 | Listening state pulse | ✅ Perfect | Subtle hue oscillation ±20° on blue |
| 1.6 | Bounce animation | ⚠️ Possible but Lacking | Timer disabled by default; available via `set_bounce(True)` but not wired to any state |
| 1.7 | Drag to reposition | ✅ Perfect | Left-click + drag; global position tracking |
| 1.8 | Always-on-top window | ✅ Perfect | `Qt.WindowStaysOnTopHint` |
| 1.9 | Transparent background (no box) | ✅ Perfect | `paintEvent` is `pass`; `WA_TranslucentBackground` |
| 1.10 | Frameless window | ✅ Perfect | `Qt.FramelessWindowHint` |
| 1.11 | System tray icon + menu | ✅ Perfect | Blue 16×16 icon; Show/Hide/Always on Top/Quit actions; double-click shows window |

---

## 2. Speech Bubble

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 2.1 | Text display | ✅ Perfect | Monospace Consolas, auto-resizes to content |
| 2.2 | Auto-hide after timeout | ✅ Perfect | 8-second single-shot timer by default |
| 2.3 | Custom duration | ✅ Perfect | `speak(text, duration_ms=8000)` — pass 0 for no auto-hide |
| 2.4 | Repositioning | ✅ Perfect | Bubbles to the right of avatar; clamps to widget bounds |
| 2.5 | Multi-line support | ✅ Perfect | Splits on `\n`, wraps correctly |
| 2.6 | Clear on state change | ⚠️ Possible but Lacking | Clears on LISTENING and IDLE transitions, but not on PROCESSING→SPEAKING switch |

---

## 3. Voice Input

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 3.1 | Ctrl+Space hotkey trigger | ✅ Perfect | Global hotkey via `pynput`; works across all windows |
| 3.2 | Right-click avatar trigger | ✅ Perfect | Emits `transcription_requested` signal |
| 3.3 | Voice Activity Detection (VAD) | ⚠️ Possible but Lacking | Simple RMS energy-based; may miss quiet speech or capture ambient noise |
| 3.4 | Offline Whisper transcription | ✅ Perfect | `faster-whisper` with `base.en` model; fully offline |
| 3.5 | Background capture thread | ✅ Perfect | `sounddevice` InputStream in daemon thread |
| 3.6 | Silence-terminated utterances | ✅ Perfect | 1000ms silence threshold; accumulates speech frames |
| 3.7 | Transcription callback to orchestrator | ✅ Perfect | `asyncio.run_coroutine_threadsafe` bridge to event loop |
| 3.8 | Whisper model selection | ⚠️ Possible but Lacking | Configurable in `settings.json` but only `base.en` tested; `small.en` unverified |

---

## 4. Text Prompt Input

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 4.1 | Inline text input field | ✅ Perfect | 180×26 Consolas QLineEdit below avatar |
| 4.2 | Enter to submit | ✅ Perfect | `returnPressed` signal routed to orchestrator |
| 4.3 | Placeholder text | ✅ Perfect | "Type a prompt…" |
| 4.4 | Auto-clear after submit | ✅ Perfect | Input cleared immediately on submit |

---

## 5. LLM Engines

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 5.1 | Online engine (OmniRoute/OpenAI) | ✅ Perfect | `AsyncOpenAI` client; vision support with base64 screenshots |
| 5.2 | Offline engine (Ollama) | ⚠️ Possible but Lacking | JSON-parsed `TOOL_CALL:` lines; no native tool calling — limited to text responses |
| 5.3 | Automatic engine selection | ✅ Perfect | Network monitor switches between online/offline |
| 5.4 | Ollama model fallback chain | ⚠️ Possible but Lacking | Configured for `qwen2.5-coder:7b` → `llama3.2` → `codellama`; only first model verified |
| 5.5 | Timeout handling | ✅ Perfect | 60s timeout on both engines |
| 5.6 | Vision (screenshot input) | ✅ Perfect | Base64 JPEG passed as `image_url` to online engine |
| 5.7 | OCR fallback for offline | ⚠️ Possible but Lacking | `pytesseract` Python package installed, but **Tesseract binary not found on system** — OCR silently returns "[Screen: OCR not available]" |

---

## 6. MCP Tools (13 total)

### System Tools

| # | Tool | Status | Notes |
|---|------|--------|-------|
| 6.1 | `take_screenshot` | ✅ Perfect | `mss` library; returns base64 JPEG (quality=75); multi-monitor support |
| 6.2 | `get_system_stats` | ✅ Perfect | CPU, RAM, battery, active window, top 50 processes via `psutil` |
| 6.3 | `click_coordinate` | ✅ Perfect | `pyautogui` with configurable button and click count |
| 6.4 | `type_text` | ✅ Perfect | `pyautogui.typewrite` with adjustable keystroke interval |
| 6.5 | `execute_terminal_command` | ⚠️ Possible but Lacking | Whitelist/blacklist filtering works; but limited command set (no `mkdir`, `cp`, `python`, etc.) |

### Web Browsing Tools

| # | Tool | Status | Notes |
|---|------|--------|-------|
| 6.6 | `web_navigate` | ⚠️ Possible but Lacking | Playwright Python package installed, but **browsers not installed** (`playwright install` not run) — web tools will fail |
| 6.7 | `web_click` | ⚠️ Possible but Lacking | Same Playwright browser issue as web_navigate |
| 6.8 | `web_type` | ⚠️ Possible but Lacking | Same Playwright browser issue |
| 6.9 | `web_get_text` | ⚠️ Possible but Lacking | Same Playwright browser issue |
| 6.10 | `web_screenshot` | ⚠️ Possible but Lacking | Same Playwright browser issue |
| 6.11 | `web_fetch` | ✅ Perfect | Uses `requests` library (no browser needed); tested working |
| 6.12 | `web_go_back` | ⚠️ Possible but Lacking | Same Playwright browser issue |
| 6.13 | `web_evaluate` | ⚠️ Possible but Lacking | Same Playwright browser issue |

### Tool Integration

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 6.14 | MCP stdio protocol | ✅ Perfect | Async subprocess with 1MB buffer; proper JSON-RPC 2.0 |
| 6.15 | Tool definitions auto-discovery | ✅ Perfect | `tools/list` RPC at startup populates LLM tool schema |
| 6.16 | Multi-iteration agentic loop | ✅ Perfect | Up to 10 iterations of LLM→tool→result loop |
| 6.17 | Post-action screenshot | ✅ Perfect | Screenshot taken after each tool batch for visual feedback |

---

## 7. HITL (Human-In-The-Loop)

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 7.1 | Confirmation modal for high-risk tools | ✅ Perfect | Dark themed `QDialog` with Approve/Edit/Deny buttons |
| 7.2 | Edit arguments dialog | ✅ Perfect | JSON editor with validation fallback |
| 7.3 | Screenshot on confirmation | ✅ Perfect | Includes desktop screenshot in modal context |
| 7.4 | Auto-approve safe tools | ✅ Perfect | `take_screenshot` and `get_system_stats` bypass HITL |
| 7.5 | 300-second timeout | ✅ Perfect | Auto-denies after 5 minutes |
| 7.6 | Cross-thread signal safety | ✅ Perfect | `run_coroutine_threadsafe` for GUI thread bridging |
| 7.7 | Configurable HITL tools | ⚠️ Possible but Lacking | List in `settings.json` but not dynamic — requires restart to change |

---

## 8. Security & Configuration

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 8.1 | Terminal command whitelist | ✅ Perfect | Checks prefix matches against config list |
| 8.2 | Terminal command blacklist | ✅ Perfect | Exact substring matches; takes priority over whitelist |
| 8.3 | PyAutoGUI fail-safe | ✅ Perfect | Move mouse to top-left corner aborts any action |
| 8.4 | Configurable timeouts | ✅ Perfect | Terminal (30s), LLM (60s), HITL (300s) |
| 8.5 | Multi-monitor screenshot | ✅ Perfect | `mss` monitor index selection |

---

## 9. Network & Connectivity

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 9.1 | Connectivity probing | ✅ Perfect | Probes Google/Cloudflare/OpenDNS DNS (port 53) |
| 9.2 | Latency reporting | ✅ Perfect | Millisecond-precision round-trip to probe host |
| 9.3 | State change logging | ✅ Perfect | Logs when online/offline transitions occur |
| 9.4 | 15-second re-check interval | ✅ Perfect | Configurable via `NetworkMonitor(poll_interval)` |

---

## 10. Conversation & Memory

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 10.1 | In-memory conversation history | ✅ Perfect | Up to 40 messages, appended after each prompt |
| 10.2 | History sliding window | ✅ Perfect | Trims to last 40 entries when exceeded |
| 10.3 | Cross-turn context | ⚠️ Possible but Lacking | History is session-only; lost on restart — no disk persistence |
| 10.4 | Clear history command | ✅ Perfect | `orch.clear_history()` available via CLI mode |
| 10.5 | Link extraction from responses | ✅ Perfect | Regex finds URLs in LLM text output; deduplicated |
| 10.6 | Screenshot artifact collection | ✅ Perfect | All screenshots from tool calls stored in `ProcessResult.screenshots` |

---

## 11. Persona & Output

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 11.1 | Great Sage system prompt | ✅ Perfect | "Notice.", "Report.", "Analysis completed." prefixes |
| 11.2 | "Master" address | ✅ Perfect | Enforced via system prompt |
| 11.3 | Concise authoritative tone | ✅ Perfect | No hedging, logical structure |
| 11.4 | Tool descriptions injected | ✅ Perfect | Dynamic injection from MCP tool list |
| 11.5 | ProcessResult text delivery | ✅ Perfect | Orchestration result returned as structured dataclass |

---

## 12. Known Gaps & Future Work

| # | Gap | Severity | Description |
|---|-----|----------|-------------|
| 12.1 | Bounce animation unused | Low | Timer disabled; no state triggers it |
| 12.2 | OCR unavailable | Medium | `pytesseract` package installed but **Tesseract binary not on PATH** — offline screenshots lack text context |
| 12.3 | Web browsing unavailable | Medium | **Playwright browsers not installed** — all 6 web tools non-functional until `playwright install chromium` is run |
| 12.4 | Terminal command whitelist narrow | Medium | Missing common tools (`mkdir`, `python`, `npm install`, etc.) |
| 12.5 | No persistent memory | Medium | Conversation history lost on restart — no disk persistence |
| 12.6 | VAD is simplistic | Low | RMS energy-based; no noise suppression or echo cancellation |
| 12.7 | Telegram bridge deprecated | Low | Code present but inactive; no cleanup |
| 12.8 | No log rotation | Low | Logs to stderr only; no file logging configured |
| 12.9 | Offline tool calling limited | Medium | Ollama uses text-based `TOOL_CALL:` protocol instead of native function calling |

---

## 13. Quick Summary

| Category | Perfect | Lacking | Not Built |
|----------|---------|---------|-----------|
| Overlay & UI | 8 | 1 | 0 |
| Speech Bubble | 5 | 1 | 0 |
| Voice Input | 5 | 2 | 0 |
| Text Prompt | 4 | 0 | 0 |
| LLM Engines | 4 | 2 | 0 |
| MCP Tools | 8 | 5 | 0 |
| HITL | 6 | 1 | 0 |
| Security | 5 | 0 | 0 |
| Network | 4 | 0 | 0 |
| Conversation | 3 | 2 | 0 |
| Persona | 5 | 0 | 0 |
| **TOTAL** | **57** | **14** | **0** |

---

*This matrix is a living document. Update statuses as features are tested and improved.*
