"""
Local stdio MCP Server for CoreControl.
Exposes system control primitives to the orchestrator via the MCP protocol.

IMPORTANT: All logging goes to stderr only. stdout is reserved for JSON-RPC
transport — any non-JSON bytes written there corrupt the protocol.
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import logging
import os
import subprocess
import sys
import time
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
