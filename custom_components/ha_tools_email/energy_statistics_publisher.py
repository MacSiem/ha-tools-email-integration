"""Explicit administrator dataset sharing; no automatic or background export.

Native external statistics are readable by all authenticated HA users. Existing
source ACL/purpose guards stay in force; they cannot restrict published history.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import uuid4

from .energy_statistics_ledger import digest, validate_state, merge_facts, make_plan, verification_plan
from .energy_statistics_recorder import NativeStatisticsRecorder, metadata_unit

EVENT_STATISTICS_SHARE_CHANGED = 'ha_tools_email_statistics_share_changed'

GLOBAL_WARNING = ('Native Recorder statistics are readable by all authenticated HA users. '
    'Disabling sharing stops future writes; it does not remove or restrict published history.')


class StatisticsStore:
    """Use the integration's existing confirmed durable-write implementation."""
    def __init__(self, hass, entry_id):
        from .storage import _ConfirmedStore
        self.store = _ConfirmedStore(hass, 1, f'ha_tools_email.energy_statistics_{digest(entry_id)}')
    async def async_load(self):
        return await self.store.async_load()
    async def async_save(self, state):
        await self.store.async_save_confirmed(state)


class EnergyStatisticsPublisher:
    def __init__(self, hass, service, *, store=None, recorder=None):
        self.hass, self.service = hass, service
        self.store = store if store is not None else StatisticsStore(hass, service.entry_id)
        self.recorder = recorder
        locks = hass.data.setdefault('ha_tools_email', {}).setdefault('energy_statistics_locks', {})
        self._lock = locks.setdefault(service.entry_id, asyncio.Lock())
        self._state, self._stopped = None, False

    def stop(self):
        self._stopped = True
        self._state = None

    def _check(self):
        state = getattr(self.hass, 'state', None)
        if self._stopped or getattr(state, 'name', state) in ('stopping', 'final_write'):
            raise RuntimeError('energy_statistics_unloaded')
        self.service._check_running()

    async def _admin(self, actor):
        self._check()
        if type(actor) is not str or not 0 < len(actor) <= 128:
            raise PermissionError('statistics_native_admin_required')
        user = await self.hass.auth.async_get_user(actor)
        self._check()
        if user is None or not user.is_active or not user.is_admin:
            raise PermissionError('statistics_native_admin_required')

    async def _load(self):
        if self._state is None:
            raw = await self.store.async_load()
            self._check()
            self._state = await self.service.coordinator._async_run_cpu(partial(
                validate_state, raw, self.service.entry_id))
            self._check()
        return self._state

    async def _save(self, state):
        """Hold the shared writer lane until a cancelled file write has ended.

        Store implementations may offload atomic writes to an executor. Task
        cancellation cannot cancel that physical write; releasing the lock early
        would let a reload overwrite its successor's durable intent.
        """
        task = asyncio.create_task(self.store.async_save(state))
        cancelled = None
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError as error:
                    cancelled = error
                    if task.done():
                        task.result()
                        break
            if cancelled is not None:
                raise cancelled
        except BaseException:
            self._state = None
            raise

    @staticmethod
    def _revision(state, expected):
        if type(expected) is not int or state['revision'] != expected:
            raise ValueError('statistics_revision_conflict')

    def _binding(self, facts, *, unknown_identity=None):
        if not facts.get('hours'):
            raise RuntimeError('statistics_dataset_unavailable')
        context, current = facts['context'], dict(facts['hours'][-1]['groups'])
        # Existing dataset identity may authorize clearing an old fact when a
        # tariff/version becomes unknown. This never fills a monetary value or
        # changes the historical currency in the producer facts. Enable/prepare
        # cannot use this fallback and always require a known identity.
        if unknown_identity is not None:
            for key in ('currency', 'price_basis', 'tariff_id'):
                if current.get(key) is None:
                    current[key] = unknown_identity[key]
        if facts.get('active_tariff_id') is not None:
            current['tariff_id'] = facts['active_tariff_id']
        if not all(isinstance(current.get(k), str) and current[k] for k in ('currency', 'price_basis', 'tariff_id')):
            raise RuntimeError('statistics_historical_currency_unavailable')
        return {'entry_id': self.service.entry_id, 'source_signature': context['source_signature'],
            'normalization_signature': context['normalization_signature'],
            'source_ids': sorted(set(facts['grid_source_ids'] + facts['consumer_source_ids'])),
            'measurement_timezone': context['measurement_timezone'],
            'policy_signature': facts['publication_policy_signature'],
            'feature_access_revision': context['feature_access_revision'],
            'tariff_id': current['tariff_id'], 'currency': current['currency'],
            'price_basis': current['price_basis'], 'membership_mode': 'current_snapshot'}

    async def _guard(self, facts, share):
        await self._admin(share['author'])
        if not await self.service.async_validate_statistics_hours(facts, share['author']):
            raise RuntimeError('statistics_context_changed')
        self._check()
        if self._binding(facts, unknown_identity=share['binding']) != share['binding']:
            raise RuntimeError('statistics_dataset_changed')

    def _summary(self, state):
        share = state['share']
        catalog = [{'statistic_id': sid, 'name': row['name'], 'currency': row['identity']['currency'],
                    'price_basis': row['identity']['price_basis'], 'epoch': row['identity']['epoch'],
                    'current_dataset': bool(share and row['identity']['epoch'] == share['epoch'])}
                   for sid, row in sorted(state['series'].items())]
        runtime = self.hass.data.get('ha_tools_email', {}).get('energy_statistics_runtime')
        automation = (runtime.public_status(state)
            if getattr(runtime, 'entry_id', None) == self.service.entry_id
            else {'state': 'unavailable' if share and share['enabled'] else 'disabled',
                  'last_completed_hour': None})
        return {'automation': automation, 'enabled': bool(share and share['enabled']), 'revision': state['revision'],
            'reader_scope': 'all_authenticated_ha_users', 'warning': GLOBAL_WARNING,
            'membership_mode': 'current_snapshot', 'pending': state['pending'] is not None,
            'unconfirmed_history': len(state['archived_intents']),
            'author_user_id': share['author'] if share else None,
            'active_epoch': share['epoch'] if share else None,
            'dataset': deepcopy(share['binding']) if share else None, 'catalog': catalog}

    @staticmethod
    def _runtime_share(state):
        share = state['share']
        return ((share['author'], share['epoch'], state['revision'])
                if share and share['enabled'] else None)

    async def async_runtime_context(self):
        """Internal consent only; no source/Recorder I/O and no invented actor."""
        self._check()
        async with self._lock:
            state = await self._load()
            self._check()
            return self._runtime_share(state)

    def _runtime_changed(self):
        self.hass.bus.async_fire(EVENT_STATISTICS_SHARE_CHANGED, {})

    async def async_status(self, actor):
        await self._admin(actor)
        async with self._lock:
            state = await self._load()
            await self._admin(actor)
            return self._summary(state)

    async def async_prepare(self, actor):
        await self._admin(actor)
        async with self._lock:
            state = await self._load()
            facts = await self.service.async_get_statistics_hours(actor, hours=1)
            await self._admin(actor)
            if not await self.service.async_validate_statistics_hours(facts, actor):
                raise RuntimeError('statistics_context_changed')
            return {'proposal': self._binding(facts), 'revision': state['revision'],
                    'warning': GLOBAL_WARNING, 'membership_mode': 'current_snapshot'}

    async def async_enable(self, actor, proposal, *, expected_revision,
                           acknowledge_global, acknowledge_current_membership):
        await self._admin(actor)
        if acknowledge_global is not True or acknowledge_current_membership is not True or type(proposal) is not dict:
            raise ValueError('statistics_explicit_share_ack_required')
        async with self._lock:
            state = await self._load(); self._revision(state, expected_revision)
            facts = await self.service.async_get_statistics_hours(actor, hours=1)
            binding = self._binding(facts)
            if binding != proposal:
                raise ValueError('statistics_proposal_changed')
            previous = state['share']
            same = previous is not None and previous['enabled'] and previous['binding'] == binding
            if state['pending'] is not None and (not same or previous['author'] != actor):
                raise RuntimeError('statistics_pending_dataset_requires_recovery')
            share = {'enabled': True, 'author': actor, 'binding': binding,
                     'epoch': previous['epoch'] if same else uuid4().hex}
            await self._guard(facts, share)
            candidate = deepcopy(state); candidate.update(share=share, revision=state['revision']+1, runtime=None)
            await self._save(candidate)
            self._state = candidate
            self._runtime_changed()
            await self._guard(facts, share)
            return self._summary(candidate)

    async def async_disable(self, actor, *, expected_revision):
        await self._admin(actor)
        async with self._lock:
            state = await self._load(); self._revision(state, expected_revision)
            candidate = deepcopy(state); candidate['revision'] += 1
            candidate['runtime'] = None
            if candidate['pending'] is not None:
                candidate['archived_intents'].append({**deepcopy(candidate['pending']),
                    'binding': deepcopy(candidate['share']['binding']),
                    'author': candidate['share']['author']})
                candidate['pending'] = None
            if candidate['share'] is not None:
                candidate['share']['enabled'] = False
            candidate = await self.service.coordinator._async_run_cpu(partial(
                validate_state, candidate, self.service.entry_id))
            await self._admin(actor)
            await self._save(candidate)
            self._state = candidate
            self._runtime_changed()
            await self._admin(actor)
            return self._summary(candidate)

    @staticmethod
    def _owned_metadata(plan, metadata):
        for row in plan:
            meta = metadata.get(row['statistic_id'])
            if meta is not None and (meta.get('source') != 'ha_tools_email'
                    or metadata_unit(meta) != row['record']['identity']['currency']):
                raise PermissionError('statistics_foreign_metadata')

    async def async_refresh_runtime(self, actor, *, expected_share, now):
        """Trusted single-hour cursor caller; never exposed by the WS schema."""
        if expected_share is None or type(now) is not datetime or now.utcoffset() is None:
            raise ValueError('invalid_statistics_runtime_request')
        return await self.async_refresh(actor, expected_share=expected_share, _runtime_now=now)

    @staticmethod
    def _runtime_result(state, status, now):
        cursor = state['runtime']
        def iso(stamp):
            return datetime.fromtimestamp(stamp/1000, timezone.utc).isoformat() if stamp is not None else None
        latest = int(now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp()*1000)
        age = now.timestamp()-cursor['next_end']/1000
        retry_after = 15 if age < 60 else 60 if age < 3600 else 300
        return {'status': status, 'retry_after': retry_after, 'completed_end': iso(cursor['last_end']),
                'pending_end': iso(cursor['next_end']),
                'retry': status == 'source_pending' or status == 'completed' and cursor['next_end'] <= latest}

    async def _complete_runtime(self, state, facts, share, now):
        if now is None:
            return self._summary(state)
        completed = deepcopy(state)
        cursor = completed['runtime']
        cursor['last_end'] = cursor['next_end']
        cursor['next_end'] += 3600000
        await self._save(completed)
        self._state = completed
        await self._guard(facts, share)
        return self._runtime_result(completed, 'completed', now)

    async def async_refresh(self, actor, *, backfill=False, acknowledge_current_membership=False,
                            expected_share=None, _runtime_now=None):
        await self._admin(actor)
        if type(backfill) is not bool or backfill and acknowledge_current_membership is not True:
            raise ValueError('statistics_backfill_current_snapshot_ack_required')
        # No overlapping work and no unbounded queue of administrator refreshes.
        if self._lock.locked():
            raise RuntimeError('energy_statistics_busy')
        async with self._lock:
            state = await self._load()
            share = state['share']
            if expected_share is not None and self._runtime_share(state) != expected_share:
                raise RuntimeError('statistics_consent_changed')
            if not share or not share['enabled']:
                return self._summary(state)
            if actor != share['author']:
                raise PermissionError('statistics_share_author_required')
            end = None
            if _runtime_now is not None:
                if backfill or expected_share is None:
                    raise ValueError('invalid_statistics_runtime_request')
                latest = _runtime_now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                latest_ms = int(latest.timestamp()*1000)
                cursor = state['runtime']
                if cursor is None:
                    candidate = deepcopy(state)
                    candidate['runtime'] = {'consent': list(expected_share), 'next_end': latest_ms,
                                            'last_end': None, 'paused': False}
                    # Persist the exact hour before any source await, even if no
                    # monetary facts can yet be written. No restart may skip it.
                    await self._save(candidate)
                    self._state = state = candidate
                    await self._admin(actor)
                    cursor = state['runtime']
                if cursor['paused']:
                    return self._runtime_result(state, 'paused', _runtime_now)
                if latest_ms-cursor['next_end'] >= 168*3600000:
                    candidate = deepcopy(state); candidate['runtime']['paused'] = True
                    await self._save(candidate)
                    self._state = candidate
                    await self._admin(actor)
                    return self._runtime_result(candidate, 'paused', _runtime_now)
                if cursor['next_end'] > latest_ms:
                    return self._runtime_result(state, 'waiting', _runtime_now)
                end = datetime.fromtimestamp(cursor['next_end']/1000, timezone.utc)
            facts = (await self.service.async_get_statistics_hours(actor, hours=1, end=end)
                     if end is not None else
                     await self.service.async_get_statistics_hours(actor, hours=168 if backfill else 1))
            await self._guard(facts, share)
            if _runtime_now is not None and facts.get('measurement_ready') is not True:
                return self._runtime_result(state, 'source_pending', _runtime_now)
            candidate, plan = await self.service.coordinator._async_run_cpu(partial(
                merge_facts, state, facts, self.service.entry_id))
            await self._guard(facts, share)
            if not plan:
                probe, view = await self.service.coordinator._async_run_cpu(partial(
                    verification_plan, state, facts))
                await self._guard(facts, share)
                if not view:
                    return await self._complete_runtime(state, facts, share, _runtime_now)
                if self.recorder is None:
                    self.recorder = NativeStatisticsRecorder(self.hass)
                existing = await self.recorder.async_metadata([row['statistic_id'] for row in view])
                await self._guard(facts, share)
                self._owned_metadata(view, existing)
                matched = await self.recorder.async_verify(view)
                await self._guard(facts, share)
                if matched:
                    return await self._complete_runtime(state, facts, share, _runtime_now)
                # Explicit refresh also repairs removed/changed native rows,
                # using only the durable owned ledger, not annual source IO.
                candidate = probe
                candidate['pending']['clear'] = list(candidate['pending']['series'])
                plan = await self.service.coordinator._async_run_cpu(partial(make_plan, candidate))
                await self._guard(facts, share)
            # Persist exact facts + intent before any enqueue, including retry.
            await self._save(candidate)
            self._state = candidate
            await self._guard(facts, share)
            if self.recorder is None:
                self.recorder = NativeStatisticsRecorder(self.hass)
            existing = await self.recorder.async_metadata([row['statistic_id'] for row in plan])
            await self._guard(facts, share)
            self._owned_metadata(plan, existing)
            # No await between fresh guard and this synchronous enqueue sequence.
            self.recorder.enqueue(plan)
            verified = await self.recorder.async_verify(plan)
            await self._guard(facts, share)
            if not verified:
                raise RuntimeError('statistics_readback_unconfirmed')
            confirmed = deepcopy(candidate); confirmed['pending'] = None
            await self._save(confirmed)
            self._state = confirmed
            await self._guard(facts, share)
            return await self._complete_runtime(confirmed, facts, share, _runtime_now)
