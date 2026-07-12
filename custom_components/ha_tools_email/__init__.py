"""HA Tools Email — built-in SMTP for HA Tools panel (zero YAML config).

Supports !secret references in password field — if the saved password
value starts with '!secret ', the actual password is resolved from
secrets.yaml at send time.
"""
import json
import logging
import os
import smtplib
import yaml
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
import homeassistant.helpers.config_validation as cv

from .const import (
    DATA_SERVICES_REGISTERED,
    DATA_STORAGE,
    DATA_WS_REGISTERED,
    DOMAIN,
    VERSION,
)
from .scheduler import async_start_scheduler, async_stop_scheduler
from .storage import EmailStorage
from .websocket_api import async_register_commands

_LOGGER = logging.getLogger(__name__)

CONFIG_DIR = "ha-tools"
CONFIG_FILE = "smtp-config.json"

DEFAULTS = {
    "server": "",
    "port": 587,
    "username": "",
    "password": "",
    "sender": "",
    "encryption": "starttls",
    "default_recipient": "",
}


def _config_path(hass: HomeAssistant) -> Path:
    """Return path to smtp-config.json inside HA config dir."""
    p = Path(hass.config.path(CONFIG_DIR))
    p.mkdir(parents=True, exist_ok=True)
    return p / CONFIG_FILE


def _load_config(hass: HomeAssistant) -> dict:
    """Load SMTP config from disk, return defaults if missing."""
    path = _config_path(hass)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULTS, **data}
            return merged
        except Exception as exc:
            _LOGGER.warning("Failed to read SMTP config: %s", exc)
    return dict(DEFAULTS)


def _save_config(hass: HomeAssistant, data: dict) -> None:
    """Persist SMTP config to disk."""
    path = _config_path(hass)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _LOGGER.info("SMTP config saved to %s", path)


def _mask_password(pwd: str) -> str:
    """Mask password for display: show first 2 and last 2 chars."""
    if not pwd or len(pwd) <= 4:
        return "****"
    return pwd[:2] + "*" * (len(pwd) - 4) + pwd[-2:]


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
        raise ValueError("SMTP not configured — open HA Tools → Settings → Email/SMTP")

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

    if encryption == "ssl":
        smtp = smtplib.SMTP_SSL(server, port, timeout=30)
    else:
        smtp = smtplib.SMTP(server, port, timeout=30)

    try:
        smtp.ehlo()
        if encryption == "starttls":
            smtp.starttls()
            smtp.ehlo()
        smtp.login(username, password)
        smtp.sendmail(sender, recipients, msg.as_string())
        _LOGGER.info("Email sent to %s via %s", recipients, server)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


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
    bucket["load_config"] = _load_config
    bucket["send_email"] = _send_email

    await _async_register_services(hass)

    if not bucket.get(DATA_WS_REGISTERED):
        async_register_commands(hass)
        bucket[DATA_WS_REGISTERED] = True

    await async_start_scheduler(hass, storage, _load_config, _send_email)
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

    async def handle_send(call: ServiceCall) -> None:
        """Handle ha_tools_email.send service call."""
        cfg = await hass.async_add_executor_job(_load_config, hass)

        to = call.data.get("to", "") or cfg.get("default_recipient", "")
        subject = call.data.get("subject", "HA Tools Email")
        body = call.data.get("body", "")
        html = call.data.get("html")

        if not to:
            raise ValueError("No recipient specified and no default_recipient configured")

        await hass.async_add_executor_job(_send_email, hass, cfg, to, subject, body, html)

    async def handle_test(call: ServiceCall) -> None:
        """Send a test email to verify SMTP config."""
        cfg = await hass.async_add_executor_job(_load_config, hass)
        to = cfg.get("default_recipient", "") or cfg.get("sender", "") or cfg.get("username", "")

        if not to:
            raise ValueError("No default_recipient or sender configured — save SMTP settings first")

        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"\u2705 HA Tools \u2014 SMTP Test ({now})"
        body = f"SMTP is configured correctly.\nServer: {cfg.get('server')}:{cfg.get('port')}\nTime: {now}"
        html = (
            f"<h2 style='color:#10b981'>\u2705 SMTP Test OK</h2>"
            f"<p>Server: <code>{cfg.get('server')}:{cfg.get('port')}</code></p>"
            f"<p>Time: {now}</p>"
            f"<hr><p style='font-size:11px;color:#999'>HA Tools Email v{VERSION}</p>"
        )

        await hass.async_add_executor_job(_send_email, hass, cfg, to, subject, body, html)

    async def handle_save_config(call: ServiceCall) -> None:
        """Save SMTP configuration to disk."""
        data = {
            "server": call.data.get("server", ""),
            "port": int(call.data.get("port", 587)),
            "username": call.data.get("username", ""),
            "password": call.data.get("password", ""),
            "sender": call.data.get("sender", ""),
            "encryption": call.data.get("encryption", "starttls"),
            "default_recipient": call.data.get("default_recipient", ""),
        }
        await hass.async_add_executor_job(_save_config, hass, data)

    async def handle_get_config(call: ServiceCall) -> ServiceResponse:
        """Return current SMTP config (password masked)."""
        cfg = await hass.async_add_executor_job(_load_config, hass)
        safe = dict(cfg)
        raw_pwd = safe.get("password", "")
        safe["uses_secret"] = raw_pwd.startswith("!secret ")
        safe["password"] = _mask_password(raw_pwd) if not safe["uses_secret"] else raw_pwd
        safe["configured"] = bool(cfg.get("server") and cfg.get("username") and raw_pwd)
        safe["available_secrets"] = await hass.async_add_executor_job(_list_secrets, hass)
        return safe

    async def handle_list_secrets(call: ServiceCall) -> ServiceResponse:
        """Return list of available secret keys from secrets.yaml."""
        keys = await hass.async_add_executor_job(_list_secrets, hass)
        return {"secrets": keys}

    # Register services
    hass.services.async_register(
        DOMAIN, "send", handle_send,
        schema=vol.Schema({
            vol.Optional("to"): cv.string,
            vol.Required("subject"): cv.string,
            vol.Required("body"): cv.string,
            vol.Optional("html"): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN, "test", handle_test,
        schema=vol.Schema({}),
    )

    hass.services.async_register(
        DOMAIN, "save_config", handle_save_config,
        schema=vol.Schema({
            vol.Required("server"): cv.string,
            vol.Required("port"): vol.Coerce(int),
            vol.Required("username"): cv.string,
            vol.Required("password"): cv.string,
            vol.Required("sender"): cv.string,
            vol.Required("encryption"): vol.In(["starttls", "ssl", "none"]),
            vol.Optional("default_recipient", default=""): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN, "get_config", handle_get_config,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN, "list_secrets", handle_list_secrets,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("HA Tools Email loaded — services: send, test, save_config, get_config, list_secrets")
    bucket[DATA_SERVICES_REGISTERED] = True
