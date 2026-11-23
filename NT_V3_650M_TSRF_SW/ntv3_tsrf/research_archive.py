"""Seal private research evidence and read it back without a network connection."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from .artifact_archive import _stage_artifacts
from .artifact_lifecycle import inspect_artifacts
from .artifact_manifest import MANIFEST, SEAL, _digest, _facts, _files, artifact_path
from .common import ROOT, read_json, sha256, verify_assets, write_json
from .resources import CODE_ROOT, INSTALLED
from .reproduction import _snapshot, _validate_snapshot
from .run_record import publish_output, require_new_output, reserve_output

RECEIPT = 'archive_manifest.json'
CHECKSUM = 'archive_manifest.sha256'
CRATE = 'ro-crate-metadata.json'
ASSETS = 'model-assets.json'
PROFILE = 'https://w3id.org/ro/crate/1.2'
CONTEXT = PROFILE + '/context'
PRIVACY = {'scope': 'private_research', 'may_contain': ['record_identifiers', 'host_paths', 'logs', 'results'],
           'public_view': 'Use incfinder export-share for an allowlisted export.'}
LICENSES = [
    {'component': 'NTv3 model and vendor assets', 'status': 'upstream_terms',
     'document': 'models/ntv3/MODEL-LICENSE.md', 'scope': 'The upstream non-commercial conditions apply.'},
    {'component': 'IncFinder code', 'status': 'no_blanket_license',
     'document': 'README.md', 'scope': 'No blanket redistribution license is granted.'},
    {'component': 'RF weights, input and research results', 'status': 'permission_not_established',
     'document': None, 'scope': 'Obtain permission from the corresponding rights holders before redistribution.'},
]
_FIELDS = {'schema_version', 'source_manifest_sha256', 'source_run_id', 'payload_manifest_sha256',
           'source_completed_utc', 'replay', 'retention', 'members', 'licenses', 'privacy'}
_PAYLOAD = 'payload'
_FILES = {RECEIPT, CHECKSUM, CRATE, ASSETS, _PAYLOAD}


def _ref(value):
    return {'@id': value}


def _asset_requirements(path, manifest_digest):
    if sha256(path) != manifest_digest:
        raise ValueError('Archive model asset manifest SHA-256 differs from the Run inventory')
    files = read_json(path)['files']
    return [{'path': item['path'], 'sha256': item['sha256'], 'size_bytes': item['size_bytes']}
            for item in files if item['path'].startswith('models/')]


def _crate(view, receipt, models):
    entries = view.manifest['artifacts']
    retired = receipt['retention']
    run = '#run-' + receipt['source_run_id']
    ids = {item['artifact_id']: ('#artifact-' + str(index) if item['storage'] == 'reference' or
                                  item['artifact_id'] in retired else quote(f"{_PAYLOAD}/{item['path']}", safe='/'))
           for index, item in enumerate(entries, 1)}
    date = receipt['source_completed_utc']
    graph = [
        {'@id': CRATE, '@type': 'CreativeWork', 'conformsTo': _ref(PROFILE), 'about': _ref('./')},
        {'@id': './', '@type': 'Dataset', 'name': 'IncFinder Run archive',
         'description': 'Private research evidence with verified artifact identities and declared external assets.',
         'datePublished': date, 'license': _ref('#permissions'), 'mentions': _ref(run),
         'hasPart': [_ref(quote(name, safe='/')) for name in sorted(receipt['members']) if name != CRATE]
                    + [_ref(RECEIPT), _ref(CHECKSUM)]},
        {'@id': '#permissions', '@type': 'CreativeWork', 'name': 'Component permission statements',
         'description': ' '.join(item['component'] + '. ' + item['scope'] for item in LICENSES)},
        *({'@id': name, '@type': 'File', 'name': name}
          for name in (RECEIPT, CHECKSUM, ASSETS, f'{_PAYLOAD}/{MANIFEST}', f'{_PAYLOAD}/{SEAL}')),
    ]
    activity = {'@id': run, '@type': ['CreateAction', 'prov:Activity'], 'name': 'IncFinder prediction',
                'prov:endedAtTime': date, 'prov:used': [], 'result': []}
    parent = (receipt['replay'] or {}).get('parent_run_id')
    if parent:
        pid = '#run-' + parent
        graph.append({'@id': pid, '@type': 'prov:Activity', 'name': 'Parent Run'})
        activity['prov:wasInformedBy'] = _ref(pid)
    graph.append(activity)
    for entry in entries:
        key = entry['artifact_id']
        aid = ids[key]
        local = not aid.startswith('#')
        entity = {'@id': aid, '@type': ['File' if local else 'Thing', 'prov:Entity'],
                  'name': entry['role'], 'incfinder:artifactId': key, 'incfinder:sha256': entry['sha256'],
                  'contentSize': str(entry['size_bytes']),
                  'incfinder:availability': ('unavailable' if key in retired else
                                             'present' if local else 'external_required')}
        if entry['parents']:
            entity['prov:wasDerivedFrom'] = [_ref(ids[parent]) for parent in entry['parents']]
        if entry['stage'] == 'source':
            activity['prov:used'].append(_ref(aid))
        else:
            entity['prov:wasGeneratedBy'] = _ref(run)
            if entry['role'] == 'result':
                activity['result'].append(_ref(aid))
        if key in retired:
            event = retired[key]
            event_id = '#retention-' + aid.removeprefix('#artifact-')
            entity.update({'prov:wasInvalidatedBy': _ref(event_id), 'prov:invalidatedAtTime': event['recorded_utc']})
            graph.append({'@id': event_id, '@type': 'prov:Activity', 'name': 'Retention removal',
                          'incfinder:planSha256': event['plan_sha256'], 'prov:used': _ref(aid)})
        graph.append(entity)
    for index, model in enumerate(models, 1):
        aid = '#model-' + str(index)
        graph.append({'@id': aid, '@type': ['Thing', 'prov:Entity'], 'name': model['path'],
                      'incfinder:sha256': model['sha256'], 'contentSize': str(model['size_bytes']),
                      'incfinder:availability': 'external_required'})
        activity['prov:used'].append(_ref(aid))
    return {'@context': [CONTEXT, {'prov': 'http://www.w3.org/ns/prov#',
                                    'incfinder': 'https://github.com/chrisjack007/IncFinder_engineer#'}],
            '@graph': graph}


def _members(path):
    return {name: dict(zip(('size_bytes', 'sha256'), _facts(member, name, stored=True)))
            for name, member in _files(path, 'archive').items() if name not in (RECEIPT, CHECKSUM)}


def _retain_missing(view, retention):
    stored_missing = {key for key, reason in view.missing.items() if reason != 'external_required'}
    if not isinstance(retention, dict) or stored_missing != retention.keys():
        raise ValueError(f'Archive is missing stored artifacts or retention evidence: {sorted(stored_missing)}')
    inventory = {item['artifact_id']: item for item in view.manifest['artifacts']}
    for key, event in retention.items():
        if (not isinstance(event, dict) or event.get('status') != 'unavailable'
                or event.get('policy') not in ('minimal', 'standard')
                or event.get('reason') not in ('removed', 'missing_after_intent')
                or not _digest(event.get('plan_sha256'))
                or any(event.get(field) != inventory[key][field] for field in ('sha256', 'size_bytes', 'parents'))):
            raise ValueError(f'Invalid archive retention receipt for {key}')
        datetime.fromisoformat(event['recorded_utc'])
        view.missing[key] = 'retention'


def load_research_archive(directory, *, expected_manifest_sha256=None, reference_paths=None):
    root = artifact_path(directory)
    if {item.name for item in root.iterdir()} != _FILES:
        raise ValueError('Archive root has missing or unexpected members')
    document, seal = root / RECEIPT, root / CHECKSUM
    for field in (document, seal):
        _facts(field, field.name, stored=True)
    digest = sha256(document)
    if seal.read_bytes() != (digest + '\n').encode('ascii') or (
            expected_manifest_sha256 is not None and digest != expected_manifest_sha256):
        raise ValueError('Archive manifest SHA-256 mismatch')
    receipt = read_json(document)
    if (not isinstance(receipt, dict) or receipt.keys() != _FIELDS or type(receipt['schema_version']) is not int
            or receipt['schema_version'] != 1 or receipt['licenses'] != LICENSES or receipt['privacy'] != PRIVACY):
        raise ValueError('Unsupported or malformed archive manifest')
    actual = _members(root)
    if not isinstance(receipt['members'], dict) or actual != receipt['members']:
        expected = receipt['members'] if isinstance(receipt['members'], dict) else {}
        changed = sorted(name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name))
        raise ValueError('Archive member SHA-256, size or presence mismatch: ' + ', '.join(changed))
    view = inspect_artifacts(root / _PAYLOAD, expected_manifest_sha256=receipt['payload_manifest_sha256'],
                             reference_paths=reference_paths)
    if (view.manifest['kind'] != 'archive' or view.manifest['source_manifest_sha256'] != receipt['source_manifest_sha256']
            or view.manifest['run_id'] != receipt['source_run_id'] or view.summary is None
            or view.summary.get('completed_utc') != receipt['source_completed_utc']):
        raise ValueError('Archive manifest identity disagrees with the sealed prediction')
    _retain_missing(view, receipt['retention'])
    if receipt['replay'] is not None:
        _validate_snapshot(view, receipt['replay'])
    models = _asset_requirements(root / ASSETS, view.manifest['model_manifest_sha256'])
    if read_json(root / CRATE) != _crate(view, receipt, models):
        raise ValueError('RO-Crate descriptor, members or provenance do not match the sealed evidence')
    return receipt, view, digest


def verify_research_archive(directory, *, expected_manifest_sha256=None, reference_paths=None, model_dir=None):
    """Verify the crate and explicitly supplied bindings, reporting remaining dependencies."""
    receipt, view, digest = load_research_archive(directory, expected_manifest_sha256=expected_manifest_sha256,
                                                 reference_paths=reference_paths)
    models = _asset_requirements(view.root.parent / ASSETS, view.manifest['model_manifest_sha256'])
    status = 'external_required'
    if model_dir is not None:
        if sha256(ROOT / 'config/assets.json') != view.manifest['model_manifest_sha256']:
            raise ValueError('The archive uses a different model contract from this installation')
        verify_assets(model_dir=artifact_path(model_dir))
        status = 'verified'
    return {'status': 'verified', 'manifest_sha256': digest, 'source_run_id': receipt['source_run_id'],
            'ro_crate_profile': PROFILE, 'members_verified': len(receipt['members']),
            'dependencies': {item['artifact_id']: view.missing.get(item['artifact_id'], 'verified')
                             for item in view.manifest['artifacts'] if item['storage'] == 'reference' or
                             item['artifact_id'] in view.missing},
            'model_dependencies': models, 'model_status': status, 'parameters_recorded': receipt['replay'] is not None,
            'licenses': receipt['licenses'], 'privacy': receipt['privacy']}


def export_research_archive(directory, destination, *, external_mode='reference', reference_paths=None,
                            expected_manifest_sha256=None):
    """Create a private RO-Crate around a verified Run; use export-share for public views."""
    if external_mode not in ('reference', 'copy'):
        raise ValueError('External mode must be reference or copy')
    source, target = artifact_path(directory), artifact_path(destination)
    if target.is_relative_to(source):
        raise ValueError('Archive destination must be outside the source Run')
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'results'):
        raise ValueError('Archives within the software root must be under results/')
    if INSTALLED and any(target.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Archives must not be written into the installed package')
    require_new_output(target)
    view = inspect_artifacts(source, expected_manifest_sha256=expected_manifest_sha256, reference_paths=reference_paths)
    if view.manifest['kind'] != 'run' or view.summary is None:
        raise ValueError('Research archive export requires a sealed prediction result')
    if any(event['status'] == 'pending' for event in view.retention.values()):
        raise ValueError('Finish the pending retention operation before archiving the Run')
    _retain_missing(view, view.retention)
    date = view.summary.get('completed_utc')
    if not isinstance(date, str) or datetime.fromisoformat(date).tzinfo is None:
        raise ValueError('Source is missing a completed publication timestamp')
    models = ROOT / 'config/assets.json'
    requirements = _asset_requirements(models, view.manifest['model_manifest_sha256'])
    manifest = {'schema_version': 1, 'source_manifest_sha256': view.digest, 'source_run_id': view.manifest['run_id'],
                'source_completed_utc': date, 'replay': _snapshot(view), 'retention': view.retention,
                'licenses': LICENSES, 'privacy': PRIVACY}
    with reserve_output(target, manifest['source_run_id']), TemporaryDirectory(prefix='.incfinder-crate-', dir=target.parent) as tmp:
        staged = Path(tmp) / 'crate'
        staged.mkdir()
        payload = staged / _PAYLOAD
        payload.mkdir()
        manifest['payload_manifest_sha256'], _ = _stage_artifacts(payload, view.manifest, view.digest, view.paths,
                                                               external_mode, portable=True)
        sealed = inspect_artifacts(payload, expected_manifest_sha256=manifest['payload_manifest_sha256'])
        (staged / ASSETS).write_bytes(models.read_bytes())
        manifest['members'] = _members(staged)
        write_json(staged / CRATE, _crate(sealed, manifest, requirements))
        manifest['members'] = _members(staged)
        write_json(staged / RECEIPT, manifest)
        seal = sha256(staged / RECEIPT)
        (staged / CHECKSUM).write_bytes((seal + '\n').encode('ascii'))
        verified = verify_research_archive(staged, expected_manifest_sha256=seal)
        publish_output(staged, target)
    return verified
