"""Wire/book adapter regressions; pure Python, no HA or network."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('wire_profiles_test_package')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    spec = importlib.util.spec_from_file_location(package.__name__ + '.tariff_profiles', ROOT / 'tariff_profiles.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    history = sys.modules[package.__name__ + '.tariff_history']
    engine = sys.modules[package.__name__ + '.tariff']


def profile(**changes):
    p = {'schema': 2, 'name': 'Umowa A', 'currency': 'PLN', 'priceBasis': 'gross',
         'timezone': 'Europe/Warsaw', 'validFrom': '2026-01-01', 'validTo': None,
         'defaultZone': 'day', 'zones': [{'id': 'day', 'label': 'Dzień', 'energyRate': .62, 'variableRate': .32}],
         'rules': [], 'exceptions': [],
         'fixed': [{'id': 'network', 'label': 'Sieć', 'monthly': 12.4},
                   {'id': 'subscription', 'label': 'Abonament', 'monthly': .75},
                   {'id': 'capacity', 'label': 'Moc', 'monthly': 11.62},
                   {'id': 'retail', 'label': 'Handlowa', 'monthly': 9.33}]}
    p.update(changes)
    return p


class WireProfileTests(unittest.TestCase):
    def test_fee_rows_and_custom_metadata_survive_json_and_book_validation(self):
        p = profile()
        p['fixed'].extend([{'id': 'własna/opłata', 'label': 'Własna opłata', 'monthly': 1.2,
                            'invoiceLine': {'code': 'A/7'}},
                           {'id': 'distribution', 'label': 'Druga sieć', 'monthly': .1}])
        p['customField'] = {'contract': 'synthetic'}
        before = copy.deepcopy(p)
        b = adapter.profile_to_book(p)
        v = b['tariffs'][0]['versions'][0]
        self.assertEqual(12.5, v['fixed_monthly']['distribution'])
        self.assertEqual(10.53, v['fixed_monthly']['other'])
        out = adapter.book_to_profile(history.validate_tariff_book(json.loads(json.dumps(b))))
        self.assertEqual(p['fixed'], out['fixed'])
        self.assertEqual(p['name'], out['name'])
        self.assertEqual(p['customField'], out['customField'])
        self.assertEqual(before, p)
        self.assertEqual(out, adapter.book_to_profile(adapter.profile_to_book(out, book=b)))

    def test_unknown_fee_is_unknown_but_explicit_empty_fee_list_is_zero(self):
        p = profile(fixed=[{'id': 'retail', 'label': 'Handlowa', 'monthly': None},
                           {'id': 'extra', 'label': 'Dodatek', 'monthly': 2}])
        b = adapter.profile_to_book(p)
        self.assertIsNone(b['tariffs'][0]['versions'][0]['fixed_monthly']['other'])
        self.assertEqual(p['fixed'], adapter.book_to_profile(b)['fixed'])
        b = adapter.profile_to_book(profile(fixed=[]))
        self.assertEqual(dict.fromkeys(history.FEE_KEYS, 0), b['tariffs'][0]['versions'][0]['fixed_monthly'])

    def test_book_and_profile_schema_two_are_structurally_distinct(self):
        b = adapter.profile_to_book(profile())
        self.assertFalse(adapter.is_profile_v2(b))
        self.assertTrue(adapter.is_profile_v2(profile()))
        with self.assertRaises(adapter.TariffValidationError):
            adapter.validate_profile_v2(b)
        with self.assertRaises(adapter.TariffValidationError):
            adapter.validate_profile_v2(dict(profile(), tariffs=[]))

    def test_exclusive_wire_end_does_not_gain_one_schema1_day(self):
        b = adapter.profile_to_book(profile(validTo='2026-02-01'))
        version = b['tariffs'][0]['versions'][0]
        self.assertNotIn('validTo', version['profile'])
        rates = history.HistoricalTariffRates(b['tariffs'][0])
        # Existing rate selector enforces the civil book boundaries.
        self.assertEqual('2026-02-01', adapter.book_to_profile(b)['validTo'])
        self.assertEqual('2026-02-01', version['valid_to'])
        self.assertTrue(rates.rate_at('2026-01-31T23:59:00+01:00')['available'])
        self.assertFalse(rates.rate_at('2026-02-01T00:00:00+01:00')['available'])

    def test_new_version_preserves_all_tariffs_history_and_reports_metadata(self):
        a = adapter.profile_to_book(profile())
        a['archive_report_reference'] = ['report-immutable']
        a = adapter.profile_to_book(profile(name='Alternative'), book=a, tariff_id='alt')
        before = copy.deepcopy(a)
        b = adapter.profile_to_book(profile(name='Umowa B', validFrom='2026-10-20'), book=a)
        self.assertEqual(before, a)
        self.assertEqual(before['archive_report_reference'], b['archive_report_reference'])
        self.assertEqual(before['tariffs'][1], b['tariffs'][1])
        versions = b['tariffs'][0]['versions']
        self.assertEqual(2, len(versions))
        self.assertEqual('2026-10-20', versions[0]['valid_to'])
        self.assertEqual(before['tariffs'][0]['versions'][0]['profile'], versions[0]['profile'])
        self.assertEqual('Umowa A', adapter.book_to_profile(b, version_id=versions[0]['id'])['name'])
        self.assertEqual('Umowa B', adapter.book_to_profile(b)['name'])
        _, changes = history.prepare_tariff_book_edit(a, b, datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertEqual([], changes)

    def test_historical_edit_stays_visible_to_existing_guard(self):
        a = adapter.profile_to_book(profile())
        p = adapter.book_to_profile(a)
        p['zones'][0]['energyRate'] = .77
        b = adapter.profile_to_book(p, book=a)
        _, changes = history.prepare_tariff_book_edit(a, b, datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertTrue(changes)
        self.assertEqual(a['tariffs'][0]['versions'][0]['id'], b['tariffs'][0]['versions'][0]['id'])

    def test_sidecar_never_masks_a_book_api_edit(self):
        b = adapter.profile_to_book(profile())
        v = b['tariffs'][0]['versions'][0]
        v['profile']['zones'][0]['energyRate'] = .91
        self.assertEqual(.91, adapter.book_to_profile(b)['zones'][0]['energyRate'])
        v['fixed_monthly']['other'] = 19
        with self.assertRaises(adapter.TariffValidationError) as raised:
            adapter.book_to_profile(b)
        self.assertEqual('wire_fee_projection_conflict', raised.exception.errors[0]['reason'])

    def test_schema1_fixed_monthly_becomes_single_fee_without_splitting(self):
        p = dict(profile(), schema=1, clock={'mode': 'civil'}, fixedMonthly=33)
        p.pop('fixed')
        p.pop('name')
        for combined in (False, True):
            with self.subTest(combined=combined):
                if combined:
                    p['zones'][0]['variableRate'] = None
                out = adapter.schema1_to_profile(p, legacy_combined=combined)
                self.assertEqual([{'id': 'fixed', 'label': 'opłaty stałe', 'monthly': 33}], out['fixed'])
                self.assertEqual('legacy_combined' if combined else 'components', out['rateSemantics'])
                b = adapter.profile_to_book(out)
                self.assertEqual(33, b['tariffs'][0]['versions'][0]['legacy_combined_monthly'])
                self.assertTrue(all(v is None for v in b['tariffs'][0]['versions'][0]['fixed_monthly'].values()))

    def test_unknown_legacy_start_is_preserved_not_invented(self):
        p = {'schema': 1, 'currency': 'PLN', 'priceBasis': 'gross', 'timezone': 'Europe/Warsaw',
             'clock': {'mode': 'civil'}, 'zones': [{'id': 'all', 'energyRate': 1, 'variableRate': None}],
             'defaultZone': 'all', 'fixedMonthly': None}
        out = adapter.schema1_to_profile(p, legacy_combined=True)
        self.assertIsNone(out['validFrom'])
        b = adapter.profile_to_book(out)
        self.assertIsNone(b['tariffs'][0]['versions'][0]['valid_from'])
        self.assertEqual('migrated_unknown_start', b['tariffs'][0]['versions'][0]['provenance'])
        self.assertIsNone(b['tariffs'][0]['versions'][0]['legacy_combined_monthly'])
        with self.assertRaises(adapter.TariffValidationError):
            adapter.profile_to_book(profile(validFrom=None))

    def test_templates_are_unpriced_and_g12w_uses_explicit_holiday_calendar(self):
        templates = adapter.list_tariff_templates(currency='PLN', timezone='Europe/Warsaw')
        self.assertEqual(['G11', 'G12', 'G12w'], [t['id'] for t in templates])
        for item in templates:
            self.assertTrue(all(z['energyRate'] is None and z['variableRate'] is None
                                for z in item['profile']['zones']))
            with self.assertRaises(adapter.TariffValidationError):
                adapter.validate_profile_v2(item['profile'])
        p = adapter.list_tariff_templates(currency='PLN', timezone='Europe/Warsaw',
                                          holiday_dates=['2026-11-11'])[2]['profile']
        p['validFrom'] = '2026-01-01'
        for z in p['zones']:
            z.update(energyRate=.4 if z['id'] == 'cheap' else .6, variableRate=0)
        b = adapter.profile_to_book(p)
        model = engine.EnergyTariff(b['tariffs'][0]['versions'][0]['profile'])
        for instant, zone in [('2026-10-17T12:00:00+02:00', 'cheap'),
                              ('2026-10-18T12:00:00+02:00', 'cheap'),
                              ('2026-11-11T12:00:00+01:00', 'cheap'),
                              ('2026-11-12T12:00:00+01:00', 'exp'),
                              ('2026-11-12T14:00:00+01:00', 'cheap')]:
            self.assertEqual(zone, model.rate_at(instant)['zone'])

    def test_card_migration_requires_backend_empty_and_explicit_rates_hours(self):
        opts = {'energy_tariff_mode': 'day_night', 'peak_rate': .9, 'off_peak_rate': .4,
                'energy_night_hour_start': 23, 'energy_day_hour_start': 7}
        args = dict(valid_from='2026-10-20', currency='PLN', timezone='Europe/Warsaw')
        self.assertIsNone(adapter.migrate_card_options(opts, backend_book={}, **args))
        p = adapter.migrate_card_options(opts, backend_book=None, **args)
        self.assertEqual([.4, .9], [z['energyRate'] for z in p['zones']])
        self.assertEqual([{'from': '23:00', 'to': '07:00'}], p['rules'][0]['intervals'])
        self.assertIn('uzupełnij dystrybucję', p['migrationNotes'])
        del opts['peak_rate']
        with self.assertRaises(adapter.TariffValidationError):
            adapter.migrate_card_options(opts, backend_book=None, **args)

    def test_existing_selector_rejects_ambiguity_and_uses_civil_dst_clock(self):
        p = profile(zones=[{'id': 'day', 'energyRate': 1, 'variableRate': 0},
                           {'id': 'night', 'energyRate': .5, 'variableRate': 0}],
                    rules=[{'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}])
        b = adapter.profile_to_book(p)
        model = engine.EnergyTariff(b['tariffs'][0]['versions'][0]['profile'])
        for instant in ('2026-10-25T02:30:00+02:00', '2026-10-25T02:30:00+01:00'):
            self.assertEqual('night', model.rate_at(instant)['zone'])
        p['rules'].append({'zone': 'day', 'intervals': [{'from': '22:00', 'to': '06:00'}]})
        with self.assertRaises(adapter.TariffValidationError):
            adapter.validate_profile_v2(p)

    def test_build1_fixture_rates_and_fee_rows_reach_the_existing_engine_unchanged(self):
        fixtures = Path(__file__).resolve().parent / 'fixtures/build1'
        source = json.loads((fixtures / 'fixture-energy-inputs-2026-10-17.json').read_text())
        golden = json.loads((fixtures / 'golden-energy-2026-10-17.json').read_text())
        # The golden may wrap its model; use the source fixture's tariff and
        # the existing selector for each reference measured hour.
        model = golden.get('model', golden)
        b = adapter.profile_to_book(source['tariff'])
        rates = engine.EnergyTariff(b['tariffs'][0]['versions'][0]['profile'])
        self.assertEqual(source['tariff']['fixed'], adapter.book_to_profile(b)['fixed'])
        for hour in model['hourly']:
            start = datetime.fromisoformat(hour['start'])
            cost = rates.price_bucket({'from': start.isoformat(),
                                       'to': (start + timedelta(hours=1)).isoformat(),
                                       'kwh': hour['kwh']})
            self.assertTrue(cost['available'])
            self.assertEqual(hour['zone'], cost['zone'])
            self.assertAlmostEqual(hour['energy_cost'], cost['energyCost'])
            self.assertAlmostEqual(hour['variable_cost'], cost['variableCost'])

    def test_migrated_fixed_clock_validity_retains_existing_instants(self):
        p = {'schema': 1, 'currency': 'PLN', 'priceBasis': 'gross', 'timezone': 'Europe/Warsaw',
             'clock': {'mode': 'fixed', 'offsetMinutes': 0}, 'validFrom': '2026-01-01',
             'validTo': '2026-01-01', 'zones': [{'id': 'all', 'energyRate': 1, 'variableRate': 0}],
             'defaultZone': 'all', 'fixedMonthly': 0}
        original = history.migrate_tariff_v1(p, legacy_combined=False)
        wire = adapter.book_to_profile(original)
        restored = adapter.profile_to_book(wire, book=original)
        self.assertEqual(p, restored['source_schema1'])
        old = history.HistoricalTariffRates(original['tariffs'][0])
        new = history.HistoricalTariffRates(restored['tariffs'][0])
        for instant in ('2025-12-31T23:30:00Z', '2026-01-01T00:30:00Z',
                        '2026-01-01T23:30:00Z', '2026-01-02T00:00:00Z'):
            self.assertEqual(old.rate_at(instant), new.rate_at(instant))

    def test_bounded_input_and_duplicate_fee_ids_are_rejected(self):
        p = profile()
        p['fixed'].append(copy.deepcopy(p['fixed'][0]))
        with self.assertRaises(adapter.TariffValidationError):
            adapter.validate_profile_v2(p)
        cycle = profile()
        cycle['cyclic'] = cycle
        with self.assertRaises(adapter.TariffValidationError):
            adapter.validate_profile_v2(cycle)


if __name__ == '__main__':
    unittest.main()
