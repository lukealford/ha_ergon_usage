"""Ingress web UI and JSON status endpoints for the Ergon Usage add-on."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import html
import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_INGRESS_PORT = 8099


def _dec(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sanitized_summary(summary: Any) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "reason": summary.reason,
        "rates_changed": summary.rates_changed,
        "readings_new": summary.readings_new,
        "readings_corrected": summary.readings_corrected,
        "backfill_days_processed": summary.backfill_days_processed,
        "backfill_days_failed": summary.backfill_days_failed,
        "errors": list(summary.errors),
        "gaps": list(summary.gaps),
    }


def build_status_payload(snapshot: Any) -> dict[str, Any]:
    """Serialize a coordinator snapshot to JSON-safe, credential-free data."""

    rates = {
        tariff: {
            "per_kwh_aud": _dec(rate.per_kwh_aud),
            "daily_supply_aud": _dec(rate.daily_supply_aud),
            "observed_at": _iso(rate.observed_at),
            "usage_effective_at": _iso(rate.usage_effective_at),
            "supply_effective_at": _iso(rate.supply_effective_at),
        }
        for tariff, rate in snapshot.rates.items()
    }
    rate_periods = {
        tariff: [
            {
                "per_kwh_aud": _dec(period.per_kwh_aud),
                "daily_supply_aud": _dec(period.daily_supply_aud),
                "usage_effective_at": _iso(period.usage_effective_at),
                "supply_effective_at": _iso(period.supply_effective_at),
            }
            for period in periods
        ]
        for tariff, periods in snapshot.rate_periods.items()
    }
    costs = {
        tariff: {
            "usage_aud": _dec(cost.usage_aud),
            "supply_aud": _dec(cost.supply_aud),
        }
        for tariff, cost in snapshot.costs.items()
    }
    return {
        "phase": snapshot.phase,
        "rates": rates,
        "rate_periods": rate_periods,
        "costs": costs,
        "backfill": {
            "completed_days": snapshot.backfill_completed,
            "total_days": snapshot.backfill_total,
        },
        "imports": {
            statistic_id: _iso(through) for statistic_id, through in snapshot.imports.items()
        },
        "last_run": _sanitized_summary(snapshot.last_run),
        "error": snapshot.error,
    }


def _rate_row(tariff: str, rate: dict[str, Any]) -> str:
    return (
        "<tr><th scope=\"row\">{t}</th>"
        "<td>{per} AUD/kWh</td><td>{sup} AUD/day</td>"
        "<td>{ue}</td><td>{se}</td></tr>".format(
            t=html.escape(str(tariff)),
            per=html.escape(str(rate["per_kwh_aud"])),
            sup=html.escape(str(rate["daily_supply_aud"])),
            ue=html.escape(str(rate["usage_effective_at"])),
            se=html.escape(str(rate["supply_effective_at"])),
        )
    )


def _render_rates(payload: dict[str, Any]) -> str:
    rows = [_rate_row(tariff, rate) for tariff, rate in payload["rates"].items()]
    prior = [
        _rate_row(tariff, period)
        for tariff, periods in payload["rate_periods"].items()
        for period in periods
    ]
    return (
        "<h2>Current rates</h2><table><caption>Per-tariff rates and effective "
        "boundaries</caption><thead><tr><th>Tariff</th><th>Usage</th><th>Supply</th>"
        "<th>Usage effective</th><th>Supply effective</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "<h2>Prior rate periods</h2><table><caption>Historical rate "
        "periods</caption><thead><tr><th>Tariff</th><th>Usage</th><th>Supply</th>"
        "<th>Usage effective</th><th>Supply effective</th></tr></thead><tbody>"
        + "".join(prior)
        + "</tbody></table>"
    )


def _render_costs(payload: dict[str, Any]) -> str:
    rows = []
    for tariff, cost in payload["costs"].items():
        rows.append(
            "<tr><th scope=\"row\">{t}</th>"
            "<td>{u} AUD usage</td><td>{s} AUD supply</td></tr>".format(
                t=html.escape(str(tariff)),
                u=html.escape(str(cost["usage_aud"])),
                s=html.escape(str(cost["supply_aud"])),
            )
        )
    return (
        "<h2>Accumulated costs</h2><table><caption>Separate usage and supply "
        "cost components</caption><thead><tr><th>Tariff</th><th>Usage</th>"
        "<th>Supply</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_index(payload: dict[str, Any]) -> str:
    """Render the semantic HTML status page (relative URLs only)."""

    error = payload.get("error")
    last_run = payload.get("last_run")
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Ergon Usage</title></head><body>"
        "<h1>Ergon Usage</h1>"
        "<progress max=\"100\" value=\"0\">Synchronizing</progress>"
        f"<p>Phase: {html.escape(str(payload['phase']))}</p>"
        "<p>Backfill completed days: "
        f"{html.escape(str(payload['backfill']['completed_days']))}</p>"
        + _render_rates(payload)
        + _render_costs(payload)
        + "<h2>Last run</h2>"
        + (
            "<p>{reason}: {new} new readings</p>".format(
                reason=html.escape(str(last_run["reason"])),
                new=html.escape(str(last_run["readings_new"])),
            )
            if last_run
            else "<p>No run recorded yet.</p>"
        )
        + "<h2>Error</h2><p>"
        + (html.escape(str(error)) if error else "None")
        + "</p>"
        '<form method="post" action="./api/run">'
        '<button type="submit">Run now</button></form>'
        "</body></html>"
    )


def create_app(coordinator: Any) -> web.Application:
    """Build the aiohttp application for ingress access."""

    app = web.Application()
    app[web.AppKey("coordinator", object)] = coordinator

    def _no_store(response: web.StreamResponse) -> web.StreamResponse:
        response.headers["Cache-Control"] = "no-store"
        return response

    async def status(request: web.Request) -> web.Response:
        snapshot = coordinator.snapshot()
        return _no_store(web.json_response(build_status_payload(snapshot)))

    async def run_now(request: web.Request) -> web.Response:
        accepted, coalesced = coordinator.run_now("manual")
        return _no_store(
            web.json_response({"accepted": accepted, "coalesced": coalesced}, status=202)
        )

    async def index(request: web.Request) -> web.Response:
        payload = build_status_payload(coordinator.snapshot())
        return _no_store(
            web.Response(text=render_index(payload), content_type="text/html")
        )

    async def health(request: web.Request) -> web.Response:
        return _no_store(web.json_response({"ok": True}))

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/run", run_now)
    app.router.add_get("/health", health)
    return app
