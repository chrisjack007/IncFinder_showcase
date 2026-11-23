"""Read and verify the evidence used by the lineage viewer."""
from contextlib import ExitStack
import csv
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import retained_view
from .artifact_manifest import _verify, artifact_path
from .common import ROOT, read_json, sha256
from .output_metadata import verify_result_metadata
from .run_record import publication_events
from .sequence_io import read_records


@dataclass
class Evidence:
    root: Path
    identity: dict
    manifest: dict
    records: list
    arrays: dict
    context: dict
    status_counts: dict
    assets: dict
    stages: dict
    manifest_evidence: list
    inputs: dict
    unavailable: dict | None = None


def _journal_evidence(root, manifest, digest):
    publication = publication_events(root, manifest, digest)
    if publication is None:
        return {}, False
    events, preparation = publication
    stages = {event['payload']['stage']: event['recorded_utc']
              for event in events if event['event_type'] == 'stage_entered'}
    stages['publication'] = preparation['recorded_utc']
    return stages, True


def _verify_rows(root, records, arrays, context, prediction):
    import numpy as np

    from .classify import _table_fields, _table_row, decide

    fields = _table_fields(context['label_order'])
    if prediction.get('tables') != fields:
        raise ValueError('Declared result table columns differ from Schema 2')
    decisions = decide(arrays['stage1'], arrays['stage2'], context['stage2_thresholds'])
    if any(not np.array_equal(arrays[name], expected) for name, expected in decisions.items()):
        raise ValueError('Recorded probabilities, gate, labels and statuses disagree')
    statuses = decisions['internal_status']
    counts = {name: int((statuses == name).sum()) for name in ('Unclassified', 'Inc_no_call', 'Inc_called')}
    passed = int(decisions['gate'].sum())
    expected_counts = {'Inc_unclassified': int(decisions['external_unclassified'].sum()),
                       'Inc_classified': counts['Inc_called']}
    if (prediction.get('internal_counts') != counts or prediction.get('counts') != expected_counts
            or prediction.get('gate_counts') != {'passed': passed, 'rejected': len(records) - passed}):
        raise ValueError('Prediction counts disagree with the verified arrays')

    with ExitStack() as stack:
        readers = {}
        for filename, columns in fields.items():
            stream = stack.enter_context((root / filename).open(encoding='utf-8-sig', newline=''))
            reader = readers[filename] = csv.reader(stream)
            if next(reader, None) != columns:
                raise ValueError(f'Unexpected lineage table columns: {filename}')
        for index, record in enumerate(records):
            rows = _table_row(index, record['header'], record['length_bp'], arrays['stage1'], arrays['stage2'],
                              decisions, context['label_order'], context['threshold_mode'], context['scientific_status'])
            for (filename, reader), expected in zip(readers.items(), rows):
                if next(reader, None) != [str(value) for value in expected]:
                    raise ValueError(f'CSV row conflicts with input identity or recorded probabilities: {filename} row {index}')
        for filename, reader in readers.items():
            if next(reader, None) is not None:
                raise ValueError(f'Unexpected extra row in verified lineage table: {filename}')
    return counts


def load_evidence(directory, *, expected_manifest_sha256=None, reference_paths=None, artifact_view=None):
    import numpy as np

    from .classify import validate_features

    root = artifact_path(directory)
    view = artifact_view or retained_view(root, expected_manifest_sha256=expected_manifest_sha256,
                                         reference_paths=reference_paths)
    if view is None:
        manifest, digest, paths = _verify(root, expected_manifest_sha256, reference_paths)
    else:
        manifest, digest, paths = view.manifest, view.digest, view.paths
        if any(reason != 'retention' for reason in view.missing.values()):
            raise ValueError('Record lineage requires the original verified dependencies')
    stages, has_journal = _journal_evidence(root, manifest, digest)
    binding = ['detached_manifest_sha256']
    if expected_manifest_sha256 is not None:
        binding.append('caller_manifest_sha256')
    if has_journal:
        binding.append('publication_event_sha256')
    summary = read_json(root / 'run_summary.json')
    prediction = read_json(root / 'prediction.json')
    context = verify_result_metadata(root, summary, prediction)
    summary_keys = ('output_schema_version', 'classification_policy_id', 'output_contract_sha256')

    inputs = {entry['role']: entry for entry in manifest['artifacts']
              if entry['role'] in ('input', 'features', 'feature_metadata')}
    feature_stage = 'embedding' if inputs['features']['storage'] == 'retained' else 'source'
    if inputs['input']['stage'] != 'source' or any(inputs[name]['stage'] != feature_stage
                                                   for name in ('features', 'feature_metadata')):
        raise ValueError('The input or feature origin disagrees with the verified manifest')
    required_ids = [inputs[name]['artifact_id'] for name in ('input', 'features', 'feature_metadata')]
    if any(identity not in paths for identity in required_ids):
        raise ValueError('Record lineage requires the original verified dependencies')
    source, feature_array, feature_json = (paths[identity] for identity in required_ids)
    if feature_json.name != 'features.json' or feature_array != feature_json.with_name('features.npy'):
        raise ValueError('Verified features do not form a canonical metadata and array pair')
    records = read_records(source)
    validate_features(feature_array.parent, records)
    declared_count = context['record_count']
    if len(records) != declared_count or summary['input'].get('record_count') != declared_count:
        raise ValueError('Record count differs between input, features and prediction')
    feature_meta = read_json(feature_json)
    if feature_meta.get('shape') != [declared_count, 1536] or feature_meta.get('dtype') != 'float32':
        raise ValueError('Feature metadata shape or dtype does not match the verified array')

    model_manifest = ROOT / 'config/assets.json'
    if manifest['model_manifest_sha256'] != sha256(model_manifest):
        raise ValueError('Result model identity does not match the supported frozen asset manifest')
    assets = {row['path']: row['sha256'] for row in read_json(model_manifest)['files']}
    if context['threshold_mode'] == 'ct' and assets['config/thresholds.tsv'] != sha256(ROOT / 'config/thresholds.tsv'):
        raise ValueError('Threshold table does not match the recorded asset identity')
    with np.load(root / 'probabilities.npz', allow_pickle=False) as data:
        arrays = {name: data[name] for name in data.files}
    records = feature_meta['records']
    status_counts = _verify_rows(root, records, arrays, context, prediction)
    identity = {
        'run_id': manifest['run_id'], 'manifest_kind': manifest['kind'], 'manifest_sha256': digest,
        'model_manifest_sha256': manifest['model_manifest_sha256'], 'contract_sha256': summary['contract_sha256'],
        'runtime_policy_sha256': summary['runtime_policy_sha256'], **{name: context[name] for name in summary_keys},
        'threshold_mode': context['threshold_mode'], 'scientific_status': context['scientific_status'],
    }
    if 'model_lineage' in summary:
        from .model_lineage import training_jobs
        identity['model_lineage'] = summary['model_lineage']
        jobs = training_jobs(ROOT, summary['model_lineage'])
        if jobs is not None:
            identity['training_jobs'] = jobs
    if 'delivery' in summary:
        identity['delivery'] = summary['delivery']
    if manifest['kind'] == 'archive':
        identity['source_manifest_sha256'] = manifest['source_manifest_sha256']
    return Evidence(root, identity, manifest, records, arrays, context, status_counts, assets, stages, binding, inputs,
                    view.missing if view else None)
