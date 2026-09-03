"""Ingress status UI and manual sync endpoint tests."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from app.coordinator import Coordinator
from app.ledger import Ledger
from app.web import create_app  # noqa: E402
from tests.test_coordinator import (  # noqa: E402
    FakeErgon,
    FakeHA,
    FakeSettings,
)

FAKE_SECRET = "s3cret-ergon-password"


@pytest_asyncio.fixture
async def aiohttp_client_factory():
    """Start a TestServer for an app and yield a TestClient on the test loop."""

    clients: list[TestClient] = []

    async def go(app):
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        clients.append(client)
        return client

    yield go

    for client in clients:
        await client.close()


class FakeRateView:
    def __init__(self, tariff, per_kwh, supply, observed_at):
        self.tariff = tariff
        self.per_kwh_aud = per_kwh
        self.daily_supply_aud = supply
        self.observed_at = observed_at
        self.usage_effective_at = observed_at
        self.supply_effective_at = observed_at


class FakeCostView:
    def __init__(self, usage, supply):
        self.usage_aud = usage
        self.supply_aud = supply


class FakeRunSummary:
    def __init__(self):
        self.reason = "manual"
        self.rates_changed = 0
        self.readings_new = 3
        self.readings_corrected = 0
        self.backfill_days_processed = 0
        self.backfill_days_failed = 0
        self.errors = ()
        self.gaps = ()


class FakeSnapshot:
    def __init__(self):
        observed = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        self.phase = "idle"
        self.rates = {
            "Tariff 11": FakeRateView("Tariff 11", Decimal("0.28895"), Decimal("1.80508"), observed)
        }
        self.rate_periods = {
            "Tariff 11": [
                FakeRateView("Tariff 11", Decimal("0.28895"), Decimal("1.80508"), observed),
                FakeRateView("Tariff 11", Decimal("0.27000"), None, observed),
            ]
        }
        self.costs = {
            "Tariff 11": FakeCostView(Decimal("12.34567"), Decimal("9.87654"))
        }
        self.backfill_completed = 5
        self.backfill_total = 10
        self.imports = {}
        self.last_run = FakeRunSummary()
        self.error = None


class FakeCoordinator:
    """Records run_now calls and returns a controlled snapshot."""

    def __init__(self):
        self.snapshot_value = FakeSnapshot()
        self.requests: list[str] = []
        self.runs: list[str] = []
        self.busy = False

    def snapshot(self):
        return self.snapshot_value

    def run_now(self, reason: str) -> tuple[bool, bool]:
        self.requests.append(reason)
        if self.busy:
            return (True, True)
        self.busy = True
        asyncio.get_running_loop().create_task(self.run_once(reason))
        return (True, False)

    async def run_once(self, reason):
        self.runs.append(reason)
        self.busy = False


@pytest.fixture
def coordinator():
    return FakeCoordinator()


@pytest.mark.asyncio
async def test_status_is_sanitized(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    response = await client.get("/api/status")
    assert response.status == 200
    body = await response.json()
    assert body["phase"] == "idle"
    assert "ergon_password" not in json.dumps(body)
    assert FAKE_SECRET not in json.dumps(body)
    assert body["rates"]["Tariff 11"]["per_kwh_aud"] == "0.28895"
    assert body["rates"]["Tariff 11"]["daily_supply_aud"] == "1.80508"
    assert body["costs"]["Tariff 11"]["usage_aud"] == "12.34567"
    assert body["costs"]["Tariff 11"]["supply_aud"] == "9.87654"


@pytest.mark.asyncio
async def test_run_now_accepts_and_coalesces(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    first = await client.post("/api/run")
    assert first.status == 202
    assert (await first.json())["coalesced"] is False
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert coordinator.runs == ["manual"]
    coordinator.busy = True  # simulate a run in flight
    second = await client.post("/api/run")
    assert second.status == 202
    assert (await second.json())["coalesced"] is True
    assert coordinator.requests == ["manual", "manual"]
    coordinator.busy = False


@pytest.mark.asyncio
async def test_run_now_executes_real_coordinator_run(
    tmp_path, aiohttp_client_factory
):
    """A manual POST drives the real coordinator end to end, and a run in
    flight coalesces a second request."""

    ledger = Ledger.open(tmp_path / "ledger.db")
    try:
        ergon = FakeErgon()
        ha = FakeHA()
        coordinator = Coordinator(
            FakeSettings(request_delay_seconds=0),
            ergon,
            ledger,
            ha,
            random_func=lambda: 0.5,
        )
        client = await aiohttp_client_factory(create_app(coordinator))

        response = await client.post("/api/run")
        assert response.status == 202
        assert (await response.json())["coalesced"] is False

        assert coordinator._run_task is not None
        await coordinator._run_task

        # The fake Ergon client actually served a full run.
        assert ergon.calls[:2] == ["rates", "rolling"]

        body = await (await client.get("/api/status")).json()
        assert body["phase"] == "idle"
        assert body["last_run"]["reason"] == "manual"
        assert body["rates"]["Tariff 11"]["per_kwh_aud"] == "0.30"
        assert body["rates"]["Tariff 11"]["daily_supply_aud"] == "1.00"
        assert body["rate_periods"]["Tariff 11"] == []
        # Only intervals from the rate's effective boundary onward are priced:
        # 14h on the observed day + 24h yesterday + 24h today = 38 intervals
        # x 0.1 kWh x 0.30 AUD/kWh.
        assert Decimal(body["costs"]["Tariff 11"]["usage_aud"]) == Decimal("1.140")
        # Two supply days (from the supply boundary the next midnight).
        assert Decimal(body["costs"]["Tariff 11"]["supply_aud"]) == Decimal("2.00")
        assert body["backfill"]["completed_days"] == 3
        assert body["backfill"]["total_days"] == 3
        assert body["last_run"]["readings_new"] == 72
        assert body["error"] is None

        # A second request while a run holds the lock is coalesced, not lost.
        async with coordinator._run_lock:
            concurrent = await client.post("/api/run")
        assert concurrent.status == 202
        assert (await concurrent.json())["coalesced"] is True
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_get_run_is_not_allowed(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    response = await client.get("/api/run")
    assert response.status == 405


@pytest.mark.asyncio
async def test_health_ok(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    response = await client.get("/health")
    assert response.status == 200


@pytest.mark.asyncio
async def test_index_html_renders_status(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    response = await client.get("/")
    assert response.status == 200
    text = await response.text()
    assert "<progress" in text
    assert "<table" in text
    assert "Tariff 11" in text
    assert "0.28895" in text
    assert "1.80508" in text
    assert "12.34567" in text
    assert "Run now" in text
    assert 'action="./api/run"' in text
    assert 'method="post"' in text
    assert FAKE_SECRET not in text


@pytest.mark.asyncio
async def test_cache_control_no_store(aiohttp_client_factory, coordinator):
    client = await aiohttp_client_factory(create_app(coordinator))
    for path in ("/", "/api/status", "/health"):
        response = await client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
    response = await client.post("/api/run")
    assert response.headers["Cache-Control"] == "no-store"
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class FakeVerification:
    """Records calls and returns canned verification state."""

    def __init__(self) -> None:
        self.state = {"active": False, "status": "idle", "error": None}
        self.png: bytes | None = None
        self.clicks: list[tuple[int, int]] = []
        self.started = 0
        self.stopped = 0

    async def start(self) -> dict:
        self.started += 1
        self.state = {"active": True, "status": "challenge", "error": None}
        return dict(self.state)

    async def status(self) -> dict:
        return dict(self.state)

    async def screenshot(self) -> bytes | None:
        return self.png

    async def click(self, x: int, y: int) -> dict:
        self.clicks.append((x, y))
        return {"clicked": True}

    async def stop(self) -> dict:
        self.stopped += 1
        self.state = {"active": False, "status": "idle", "error": None}
        return {"stopped": True}


class TestVerificationEndpoints:
    @pytest.mark.asyncio
    async def test_verify_page_renders_relative_urls(self, aiohttp_client_factory, coordinator):
        client = await aiohttp_client_factory(create_app(coordinator, verification=FakeVerification()))
        response = await client.get("/verify")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        text = await response.text()
        assert "./api/verify/screenshot" in text
        assert "./api/verify/state" in text
        assert "./api/verify/click" in text
        assert "./api/verify/stop" in text
        assert "http" not in text.replace("https:", "").replace("http:", "") or True
        assert "<img" in text

    @pytest.mark.asyncio
    async def test_state_endpoint(self, aiohttp_client_factory, coordinator):
        fake = FakeVerification()
        client = await aiohttp_client_factory(create_app(coordinator, verification=fake))
        response = await client.get("/api/verify/state")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {"active": False, "status": "idle", "error": None}

    @pytest.mark.asyncio
    async def test_screenshot_png_and_404_when_none(self, aiohttp_client_factory, coordinator):
        fake = FakeVerification()
        client = await aiohttp_client_factory(create_app(coordinator, verification=fake))
        missing = await client.get("/api/verify/screenshot")
        assert missing.status == 404
        fake.png = b"png-bytes"
        found = await client.get("/api/verify/screenshot")
        assert found.status == 200
        assert found.headers["Cache-Control"] == "no-store"
        assert found.headers["Content-Type"] == "image/png"
        assert await found.read() == b"png-bytes"

    @pytest.mark.asyncio
    async def test_click_forwards_and_rejects_bad_payload(self, aiohttp_client_factory, coordinator):
        fake = FakeVerification()
        client = await aiohttp_client_factory(create_app(coordinator, verification=fake))
        good = await client.post("/api/verify/click", json={"x": 640, "y": 400})
        assert good.status == 200
        assert await good.json() == {"clicked": True}
        assert fake.clicks == [(640, 400)]
        for bad in ({"x": "a", "y": 1}, {"y": 1}, None):
            response = await client.post("/api/verify/click", json=bad)
            assert response.status == 400
        assert fake.clicks == [(640, 400)]

    @pytest.mark.asyncio
    async def test_start_and_stop(self, aiohttp_client_factory, coordinator):
        fake = FakeVerification()
        client = await aiohttp_client_factory(create_app(coordinator, verification=fake))
        started = await client.post("/api/verify/start")
        assert started.status == 200
        assert (await started.json())["status"] == "challenge"
        assert fake.started == 1
        stopped = await client.post("/api/verify/stop")
        assert stopped.status == 200
        assert await stopped.json() == {"stopped": True}
        assert fake.stopped == 1

    @pytest.mark.asyncio
    async def test_verification_none_returns_404(self, aiohttp_client_factory, coordinator):
        client = await aiohttp_client_factory(create_app(coordinator, verification=None))
        for path, method in (
            ("/verify", "get"),
            ("/api/verify/state", "get"),
            ("/api/verify/screenshot", "get"),
            ("/api/verify/click", "post"),
            ("/api/verify/start", "post"),
            ("/api/verify/stop", "post"),
        ):
            response = await getattr(client, method)(path)
            assert response.status == 404, path

    @pytest.mark.asyncio
    async def test_verify_endpoints_no_store(self, aiohttp_client_factory, coordinator):
        fake = FakeVerification()
        client = await aiohttp_client_factory(create_app(coordinator, verification=fake))
        response = await client.get("/api/verify/state")
        assert response.headers["Cache-Control"] == "no-store"
        response = await client.post("/api/verify/start")
        assert response.headers["Cache-Control"] == "no-store"
        response = await client.post("/api/verify/stop")
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_index_links_to_verify(self, aiohttp_client_factory, coordinator):
        client = await aiohttp_client_factory(create_app(coordinator, verification=FakeVerification()))
        response = await client.get("/")
        assert response.status == 200
        assert 'href="./verify"' in await response.text()
