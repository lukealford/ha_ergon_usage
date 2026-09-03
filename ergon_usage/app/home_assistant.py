"""Home Assistant Supervisor statistics import over the WebSocket API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from typing import Sequence

import aiohttp

from ergon_usage.app.errors import ImportError
from ergon_usage.app.models import StatisticPoint

_LOGGER = logging.getLogger(__name__)

_WS_URL = "ws://supervisor/core/websocket"
_CONNECT_TIMEOUT = 30.0
_RECEIVE_TIMEOUT = 30.0


@dataclass(frozen=True)
class StatisticMetadata:
    """Metadata describing an external statistic to import."""

    statistic_id: str
    name: str
    unit_class: str | None
    unit_of_measurement: str | None


class HomeAssistantClient:
    """Imports external statistics into Home Assistant via the Supervisor WebSocket API."""

    def __init__(
        self,
        settings_or_token: object,
        session_factory=None,
        *,
        base_url: str = _WS_URL,
        connect_timeout: float = _CONNECT_TIMEOUT,
        receive_timeout: float = _RECEIVE_TIMEOUT,
    ) -> None:
        token = getattr(settings_or_token, "supervisor_token", None)
        if token is None:
            token = settings_or_token
        if not isinstance(token, str) or not token:
            raise ValueError("A supervisor token is required.")
        self._token = token
        self._session_factory = session_factory or aiohttp.ClientSession
        self._base_url = base_url
        self._connect_timeout = connect_timeout
        self._receive_timeout = receive_timeout

    async def import_statistics(
        self,
        metadata: StatisticMetadata,
        points: Sequence[StatisticPoint],
    ) -> None:
        if not points:
            raise ImportError("No usage points available to import.")

        stats = []
        for point in points:
            entry: dict = {"start": _utc_iso(point.start), "sum": float(point.sum)}
            if point.state is not None:
                entry["state"] = float(point.state)
            stats.append(entry)

        try:
            timeout = aiohttp.ClientTimeout(total=self._connect_timeout)
            async with self._session_factory(timeout=timeout) as session:
                async with session.ws_connect(
                    self._base_url,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientWSTimeout(ws_close=self._connect_timeout),
                ) as ws:
                    await self._authenticate(ws)
                    await self._send_import(ws, metadata, stats)
        except ImportError:
            raise
        except (aiohttp.ClientError, ConnectionError, OSError) as error:
            _LOGGER.debug("Statistics import connection failed: %s", type(error).__name__)
            raise ImportError("Unable to connect to Home Assistant to import statistics.") from error

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        message = await self._receive_json(ws)
        if message.get("type") != "auth_required":
            raise ImportError("Home Assistant did not request authentication.")
        await ws.send_json({"type": "auth", "access_token": self._token})
        message = await self._receive_json(ws)
        if message.get("type") == "auth_invalid":
            raise ImportError("Home Assistant rejected the Supervisor authentication token.")
        if message.get("type") != "auth_ok":
            raise ImportError("Home Assistant did not confirm authentication.")

    async def _send_import(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        metadata: StatisticMetadata,
        stats: list[dict],
    ) -> None:
        command = {
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "source": "ergon",
                "statistic_id": metadata.statistic_id,
                "name": metadata.name,
                "has_sum": True,
                "mean_type": 0,
                "unit_class": metadata.unit_class,
                "unit_of_measurement": metadata.unit_of_measurement,
            },
            "stats": stats,
        }
        await ws.send_json(command)
        message = await self._receive_json(ws)
        if message.get("type") != "result" or message.get("id") != command["id"]:
            raise ImportError("Home Assistant returned an unexpected response during import.")
        if not message.get("success"):
            raise ImportError("Home Assistant reported an error importing statistics.")

    async def _receive_json(self, ws: aiohttp.ClientWebSocketResponse) -> dict:
        while True:
            try:
                message = await ws.receive(timeout=self._receive_timeout)
            except asyncio.TimeoutError:
                _LOGGER.debug("Statistics import receive timed out")
                raise ImportError("Home Assistant did not respond in time during import.")
            if message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                raise ImportError("Connection to Home Assistant was lost during import.")
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                return json.loads(message.data)
            except json.JSONDecodeError:
                raise ImportError("Home Assistant returned an invalid response during import.")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
