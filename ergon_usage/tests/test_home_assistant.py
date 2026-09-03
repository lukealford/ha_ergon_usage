"""Tests for the Home Assistant Supervisor statistics WebSocket client."""

from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.errors import ImportError as ErgonImportError
from app.home_assistant import HomeAssistantClient, StatisticMetadata
from app.models import StatisticPoint

TOKEN = "super-secret-supervisor-token"


@dataclass
class FakeHAServer:
    auth_message: dict | None = None
    import_message: dict | None = None
    auth_reply: str = "ok"  # "ok" or "invalid"
    result_success: bool = True
    drop_after_auth: bool = False
    drop_after_command: bool = False
    hang_after_auth_required: bool = False


async def _make_app(server: FakeHAServer) -> web.Application:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        if server.hang_after_auth_required:
            await asyncio.Event().wait()
            return ws
        server.auth_message = await ws.receive_json()
        if server.auth_reply == "invalid":
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        if server.drop_after_auth:
            await ws.close()
            return ws
        command = await ws.receive_json()
        server.import_message = command
        if server.drop_after_command:
            await ws.close()
            return ws
        result: dict = {"id": command["id"], "type": "result", "success": server.result_success}
        if not server.result_success:
            result["error"] = {"code": "unknown_error", "message": "recorder exploded"}
        await ws.send_json(result)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/core/websocket", websocket_handler)
    return app


@pytest_asyncio.fixture
async def ws_server():
    server = FakeHAServer()
    test_server = TestServer(await _make_app(server))
    await test_server.start_server()
    server.url = str(test_server.make_url("/core/websocket")).replace("http://", "ws://", 1)
    yield server
    await test_server.close()


def make_client(server: FakeHAServer) -> HomeAssistantClient:
    return HomeAssistantClient(TOKEN, base_url=server.url)


def usage_points() -> list[StatisticPoint]:
    return [
        StatisticPoint(
            start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            sum=Decimal("1.5"),
        ),
        StatisticPoint(
            start=datetime(2026, 1, 15, 0, 30, tzinfo=timezone.utc),
            sum=Decimal("2.75"),
            state=Decimal("0.52"),
        ),
    ]


class TestImportStatistics:
    @pytest.mark.asyncio
    async def test_import_authenticates_and_uses_current_metadata(self, ws_server):
        await make_client(ws_server).import_statistics(
            StatisticMetadata("ergon:a_test_tariff_11", "Ergon Tariff 11", "energy", "kWh"),
            usage_points(),
        )
        assert ws_server.auth_message == {"type": "auth", "access_token": TOKEN}
        payload = ws_server.import_message
        assert payload["type"] == "recorder/import_statistics"
        assert payload["metadata"]["source"] == "ergon"
        assert payload["metadata"]["statistic_id"] == "ergon:a_test_tariff_11"
        assert payload["metadata"]["name"] == "Ergon Tariff 11"
        assert payload["metadata"]["has_sum"] is True
        assert payload["metadata"]["mean_type"] == 0
        assert payload["metadata"]["unit_class"] == "energy"
        assert payload["metadata"]["unit_of_measurement"] == "kWh"

    @pytest.mark.asyncio
    async def test_cost_metadata_matches_external_utility_pattern(self, ws_server):
        points = [StatisticPoint(datetime(2026, 1, 15, tzinfo=timezone.utc), Decimal("0.4"), Decimal("0.52"))]
        await make_client(ws_server).import_statistics(
            StatisticMetadata("ergon:a_test_tariff_11_cost", "Ergon Tariff 11 cost", None, None),
            points,
        )
        metadata = ws_server.import_message["metadata"]
        assert metadata["source"] == "ergon"
        assert metadata["unit_class"] is None
        assert metadata["unit_of_measurement"] is None
        assert ws_server.import_message["stats"][0]["state"] == 0.52

    @pytest.mark.asyncio
    async def test_stats_serialized_with_utc_iso_and_state_only_when_set(self, ws_server):
        await make_client(ws_server).import_statistics(
            StatisticMetadata("ergon:a_test_tariff_11", "Ergon Tariff 11", "energy", "kWh"),
            usage_points(),
        )
        stats = ws_server.import_message["stats"]
        assert stats[0] == {"start": "2026-01-15T00:00:00+00:00", "sum": 1.5}
        assert "state" not in stats[0]
        assert stats[1]["start"] == "2026-01-15T00:30:00+00:00"
        assert stats[1]["sum"] == 2.75
        assert stats[1]["state"] == 0.52

    @pytest.mark.asyncio
    async def test_auth_invalid_raises_sanitized_import_error(self, ws_server, caplog):
        ws_server.auth_reply = "invalid"
        client = make_client(ws_server)
        with caplog.at_level("ERROR"):
            with pytest.raises(ErgonImportError):
                await client.import_statistics(
                    StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
                )
        assert TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_unsuccessful_result_raises_import_error(self, ws_server, caplog):
        ws_server.result_success = False
        with caplog.at_level("ERROR"):
            with pytest.raises(ErgonImportError):
                await make_client(ws_server).import_statistics(
                    StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
                )
        assert TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_connection_loss_mid_exchange_raises_import_error(self, ws_server, caplog):
        ws_server.drop_after_command = True
        with caplog.at_level("ERROR"):
            with pytest.raises(ErgonImportError):
                await make_client(ws_server).import_statistics(
                    StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
                )
        assert TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_connection_loss_after_auth_raises_import_error(self, ws_server):
        ws_server.drop_after_auth = True
        with pytest.raises(ErgonImportError):
            await make_client(ws_server).import_statistics(
                StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
            )

    @pytest.mark.asyncio
    async def test_empty_points_rejected_before_connecting(self, ws_server):
        with pytest.raises(ErgonImportError):
            await make_client(ws_server).import_statistics(
                StatisticMetadata("ergon:x", "X", "energy", "kWh"), []
            )
        assert ws_server.auth_message is None
        assert ws_server.import_message is None

    @pytest.mark.asyncio
    async def test_token_never_in_error_message(self, ws_server):
        ws_server.result_success = False
        client = make_client(ws_server)
        with pytest.raises(ErgonImportError) as exc_info:
            await client.import_statistics(
                StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
            )
        assert TOKEN not in str(exc_info.value)
        assert TOKEN not in exc_info.value.safe_message

    @pytest.mark.asyncio
    async def test_unresponsive_server_raises_import_error_within_timeout(self, ws_server, caplog):
        ws_server.hang_after_auth_required = True
        client = HomeAssistantClient(TOKEN, base_url=ws_server.url, receive_timeout=0.2)
        with caplog.at_level("ERROR"):
            with pytest.raises(ErgonImportError):
                await client.import_statistics(
                    StatisticMetadata("ergon:x", "X", "energy", "kWh"), usage_points()
                )
        assert TOKEN not in caplog.text
        assert "hang_after_auth_required" not in caplog.text
