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
from zoneinfo import ZoneInfo

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


_HA_THEME_JS = (
    "<script>"
    "(function() {"
    "  var apply = function(vars) {"
    "    if (!vars) return;"
    "    var root = document.documentElement;"
    "    for (var k in vars) { if (k.indexOf('--') === 0) root.style.setProperty(k, vars[k]); }"
    "  };"
    "  var fromParent = function() {"
    "    try {"
    "      var doc = window.parent.document;"
    "      var cs = getComputedStyle(doc.documentElement);"
    "      var names = ['--primary-color','--accent-color','--primary-text-color','--secondary-text-color',"
    "        '--primary-background-color','--secondary-background-color','--card-background-color',"
    "        '--divider-color','--error-color','--warning-color','--success-color','--info-color',"
    "        '--text-primary-color','--ha-card-border-radius','--paper-item-disabled-color'];"
    "      var vars = {};"
    "      names.forEach(function(n) { var v = cs.getPropertyValue(n).trim(); if (v) vars[n] = v; });"
    "      if (Object.keys(vars).length) { apply(vars); return true; }"
    "    } catch (e) {}"
    "    return false;"
    "  };"
    "  if (!fromParent()) {"
    "    var tries = 0;"
    "    var iv = setInterval(function() { if (fromParent() || ++tries > 10) clearInterval(iv); }, 500);"
    "  }"
    "  window.addEventListener('message', function(ev) {"
    "    if (ev.data && ev.data.themeVars) apply(ev.data.themeVars);"
    "  });"
    "})();"
    "</script>"
)

_BASE_CSS = (
    "<style>"
    ":root {"
    "  --primary-color:#03a9f4; --accent-color:#ff9800;"
    "  --primary-text-color:#212121; --secondary-text-color:#727272;"
    "  --primary-background-color:#fafafa; --card-background-color:#ffffff;"
    "  --divider-color:#e0e0e0; --error-color:#db4437; --success-color:#43a047;"
    "  --warning-color:#ffa600; --info-color:#039be5;"
    "  --ha-card-border-radius:12px;"
    "  --ergon-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "}"
    "@media (prefers-color-scheme: dark) {"
    "  :root:not([data-ha-theme]) {"
    "    --primary-text-color:#e1e1e1; --secondary-text-color:#9b9b9b;"
    "    --primary-background-color:#111111; --card-background-color:#1c1c1c;"
    "    --divider-color:#2c2c2c;"
    "  }"
    "}"
    "* { box-sizing:border-box; }"
    "body { margin:0; padding:16px; font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "  background:var(--primary-background-color); color:var(--primary-text-color);"
    "  font-size:14px; line-height:1.5; }"
    "h1 { font-size:22px; font-weight:400; margin:0 0 16px; display:flex; align-items:center; gap:10px; }"
    "h1 .logo { width:32px; height:32px; border-radius:50%; background:var(--primary-color);"
    "  display:inline-flex; align-items:center; justify-content:center; color:#fff; font-size:16px;"
    "  font-weight:600; flex:none; }"
    "h2 { font-size:15px; font-weight:500; margin:0; }"
    ".grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; }"
    ".card { background:var(--card-background-color); border-radius:var(--ha-card-border-radius,12px);"
    "  border:1px solid var(--divider-color); padding:16px; box-shadow:0 2px 4px rgba(0,0,0,.08); }"
    ".card-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }"
    ".badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:500;"
    "  background:var(--divider-color); color:var(--primary-text-color); text-transform:capitalize; }"
    ".badge.ok { background:var(--success-color); color:#fff; }"
    ".badge.warn { background:var(--warning-color); color:#fff; }"
    ".badge.err { background:var(--error-color); color:#fff; }"
    "table { width:100%; border-collapse:collapse; font-size:13px; }"
    "caption { text-align:left; color:var(--secondary-text-color); font-size:12px; padding-bottom:6px; }"
    "th, td { padding:6px 8px; text-align:left; border-bottom:1px solid var(--divider-color); }"
    "th[scope=row] { color:var(--secondary-text-color); font-weight:500; }"
    "thead th { color:var(--secondary-text-color); font-size:11px; text-transform:uppercase;"
    "  letter-spacing:.04em; border-bottom:1px solid var(--divider-color); }"
    "tbody tr:last-child th, tbody tr:last-child td { border-bottom:none; }"
    ".num { font-family:var(--ergon-mono); font-variant-numeric:tabular-nums; }"
    ".muted { color:var(--secondary-text-color); }"
    ".stat { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--divider-color); }"
    ".stat:last-child { border-bottom:none; }"
    "progress { width:100%; height:6px; -webkit-appearance:none; appearance:none; border:none;"
    "  background:var(--divider-color); border-radius:3px; overflow:hidden; display:block; margin:12px 0; }"
    "progress::-webkit-progress-bar { background:var(--divider-color); }"
    "progress::-webkit-progress-value { background:var(--primary-color); }"
    "progress::-moz-progress-bar { background:var(--primary-color); }"
    "button { background:var(--primary-color); color:#fff; border:none; border-radius:8px;"
    "  padding:9px 18px; font-size:14px; font-weight:500; cursor:pointer; font-family:inherit; }"
    "button:hover { filter:brightness(1.08); }"
    "button.secondary { background:transparent; color:var(--primary-color);"
    "  border:1px solid var(--primary-color); }"
    ".error-box { border-left:3px solid var(--error-color); padding:10px 12px;"
    "  background:rgba(219,68,55,.08); border-radius:0 8px 8px 0; margin-top:12px; }"
    ".verify-img { width:100%; max-width:960px; border-radius:var(--ha-card-border-radius,12px);"
    "  border:1px solid var(--divider-color); display:block; cursor:crosshair; background:#000; }"
    ".toolbar { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; align-items:center; }"
    "input[type=email], input[type=password] { width:100%; padding:9px 12px; margin:4px 0 10px;"
    "  border:1px solid var(--divider-color); border-radius:8px; font-size:14px;"
    "  background:var(--card-background-color); color:var(--primary-text-color); font-family:inherit; }"
    "label { color:var(--secondary-text-color); font-size:12px; }"
    "a { color:var(--primary-color); text-decoration:none; }"
    "a:hover { text-decoration:underline; }"
    "footer { margin-top:16px; color:var(--secondary-text-color); font-size:12px; }"
    "</style>"
)


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        + _BASE_CSS
        + _HA_THEME_JS
        + "</head><body>"
        + body
        + "</body></html>"
    )


def _phase_badge(phase: str) -> str:
    text = html.escape(str(phase))
    lower = str(phase).lower()
    if lower in {"idle", "done", "complete"}:
        return f'<span class="badge ok">{text}</span>'
    if lower in {"error", "failed"}:
        return f'<span class="badge err">{text}</span>'
    if lower in {"error_present", "degraded"}:
        return f'<span class="badge warn">{text}</span>'
    return f'<span class="badge">{text}</span>'


def _format_ts(value: Any) -> str:
    """Compact display of an ISO timestamp in Brisbane local time."""

    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value)).astimezone(
            ZoneInfo("Australia/Brisbane")
        )
    except ValueError:
        return html.escape(str(value))
    now = datetime.now(ZoneInfo("Australia/Brisbane"))
    if moment.date() == now.date():
        return moment.strftime("Today %H:%M")
    if moment.year != now.year:
        return moment.strftime("%b %Y")
    return moment.strftime("%d %b, %H:%M")


def _rate_row(tariff: str, rate: dict[str, Any]) -> str:
    per = rate["per_kwh_aud"]
    sup = rate["daily_supply_aud"]
    return (
        "<tr><th scope=\"row\">{t}</th>"
        "<td class=\"num\">{per}</td><td class=\"num\">{sup}</td>"
        "<td class=\"muted\">{ue}</td><td class=\"muted\">{se}</td></tr>".format(
            t=html.escape(str(tariff)),
            per="—" if per is None else f"${html.escape(str(per))}",
            sup="—" if sup is None else f"${html.escape(str(sup))}",
            ue=_format_ts(rate["usage_effective_at"]),
            se=_format_ts(rate["supply_effective_at"]),
        )
    )


_RATES_TABLE_HEAD = (
    "<thead><tr><th>Tariff</th><th>Usage</th><th>Supply</th>"
    "<th>Usage effective</th><th>Supply effective</th></tr></thead>"
)


def _render_rates(payload: dict[str, Any]) -> str:
    rows = [_rate_row(tariff, rate) for tariff, rate in payload["rates"].items()]
    prior = [
        _rate_row(tariff, period)
        for tariff, periods in payload["rate_periods"].items()
        for period in periods
    ]
    current = (
        "<table>"
        + f"<caption>Current rates per tariff</caption>{_RATES_TABLE_HEAD}<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class=\"muted\">No rates observed yet.</p>"
    )
    history = (
        "<h2>Prior rate periods</h2><table>"
        + f"<caption>Historical rate periods</caption>{_RATES_TABLE_HEAD}<tbody>"
        + "".join(prior)
        + "</tbody></table>"
        if prior
        else ""
    )
    return (
        '<section class="card"><div class="card-head"><h2>Current rates</h2></div>'
        + current
        + "</section>"
        + (f'<section class="card"><div class="card-head">{history.split("</h2>", 1)[1]}</div></section>' if history else "")
    )


def _render_costs(payload: dict[str, Any]) -> str:
    rows = []
    for tariff, cost in payload["costs"].items():
        usage = cost["usage_aud"]
        supply = cost["supply_aud"]
        rows.append(
            "<tr><th scope=\"row\">{t}</th>"
            "<td class=\"num\">{u}</td><td class=\"num\">{s}</td></tr>".format(
                t=html.escape(str(tariff)),
                u="—" if usage is None else f"${html.escape(str(usage))}",
                s="—" if supply is None else f"${html.escape(str(supply))}",
            )
        )
    body = (
        "<table><caption>Accumulated usage and supply components</caption>"
        "<thead><tr><th>Tariff</th><th>Usage</th><th>Supply</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class=\"muted\">No costs accumulated yet.</p>"
    )
    return (
        '<section class="card"><div class="card-head"><h2>Accumulated costs</h2></div>'
        + body
        + "</section>"
    )


def render_index(payload: dict[str, Any]) -> str:
    """Render the HA-styled status dashboard (relative URLs only)."""

    error = payload.get("error")
    last_run = payload.get("last_run")
    completed = payload["backfill"]["completed_days"]
    total = payload["backfill"]["total_days"]
    if last_run:
        run_bits = [
            f"{html.escape(str(last_run['readings_new']))} new",
            f"{html.escape(str(last_run['readings_corrected']))} corrected",
            f"{html.escape(str(last_run['backfill_days_processed']))} backfill days",
        ]
        if last_run["errors"]:
            run_bits.append(
                f'<span style="color:var(--error-color)">{len(last_run["errors"])} errors</span>'
            )
        last_run_html = (
            "<div class=\"stat\"><span class=\"muted\">Last run</span><span>"
            + html.escape(str(last_run["reason"]))
            + " — "
            + ", ".join(run_bits)
            + "</span></div>"
        )
    else:
        last_run_html = (
            '<div class="stat"><span class="muted">Last run</span>'
            '<span class="muted">No run recorded yet</span></div>'
        )
    error_html = (
        f'<div class="error-box">{html.escape(str(error))}</div>'
        if error
        else ""
    )
    body = (
        '<h1><span class="logo">E</span>Ergon Usage</h1>'
        + f'<script>document.documentElement.dataset.status = '
        f"{html.escape(json.dumps({'phase': payload['phase']}))};</script>"
        '<div class="card">'
        '<div class="card-head"><h2>Sync</h2>'
        + _phase_badge(payload["phase"])
        + "</div>"
        "<progress max=\"100\" value=\"0\">Synchronizing</progress>"
        + f'<div class="stat"><span class="muted">Backfill progress</span>'
        f"<span class=\"num\">{html.escape(str(completed))} / {html.escape(str(total))} days</span></div>"
        + last_run_html
        + error_html
        + '<div class="toolbar" style="margin-bottom:0">'
        '<button type="button" id="run-now">Run now</button>'
        '<span id="run-note" class="muted" role="status"></span>'
        '<a href="./verify"><button type="button" class="secondary">'
        "WAF verification</button></a>"
        '<details style="margin-left:auto">'
        '<summary class="muted" style="cursor:pointer;font-size:12px">Maintenance</summary>'
        '<div class="toolbar" style="margin-top:8px">'
        '<input type="number" id="reset-days" min="1" max="730" value="14" '
        'style="width:80px;padding:8px;border:1px solid var(--divider-color);'
        'border-radius:8px;background:var(--card-background-color);'
        'color:var(--primary-text-color)">'
        '<button type="button" id="reset-backfill" class="secondary">Reset backfill</button>'
        "</div>"
        '<p class="muted" style="font-size:12px;margin:6px 0 0">Re-fetches the '
        "last N days of usage from the portal. Existing readings, rates, and "
        "statistics are kept.</p>"
        '<div class="toolbar" style="margin-top:4px">'
        '<button type="button" id="republish">Republish to HA</button>'
        "</div>"
        '<p class="muted" style="font-size:12px;margin:6px 0 0">No portal visit: '
        "recalculates every statistic from data already stored here and "
        "re-imports the full history into Home Assistant.</p>"
        "</details>"
        "</div></div>"
        + '<div class="grid" style="margin-top:12px">'
        + _render_rates(payload)
        + _render_costs(payload)
        + "</div>"
        + '<footer>Statistics appear in Home Assistant as <code>ergon:*</code> '
        "long-term statistics.</footer>"
        + "<script>"
        "var runBtn = document.getElementById('run-now');"
        "var runNote = document.getElementById('run-note');"
        "var pollTimer = null;"
        "function setRunning(running) {"
        "  runBtn.disabled = running;"
        "  runBtn.textContent = running ? 'Running…' : 'Run now';"
        "  if (running) { runNote.textContent = 'Synchronization in progress'; }"
        "}"
        "async function pollPhase() {"
        "  try {"
        "    const r = await fetch('./api/status', {cache: 'no-store'});"
        "    const s = await r.json();"
        "    const phase = String(s.phase || '').toLowerCase();"
        "    if (phase === 'running' || phase === 'syncing') {"
        "      setRunning(true);"
        "      if (!pollTimer) pollTimer = setTimeout(pollPhase, 2000);"
        "      return;"
        "    }"
        "    setRunning(false);"
        "    runNote.textContent = '';"
        "    window.location.reload();"
        "  } catch (e) {"
        "    if (!pollTimer) pollTimer = setTimeout(pollPhase, 2000);"
        "  }"
        "}"
        "runBtn.addEventListener('click', async () => {"
        "  if (runBtn.disabled) return;"
        "  setRunning(true);"
        "  try { await fetch('./api/run', {method: 'POST'}); } catch (e) {}"
        "  pollPhase();"
        "});"
        "(function initialPhase() {"
        "  try {"
        "    const s = JSON.parse(document.documentElement.dataset.status || '{}');"
        "    const phase = String(s.phase || '').toLowerCase();"
        "    if (phase === 'running' || phase === 'syncing') {"
        "      setRunning(true); pollPhase();"
        "    }"
        "  } catch (e) {}"
        "})();"
        "document.getElementById('reset-backfill').addEventListener('click', async () => {"
        "  const btn = document.getElementById('reset-backfill');"
        "  const days = parseInt(document.getElementById('reset-days').value, 10);"
        "  if (!days || days < 1 || days > 730) return;"
        "  if (!confirm('Re-fetch the last ' + days + ' days of usage?')) return;"
        "  btn.disabled = true; btn.textContent = 'Resetting…';"
        "  try {"
        "    const r = await fetch('./api/reset-backfill', {method: 'POST',"
        "      headers: {'Content-Type': 'application/json'},"
        "      body: JSON.stringify({days: days})});"
        "    const s = await r.json();"
        "    if (typeof s.cleared === 'number') {"
        "      runNote.textContent = 'Cleared ' + s.cleared + ' day(s); triggering re-fetch';"
        "      await fetch('./api/run', {method: 'POST'});"
        "      setRunning(true); pollPhase();"
        "    }"
        "  } catch (e) {}"
        "  btn.disabled = false; btn.textContent = 'Reset backfill';"
        "});"
        "document.getElementById('republish').addEventListener('click', async () => {"
        "  const btn = document.getElementById('republish');"
        "  btn.disabled = true; btn.textContent = 'Publishing…';"
        "  try { await fetch('./api/republish', {method: 'POST'}); } catch (e) {}"
        "  setRunning(true); pollPhase();"
        "  btn.disabled = false; btn.textContent = 'Republish to HA';"
        "});"
        "</script>"
    )
    return _page("Ergon Usage", body)


def render_verify_page() -> str:
    """Interactive WAF-verification viewer (relative URLs only)."""

    body = (
        '<h1><span class="logo">E</span>WAF verification</h1>'
        '<div class="card">'
        '<div class="card-head"><h2>Portal session</h2>'
        '<span id="state" class="badge">Loading…</span></div>'
        '<img id="shot" class="verify-img" alt="Portal screenshot"'
        ' width="640" height="400" src="./api/verify/screenshot">'
        '<div class="toolbar">'
        '<button type="button" id="start">Start session</button>'
        '<button type="button" id="begin" class="secondary">Begin challenge</button>'
        '<button type="button" id="reload" class="secondary">Reload page</button>'
        '<form method="post" action="./api/verify/stop" style="display:inline;margin:0">'
        '<button type="submit" class="secondary">Stop</button></form>'
        "</div></div>"
        '<div id="loginform" class="card" style="display:none;margin-top:12px">'
        "<h2>Sign in</h2>"
        '<p class="muted">Enter your Ergon portal credentials to submit the '
        "sign-in form in the streamed browser. They are sent only to the "
        "Ergon portal.</p>"
        '<label for="login-email">Email</label>'
        '<input type="email" id="login-email" autocomplete="username">'
        '<label for="login-password">Password</label>'
        '<input type="password" id="login-password" autocomplete="current-password">'
        '<button type="button" id="login-submit">Submit sign-in</button>'
        "</div>"
        "<script>"
        "const VIEW_W = 1280, VIEW_H = 800;"
        "async function refreshState() {"
        "  try {"
        "    const r = await fetch('./api/verify/state', {cache: 'no-store'});"
        "    const s = await r.json();"
        "    document.getElementById('state').textContent ="
        "    document.getElementById('state').textContent ="
        "      s.status + (s.error ? ' — ' + s.error : '');"
        "    document.getElementById('state').className ="
        "      'badge' + (s.status === 'done' ? ' ok' : (s.status === 'challenge' || s.status === 'signin' ? ' warn' : ''));"
        "    document.getElementById('loginform').style.display ="
        "      (s.status === 'signin') ? 'block' : 'none';"
        "    if (s.status === 'done') {"
        "      document.getElementById('shot').src = '';"
        "      return;"
        "    }"
        "  } catch (e) {}"
        "  setTimeout(refreshState, 2000);"
        "}"
        "async function refreshShot() {"
        "  try {"
        "    const r = await fetch('./api/verify/state', {cache: 'no-store'});"
        "    const s = await r.json();"
        "    if (s.status === 'challenge' || s.status === 'signin') {"
        "      document.getElementById('shot').src ="
        "        './api/verify/screenshot?t=' + Date.now();"
        "    }"
        "  } catch (e) {}"
        "  setTimeout(refreshShot, 2000);"
        "}"
        "document.getElementById('shot').addEventListener('click', async (ev) => {"
        "  const img = ev.target;"
        "  const rect = img.getBoundingClientRect();"
        "  const x = Math.round((ev.clientX - rect.left) / rect.width * VIEW_W);"
        "  const y = Math.round((ev.clientY - rect.top) / rect.height * VIEW_H);"
        "  try {"
        "    await fetch('./api/verify/click', {method: 'POST',"
        "      headers: {'Content-Type': 'application/json'},"
        "      body: JSON.stringify({x: x, y: y})});"
        "  } catch (e) {}"
        "});"
        "document.getElementById('begin').addEventListener('click', async () => {"
        "  try { await fetch('./api/verify/begin', {method: 'POST'}); } catch (e) {}"
        "  refreshShot();"
        "});"
        "document.getElementById('reload').addEventListener('click', async () => {"
        "  try { await fetch('./api/verify/reload', {method: 'POST'}); } catch (e) {}"
        "  refreshShot();"
        "});"
        "document.getElementById('start').addEventListener('click', async () => {"
        "  try { await fetch('./api/verify/start', {method: 'POST'}); } catch (e) {}"
        "  refreshState(); refreshShot();"
        "});"
        "document.getElementById('login-submit').addEventListener('click', async () => {"
        "  const email = document.getElementById('login-email').value;"
        "  const password = document.getElementById('login-password').value;"
        "  try {"
        "    await fetch('./api/verify/login', {method: 'POST',"
        "      headers: {'Content-Type': 'application/json'},"
        "      body: JSON.stringify({email: email, password: password})});"
        "    document.getElementById('login-password').value = '';"
        "  } catch (e) {}"
        "  refreshState(); refreshShot();"
        "});"
        "refreshState(); refreshShot();"
        "</script>"
    )
    return _page("WAF verification", body)


def create_app(coordinator: Any, verification: Any = None) -> web.Application:
    """Build the aiohttp application for ingress access.

    ``verification`` is a VerificationManager (or compatible); when None the
    /verify viewer and its endpoints return 404.
    """

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

    async def reset_backfill(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            days = int(payload["days"])
        except Exception:  # noqa: BLE001 - malformed bodies rejected uniformly
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        if not 1 <= days <= 730:
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        cleared = coordinator.reset_backfill(days)
        # Kick off a re-fetch run immediately so the user does not need a
        # second click; the run re-backfills the cleared days.
        coordinator.run_now("manual")
        return _no_store(web.json_response({"cleared": cleared}))

    async def republish(request: web.Request) -> web.Response:
        accepted = coordinator.republish()
        return _no_store(
            web.json_response({"accepted": accepted}, status=202)
        )

    async def index(request: web.Request) -> web.Response:
        payload = build_status_payload(coordinator.snapshot())
        return _no_store(
            web.Response(text=render_index(payload), content_type="text/html")
        )

    async def health(request: web.Request) -> web.Response:
        return _no_store(web.json_response({"ok": True}))

    async def verify_page(request: web.Request) -> web.Response:
        return _no_store(
            web.Response(text=render_verify_page(), content_type="text/html")
        )

    async def verify_state(request: web.Request) -> web.Response:
        state = await verification.status()
        return _no_store(web.json_response(state))

    async def verify_screenshot(request: web.Request) -> web.StreamResponse:
        png = await verification.screenshot()
        if png is None:
            return _no_store(web.Response(status=404))
        return _no_store(
            web.Response(body=png, content_type="image/png")
        )

    async def verify_click(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            x = payload["x"]
            y = payload["y"]
        except Exception:  # noqa: BLE001 - malformed bodies rejected uniformly
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        # Coordinates must be plain ints (not bool) within the viewer viewport.
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int)
            or not isinstance(y, int)
            or not (0 <= x < 1280 and 0 <= y < 800)
        ):
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        result = await verification.click(x, y)
        return _no_store(web.json_response(result))

    async def verify_reload(request: web.Request) -> web.Response:
        return _no_store(web.json_response(await verification.reload()))

    async def verify_begin(request: web.Request) -> web.Response:
        return _no_store(web.json_response(await verification.click_begin()))

    async def verify_login(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            email = payload["email"]
            password = payload["password"]
        except Exception:  # noqa: BLE001 - malformed bodies rejected uniformly
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
            return _no_store(web.json_response({"error": "invalid"}, status=400))
        result = await verification.fill_login(email, password)
        # The password is never echoed back in any response or log.
        return _no_store(web.json_response(result))

    async def verify_start(request: web.Request) -> web.Response:
        state = await verification.start()
        return _no_store(web.json_response(state))

    async def verify_stop(request: web.Request) -> web.Response:
        result = await verification.stop()
        return _no_store(web.json_response(result))

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/run", run_now)
    app.router.add_post("/api/reset-backfill", reset_backfill)
    app.router.add_post("/api/republish", republish)
    app.router.add_get("/health", health)
    if verification is not None:
        app.router.add_get("/verify", verify_page)
        app.router.add_get("/api/verify/state", verify_state)
        app.router.add_get("/api/verify/screenshot", verify_screenshot)
        app.router.add_post("/api/verify/click", verify_click)
        app.router.add_post("/api/verify/reload", verify_reload)
        app.router.add_post("/api/verify/begin", verify_begin)
        app.router.add_post("/api/verify/login", verify_login)
        app.router.add_post("/api/verify/start", verify_start)
        app.router.add_post("/api/verify/stop", verify_stop)
    return app
