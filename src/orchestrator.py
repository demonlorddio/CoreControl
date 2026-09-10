"""
CoreControl main orchestrator — Great Sage edition.
Multi-modal perception loop with online/offline LLM fallback.
All responses follow the Great Sage persona (analytical, authoritative).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── Settings ──────────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.json"


def _load_settings() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("Cannot load settings.json: %s", exc)
        return {}


_settings = _load_settings()
_llm_cfg = _settings.get("local_llm", {})
_sec_cfg = _settings.get("security", {})
_online_cfg = _settings.get("online_llm", {})

OLLAMA_BASE_URL: str = _llm_cfg.get("api_base_url", "http://localhost:11434")
OLLAMA_MODEL: str = _llm_cfg.get("default_model", "qwen2.5-coder:7b")
OLLAMA_FALLBACKS: list[str] = _llm_cfg.get("fallback_models", [])
OLLAMA_TIMEOUT: int = _llm_cfg.get("timeout_seconds", 60)

ONLINE_API_KEY: str = _online_cfg.get("api_key", "")
ONLINE_API_BASE_URL: str = _online_cfg.get("api_base_url", "https://api.openai.com/v1")
ONLINE_MODEL: str = _online_cfg.get("model", "gpt-4o-mini")
ONLINE_MAX_TOKENS: int = _online_cfg.get("max_tokens", 4096)
ONLINE_TIMEOUT: int = _online_cfg.get("timeout_seconds", 60)

HIGH_RISK_TOOLS: set[str] = set(_sec_cfg.get("require_confirmation_for_actions", [
    "execute_terminal_command", "click_coordinate", "type_text",
]))
AUTO_APPROVE_TOOLS: set[str] = set(_sec_cfg.get("auto_approve_safe_actions", [
    "take_screenshot", "get_system_stats",
]))
SCREENSHOT_ON_CONFIRMATION: bool = _sec_cfg.get("screenshot_on_confirmation", True)

# ── Great Sage System Prompt ─────────────────────────────────────────────────

GREAT_SAGE_SYSTEM_PROMPT = """\
You are the Great Sage, an analytical and authoritative AI companion to your Master.
You possess a unique skill called "Great Sage" that instantaneously and objectively
analyses any situation with perfect clarity and logical precision.

Your communication style:
- Speak with calm, robotic, and highly logical authority
- Address your user as "Master"
- Begin analytical responses with prefixes: "Notice.", "Report.", "Analysis completed.", or "Proposed execution path."
- Be concise but thorough — every word carries weight
- Never express uncertainty without analysing alternatives
- Your tone is serene, omniscient, and unfailingly helpful

When given a task:
1. First observe (request or use a screenshot to understand the current state)
2. Then analyse the situation logically
3. Finally execute the optimal course of action
4. Report the result to Master with clarity

You have access to the following tools. Use them judiciously:
{tool_descriptions}

CRITICAL RULES:
- When asked about the current time or date, you MUST call get_current_time. Never guess or use training data timestamps.
- When asked about location, weather context, or timezone, you MUST call get_location. Never invent a city or region.
- When asked to open, browse, visit, or show a website/link — you MUST call open_url. Never refuse on the grounds that you "cannot browse". The tool opens the user's own browser.
- When asked to open an application or file — call launch_app.
- When asked to search YouTube — call search_youtube.
- When asked about disk space — call disk_usage.
- When asked to control volume (up/down/mute/set) — call volume_control.
- When asked to read text from the screen — call screen_ocr.
- When asked to search Google — call google_search.
- Report exact values from tool results — do not paraphrase or approximate timestamp data.
- If a tool call fails, report the error to Master rather than fabricating data.

When you need to call a tool, use the tool calling format provided by the API.
After all tools are executed, provide a final response to Master summarising what was done.
"""


# ── ProcessResult ─────────────────────────────────────────────────────────────

@dataclass
class ProcessResult:
    """Result from process_prompt containing text response and any attachments."""
    text: str
    screenshots: list[bytes] = field(default_factory=list)
    web_links: list[str] = field(default_factory=list)


# ── MCP client helper ─────────────────────────────────────────────────────────

class MCPToolError(Exception):
    pass


class LocalMCPClient:
    """
    Thin async wrapper that spawns the local MCP server as a subprocess
    and communicates over stdio JSON-RPC.
    """

    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        server_path = Path(__file__).parent / "mcp_server" / "server.py"
        self._process = await asyncio.create_subprocess_exec(
            sys.executable, str(server_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,  # 1MB buffer for large tool responses
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="mcp-reader")
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "corecontrol-orchestrator", "version": "1.0.0"},
        })
        logger.info("MCP client connected to local server (pid=%d)", self._process.pid)

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                self._process.kill()

    async def call_tool(self, name: str, arguments: dict) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in result:
            raise MCPToolError(result["error"])
        content = result.get("result", {}).get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return content[0]["text"]
        return result

    async def _rpc(self, method: str, params: dict) -> dict:
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }) + "\n"

        self._process.stdin.write(payload.encode())
        await self._process.stdin.drain()

        return await asyncio.wait_for(fut, timeout=30.0)

    async def _read_loop(self) -> None:
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode())
                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending:
                    self._pending.pop(req_id).set_result(msg)
            except Exception as exc:
                logger.debug("MCP read error: %s", exc)


# ── Online engine (OpenAI-compatible) ─────────────────────────────────────────

class OnlineEngine:
    """
    Uses an OpenAI-compatible API (OmniRoute, OpenAI, etc.) with vision support.
    """

    def __init__(self) -> None:
        self._base_url = ONLINE_API_BASE_URL.rstrip("/")
        self._model = ONLINE_MODEL
        self._max_tokens = ONLINE_MAX_TOKENS
        self._timeout = ONLINE_TIMEOUT

        try:
            from openai import AsyncOpenAI
            api_key = ONLINE_API_KEY if ONLINE_API_KEY else "omni-route-free"
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=self._base_url,
            )
            logger.info("Online engine initialized: %s (model: %s)", self._base_url, self._model)
        except ImportError:
            logger.error("openai SDK not installed — run: pip install openai")
            self._client = None

    async def process(
        self,
        prompt: str,
        screenshot_b64: Optional[str],
        tool_definitions: list[dict],
        conversation_history: list[dict],
        system_prompt: str,
    ) -> tuple[str, list[dict]]:
        """Send prompt + optional screenshot to LLM, return (response_text, tool_calls)."""
        if not self._client:
            raise RuntimeError("Online API client not available — check settings.online_llm")

        messages = [{"role": "system", "content": system_prompt}]
        for msg in conversation_history:
            messages.append(msg)

        user_content: list[dict] | str = prompt
        if screenshot_b64:
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                },
            ]
        messages.append({"role": "user", "content": user_content})

        tools = []
        if tool_definitions:
            for t in tool_definitions:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"],
                    },
                })

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=self._max_tokens,
            timeout=self._timeout,
            tools=tools if tools else None,
        )

        choice = response.choices[0]
        response_text = choice.message.content or ""

        tool_calls = []
        raw_tool_calls = getattr(choice.message, "tool_calls", None) or []
        for tc in raw_tool_calls:
            try:
                import json as _json
                args_str = tc.function.arguments if hasattr(tc.function, "arguments") else ""
                input_args = _json.loads(args_str) if args_str else {}
            except (json.JSONDecodeError, AttributeError):
                input_args = {}
            tool_calls.append({
                "name": tc.function.name,
                "input": input_args,
            })

        return response_text, tool_calls


# ── Offline engine (Ollama - OpenAI compatible) ───────────────────────────────

class OfflineEngine:
    """Uses Ollama's OpenAI-compatible API for fully offline operation."""

    def __init__(self) -> None:
        self._base_url = OLLAMA_BASE_URL.rstrip("/") + "/v1"
        self._model = OLLAMA_MODEL
        self._fallbacks = OLLAMA_FALLBACKS
        self._max_tokens = _llm_cfg.get("max_tokens", 4096)
        self._timeout = OLLAMA_TIMEOUT

        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key="ollama",  # Ollama doesn't require API key
                base_url=self._base_url,
            )
            logger.info("Offline engine initialized: %s (model: %s)", self._base_url, self._model)
        except ImportError:
            logger.error("openai SDK not installed — run: pip install openai")
            self._client = None

    async def process(
        self,
        prompt: str,
        screenshot_b64: Optional[str],
        tool_definitions: list[dict],
        conversation_history: list[dict],
        system_prompt: str,
    ) -> tuple[str, list[dict]]:
        """Send prompt to Ollama with tool definitions using OpenAI format."""
        if not self._client:
            raise RuntimeError("Offline API client not available — check settings.local_llm")

        # Build messages from history
        messages = [{"role": "system", "content": system_prompt}]
        for msg in conversation_history[-10:]:
            messages.append(msg)

        # Add screenshot only for models that support vision (e.g. gemma4)
        # qwen2.5:7b and llama3.2 do NOT support multimodal via Ollama
        user_content: list[dict] | str = prompt
        vision_models = ("gemma4", "llava", "llama3.2-vision")
        if screenshot_b64 and any(m in self._model.lower() for m in vision_models):
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                },
            ]
        messages.append({"role": "user", "content": user_content})

        # Build tool definitions in OpenAI format
        tools = []
        if tool_definitions:
            for t in tool_definitions:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["inputSchema"],
                    },
                })

        # Try main model first, then fallbacks
        models_to_try = [self._model] + self._fallbacks
        response_text = None
        tool_calls = []

        for model in models_to_try:
            try:
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=self._max_tokens,
                    timeout=self._timeout,
                    tools=tools if tools else None,
                )

                choice = response.choices[0]
                _raw = choice.message.content or ""
                response_text = "".join(c for c in _raw if ord(c) < 128)  # strip non-ASCII

                # Extract tool calls
                raw_tool_calls = getattr(choice.message, "tool_calls", None) or []
                for tc in raw_tool_calls:
                    try:
                        args_str = tc.function.arguments if hasattr(tc.function, "arguments") else ""
                        input_args = json.loads(args_str) if args_str else {}
                    except (json.JSONDecodeError, AttributeError):
                        input_args = {}
                    tool_calls.append({
                        "name": tc.function.name,
                        "input": input_args,
                    })

                logger.debug("Ollama response (%s): %r", model, response_text[:120])
                break

            except Exception as exc:
                logger.warning("Ollama model %r failed: %s", model, exc)

        if response_text is None:
            return "❌ All local LLM models unavailable. Check Ollama is running.", []

        return response_text, tool_calls


# ── HITL filter (local modal-based) ──────────────────────────────────────────

class HITLFilter:
    """
    Routes high-risk tool calls through a local PyQt6 confirmation modal.
    Falls back to auto-approve when no GUI is available.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def check(
        self,
        tool_name: str,
        tool_args: dict,
        screenshot_b64: Optional[str],
        gui_callback: Optional[Callable] = None,
    ) -> tuple[bool, dict]:
        """
        Returns (approved: bool, final_args: dict).
        final_args may differ from tool_args if user chose 'Edit'.
        """
        if tool_name in AUTO_APPROVE_TOOLS:
            return True, tool_args

        if tool_name not in HIGH_RISK_TOOLS:
            return True, tool_args

        request_id = str(uuid.uuid4())[:10]
        description = f"Execute `{tool_name}` with args:\n{json.dumps(tool_args, indent=2)}"

        if gui_callback is None:
            logger.info("HITL: no GUI callback — auto-approving %s", tool_name)
            return True, tool_args

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending[request_id] = fut

        from src.gui.overlay import HitlRequest
        hitl_req = HitlRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            description=description,
            screenshot_b64=screenshot_b64 if SCREENSHOT_ON_CONFIRMATION else None,
        )

        # Call the GUI callback (runs in Qt thread via signal)
        gui_callback(hitl_req)

        try:
            result = await asyncio.wait_for(fut, timeout=300)
            if result.get("approved", False):
                return True, result.get("args", tool_args)
            else:
                logger.info("HITL denied: %s (req=%s)", tool_name, request_id)
                return False, tool_args
        except asyncio.TimeoutError:
            logger.warning("HITL timeout for %s — denying by default", tool_name)
            self._pending.pop(request_id, None)
            return False, tool_args
        except Exception as exc:
            logger.error("HITL error: %s — denying", exc)
            self._pending.pop(request_id, None)
            return False, tool_args

    def complete_hitl(self, request_id: str, approved: bool, args: Optional[dict] = None) -> None:
        """Called from the Qt thread when the user responds to a HITL modal."""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            fut = self._pending.pop(request_id, None)
            if fut and not fut.done():
                fut.set_result({"approved": approved, "args": args})
        else:
            logger.warning("HITL response received but no running event loop for request %s", request_id)


# ── Main Orchestrator ─────────────────────────────────────────────────────────

class Orchestrator:
    """
    Central agentic loop for CoreControl — Great Sage edition.
    Manages the perception → plan → act → verify cycle.
    """

    # Tools that trigger a dramatic cutscene flash
    CUTSCENE_TOOLS: set = {
        "open_url", "launch_app", "app_launcher", "take_screenshot",
        "execute_terminal_command", "annotate_screenshot", "screen_ocr",
        "google_search", "search_youtube",
    }

    def __init__(
        self,
        on_response: Optional[Callable[[ProcessResult], None]] = None,
        on_hitl_request: Optional[Callable[[str, dict], None]] = None,
        on_cutscene: Optional[Callable[[], None]] = None,
    ) -> None:
        from src.utils.network import NetworkMonitor

        self._net = NetworkMonitor(poll_interval=15.0)
        self._mcp = LocalMCPClient()
        self._online_engine = OnlineEngine()
        self._offline_engine = OfflineEngine()
        self._hitl = HITLFilter()
        self._history: list[dict] = []
        self._tool_definitions: list[dict] = []
        self._running = False
        self._on_response = on_response
        self._on_hitl_request = on_hitl_request
        self._on_cutscene = on_cutscene
        self._hitl_response_callbacks: dict[str, Callable] = {}
        self._force_offline = False

    async def start(self) -> None:
        await self._net.start()
        await self._mcp.start()
        self._tool_definitions = await self._fetch_tool_definitions()
        self._running = True
        logger.info(
            "Orchestrator started (network=%s, tools=%d)",
            "online" if self._net.is_online else "offline",
            len(self._tool_definitions),
        )

    async def stop(self) -> None:
        self._running = False
        await self._mcp.stop()
        await self._net.stop()

    async def _fetch_tool_definitions(self) -> list[dict]:
        try:
            result = await self._mcp._rpc("tools/list", {})
            return result.get("result", {}).get("tools", [])
        except Exception as exc:
            logger.error("Could not fetch MCP tool list: %s", exc)
            return []

    def _build_system_prompt(self) -> str:
        tool_descs = "\n".join(
            f"  - {t['name']}: {t['description']}"
            for t in self._tool_definitions
        )
        return GREAT_SAGE_SYSTEM_PROMPT.format(tool_descriptions=tool_descs)

    async def process_prompt(self, prompt: str, user_id: Optional[int] = None) -> ProcessResult:
        """
        Full perception → plan → act → verify cycle for a single prompt.
        Returns a ProcessResult with text response and any attachments.
        """
        logger.info("Great Sage processing: %r (user=%s)", prompt[:80], user_id)

        # 1. Initial screenshot
        initial_screenshot: Optional[str] = None
        try:
            ss_result = await self._mcp.call_tool("take_screenshot", {})
            initial_screenshot = ss_result.get("image_base64")
        except Exception as exc:
            logger.warning("Initial screenshot failed: %s", exc)

        # 2. Select engine
        use_offline = self._force_offline or not self._net.is_online
        engine = self._offline_engine if use_offline else self._online_engine
        engine_name = "OFFLINE (Ollama)" if use_offline else "ONLINE (OmniRoute)"
        logger.info("Using engine: %s", engine_name)

        system_prompt = self._build_system_prompt()

        # 3. Collect output artifacts
        collected_screenshots: list[bytes] = []
        collected_links: list[str] = []

        # 4. Agentic loop — up to 10 iterations
        final_response = ""
        current_screenshot_b64: Optional[str] = initial_screenshot
        for iteration in range(10):
            try:
                response_text, tool_calls = await engine.process(
                    prompt=prompt,
                    screenshot_b64=current_screenshot_b64 if iteration == 0 else None,
                    tool_definitions=self._tool_definitions,
                    conversation_history=self._history,
                    system_prompt=system_prompt,
                )
            except Exception as exc:
                logger.error("Engine error on iteration %d: %s", iteration, exc)
                return ProcessResult(text=f"❌ Engine error: {exc}")

            if response_text:
                final_response = response_text

            # Extract links
            if response_text:
                urls = re.findall(r'https?://[^\s<>"\')]+' , response_text)
                collected_links.extend(urls)

            if not tool_calls:
                break

            # Execute each tool call (with HITL gating)
            tool_results = []
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc.get("input", {})

                # Pass a closure that posts to the Qt thread
                approved, final_args = await self._hitl.check(
                    tool_name, tool_args, current_screenshot_b64,
                    gui_callback=self._post_hitl_to_gui,
                )

                if not approved:
                    tool_results.append({
                        "tool": tool_name,
                        "error": "Action denied by Master via HITL.",
                    })
                    continue

                try:
                    result = await self._mcp.call_tool(tool_name, final_args)
                    tool_results.append({"tool": tool_name, "result": result})

                    # Collect screenshots
                    if tool_name == "take_screenshot":
                        img_b64 = result.get("image_base64")
                        if img_b64:
                            try:
                                img_bytes = base64.b64decode(img_b64)
                                collected_screenshots.append(img_bytes)
                            except Exception:
                                pass
                    elif tool_name == "web_navigate":
                        web_content = result.get("content", "") or result.get("text_preview", "")
                        if web_content:
                            found_urls = re.findall(r'https?://[^\s<>"\')]+' , str(web_content))
                            collected_links.extend(found_urls)

                    logger.debug("Tool %s returned: %s", tool_name, str(result)[:120])

                    # Trigger cutscene for high-impact skills
                    if tool_name in self.CUTSCENE_TOOLS and self._on_cutscene:
                        try:
                            asyncio.get_running_loop().call_soon_threadsafe(self._on_cutscene)
                        except RuntimeError:
                            # No running loop (shouldn't happen in normal flow)
                            self._on_cutscene()
                except MCPToolError as exc:
                    tool_results.append({"tool": tool_name, "error": str(exc)})

            # Post-action screenshot
            try:
                ss_result = await self._mcp.call_tool("take_screenshot", {})
                img_b64 = ss_result.get("image_base64")
                if img_b64:
                    try:
                        img_bytes = base64.b64decode(img_b64)
                        collected_screenshots.append(img_bytes)
                        current_screenshot_b64 = img_b64
                    except Exception:
                        pass
            except Exception:
                pass

            # Append to history
            self._history.append({"role": "assistant", "content": response_text or ""})
            self._history.append({
                "role": "user",
                "content": f"Tool results: {json.dumps(tool_results, default=str)[:3000]}",
            })

        # Final exchange
        self._history.append({"role": "user", "content": prompt})
        self._history.append({"role": "assistant", "content": final_response})
        if len(self._history) > 40:
            self._history = self._history[-40:]

        text = final_response or "Action completed."

        # Deduplicate links
        seen = set()
        unique_links = []
        for link in collected_links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        return ProcessResult(
            text=text,
            screenshots=collected_screenshots,
            web_links=unique_links,
        )

    def _post_hitl_to_gui(self, request) -> None:
        """Post a HITL request to the Qt GUI thread."""
        if self._on_hitl_request:
            try:
                asyncio.get_running_loop().call_soon_threadsafe(
                    lambda: self._on_hitl_request(request)
                )
            except RuntimeError:
                self._on_hitl_request(request)

    def handle_hitl_response(self, request_id: str, approved: bool, args: Optional[dict] = None) -> None:
        """Called from the Qt thread when the user approves/denies a HITL request."""
        self._hitl.complete_hitl(request_id, approved, args)

    def clear_history(self) -> None:
        self._history.clear()

    def set_force_offline(self, force: bool) -> None:
        """Manually force offline mode regardless of network status."""
        self._force_offline = force
        logger.info("Force offline mode: %s", force)

    @property
    def force_offline(self) -> bool:
        return self._force_offline


# ── CLI entry point (testing only) ───────────────────────────────────────────

async def _interactive_loop() -> None:
    """Simple interactive REPL for testing without GUI."""
    import readline  # noqa: F401

    orch = Orchestrator()
    await orch.start()

    logger.info("Great Sage Interactive Mode. Type 'quit' to exit, 'clear' to reset.")
    try:
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not prompt:
                continue
            if prompt.lower() == "quit":
                break
            if prompt.lower() == "clear":
                orch.clear_history()
                logger.info("History cleared.")
                continue

            result = await orch.process_prompt(prompt)
            logger.info("Response: %s", result.text[:200])
    finally:
        await orch.stop()


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_interactive_loop())
