"""Store-backed persistence for HA Tools Email."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
try:
    from homeassistant.core import CoreState
except ImportError:  # Lightweight boundary fixtures; HA provides the real enum.
    CoreState = None
from homeassistant.helpers.storage import Store

from .tariff import TariffValidationError, validate_tariff_profile
from .tariff_history import prepare_tariff_book_edit, validate_tariff_book
from .scheduler import normalize_schedule_calendar
from .history import HISTORY_SCHEMA, HISTORY_LIMIT, HISTORY_DAYS, clean_attempt, retained_attempts

from .const import (
    DEFAULT_SCHEDULE_TIME,
    DOMAIN,
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


class TariffRevisionConflict(ValueError):
    """The caller must reload settings before making a new explicit save."""


class StorageWriteError(RuntimeError):
    """A logical state save returned without a confirmed lower Store write."""


class _ConfirmedStore(Store):
    """Reuse Frigate's lower-write receipt for explicit durable state saves.

    HA Store may log and swallow lower write errors or skip a write at shutdown.
    The base async_save keeps HA behavior; callers explicitly require confirmation.
    EmailStorage serializes all calls through its existing lock.
    """

    def __init__(self, hass: HomeAssistant, *args: Any, **kwargs: Any) -> None:
        self._state_hass = hass
        self._confirming_write = False
        self._write_confirmed = False
        self._last_write_error: Exception | None = None
        super().__init__(hass, *args, **kwargs)

    async def async_save_confirmed(self, data: dict[str, Any]) -> None:
        """Require a completed lower write before publishing logical state."""
        stopping = CoreState.stopping if CoreState else "stopping"
        if getattr(self._state_hass, "state", None) == stopping:
            raise StorageWriteError("email state write skipped during shutdown")
        self._last_write_error = None
        self._write_confirmed = False
        self._confirming_write = True
        try:
            await super().async_save(data)
            if error := self._last_write_error:
                raise StorageWriteError("email state was not durably saved") from error
            if not self._write_confirmed:
                raise StorageWriteError("email state write was not confirmed")
        finally:
            self._confirming_write = False

    async def _async_write_data(self, *args: Any, **kwargs: Any) -> None:
        try:
            # Older HA passes (path, data); current HA passes (data).
            await super()._async_write_data(*args, **kwargs)
        except Exception as error:
            if self._confirming_write:
                self._last_write_error = error
            raise  # Preserve HA's existing error handling/logging.
        else:
            if self._confirming_write:
                self._write_confirmed = True


def _default_state() -> dict[str, Any]:
    """Return the default persisted shape."""
    return {
        "smtp_config": None,
        "tariff_profile": None,
        "tariff_book": None,
        "tariff_revision": 0,
        "tariff_updated_at": None,
        "schedules": [],
        "digest_state": {},
        "scheduler_state": {},
        # Set once a password has been entered through the options flow. Passwords
        # saved before 2.1.0 went through action data, which the recorder keeps.
        "security": {"password_via_options": False},
    }


class EmailStorage:
    """Thin async wrapper around Home Assistant Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind storage to a Home Assistant instance."""
        self.hass = hass
        self._store: _ConfirmedStore = _ConfirmedStore(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None
        self._tariff_only_loaded: dict[str, Any] | None = None
        self._book_pending = 0

    async def async_load(self) -> dict[str, Any]:
        """Load storage, creating defaults when absent."""
        async with self._lock:
            if self._data is None:
                loaded = await self._store.async_load()
                if not isinstance(loaded, dict):
                    loaded = {}
                self._data = _default_state()
                self._deep_merge(self._data, loaded)
                await self._async_migrate_legacy_smtp_config_locked(loaded)
            if self._tariff_only_loaded is not None:
                loaded = deepcopy(self._data)
                await self._async_migrate_legacy_smtp_config_locked(loaded)
                self._tariff_only_loaded = None
            return deepcopy(self._data)

    @property
    def current_tariff_revision(self) -> int | None:
        """Synchronous publication token after confirmed writes, without IO.

        Used after the final authorization await so unload/save cannot publish an
        answer whose context was invalidated during that await.
        """
        if self._data is None:
            return None
        revision = self._data.get('tariff_revision', 0)
        return revision if type(revision) is int and revision >= 0 else 0

    async def async_get_tariff(self) -> dict[str, Any]:
        """Authenticated household settings read; never migrates or reads SMTP files."""
        async with self._lock:
            data = await self._async_tariff_data_locked()
            return self._tariff_state(data)

    @contextmanager
    def _book_admission(self):
        if self._book_pending >= 4:
            raise RuntimeError('energy_pricing_busy')
        self._book_pending += 1
        try:
            yield
        finally:
            self._book_pending -= 1

    async def _async_book_cpu(self, job):
        # Reuse the existing bounded executor lane, including cancellation and
        # unload protection. No second unbounded thread pool for large books.
        bucket = getattr(self.hass, 'data', {}).get(DOMAIN, {})
        coordinator = bucket.get('energy_snapshot')
        if coordinator is None or not getattr(coordinator, 'running', True):
            raise RuntimeError('energy_unavailable')
        result = await coordinator._async_run_cpu(job)
        if (getattr(self.hass, 'data', {}).get(DOMAIN, {}).get('energy_snapshot') is not coordinator
                or not getattr(coordinator, 'running', True)):
            raise RuntimeError('energy_snapshot_unloaded')
        return result

    async def async_get_tariff_book(self) -> dict[str, Any]:
        """Read an additive book snapshot without migrating SMTP or guessing v1."""
        with self._book_admission():
            async with self._lock:
                data = await self._async_tariff_data_locked()
                state = self._tariff_state(data)
                raw = deepcopy(data.get('tariff_book'))
            if raw is not None:
                try:
                    raw = await self._async_book_cpu(partial(validate_tariff_book, raw))
                except TariffValidationError:
                    raw = {'schema': 0}
            return {'book': raw, 'revision': state['revision'], 'updated_at': state['updated_at'],
                    'legacy_profile': state['profile'] if raw is None else None}

    async def async_preview_tariff_book(self, book, *, expected_revision, now=None):
        """Return validated proposed history and exact past ranges, without writes."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise TariffValidationError([{'path': 'expected_revision', 'reason': 'invalid_revision'}])
        with self._book_admission():
            async with self._lock:
                data = await self._async_tariff_data_locked()
                if self._tariff_state(data)['revision'] != expected_revision:
                    raise TariffRevisionConflict('tariff_revision_conflict')
                clean, changes = await self._async_book_cpu(partial(
                    prepare_tariff_book_edit, data.get('tariff_book'), book, now or datetime.now(timezone.utc)))
                return {'book': clean, 'historical_changes': changes, 'revision': expected_revision}

    async def async_save_tariff_book(self, book, *, expected_revision, historical_edit_ack=False,
                                    now=None, before_save=None) -> dict[str, Any]:
        """Publish one validated copy only after the existing confirmed Store write.

        The admin route supplies a fresh authorization/lifecycle callback after
        CPU validation. Historical edits need explicit acknowledgement; the
        legacy profile and archived attempt reports remain byte-for-byte data.
        """
        if type(expected_revision) is not int or expected_revision < 0 or type(historical_edit_ack) is not bool:
            raise TariffValidationError([{'path': 'expected_revision', 'reason': 'invalid_revision_or_ack'}])
        with self._book_admission():
            async with self._lock:
                data = await self._async_tariff_data_locked()
                if self._tariff_state(data)['revision'] != expected_revision:
                    raise TariffRevisionConflict('tariff_revision_conflict')
                clean, changes = await self._async_book_cpu(partial(
                    prepare_tariff_book_edit, data.get('tariff_book'), book, now or datetime.now(timezone.utc)))
                if changes and not historical_edit_ack:
                    raise ValueError('historical_edit_ack_required')
                if before_save is not None and not await before_save():
                    raise PermissionError('unauthorized')
                candidate = deepcopy(data)
                candidate.update(tariff_book=clean, tariff_revision=expected_revision + 1,
                                 tariff_updated_at=_now_iso())
                await self._store.async_save_confirmed(candidate)
                self._data = candidate
                return {'book': deepcopy(clean), 'revision': expected_revision + 1,
                        'updated_at': candidate['tariff_updated_at'], 'legacy_profile': None}

    async def async_save_tariff(self, profile: Any, *, expected_revision: int) -> dict[str, Any]:
        """Validate, compare revision, save a copy, then publish confirmed state.

        No retry: a stale editor must explicitly reload. None clears the profile.
        SMTP, schedules and unknown Store keys are retained verbatim.
        """
        clean = validate_tariff_profile(profile)
        if type(expected_revision) is not int or expected_revision < 0:
            raise TariffValidationError([{"path": "expected_revision", "reason": "invalid_revision"}])
        async with self._lock:
            data = await self._async_tariff_data_locked()
            current = self._tariff_state(data)
            if current["revision"] != expected_revision:
                raise TariffRevisionConflict("tariff_revision_conflict")
            candidate = deepcopy(data)
            candidate.update(tariff_profile=clean, tariff_revision=expected_revision + 1,
                             tariff_updated_at=_now_iso())
            await self._store.async_save_confirmed(candidate)
            self._data = candidate
            return self._tariff_state(candidate)

    async def _async_tariff_data_locked(self) -> dict[str, Any]:
        """Load Store verbatim for tariffs, deferring the legacy SMTP migration."""
        if self._data is None:
            loaded = await self._store.async_load()
            loaded = loaded if isinstance(loaded, dict) else {}
            self._data = _default_state()
            self._deep_merge(self._data, loaded)
            self._tariff_only_loaded = deepcopy(loaded)
        return self._data

    @staticmethod
    def _tariff_state(data: dict[str, Any]) -> dict[str, Any]:
        revision = data.get("tariff_revision", 0)
        revision = revision if type(revision) is int and revision >= 0 else 0
        profile = data.get("tariff_profile")
        try:
            profile = validate_tariff_profile(profile)
        except TariffValidationError:
            # Keep the persisted value untouched, but expose a bounded invalid
            # marker rather than unknown historical fields to household readers.
            profile = {"schema": 0}
        stamp = data.get("tariff_updated_at")
        return {"profile": profile, "revision": revision,
                "updated_at": stamp if isinstance(stamp, str) and len(stamp) <= 64 else None}

    async def async_get_attempt_history(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Admin metadata read, with age/count filtering and no SMTP migration."""
        async with self._lock:
            data = await self._async_tariff_data_locked()
            entries = retained_attempts(data.get("attempt_history"), now or datetime.now(timezone.utc))
            return {"schema": HISTORY_SCHEMA, "limit": HISTORY_LIMIT,
                    "retention_days": HISTORY_DAYS, "attempts": deepcopy(list(reversed(entries)))}

    async def async_append_attempt(self, attempt: dict[str, Any], *, now: datetime | None = None) -> None:
        """Copy-on-write, serialized Store save; publish only after atomic persistence.

        Additive sub-schema migration keeps SMTP/tariff/schedules/unknown keys
        verbatim. Home Assistant Store owns executor I/O and atomic replacement.
        No filesystem work occurs on the event loop here.
        """
        async with self._lock:
            data = await self._async_tariff_data_locked()
            clock = now or datetime.now(timezone.utc)
            entries = retained_attempts(data.get("attempt_history"), clock)
            clean = clean_attempt(attempt)
            if clean is None:
                raise ValueError("invalid_attempt")
            entries = [entry for entry in entries if entry["id"] != clean["id"]]
            entries.append(clean)
            candidate = deepcopy(data)
            section = deepcopy(data.get("attempt_history") or {})
            section.update(schema=HISTORY_SCHEMA, attempts=entries)
            section["attempts"] = retained_attempts(section, clock)
            candidate["attempt_history"] = section
            await self._store.async_save_confirmed(candidate)
            self._data = candidate

    async def async_get_state(self) -> dict[str, Any]:
        """Return the full persisted state."""
        return await self.async_load()

    async def async_get_smtp_config(self) -> dict[str, Any]:
        """Return the SMTP configuration from Store with defaults applied."""
        data = await self.async_load()
        return self._clean_smtp_config(data.get("smtp_config"))

    async def async_save_smtp_config(
        self, config: dict[str, Any], *, password_via_options: bool | None = None
    ) -> None:
        """Persist the SMTP configuration in Home Assistant Store."""
        clean = self._clean_smtp_config(config)
        async with self._lock:
            data = await self._ensure_loaded_locked()
            candidate = deepcopy(data)
            candidate["smtp_config"] = clean
            if password_via_options is not None:
                security = candidate.setdefault("security", {})
                security["password_via_options"] = bool(password_via_options)
            await self._store.async_save_confirmed(candidate)
            self._data = candidate

    async def async_password_via_options(self) -> bool:
        """Return True when the stored password was entered through the options flow."""
        data = await self.async_load()
        return bool((data.get("security") or {}).get("password_via_options"))

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
        candidate = deepcopy(self._data)
        candidate["smtp_config"] = clean
        await self._store.async_save_confirmed(candidate)
        self._data = candidate
        await self.hass.async_add_executor_job(_remove_legacy_config, legacy_path)
        _LOGGER.info("Migrated SMTP configuration to Home Assistant Store")

    async def async_list_schedules(self) -> list[dict[str, Any]]:
        """Return configured schedules."""
        async with self._lock:
            data = await self._async_tariff_data_locked()
            # Cold ZoneInfo lookup reads tzdata; keep validation off HA's loop.
            return await asyncio.to_thread(self._clean_schedule_list, deepcopy(data.get("schedules", [])))

    async def async_get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        """Return a schedule by id."""
        for schedule in await self.async_list_schedules():
            if schedule.get("id") == schedule_id:
                return deepcopy(schedule)
        return None

    async def async_upsert_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        """Validate an explicit save, preserving other Store sections verbatim."""
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be an object")
        # Reject calendar fields before any load/save or SMTP side effect.
        await asyncio.to_thread(normalize_schedule_calendar, schedule)
        async with self._lock:
            data = await self._async_tariff_data_locked()
            candidate = deepcopy(data)
            schedule_id = str(schedule.get("id") or uuid.uuid4())
            rows = candidate.get("schedules", [])
            if not isinstance(rows, list):
                raise ValueError("stored schedules must be a list")
            index = next((i for i, row in enumerate(rows)
                          if isinstance(row, dict) and self._stored_schedule_id(row) == schedule_id), None)
            existing = rows[index] if index is not None else {}
            merged = {**existing, **schedule}
            if schedule.get("use_ha_timezone") is True:
                merged.pop("timezone", None)
            # Canonical edits supersede inherited aliases, explicit conflicts fail.
            for canonical, aliases in (("weekday", ("weekly_day",)),
                                       ("month_day", ("day", "monthday"))):
                if canonical in schedule:
                    for alias in aliases:
                        if alias not in schedule:
                            merged.pop(alias, None)
            # Transient list metadata is never persisted from UI echoes.
            for key in ("next_fire", "effective_timezone", "calendar_policy"):
                merged.pop(key, None)
            clean = await asyncio.to_thread(self._clean_schedule, merged, existing_id=schedule_id)
            clean["updated_at"] = _now_iso()
            clean["created_at"] = existing.get("created_at") or clean["updated_at"]
            if index is None:
                rows.append(clean)
            else:
                rows[index] = clean
            candidate["schedules"] = rows
            await self._store.async_save_confirmed(candidate)
            self._data = candidate
            return deepcopy(clean)

    async def async_delete_schedule(self, schedule_id: str) -> bool:
        """Delete one schedule by id."""
        async with self._lock:
            data = deepcopy(await self._async_tariff_data_locked())
            before = len(data["schedules"])
            data["schedules"] = [
                schedule
                for schedule in data["schedules"]
                if not isinstance(schedule, dict) or self._stored_schedule_id(schedule) != schedule_id
            ]
            removed = len(data["schedules"]) != before
            if removed:
                data["scheduler_state"].pop(schedule_id, None)
                await self._store.async_save_confirmed(data)
                self._data = data
            return removed

    async def async_get_digest_state(self, kind: str, cadence: str) -> dict[str, Any]:
        """Return digest dedup state for one report stream."""
        async with self._lock:
            data = await self._async_tariff_data_locked()
            return deepcopy(data.get("digest_state", {}).get(kind, {}).get(cadence, {}))

    async def async_set_digest_state(
        self, kind: str, cadence: str, state: dict[str, Any]
    ) -> None:
        """Persist digest dedup state for one report stream."""
        async with self._lock:
            data = deepcopy(await self._async_tariff_data_locked())
            data.setdefault("digest_state", {}).setdefault(kind, {})[cadence] = deepcopy(
                state
            )
            await self._store.async_save_confirmed(data)
            self._data = data

    async def async_get_last_fired(self, schedule_id: str) -> str | None:
        """Return the last fired period key for a schedule."""
        async with self._lock:
            data = await self._async_tariff_data_locked()
            state = data.get("scheduler_state", {}).get(schedule_id, {})
            return state.get("last_fired_period")

    async def async_set_last_fired(
        self, schedule_id: str, period_key: str, fired_at: str
    ) -> None:
        """Persist schedule last-fired metadata."""
        async with self._lock:
            data = deepcopy(await self._async_tariff_data_locked())
            data.setdefault("scheduler_state", {})[schedule_id] = {
                **data.get("scheduler_state", {}).get(schedule_id, {}),
                "last_fired_period": period_key,
                "last_fired_at": fired_at,
            }
            await self._store.async_save_confirmed(data)
            self._data = data

    async def _ensure_loaded_locked(self) -> dict[str, Any]:
        """Load data while the caller holds the lock."""
        if self._data is None:
            loaded = await self._store.async_load()
            if not isinstance(loaded, dict):
                loaded = {}
            self._data = _default_state()
            self._deep_merge(self._data, loaded)
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
            **normalize_schedule_calendar(schedule),
            "id": str(existing_id or schedule.get("id") or uuid.uuid4()),
            "kind": kind,
            "cadence": cadence,
            "time": time_value,
            "recipients": recipients,
            "enabled": bool(schedule.get("enabled", True)),
        }

    @staticmethod
    def _stored_schedule_id(schedule: dict[str, Any]) -> str:
        """Stable projection for ID-less legacy rows; never persist on read."""
        if schedule.get("id"):
            return str(schedule["id"])
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
            "ha_tools_email:legacy:" + json.dumps(schedule, sort_keys=True)))

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
                clean.append(cls._clean_schedule(schedule, existing_id=cls._stored_schedule_id(schedule)))
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
