"""Replay a verified prediction into a new Run using recorded settings."""
from pathlib import Path

from . import __version__
from .api import _dry_run, _predict
from .artifact_lifecycle import inspect_artifacts
from .artifact_manifest import _digest, artifact_path
from .common import ROOT, environment, read_json, sha256, verify_assets
from .output_metadata import load_output_contract, parse_output_formats
from .resources import model_location
from .run_events import canonical_uuid
from .run_record import _recorded_output, load_run_record, output_path, require_new_output

INTEGER_SETTINGS = ('batch_size', 'threads', 'jobs', 'max_length')
FLAG_SETTINGS = ('allow_encoder_drift', 'allow_out_of_domain')
SETTINGS = ('device', 'threshold_mode', *INTEGER_SETTINGS, *FLAG_SETTINGS, 'output_formats')


def _artifact(view, role):
    return next(item for item in view.manifest['artifacts'] if item['role'] == role)


def _required(view, role):
    item = _artifact(view, role)
    if item['artifact_id'] not in view.paths:
        reason = view.missing.get(item['artifact_id'])
        message = 'removed by retention' if reason == 'retention' else 'provide an explicit reference binding'
        raise FileNotFoundError(f"Missing {role} artifact ({item['artifact_id']}): {message}")
    return view.paths[item['artifact_id']]


def _published_run(run_id):
    try:
        state = load_run_record(run_id)
    except FileNotFoundError as exc:
        raise ValueError('Historical Run has no verifiable publication or parameter snapshot') from exc
    directory = _recorded_output(state)
    expected = (state.get('publication') or {}).get('artifact_manifest_sha256')
    if state['status'] != 'succeeded' or directory is None or not _digest(expected):
        raise ValueError('Historical Run has no verifiable completed publication or parameter snapshot')
    return directory, expected, state


def _snapshot(view):
    if not view.publication:
        return None
    state = view.publication[2]
    settings = {key: state['parameters'].get(key, {}).get('value') for key in SETTINGS}
    model = state.get('model', {})
    if (model.get('manifest_sha256') != view.manifest['model_manifest_sha256']
            or state.get('input_sha256') != _artifact(view, 'input')['sha256']
            or state.get('contract_sha256') != view.summary.get('contract_sha256')
            or state.get('runtime_policy_sha256') != view.summary.get('runtime_policy_sha256')
            or state.get('features', {}).get('sha256') != _artifact(view, 'features')['sha256']):
        raise ValueError('Run parameter snapshot conflicts with the sealed identities')
    snapshot = {'parameters': settings, 'parent_run_id': state['parent_run_id'],
                'entry': 'features' if state['features']['external'] else 'sequence'}
    _validate_snapshot(view, snapshot)
    return snapshot


def _validate_snapshot(view, snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {'parameters', 'parent_run_id', 'entry'}:
        raise ValueError('Historical Run lacks the required recorded replay parameters')
    settings = snapshot['parameters']
    if (not isinstance(settings, dict) or set(settings) != set(SETTINGS)
            or settings['device'] not in ('cpu', 'cuda')
            or settings['threshold_mode'] not in ('ct', 'u05')
            or any(type(settings[key]) is not int or settings[key] < 1 for key in INTEGER_SETTINGS)
            or any(type(settings[key]) is not bool for key in FLAG_SETTINGS)
            or snapshot['entry'] not in ('sequence', 'features')
            or (snapshot['parent_run_id'] is not None and not canonical_uuid(snapshot['parent_run_id']))):
        raise ValueError('Historical Run lacks the required recorded replay parameters')
    summary = view.summary
    if (summary['prediction']['threshold_mode'] != settings['threshold_mode']
            or parse_output_formats(summary.get('delivery', {}).get('requested_formats')) !=
            parse_output_formats(settings['output_formats'])
            or any(_artifact(view, role)['stage'] != ('source' if snapshot['entry'] == 'features' else 'embedding')
                   for role in ('features', 'feature_metadata'))):
        raise ValueError('Replay parameters disagree with the sealed output policy or feature origin')
    if snapshot['entry'] == 'sequence':
        if not settings['allow_encoder_drift'] or summary.get('embedding_device') != settings['device']:
            raise ValueError('Recorded encoding entry conflicts with the research-preview policy')
        feature_info = view.paths.get(_artifact(view, 'feature_metadata')['artifact_id'])
        if feature_info is not None:
            from .feature_metadata import read_feature_metadata

            feature_info = read_feature_metadata(feature_info)[0]
            if any(feature_info.get(key) != settings[key] for key in ('device', 'threads', 'batch_size')):
                raise ValueError('Recorded encoding parameters disagree with verified feature metadata')
    return snapshot


def _compatible(view, settings):
    summary = view.summary
    if summary.get('version') != __version__:
        raise ValueError('Source software version is unsupported for replay')
    identities = {'contract_sha256': sha256(ROOT / 'config/contract.json'),
                  'runtime_policy_sha256': sha256(ROOT / 'config/runtime.json'), **load_output_contract()}
    if any(summary.get(key) != value for key, value in identities.items()):
        raise ValueError('Source scientific, runtime or output contract does not match this installation')
    if view.manifest['model_manifest_sha256'] != sha256(ROOT / 'config/assets.json'):
        raise ValueError('Source model asset manifest does not match this installation')
    mode = settings['threshold_mode']
    from .common import load_thresholds

    contract = read_json(ROOT / 'config/contract.json')
    thresholds = load_thresholds(contract) if mode == 'ct' else [0.5] * len(contract['labels'])
    if summary['prediction']['stage2_thresholds'] != thresholds:
        raise ValueError('Source threshold values do not match this installation')


def _runtime(summary, snapshot):
    roles = {'rf': summary['prediction'].get('environment')}
    if snapshot['entry'] == 'sequence':
        roles['embedding'] = summary.get('embedding_environment')
    if 'xlsx' in snapshot['parameters']['output_formats']:
        roles['report'] = summary.get('report_environment')
    observed = {}
    for role, recorded in roles.items():
        current = environment(role)
        if not isinstance(recorded, dict) or not current.keys() <= recorded.keys():
            raise ValueError(f'Historical Run lacks recorded {role} dependency versions')
        changed = sorted(key for key in current if current[key] != recorded[key])
        if changed:
            raise ValueError(f"Recorded {role} environment does not match: {', '.join(changed)}")
        observed[role] = current
    return observed


def reproduce(source, destination, *, reference_paths=None, model_dir=None, expected_manifest_sha256=None,
              dry_run=False, progress=None):
    """Reproduce a local Run ID or sealed archive; use only verified dependencies."""
    local = isinstance(source, str) and canonical_uuid(source)
    if local:
        directory, recorded, local_state = _published_run(source)
        if expected_manifest_sha256 is not None and expected_manifest_sha256 != recorded:
            raise ValueError('Requested digest differs from the local publication event')
        view = inspect_artifacts(directory, expected_manifest_sha256=recorded, reference_paths=reference_paths)
        snapshot = _snapshot(view)
        parent_id = source
        source_digest = view.digest
    else:
        from .research_archive import load_research_archive

        directory = artifact_path(source)
        if (directory / 'share_manifest.json').is_file():
            raise ValueError('A redacted share is audit evidence and cannot be used to replay a Run')
        if (directory / 'archive_manifest.json').is_file():
            receipt, view, archive_digest = load_research_archive(
                directory, expected_manifest_sha256=expected_manifest_sha256, reference_paths=reference_paths)
            snapshot, source_digest = receipt['replay'], receipt['source_manifest_sha256']
        else:
            view = inspect_artifacts(directory, expected_manifest_sha256=expected_manifest_sha256,
                                     reference_paths=reference_paths)
            if view.manifest['kind'] != 'archive':
                raise ValueError('Reproduce requires a local Run ID or a sealed Run archive')
            archive_digest, source_digest = view.digest, view.manifest['source_manifest_sha256']
            snapshot = None
        parent_id = None
        try:
            original, recorded, local_state = _published_run(view.manifest['run_id'])
        except ValueError:
            pass
        else:
            if (recorded == source_digest and
                    local_state['publication']['summary_sha256'] == sha256(view.root / 'run_summary.json')):
                parent_id = view.manifest['run_id']
                if snapshot is None and original.is_dir():
                    original_view = inspect_artifacts(original, expected_manifest_sha256=recorded,
                                                       read_references=False)
                    snapshot = _snapshot(original_view)
    if snapshot is None:
        raise ValueError('Historical archive lacks a verified replay parameter snapshot')
    settings = dict(snapshot['parameters'])
    _compatible(view, settings)
    target = output_path(destination, None)
    if target.is_relative_to(directory) or (not local and parent_id and target.is_relative_to(original)):
        raise ValueError('Replay output must be outside the source Run or archive')
    require_new_output(target)
    input_path = _required(view, 'input')
    feature_dir, source_features = None, {}
    if snapshot['entry'] == 'features':
        matrix, metadata = (_required(view, role) for role in ('features', 'feature_metadata'))
        if metadata.name != 'features.json' or matrix != metadata.with_name('features.npy'):
            raise ValueError('Verified feature bindings must form a features.npy and features.json pair')
        feature_dir = matrix.parent
        source_features = {'features_sha256': _artifact(view, 'features')['sha256'],
                           'feature_metadata_sha256': _artifact(view, 'feature_metadata')['sha256']}

    if model_dir is None:
        if not local:
            raise ValueError('Missing model binding for archived replay: provide --model-dir')
        model_dir = view.publication[2]['parameters'].get('model_dir', {}).get('value')
        if not model_dir:
            raise ValueError('Historical Run lacks a model location: provide --model-dir')
    model_dir = model_location(model_dir)['path']
    models = verify_assets(quick=not dry_run, model_dir=model_dir)
    runtime = _runtime(view.summary, snapshot)
    origin = {'source_run_id': view.manifest['run_id'], 'source_manifest_sha256': source_digest,
              'entry': snapshot['entry'], 'input_sha256': _artifact(view, 'input')['sha256'], **source_features}
    if not local:
        origin['archive_manifest_sha256'] = archive_digest
    options = dict(settings, input=input_path, output=target, features_dir=feature_dir, model_dir=model_dir,
                   parent_run_id=parent_id)
    if dry_run:
        _dry_run(**options)
        return {'status': 'READY', 'reproduction': origin, 'parameters': settings, 'parent_run_id': parent_id,
                'runtime': runtime, 'model_manifest_sha256': models['manifest_sha256'],
                'bindings': {'input': str(input_path), 'features_dir': str(feature_dir) if feature_dir else None,
                             'model_dir': str(model_dir)}, 'output': str(target)}
    return _predict(**options, entry_point='reproduce', reproduction=origin, progress=progress)
