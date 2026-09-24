"""HA Tools Email — built-in SMTP for HA Tools panel (zero YAML config).

Supports !secret references in password field — if the saved password
value starts with '!secret ', the actual password is resolved from
secrets.yaml at send time.
"""
import inspect
import logging
from collections.abc import Awaitable, Callable
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import voluptuous as vol
import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util.ssl import client_context

from .const import (
    CONF_PASSWORD_SECRET,
    DATA_SERVICES_REGISTERED,
    DATA_STORAGE,
    DATA_WS_REGISTERED,
    DOMAIN,
    ISSUE_PASSWORD_IN_HISTORY,
    SETTINGS_HINT,
    SMTP_DEFAULTS,
    VALID_ENCRYPTION,
    VERSION,
)
from .scheduler import async_start_scheduler, async_stop_scheduler
from .smtp import open_smtp_connection
from .storage import EmailStorage
from .websocket_api import async_register_commands

_LOGGER = logging.getLogger(__name__)

def _resolve_secret(hass: HomeAssistant, value: str) -> str:
    """Resolve !secret references from secrets.yaml.

    If value starts with '!secret ', look up the key in secrets.yaml.
    Otherwise return the value as-is.
    """
    if not value or not value.startswith("!secret "):
        return value
    secret_key = value[8:].strip()
    if not secret_key:
        return value
    secrets_path = Path(hass.config.path("secrets.yaml"))
    if not secrets_path.exists():
        raise ValueError(f"secrets.yaml not found — cannot resolve !secret {secret_key}")
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f) or {}
        if secret_key not in secrets:
            raise ValueError(f"Key '{secret_key}' not found in secrets.yaml")
        return str(secrets[secret_key])
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse secrets.yaml: {exc}")


def _list_secrets(hass: HomeAssistant) -> list[str]:
    """Return list of available secret keys (names only, no values)."""
    secrets_path = Path(hass.config.path("secrets.yaml"))
    if not secrets_path.exists():
        return []
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f) or {}
        return sorted(secrets.keys())
    except Exception:
        return []


def _send_email(hass: HomeAssistant, cfg: dict, to: str, subject: str, body: str, html: str | None = None) -> None:
    """Send email via SMTP using provided config."""
    server = cfg.get("server", "")
    port = int(cfg.get("port", 587))
    username = cfg.get("username", "")
    password = _resolve_secret(hass, cfg.get("password", ""))
    sender = cfg.get("sender", "") or username
    encryption = cfg.get("encryption", "starttls")

    if not server or not username or not password:
        raise ValueError(f"SMTP not configured — open {SETTINGS_HINT}")

    # Parse recipients
    recipients = [r.strip() for r in to.split(",") if r.strip() and "@" in r.strip()]
    if not recipients:
        raise ValueError(f"No valid recipients in: {to}")

    # Build message
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    # Always attach plain text
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach HTML if provided
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    # Connect and send
    _LOGGER.debug("Connecting to %s:%d (encryption=%s)", server, port, encryption)

    smtp = open_smtp_connection(server, port, encryption, client_context())

    try:
        smtp.login(username, password)
        smtp.sendmail(sender, recipients, msg.as_string())
        _LOGGER.info("Email sent to %s via %s", recipients, server)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def build_test_message(cfg: dict) -> tuple[str, str, str, str]:
    """Return (recipient, subject, body, html) for an SMTP test message."""
    from datetime import datetime

    to = (
        cfg.get("default_recipient", "")
        or cfg.get("sender", "")
        or cfg.get("username", "")
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"\u2705 HA Tools \u2014 SMTP Test ({now})"
    body = f"SMTP is configured correctly.\nServer: {cfg.get('server')}:{cfg.get('port')}\nTime: {now}"
    html = (
        f"<h2 style='color:#10b981'>\u2705 SMTP Test OK</h2>"
        f"<p>Server: <code>{cfg.get('server')}:{cfg.get('port')}</code></p>"
        f"<p>Time: {now}</p>"
        f"<hr><p style='font-size:11px;color:#999'>HA Tools Email v{VERSION}</p>"
    )
    return to, subject, body, html


def secret_reference(key: str) -> str:
    """Return the stored form of a secrets.yaml reference."""
    return f"!secret {key.strip()}"


async def async_update_password_issue(hass: HomeAssistant, storage: EmailStorage) -> None:
    """Warn once about literal passwords saved through action data before 2.1.0."""
    cfg = await storage.async_get_smtp_config()
    password = str(cfg.get("password", ""))
    at_risk = (
        bool(password)
        and not password.startswith("!secret ")
        and not await storage.async_password_via_options()
    )
    if at_risk:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_PASSWORD_IN_HISTORY,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PASSWORD_IN_HISTORY,
            learn_more_url="https://github.com/MacSiem/ha-tools-email-integration#smtp-password-security",
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_PASSWORD_IN_HISTORY)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up HA Tools Email component."""
    await _async_ensure_setup(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA Tools Email from a config entry."""
    await _async_ensure_setup(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    async_stop_scheduler(hass)
    return True


async def _async_ensure_setup(hass: HomeAssistant) -> None:
    """Initialize storage, services, websocket API, and scheduler once."""
    bucket = hass.data.setdefault(DOMAIN, {})
    storage = await _async_get_storage(hass)
    bucket["load_config"] = storage.async_get_smtp_config
    bucket["send_email"] = _send_email

    await _async_register_services(hass)

    if not bucket.get(DATA_WS_REGISTERED):
        async_register_commands(hass)
        bucket[DATA_WS_REGISTERED] = True

    await async_start_scheduler(
        hass, storage, storage.async_get_smtp_config, _send_email
    )
    await async_update_password_issue(hass, storage)
    _LOGGER.info("HA Tools Email loaded")


async def _async_get_storage(hass: HomeAssistant) -> EmailStorage:
    """Return the Store wrapper, loading it on first use."""
    bucket = hass.data.setdefault(DOMAIN, {})
    storage = bucket.get(DATA_STORAGE)
    if storage is None:
        storage = EmailStorage(hass)
        await storage.async_load()
        bucket[DATA_STORAGE] = storage
    return storage


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register legacy services used by existing HA Tools cards."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get(DATA_SERVICES_REGISTERED):
        return
    storage = await _async_get_storage(hass)

    async def handle_send(call: ServiceCall) -> None:
        """Handle ha_tools_email.send service call."""
        cfg = await storage.async_get_smtp_config()

        to = call.data.get("to", "") or cfg.get("default_recipient", "")
        subject = call.data.get("subject", "HA Tools Email")
        body = call.data.get("body", "")
        html = call.data.get("html")

        if not to:
            raise ValueError("No recipient specified and no default_recipient configured")

        await hass.async_add_executor_job(_send_email, hass, cfg, to, subject, body, html)

    async def handle_test(call: ServiceCall) -> None:
        """Send a test email to verify SMTP config."""
        cfg = await storage.async_get_smtp_config()
        to, subject, body, html = build_test_message(cfg)
        if not to:
            raise ValueError(
                f"No default_recipient or sender configured — open {SETTINGS_HINT}"
            )
        await hass.async_add_executor_job(
            _send_email, hass, cfg, to, subject, body, html
        )

    async def handle_save_config(call: ServiceCall) -> None:
        """Save non-secret SMTP settings; passwords are never accepted as action data.

        Home Assistant records action data in its database, so a password passed
        here would be kept in the recorder history and in every backup.
        """
        if "password" in call.data:
            raise ServiceValidationError(
                "Passwords can no longer be passed as action data because Home Assistant "
                "stores action data in its database. Set the password in "
                f"{SETTINGS_HINT}, or pass password_secret with the name of a "
                "secrets.yaml key."
            )
        current = await storage.async_get_smtp_config()
        password = current.get("password", "")
        secret_key = str(call.data.get(CONF_PASSWORD_SECRET, "") or "").strip()
        if secret_key:
            keys = await hass.async_add_executor_job(_list_secrets, hass)
            if secret_key not in keys:
                raise ServiceValidationError(
                    f"secrets.yaml has no key named '{secret_key}'"
                )
            password = secret_reference(secret_key)
        data = {
            "server": call.data.get("server", ""),
            "port": int(call.data.get("port", 587)),
            "username": call.data.get("username", ""),
            "password": password,
            "sender": call.data.get("sender", ""),
            "encryption": call.data.get("encryption", "starttls"),
            "default_recipient": call.data.get("default_recipient", ""),
        }
        await storage.async_save_smtp_config(data)
        await async_update_password_issue(hass, storage)

    async def handle_get_config(call: ServiceCall) -> ServiceResponse:
        """Return non-secret SMTP state to an administrator."""
        cfg = await storage.async_get_smtp_config()
        raw_pwd = str(cfg.get("password", ""))
        return {
            "server": cfg.get("server", ""),
            "port": cfg.get("port", SMTP_DEFAULTS["port"]),
            "username": cfg.get("username", ""),
            "sender": cfg.get("sender", ""),
            "encryption": cfg.get("encryption", SMTP_DEFAULTS["encryption"]),
            "default_recipient": cfg.get("default_recipient", ""),
            "uses_secret": raw_pwd.startswith("!secret "),
            "configured": bool(
                cfg.get("server") and cfg.get("username") and raw_pwd
            ),
        }

    async def handle_list_secrets(call: ServiceCall) -> ServiceResponse:
        """Return list of available secret keys from secrets.yaml."""
        keys = await hass.async_add_executor_job(_list_secrets, hass)
        return {"secrets": keys}

    # Register services
    async_register_admin_service(
        hass, DOMAIN, "send", handle_send,
        schema=vol.Schema({
            vol.Optional("to"): cv.string,
            vol.Required("subject"): cv.string,
            vol.Required("body"): cv.string,
            vol.Optional("html"): cv.string,
        }),
    )

    async_register_admin_service(
        hass, DOMAIN, "test", handle_test,
        schema=vol.Schema({}),
    )

    async_register_admin_service(
        hass, DOMAIN, "save_config", handle_save_config,
        schema=vol.Schema({
            vol.Required("server"): cv.string,
            vol.Required("port"): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required("username"): cv.string,
            # Accepted only to return a clear error; never stored.
            vol.Optional("password"): cv.string,
            vol.Optional(CONF_PASSWORD_SECRET): cv.string,
            vol.Required("sender"): cv.string,
            vol.Required("encryption"): vol.In(list(VALID_ENCRYPTION)),
            vol.Optional("default_recipient", default=""): cv.string,
        }),
    )

    _register_admin_response_service(
        hass, DOMAIN, "get_config", handle_get_config,
        schema=vol.Schema({}),
    )

    _register_admin_response_service(
        hass, DOMAIN, "list_secrets", handle_list_secrets,
        schema=vol.Schema({}),
    )

    _LOGGER.info(
        "HA Tools Email loaded — services: send, test, save_config, "
        "get_config, list_secrets"
    )
    bucket[DATA_SERVICES_REGISTERED] = True


def _register_admin_response_service(
    hass: HomeAssistant,
    domain: str,
    service: str,
    handler: Callable[[ServiceCall], Awaitable[ServiceResponse]],
    *,
    schema: vol.Schema,
) -> None:
    """Register an admin response service across supported HA versions."""
    if "supports_response" in inspect.signature(
        async_register_admin_service
    ).parameters:
        async_register_admin_service(
            hass,
            domain,
            service,
            handler,
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )
        return

    async def admin_handler(call: ServiceCall) -> ServiceResponse:
        """Backport the helper's fail-closed gate while preserving responses."""
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                raise Unauthorized(context=call.context)
        return await handler(call)

    hass.services.async_register(
        domain,
        service,
        admin_handler,
        schema=schema,
        supports_response=SupportsResponse.ONLY,
    )
