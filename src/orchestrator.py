"""
CoreControl main orchestrator.
Multi-modal perception loop with online/offline LLM fallback engine.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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

# Online LLM config (OpenAI-compatible, e.g., Omniroute, OpenAI, etc.)
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
        # Send initialize
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
    Uses an OpenAI-compatible API (Omniroute, OpenAI, etc.) with vision support.
    Falls back to Ollama if no online API is configured.
    """

    def __init__(self) -> None:
        self._base_url = ONLINE_API_BASE_URL.rstrip("/")
        self._model = ONLINE_MODEL
        self._max_tokens = ONLINE_MAX_TOKENS
        self._timeout = ONLINE_TIMEOUT

        try:
            from openai import AsyncOpenAI
            # Use empty key for services like OmniRoute that support free tiers
            api_key = ONLINE_API_KEY if ONLINE_API_KEY else "omni-route-free"
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=self._base_url,
            )
            logger.info("Online engine initialized: %s (model: %s)", self._base_url, self._model)
        except ImportError:
            logger.error("openai SDK not installed — install with: pip install openai")
            self._client = None

    async def process(
        self,
        prompt: str,
        screenshot_b64: Optional[str],
        tool_definitions: list[dict],
        conversation_history: list[dict],
    ) -> tuple[str, list[dict]]:
        """
        Send prompt + optional screenshot to LLM, return (response_text, tool_calls).
        """
        if not self._client:
            raise RuntimeError("Online API client not available — check settings.online_llm.api_base_url")

        # Build messages in OpenAI format
        messages = []
        for msg in conversation_history:
            messages.append(msg)

        # Add current user message (with optional image)
        user_content: list[dict] | str = prompt
        if screenshot_b64:
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{screenshot_b64}"
                    },
                },
            ]
        messages.append({"role": "user", "content": user_content})

        # Build tools in OpenAI format
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
        # Handle tool_calls on message object (OpenAI-compatible APIs)
        raw_tool_calls = getattr(choice.message, 'tool_calls', None) or []
        for tc in raw_tool_calls:
            try:
                import json as _json
                args_str = tc.function.arguments if hasattr(tc.function, 'arguments') else ""
                input_args = _json.loads(args_str) if args_str else {}
            except (json.JSONDecodeError, AttributeError):
                input_args = {}
            tool_calls.append({
                "name": tc.function.name,
                "input": input_args,
            })

        return response_text, tool_calls


# ── Offline engine (Ollama) ───────────────────────────────────────────────────

class OfflineEngine:
    """Uses a locally hosted Ollama model for fully offline operation."""

    def __init__(self) -> None:
        self._base_url = OLLAMA_BASE_URL
        self._model = OLLAMA_MODEL
        self._fallbacks = OLLAMA_FALLBACKS

    async def _ocr_screenshot(self, screenshot_b64: str) -> str:
        """Extract text from screenshot via pytesseract (offline OCR)."""
        try:
            import io
            import pytesseract
            from PIL import Image

            img_bytes = base64.b64decode(screenshot_b64)
            img = Image.open(io.BytesIO(img_bytes))
            text = pytesseract.image_to_string(img)
            return f"[Screen OCR]\n{text[:2000]}" if text.strip() else "[Screen: no readable text]"
        except ImportError:
            return "[Screen: OCR not available — install pytesseract]"
        except Exception as exc:
            logger.warning("OCR failed: %s", exc)
            return "[Screen: OCR failed]"

    async def process(
        self,
        prompt: str,
        screenshot_b64: Optional[str],
        tool_definitions: list[dict],
        conversation_history: list[dict],
    ) -> tuple[str, list[dict]]:
        """Send prompt to Ollama with tool definitions in system prompt."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx not installed — run: pip install httpx")

        screen_context = ""
        if screenshot_b64:
            screen_context = await self._ocr_screenshot(screenshot_b64)

        tools_json = json.dumps(
            [{"name": t["name"], "description": t["description"]} for t in tool_definitions],
            indent=2,
        )

        system_prompt = (
            "You are CoreControl, an offline AI desktop assistant.\n"
            f"Available tools:\n{tools_json}\n\n"
            "To call a tool, respond with EXACTLY this JSON (nothing else on that line):\n"
            'TOOL_CALL: {"name": "<tool_name>", "input": {<args>}}\n\n'
            "After tool results are provided, continue reasoning and call more tools if needed.\n"
            "Give a final plain-text response once done."
        )

        messages = []
        for msg in conversation_history[-10:]:  # limit context window
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            messages.append({"role": role, "content": content})

        user_msg = prompt
        if screen_context:
            user_msg = f"{screen_context}\n\nUser request: {prompt}"
        messages.append({"role": "user", "content": user_msg})

        models_to_try = [self._model] + self._fallbacks
        for model in models_to_try:
            try:
                async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/chat",
                        json={
                            "model": model,
                            "messages": [{"role": "system", "content": system_prompt}] + messages,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    response_text: str = data["message"]["content"]
                    logger.debug("Ollama response (%s): %r", model, response_text[:120])
                    break
            except Exception as exc:
                logger.warning("Ollama model %r failed: %s", model, exc)
                response_text = None

        if response_text is None:
            return "❌ All local LLM models unavailable. Check Ollama is running.", []

        # Parse TOOL_CALL lines
        tool_calls: list[dict] = []
        clean_lines = []
        for line in response_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("TOOL_CALL:"):
                try:
                    payload = json.loads(stripped[len("TOOL_CALL:"):].strip())
                    tool_calls.append({"name": payload["name"], "input": payload.get("input", {})})
                except json.JSONDecodeError as exc:
                    logger.warning("Could not parse TOOL_CALL: %s", exc)
            else:
                clean_lines.append(line)

        return "\n".join(clean_lines).strip(), tool_calls


# ── HITL filter ───────────────────────────────────────────────────────────────

class HITLFilter:
    """
    Routes high-risk tool calls through the Telegram gateway for human approval.
    Falls back to auto-approve when no gateway is configured.
    """

    def __init__(self, gateway=None) -> None:
        self._gateway = gateway

    async def check(
        self,
        tool_name: str,
        tool_args: dict,
        screenshot_b64: Optional[str],
    ) -> tuple[bool, dict]:
        """
        Returns (approved: bool, final_args: dict).
        final_args may differ from tool_args if user chose 'Edit'.
        """
        if tool_name in AUTO_APPROVE_TOOLS:
            return True, tool_args

        if tool_name not in HIGH_RISK_TOOLS:
            return True, tool_args

        if self._gateway is None:
            logger.info("HITL: no gateway — auto-approving %s", tool_name)
            return True, tool_args

        request_id = str(uuid.uuid4())[:8]
        description = f"Execute {tool_name} with args: {json.dumps(tool_args)[:200]}"

        try:
            from src.bridge.telegram_bot import HITLDecision
            result = await self._gateway.request_hitl_approval(
                request_id=request_id,
                tool_name=tool_name,
                tool_args=tool_args,
                description=description,
                screenshot_b64=screenshot_b64 if SCREENSHOT_ON_CONFIRMATION else None,
            )

            if result.decision == HITLDecision.APPROVE:
                return True, tool_args
            elif result.decision == HITLDecision.EDIT:
                return True, result.modified_args or tool_args
            else:  # DENY
                logger.info("HITL denied: %s", tool_name)
                return False, tool_args

        except asyncio.TimeoutError:
            logger.warning("HITL timeout for %s — denying by default", tool_name)
            return False, tool_args
        except Exception as exc:
            logger.error("HITL error: %s — denying", exc)
            return False, tool_args


# ── Main Orchestrator ─────────────────────────────────────────────────────────

@dataclass
class ProcessResult:
    """Result from process_prompt containing text response and any attachments."""
    text: str
    screenshots: list[bytes] = field(default_factory=list)
    web_links: list[str] = field(default_factory=list)


class Orchestrator:
    """
    Central agentic loop for CoreControl.
    Manages the perception → plan → act → verify cycle.
    """

    def __init__(self, telegram_gateway=None) -> None:
        from src.utils.network import NetworkMonitor

        self._net = NetworkMonitor(poll_interval=15.0)
        self._mcp = LocalMCPClient()
        self._online_engine = OnlineEngine()
        self._offline_engine = OfflineEngine()
        self._hitl = HITLFilter(gateway=telegram_gateway)
        self._history: list[dict] = []
        self._tool_definitions: list[dict] = []
        self._running = False

    async def start(self) -> None:
        await self._net.start()
        await self._mcp.start()
        self._tool_definitions = await self._fetch_tool_definitions()
        self._running = True
        logger.info(
            "Orchestrator started (network=%s)",
            "online" if self._net.is_online else "offline",
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

    async def process_prompt(self, prompt: str, user_id: Optional[int] = None) -> ProcessResult:
        """
        Full perception → plan → act → verify cycle for a single prompt.
        Returns a ProcessResult with text response and any attachments (screenshots, links).
        """
        logger.info("Processing prompt: %r (user=%s)", prompt[:80], user_id)

        # 1. Take an initial screenshot to capture current visual state
        initial_screenshot: Optional[str] = None
        try:
            ss_result = await self._mcp.call_tool("take_screenshot", {})
            initial_screenshot = ss_result.get("image_base64")
        except Exception as exc:
            logger.warning("Initial screenshot failed: %s", exc)

        # 2. Select engine based on network state
        if self._net.is_online:
            logger.info("Using ONLINE engine (Claude)")
            engine = self._online_engine
        else:
            logger.info("Using OFFLINE engine (Ollama)")
            engine = self._offline_engine

        # Collect output artifacts
        collected_screenshots: list[bytes] = []
        collected_links: list[str] = []

        # 3. Agentic loop — up to 10 iterations
        final_response = ""
        current_screenshot_b64: Optional[str] = initial_screenshot
        for iteration in range(10):
            try:
                response_text, tool_calls = await engine.process(
                    prompt=prompt,
                    screenshot_b64=current_screenshot_b64 if iteration == 0 else None,
                    tool_definitions=self._tool_definitions,
                    conversation_history=self._history,
                )
            except Exception as exc:
                logger.error("Engine error on iteration %d: %s", iteration, exc)
                return ProcessResult(text=f"❌ Engine error: {exc}")

            if response_text:
                final_response = response_text

            # Extract links from response text
            if response_text:
                import re
                urls = re.findall(r'https?://[^\s<>"\')]+' , response_text)
                collected_links.extend(urls)

            if not tool_calls:
                # No more tool calls — cycle complete
                break

            # 4. Execute each tool call (with HITL gating)
            tool_results = []
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc.get("input", {})

                approved, final_args = await self._hitl.check(
                    tool_name, tool_args, current_screenshot_b64
                )

                if not approved:
                    tool_results.append({
                        "tool": tool_name,
                        "error": "Action denied by user via HITL.",
                    })
                    continue

                try:
                    result = await self._mcp.call_tool(tool_name, final_args)
                    tool_results.append({"tool": tool_name, "result": result})

                    # Extract screenshot from result
                    if tool_name == "take_screenshot":
                        img_b64 = result.get("image_base64")
                        if img_b64:
                            try:
                                img_bytes = base64.b64decode(img_b64)
                                collected_screenshots.append(img_bytes)
                            except Exception:
                                pass
                    elif tool_name == "web_navigate":
                        web_content = result.get("content", "")
                        if web_content:
                            # Extract any links from web content
                            import re as re_mod
                            found_urls = re_mod.findall(r'https?://[^\s<>"\')]+' , str(web_content))
                            collected_links.extend(found_urls)

                    logger.debug("Tool %s returned: %s", tool_name, str(result)[:120])
                except MCPToolError as exc:
                    tool_results.append({"tool": tool_name, "error": str(exc)})

            # 5. Post-action screenshot for closed-loop visual verification
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

            # Append tool results to history for the next iteration
            self._history.append({
                "role": "assistant",
                "content": response_text or "",
            })
            self._history.append({
                "role": "user",
                "content": f"Tool results: {json.dumps(tool_results, default=str)[:3000]}",
            })

        # 6. Append final exchange to conversation history (cap at 20 turns)
        self._history.append({"role": "user", "content": prompt})
        self._history.append({"role": "assistant", "content": final_response})
        if len(self._history) > 40:
            self._history = self._history[-40:]

        text = final_response or "✅ Action completed."

        # Deduplicate links
        seen_links = set()
        unique_links = []
        for link in collected_links:
            if link not in seen_links:
                seen_links.add(link)
                unique_links.append(link)

        return ProcessResult(
            text=text,
            screenshots=collected_screenshots,
            web_links=unique_links,
        )

    def clear_history(self) -> None:
        self._history.clear()


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _interactive_loop() -> None:
    """Simple interactive REPL for testing without Telegram."""
    import readline  # noqa: F401 — improves input() on Unix

    orchestrator = Orchestrator()
    await orchestrator.start()

    print("CoreControl interactive mode. Type 'quit' to exit, 'clear' to reset history.")
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
                orchestrator.clear_history()
                print("History cleared.")
                continue

            response = await orchestrator.process_prompt(prompt)
            print(f"\n{response}")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_interactive_loop())
