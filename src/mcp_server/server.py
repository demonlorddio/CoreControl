"""
Local stdio MCP Server for CoreControl.
Exposes system control primitives to the orchestrator via the MCP protocol.

IMPORTANT: All logging goes to stderr only. stdout is reserved for JSON-RPC
transport — any non-JSON bytes written there corrupt the protocol.
"""

from __future__ import annotations

import base64
import datetime
import fnmatch
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import mcp.server.stdio
import psutil
import pyautogui
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)
from mss import mss
from PIL import Image

# ── Web browsing state ────────────────────────────────────────────────────────
_browser = None
_page = None


def _get_browser():
    """Lazy-init Playwright browser instance."""
    global _browser, _page
    if _browser is None:
        from playwright.async_api import async_playwright
        _pw = async_playwright().start()
        _browser = _pw.__await_send(None)  # type: ignore
        _browser = None  # We'll use sync for simplicity
    return _browser

# ── Logging: stderr ONLY ────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("corecontrol.mcp_server")

# ── Settings ─────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "settings.json"


def _load_settings() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("Could not load settings.json: %s — using defaults", exc)
        return {}


_settings = _load_settings()
_terminal_cfg = _settings.get("terminal", {})
_WHITELIST: list[str] = _terminal_cfg.get("whitelist", [])
_BLACKLIST: list[str] = _terminal_cfg.get("blacklist", [])
_CMD_TIMEOUT: int = _terminal_cfg.get("timeout_seconds", 30)
_MAX_OUTPUT: int = _terminal_cfg.get("max_output_length", 10_000)

# Disable PyAutoGUI fail-safe only if explicitly turned off in config
_failsafe_cfg = _settings.get("failsafe", {})
pyautogui.FAILSAFE = _failsafe_cfg.get("enable_pyautogui_failsafe", True)
pyautogui.PAUSE = 0.05  # slight delay between actions

# ── MCP Server init ──────────────────────────────────────────────────────────

async def handle_list_tools(context, params) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def handle_call_tool(context, params) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    logger.info("Tool called: %s  args=%s", name, arguments)
    try:
        if name == "take_screenshot":
            return _call_take_screenshot(arguments)
        elif name == "click_coordinate":
            return _call_click_coordinate(arguments)
        elif name == "type_text":
            return _call_type_text(arguments)
        elif name == "get_system_stats":
            return _call_get_system_stats(arguments)
        elif name == "get_current_time":
            return _call_get_current_time(arguments)
        elif name == "get_location":
            return _call_get_location(arguments)
        elif name == "execute_terminal_command":
            return _call_execute_terminal_command(arguments)
        elif name == "web_navigate":
            return _call_web_navigate(arguments)
        elif name == "web_click":
            return _call_web_click(arguments)
        elif name == "web_type":
            return _call_web_type(arguments)
        elif name == "web_get_text":
            return _call_web_get_text(arguments)
        elif name == "web_screenshot":
            return _call_web_screenshot(arguments)
        elif name == "web_fetch":
            return _call_web_fetch(arguments)
        elif name == "web_go_back":
            return _call_web_go_back(arguments)
        elif name == "web_evaluate":
            return _call_web_evaluate(arguments)
        elif name == "open_url":
            return _call_open_url(arguments)
        elif name == "launch_app":
            return _call_launch_app(arguments)
        elif name == "clipboard_read":
            return _call_clipboard_read(arguments)
        elif name == "clipboard_write":
            return _call_clipboard_write(arguments)
        elif name == "set_timer":
            return _call_set_timer(arguments)
        elif name == "annotate_screenshot":
            return _call_annotate_screenshot(arguments)
        elif name == "rename_files":
            return _call_rename_files(arguments)
        elif name == "kill_process":
            return _call_kill_process(arguments)
        elif name == "disk_usage":
            return _call_disk_usage(arguments)
        elif name == "search_youtube":
            return _call_search_youtube(arguments)
        elif name == "github_op":
            return _call_github_op(arguments)
        elif name == "volume_control":
            return _call_volume_control(arguments)
        elif name == "screen_ocr":
            return _call_screen_ocr(arguments)
        elif name == "google_search":
            return _call_google_search(arguments)
        elif name == "app_launcher":
            return _call_app_launcher(arguments)
        else:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))], isError=True)
    except Exception as exc:
        logger.exception("Unhandled error in tool %s", name)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": str(exc)}))], isError=True)


server = Server(
    "corecontrol-mcp",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# ── Tool schemas ─────────────────────────────────────────────────────────────
TOOLS: list[Tool] = [
    Tool(
        name="take_screenshot",
        description=(
            "Capture the primary system display and return it as a base64-encoded "
            "JPEG string suitable for LLM Vision analysis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_index": {
                    "type": "integer",
                    "description": "0-based monitor index (0 = primary).",
                    "default": 0,
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="click_coordinate",
        description="Move the mouse to (x, y) and click.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Horizontal pixel coordinate."},
                "y": {"type": "integer", "description": "Vertical pixel coordinate."},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button to press.",
                    "default": "left",
                },
                "clicks": {
                    "type": "integer",
                    "description": "Number of clicks (1 = single, 2 = double).",
                    "default": 1,
                },
            },
            "required": ["x", "y"],
        },
    ),
    Tool(
        name="type_text",
        description="Type a string into the currently focused window.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
                "interval_ms": {
                    "type": "integer",
                    "description": "Delay between keystrokes in milliseconds.",
                    "default": 25,
                },
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="get_system_stats",
        description=(
            "Return active window title, running process list, battery level, "
            "CPU usage, and memory usage."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_processes": {
                    "type": "boolean",
                    "description": "Whether to include the full process list.",
                    "default": True,
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="get_current_time",
        description=(
            "Return the current local date, time, and timezone of this machine. "
            "Use this whenever the user asks about the current time or date."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="get_location",
        description=(
            "Return the geographic location of this machine based on its public IP address. "
            "Includes city, region, country, timezone, and coordinates. "
            "Use this whenever the user asks about location, weather context, or timezone."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="execute_terminal_command",
        description=(
            "Execute a shell command and return its stdout/stderr output. "
            "Only whitelisted commands are permitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Working directory for the command (optional).",
                },
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="web_navigate",
        description="Navigate a headless browser to a URL and return page title and content summary.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to wait for page load (default: 2).",
                    "default": 2,
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="web_click",
        description="Click an element on the current web page by CSS selector.",
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click (e.g., '#login-btn', '.submit').",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for element (default: 5).",
                    "default": 5,
                },
            },
            "required": ["selector"],
        },
    ),
    Tool(
        name="web_type",
        description="Type text into an input field on the current web page.",
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input field.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type.",
                },
                "clear_first": {
                    "type": "boolean",
                    "description": "Clear existing text first (default: True).",
                    "default": True,
                },
            },
            "required": ["selector", "text"],
        },
    ),
    Tool(
        name="web_get_text",
        description="Extract text content from the current web page or a specific element.",
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector to extract from (omit for full page).",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Max characters to return (default: 5000).",
                    "default": 5000,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="web_screenshot",
        description="Take a screenshot of the current web page.",
        inputSchema={
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture full page including scrolled content (default: False).",
                    "default": False,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="web_fetch",
        description="Fetch a URL directly (no browser) and return HTML/content. Good for APIs and simple pages.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default: 15).",
                    "default": 15,
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="web_go_back",
        description="Go back to the previous page in browser history.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="web_evaluate",
        description="Execute JavaScript on the current web page and return the result.",
        inputSchema={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "JavaScript code to execute.",
                },
            },
            "required": ["script"],
        },
    ),
    Tool(
        name="open_url",
        description=(
            "Open a URL in the system's default web browser. "
            "Use this whenever the user asks you to browse a website, open a link, "
            "visit a page, or show them something on the web. "
            "Do NOT use web_navigate for this — use open_url instead to open the user's own browser."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to open (must start with http:// or https://).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional human-readable label for the action (e.g. 'Opening YouTube', 'Viewing GitHub repo').",
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="launch_app",
        description=(
            "Launch an application or open a file by name or path. "
            "Use this to open programs (e.g. 'notepad', 'chrome', 'code') "
            "or files (e.g. 'C:\\Users\\Me\\doc.pdf'). "
            "On Windows uses Start-Process; on Linux uses xdg-open."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Application name, executable, or file path to launch.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional command-line arguments.",
                },
            },
            "required": ["target"],
        },
    ),
    Tool(
        name="clipboard_read",
        description="Read the current text content from the system clipboard.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="clipboard_write",
        description=(
            "Write text to the system clipboard. "
            "Use this when Master wants to copy text to share it elsewhere."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text content to copy to clipboard.",
                },
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="set_timer",
        description=(
            "Set a countdown timer that triggers a desktop notification when it expires. "
            "Use this when the user asks to 'set a timer for X minutes' or 'remind me in X minutes'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "Duration in minutes (1-120).",
                },
                "label": {
                    "type": "string",
                    "description": "Optional label shown in the notification (e.g. 'Timer', 'Break time').",
                },
            },
            "required": ["minutes"],
        },
    ),
    Tool(
        name="annotate_screenshot",
        description=(
            "Take a screenshot and draw an annotation (arrow, circle, or rectangle) on it. "
            "Returns the annotated image as base64. "
            "Use this when Master wants to highlight something on screen."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "enum": ["arrow", "circle", "rectangle", "text"],
                    "description": "Type of annotation.",
                },
                "start_x": {"type": "integer", "description": "Start X pixel coordinate."},
                "start_y": {"type": "integer", "description": "Start Y pixel coordinate."},
                "end_x": {"type": "integer", "description": "End X pixel coordinate (for arrow/rect)."},
                "end_y": {"type": "integer", "description": "End Y pixel coordinate (for arrow/rect)."},
                "radius": {"type": "integer", "description": "Circle radius in pixels."},
                "color": {
                    "type": "string",
                    "description": "RGB color as 'r,g,b' (default: 255,0,0 red).",
                },
                "text": {
                    "type": "string",
                    "description": "Text label to draw (for shape='text').",
                },
                "thickness": {"type": "integer", "description": "Line thickness in pixels (default: 3).", "default": 3},
            },
            "required": ["shape", "start_x", "start_y"],
        },
    ),
    Tool(
        name="rename_files",
        description=(
            "Rename files in a directory by applying a pattern. "
            "Supports prefix, suffix, replace (find/replace substring), and extension change. "
            "Dry-run mode returns proposed names without renaming."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Path to the directory containing files to rename.",
                },
                "pattern": {
                    "type": "string",
                    "enum": ["prefix", "suffix", "replace", "extension"],
                    "description": "Rename operation type.",
                },
                "value": {
                    "type": "string",
                    "description": "Value to apply (prefix/suffix text, find/replace pair as 'old=new', or new extension).",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter (e.g. '*.jpg'). Defaults to all files.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, show what would happen without renaming.",
                    "default": True,
                },
            },
            "required": ["directory", "pattern", "value"],
        },
    ),
    Tool(
        name="kill_process",
        description=(
            "Terminate a running process by name or PID. "
            "Returns list of killed processes. Use with caution."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Process name to kill (case-insensitive match).",
                },
                "pid": {
                    "type": "integer",
                    "description": "Specific PID to kill (alternative to name).",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="disk_usage",
        description=(
            "Return disk space usage per mounted drive/partition. "
            "Shows total, used, free, and percentage for each."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="search_youtube",
        description=(
            "Search YouTube for a query and return the top results. "
            "Opens the search results page in the browser and also returns structured data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string.",
                },
                "open_browser": {
                    "type": "boolean",
                    "description": "Also open YouTube search results page in browser (default: True).",
                    "default": True,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="github_op",
        description=(
            "Perform GitHub operations: list issues, list PRs, get repo info, "
            "create a branch, or clone a repo. "
            "Requires git and gh CLI to be installed and authenticated."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["list_issues", "list_prs", "repo_info", "create_branch", "clone"],
                    "description": "The operation to perform.",
                },
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in 'owner/repo' format.",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name (for create_branch).",
                },
                "source_branch": {
                    "type": "string",
                    "description": "Source branch (for create_branch, default: main).",
                },
                "local_path": {
                    "type": "string",
                    "description": "Local directory for clone (for clone).",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter for issues/PRs (default: open).",
                },
            },
            "required": ["op", "repo"],
        },
    ),
    Tool(
        name="volume_control",
        description=(
            "Get or change the system master volume on Windows. "
            "Supports get, set (0–100), mute/unmute, and step (+/−). "
            "Requires Windows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set", "mute", "unmute", "up", "down"],
                    "description": "Action to perform.",
                },
                "level": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Volume level 0–100 (used with action='set').",
                },
                "step": {
                    "type": "integer",
                    "description": "Volume step percent (default: 5, used with up/down).",
                    "default": 5,
                },
            },
            "required": ["action"],
        },
    ),
    Tool(
        name="screen_ocr",
        description=(
            "Capture the screen (or a region) and run OCR to extract all readable text. "
            "Use when Master needs to read text from anywhere on screen — "
            "images, game UI, PDF previews, etc. Returns the extracted text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Screen region: 'full' (default), 'top', 'bottom', 'left', 'right', "
                                  "or 'center'. For exact pixels use 'x,y,w,h' (e.g. '0,0,500,400').",
                    "default": "full",
                },
                "language": {
                    "type": "string",
                    "description": "Tesseract language code (default: eng). Add '+' for multi-language e.g. 'eng+hin'.",
                    "default": "eng",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Max characters to return (default: 4000).",
                    "default": 4000,
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="google_search",
        description=(
            "Perform a Google search: open results in the browser and return the search URL "
            "plus a brief content preview fetched directly. Use when Master asks to 'search Google for X' "
            "or 'look up Y'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "open_browser": {
                    "type": "boolean",
                    "description": "Also open Google search results page (default: True).",
                    "default": True,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of result snippets to return (default: 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="app_launcher",
        description=(
            "Launch an application or open a file by name, executable, or full path. "
            "Broader than launch_app — also supports common shortcut names "
            "like 'settings', 'calculator', 'cmd', 'powershell', 'notepad++'. "
            "On Windows uses Start-Process; on Linux uses xdg-open."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Application name or executable (e.g. 'chrome', 'code', 'notepad').",
                },
                "path": {
                    "type": "string",
                    "description": "Optional full path to executable or file (overrides name if given).",
                },
                "args": {
                    "type": "string",
                    "description": "Optional command-line arguments.",
                },
            },
            "required": [],
        },
    ),
]


# ── Tool handlers ─────────────────────────────────────────────────────────────
# (Handlers are now passed to Server constructor via on_list_tools and on_call_tool)


def _tool_result(data: Any) -> CallToolResult:
    """Wrap a value as a JSON TextContent for MCP responses."""
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])


def _error_result(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": message}))], isError=True)


# ── Individual tool implementations ──────────────────────────────────────────

def _call_take_screenshot(args: dict) -> CallToolResult:
    monitor_idx = int(args.get("monitor_index", 0))
    with mss() as sct:
        monitors = sct.monitors
        # monitors[0] is the combined virtual screen; monitors[1+] are physical
        real_monitors = monitors[1:]
        if monitor_idx >= len(real_monitors):
            monitor_idx = 0
        region = real_monitors[monitor_idx]
        raw = sct.grab(region)

    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    # Compress to JPEG for efficient LLM Vision transport
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    logger.debug("Screenshot captured: %dx%d → %d bytes (b64)", raw.width, raw.height, len(b64))
    return _tool_result({
        "image_base64": b64,
        "format": "jpeg",
        "width": raw.width,
        "height": raw.height,
        "monitor_index": monitor_idx,
        "timestamp": time.time(),
    })


def _call_click_coordinate(args: dict) -> CallToolResult:
    x = int(args["x"])
    y = int(args["y"])
    button: str = str(args.get("button", "left"))
    clicks: int = int(args.get("clicks", 1))

    if button not in {"left", "right", "middle"}:
        return _error_result(f"Invalid button: {button!r}")

    pyautogui.moveTo(x, y, duration=0.1)
    pyautogui.click(x, y, button=button, clicks=clicks, interval=0.1)
    logger.info("Clicked %s×%d at (%d, %d)", button, clicks, x, y)
    return _tool_result({"clicked": True, "x": x, "y": y, "button": button, "clicks": clicks})


def _call_type_text(args: dict) -> CallToolResult:
    text: str = str(args["text"])
    interval_ms: int = int(args.get("interval_ms", 25))
    interval_sec = interval_ms / 1000.0

    pyautogui.typewrite(text, interval=interval_sec)
    logger.info("Typed %d characters", len(text))
    return _tool_result({"typed": True, "length": len(text)})


def _get_active_window_title() -> str:
    """Best-effort retrieval of the active window title across platforms."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        pass
    try:
        import subprocess as sp
        # macOS
        result = sp.run(
            ["osascript", "-e", 'tell application "System Events" to name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _call_get_system_stats(args: dict) -> CallToolResult:
    include_processes: bool = bool(args.get("include_processes", True))

    cpu_percent = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    battery = psutil.sensors_battery()

    processes: list[dict] = []
    if include_processes:
        for proc in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_percent"]):
            try:
                processes.append(proc.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        # Sort by CPU usage descending, cap at top 50
        processes.sort(key=lambda p: p.get("cpu_percent") or 0, reverse=True)
        processes = processes[:50]

    stats = {
        "active_window": _get_active_window_title(),
        "cpu_percent": cpu_percent,
        "memory": {
            "total_mb": round(mem.total / 1_048_576),
            "available_mb": round(mem.available / 1_048_576),
            "used_percent": mem.percent,
        },
        "battery": {
            "percent": battery.percent if battery else None,
            "plugged_in": battery.power_plugged if battery else None,
            "seconds_left": battery.secsleft if battery else None,
        } if battery else None,
        "processes": processes if include_processes else [],
        "timestamp": time.time(),
    }
    return _tool_result(stats)


def _is_command_permitted(command: str) -> tuple[bool, str]:
    """
    Validate a command against the whitelist/blacklist from settings.json.
    Returns (permitted: bool, reason: str).
    """
    cmd_lower = command.strip().lower()

    # Check blacklist first — explicit denials take priority
    for blocked in _BLACKLIST:
        if blocked.lower() in cmd_lower:
            return False, f"Command matches blacklist entry: '{blocked}'"

    # If whitelist is non-empty, command must start with an approved prefix
    if _WHITELIST:
        for allowed in _WHITELIST:
            if cmd_lower.startswith(allowed.lower()):
                return True, "whitelisted"
        return False, "Command not found in whitelist"

    # Empty whitelist = allow all non-blacklisted commands
    return True, "no whitelist configured"


def _call_execute_terminal_command(args: dict) -> CallToolResult:
    command: str = str(args["command"])
    working_dir: str | None = args.get("working_directory")

    permitted, reason = _is_command_permitted(command)
    if not permitted:
        logger.warning("Blocked command: %r — %s", command, reason)
        return _error_result(f"Command not permitted: {reason}")

    logger.info("Executing: %r (cwd=%s)", command, working_dir)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT,
            cwd=working_dir,
        )
        stdout = result.stdout[:_MAX_OUTPUT]
        stderr = result.stderr[:_MAX_OUTPUT]

        return _tool_result({
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": command,
            "timed_out": False,
        })
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ds: %r", _CMD_TIMEOUT, command)
        return _tool_result({
            "returncode": -1,
            "stdout": "",
            "stderr": f"Command timed out after {_CMD_TIMEOUT} seconds.",
            "command": command,
            "timed_out": True,
        })


# ── Web browsing tools ───────────────────────────────────────────────────────

def _get_page():
    """Get or create the Playwright page (singleton pattern)."""
    global _browser, _page
    if _page is None or _browser is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(headless=True)
        _page = _browser.new_page()
    return _page


def _call_web_navigate(args: dict) -> CallToolResult:
    """Navigate browser to a URL."""
    url = str(args.get("url", ""))
    wait = int(args.get("wait_seconds", 2))
    try:
        page = _get_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        if wait > 0:
            page.wait_for_timeout(wait * 1000)
        title = page.title()
        # Get a summary of the page content
        text_content = page.inner_text("body")[:3000]
        logger.info("Navigated to %s — title: %s", url, title)
        return _tool_result({
            "success": True,
            "url": page.url,
            "title": title,
            "text_preview": text_content[:1500],
        })
    except Exception as exc:
        logger.error("web_navigate error: %s", exc)
        return _error_result(f"Navigation failed: {exc}")


def _call_web_click(args: dict) -> CallToolResult:
    """Click an element on the current page."""
    selector = str(args.get("selector", ""))
    timeout = int(args.get("timeout", 5))
    try:
        page = _get_page()
        page.click(selector, timeout=timeout * 1000)
        page.wait_for_load_state("networkidle", timeout=10000)
        return _tool_result({
            "success": True,
            "clicked": selector,
            "url": page.url,
            "title": page.title(),
        })
    except Exception as exc:
        logger.error("web_click error: %s", exc)
        return _error_result(f"Click failed: {exc}")


def _call_web_type(args: dict) -> CallToolResult:
    """Type text into an input field."""
    selector = str(args.get("selector", ""))
    text = str(args.get("text", ""))
    clear_first = bool(args.get("clear_first", True))
    try:
        page = _get_page()
        if clear_first:
            page.fill(selector, text, timeout=5000)
        else:
            page.type_(selector, text, timeout=5000)
        return _tool_result({
            "success": True,
            "typed": text,
            "selector": selector,
        })
    except Exception as exc:
        logger.error("web_type error: %s", exc)
        return _error_result(f"Type failed: {exc}")


def _call_web_get_text(args: dict) -> CallToolResult:
    """Extract text from the page."""
    selector = str(args.get("selector", ""))
    max_len = int(args.get("max_length", 5000))
    try:
        page = _get_page()
        if selector:
            text = page.inner_text(selector)
        else:
            text = page.inner_text("body")
        return _tool_result({
            "success": True,
            "text": text[:max_len],
            "url": page.url,
            "title": page.title(),
        })
    except Exception as exc:
        logger.error("web_get_text error: %s", exc)
        return _error_result(f"Get text failed: {exc}")


def _call_web_screenshot(args: dict) -> CallToolResult:
    """Take a screenshot of the current page."""
    full_page = bool(args.get("full_page", False))
    try:
        page = _get_page()
        screenshot_bytes = page.screenshot(full_page=full_page)
        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        return _tool_result({
            "success": True,
            "screenshot_base64": b64,
            "format": "png",
            "url": page.url,
            "title": page.title(),
        })
    except Exception as exc:
        logger.error("web_screenshot error: %s", exc)
        return _error_result(f"Screenshot failed: {exc}")


def _call_web_fetch(args: dict) -> CallToolResult:
    """Fetch a URL without browser (for APIs, simple pages)."""
    url = str(args.get("url", ""))
    timeout = int(args.get("timeout", 15))
    try:
        import requests
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "CoreControl/1.0"})
        # Strip HTML tags for text extraction
        import re
        text = re.sub(r'<[^>]+>', ' ', resp.text)
        text = re.sub(r'\s+', ' ', text).strip()[:5000]
        return _tool_result({
            "success": True,
            "url": url,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "text_preview": text,
        })
    except Exception as exc:
        logger.error("web_fetch error: %s", exc)
        return _error_result(f"Fetch failed: {exc}")


def _call_web_go_back(args: dict) -> CallToolResult:
    """Go back in browser history."""
    try:
        page = _get_page()
        page.go_back()
        return _tool_result({
            "success": True,
            "url": page.url,
            "title": page.title(),
        })
    except Exception as exc:
        logger.error("web_go_back error: %s", exc)
        return _error_result(f"Go back failed: {exc}")


def _call_web_evaluate(args: dict) -> CallToolResult:
    """Execute JavaScript on the page."""
    script = str(args.get("script", ""))
    try:
        page = _get_page()
        result = page.evaluate(script)
        return _tool_result({
            "success": True,
            "result": result,
            "url": page.url,
        })
    except Exception as exc:
        logger.error("web_evaluate error: %s", exc)
        return _error_result(f"Evaluate failed: {exc}")


def _call_get_current_time(args: dict) -> CallToolResult:
    """Return the current local date, time, and timezone."""
    now = datetime.datetime.now().astimezone()
    return _tool_result({
        "iso": now.isoformat(),
        "formatted": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(now.tzinfo),
        "utc_offset": now.strftime("%z"),
    })


def _call_open_url(args: dict) -> CallToolResult:
    """Open a URL in the system default browser."""
    url = str(args.get("url", "")).strip()
    title = str(args.get("title", "URL"))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url, new=1, autoraise=True)
        logger.info("Opened %r in browser — %s", url, title)
        return _tool_result({
            "success": True,
            "url": url,
            "title": title,
            "platform": platform.system(),
        })
    except Exception as exc:
        logger.error("open_url error: %s", exc)
        return _error_result(f"Could not open URL: {exc}")


def _call_get_location(args: dict) -> CallToolResult:
    """Return geographic location via public IP geolocation."""
    try:
        import requests
        r = requests.get("https://ipinfo.io/json", timeout=8)
        data = r.json()
        loc = data.get("loc", "unknown")
        lat, lon = loc.split(",") if "," in loc else ("unknown", "unknown")
        return _tool_result({
            "ip": data.get("ip", "unknown"),
            "city": data.get("city", "unknown"),
            "region": data.get("region", "unknown"),
            "country": data.get("country", "unknown"),
            "country_name": data.get("country", "unknown"),
            "postal": data.get("postal", "unknown"),
            "timezone": data.get("timezone", "unknown"),
            "latitude": lat,
            "longitude": lon,
        })
    except Exception as exc:
        logger.warning("Location fetch failed: %s", exc)
        return _error_result(f"Could not resolve location: {exc}")


# ── New tool handlers ────────────────────────────────────────────────────────

def _call_launch_app(args: dict) -> CallToolResult:
    target = str(args.get("target", "")).strip()
    extra_args = str(args.get("args", "")).strip()
    full_cmd = f'{target} {"-- " + extra_args if extra_args else ""}'.strip()
    try:
        subprocess.Popen(full_cmd, shell=True)
        return _tool_result({"success": True, "launched": target, "command": full_cmd})
    except Exception as exc:
        return _error_result(f"Could not launch {target}: {exc}")


def _call_clipboard_read(_args: dict) -> CallToolResult:
    try:
        import pyperclip
        text = pyperclip.paste()
        return _tool_result({"success": True, "text": text, "length": len(text)})
    except Exception as exc:
        return _error_result(f"Clipboard read failed: {exc}")


def _call_clipboard_write(args: dict) -> CallToolResult:
    try:
        import pyperclip
        text = str(args.get("text", ""))
        pyperclip.copy(text)
        return _tool_result({"success": True, "length": len(text)})
    except Exception as exc:
        return _error_result(f"Clipboard write failed: {exc}")


def _call_set_timer(args: dict) -> CallToolResult:
    minutes = max(1, int(args.get("minutes", 1)))
    label = str(args.get("label", "Timer"))
    seconds = minutes * 60

    def _ring():
        time.sleep(seconds)
        try:
            import ctypes
            ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
        except Exception:
            pass
        logger.info("Timer expired: %s (%d min)", label, minutes)

    threading.Thread(target=_ring, daemon=True).start()
    return _tool_result({
        "success": True,
        "message": f"Timer set for {minutes} minute{'s' if minutes != 1 else ''} — '{label}'",
        "seconds": seconds,
    })


def _call_annotate_screenshot(args: dict) -> CallToolResult:
    try:
        import numpy as np
        has_numpy = True
    except ImportError:
        has_numpy = False

    try:
        from PIL import ImageDraw, ImageFont

        # Capture full primary monitor
        with mss() as sct:
            monitor = sct.monitors[1]  # primary
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            img = img.convert("RGBA")
            draw = ImageDraw.Draw(img)

        shape = str(args.get("shape", "arrow")).lower()
        sx = int(args["start_x"])
        sy = int(args["start_y"])
        color_str = str(args.get("color", "255,0,0"))
        color = tuple(int(c) for c in color_str.split(","))[:3] + (255,)
        thickness = int(args.get("thickness", 3))

        if shape == "arrow":
            ex = int(args.get("end_x", sx + 100))
            ey = int(args.get("end_y", sy + 100))
            draw.line([(sx, sy), (ex, ey)], fill=color, width=thickness)
            # Arrowhead
            import math
            angle = math.atan2(ey - sy, ex - sx)
            head_len = 15 + thickness * 3
            for offset in [angle + math.pi / 5, angle - math.pi / 5]:
                dx = head_len * math.cos(offset)
                dy = head_len * math.sin(offset)
                draw.line([(ex, ey), (ex + dx, ey + dy)], fill=color, width=thickness)
        elif shape == "circle":
            r = int(args.get("radius", 60))
            draw.ellipse([sx - r, sy - r, sx + r, sy + r], outline=color, width=thickness)
        elif shape == "rectangle":
            ex = int(args.get("end_x", sx + 100))
            ey = int(args.get("end_y", sy + 100))
            draw.rectangle([sx, sy, ex, ey], outline=color, width=thickness)
        elif shape == "text":
            txt = str(args.get("text", "annotation"))
            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except Exception:
                font = ImageFont.load_default()
            draw.text((sx, sy), txt, fill=color, font=font)
        else:
            return _error_result(f"Unknown shape: {shape!r}")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return _tool_result({"success": True, "image_base64": b64, "format": "png"})
    except Exception as exc:
        logger.error("annotate_screenshot error: %s", exc)
        return _error_result(f"Annotation failed: {exc}")


def _call_rename_files(args: dict) -> CallToolResult:
    try:
        import fnmatch
    except ImportError:
        fnmatch = None

    directory = str(args.get("directory", "")).strip()
    pattern = str(args.get("pattern", "")).strip()
    value = str(args.get("value", "")).strip()
    glob_filter = str(args.get("glob", "*")).strip()
    dry_run = bool(args.get("dry_run", True))

    if not os.path.isdir(directory):
        return _error_result(f"Directory not found: {directory}")

    results = []
    for fname in sorted(os.listdir(directory)):
        if glob_filter != "*" and not fnmatch.filter([fname], glob_filter):
            continue
        base, ext = os.path.splitext(fname)
        new_name = fname
        try:
            if pattern == "prefix":
                new_name = value + fname
            elif pattern == "suffix":
                new_name = fname + value
            elif pattern == "replace":
                if "=" in value:
                    old_s, new_s = value.split("=", 1)
                    new_name = fname.replace(old_s, new_s)
                else:
                    new_name = fname
            elif pattern == "extension":
                new_name = base + value if value.startswith(".") else base + "." + value
            else:
                return _error_result(f"Unknown pattern: {pattern!r}")
        except Exception as exc:
            results.append({"file": fname, "error": str(exc)})
            continue

        if new_name == fname:
            continue
        old_path = os.path.join(directory, fname)
        new_path = os.path.join(directory, new_name)
        if dry_run:
            results.append({"file": fname, "new_name": new_name, "action": "dry-run"})
        else:
            try:
                os.rename(old_path, new_path)
                results.append({"file": fname, "new_name": new_name, "action": "renamed"})
            except Exception as exc:
                results.append({"file": fname, "error": str(exc)})

    return _tool_result({"success": True, "dry_run": dry_run, "changes": results})


def _call_kill_process(args: dict) -> CallToolResult:
    killed = []
    try:
        pid_arg = args.get("pid")
        name_arg = str(args.get("name", "")).strip().lower()

        if pid_arg is not None:
            pid = int(pid_arg)
            try:
                p = psutil.Process(pid)
                p.kill()
                killed.append({"pid": pid, "name": p.name(), "status": "killed"})
            except psutil.NoSuchProcess:
                killed.append({"pid": pid, "status": "not found"})
        elif name_arg:
            for proc in psutil.process_iter(["pid", "name"]):
                if name_arg in proc.info["name"].lower():
                    try:
                        proc.kill()
                        killed.append({"pid": proc.info["pid"], "name": proc.info["name"], "status": "killed"})
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        killed.append({"pid": proc.info["pid"], "name": proc.info["name"], "status": "access denied"})
        else:
            return _error_result("Provide either 'pid' or 'name'")

    except Exception as exc:
        return _error_result(f"kill_process error: {exc}")

    return _tool_result({"success": True, "killed": killed, "count": len(killed)})


def _call_disk_usage(_args: dict) -> CallToolResult:
    try:
        usage = psutil.disk_usage("/")
        partitions = []
        for part in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(part.mountpoint)
                partitions.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(u.total / 1_073_741_824, 2),
                    "used_gb": round(u.used / 1_073_741_824, 2),
                    "free_gb": round(u.free / 1_073_741_824, 2),
                    "percent_used": u.percent,
                })
            except Exception:
                pass
        return _tool_result({"partitions": partitions, "root": {
            "total_gb": round(usage.total / 1_073_741_824, 2),
            "used_gb": round(usage.used / 1_073_741_824, 2),
            "free_gb": round(usage.free / 1_073_741_824, 2),
            "percent_used": usage.percent,
        }})
    except Exception as exc:
        return _error_result(f"disk_usage error: {exc}")


def _call_search_youtube(args: dict) -> CallToolResult:
    query = str(args.get("query", "")).strip()
    open_browser = bool(args.get("open_browser", True))
    search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    if open_browser:
        webbrowser.open(search_url, new=1, autoraise=True)
    return _tool_result({
        "success": True,
        "query": query,
        "search_url": search_url,
        "browser_opened": open_browser,
    })


def _call_github_op(args: dict) -> CallToolResult:
    op = str(args.get("op", "")).strip()
    repo = str(args.get("repo", "")).strip()
    _gh = shutil.which("gh") or shutil.which("git")
    if not _gh:
        return _error_result("GitHub CLI (gh) or git not found in PATH")

    try:
        if op == "list_issues":
            state = str(args.get("state", "open"))
            r = subprocess.run([_gh, "issue", "list", "--repo", repo, "--state", state, "--limit", "10"],
                               capture_output=True, text=True, timeout=15)
            return _tool_result({"success": True, "op": op, "output": r.stdout.strip() or r.stderr.strip()})

        elif op == "list_prs":
            state = str(args.get("state", "open"))
            r = subprocess.run([_gh, "pr", "list", "--repo", repo, "--state", state, "--limit", "10"],
                               capture_output=True, text=True, timeout=15)
            return _tool_result({"success": True, "op": op, "output": r.stdout.strip() or r.stderr.strip()})

        elif op == "repo_info":
            r = subprocess.run([_gh, "repo", "view", repo], capture_output=True, text=True, timeout=15)
            return _tool_result({"success": True, "op": op, "output": r.stdout.strip() or r.stderr.strip()})

        elif op == "create_branch":
            branch = str(args.get("branch", "")).strip()
            source = str(args.get("source_branch", "main")).strip()
            r = subprocess.run([_gh, "branch", "create", branch, "--source", source, "--repo", repo],
                               capture_output=True, text=True, timeout=15)
            return _tool_result({"success": r.returncode == 0, "op": op, "branch": branch,
                                 "output": r.stdout.strip() or r.stderr.strip()})

        elif op == "clone":
            local_path = str(args.get("local_path", "."))
            r = subprocess.run(["git", "clone", f"https://github.com/{repo}.git", local_path],
                               capture_output=True, text=True, timeout=60)
            return _tool_result({"success": r.returncode == 0, "op": op,
                                 "output": r.stdout.strip() or r.stderr.strip()})

        else:
            return _error_result(f"Unknown github_op: {op!r}")

    except subprocess.TimeoutExpired:
        return _error_result(f"GitHub operation timed out: {op}")
    except Exception as exc:
        return _error_result(f"GitHub op error: {exc}")


# ── Volume, OCR, Google Search, App Launcher ────────────────────────────────

def _call_volume_control(args: dict) -> CallToolResult:
    """Volume control via Windows WM_APPCOMMAND broadcast."""
    action = str(args.get("action", "get")).lower()
    try:
        import ctypes
        user32 = ctypes.windll.user32
        HWND_BROADCAST = 0xFFFF
        WM_APPCOMMAND = 0x0319
        SMTO_ABORTIFHUNG = 0x0002
        APPCOMMAND_VOLUME_MUTE = 0x00080000
        APPCOMMAND_VOLUME_DOWN = 0x00090000
        APPCOMMAND_VOLUME_UP = 0x000A0000

        def _send_cmd(cmd: int):
            user32.SendMessageTimeoutW(HWND_BROADCAST, WM_APPCOMMAND, 0, cmd, SMTO_ABORTIFHUNG, 5000, None)

        if action == "get":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Volume -Audio).VolumePercent"],
                capture_output=True, text=True, timeout=5
            )
            level = int(r.stdout.strip()) if r.stdout.strip().isdigit() else -1
            return _tool_result({"success": True, "level": level})

        if action == "set":
            level = max(0, min(100, int(args.get("level", 50))))
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Volume -Audio).VolumePercent"],
                capture_output=True, text=True, timeout=5
            )
            current = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 50
            diff = level - current
            if diff > 0:
                for _ in range((diff + 4) // 5):
                    _send_cmd(APPCOMMAND_VOLUME_UP)
            elif diff < 0:
                for _ in range((-diff + 4) // 5):
                    _send_cmd(APPCOMMAND_VOLUME_DOWN)
            return _tool_result({"success": True, "action": "set", "level": level})

        if action == "mute":
            _send_cmd(APPCOMMAND_VOLUME_MUTE)
            return _tool_result({"success": True, "action": "mute"})

        if action == "unmute":
            _send_cmd(APPCOMMAND_VOLUME_MUTE)
            return _tool_result({"success": True, "action": "unmute"})

        if action in ("up", "down"):
            step = int(args.get("step", 5))
            cmd = APPCOMMAND_VOLUME_UP if action == "up" else APPCOMMAND_VOLUME_DOWN
            for _ in range((step + 4) // 5):
                _send_cmd(cmd)
            return _tool_result({"success": True, "action": action, "step": step})

        return _error_result(f"Unknown volume action: {action!r}")

    except Exception as exc:
        logger.error("volume_control error: %s", exc)
        return _error_result(f"Volume control failed: {exc}")


def _call_screen_ocr(args: dict) -> CallToolResult:
    """OCR on a screenshot using pytesseract."""
    try:
        import pytesseract
    except ImportError:
        return _error_result("pytesseract not installed. Run: pip install pytesseract")

    try:
        from PIL import Image
    except ImportError:
        return _error_result("PIL not installed. Run: pip install Pillow")

    region_str = str(args.get("region", "full")).strip().lower()
    lang = str(args.get("language", "eng"))
    max_len = int(args.get("max_length", 4000))

    # Capture region
    with mss() as sct:
        monitors = sct.monitors
        full = monitors[1]  # primary monitor rect
        if region_str == "full":
            region = full
        elif "," in region_str:
            parts = region_str.split(",")
            region = {"left": int(parts[0]), "top": int(parts[1]),
                      "width": int(parts[2]), "height": int(parts[3])}
        elif region_str == "top":
            h = full["height"] // 3
            region = {**full, "height": h}
        elif region_str == "bottom":
            h = full["height"] // 3
            region = {**full, "top": full["top"] + 2 * h, "height": h}
        elif region_str == "left":
            w = full["width"] // 3
            region = {**full, "width": w}
        elif region_str == "right":
            w = full["width"] // 3
            region = {**full, "left": full["left"] + 2 * w, "width": w}
        elif region_str == "center":
            w, h = full["width"] // 3, full["height"] // 3
            region = {**full, "left": full["left"] + full["width"] // 2 - w // 2,
                      "top": full["top"] + full["height"] // 2 - h // 2, "width": w, "height": h}
        else:
            region = full

        raw = sct.grab(region)

    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    try:
        text = pytesseract.image_to_string(img, lang=lang)
    except Exception as exc:
        return _error_result(f"OCR failed: {exc}")

    text = text[:max_len]
    logger.info("OCR complete: %d chars extracted", len(text))
    return _tool_result({"success": True, "text": text, "region": region, "length": len(text)})


def _call_google_search(args: dict) -> CallToolResult:
    """Search Google and optionally open results in browser."""
    query = str(args.get("query", "")).strip()
    open_browser = bool(args.get("open_browser", True))
    max_results = int(args.get("max_results", 5))

    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    if open_browser:
        webbrowser.open(search_url, new=1, autoraise=True)

    # Fetch a content preview
    try:
        import requests
        resp = requests.get(search_url, timeout=10,
                            headers={"User-Agent": "CoreControl/1.0"})
        import re
        # Extract text snippets from Google's HTML
        text = re.sub(r'<[^>]+>', ' ', resp.text)
        text = re.sub(r'\s+', ' ', text).strip()[:8000]
        # Try to find snippet elements (Google's meta descriptions)
        snippets = re.findall(r'(?<=<span[^>]*class="[^"]*[^"]*")[^<]{50,300}', text)
        snippets = snippets[:max_results]
    except Exception as exc:
        logger.warning("Google search fetch failed: %s", exc)
        snippets = []

    return _tool_result({
        "success": True,
        "query": query,
        "search_url": search_url,
        "browser_opened": open_browser,
        "snippets": snippets if snippets else ["(Could not fetch snippets — browser opened instead)"],
    })


def _call_app_launcher(args: dict) -> CallToolResult:
    """Launch an application by name or path (enhanced app launcher)."""
    target = str(args.get("path", args.get("name", ""))).strip()
    extra_args = str(args.get("args", "")).strip()
    if not target:
        return _error_result("Provide either 'name' or 'path'")

    platform_sys = platform.system()
    full_cmd = target
    if extra_args:
        full_cmd += " " + extra_args

    try:
        if platform_sys == "Windows":
            # Map common shortcut names to actual executables
            shortcuts = {
                "settings": "ms-settings:",
                "calculator": "calc",
                "notepad": "notepad",
                "paint": "ms-paint:",
                "photos": "ms-photos:",
                "store": "ms-windows-store:",
                "explorer": "explorer",
                "cmd": "cmd /k",
                "powershell": "powershell -NoExit",
                "taskmgr": "taskmgr",
                "services": "services.msc",
                "diskmgmt": "diskmgmt.msc",
                "devmgmt": "devmgmt.msc",
                "eventvwr": "eventvwr",
                "control": "control",
                "perfmon": "perfmon",
                "regedit": "regedit",
                "wordpad": "wordpad",
                "zoom": "zoom://",
                "teams": "msteams:",
                "discord": "discord:",
                "spotify": "spotify:",
            }
            lower_target = target.lower()
            if lower_target in shortcuts:
                full_cmd = shortcuts[lower_target]
                if extra_args:
                    full_cmd += " " + extra_args

            proc = subprocess.Popen(full_cmd, shell=True, creationflags=0)
        else:
            proc = subprocess.Popen(full_cmd, shell=True)

        return _tool_result({"success": True, "launched": target, "platform": platform_sys, "command": full_cmd})
    except Exception as exc:
        return _error_result(f"Could not launch {target}: {exc}")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("CoreControl MCP server starting (stdio transport)")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="corecontrol-mcp",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
