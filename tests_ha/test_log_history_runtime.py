"""Durable synthetic log stream across restart and native 24-hour coverage."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.helpers import entity_registry as er

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.composer import async_build_log_digest_payload, _presentation
from custom_components.ha_tools_email.log_history import LogHistory
from tests_ha.test_email_native import schedule
from tests_ha.test_ha_runtime import _setup
import hashlib

pytestmark = pytest.mark.asyncio
UTC = timezone.utc


async def test_restart_in_active_hour_report_and_24h_sensors(hass, hass_storage, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    bucket = hass.data[DOMAIN]
    history = bucket['log_history']
    freezer.move_to('2026-10-01T00:10:00Z')
    logging.getLogger('fixture.synthetic').warning('PRIVATE_MESSAGE_DO_NOT_PERSIST synthetic')
    freezer.move_to('2026-10-01T00:20:00Z')
    await history.async_stop()
    persisted = await history.store.async_load()
    assert 'PRIVATE_MESSAGE_DO_NOT_PERSIST' not in repr(persisted)
    assert 'message' not in repr(persisted)
    assert 'exception' not in repr(persisted)
    freezer.move_to('2026-10-01T00:25:00Z')
    history = LogHistory(hass)
    await history.async_start()
    bucket['log_history'] = history
    freezer.move_to('2026-10-01T00:40:00Z')
    logging.getLogger('fixture.synthetic').error('after restart synthetic')
    freezer.move_to('2026-10-02T00:00:00Z')
    await history.async_checkpoint()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 2, tzinfo=UTC)
    rows, coverage = await history.async_query(start, end)
    synthetic = [row for row in rows if row['name'] == 'fixture.synthetic']
    assert sum(row['count'] for row in synthetic) == 2
    assert not coverage['complete']
    for cadence, window_start in [('daily', start), ('weekly', start - timedelta(days=6))]:
        payload = await async_build_log_digest_payload(hass, storage, cadence=cadence, now=end,
            window_start=window_start, window_end=end, manual_window=True,
            presentation=_presentation('pl', 'UTC'))
        assert payload['summary']['source']['name'] == 'system_log'
        assert payload['summary']['logger_counts']['fixture.synthetic'] == 2
        assert payload['summary']['level_counts']['WARNING'] >= 1
        assert payload['summary']['level_counts']['ERROR'] >= 1
        for part in ('body', 'html'):
            assert 'Niepełne pokrycie dziennika' in payload[part]
    native = bucket['email_native']
    await native.async_refresh()
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_email_{key}_errors_24h')
    assert hass.states.get(entity_id).state == 'unknown'
    freezer.move_to('2026-10-02T00:26:00Z')
    await history.async_checkpoint()
    await native.async_refresh()
    await hass.async_block_till_done()
    assert int(hass.states.get(entity_id).state) >= 1
    assert hass.states.get(entity_id).attributes['coverage']['complete'] is True
    freezer.move_to('2026-10-02T01:25:00Z')
    logging.getLogger('fixture.synthetic').error('new full day')
    freezer.move_to('2026-10-03T01:00:00Z')
    await history.async_checkpoint()
    await native.async_refresh()
    await hass.async_block_till_done()
    assert int(hass.states.get(entity_id).state) >= 1
    assert hass.states.get(entity_id).attributes['coverage']['complete'] is True
    await history.async_stop()
    bucket.pop('log_history')


async def test_retention_prunes_older_than_33_days(hass, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    history = LogHistory(hass)
    await history.async_start()
    logging.getLogger('fixture.retention').warning('old synthetic')
    freezer.move_to('2026-10-01T01:00:00Z')
    await history.async_checkpoint()
    freezer.move_to('2026-11-04T02:00:00Z')
    await history.async_checkpoint()
    assert not history.data['hours']
    await history.async_stop()
