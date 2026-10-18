"""Mean group cents must reconcile with the same eight household price masks."""
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import unittest
from tests import test_energy_analysis_service as fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import tariff, version

mean = importlib.import_module(fixtures.service.__package__ + '.energy_group_mean7')
house = importlib.import_module(fixtures.service.__package__ + '.energy_comparisons')


class MeanGroupCentsTest(unittest.TestCase):
    def fixture(self, *, negative=False, holes=None, rates=None, values=None, extra_consumer_hole=False):
        values = values if values is not None else [.00605, *([.0002125]*6), 0]
        ids=['sensor.a','sensor.b','sensor.c']
        membership=fixtures.registry_model.resolve_membership(ids, {
            'entities': {sid:{'area_id':sid[-1]} for sid in ids},
            'areas': {sid[-1]:{'floor_id':'floor'} for sid in ids},
            'floors': {'floor':{'name':'Floor'}}}, revision=2,captured_at='2026-01-10T00:00Z')
        periods=[{'start':f'2026-01-{day:02d}T00:00Z','end':f'2026-01-{day+1:02d}T00:00Z'} for day in [8,*range(1,8)]]
        projections=[]; grids=[]
        for i,(period,value) in enumerate(zip(periods,values)):
            grid=readings(**period,sources=('sensor.grid',),value=float(Decimal(str(value))*3),
                missing=tuple(('sensor.grid',h) for h in (holes or {}).get(i,())))
            gaps=[(sid,h) for sid in ids for h in (holes or {}).get(i,())]
            if extra_consumer_hole and i==7:gaps.append(('sensor.a',1))
            consumer=readings(**period,sources=tuple(ids),value=value,missing=gaps)
            accumulator=fixtures.service.EnergyAnalysisAccumulator(None,{**period,'kind':'day','timezone':'UTC'},
                {'registry_revision':2},generated_at=datetime(2026,1,10,tzinfo=timezone.utc),
                consumers=[{'stat_consumption':sid} for sid in ids],membership=membership)
            projections.append(accumulator.add_chunk(grid,period['start'],period['end'],
                consumer_snapshot=consumer,include_group_hours=True));grids.append(grid)
        if rates is None:
            v=version(rate=-1 if negative else 1)
            if negative:v['profile']['allowNegativeEnergy']=True
            rates=tariff([v])
        compared=house.compare_daily_mean7(grids[0],grids[1:],periods[0],periods[1:],timezone_name='UTC',tariff=rates)
        return projections,periods,rates,compared

    def compare(self,data):
        projections,periods,rates,compared=data
        return mean.compare_daily_group_mean7(projections,periods,timezone_name='UTC',tariff=rates,
            household_comparison=compared)

    def assert_cost_partitions(self,result,household):
        self.assertTrue(result['reconciles_household_delta'])
        self.assertTrue(result['reconciles_household_cost_delta'])
        for side in ('current','baseline'):
            target=Decimal(household['costs'][side]['usage_cost'])
            for level,extra in [('devices',()),('areas',('unassigned',)),('floors',())]:
                rows=[*result[level].values(),result['unmeasured'],*[result[k] for k in extra]]
                self.assertEqual(target,sum((Decimal(row['costs'][side]['usage_cost']) for row in rows),Decimal(0)))
            for row in result['devices'].values():self.assertNotIn('fixed_cost',row['costs'][side])
        target=Decimal(household['costs']['delta_cost'])
        self.assertEqual(target,sum((Decimal(row['delta_usage_cost']) for row in result['areas'].values()),
            Decimal(result['unmeasured']['delta_usage_cost'])+Decimal(result['unassigned']['delta_usage_cost'])))

    def test_full_mean_raw_component_average_and_all_partitions_match_household_cents(self):
        data=self.fixture();result=self.compare(data);self.assert_cost_partitions(result,data[3])
        self.assertEqual(('0.44','0.01','0.43'),tuple(data[3]['costs'][key]['usage_cost'] for key in ('current','baseline'))+(data[3]['costs']['delta_cost'],))
        self.assertEqual(['0.15','0.15','0.14'],[row['costs']['current']['usage_cost'] for row in result['devices'].values()])
        self.assertEqual(['0.01','0.00','0.00'],[row['costs']['baseline']['usage_cost'] for row in result['devices'].values()])

    def test_partial_mean_same_eight_price_masks_reconciles_after_rounding(self):
        data=self.fixture(holes={i:{0} for i in range(8)})
        result=self.compare(data);self.assert_cost_partitions(result,data[3])
        self.assertEqual('common_measurements',data[3]['comparison_scope'])
        self.assertEqual('common_measurements',result['areas']['a']['comparison_scope'])

    def test_negative_prices_mean_cents_remain_exact(self):
        data=self.fixture(negative=True);result=self.compare(data);self.assert_cost_partitions(result,data[3])
        self.assertEqual('-0.43',data[3]['costs']['delta_cost'])

    def test_currency_or_missing_variable_keeps_energy_but_no_costproof(self):
        for mode in ('currency','missing'):
            old=version();new=version(start='2026-01-08',version_id='new')
            if mode=='currency':new['profile']['currency']='PLN'
            else:new['profile']['zones'][0].pop('variableRate')
            with self.subTest(mode=mode):
                data=self.fixture(rates=tariff([old,new]));result=self.compare(data)
                self.assertTrue(result['reconciles_household_delta'])
                self.assertFalse(result['reconciles_household_cost_delta'])
                self.assertIsNone(result['areas']['a']['delta_usage_cost'])

    def test_unknown_price_outside_selected_mask_does_not_block_structural_empty_group(self):
        # Every chosen mask excludes hour00, exactly the only unpriced hour.
        v=version()
        v['profile']['dynamic']={'intervals':[{'from':'2026-01-01T01:00Z',
            'to':'2026-01-09T00:00Z','energyRate':1,'variableRate':0}]}
        data=self.fixture(holes={i:{0} for i in range(8)},rates=tariff([v]))
        result=self.compare(data);self.assert_cost_partitions(result,data[3])
        self.assertEqual('0.00',result['unassigned']['costs']['current']['usage_cost'])

    def test_different_group_mask_cannot_claim_cost_reconciliation(self):
        data=self.fixture(holes={i:{0} for i in range(8)},extra_consumer_hole=True)
        result=self.compare(data)
        self.assertFalse(result['reconciles_household_delta'])
        self.assertFalse(result['reconciles_household_cost_delta'])

    def test_dst_baseline_mean_fractional_price_seconds_survive_structural_zero(self):
        from tests import test_group_mean7_edges as edges
        projections,periods,grids,zone,rates=edges.GroupMeanSevenEdgesTest().fixture(
            zone='Europe/Warsaw',anchor='2026-10-26')
        compared=house.compare_daily_mean7(grids[0],grids[1:],periods[0],periods[1:],
            timezone_name=zone,tariff=rates)
        result=mean.compare_daily_group_mean7(projections,periods,timezone_name=zone,
            tariff=rates,household_comparison=compared)
        self.assert_cost_partitions(result,compared)
        # One25h plus six24h baselines: 169h/7, no floor-to-whole-seconds.
        self.assertEqual(169*3600/7,compared['costs']['baseline']['mask_source_seconds'])
        self.assertEqual(compared['costs']['baseline']['mask_source_seconds'],
            result['unassigned']['costs']['baseline']['mask_source_seconds'])

    def test_mixed_legacy_baseline_preserves_full_unknown_and_known_components(self):
        versions=[version(rate_semantics='legacy_combined'),
            version(start='2026-01-04',version_id='components'),
            version(start='2026-01-08',version_id='legacy',rate_semantics='legacy_combined')]
        data=self.fixture(rates=tariff(versions));result=self.compare(data)
        self.assert_cost_partitions(result,data[3])
        for row in result['devices'].values():
            side=row['costs']['baseline']
            self.assertIsNone(side['energy_cost']);self.assertIsNone(side['variable_cost'])
            self.assertIsNotNone(side['known_costs']['energy_cost'])
            self.assertIsNotNone(side['known_costs']['legacy_combined_cost'])
            self.assertEqual(Decimal(side['usage_cost']),sum((Decimal(value)
                for value in side['known_costs'].values() if value is not None),Decimal(0)))
