"""Publish an allowlisted, offline view of a sealed Run or archive."""
import csv
from tempfile import TemporaryDirectory

from . import __version__
from .artifact_lifecycle import inspect_artifacts
from .artifact_manifest import _facts, artifact_path
from .common import asset_path, read_json, sha256, write_json
from .lineage_export import _html, _table
from .output_metadata import delivered_result_files, result_inventory, verify_result_metadata
from .resources import CODE_ROOT, INSTALLED, ROOT
from .run_record import publish_output, reserve_output


REPLAY_STAGES = ('sequence', 'features', 'report')
_RESULT = 'inc_results.csv'
_FIELDS = {
    'gate_pass': {'0', '1'},
    'predicted_label_count': {str(n) for n in range(22)},
    'internal_status': {'Unclassified', 'Inc_no_call', 'Inc_called'},
}
_RF_WEIGHTS = {'models/rf/stage1.joblib', 'models/rf/stage2.joblib'}


def _model_requirements(view, replay_from, model_dir):
    frozen = ROOT / 'config/assets.json'
    if view.manifest['model_manifest_sha256'] != sha256(frozen):
        return 'unknown_model_identity'
    if model_dir is None:
        return 'model_binding_required'

    entries = {entry['path']: entry for entry in read_json(frozen)['files']}
    required = set(entries) if replay_from == 'sequence' else _RF_WEIGHTS | {'config/thresholds.tsv'}
    if not required <= entries.keys():
        return 'unknown_model_identity'
    missing = False
    for name in sorted(required):
        path = asset_path(name, model_dir)
        if not path.exists():
            missing = True
            continue
        entry = entries[name]
        if _facts(path, name) != (entry['size_bytes'], entry['sha256']):
            raise ValueError('A supplied model asset does not match the recorded SHA-256')
    return 'model_binding_required' if missing else None


def _replay_assessment(view, replay_from, aliases, model_dir):
    if replay_from not in REPLAY_STAGES:
        raise ValueError('Unsupported replay entry point')
    if replay_from == 'report' and model_dir is not None:
        raise ValueError('Report replay does not use a model binding')

    summary = view.summary
    required_roles = {'input'} if replay_from == 'sequence' else {'input', 'features', 'feature_metadata'}
    files = result_inventory(delivered_result_files(summary)) if summary else set()
    needed = [entry for entry in view.manifest['artifacts']
              if (entry['storage'] != 'reference' and entry['path'] in files)
              or (replay_from != 'report' and entry['role'] in required_roles)]
    requirements = []
    for entry in needed:
        identity = entry['artifact_id']
        reason = view.missing.get(identity)
        requirements.append({'id': aliases[identity], 'role': entry['role'], 'state': reason or 'verified'})

    missing_internal = any(r['state'] in ('retention', 'missing') for r in requirements)
    known = (summary and summary.get('version') == __version__ and
             summary.get('contract_sha256') == sha256(ROOT / 'config/contract.json') and
             summary.get('runtime_policy_sha256') == sha256(ROOT / 'config/runtime.json'))
    if not known or missing_internal:
        return {'entry_point': replay_from, 'status': 'audit_only', 'requirements': requirements,
                'reason': 'missing_required_artifact' if missing_internal else 'incomplete_software_or_contract_identity'}

    prediction = read_json(view.root / 'prediction.json')
    context = verify_result_metadata(view.root, summary, prediction)
    if replay_from != 'report':
        if not isinstance(prediction.get('environment'), dict) or not prediction['environment']:
            return {'entry_point': replay_from, 'status': 'audit_only', 'requirements': requirements,
                    'reason': 'missing_rf_environment_identity'}
        if replay_from == 'sequence':
            feature = next(entry for entry in view.manifest['artifacts'] if entry['role'] == 'features')
            options = view.publication[2].get('parameters', {}) if view.publication else {}
            if (feature['stage'] != 'embedding' or not summary.get('embedding_environment')
                    or not all(name in options for name in ('device', 'batch_size', 'max_length', 'threads'))):
                return {'entry_point': replay_from, 'status': 'audit_only', 'requirements': requirements,
                        'reason': 'missing_equivalent_encoding_context'}
        reason = _model_requirements(view, replay_from, model_dir)
        if reason == 'unknown_model_identity':
            return {'entry_point': replay_from, 'status': 'audit_only', 'requirements': requirements, 'reason': reason}
        requirements.append({'role': 'models', 'state': reason or 'verified'})

    external = any(r['state'] in ('external_required', 'model_binding_required') for r in requirements)
    return {'entry_point': replay_from, 'status': 'external_required' if external else 'complete',
            'requirements': requirements, 'output_schema_version': context['output_schema_version']}


def _projection(view, aliases):
    artifacts = []
    for entry in view.manifest['artifacts']:
        identity = entry['artifact_id']
        item = {'id': aliases[identity], 'role': entry['role'], 'stage': entry['stage'],
                'parents': [aliases[parent] for parent in entry['parents']],
                'availability': 'available' if identity in view.paths else 'unavailable'}
        if identity in view.missing:
            item['reason'] = view.missing[identity]
        artifacts.append(item)
    return {'schema_version': 1, 'kind': 'shared_file_lineage', 'artifacts': artifacts}


def _project_result(view, fields, output):
    if len(fields) != len(set(fields)) or any(field not in _FIELDS for field in fields):
        raise ValueError('An approved result column is duplicated or outside the sharing allowlist')
    entry = next((item for item in view.manifest['artifacts'] if item['path'] == _RESULT), None)
    if entry is None or entry['role'] != 'result' or entry['storage'] != 'retained':
        raise ValueError('The sealed result table is outside the approved artifact types')
    path = view.paths.get(entry['artifact_id'])
    if path is None:
        raise ValueError('The approved result table is unavailable')
    count = 0
    with path.open(encoding='utf-8-sig', newline='') as original, output.open('x', encoding='ascii', newline='') as share:
        reader = csv.reader(original)
        header = next(reader, [])
        if len(header) != len(set(header)) or any(field not in header for field in fields):
            raise ValueError('The approved columns do not match the sealed result table')
        indexes = [header.index(field) for field in fields]
        writer = csv.writer(share, lineterminator='\n')
        writer.writerow(fields)
        for row in reader:
            if len(row) != len(header):
                raise ValueError('A result table row does not match its declared columns')
            projected = [row[index] for index in indexes]
            if any(value not in _FIELDS[field] for field, value in zip(fields, projected, strict=True)):
                raise ValueError('An approved result column contains a value outside its declared domain')
            writer.writerow(projected)
            count += 1
    if _facts(path, _RESULT, stored=True) != (entry['size_bytes'], entry['sha256']):
        raise ValueError('The source result changed while projecting approved fields')
    return {'file': output.name, 'fields': list(fields), 'row_count': count}


def export_share(directory, destination, *, replay_from='features', reference_paths=None, model_dir=None,
                 expected_manifest_sha256=None, result_fields=(), html=False):
    """Export only approved types and fields from verified source facts."""
    root, target = artifact_path(directory), artifact_path(destination)
    if target.is_relative_to(root):
        raise ValueError('Share destination must be outside the sealed source')
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'results'):
        raise ValueError('Share exports within the software root must be under results/')
    if INSTALLED and any(target.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Shares must not be written into the installed package')

    from .research_archive import RECEIPT, load_research_archive

    if (root / RECEIPT).is_file():
        _, view, _ = load_research_archive(root, expected_manifest_sha256=expected_manifest_sha256,
                                           reference_paths=reference_paths)
    else:
        view = inspect_artifacts(root, expected_manifest_sha256=expected_manifest_sha256, reference_paths=reference_paths)
    aliases = {entry['artifact_id']: f'a{index}' for index, entry in enumerate(view.manifest['artifacts'], 1)}
    graph = _projection(view, aliases)
    replay = _replay_assessment(view, replay_from, aliases, model_dir)
    with reserve_output(target, view.manifest['run_id']), TemporaryDirectory(prefix='.incfinder-share-', dir=target.parent) as tmp:
        staged = artifact_path(tmp) / 'share'
        staged.mkdir()
        write_json(staged / 'lineage.json', graph)
        payload = _project_result(view, result_fields, staged / 'result-fields.csv') if result_fields else None
        if html:
            rows = [(a['id'], a['role'], a['stage'], a['availability'], ', '.join(a['parents'])) for a in graph['artifacts']]
            document = _html('Shared lineage', '<h1>Shared lineage</h1>' +
                             _table(('ID', 'Role', 'Stage', 'Availability', 'Parents'), rows))
            (staged / 'index.html').write_text(document, encoding='utf-8', newline='\n')
        files = {path.name: sha256(path) for path in sorted(staged.iterdir())}
        share = {'schema_version': 1, 'kind': 'redacted_share', 'source_kind': view.manifest['kind'],
                 'reproducibility': {**replay, 'scope': 'source_and_verified_bindings'},
                 'result_projection': payload, 'files': files}
        write_json(staged / 'share_manifest.json', share)
        if (set(p.name for p in staged.iterdir()) != {*files, 'share_manifest.json'} or
                read_json(staged / 'share_manifest.json') != share or
                any(sha256(staged / name) != digest for name, digest in files.items())):
            raise ValueError('Share output failed independent read-back')
        publish_output(staged, target)
    return share
