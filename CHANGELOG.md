# Changelog

## 2.4.0 (Unreleased)

- Add shared light/dark mail surfaces and an explicit preview theme, readable severity badges and values beside chart bars. Keep the complete shared Log caveat once, preserving per-chart omissions and missing/zero explanations.
- Format generated mail dates and period bounds in Home Assistant’s configured time zone and PL/EN language, with DST offsets and separate technical UTC time. Preserve calendar boundaries, source identities and pricing. Use readable dates in chart labels and comparisons while retaining calendar-day identities and both offsets of repeated hours.

- Add one shared Energy snapshot and five stable entities: tariff classification, current usage price, cheap-zone binary state, next cheap start and cost today. Prices use tariff currency; missing data or variable fees stay unavailable and fixed monthly fees remain separate.
- Price Energy Optimizer 4.0 card histories through the same EnergyTariff engine, including ephemeral card overrides. Bound batch size, CPU work and pending jobs; do not read Recorder or change settings for supplied-history pricing.
- Refresh shared measurements by exact source/unit/day context and completed absolute hours. Reprice tariff boundaries without redundant Recorder work, stop listeners/timers on unload, and reject late results after reload, authorization or revision changes.
- Preserve optional explicit cheap/expensive/mixed classification on zones and dynamic intervals. Never infer classification from names, minimum prices or legacy tariff mode.
- Keep monetary daily cost without a statistics state class; current price remains a measurement in currency/kWh. Add native coverage across the actual minimum, stable and beta Home Assistant versions.

## 2.3.1 (2026-10-16)

- Keep valid Energy Recorder measurements when another source has a gap, invalid interval or unsupported metadata. Show recorded usage, per-source measured hours, overall coverage, the available hourly profile and a source ranking; incomplete full-period totals and comparisons remain unavailable.
- Replace empty completed-period Log totals with an explicit unavailable-history notice. When the current HA buffer contains newer diagnostics, show them in a separate, clearly labelled section outside the requested period. A restart is never presented as evidence of zero errors.
- Give Energy and Log emails a shared self-contained table layout, clear metric panels, semantic coverage colours and responsive source tables. Retain plain-text alternatives and inline charts without external assets or tracking.
- Use Recorder display names for external statistics without a state entity, and compact chart labels while preserving UTC offsets, DST distinctions and raw interval metadata. Keep report dates separate from actual generation timestamps.

## 2.3.0 (2026-10-15)

- Preview the actual subject, plain text and self-contained HTML without sending or changing schedules, history or digest state. The same composer builds the requested manual report.
- Choose daily, weekly or monthly rolling windows or previous completed calendar periods for manual Energy and Log reports. Explicit manual Log sends are independent of scheduled digest deduplication.
- Compare completed calendar Energy reports with the adjacent previous calendar period, preserving actual durations, DST, missing data and measured zero. Unsupported comparisons remain unavailable.
- Keep the source notes, charts and footer inside one readable email column; include UTF-8 and viewport metadata.
- Preserve full Unicode messages and emoji while replacing malformed unpaired surrogate characters at the mail boundary; a malformed log entry can no longer prevent MIME encoding.

## 2.2.0 (2026-10-15)

- Add shared measured-energy tariff profiles with revision checks, household reads and administrator writes. Energy, variable charges and monthly fixed fees stay separate; no tariff is assumed.
- Retain up to 128 dispatch-attempt metadata records for 90 days across manual reports, schedules, services and options tests. SMTP acceptance remains separate from delivery and history availability.
- Add explicit schedule timezone, weekday/month day and computed next fire; scheduled reports use previous completed local calendar periods with DST handling. Read-only calendar previews do not send or write.
- Add self-contained static charts and matching text data from the same report payload, preserving missing measurements and units.
- Require confirmed HA Store writes before publishing settings, schedules, attempt history or duplicate-fire state. Failed legacy SMTP migration keeps the original file; failed metadata after SMTP acceptance never becomes a failed-send claim.
- Report unconfirmed options saves safely in English/Polish and enable native storage tests in Linux CI, including minimum/current/beta HA Core.

## 2.1.2 (2026-09-30)

- Energy reports use unique configured Energy Dashboard grid-import sources and Recorder metadata units, with exact completed-hour windows for the last 24 hours, 7 days or 30 days.
- Withhold totals for missing, incomplete or unsupported statistics; preserve measured zero and reject nonfinite values. Costs remain unavailable without a tariff.
- Add administrator-only read-only `preview_energy_report`, using the same composer as manual sends and schedules without sending mail or updating schedule state.

## 2.1.1 (2026-09-30)

- Report successful scheduled sends as `accepted_by_smtp`; detect partial SMTP recipient refusal without logging addresses.
- Serialize schedule callbacks under a lock before checking and persisting the per-period send key; redact recipient and secret-key details from logs and errors.

## 2.1.0

- **Security:** the SMTP password is no longer accepted as action data. Home Assistant records action data in its database, so a password passed to `ha_tools_email.save_config` was also kept in the recorder database and in backups. The action now rejects `password` with a clear error and accepts `password_secret` (a `secrets.yaml` key name); omitting both keeps the current password.
- **New:** SMTP settings are edited in **Settings → Devices & services → HA Tools Email → Configure**, with validation and an optional test email before saving. The password is never shown again and is not stored in the config entry options.
- **New:** a repair notice asks users who saved a literal password through the action before 2.1.0 to rotate it; it disappears once a password is saved in Configure.
- **Security:** the `get_config` and `list_schedules` websocket commands now require an administrator, matching the documentation.
- **New:** config entry diagnostics with passwords, usernames, addresses and the server name redacted.
- Error messages point to the integration options instead of the retired HA Tools panel; the integration allows only one config entry.
- **Fix:** server-side schedules work again. Setup failed with a `TypeError` whenever a schedule was stored, because the scheduler passed an unsupported `local` argument to Home Assistant's time tracker; creating a schedule failed the same way.
- CI: the integration is now tested inside a real Home Assistant core (options flow, repair, admin gates, diagnostics, schedules).
## 2.0.3

- Verify SMTP server certificates and hostnames for both implicit TLS and STARTTLS using Home Assistant's client SSL context.
- Restrict the `send` and `test` services to administrators while preserving internal automation calls without a user context.
- Remove the accidentally committed agent run log and ignore future `codex-runs` output.

## 2.0.2

- Moved SMTP credentials from the legacy JSON file to Home Assistant's Store helper, with save-before-delete migration for existing installations.
- Restricted the legacy `save_config`, `get_config`, and `list_secrets` services to administrators.
- Removed password fragments, `!secret` references, and secret-key discovery from `get_config` responses.
- Raised the declared minimum Home Assistant version to 2024.7.0 and removed duplicate logo assets.

## 2.0.1

- Fixed `send.to` service documentation: the field is optional (matching the actual service schema) and falls back to the default recipient saved in the SMTP configuration when omitted.
- Translated the SMTP test email copy from Polish to English.
- Test email footer now reports the current integration version instead of a hardcoded "v1.0".

## 2.0.0

- Added a zero-input config flow for UI-based installation.
- Added Store-backed report schedules for log digests and energy reports.
- Added server-side log digest composition from Home Assistant `system_log`.
- Added server-side energy report composition from recorder statistics for discovered kWh energy sensors.
- Added websocket commands for non-secret config reads, schedule CRUD, schedule listing, and immediate report sending.
- Added HACS and hassfest GitHub workflows.
- Added English and Polish config-flow translations.
- Added pure-python tests for composer rendering and scheduler next-fire behavior.

## 1.0.0

- Initial SMTP service integration for HA Tools cards.
- Added `ha_tools_email.send`, `test`, `save_config`, `get_config`, and `list_secrets` services.
- Supported file-backed SMTP settings in `<config>/ha-tools/smtp-config.json`.
- Supported `!secret <key>` password references resolved from `secrets.yaml` at send time.
