# 📧 HA Tools Email

Built-in SMTP integration for Home Assistant — required by [`ha-energy-email`](https://github.com/MacSiem/ha-energy-email) and [`ha-log-email`](https://github.com/MacSiem/ha-log-email) Lovelace cards.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1+-blue.svg?logo=homeassistant)](https://www.home-assistant.io/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE) [![Version](https://img.shields.io/badge/Version-1.0.0-success.svg)](#)

## What it does

Adds 5 services for sending email through HA's own SMTP connection (no `notify.smtp` configuration required):

| Service | Purpose |
|---|---|
| `ha_tools_email.send` | Send an email to one or many recipients (plain text + optional HTML) |
| `ha_tools_email.test` | Send a test message to verify SMTP settings |
| `ha_tools_email.save_config` | Persist SMTP server/port/credentials/encryption to disk (`<config>/ha-tools/smtp-config.json`) |
| `ha_tools_email.get_config` | Return current settings (password masked) — used by HA Tools cards to show "configured" state |
| `ha_tools_email.list_secrets` | Return list of available `secrets.yaml` keys (no values) — for password picker UI |

Configuration is stored in `<config>/ha-tools/smtp-config.json`. The password field can be a literal value or an `!secret` reference (e.g. `!secret gmail_app_password`) that resolves at send time from `secrets.yaml`.

## Installation

### Via HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Repository: `https://github.com/MacSiem/ha-tools-email-integration` — Category: **Integration**
3. Install **HA Tools Email**
4. Restart Home Assistant
5. Add to `configuration.yaml`:

   ```yaml
   ha_tools_email:
   ```

6. Restart again.

### Manual

1. Copy `custom_components/ha_tools_email/` to `<config>/custom_components/`.
2. Add `ha_tools_email:` to `configuration.yaml`.
3. Restart Home Assistant.

## First-run configuration

Open the **HA Tools Email** Lovelace card (or call `ha_tools_email.save_config` from Developer Tools → Services) and supply:

- `server` — e.g. `smtp.gmail.com`
- `port` — e.g. `587`
- `username` — your email login
- `password` — either the actual password (or app password), or `!secret <key>` to read from `secrets.yaml`
- `sender` — From address (often same as username)
- `encryption` — `starttls`, `ssl`, or `none`
- `default_recipient` — fallback recipient when not specified in `send`

The values land in `<config>/ha-tools/smtp-config.json` (chmod 600 recommended).

## Sending an email manually

```yaml
service: ha_tools_email.send
data:
  to: "user@example.com"
  subject: "HA Tools — Daily summary"
  body: "Plain-text body here."
  html: "<h2>Optional HTML body</h2>"
```

Or test the connection:

```yaml
service: ha_tools_email.test
```

## Privacy & security

- Credentials live only on your HA instance (`<config>/ha-tools/smtp-config.json`).
- Passwords are never echoed back through `get_config` — they are masked or replaced with the `!secret <key>` reference.
- No external network calls beyond your configured SMTP server.

## Supported by HA Tools cards

- [`ha-energy-email`](https://github.com/MacSiem/ha-energy-email) — daily/weekly/monthly energy reports
- [`ha-log-email`](https://github.com/MacSiem/ha-log-email) — error/warning digests
- Any other tool can call `ha_tools_email.send` directly

If `ha_tools_email` is **not** installed, those cards show an inline install prompt and remain rendered (donate footer + UI structure intact).

## Support

If this integration is useful to you, consider supporting development:

- ☕ [Buy Me a Coffee](https://buymeacoffee.com/macsiem)
- 💳 [PayPal](https://www.paypal.com/donate/?hosted_button_id=Y967H4PLRBN8W)

## License

MIT — see [LICENSE](LICENSE).
