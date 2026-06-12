"""Config flow for HA Tools Email."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries

from .const import DOMAIN, NAME


class HAToolsEmailConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Zero-input single-instance setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create the single config entry without asking for YAML."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=NAME, data={})
