# HA Tools Email Integration Report

## Status

`status`: done

`report_path`: `/Users/maciej/repos/ha-tools-email-integration/codex-runs/email-integration-report.md`

`scope`: local repo changes only. No commits, pushes, releases, or GitHub mutations.

## File Tree

Changed or added files:

- `.github/workflows/hacs.yml`
- `.github/workflows/hassfest.yml`
- `CHANGELOG.md`
- `README.md`
- `hacs.json`
- `custom_components/ha_tools_email/__init__.py`
- `custom_components/ha_tools_email/composer.py`
- `custom_components/ha_tools_email/config_flow.py`
- `custom_components/ha_tools_email/const.py`
- `custom_components/ha_tools_email/manifest.json`
- `custom_components/ha_tools_email/scheduler.py`
- `custom_components/ha_tools_email/services.yaml`
- `custom_components/ha_tools_email/storage.py`
- `custom_components/ha_tools_email/translations/en.json`
- `custom_components/ha_tools_email/translations/pl.json`
- `custom_components/ha_tools_email/websocket_api.py`
- `tests/__main__.py`
- `tests/test_composer.py`
- `tests/test_scheduler.py`
- `codex-runs/email-integration-report.md`

## Design

### Setup And Compatibility

- `__init__.py` still owns the original SMTP config file, secret resolution, `_send_email`, and all five existing services: `send`, `test`, `save_config`, `get_config`, `list_secrets`.
- New setup paths converge through `_async_ensure_setup()` so both legacy YAML setup and config-entry setup register the same services, Store, websocket API, and scheduler.
- Service registration is guarded with `DATA_SERVICES_REGISTERED` to avoid duplicate registration.

### Store

- `storage.py` wraps Home Assistant `Store`.
- Persisted shape contains:
  - `schedules`: server-side schedule list.
  - `digest_state`: last log digest timestamp and dedup keys.
  - `scheduler_state`: last fired period per schedule.
- Schedule shape:

```json
{
  "kind": "log_digest|energy_report",
  "cadence": "daily|weekly|monthly",
  "time": "HH:MM",
  "recipients": ["user@example.com"],
  "enabled": true
}
```

### Scheduler

- `scheduler.py` re-arms `async_track_time_change` callbacks on startup and after websocket schedule mutations.
- Daily fires every day at `time`.
- Weekly fires Monday at `time`.
- Monthly fires on the first day of the month at `time`.
- On fire, the scheduler composes the report server-side and sends it through the existing `_send_email` path passed from `__init__.py`.
- Last-fired period keys prevent duplicate sends if the scheduler reloads around the same fire window.

### Composer

- `composer.py` contains pure render helpers plus HA-runtime collectors.
- Log digest:
  - Reads `hass.data["system_log"].records.to_list()`, matching Home Assistant's `system_log/list` backing data.
  - Filters from the last stored digest timestamp.
  - Aggregates counts by level and logger.
  - Renders compact HTML with counts, top loggers, top recent errors, and top recent warnings.
- Energy report:
  - Discovers `sensor.*` entities with `device_class: energy` or kWh/Wh energy units.
  - Uses recorder `statistics_during_period` with `change` values.
  - Renders total kWh and top consumers.

### Websocket API

- `ha_tools_email/get_config`: returns non-secret SMTP state, `smtp_configured`, and schedules. Password is never returned.
- `ha_tools_email/list_schedules`: returns schedules.
- `ha_tools_email/set_schedule`: create/update/delete schedules. Admin required.
- `ha_tools_email/send_now`: compose and send `kind` + `cadence` immediately. Admin required.

## Manifest / HACS

- `manifest.json` now sets:
  - `version`: `2.0.0`
  - `config_flow`: `true`
  - `integration_type`: `service`
  - `iot_class`: `calculated`
  - dependencies: `recorder`, `system_log`, `websocket_api`
- `hacs.json` remains integration-compatible and renders the README.
- Added HACS and hassfest workflows.
- Added English and Polish config-flow translations.

## IoT Class Choice

Chosen: `calculated`.

Reason: this integration does not model an IoT device state and does not receive state pushes from an SMTP/cloud service. It calculates reports from HA-owned data (`system_log`, recorder statistics) and performs an outbound SMTP action. `cloud_push` would imply an external cloud service pushes state to Home Assistant, and `cloud_polling` would imply Home Assistant polls a cloud service for state. Both are misleading here.

Source checked: Home Assistant Developer Docs list `calculated` as an accepted manifest IoT class for integrations that provide calculated results: https://developers.home-assistant.io/docs/creating_integration_manifest/#iot-class

## Verification

Requested commands:

```text
$ python3 -m py_compile custom_components/ha_tools_email/*.py
exit 0, no output
```

```text
$ python tests
test_energy_report_renders_totals_top_consumers_and_escapes_names ... ok
test_log_digest_escapes_entries_and_counts_by_level_logger ... ok
test_daily_fire_is_today_when_time_is_future ... ok
test_daily_fire_moves_to_tomorrow_after_time_passed ... ok
test_monthly_fire_uses_first_day ... ok
test_weekly_fire_uses_monday_default ... ok

Ran 6 tests in 0.000s
OK
```

```text
$ grep -nE 'DOMAIN, "(send|test|save_config|get_config|list_secrets)"' custom_components/ha_tools_email/__init__.py
292:        DOMAIN, "send", handle_send,
302:        DOMAIN, "test", handle_test,
307:        DOMAIN, "save_config", handle_save_config,
320:        DOMAIN, "get_config", handle_get_config,
326:        DOMAIN, "list_secrets", handle_list_secrets,
```

Additional checks:

- `python3 -m json.tool custom_components/ha_tools_email/manifest.json`: exit 0.
- `python3 -m json.tool hacs.json`: exit 0.
- `python3 -m json.tool custom_components/ha_tools_email/translations/en.json`: exit 0.
- `python3 -m json.tool custom_components/ha_tools_email/translations/pl.json`: exit 0.
- `git diff --check`: exit 0.
- Secret-pattern scan over touched files: no matches.

## Risks

- I did not run hassfest locally because this repo does not include Home Assistant/hassfest tooling; workflows were added for CI validation.
- I did not run a live Home Assistant instance, so websocket runtime and scheduler behavior still need HA runtime smoke testing.
- Energy reports depend on recorder statistics. If recorder is disabled or sensors lack compatible statistics, the report will render with no energy rows.
- Server-side energy reports currently use kWh totals only; no tariff configuration is exposed in the schedule schema.
- Existing cards were read-only per scope and still use the legacy service calls, not the new websocket schedule API.

## Follow-Up

- Push only after coordinator review, then read back HACS and hassfest action results.
- Optionally update `ha-log-email.js` and `ha-energy-email.js` later to manage schedules through the new websocket API.
- Optionally add schedule options for weekly weekday, monthly day, currency, and tariff price after the base API is validated.

## State

`git_state`: changed, uncommitted local edits on `main` at `0f89b09`; no commit created.

`github_state`: not_touched.

`telegram_state`: degraded, `/Users/maciej/ai-stack/scripts/tg_notify.sh` exited 6 with no stdout.

`instruction_sync`: not_applicable.

`durable_capture`: repo-local report updated. Obsidian/Notion were not mutated due the repo-only edit scope; coordinator should index this report in Notion if this run should appear in the user-facing Claude Artifacts layer.
