# Changelog

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
