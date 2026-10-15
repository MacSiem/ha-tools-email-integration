# HA Tools Email

![Preview](banner.png)

SMTP integration for Home Assistant. It sends email for HA Tools
cards through simple service calls, and it can also compose and send
scheduled server-side log digests and energy reports — no `notify:` platform
or external mail relay required.

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.7+-blue.svg?logo=homeassistant)](https://www.home-assistant.io/) [![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE) [![Version](https://img.shields.io/github/v/release/MacSiem/ha-tools-email-integration)](https://github.com/MacSiem/ha-tools-email-integration/releases)

Part of the [HA Tools](https://github.com/MacSiem) ecosystem.

## How it works

**Short version: add the integration, open Configure, enter your SMTP
settings once, then send or schedule reports.**

1. **Zero-input config flow.** Adding the integration creates a single config
   entry with no form fields.
2. **SMTP settings in the integration options.** Open **Settings → Devices &
   services → HA Tools Email → Configure** and enter server, port, encryption,
   username, password (or the name of a `secrets.yaml` key), sender and default
   recipients. You can send a test email before saving. Settings are kept with
   Home Assistant's Store helper; the password is never shown again and is never
   stored in the config entry options. Existing
   `<config>/ha-tools/smtp-config.json` settings are migrated automatically
   and the plaintext legacy file is removed only after Store saves
   successfully.
3. **Cards use the same SMTP integration.** Compatible Log and Energy cards
   can request server-composed reports through `send_now` and read the actual
   mail before sending. Existing `get_config`, `test` and `send` service calls
   remain supported for older cards and custom automations.
4. **Server-side scheduling is optional.** Since v2.0.0 you can also store
   schedules (`log_digest` or `energy_report`, daily/weekly/monthly) through
   the `ha_tools_email/set_schedule` websocket command. The integration then
   composes the report from Home Assistant's own `system_log` records or
   recorder statistics and sends it through the same SMTP path, without any
   card open in a browser.

### What is automatic vs. manual

| Automatic | Manual |
|---|---|
| Config entry creation (no fields to fill in) | Entering SMTP settings once in **Configure** |
| Report composition (log digest, energy report) once scheduled | Creating a schedule via the websocket API |
| Per-period duplicate-fire guard across normal callbacks and restarts | Sending an on-demand report (`send_now` / `send` service) |
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
| `ha_tools_email.save_config` | Admin-only: persist non-secret SMTP settings. Passwords are not accepted as action data; use **Configure** or `password_secret`. |
| `ha_tools_email.get_config` | Admin-only: return non-secret SMTP state. Password data and secret-key names are never included. |
| `ha_tools_email.list_secrets` | Admin-only: return available `secrets.yaml` key names only — secret values are never returned. |

```yaml
service: ha_tools_email.save_config
data:
  server: smtp.example.com
  port: 587
  username: user@example.com
  password_secret: smtp_app_password   # name of a secrets.yaml key; optional
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
        Check the device and the SMTP settings in Settings → Devices & services → HA Tools Email → Configure.
```

## Energy data and previews

Energy reports measure the unique grid-import `stat_energy_from` sources configured
in Home Assistant's Energy Dashboard. Each grid source uses its nonempty flat
`stat_energy_from` when present, otherwise imports from `flow_from` are used.
Exports, solar sources and arbitrary discovered device meters are excluded.
Daily, weekly and monthly reports cover the last 24, 168 and 720 completed UTC hours;
the exact start and end are included in the report. Recorder metadata determines Wh/kWh/MWh
conversion to kWh (Wh × 0.001, kWh × 1, MWh × 1000). Missing hours or unsupported metadata withhold the period total; measured
zero stays zero. No tariff is assumed. With an explicitly saved shared tariff,
measured hourly energy is priced against its matching zones and rules. The
report states energy cost, variable per-kWh charges and monthly fixed fees
separately; the fixed fee is not silently prorated or added to an energy total.
Missing measurements, conflicting rules or missing rates remain unavailable.

Server schedules use the previous completed local day, ISO week or calendar
month in the schedule's timezone. On-demand `send_now` and the default preview
keep the existing rolling completed-hour periods described above. To preview a
completed calendar period without sending, request
`ha_tools_email/preview_energy_report` with `calendar_period: true` and an
optional IANA `timezone`. Returned bounds and coverage state the actual range.

HTML email contains self-contained static charts built from the same summary
and measured records as its tables; plain text includes the corresponding data.
No remote chart script or image is fetched. Missing values are labelled rather
than drawn as measured zero. Long daily series state their display limit; the
full report model is not replaced by the displayed subset.

Administrators can read `ha_tools_email/preview_energy_report` with `cadence` set to
`daily`, `weekly` or `monthly`. It returns the same summary model used by `send_now`
and schedules, without sending email or changing schedules. Energy Email cards need
a version supporting this endpoint to show the server preview.

## Actual mail preview and manual periods (2.3+)

Choose **daily**, **weekly** or **monthly**, then choose the period:

| Period mode | Log digest | Energy report |
|---|---|---|
| `rolling` | Retained groups last observed in the last 24 hours, 7 days or 30 days | Last 24, 168 or 720 completed Recorder hours |
| `calendar` | Previous completed local day, ISO week or calendar month | Previous completed local day, ISO week or calendar month |

Calendar periods use the requested IANA timezone, otherwise Home Assistant's
configured timezone. Their lengths can differ across DST transitions and months.
The exact start is inclusive and the end exclusive. Log totals describe retained
aggregates, not exact event counts or complete historical coverage; a retained
group may include earlier occurrences. Missing timestamps are excluded from an
explicit manual window. Missing Energy measurements remain unavailable.

Use the administrator-only preview command for either kind:

```json
{
  "type": "ha_tools_email/preview_log_digest",
  "cadence": "monthly",
  "format": "mail",
  "period_mode": "calendar",
  "timezone": "Europe/Warsaw",
  "max_entries": 50
}
```

For Energy, use `ha_tools_email/preview_energy_report` and omit `max_entries`.
The response includes `subject`, `body`, `html`, `cadence`, `period`, `source`,
`generated_at` and the effective calendar timezone. It does not contact SMTP or
write settings, schedules, history or digest state. The existing summary-format
preview and Energy `calendar_period: true` alias remain available.

To send the same selected kind and period, call `ha_tools_email/send_now` with
`kind` (`log_digest` or `energy_report`), the same `cadence`, `period_mode` and
optional `timezone`, plus optional `recipients`. For Log, `max_entries` is an
integer from 1 to 1000, default 10, limiting each recent severity list rather than
its aggregate counts. Both preview and send compose from current server data;
new data between the two requests can change the report. Explicit manual sends
do not update the scheduled Log digest's seen keys or last-digest time.

Calendar Energy reports include comparison against the adjacent previous
calendar period when both periods have complete supported measurements. Actual
hours and dates remain visible; totals are not normalized to equal duration.
A zero comparison baseline has no percentage change. Rolling reports do not
claim a calendar comparison. Grid-import statistics do not establish individual
appliance consumption or a solar-flow diagram.

`accepted_by_smtp` means that the configured SMTP server accepted the attempt;
it does not confirm delivery to the recipient's inbox. Unconfirmed results do
not trigger an automatic resend. HTML and plain text retain full messages,
Polish characters and emoji; malformed unpaired surrogate characters become the
Unicode replacement character without changing stored log identity.

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
- `cadence`: `daily`, `weekly` or `monthly` at the chosen `time`.
- `timezone`: optional IANA zone; if absent, use Home Assistant's configured zone.
- `weekday`: weekly day, Monday 0 through Sunday 6; default Monday.
- `month_day`: monthly day 1–31; short months clamp to their last day; default 1.
- `use_ha_timezone: true` on an explicit edit clears a previous zone override.
- A spring DST gap moves forward by the offset change; the repeated autumn time
  fires only on its first occurrence. The same resolver computes `next_fire`.
- A confirmed period marker is stored before SMTP. If that save fails, no mail
  is sent. An uncertain transport result is not retried automatically, including
  after reload or restart.
- If `recipients` is empty, the scheduler falls back to `default_recipient` from the saved SMTP config.

Log digests read Home Assistant `system_log` records, aggregate counts by
level and logger, deduplicate against the previous digest, and include the
top 10 recent errors and warnings. Energy reports use only configured Energy Dashboard grid imports and read
Recorder `statistics_during_period` changes with Wh/kWh/MWh metadata. Complete
hourly coverage across every source is required before rendering total kWh and
a grid-source breakdown; unrelated appliance counters are not added.

## Websocket API

Commands use the integration domain as the `type` prefix.

Read non-secret SMTP state and schedules (admin required):

```json
{ "type": "ha_tools_email/get_config" }
```

List schedules (admin required):

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

SMTP configuration, schedules, previews, sending and attempt history require
an administrator connection. The legacy services
that save/read SMTP settings or list secret-key names are admin-only too.

## Shared tariffs

`ha_tools_email/get_tariff` returns only `profile`, `revision` and `updated_at`
for an authenticated household connection. It does not read SMTP settings,
migrate legacy files, send mail or change the saved profile. Administrator-only
`ha_tools_email/set_tariff` accepts a validated `profile` (or `null` to clear it)
and `expected_revision`; conflicts require an explicit reload rather than retry.
A successful save requires a completed underlying HA Store write.

Custom profiles support currency, IANA civil time or an explicit fixed meter
clock, zone energy and variable rates, separate monthly fees, weekday/season
rules and date exceptions. Zero rates are valid. Measurements spanning a rate
boundary are not arbitrarily split or priced from an average. Configure prices
explicitly; no national tariff, holiday calendar or current market price is
installed automatically. Compatible Smart Reports and Energy cards use this
shared service; basic Recorder reports do not require SMTP configuration.

## Dispatch attempt history

Administrator-only `ha_tools_email/get_attempt_history` returns at most 128
logical attempts retained for 90 days. Manual services, on-demand reports,
schedules and options tests use one dispatcher and the same history. Metadata
contains an attempt ID, time, controlled kind/source/cadence/period, a hashed
schedule reference, recipient count and controlled outcome. It excludes mail
addresses, subjects, bodies, credentials and raw exception text.

`accepted_by_smtp` means the SMTP server accepted the message, not that it was
delivered. Transport interruption or cancellation can have an unknown outcome;
there is no automatic resend. A failed history write is reported separately as
`history_status: unavailable`. For log digests, failure of the post-send dedup
metadata is separately `digest_state_status: unavailable` and does not turn an
accepted message into `send_failed`. Check the previous attempt before deciding
to send again. SMTP and Store are separate systems: a process crash between
acceptance and the history write may leave no history record.

Schedules and history publish new in-memory state only after confirmed storage.
Failed legacy SMTP migration leaves its file in place; a failed options save
keeps the form open. If an options test was already accepted, the form states
that separately and does not select the test again automatically.

## Installation

### HACS default catalog

HA Tools Email is included in the HACS default catalog as an Integration.
Download it in HACS, restart Home Assistant, then add **HA Tools Email** under
**Settings → Devices & services → Add integration**. Configure SMTP in its
options only when sending is needed. Existing config entries and Store data
are retained on upgrade; do not delete them to update the integration.

### HACS Custom Repository

1. Open HACS.
2. Open **Custom repositories**.
3. Add `https://github.com/MacSiem/ha-tools-email-integration` with category **Integration**.
4. Install **HA Tools Email**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration** and add **HA Tools Email**. The setup form has no fields — just confirm.
7. Open **Configure** on the HA Tools Email integration and enter your SMTP settings. Optionally enable **Send a test email before saving**. SMTP acceptance confirms handoff to the server; check the inbox or provider separately for delivery.

### Manual

1. Copy `custom_components/ha_tools_email/` to `<config>/custom_components/`.
2. Restart Home Assistant.
3. Add **HA Tools Email** from **Settings → Devices & services**.
4. Enter SMTP settings as in step 7 above.

## FAQ

**Where does my email go?**
Only to the SMTP server you configure in the integration options. The
integration does not relay through any third-party or Home Assistant Cloud
service.

**Is my SMTP password exposed to the frontend or other users?**
No, and the two read paths are deliberately different:

- The admin-only `ha_tools_email/get_config` **websocket** command returns a
  `_safe_smtp_config` payload with only `server`, `port`, `username`,
  `sender`, `encryption`, `default_recipient`, `uses_secret` and
  `smtp_configured` — there is no `password` key in the response at all.
- The admin-only `ha_tools_email.get_config` **service** returns the same
  non-secret SMTP state. It never returns a password, password fragment,
  `!secret` reference, or list of available secret-key names.

**Can any logged-in user read or change my SMTP settings?**
No. Since 2.1.0 reading non-secret config and schedules through the websocket
API (`get_config`, `list_schedules`) requires an administrator. Creating, editing or
deleting a schedule (`set_schedule`) and triggering an on-demand send
(`send_now`) require an admin-level websocket connection. The legacy
`save_config`, `get_config`, and `list_secrets` services also require an
administrator. Sending mail through `ha_tools_email.send` and testing the
household SMTP account also require an administrator for interactive calls.
Home Assistant automations without a user context continue to work.

**Does a successful send mean delivery to the inbox?**
No. `accepted_by_smtp` means the configured SMTP server accepted the message.
Final delivery and any later bounce must be checked at the SMTP provider.
The schedule period marker is confirmed before SMTP, preventing a second
callback after reload or restart from sending that period again. A crash after
the marker but before SMTP can leave that period unsent; a crash after SMTP but
before history persistence can leave no attempt record. Check the previous
attempt before a manual resend; neither case triggers automatic retry.

<a id="smtp-password-security"></a>
**Why can't I pass the password to `save_config` any more?**
Home Assistant records the data of every action call in its database. A
password passed to `ha_tools_email.save_config` before 2.1.0 was therefore
also kept in the recorder database and in backups that include it. Since
2.1.0 the password is entered only in **Configure**, and the action rejects a
`password` field. If you saved a password through the action before, Home
Assistant shows a repair notice: create a new app password with your email
provider, enter it in **Configure**, and revoke the old one.

**Do I need a `notify:` platform configured?**
No. The integration talks to your SMTP server directly with `smtplib`.

**What happens if `secrets.yaml` doesn't have the key I referenced?**
The send fails with an explicit error identifying the missing key — nothing
is sent silently.

**Which cards use this integration?**
`ha-log-email` and `ha-energy-email` from their HA Tools frontend packages call
the services described above. Existing automations using
`ha_tools_email.send` continue to work; interactive calls from a
logged-in user require administrator access.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Support

If this tool makes your Home Assistant life easier, consider supporting development:

- [Buy Me a Coffee](https://buymeacoffee.com/macsiem)
- [PayPal](https://www.paypal.com/donate/?hosted_button_id=Y967H4PLRBN8W)

## License

MIT - see [LICENSE](LICENSE).

## Privacy and data

SMTP settings, recipients and reports are processed on your Home Assistant server. Sending a message transmits its content to the configured SMTP provider and recipients. Review those destinations and report content before enabling a schedule. Do not share passwords, addresses or raw configuration in public issues.

See [SECURITY.md](SECURITY.md) for safe vulnerability reporting and [NOTICE](NOTICE) for licensing notices.
