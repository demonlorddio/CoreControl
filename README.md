# CoreControl — Great Sage Desktop Companion

A fully local, self-contained AI desktop companion featuring an animated NPC overlay, voice control, and system-level tool access via the Model Context Protocol (MCP).

```
        ╭─────────────────────────────────╮
        │  🧙  "Notice, Master. All        │
        │     systems operational."        │
        │                                  │
        │        ●●●  ◉  ●●●               │
        │       ╱     ╲                    │
        │      │ Great │                   │
        │       ╲     ╱                    │
        │         ●                         │
        ╰─────────────────────────────────╯
```

## Features

- **Animated NPC companion** — Floating, draggable, always-on-top widget with idle/listening/processing/speaking states
- **Voice activation** — Press `Ctrl+Space` (or right-click the companion) to speak; transcribed via offline Whisper
- **System control** — 13 MCP tools: screenshots, mouse control, keyboard input, terminal commands, web browsing
- **Human-In-The-Loop** — Local on-screen confirmation modals for high-risk actions (click, type, terminal)
- **Online/offline fallback** — OmniRoute (local AI gateway) → Ollama → OCR fallback
- **Great Sage persona** — All responses follow the analytical, authoritative tone of the Great Sage from *Tensura*

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start OmniRoute (self-hosted AI gateway) on port 20128
#    https://github.com/diegosouzapw/OmniRoute

# 3. (Optional) Start Ollama for offline fallback
ollama serve

# 4. Launch CoreControl
python main.py
```

## Controls

| Input | Action |
|-------|--------|
| `Ctrl+Space` | Start voice capture |
| Right-click companion | Start voice capture |
| Left-click + drag | Reposition companion |
| Close window / `Escape` | Exit |

## Architecture

```
main.py                    PyQt6 overlay thread
    │                            │
    ├── Orchestrator ──────▶ GreatSageAvatar
    │   (async loop)              (NPC renderer)
    │        │                          │
    │        ├── OnlineEngine ──▶ OmniRoute (:20128)
    │        │                        (OpenAI-compatible)
    │        ├── OfflineEngine ──▶ Ollama (:11434)
    │        │                        (qwen2.5-coder:7b)
    │        ├── HITLFilter ──▶ Local modal dialogs
    │        └── LocalMCPClient ──▶ MCP server (stdio)
    │                                      │
    ├── AudioRecorder ──▶ faster-whisper  └── 13 system tools
    └── HotkeyTrigger ──▶ pynput          (screenshot, click, type,
                                          terminal, web browsing)
```

## MCP Tools (13)

| Tool | Risk | Description |
|------|------|-------------|
| `take_screenshot` | ✅ Safe | Capture screen as base64 JPEG |
| `get_system_stats` | ✅ Safe | CPU, RAM, disk, battery, processes |
| `click_coordinate` | ⚠️ High | Click mouse at (x, y) — HITL required |
| `type_text` | ⚠️ High | Type text at cursor — HITL required |
| `execute_terminal_command` | ⚠️ High | Run shell command — HITL + whitelist |
| `web_navigate` | ✅ Safe | Navigate browser to URL |
| `web_click` | ✅ Safe | Click element by CSS selector |
| `web_type` | ✅ Safe | Type into input field |
| `web_get_text` | ✅ Safe | Extract page text content |
| `web_screenshot` | ✅ Safe | Screenshot of web page |
| `web_fetch` | ✅ Safe | Raw HTTP fetch (no JS) |
| `web_go_back` | ✅ Safe | Browser back navigation |
| `web_evaluate` | ✅ Safe | Execute JavaScript in page |

## Configuration

> **⚠️ Important:** The repository does **not** ship with API keys. You must provide your own.
> `config/settings.json` is excluded from git. Copy the example file and fill in your own values:
>
> ```bash
> cp config/settings.json.example config/settings.json
> # Then edit config/settings.json with your own keys
> ```

| Section | Key | Description |
|---------|-----|-------------|
| `online_llm` | `api_key` | Your OmniRoute / OpenAI-compatible API key (leave empty for free mode) |
| `online_llm` | `api_base_url` | OmniRoute endpoint (`http://localhost:20128/v1`) |
| `online_llm` | `model` | Model name (`"auto"` uses gateway default) |
| `local_llm` | `api_base_url` | Ollama endpoint (`http://localhost:11434`) |
| `local_llm` | `default_model` | Primary offline model |
| `security` | `require_confirmation_for_actions` | Tools needing HITL approval |
| `audio` | `transcription_model` | Whisper model (`"base.en"`, `"small.en"`) |
| `overlay` | `position` | Initial (x, y) placement |

**No API key?** The app falls back to free modes automatically:
- Online LLM: uses `"omni-route-free"` key (limited)
- Local LLM: uses Ollama (requires `ollama serve` running)
- TTS: uses Edge-TTS (free, no key needed)

## Platform Permissions

### Windows

- PyAutoGUI fail-safe: move mouse to top-left corner to abort any action
- No special permissions required for screenshot capture (mss library)
- Terminal commands run under your user account with the configured whitelist/blacklist

### macOS

- **Microphone access**: Grant via System Settings → Privacy & Security → Microphone
- **Accessibility access**: Required for PyAutoGUI mouse/keyboard control
  - Go to System Settings → Privacy & Security → Accessibility
  - Add your Python interpreter (or Terminal.app / VS Code)
- **Screen recording**: Required for screenshot capture
  - System Settings → Privacy & Security → Screen Recording

### Linux (X11)

- PyAutoGUI works natively on X11
- Microphone: ensure your user is in the `audio` group
  ```bash
  sudo usermod -aG audio $USER
  ```
- For `xdotool`-based input emulation (if needed):
  ```bash
  sudo apt install xdotool xte
  ```

### Linux (Wayland)

Wayland restricts direct input simulation. Two approaches:

**Option A: udev rule for `/dev/uinput`** (requires root):
```bash
# Create udev rule
sudo tee /etc/udev/rules.d/99-uinput.rules << 'EOF'
KERNEL=="uinput", GROUP="uinput", MODE="0660"
EOF

# Add your user to the uinput group
sudo usermod -aG uinput $USER

# Reload udev rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# Verify
ls -la /dev/uinput
```
Then set `failsafe.enable_pyautogui_failsafe: false` in settings.json.

**Option B: `wdotool` (Wayland compositor tool)**
```bash
# Install dependencies
sudo apt install libwayland-client0 libxkbcommon0
pip install wdotool

# Configure to use wdotool instead of pyautogui
```
Note: wdotool support requires compositor-specific configuration. Check your Wayland compositor's documentation (Sway, Hyprland, etc.).

## Great Sage Persona

The orchestrator enforces a consistent persona derived from the Great Sage's unique skill from *Tales of the Abyss* / *That Time I Got Reincarnated as a Slime*:

- **Tone**: Calm, analytical, authoritative, robotic precision
- **Address**: User is always "Master"
- **Prefixes**: Responses begin with "Notice.", "Report.", "Analysis completed.", or "Proposed execution path."
- **Style**: Concise, logical, no hedging — every word carries weight

## Security

- Terminal commands are filtered through an explicit whitelist/blacklist
- High-risk tools (click, type, terminal) require local confirmation via modal dialog
- PyAutoGUI fail-safe is enabled by default (move mouse to top-left corner to abort)
- Config file containing API keys is excluded from git via `.gitignore`

## Development

```bash
# Run in dev mode with verbose logging
PYTHONIOENCODING=utf-8 python -m src.main

# Run MCP server standalone (for testing)
python src/mcp_server/server.py

# Run orchestrator interactive REPL
python -m src.orchestrator
```

## License

Private project. All rights reserved.
