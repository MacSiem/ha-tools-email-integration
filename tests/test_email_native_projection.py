"""Pure per-schedule native facts use confirmed history and canonical identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from tests.test_tariff_profile import load_storage
from tests.test_attempt_history import record

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


class NativeEmailProjectionTests(unittest.TestCase):
    def fixture(self):
        storage_module = load_storage()
        projector = storage_module.project_native_reports
        storage = storage_module.EmailStorage
        return projector, storage._stored_schedule_id

    def project(self, data):
        projector, resolver = self.fixture()
        return projector(data, now=NOW, local_timezone='UTC', schedule_id_resolver=resolver)

    def test_per_schedule_isolation_explicit_and_exact_legacy_identity_no_private_projection(self):
        explicit = {'id': 'stable', 'kind': 'energy_report', 'cadence': 'daily',
                    'time': '15:00', 'enabled': True, 'label': 'PRIVATE',
                    'recipients': ['PRIVATE@example.invalid']}
        legacy = {'kind': 'log_digest', 'cadence': 'weekly', 'weekday': 2, 'time': '07:00',
                  'enabled': True, 'label': 'PRIVATE_LEGACY', 'recipients': ['OTHER_PRIVATE@example.invalid']}
        _, resolver = self.fixture()
        key = hashlib.sha256(b'stable').hexdigest()
        legacy_key = hashlib.sha256(resolver(legacy).encode()).hexdigest()
        attempt = record(schedule_ref=key, recipient_count=0)
        data = {'schedules': [explicit, legacy], 'attempt_history': {'schema': 1, 'attempts': [attempt]},
                'smtp_config': {'password': 'PRIVATE_PASSWORD'}}
        before = deepcopy(data)
        result = self.project(data)
        self.assertEqual({key, legacy_key}, set(result['schedules']))
        self.assertEqual('accepted_by_smtp', result['schedules'][key]['last_attempt']['outcome'])
        self.assertEqual(0, result['schedules'][key]['last_attempt']['recipient_count'])
        self.assertIsNone(result['schedules'][legacy_key]['last_attempt'])
        self.assertEqual(resolver(legacy), result['identities'][legacy_key])
        self.assertNotIn('PRIVATE', repr(result['schedules']))
        self.assertNotIn('@', repr(result['schedules']))
        self.assertEqual(before, data)
        self.assertIsNone(result['schedules'][key]['errors_24h'])
        self.assertIsNone(result['schedules'][key]['warnings_24h'])
        self.assertIsNot(result['schedules'][key]['errors_24h'], 0)

    def test_disabled_missing_history_and_unknown_keep_real_counts_unknown(self):
        row = {'id': 'a', 'kind': 'log_digest', 'cadence': 'daily', 'time': '12:30', 'enabled': False}
        result = self.project({'schedules': [row], 'attempt_history': {'schema': 99}})
        value = next(iter(result['schedules'].values()))
        self.assertFalse(value['enabled'])
        self.assertIsNone(value['next_report_at'])
        self.assertIsNone(value['last_attempt'])
        self.assertFalse(value['history_ready'])
        self.assertEqual({}, result['repairs'])
        self.assertEqual({}, self.project({'schedules': []})['schedules'])
        with self.assertRaises(ValueError): self.project({'schedules': [row] * 129})
        with self.assertRaises(ValueError): self.project({'schedules': [row, row]})

    def test_repair_conditions_known_failures_unknown_and_confirmed_recovery(self):
        _, resolver = self.fixture()
        projector = self.fixture()[0]
        outcomes = ['authentication_failed', 'send_failed', 'recipients_refused']
        rows = [record(id=f'00000000-0000-4000-8000-{index:012d}', outcome=outcome,
            timestamp=(NOW - timedelta(minutes=3-index)).isoformat()) for index, outcome in enumerate(outcomes)]
        result = projector({'schedules': [], 'attempt_history': {'schema': 1, 'attempts': rows}},
            now=NOW, local_timezone='UTC', schedule_id_resolver=resolver)
        self.assertEqual({'email_native_auth': True, 'email_native_failures': True}, result['repairs'])
        rows.append(record(id='00000000-0000-4000-8000-000000000099', timestamp=NOW.isoformat(),
                           outcome='transport_outcome_unknown'))
        result = projector({'schedules': [], 'attempt_history': {'schema': 1, 'attempts': rows}},
            now=NOW, local_timezone='UTC', schedule_id_resolver=resolver)
        self.assertIsNone(result['repairs']['email_native_failures'])
        rows[-1]['outcome'] = 'accepted_by_smtp'
        result = projector({'schedules': [], 'attempt_history': {'schema': 1, 'attempts': rows}},
            now=NOW, local_timezone='UTC', schedule_id_resolver=resolver)
        self.assertEqual({'email_native_auth': False, 'email_native_failures': False}, result['repairs'])

    def test_calendar_range_with_same_zoneinfo_and_different_folds_compares_utc_instants(self):
        from zoneinfo import ZoneInfo
        projector, _ = self.fixture()
        occurrences = projector.__globals__['calendar_occurrences']
        zone = ZoneInfo('Europe/Warsaw')
        start = datetime(2026, 10, 25, 2, 0, tzinfo=zone, fold=0)
        end = datetime(2026, 10, 25, 2, 0, tzinfo=zone, fold=1)
        result = occurrences({'cadence': 'daily', 'time': '02:30', 'timezone': 'Europe/Warsaw'},
            start, end, local_timezone='Europe/Warsaw')
        self.assertEqual([datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)], result)
