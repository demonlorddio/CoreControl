"""
Network monitoring and status utility for CoreControl.
Checks internet connectivity and provides status to the orchestrator.
"""

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Optional

# Log to stderr only — never stdout (would corrupt MCP JSON-RPC)
logger = logging.getLogger(__name__)


@dataclass
class NetworkStatus:
    is_online: bool
    latency_ms: Optional[float]
    last_checked: float = field(default_factory=time.time)
    check_host: str = "8.8.8.8"
    check_port: int = 53


# Hosts to probe in order; first success = online
_PROBE_TARGETS = [
    ("8.8.8.8", 53),       # Google DNS
    ("1.1.1.1", 53),       # Cloudflare DNS
    ("208.67.222.222", 53), # OpenDNS
]
_CONNECT_TIMEOUT = 2.0  # seconds


def check_connectivity(timeout: float = _CONNECT_TIMEOUT) -> NetworkStatus:
    """
    Synchronously probe external hosts to determine connectivity.
    Returns a NetworkStatus with latency in ms, or None if offline.
    """
    for host, port in _PROBE_TARGETS:
        try:
            start = time.monotonic()
            with socket.create_connection((host, port), timeout=timeout):
                latency_ms = (time.monotonic() - start) * 1000
                logger.debug("Network online: %s:%d (%.1f ms)", host, port, latency_ms)
                return NetworkStatus(
                    is_online=True,
                    latency_ms=round(latency_ms, 2),
                    last_checked=time.time(),
                    check_host=host,
                    check_port=port,
                )
        except OSError as exc:
            logger.debug("Probe %s:%d failed: %s", host, port, exc)

    logger.info("Network appears offline — all probes failed")
    return NetworkStatus(
        is_online=False,
        latency_ms=None,
        last_checked=time.time(),
    )


async def check_connectivity_async(timeout: float = _CONNECT_TIMEOUT) -> NetworkStatus:
    """Async wrapper so the orchestrator can await without blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, check_connectivity, timeout)


class NetworkMonitor:
    """
    Background monitor that re-checks connectivity at a fixed interval
    and caches the last known status for fast reads.
    """

    def __init__(self, poll_interval: float = 15.0):
        self._poll_interval = poll_interval
        self._status: NetworkStatus = NetworkStatus(
            is_online=False, latency_ms=None
        )
        self._task: Optional[asyncio.Task] = None

    @property
    def status(self) -> NetworkStatus:
        return self._status

    @property
    def is_online(self) -> bool:
        return self._status.is_online

    async def start(self) -> None:
        """Start the background polling loop."""
        # Initial check
        self._status = await check_connectivity_async()
        logger.info(
            "Initial network status: %s",
            "ONLINE" if self._status.is_online else "OFFLINE",
        )
        self._task = asyncio.create_task(self._poll_loop(), name="network-monitor")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            prev_online = self._status.is_online
            self._status = await check_connectivity_async()
            if self._status.is_online != prev_online:
                state = "ONLINE" if self._status.is_online else "OFFLINE"
                logger.info("Network state changed → %s", state)
