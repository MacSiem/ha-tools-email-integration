"""Store-backed persistence for HA Tools Email."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_SCHEDULE_TIME,
    KIND_LOG_DIGEST,
    SMTP_DEFAULTS,
    STORAGE_KEY,
    STORAGE_VERSION,
    VALID_CADENCES,
    VALID_KINDS,
)

_LOGGER = logging.getLogger(__name__)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SMTP_KEYS = frozenset(SMTP_DEFAULTS)
_LEGACY_CONFIG_PARTS = ("ha-tools", "smtp-config.json")


def _default_state() -> dict[str, Any]:
    """Return the default persisted shape."""
    return {
        "smtp_config": None,
        "schedules": [],
        "digest_state": {},
        "scheduler_state": {},
    }


class EmailStorage:
    """Thin async wrapper around Home Assistant Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind storage to a Home Assistant instance."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any]:
        """Load storage, creating defaults when absent."""
        async with self._lock:
            if self._data is None:
                loaded = await self._store.async_load()
                if not isinstance(loaded, dict):
                    loaded = {}
                self._data = _default_state()
                self._deep_merge(self._data, loaded)
                self._data["schedules"] = self._clean_schedule_list(
                    self._data.get("schedules", [])
                )
                await self._async_migrate_legacy_smtp_config_locked(loaded)
            return deepcopy(self._data)

    async def async_get_state(self) -> dict[str, Any]:
        """Return the full persisted state."""
        return await self.async_load()

    async def async_get_smtp_config(self) -> dict[str, Any]:
        """Return the SMTP configuration from Store with defaults applied."""
        data = await self.async_load()
        return self._clean_smtp_config(data.get("smtp_config"))

    async def async_save_smtp_config(self, config: dict[str, Any]) -> None:
        """Persist the SMTP configuration in Home Assistant Store."""
        clean = self._clean_smtp_config(config)
        async with self._lock:
            data = await self._ensure_loaded_locked()
            data["smtp_config"] = clean
            await self._store.async_save(data)

    async def _async_migrate_legacy_smtp_config_locked(
        self, loaded: dict[str, Any]
    ) -> None:
        """Migrate the old JSON file after Store has loaded successfully."""
        legacy_path = Path(self.hass.config.path(*_LEGACY_CONFIG_PARTS))
        stored_config = loaded.get("smtp_config")

        if isinstance(stored_config, dict):
            await self.hass.async_add_executor_job(_remove_legacy_config, legacy_path)
            return

        legacy_config = await self.hass.async_add_executor_job(
            _read_legacy_config, legacy_path
        )
        if legacy_config is None:
            return

        clean = self._clean_smtp_config(legacy_config)
        self._data["smtp_config"] = clean
        await self._store.async_save(self._data)
        await self.hass.async_add_executor_job(_remove_legacy_config, legacy_path)
        _LOGGER.info("Migrated SMTP configuration to Home Assistant Store")

    async def async_list_schedules(self) -> list[dict[str, Any]]:
        """Return configured schedules."""
        data = await self.async_load()
        return data["schedules"]

    async def async_get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        """Return a schedule by id."""
        data = await self.async_load()
        for schedule in data["schedules"]:
            if schedule.get("id") == schedule_id:
                return deepcopy(schedule)
        return None

    async def async_upsert_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        """Create or update one schedule."""
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be an object")

        async with self._lock:
            data = await self._ensure_loaded_locked()
            schedule_id = str(schedule.get("id") or uuid.uuid4())
            clean = self._clean_schedule(schedule, existing_id=schedule_id)
            clean["updated_at"] = _now_iso()

            for idx, existing in enumerate(data["schedules"]):
                if existing.get("id") == schedule_id:
                    clean["created_at"] = existing.get("created_at") or clean["updated_at"]
                    data["schedules"][idx] = clean
                    await self._store.async_save(data)
                    return deepcopy(clean)

            clean["created_at"] = clean["updated_at"]
            data["schedules"].append(clean)
            await self._store.async_save(data)
            return deepcopy(clean)

    async def async_delete_schedule(self, schedule_id: str) -> bool:
        """Delete one schedule by id."""
        async with self._lock:
            data = await self._ensure_loaded_locked()
            before = len(data["schedules"])
            data["schedules"] = [
                schedule
                for schedule in data["schedules"]
                if schedule.get("id") != schedule_id
            ]
            removed = len(data["schedules"]) != before
            if removed:
                data["scheduler_state"].pop(schedule_id, None)
                await self._store.async_save(data)
            return removed

    async def async_get_digest_state(self, kind: str, cadence: str) -> dict[str, Any]:
        """Return digest dedup state for one report stream."""
        data = await self.async_load()
        return deepcopy(
            data.get("digest_state", {}).get(kind, {}).get(cadence, {})
        )

    async def async_set_digest_state(
        self, kind: str, cadence: str, state: dict[str, Any]
    ) -> None:
        """Persist digest dedup state for one report stream."""
        async with self._lock:
            data = await self._ensure_loaded_locked()
            data.setdefault("digest_state", {}).setdefault(kind, {})[cadence] = deepcopy(
                state
            )
            await self._store.async_save(data)

    async def async_get_last_fired(self, schedule_id: str) -> str | None:
        """Return the last fired period key for a schedule."""
        data = await self.async_load()
        state = data.get("scheduler_state", {}).get(schedule_id, {})
        return state.get("last_fired_period")

    async def async_set_last_fired(
        self, schedule_id: str, period_key: str, fired_at: str
    ) -> None:
        """Persist schedule last-fired metadata."""
        async with self._lock:
            data = await self._ensure_loaded_locked()
            data.setdefault("scheduler_state", {})[schedule_id] = {
                "last_fired_period": period_key,
                "last_fired_at": fired_at,
            }
            await self._store.async_save(data)

    async def _ensure_loaded_locked(self) -> dict[str, Any]:
        """Load data while the caller holds the lock."""
        if self._data is None:
            loaded = await self._store.async_load()
            if not isinstance(loaded, dict):
                loaded = {}
            self._data = _default_state()
            self._deep_merge(self._data, loaded)
            self._data["schedules"] = self._clean_schedule_list(
                self._data.get("schedules", [])
            )
        return self._data

    @staticmethod
    def _clean_smtp_config(config: Any) -> dict[str, Any]:
        """Return only supported SMTP fields with stable defaults."""
        if not isinstance(config, dict):
            return dict(SMTP_DEFAULTS)
        clean = {key: config[key] for key in _SMTP_KEYS if key in config}
        merged = {**SMTP_DEFAULTS, **clean}
        try:
            merged["port"] = int(merged["port"])
        except (TypeError, ValueError):
            merged["port"] = SMTP_DEFAULTS["port"]
        return merged

    @staticmethod
    def _clean_schedule(
        schedule: dict[str, Any], existing_id: str | None = None
    ) -> dict[str, Any]:
        """Validate and normalize a schedule payload."""
        kind = str(schedule.get("kind") or "").strip()
        cadence = str(schedule.get("cadence") or "").strip()
        time_value = str(schedule.get("time") or DEFAULT_SCHEDULE_TIME).strip()

        if kind not in VALID_KINDS:
            raise ValueError(f"invalid schedule kind: {kind}")
        if cadence not in VALID_CADENCES:
            raise ValueError(f"invalid schedule cadence: {cadence}")
        if not _TIME_RE.match(time_value):
            raise ValueError("schedule time must use HH:MM in 24-hour format")

        recipients = schedule.get("recipients") or []
        if isinstance(recipients, str):
            recipients = [item.strip() for item in recipients.split(",")]
        if not isinstance(recipients, list):
            raise ValueError("recipients must be a list or comma-separated string")
        recipients = [str(item).strip() for item in recipients if str(item).strip()]

        return {
            "id": str(existing_id or schedule.get("id") or uuid.uuid4()),
            "kind": kind,
            "cadence": cadence,
            "time": time_value,
            "recipients": recipients,
            "enabled": bool(schedule.get("enabled", True)),
        }

    @classmethod
    def _clean_schedule_list(cls, schedules: Any) -> list[dict[str, Any]]:
        """Return only valid schedules from stored data."""
        clean: list[dict[str, Any]] = []
        if not isinstance(schedules, list):
            return clean
        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            try:
                clean.append(cls._clean_schedule(schedule, existing_id=schedule.get("id")))
            except ValueError:
                continue
        return clean

    @classmethod
    def _deep_merge(cls, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._deep_merge(target[key], value)
            else:
                target[key] = deepcopy(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_legacy_config(path: Path) -> dict[str, Any] | None:
    """Read a legacy SMTP JSON file off the event loop."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as file_handle:
            loaded = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not migrate legacy SMTP configuration: %s", err)
        return None
    if not isinstance(loaded, dict):
        _LOGGER.warning("Could not migrate legacy SMTP configuration: invalid data")
        return None
    return loaded


def _remove_legacy_config(path: Path) -> None:
    """Remove the plaintext legacy file after Store persistence succeeds."""
    try:
        path.unlink(missing_ok=True)
    except OSError as err:
        _LOGGER.warning("Could not remove migrated legacy SMTP configuration: %s", err)
