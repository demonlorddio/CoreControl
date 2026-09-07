# CoreControl — AI Desktop Assistant

**CoreControl** is a secure, AI-powered desktop assistant that runs locally as a background Python daemon. It connects a Telegram remote interface and local voice input to your machine using a custom MCP (Model Context Protocol) server, OS automation hooks, and an online/offline reasoning engine.

---

## Architecture Overview

```
Telegram / Voice Input
        │
        ▼
  orchestrator.py  ◄──── Online: Claude API (cloud)
        │                Offline: Ollama (local LLM)
        ▼
  HITL Filter  ──► Telegram confirmation card
        │
        ▼
  MCP Server (stdio)
        │
  ┌─────┴──────┐
  │ screenshot │ click │ type │ stats │ terminal │
  └────────────┘
        │
  PyAutoGUI / mss / psutil / subprocess
        ▼
    Host OS
```

---

## Directory Structure

```
corecontrol/
├── config/
│   └── settings.json           # Config, whitelists, emergency controls
├── src/
│   ├── bridge/
│   │   └── telegram_bot.py     # Telegram gateway + HITL middleware
│   ├── gui/
│   │   ├── overlay.py          # Translucent PyQt6 floating overlay
│   │   └── audio_recorder.py   # Microphone VAD + offline transcription
│   ├── mcp_server/
│   │   └── server.py           # Local stdio MCP server
│   ├── orchestrator.py         # Agentic perception loop + LLM fallback
│   └── utils/
│       └── network.py          # Connectivity monitor
├── requirements.txt
└── README.md
```

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure settings

Edit `config/settings.json`:

```json
{
  "telegram": {
    "bot_token": "<YOUR_BOT_TOKEN>",
    "whitelisted_user_ids": [123456789]
  },
  "local_llm": {
    "api_base_url": "http://localhost:11434",
    "default_model": "qwen2.5-coder:7b"
  }
}
```

**Get your bot token**: Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and copy the token.  
**Get your user ID**: Message [@userinfobot](https://t.me/userinfobot).

### 3. Set the Anthropic API key (online mode)

```bash
# Windows
set ANTHROPIC_API_KEY=sk-ant-...

# macOS / Linux
export ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Install and start Ollama (offline mode)

Download from [ollama.com](https://ollama.com), then:

```bash
ollama pull qwen2.5-coder:7b
ollama serve          # starts on http://localhost:11434
```

### 5. Run CoreControl

**Interactive REPL (no Telegram)**:
```bash
python -m src.orchestrator
```

**With Telegram + overlay**:
```bash
python main.py
```

---

## Platform Setup

### Windows

PyAutoGUI works without extra configuration on Windows.

For `pytesseract` (offline OCR fallback):
1. Download [Tesseract-OCR installer](https://github.com/UB-Mannheim/tesseract/wiki).
2. Add the install path to `PATH`, or set:
   ```python
   pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
   ```

For `sounddevice`, install PortAudio:
```bash
pip install sounddevice
# PortAudio is bundled in the Windows wheel — no extra steps.
```

---

### macOS — Quartz & Assistive Technology Permissions

macOS requires explicit permission grants before synthetic input events or screen capture work.

#### Screen Recording (mss / screenshots)
1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**.
2. Click **+** and add your terminal application (e.g., Terminal, iTerm2, VS Code).
3. Restart the terminal.

#### Accessibility / Input Control (PyAutoGUI)
1. Open **System Settings → Privacy & Security → Accessibility**.
2. Click **+** and add your terminal (or Python binary).
3. If running from a virtual environment: add `.venv/bin/python` explicitly.

#### Quartz Event Services (advanced)
Low-level keystroke injection uses Apple's `Quartz Event Services` (Carbon API).  
Install the `pyobjc` bridge if needed:
```bash
pip install pyobjc-framework-Quartz
```

Verify access is granted:
```bash
python -c "import pyautogui; pyautogui.position()"
# Should print cursor coordinates without raising PermissionError.
```

---

### Linux — Wayland Input

Wayland's security model blocks traditional `XTEST`/`xdotool` input injection. Two options:

#### Option A — Wayland Portal (`wdotool`)

`wdotool` communicates through Wayland's **XDG RemoteDesktop portal** using the `libei` input-emulation protocol.

```bash
# Arch
sudo pacman -S wdotool

# Build from source
git clone https://github.com/mctechnology17/wdotool
cd wdotool && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make && sudo make install
```

Required libraries:
```bash
sudo apt install libwayland-client0 libxkbcommon-dev libeis-dev
```

Usage:
```bash
wdotool type "hello world"
wdotool click 1          # left click
wdotool mousemove 500 300
```

Grant RemoteDesktop portal access when prompted by your compositor (GNOME, KDE, etc.).

#### Option B — `/dev/uinput` Kernel Driver

The Linux kernel's `uinput` module lets you create a virtual input device with full root-equivalent capabilities.

**1. Load the module:**
```bash
sudo modprobe uinput

# Auto-load on boot
echo 'uinput' | sudo tee /etc/modules-load.d/uinput.conf
```

**2. Configure udev permissions:**
```bash
sudo tee /etc/udev/rules.d/99-uinput.rules <<'EOF'
KERNEL=="uinput", GROUP="uinput", MODE="0660"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

**3. Add your user to the `uinput` group:**
```bash
sudo groupadd -f uinput
sudo usermod -aG uinput $USER
# Log out and back in, then verify:
groups $USER   # should include 'uinput'
```

**4. Install the Python `uinput` library:**
```bash
pip install python-uinput
```

#### X11 fallback (non-Wayland)

On X11 sessions, PyAutoGUI works natively. Ensure `xdotool` is installed for any shell-level automation:
```bash
sudo apt install xdotool
```

---

## Configuration Reference

`config/settings.json` controls all runtime behaviour:

| Key | Description |
|-----|-------------|
| `telegram.bot_token` | Telegram bot token from @BotFather |
| `telegram.whitelisted_user_ids` | List of numeric Telegram user IDs allowed to control the assistant |
| `telegram.enable_hitl` | Enable Human-In-The-Loop confirmation for risky actions |
| `telegram.hitl_timeout_seconds` | Seconds to wait for HITL decision before auto-denying (default: 300) |
| `failsafe.enable_pyautogui_failsafe` | Move mouse to top-left corner to abort all actions |
| `terminal.whitelist` | Command prefixes that are allowed to execute |
| `terminal.blacklist` | Command fragments that are always blocked |
| `terminal.timeout_seconds` | Max seconds a shell command may run (default: 30) |
| `local_llm.api_base_url` | Ollama server URL (default: `http://localhost:11434`) |
| `local_llm.default_model` | Primary model for offline mode |
| `security.require_confirmation_for_actions` | Tools that require HITL approval |
| `security.auto_approve_safe_actions` | Tools that run without confirmation |
| `audio.vad_threshold` | RMS energy threshold for voice activity detection (0.0–1.0) |
| `audio.transcription_model` | Whisper model size: `tiny`, `base.en`, `small`, `medium` |

---

## Human-In-The-Loop (HITL) Flow

When CoreControl is about to execute a high-risk action (e.g., run a terminal command, click, or type), it:

1. Pauses execution.
2. Sends a confirmation card to your Telegram chat with:
   - Tool name and arguments.
   - A screenshot of the current screen state.
3. Waits for your decision via inline keyboard:
   - **✅ Approve** — executes exactly as planned.
   - **✏️ Edit** — prompts you to send modified JSON arguments, then executes.
   - **❌ Deny** — cancels the action and sends an error back to the orchestrator.

Configure which tools require HITL in `security.require_confirmation_for_actions`.

---

## Security Notes

- **Whitelist is enforced server-side** in the MCP layer — commands are validated before any `subprocess` call.
- **Telegram authentication** silently drops all messages from non-whitelisted user IDs.
- **PyAutoGUI fail-safe** is enabled by default — moving the mouse to the top-left corner aborts all active automation.
- The MCP server writes **all logs to `stderr`** — `stdout` is reserved for JSON-RPC transport only.
- Secrets (API keys, bot tokens) should be stored as **environment variables** and referenced in `settings.json` via a secrets manager in production deployments.

---

## Troubleshooting

**`PermissionError` on macOS when taking screenshots or moving the mouse**  
→ Grant Accessibility and Screen Recording permissions as described above.

**`sounddevice` cannot open microphone on Linux**  
→ Install PulseAudio or PipeWire dev headers: `sudo apt install portaudio19-dev`

**Ollama returns connection refused**  
→ Run `ollama serve` in a separate terminal and confirm it listens on port 11434.

**Telegram bot not responding**  
→ Check that `bot_token` is set and your Telegram user ID is in `whitelisted_user_ids`.

**`faster-whisper` model download fails in offline mode**  
→ Pre-download the model while online: `python -c "from faster_whisper import WhisperModel; WhisperModel('base.en')"`  
   Models are cached in `~/.cache/huggingface/hub/`.
