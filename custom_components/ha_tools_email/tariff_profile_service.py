"""Pure projection/edit jobs for the additive tariff-profile API.

The canonical book is never replaced by a list of wire profiles. Schema-1
migration always needs explicit rate semantics, and remains read-only until an
explicit save passes the same historical-edit proof as the book API.
"""
from copy import deepcopy

from .tariff import TariffValidationError
from .tariff_history import migrate_tariff_v1, prepare_tariff_book_edit, validate_tariff_book
from .tariff_profiles import book_to_profile, is_profile_v2, profile_to_book


def _fail(reason):
    raise TariffValidationError([{'path': 'profile', 'reason': reason}])


def _base_book(book, legacy_profile, legacy_combined):
    if book is not None:
        return validate_tariff_book(book)
    if legacy_profile is None:
        return None
    if type(legacy_combined) is not bool:
        _fail('explicit_legacy_semantics_required')
    migrated = migrate_tariff_v1(legacy_profile, legacy_combined=legacy_combined)
    migrated['tariffs'][0]['name'] = 'Taryfa'
    return migrated


def project_profile_state(book, legacy_profile, *, tariff_id=None, version_id=None,
                          legacy_combined=None):
    """Return selected wire version plus its entire sorted history, without IO."""
    if (book is None and legacy_profile is not None
            and (not isinstance(legacy_profile, dict) or legacy_profile.get('schema') != 1)):
        _fail('invalid_legacy_profile')
    if book is None and legacy_profile is not None and legacy_combined is None:
        # An old valid profile remains available through the unchanged old API.
        # Do not claim that its missing component was known zero or combined.
        return {'status': 'migration_required', 'profile': None, 'tariff_profiles': [],
                'tariff_id': None, 'version_id': None,
                'migration_required': ['explicit_legacy_semantics_required'], 'tariffs': []}
    clean = _base_book(book, legacy_profile, legacy_combined)
    if clean is None:
        if tariff_id is not None or version_id is not None:
            _fail('unknown_tariff_or_version')
        return {'status': 'empty', 'profile': None, 'tariff_profiles': [],
                'tariff_id': None, 'version_id': None, 'tariffs': []}
    target = tariff_id if tariff_id is not None else clean.get('active_tariff_id')
    tariff = next((t for t in clean['tariffs'] if t['id'] == target), None)
    if tariff is None:
        # A deliberately inactive book must remain inactive. The caller chooses
        # a tariff explicitly rather than accidentally activating the first one.
        if target is not None or version_id is not None:
            _fail('unknown_tariff_or_version')
        return {'status': 'inactive', 'profile': None, 'tariff_profiles': [],
                'tariff_id': None, 'version_id': None,
                'tariffs': [{'id': t['id'], 'name': t['name'], 'origin': deepcopy(t['origin'])}
                            for t in clean['tariffs']]}
    selected = (tariff['versions'][-1] if version_id is None else
                next((v for v in tariff['versions'] if v['id'] == version_id), None))
    if selected is None:
        _fail('unknown_version')
    rows = [{'version_id': v['id'], 'profile': book_to_profile(clean, tariff_id=target, version_id=v['id'])}
            for v in tariff['versions']]
    return {'status': 'ready', 'profile': book_to_profile(clean, tariff_id=target, version_id=selected['id']),
            'tariff_profiles': rows, 'tariff_id': target, 'version_id': selected['id'],
            'tariffs': [{'id': t['id'], 'name': t['name'], 'origin': deepcopy(t['origin'])}
                        for t in clean['tariffs']],
            'migration_preview': book is None and legacy_profile is not None}


def prepare_profile_edit(book, legacy_profile, profile, *, now, tariff_id=None,
                         version_id=None, legacy_combined=None):
    """Merge one profile then prove historical effect against the actual base.

    A schema-1 import into an existing user tariff is not a silent overwrite:
    it must target a new tariff or an existing legacy tariff. The separate old
    set_tariff route retains its original schema-1 behavior.
    """
    before = _base_book(book, legacy_profile, legacy_combined)
    if is_profile_v2(profile):
        proposed = profile_to_book(profile, book=before, tariff_id=tariff_id, version_id=version_id)
        target = tariff_id if tariff_id is not None else proposed['active_tariff_id']
        start = profile.get('validFrom')
    elif isinstance(profile, dict) and type(profile.get('schema')) is int and profile['schema'] == 1:
        if type(legacy_combined) is not bool:
            _fail('explicit_legacy_semantics_required')
        target = tariff_id if tariff_id is not None else (
            before.get('active_tariff_id') if before else 'legacy')
        incoming = migrate_tariff_v1(profile, legacy_combined=legacy_combined, tariff_id=target)
        incoming['tariffs'][0]['name'] = 'Taryfa'
        wire = book_to_profile(incoming)
        wire['name'] = 'Taryfa'
        if before is None:
            proposed = incoming
            if version_id is not None:
                proposed['tariffs'][0]['versions'][0]['id'] = version_id
        else:
            old = next((t for t in before['tariffs'] if t['id'] == target), None)
            if old is not None and old['origin']['kind'] != 'legacy':
                _fail('schema1_import_requires_legacy_or_new_tariff')
            if old is not None:
                wire.pop('origin', None)  # Preserve the existing legacy origin.
            proposed = profile_to_book(wire, book=before, tariff_id=target, version_id=version_id)
        start = wire.get('validFrom')
        imported = next(t for t in proposed['tariffs'] if t['id'] == target)
        imported_version = next(v for v in imported['versions'] if v.get('valid_from') == start)
        imported_version['source_schema1'] = deepcopy(profile)
    else:
        _fail('invalid_wire_profile_or_schema1')
    clean, changes = prepare_tariff_book_edit(before, proposed, now)
    tariff = next(t for t in clean['tariffs'] if t['id'] == target)
    selected = next(v for v in tariff['versions'] if v.get('valid_from') == start)
    result = project_profile_state(clean, None, tariff_id=target, version_id=selected['id'])
    result['historical_changes'] = changes
    return clean, result
