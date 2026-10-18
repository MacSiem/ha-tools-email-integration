"""Mail67 native HA auth/Recorder and dispatcher seam; owner executes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import async_add_external_statistics
from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.report_window import resolve_report_window
from custom_components.ha_tools_email.scheduler import async_send_report
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile

pytestmark=pytest.mark.asyncio


@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_mail67_native_prepared_facts_dispatch_and_unload(hass,hass_storage,
        recorder_mock,hass_admin_user):
    await hass.config.async_set_time_zone('UTC')
    manager=await async_get_manager(hass)
    manager.data={'energy_sources':[{'type':'grid','flow_from':[{'stat_energy_from':'mail67:grid'}],'flow_to':[]}],
                  'device_consumption':[]}
    metadata={'source':'mail67','statistic_id':'mail67:grid','name':'Synthetic mail67',
              'has_mean':False,'has_sum':True,'unit_of_measurement':'kWh'}
    if hasattr(models,'StatisticMeanType'):
        metadata.update(mean_type=models.StatisticMeanType.NONE,unit_class='energy')
    start=datetime(2026,10,17,11,tzinfo=timezone.utc)
    async_add_external_statistics(hass,metadata,[{'start':start+timedelta(hours=i),'sum':float(i)} for i in range(26)])
    await recorder_mock.async_block_till_done()
    entry,storage=await _setup(hass)
    await storage.async_save_tariff(profile(),expected_revision=0)
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(),legacy_combined=False),expected_revision=1)
    bucket=hass.data[DOMAIN];service=bucket['energy_analysis']
    now=datetime(2026,10,18,12,30,tzinfo=timezone.utc)
    request=resolve_report_window('daily',now,'UTC')
    read=await service.async_prepare_report(request,hass_admin_user.id,purpose='report_read',
        language='en',timezone_name='UTC',hour_cycle='24')
    assert Decimal(read['summary']['total_kwh']) == 24
    assert read['period']['hours'] == 24
    assert read['snapshot_id'] in read['csv'] and read['snapshot_id'] in read['html']
    sent=[]
    def send(*args):sent.append(args)
    async def config():return {'default_recipient':'synthetic@example.invalid'}
    bucket.update(send_email=send,load_config=config)
    with patch('custom_components.ha_tools_email.composer.async_get_energy_usage',
            side_effect=AssertionError('old energy engine')):
        result=await async_send_report(hass,storage,kind='energy_report',cadence='daily',
            recipients=[],now=now,actor=hass_admin_user.id)
    assert result['status'] == 'accepted_by_smtp' and len(sent) == 1
    assert '24' in sent[0][4] and '24' in sent[0][5]
    service.async_stop()
    assert not await service.async_validate_result(read,hass_admin_user.id)
    with pytest.raises(RuntimeError):
        await service.async_prepare_report(request,hass_admin_user.id,purpose='report_execute',
            language='en',timezone_name='UTC',hour_cycle='24')
    assert len(sent) == 1
