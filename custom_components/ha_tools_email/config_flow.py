"""Config and options flow for HA Tools Email."""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_PASSWORD_SECRET,
    CONF_SEND_TEST,
    DATA_STORAGE,
    DOMAIN,
    NAME,
    VALID_ENCRYPTION,
)

_LOGGER = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class HAToolsEmailConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Zero-input single-instance setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create the single config entry; SMTP is configured in the options."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=NAME, data={})

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the SMTP settings flow."""
        return HAToolsEmailOptionsFlow()


class HAToolsEmailOptionsFlow(config_entries.OptionsFlow):
    """Edit SMTP settings without passing secrets through action data.

    Settings live in the integration's Store (not in entry.options), and the
    stored password is never sent back to the form.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show and save the SMTP settings."""
        # Imported lazily: __init__ imports this module's package.
        from . import (
            _list_secrets,
            _send_email,
            async_update_password_issue,
            build_test_message,
            secret_reference,
        )

        storage = self.hass.data.get(DOMAIN, {}).get(DATA_STORAGE)
        if storage is None:
            return self.async_abort(reason="not_loaded")
        current = await storage.async_get_smtp_config()
        current_password = str(current.get("password", ""))
        errors: dict[str, str] = {}

        if user_input is not None:
            secret_key = str(user_input.get(CONF_PASSWORD_SECRET, "") or "").strip()
            new_password = str(user_input.get("password", "") or "")
            sender = str(user_input.get("sender", "")).strip()
            recipient = str(user_input.get("default_recipient", "") or "").strip()

            if secret_key:
                keys = await self.hass.async_add_executor_job(_list_secrets, self.hass)
                if secret_key not in keys:
                    errors[CONF_PASSWORD_SECRET] = "unknown_secret"
            if sender and not _EMAIL_RE.match(sender):
                errors["sender"] = "invalid_email"
            if recipient and not all(
                _EMAIL_RE.match(item.strip()) for item in recipient.split(",") if item.strip()
            ):
                errors["default_recipient"] = "invalid_email"

            if secret_key and not errors:
                password = secret_reference(secret_key)
            elif new_password:
                password = new_password
            else:
                password = current_password
            if not password and not errors:
                errors["password"] = "password_required"

            cfg = {
                "server": str(user_input.get("server", "")).strip(),
                "port": int(user_input.get("port", 587)),
                "encryption": user_input.get("encryption", "starttls"),
                "username": str(user_input.get("username", "")).strip(),
                "password": password,
                "sender": sender,
                "default_recipient": recipient,
            }

            if not errors and user_input.get(CONF_SEND_TEST):
                to, subject, body, html = build_test_message(cfg)
                try:
                    await self.hass.async_add_executor_job(
                        _send_email, self.hass, cfg, to, subject, body, html
                    )
                except smtplib.SMTPAuthenticationError:
                    errors["base"] = "invalid_auth"
                except (smtplib.SMTPException, ssl.SSLError, OSError):
                    errors["base"] = "cannot_connect"
                except ValueError:
                    errors["base"] = "invalid_config"
                except Exception:  # noqa: BLE001 - surface as a form error, never crash the flow
                    _LOGGER.exception("Unexpected error while sending the SMTP test")
                    errors["base"] = "unknown"

            if not errors:
                password_changed = bool(secret_key or new_password)
                await storage.async_save_smtp_config(
                    cfg, password_via_options=True if password_changed else None
                )
                await async_update_password_issue(self.hass, storage)
                # Nothing secret is kept in entry.options.
                return self.async_create_entry(data={})

            current = {**current, **{k: v for k, v in cfg.items() if k != "password"}}

        if current_password.startswith("!secret "):
            password_state = f"secrets.yaml: {current_password[8:].strip()}"
        elif current_password:
            password_state = "set"
        else:
            password_state = "not set"

        schema = vol.Schema(
            {
                vol.Required("server", default=current.get("server", "")): selector.TextSelector(),
                vol.Required("port", default=int(current.get("port", 587))): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required("encryption", default=current.get("encryption", "starttls")): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(VALID_ENCRYPTION), translation_key="encryption")
                ),
                vol.Required("username", default=current.get("username", "")): selector.TextSelector(),
                vol.Optional("password"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_PASSWORD_SECRET): selector.TextSelector(),
                vol.Required("sender", default=current.get("sender", "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                ),
                vol.Optional("default_recipient", default=current.get("default_recipient", "")): selector.TextSelector(),
                vol.Optional(CONF_SEND_TEST, default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"password_state": password_state},
        )
