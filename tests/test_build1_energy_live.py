"""Future transitions must follow effective versions, UTC instants and engine rules."""
import copy
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests.test_tariff_history import book, profile, version

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('build1_live_fixture')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    spec = importlib.util.spec_from_file_location(package.__name__ + '.energy_live', ROOT / 'energy_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)


def at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def scheduled(*, rules=None, exceptions=None):
    return profile(currency='PLN', timezone='Europe/Warsaw', defaultZone='exp', zones=[
        {'id': 'cheap', 'label': 'tania', 'energyRate': .4, 'variableRate': .22, 'classification': 'cheap'},
        {'id': 'exp', 'label': 'droga', 'energyRate': .62, 'variableRate': .32, 'classification': 'expensive'}],
        rules=rules if rules is not None else [{'zone': 'cheap', 'priority': 10, 'intervals': [
            {'from': '00:00', 'to': '06:00'}, {'from': '13:00', 'to': '15:00'}, {'from': '22:00', 'to': '24:00'}]}],
        exceptions=exceptions or [])


class Build1LiveTests(unittest.TestCase):
    def test_current_cheap_finds_exit_not_next_cheap_entry(self):
        b = book([version(profile=scheduled())])
        before = copy.deepcopy(b)
        result = live.build_current(b, now=at('2026-10-17T14:17:12+02:00'))
        self.assertEqual(before, b)
        self.assertEqual('tania', result['label'])
        self.assertEqual(.62, result['rate'])
        self.assertEqual('2026-10-17T15:00:00+02:00', result['next_change'])
        self.assertEqual('droga', result['next_label'])
        self.assertEqual(.94, result['next_rate'])
        self.assertEqual('known', result['next_status'])
        self.assertEqual('2026-10-17T14:17:12+02:00', result['as_of'])
        self.assertTrue(result['next_available'])
        self.assertNotIn('cheap_share', result)
        self.assertNotIn('cheap_kwh', result)

    def test_arbitrary_zone_identity_changes_even_if_rate_stays_same(self):
        p = scheduled()
        p['zones'][0].update(energyRate=.62, variableRate=.32)
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T12:59:59+02:00'))
        self.assertEqual('2026-10-17T13:00:00+02:00', result['next_change'])
        self.assertEqual(result['rate'], result['next_rate'])
        self.assertEqual('cheap', result['next_zone_id'])

    def test_exception_overrides_regular_rule_and_identical_midnight_is_skipped(self):
        p = scheduled(exceptions=[{'zone': 'cheap', 'priority': 20, 'dates': ['2026-10-17'],
            'intervals': [{'from': '00:00', 'to': '24:00'}]}])
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T23:30:00+02:00'))
        self.assertEqual('2026-10-18T06:00:00+02:00', result['next_change'])

    def test_effective_version_change_is_detected_with_same_zone_id(self):
        old = version(valid_to='2026-10-18', rate=.1234567)
        future = version('2026-10-18', .2345678, 'new')
        result = live.build_current(book([old, future]), now=at('2026-10-17T23:59:30Z'))
        self.assertEqual(.1234567, result['rate'])
        self.assertEqual('2026-10-18T00:00:00+00:00', result['next_change'])
        self.assertEqual(.2345678, result['next_rate'])
        self.assertEqual('day', result['zone_id'])
        self.assertEqual('day', result['next_zone_id'])

    def test_identical_future_version_does_not_report_fake_change(self):
        result = live.build_current(book([version(valid_to='2026-10-18'), version('2026-10-18', 1, 'new')]),
            now=at('2026-10-17T23:59:30Z'), horizon=timedelta(hours=1))
        self.assertIsNone(result['next_change'])
        self.assertEqual('not_found_in_horizon', result['next_status'])

    def test_availability_gap_is_a_transition_and_initial_gap_can_find_recovery(self):
        b = book([version(valid_to='2026-10-18')])
        before = live.build_current(b, now=at('2026-10-17T23:59:30Z'))
        self.assertEqual('2026-10-18T00:00:00+00:00', before['next_change'])
        self.assertFalse(before['next_available'])
        self.assertIsNone(before['next_rate'])
        self.assertEqual('unknown_historical_version', before['next_reason'])
        gap = live.build_current(book([version('2026-10-19',2,'new')]), now=at('2026-10-18T12:00:00Z'))
        self.assertFalse(gap['available'])
        self.assertIsNone(gap['rate'])
        self.assertTrue(gap['next_available'])
        self.assertEqual('2026-10-19T00:00:00+00:00', gap['next_change'])

    def test_dynamic_millisecond_boundary_not_rounded_up_to_minute(self):
        edge = '2026-10-17T13:00:00.123Z'
        p = profile(dynamic={'intervals': [
            {'from': '2026-10-17T00:00:00Z', 'to': edge, 'energyRate': .3, 'variableRate': .2},
            {'from': edge, 'to': '2026-10-18T00:00:00Z', 'energyRate': .5, 'variableRate': .2}]})
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T13:00:00.100Z'))
        self.assertEqual('2026-10-17T13:00:00.123000+00:00', result['next_change'])
        self.assertEqual(.7, result['next_rate'])

    def test_fall_fold_can_change_at_clock_rollback_before_repeated_interval(self):
        p = scheduled(rules=[{'zone': 'cheap', 'priority': 10,
            'intervals': [{'from': '02:30', 'to': '03:00'}]}])
        now = datetime(2026, 10, 25, 2, 45, tzinfo=ZoneInfo('Europe/Warsaw'), fold=0)
        result = live.build_current(book([version(profile=p)]), now=now)
        self.assertEqual('tania', result['label'])
        self.assertEqual('2026-10-25T02:00:00+01:00', result['next_change'])
        self.assertEqual('droga', result['next_label'])
        self.assertGreater(at(result['next_change']).timestamp(), now.timestamp())

    def test_spring_gap_skips_nonexistent_local_02_hour(self):
        p = scheduled(rules=[{'zone': 'cheap', 'priority': 10,
            'intervals': [{'from': '02:00', 'to': '04:00'}]}])
        result = live.build_current(book([version(profile=p)]), now=at('2026-03-29T01:59:30+01:00'))
        self.assertEqual('2026-03-29T03:00:00+02:00', result['next_change'])
        self.assertEqual('tania', result['next_label'])

    def test_fixed_clock_uses_engine_clock_without_reinterpreting_as_civil(self):
        p = scheduled()
        p['clock'] = {'mode': 'fixed', 'offsetMinutes': 0}
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T14:17:00+02:00'))
        self.assertEqual('droga', result['label'])
        self.assertEqual('2026-10-17T15:00:00+02:00', result['next_change'])
        self.assertEqual('tania', result['next_label'])
        self.assertEqual({'mode': 'fixed', 'offsetMinutes': 0}, result['clock'])

    def test_search_horizon_is_elapsed_time_across_dst(self):
        p = profile(timezone='Europe/Warsaw')
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-25T01:30:00+02:00'), horizon=timedelta(hours=2))
        self.assertEqual('2026-10-25T02:30:00+01:00', result['searched_until'])
        self.assertEqual('not_found_in_horizon', result['next_status'])

    def test_negative_energy_rate_and_decimal_precision_are_preserved(self):
        p = profile(rate=-.1234567, allowNegativeEnergy=True)
        p['zones'][0]['variableRate'] = .01
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T12:00:00Z'), horizon=timedelta(minutes=1))
        self.assertEqual(-.1134567, result['rate'])
        self.assertIsNone(result['classification'])

    def test_legacy_combined_does_not_add_variable_component_again(self):
        v = version(rate=.72, rate_semantics='legacy_combined')
        v['profile']['zones'][0]['variableRate'] = None
        result = live.build_current(book([v]), now=at('2026-10-17T12:00:00Z'), horizon=timedelta(minutes=1))
        self.assertEqual(.72, result['rate'])
        self.assertEqual('legacy_combined', result['rate_semantics'])

    def test_unknown_variable_rate_is_null_not_zero(self):
        p = profile()
        p['zones'][0]['variableRate'] = None
        result = live.build_current(book([version(profile=p)]), now=at('2026-10-17T12:00:00Z'), horizon=timedelta(minutes=1))
        self.assertTrue(result['available'])
        self.assertIsNone(result['rate'])
        self.assertEqual('usage_rate_unknown', result['reason'])
        self.assertIsNone(result['variable_rate'])

    def test_bounded_horizon_no_change_is_not_a_claim_of_never(self):
        result = live.build_current(book([version()]), now=at('2026-10-17T12:00:00Z'), horizon=timedelta(minutes=2))
        self.assertEqual('not_found_in_horizon', result['next_status'])
        self.assertEqual('2026-10-17T12:02:00+00:00', result['searched_until'])
        self.assertIsNone(result['next_change'])
        for horizon in (timedelta(0), timedelta(days=9), timedelta(microseconds=1)):
            with self.assertRaises(ValueError):
                live.build_current(book(), now=at('2026-10-17T12:00:00Z'), horizon=horizon)
        with self.assertRaisesRegex(ValueError, 'aware_now_required'):
            live.build_current(book(), now=datetime(2026, 10, 17))

    def test_invalid_unconfigured_unselected_are_explicit_and_allowlisted(self):
        no_active = book(); no_active.pop('active_tariff_id')
        for b, reason in [(None, 'tariff_not_configured'), ({'schema': 0}, 'invalid_tariff_book'),
                          (no_active, 'no_active_tariff')]:
            result = live.build_current(b, now=at('2026-10-17T12:00:00Z'))
            self.assertEqual(reason, result['reason'])
            self.assertEqual('unavailable', result['next_status'])
            self.assertIsNone(result['next_change'])
            self.assertIsNone(result['searched_until'])
        b = book([version()]); b['private_secret'] = 'must_not_leak'
        result = live.build_current(b, now=at('2026-10-17T12:00:00Z'), horizon=timedelta(minutes=1))
        self.assertNotIn('must_not_leak', repr(result))
        self.assertNotIn('versions', result)
        self.assertNotIn('profile', result)
        self.assertNotIn('fixed_monthly', result)
