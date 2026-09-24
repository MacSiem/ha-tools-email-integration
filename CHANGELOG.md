# Changelog

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
