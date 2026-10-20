"""Private, bounded metadata for logical SMTP dispatch attempts.

No delivery claim, retry, content export, filesystem access, or exception text.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

HISTORY_SCHEMA = 1
HISTORY_LIMIT = 128
HISTORY_DAYS = 90
_KINDS = frozenset({'manual', 'test', 'log_digest', 'energy_report'})
_CADENCES = frozenset({'daily', 'weekly', 'monthly'})
_OUTCOMES = frozenset({'accepted_by_smtp', 'accepted_by_notify', 'preparation_failed', 'authentication_failed',
                       'recipients_refused', 'partial_acceptance', 'tls_failed',
                       'transport_outcome_unknown', 'send_failed', 'cancelled_outcome_unknown', 'outcome_unknown'})
_PERIOD = re.compile(r'^(daily:\d{4}-\d{2}-\d{2}|weekly:\d{4}-W\d{2}|monthly:\d{4}-\d{2})$')


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None
    except ValueError:
        return None


def clean_attempt(value: Any) -> dict[str, Any] | None:
    """Allowlist even persisted metadata; never expose arbitrary stored text."""
    if not isinstance(value, dict):
        return None
    timestamp = _stamp(value.get('timestamp'))
    try:
        attempt_id = str(uuid.UUID(value.get('id')))
    except (ValueError, TypeError, AttributeError):
        return None
    if timestamp is None:
        return None
    period = value.get('period')
    ref = value.get('schedule_ref')
    count = value.get('recipient_count')
    return {
        'id': attempt_id,
        'timestamp': timestamp.isoformat(),
        'source': value.get('source') if value.get('source') in ('manual', 'schedule') else 'unknown',
        'kind': value.get('kind') if isinstance(value.get('kind'), str) and value.get('kind') in _KINDS else None,
        'cadence': value.get('cadence') if isinstance(value.get('cadence'), str) and value.get('cadence') in _CADENCES else None,
        'period': period if isinstance(period, str) and _PERIOD.fullmatch(period) else None,
        'schedule_ref': ref if isinstance(ref, str) and re.fullmatch(r'[0-9a-f]{64}', ref) else None,
        'recipient_count': count if type(count) is int and 0 <= count <= 100000 else None,
        'outcome': value.get('outcome') if isinstance(value.get('outcome'), str) and value.get('outcome') in _OUTCOMES else 'outcome_unknown',
    }


def retained_attempts(section: Any, now: datetime) -> list[dict[str, Any]]:
    """Lazy schema-0 migration; future schemas remain untouched and unavailable."""
    if section is None:
        section = {}
    if not isinstance(section, dict) or type(section.get('schema', 0)) is not int or section.get('schema', 0) not in (0, HISTORY_SCHEMA):
        raise ValueError('history_schema_unavailable')
    entries = section.get('attempts', [])
    if not isinstance(entries, list):
        entries = []
    cutoff = now.astimezone(timezone.utc) - timedelta(days=HISTORY_DAYS)
    clean = []
    seen = set()
    for raw in entries:
        entry = clean_attempt(raw)
        if entry is None or entry['id'] in seen:
            continue
        if cutoff <= _stamp(entry['timestamp']) <= now.astimezone(timezone.utc):
            seen.add(entry['id'])
            clean.append(entry)
    # Persist insertion order; the last 128 completed dispatches win.
    return clean[-HISTORY_LIMIT:]


class PartialSMTPAcceptance(ValueError):
    """Some recipients may already have been accepted; never implicitly retry."""


def failure_outcome(error: BaseException, *, prepared: bool) -> str:
    if not prepared:
        return 'preparation_failed'
    if isinstance(error, PartialSMTPAcceptance):
        return 'partial_acceptance'
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'authentication_failed'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'recipients_refused'
    if isinstance(error, ssl.SSLError):
        return 'tls_failed'
    if isinstance(error, asyncio.CancelledError):
        return 'cancelled_outcome_unknown'
    if isinstance(error, (OSError, smtplib.SMTPServerDisconnected)):
        return 'transport_outcome_unknown'
    return 'send_failed'


async def async_dispatch_attempt(hass, storage, send, prepare, *, source='manual',
                                 kind='manual', cadence=None, period=None,
                                 schedule_id=None, now=None) -> dict[str, Any]:
    """Exactly one logical record around the shared actual executor send path.

    Store failure never escapes a successful SMTP handoff. The original transport
    exception still escapes unchanged. Cancellation during executor work has an
    unknown outcome; we neither retry nor claim rejection/delivery.
    """
    now = now or datetime.now(timezone.utc)
    attempt = {
        'id': str(uuid.uuid4()), 'timestamp': now.isoformat(), 'source': source,
        'kind': kind, 'cadence': cadence, 'period': period,
        'schedule_ref': hashlib.sha256(str(schedule_id).encode()).hexdigest() if schedule_id is not None else None,
        'recipient_count': None, 'outcome': 'preparation_failed',
    }
    prepared = False
    history_status = 'unavailable'
    try:
        cfg, to, subject, body, html = await prepare()
        attempt['recipient_count'] = len([r for r in to.split(',') if r.strip() and '@' in r.strip()])
        prepared = True
        await hass.async_add_executor_job(send, hass, cfg, to, subject, body, html)
        attempt['outcome'] = 'accepted_by_smtp'
    except (Exception, asyncio.CancelledError) as error:
        attempt['outcome'] = failure_outcome(error, prepared=prepared)
        raise
    finally:
        try:
            await storage.async_append_attempt(attempt, now=now)
            history_status = 'recorded'
        except Exception:
            # Never log exception text, addresses, content, or Store paths.
            pass
    return {'ok': True, 'status': 'accepted_by_smtp',
            'attempt_id': attempt['id'], 'history_status': history_status}


async def async_dispatch_channel_attempt(storage, prepare, handoff, *, source='manual',
        kind='energy_report', cadence=None, period=None, schedule_id=None, now=None):
    """One awaited native service handoff, preserving SMTP's no-retry lifecycle.

    A completed HA service call is acceptance, never evidence of a phone delivery.
    prepare includes authorization and final context fences before handoff.
    """
    now = now or datetime.now(timezone.utc)
    attempt = {'id': str(uuid.uuid4()), 'timestamp': now.isoformat(), 'source': source,
        'kind': kind, 'cadence': cadence, 'period': period,
        'schedule_ref': hashlib.sha256(str(schedule_id).encode()).hexdigest() if schedule_id is not None else None,
        'recipient_count': None, 'outcome': 'preparation_failed'}
    prepared, history_status = False, 'unavailable'
    try:
        payload = await prepare()
        prepared = True
        await handoff(payload)
        attempt['outcome'] = 'accepted_by_notify'
    except (Exception, asyncio.CancelledError) as error:
        attempt['outcome'] = failure_outcome(error, prepared=prepared)
        raise
    finally:
        try:
            await storage.async_append_attempt(attempt, now=now)
            history_status = 'recorded'
        except Exception:
            pass
    return {'ok': True, 'status': 'accepted_by_notify',
        'attempt_id': attempt['id'], 'history_status': history_status}
