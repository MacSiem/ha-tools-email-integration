"""Rates78: additive exact public usage rates from the actual producer/helper.

Owner executes RED/GREEN through FIFO. No replacement pricing or test classes
imported into discovery; source IO uses existing measured-hour fixtures.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import unittest
from unittest.mock import patch

from tests import test_energy_analysis as support
from tests import test_tariff_history as tariff_support

analysis = support.analysis


class Rates78PublicTests(unittest.TestCase):
    def build(self, versions=None, *, start='2026-01-01T00:00:00+00:00',
              end='2026-01-01T02:00:00+00:00'):
        book = tariff_support.book(versions)
        period = {'start':start,'end':end,'timezone':'UTC','kind':'day'}
        builder = analysis.EnergyAnalysisAccumulator(book,period,
            {'entry_id':'rates78','tariff_revision':4,'registry_revision':2},
            generated_at=datetime.fromisoformat(end))
        builder.add_chunk(support.readings(start,end,sources=('sensor.grid',)),start,end)
        return book,builder,builder.finish()

    def version(self, energy=0.123456789, variable=0.2, **kwargs):
        version=tariff_support.version(**kwargs)
        version['profile']['zones'][0].update(energyRate=energy,variableRate=variable)
        return version

    def test_actual_producer_exact_combined_rate_without_cent_rounding(self):
        version=self.version();before=deepcopy(version)
        _,_,result=self.build([version])
        rate=result['rates'][0]
        self.assertEqual([{'zone_id':'day','usage_rate':'0.323456789'}],rate['zone_usage_rates'])
        self.assertEqual(0.123456789,rate['profile']['zones'][0]['energyRate'])
        self.assertEqual(0.2,rate['profile']['zones'][0]['variableRate'])
        self.assertEqual(before,version)
        json.dumps(result,allow_nan=False)

    def test_public_projection_uses_existing_unrounded_helper_with_one_kwh(self):
        version=self.version();_,_,result=self.build([version])
        public_input={**version,'version_id':version['id'],'currency':'EUR','price_basis':'gross',
                      'clock':{'mode':'civil'}}
        original=analysis.calculate_energy_amounts
        with patch.object(analysis,'calculate_energy_amounts',wraps=original) as helper:
            rows=analysis._public_rates([public_input])
        helper.assert_called_once_with(1,0.123456789,0.2,'components')
        self.assertEqual(result['rates'][0]['zone_usage_rates'],rows[0]['zone_usage_rates'])

    def test_string_precision_and_unknown_components_follow_helper_contract(self):
        # Settings remain JSON numbers. Direct internal projection also accepts
        # the helper's exact decimal strings; this does not relax book validation.
        version=self.version();base={**version,'version_id':'a','currency':'EUR',
            'price_basis':'gross','clock':{'mode':'civil'}}
        before=deepcopy(base)
        base['profile']['zones']=[
            {'id':'precise','energyRate':'0.123456789123456789','variableRate':'0.2'},
            {'id':'unknown_variable','energyRate':'0.1','variableRate':None},
            {'id':'unknown_energy','energyRate':None,'variableRate':'0.2'},
            {'id':'zero','energyRate':'0','variableRate':'0'}]
        original=deepcopy(base)
        projected=analysis._public_rates([base])[0]
        self.assertEqual([{'zone_id':'precise','usage_rate':'0.323456789123456789'},
            {'zone_id':'unknown_variable','usage_rate':None},
            {'zone_id':'unknown_energy','usage_rate':None},
            {'zone_id':'zero','usage_rate':'0'}],projected['zone_usage_rates'])
        self.assertEqual(original,base)
        self.assertEqual(before['rate_semantics'],projected['rate_semantics'])

    def test_actual_unknown_variable_and_zero_rates_are_not_conflated(self):
        for variable,expected in [(None,None),(0,'0')]:
            with self.subTest(variable=variable):
                _,_,result=self.build([self.version(energy=0,variable=variable)])
                self.assertEqual([{'zone_id':'day','usage_rate':expected}],result['rates'][0]['zone_usage_rates'])
                self.assertEqual(variable,result['rates'][0]['profile']['zones'][0]['variableRate'])

    def test_legacy_combined_energy_is_already_combined_variable_zero_or_null(self):
        for variable in (0,None):
            with self.subTest(variable=variable):
                version=self.version(energy=0.123456789,variable=variable,rate_semantics='legacy_combined')
                _,_,result=self.build([version])
                self.assertEqual([{'zone_id':'day','usage_rate':'0.123456789'}],result['rates'][0]['zone_usage_rates'])
                self.assertEqual('legacy_combined',result['rates'][0]['rate_semantics'])
                self.assertEqual(variable,result['rates'][0]['profile']['zones'][0]['variableRate'])

    def test_negative_rate_requires_existing_profile_opt_in_and_preserves_sign(self):
        version=self.version(energy=-0.123456789,variable=0.02)
        with self.assertRaises(ValueError):self.build([version])
        version['profile']['allowNegativeEnergy']=True
        _,_,result=self.build([version])
        self.assertEqual([{'zone_id':'day','usage_rate':'-0.103456789'}],result['rates'][0]['zone_usage_rates'])
        self.assertIs(True,result['rates'][0]['profile']['allowNegativeEnergy'])

    def test_each_zone_uses_its_own_components_and_order_without_mutating_profile(self):
        version=self.version()
        version['profile']['zones'].append({'id':'night','energyRate':0.01,'variableRate':0.002})
        before=deepcopy(version)
        _,_,result=self.build([version])
        self.assertEqual([{'zone_id':'day','usage_rate':'0.323456789'},
                          {'zone_id':'night','usage_rate':'0.012'}],result['rates'][0]['zone_usage_rates'])
        self.assertEqual(before,version)

    def test_currency_basis_and_versions_remain_independent_when_bill_cannot_combine(self):
        first=self.version(start='2026-01-01',version_id='old',valid_to='2026-01-02')
        second=self.version(energy=0.4,variable=0.05,start='2026-01-02',version_id='new')
        second['profile'].update(currency='PLN',priceBasis='net')
        _,_,result=self.build([first,second],start='2026-01-01T23:00:00+00:00',end='2026-01-02T01:00:00+00:00')
        self.assertIsNone(result['totals']['total_cost'])
        self.assertEqual([('old','EUR','gross','0.323456789'),('new','PLN','net','0.45')],
            [(row['version_id'],row['currency'],row['price_basis'],row['zone_usage_rates'][0]['usage_rate'])
             for row in result['rates']])

    def test_dynamic_profile_does_not_publish_fallback_zone_as_instantaneous_rate(self):
        version=self.version(energy=99,variable=7)
        intervals=[{'from':'2026-01-01T00:00:00+00:00','to':'2026-01-01T01:00:00+00:00',
                    'energyRate':0.123456789,'variableRate':0.2},
                   {'from':'2026-01-01T01:00:00+00:00','to':'2026-01-01T02:00:00+00:00',
                    'energyRate':0.5,'variableRate':None}]
        version['profile']['dynamic']={'intervals':deepcopy(intervals)}
        _,_,result=self.build([version])
        rate=result['rates'][0]
        self.assertEqual([{'zone_id':'day','usage_rate':None}],rate['zone_usage_rates'])
        self.assertEqual(intervals,rate['profile']['dynamic']['intervals'])
        self.assertEqual(99,rate['profile']['zones'][0]['energyRate'])
        self.assertEqual(7,rate['profile']['zones'][0]['variableRate'])
        self.assertEqual('partial',result['pricing_status'])

    def test_additive_metadata_is_detached_and_included_in_deterministic_snapshot_id(self):
        version=self.version();_,builder,result=self.build([version]);original=deepcopy(result)
        _,_,same=self.build([deepcopy(version)])
        self.assertEqual(result['snapshot_id'],same['snapshot_id'])
        unsigned=deepcopy(result);unsigned.pop('snapshot_id')
        self.assertEqual(hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':'),
            allow_nan=False).encode('utf-8')).hexdigest(),result['snapshot_id'])
        result['rates'][0]['zone_usage_rates'][0]['usage_rate']='tampered'
        result['rates'][0]['profile']['zones'][0]['energyRate']=123
        self.assertEqual(original,builder.finish())
        changed=self.version(variable=0.3);_,_,different=self.build([changed])
        self.assertNotEqual(original['snapshot_id'],different['snapshot_id'])
        self.assertEqual('0.423456789',different['rates'][0]['zone_usage_rates'][0]['usage_rate'])
