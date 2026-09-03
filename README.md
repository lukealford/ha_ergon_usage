# Ergon Usage — Home Assistant Add-on

Securely collect and view Ergon Electricity electricity usage in Home Assistant.

The add-on logs in to the Ergon Energy customer portal with Playwright, discovers
your account and tariffs, fetches current rates, the rolling three-day usage
view, and
historical usage, then imports the results into Home Assistant as long-term
statistics. A built-in web panel (via ingress) shows account, tariff, rate, and
sync status — never your credentials.

## What it does

- Discovers the single `A-...` account on the Ergon portal and its tariffs.
- Tracks each tariff's per-kWh rates (with effective-date boundaries) and daily
  supply price.
- Imports hourly energy usage per tariff as Home Assistant statistics named
  `ergon:<tariff>` and per-tariff cost statistics named `ergon:<tariff>_cost`.
- Performs a one-time historical backfill (up to 365 days) in bounded batches,
  resuming across restarts.
- Re-syncs every `poll_interval_hours` to pick up fresh rolling usage.
- Exposes `/health` and a sanitized `/api/status` on the ingress port.

## Installing (add-on store — NOT HACS)

This is a Home Assistant **add-on**. HACS does not support add-ons and is not
used here.

1. In Home Assistant, open **Settings → Add-ons → Add-on Store**.
2. Open the top-right menu and choose **Repositories** and add
   `https://github.com/lukealford/ergon_usage`.
3. Refresh the store, find **Ergon Usage**, and click **Install**.

The add-on lives at the root of the repository (single add-on layout), so the
store discovers it directly from the repository URL; no subfolder selection is
needed.

(If you have the repository checked out on the Home Assistant machine, you can
add the local path as a repository instead.)

## Configuration options

| Option | Type | Range | Default | Description |
| --- | --- | --- | --- | --- |
| `ergon_email` | email | required | — | Ergon portal login email (stored only in add-on config). |
| `ergon_password` | password | required | — | Ergon portal login password (stored only in add-on config). |
| `poll_interval_hours` | int | 6–48 | 12 | Hours between rolling re-syncs. |
| `initial_history_days` | int | 1–730 | 365 | Days of history fetched on first run. |
| `backfill_batch_days` | int | 1–60 | 30 | Days per backfill batch per run. |
| `request_delay_seconds` | int | 0–60 | 3 | Delay between portal requests. |
| `retry_limit` | int | 0–10 | 5 | Retries per failed portal request. |
| `tariff_name_overrides` | dict | — | `{}` | Map Ergon tariff names to friendly statistic names. |

## Safe first-run procedure (recommended)

Before trusting the parser with a full year of history, validate it with a
minimal first run:

1. Configure credentials and set `initial_history_days: 1` and
   `backfill_batch_days: 1`. Start the add-on.
2. Confirm in the status panel: one `A-...` account, your expected tariffs, the
   displayed per-kWh and supply prices, 24 hourly values per tariff (allowing
   gaps the portal itself reports), and correct Brisbane timestamps.
3. Confirm no costs appear before the first observed rate boundary, and that
   Home Assistant received the statistics
   (`Developer tools → Statistics`, or query `recorder/import_statistics` results).
4. In **Energy Dashboard**, select each `ergon:*` energy statistic as a grid
   source and the matching `ergon:*_cost` statistic where costs are tracked.
5. Once validated, change `initial_history_days: 365` and
   `backfill_batch_days: 30` and restart the add-on once.

This prevents an unverified parser from making hundreds of requests.

## Cost boundaries

Cost statistics begin only at the first observed rate boundary for each tariff —
  rates are only known from the moment the portal exposes them. Backfilled
  readings after that boundary **are costed**; usage earlier than the first
  observed rate remains energy-only.
## Status UI

The add-on's ingress panel (Sidebar → Ergon Usage) shows the discovered
account, tariff names with current per-kWh and supply prices, the most recent
sync timestamp, and sync progress. The same data is served sanitized at
`/api/status`; credentials and tokens are never included and are redacted from
all log output.

## Troubleshooting

Failures surface as sanitized error categories:

- `authentication_error` — credentials rejected by the Ergon portal.
- `account_discovery_error` — could not identify the Ergon account.
- `extraction_error` (retryable) — usage/rate data could not be extracted.
- `import_error` — Home Assistant statistics import failed.

Check the add-on log (**Log** tab) for the category and safe message. Data is
stored in `/data/ergon_usage.sqlite3`; removing the add-on's data resets state.

## Architecture

- `app/main.py` — entry point: settings, logging redaction, web server, signals.
- `app/coordinator.py` — sync/backfill orchestration and lifecycle.
- `app/ergon.py` — Playwright portal client (login, discovery, extraction).
- `app/extractor.py` / `app/normalize.py` — HTML/JSON extraction and normalization.
- `app/tariff_rates.py` / `app/costs.py` — rate boundaries and cost computation.
- `app/ledger.py` — SQLite persistence and idempotency.
- `app/home_assistant.py` — Supervisor WebSocket statistics import.
- `app/web.py` — `/health` and sanitized `/api/status`.

## Development and testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app
```

The container image is defined by `Dockerfile`/`build.yaml` (multi-arch:
amd64, aarch64) and starts via `run.sh`.

## License

Distributed under the repository's license terms. Ergon credentials never leave
your Home Assistant instance.
