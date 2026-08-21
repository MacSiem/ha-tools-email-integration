# HA Tools Email

![Preview](banner.png)

Built-in SMTP integration for Home Assistant. It sends email for HA Tools
cards through simple service calls, and it can also compose and send
scheduled server-side log digests and energy reports — no `notify:` platform
or external mail relay required.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.7+-blue.svg?logo=homeassistant)](https://www.home-assistant.io/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE) [![Version](https://img.shields.io/github/v/release/MacSiem/ha-tools-email-integration)](https://github.com/MacSiem/ha-tools-email-integration/releases)

Part of the [HA Tools](https://github.com/MacSiem) ecosystem.

## How it works

**Short version: add the integration, save SMTP settings once, then send or
schedule reports.**

1. **Zero-input config flow.** Adding the integration creates a single config
   entry with no form fields — SMTP credentials are not entered during setup.
2. **SMTP settings via service call.** Call `ha_tools_email.save_config` (or
   use the SMTP settings panel inside HA Tools cards, which calls the same
   service) to persist server, port, username, password, sender and
   encryption with Home Assistant's Store helper. Existing
   `<config>/ha-tools/smtp-config.json` settings are migrated automatically
   and the plaintext legacy file is removed only after Store saves
   successfully. The password field accepts a literal value or an
   `!secret <key>` reference resolved from `secrets.yaml` at send time.
3. **Cards send through the existing services.** `ha-log-email` and
   `ha-energy-email` (in the main HA Tools repo) call
   `ha_tools_email.get_config`, `ha_tools_email.test` and
   `ha_tools_email.send` directly — no changes needed for existing cards.
4. **Server-side scheduling is optional.** Since v2.0.0 you can also store
   schedules (`log_digest` or `energy_report`, daily/weekly/monthly) through
   the `ha_tools_email/set_schedule` websocket command. The integration then
   composes the report from Home Assistant's own `system_log` records or
   recorder statistics and sends it through the same SMTP path, without any
   card open in a browser.

### What is automatic vs. manual

| Automatic | Manual |
|---|---|
| Config entry creation (no fields to fill in) | Saving SMTP server/port/credentials once |
| Report composition (log digest, energy report) once scheduled | Creating a schedule via the websocket API |
| Duplicate-fire prevention across HA restarts | Sending an on-demand report (`send_now` / `send` service) |
| `!secret` resolution at send time | Adding secrets to `secrets.yaml` |

## Entities

This integration does not create any entities. It is a `service`-type
integration (`manifest.json` → `"integration_type": "service"`) that exposes
services and a websocket API only.

## Services

| Service | Purpose |
|---|---|
| `ha_tools_email.send` | Send an email to one or many recipients with plain text and optional HTML. |
| `ha_tools_email.test` | Send a test message to the configured default recipient/sender to verify SMTP settings. |
| `ha_tools_email.save_config` | Admin-only: persist SMTP server, port, credentials, sender, encryption, and default recipient. |
| `ha_tools_email.get_config` | Admin-only: return non-secret SMTP state. Password data and secret-key names are never included. |
| `ha_tools_email.list_secrets` | Admin-only: return available `secrets.yaml` key names only — secret values are never returned. |

```yaml
service: ha_tools_email.save_config
data:
  server: smtp.example.com
  port: 587
  username: user@example.com
  password: "!secret smtp_app_password"
  sender: user@example.com
  encryption: starttls
  default_recipient: recipient@example.com
```

```yaml
service: ha_tools_email.send
data:
  to: "user@example.com"
  subject: "HA Tools report"
  body: "Plain-text body."
  html: "<h2>Optional HTML body</h2>"
```

## Automation example

```yaml
alias: Email me when the freezer sensor goes offline
trigger:
  - platform: state
    entity_id: sensor.freezer_temperature
    to: "unavailable"
    for: "00:15:00"
action:
  - service: ha_tools_email.send
    data:
      subject: "⚠️ Freezer sensor offline"
      body: >
        sensor.freezer_temperature has been unavailable for 15 minutes.
        Check the device and the SMTP delivery in HA Tools → Settings → Email/SMTP.
```

## Scheduled reports

Schedules are stored with Home Assistant's `Store` helper, not YAML, and are
managed through the websocket API below. Each schedule looks like:

```json
{
  "kind": "log_digest",
  "cadence": "daily",
  "time": "07:30",
  "recipients": ["user@example.com"],
  "enabled": true
}
```

- `kind`: `log_digest` or `energy_report`.
- `cadence`: `daily` (every day at `time`), `weekly` (Monday at `time`), or `monthly` (first day of the month at `time`).
- If `recipients` is empty, the scheduler falls back to `default_recipient` from the saved SMTP config.

Log digests read Home Assistant `system_log` records, aggregate counts by
level and logger, deduplicate against the previous digest, and include the
top 10 recent errors and warnings. Energy reports auto-discover `sensor.*`
entities with `device_class: energy` or kWh/Wh units and pull recorder
`statistics_during_period` changes for the period, then render total kWh and
a top-consumers table.

## Websocket API

Commands use the integration domain as the `type` prefix.

Read non-secret SMTP state and schedules (no admin required):

```json
{ "type": "ha_tools_email/get_config" }
```

List schedules (no admin required):

```json
{ "type": "ha_tools_email/list_schedules" }
```

Create or update a schedule (admin required):

```json
{
  "type": "ha_tools_email/set_schedule",
  "action": "upsert",
  "schedule": {
    "kind": "energy_report",
    "cadence": "weekly",
    "time": "08:00",
    "recipients": ["user@example.com"],
    "enabled": true
  }
}
```

Delete a schedule (admin required):

```json
{
  "type": "ha_tools_email/set_schedule",
  "action": "delete",
  "schedule_id": "schedule-id"
}
```

Compose and send a report immediately (admin required):

```json
{
  "type": "ha_tools_email/send_now",
  "kind": "log_digest",
  "cadence": "daily",
  "recipients": ["user@example.com"]
}
```

`get_config` and `list_schedules` are readable by any logged-in user and
return non-secret state; `set_schedule` and `send_now` require an admin
connection. The similarly named legacy services that save/read SMTP settings
or list secret-key names are admin-only.

## Installation

### HACS Custom Repository

1. Open HACS.
2. Open **Custom repositories**.
3. Add `https://github.com/MacSiem/ha-tools-email-integration` with category **Integration**.
4. Install **HA Tools Email**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration** and add **HA Tools Email**. The setup form has no fields — just confirm.
7. Save SMTP settings: call `ha_tools_email.save_config` from Developer Tools → Actions, or use the SMTP settings panel inside an HA Tools email card (`ha-log-email` / `ha-energy-email`).
8. Optionally call `ha_tools_email.test` to confirm delivery.

### Manual

1. Copy `custom_components/ha_tools_email/` to `<config>/custom_components/`.
2. Restart Home Assistant.
3. Add **HA Tools Email** from **Settings → Devices & services**.
4. Save SMTP settings as in step 7 above.

## FAQ

**Where does my email go?**
Only to the SMTP server you configure in `ha_tools_email.save_config`. The
integration does not relay through any third-party or Home Assistant Cloud
service.

**Is my SMTP password exposed to the frontend or other users?**
No, and the two read paths are deliberately different:

- The `ha_tools_email/get_config` **websocket** command returns a
  `_safe_smtp_config` payload with only `server`, `port`, `username`,
  `sender`, `encryption`, `default_recipient`, `uses_secret` and
  `smtp_configured` — there is no `password` key in the response at all.
- The admin-only `ha_tools_email.get_config` **service** returns the same
  non-secret SMTP state. It never returns a password, password fragment,
  `!secret` reference, or list of available secret-key names.

**Can any logged-in user read or change my SMTP settings?**
Any logged-in user can read non-secret config and existing schedules through
the websocket API (`get_config`, `list_schedules`). Creating, editing or
deleting a schedule (`set_schedule`) and triggering an on-demand send
(`send_now`) require an admin-level websocket connection. The legacy
`save_config`, `get_config`, and `list_secrets` services also require an
administrator.

**Do I need a `notify:` platform configured?**
No. The integration talks to your SMTP server directly with `smtplib`.

**What happens if `secrets.yaml` doesn't have the key I referenced?**
The send fails with an explicit error identifying the missing key — nothing
is sent silently.

**Which cards use this integration?**
`ha-log-email` and `ha-energy-email` from the main HA Tools repository call
the services described above. Existing automations using
`ha_tools_email.send` continue to work unchanged in v2.0.2.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Support

If this tool makes your Home Assistant life easier, consider supporting development:

- [Buy Me a Coffee](https://buymeacoffee.com/macsiem)
- [PayPal](https://www.paypal.com/donate/?hosted_button_id=Y967H4PLRBN8W)

## License

MIT - see [LICENSE](LICENSE).
