"""Bind result files to an explicit, supported output policy."""
import csv
import hashlib
import json
from pathlib import Path
import re

from .common import ROOT, load_thresholds, read_json, sha256, write_json
from .model_lineage import validate_binding


POLICY_ID = 'external_unclassified_final21zero_v1'
LEGACY_POLICY_ID = 'legacy_v1_native_gate_flags'
CURRENT_SCHEMA_VERSION = 2
RESULT_FILES = (
    'layer1_decisions.csv', 'layer2_probabilities.csv', 'inc_label_predictions.csv',
    'inc_results.csv', 'inc_predictions.xlsx', 'probabilities.npz',
)
WORKBOOK = 'inc_predictions.xlsx'
CORE_RESULT_FILES = tuple(name for name in RESULT_FILES if name != WORKBOOK)
CSV_RESULT_FILES = tuple(name for name in CORE_RESULT_FILES if name.endswith('.csv'))
OUTPUT_FORMATS = ('csv', 'npz', 'xlsx')
REPORT_STATES = frozenset({'not_requested', 'succeeded', 'failed', 'interrupted'})
SOURCE_VERSIONS = {'v1': 1, 'v2': CURRENT_SCHEMA_VERSION}
_NPZ_ADDITIONS = {
    'external_unclassified': 'bool (N,)',
    'external_classified': 'bool (N,)',
    'internal_status': 'Unicode (N,); no object arrays',
}
_SUPPORTED_OUTPUT_FIELDS = {
    'classification_policy_id': POLICY_ID,
    'external_unclassified': 'all 21 final Inc calls are zero',
    'external_classified': 'at least one final Inc call is one',
    'external_display_label': 'Inc Unclassified',
    'final_status_values': ['Inc_unclassified', 'Inc_classified'],
    'internal_status_values': ['Unclassified', 'Inc_no_call', 'Inc_called'],
    'gate_operator': '>=',
    'label_operator': '>=',
    'rejected_stage2_probabilities': 'NaN in NPZ; NA in CSV/XLSX',
    'external_unclassified_continuous_score': None,
    'prediction_is_ground_truth': False,
    'npz_additions': _NPZ_ADDITIONS,
}


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate output contract fields')
    return result


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON value: {value}')


def load_output_contract():
    """Return the supported output identity derived from one resource read."""
    try:
        payload = (ROOT / 'config/output_contract.json').read_bytes()
        contract = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_fields,
                              parse_constant=_invalid_constant)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read output contract: {exc}') from exc
    if not isinstance(contract, dict):
        raise ValueError('Output contract must be a JSON object')
    version = contract.get('schema_version')
    if type(version) is not int or version != CURRENT_SCHEMA_VERSION:
        raise ValueError(f'Unsupported output contract schema version: {version!r}')
    for name, expected in _SUPPORTED_OUTPUT_FIELDS.items():
        if name not in contract or type(contract[name]) is not type(expected) or contract[name] != expected:
            raise ValueError(f'Missing or inconsistent output contract field: {name}')
    definitions = contract.get('internal_status_definitions')
    if not isinstance(definitions, dict) or definitions.keys() != set(contract['internal_status_values']):
        raise ValueError('Missing or inconsistent output contract field: internal_status_definitions')
    descriptions = dict(definitions, stage1_pred_scope=contract.get('stage1_pred_scope'),
                        invalid_or_missing_prediction=contract.get('invalid_or_missing_prediction'))
    for name, value in descriptions.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Missing or invalid output contract description: {name}')
    return {'output_schema_version': version, 'classification_policy_id': contract['classification_policy_id'],
            'output_contract_sha256': hashlib.sha256(payload).hexdigest()}


def _declared_version(source_version):
    if source_version is not None and source_version not in SOURCE_VERSIONS:
        raise ValueError("source_version must be 'v1' or 'v2'")
    return SOURCE_VERSIONS.get(source_version)


def _validate_context(prediction, version, *, output_contract=None):
    """Check only the declared policy and frozen label/threshold identities."""
    if not isinstance(prediction, dict):
        raise ValueError('Result policy metadata must be a JSON object')
    contract = read_json(ROOT / 'config/contract.json')
    mode = prediction.get('threshold_mode')
    if mode not in ('ct', 'u05'):
        raise ValueError('Missing or invalid threshold_mode; it cannot be inferred')
    thresholds = load_thresholds(contract) if mode == 'ct' else [0.5] * 21
    if (prediction.get('label_order') != contract['labels']
            or prediction.get('stage1_threshold') != 0.5
            or prediction.get('stage2_thresholds') != thresholds):
        raise ValueError('Result labels or thresholds do not match the frozen contract')
    count = prediction.get('record_count')
    if type(count) is not int or count <= 0:
        raise ValueError('record_count must be a positive integer')
    scientific_status = prediction.get('scientific_status')
    if scientific_status not in ('UNVERIFIED', 'RESEARCH_PREVIEW', 'FROZEN_REFERENCE'):
        raise ValueError('Missing or invalid scientific status')
    schema = prediction.get('output_schema_version', 1)
    if type(schema) is not int or schema != version:
        raise ValueError('Explicit source version conflicts with the result schema')
    if version == CURRENT_SCHEMA_VERSION:
        identity = load_output_contract() if output_contract is None else output_contract
        if prediction.get('classification_policy_id') != identity['classification_policy_id']:
            raise ValueError('Unsupported or inconsistent classification policy')
        if prediction.get('output_contract_sha256') != identity['output_contract_sha256']:
            raise ValueError('Missing or unsupported output contract SHA-256')
    else:
        if prediction.get('classification_policy_id') not in (None, LEGACY_POLICY_ID):
            raise ValueError('V1 source declaration conflicts with a newer output policy')
        identity = {'output_schema_version': version, 'classification_policy_id': LEGACY_POLICY_ID,
                    'output_contract_sha256': None}
    return {**identity,
            'threshold_mode': mode, 'stage1_threshold': 0.5, 'stage2_thresholds': thresholds,
            'label_order': contract['labels'], 'record_count': count, 'scientific_status': scientific_status}


def _validate_current_context(metadata, output_contract=None):
    if not isinstance(metadata, dict) or metadata.get('output_schema_version') != CURRENT_SCHEMA_VERSION:
        raise ValueError('Unsupported output schema; migrate V1 with migrate_v1_results first')
    _validate_context(metadata, CURRENT_SCHEMA_VERSION, output_contract=output_contract)


def require_supported_policy(metadata):
    """Return V2 metadata or reject it; V1 requires explicit directory migration."""
    _validate_current_context(metadata)
    return metadata


def _check_structure(path, metadata):
    """Recognize version-bearing fields without claiming full numerical QC."""
    version = metadata['output_schema_version']
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            fields = next(reader, [])
            if not fields or len(fields) != len(set(fields)):
                raise ValueError('Missing or duplicated CSV columns')
            count = 0
            mode_column = fields.index('threshold_mode') if 'threshold_mode' in fields else None
            for row in reader:
                if len(row) != len(fields):
                    raise ValueError('CSV row does not match its declared columns')
                if mode_column is not None and row[mode_column] != metadata['threshold_mode']:
                    raise ValueError('CSV threshold mode conflicts with its metadata')
                count += 1
            if count != metadata['record_count']:
                raise ValueError('CSV record count conflicts with its metadata')
        if {'Inc_unclassified', 'Inc_classified'}.issubset(fields) or 'final_status' in fields:
            trace = {'gate_pass', 'internal_status'}
            if (version == CURRENT_SCHEMA_VERSION and not trace.issubset(fields)) or (version == 1 and trace & set(fields)):
                raise ValueError('CSV fields conflict with the declared output version')
    elif path.suffix.lower() == '.npz':
        _check_npz(path, metadata, version)
    elif path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        with path.open('rb') as stream:
            workbook = load_workbook(stream, read_only=True, data_only=False)
            try:
                for name in ('Inc_prediction', 'Inc_result'):
                    if name not in workbook.sheetnames:
                        raise ValueError('Missing version-bearing XLSX sheets')
                    fields = set(next(workbook[name].values, ()))
                    trace = {'gate_pass', 'internal_status'}
                    if (version == CURRENT_SCHEMA_VERSION and not trace.issubset(fields)) or (version == 1 and trace & fields):
                        raise ValueError('XLSX fields conflict with the declared output version')
            finally:
                workbook.close()
    else:
        raise ValueError('Result metadata supports CSV, XLSX and NPZ files')



def _check_npz(path, metadata, version):
    from math import prod
    from zipfile import BadZipFile, ZipFile

    import numpy as np

    native = {'stage1', 'stage2', 'gate', 'labels'}
    added = _NPZ_ADDITIONS.keys()
    expected = native | added if version == CURRENT_SCHEMA_VERSION else native
    count = metadata['record_count']
    shapes = {'stage1': (count,), 'stage2': (count, 21), 'gate': (count,), 'labels': (count, 21)}
    dtypes = {'stage1': 'float32', 'stage2': 'float32', 'gate': 'bool', 'labels': 'uint8'}
    if version == CURRENT_SCHEMA_VERSION:
        shapes.update({name: (count,) for name in added})
        dtypes.update(external_unclassified='bool', external_classified='bool')
    try:
        with ZipFile(path) as archive:
            members = archive.namelist()
            if set(members) != {name + '.npy' for name in expected} or len(members) != len(expected):
                raise ValueError('NPZ arrays conflict with the declared output version')
            for name in expected:
                info = archive.getinfo(name + '.npy')
                with archive.open(info) as stream:
                    magic = np.lib.format.read_magic(stream)
                    if magic == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                    elif magic in ((2, 0), (3, 0)):
                        # The required plain dtypes have ASCII headers in either version.
                        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                    else:
                        raise ValueError('Unsupported NPY header in a result array')
                    if dtype.hasobject:
                        raise ValueError('Result arrays must not contain Python objects')
                    if name == 'internal_status' and dtype.kind != 'U':
                        raise ValueError('NPZ internal states must be Unicode strings')
                    if shape != shapes[name] or (name in dtypes and dtype != np.dtype(dtypes[name])):
                        raise ValueError('NPZ shape or dtype conflicts with its output metadata')
                    if info.file_size != stream.tell() + prod(shape) * dtype.itemsize:
                        raise ValueError('NPZ array byte count disagrees with its header')
            if archive.testzip() is not None:
                raise ValueError('Corrupted NPZ member')
    except (BadZipFile, EOFError) as exc:
        raise ValueError('Invalid NPZ container') from exc


def _validate_sidecar(path, metadata, declared, *, output_contract=None):
    if not isinstance(metadata, dict) or type(metadata.get('metadata_schema_version')) is not int or metadata['metadata_schema_version'] != 1:
        raise ValueError('Unsupported metadata schema version')
    version = metadata.get('output_schema_version')
    if declared is not None and version != declared:
        raise ValueError('Explicit source version conflicts with existing metadata')
    _validate_current_context(metadata, output_contract)
    if 'model_lineage' in metadata:
        validate_binding(metadata['model_lineage'])
    if metadata.get('result_file') != path.name:
        raise ValueError('Metadata belongs to a different result file')
    digest = metadata.get('result_sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or digest != sha256(path):
        raise ValueError('Result SHA-256 does not match its metadata')
    _check_structure(path, metadata)
    return dict(metadata, metadata_source='sidecar')


def load_output_metadata(result_path, *, source_version=None):
    """Validate a file and its sidecar, or an explicitly declared legacy context.

    A sidecar travels with its result: ``name.csv.metadata.json``. Without it,
    an explicit version and matching prediction/run-summary JSON are required.
    V1 has no separate output contract: its returned identity is caller-declared
    and require_supported_policy rejects it until it has been migrated.
    Raises ValueError for incompatible, incomplete or conflicting identities.
    This is a version/integrity guard, not a replacement for numerical QC.
    """
    declared = _declared_version(source_version)
    path = Path(result_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'Result file does not exist: {path}')
    sidecar = path.with_name(path.name + '.metadata.json')
    if sidecar.exists():
        return _validate_sidecar(path, read_json(sidecar), declared)
    if declared is None:
        raise ValueError("Missing output metadata; explicitly declare source_version='v1' or 'v2'")
    prediction_path, summary_path = path.parent / 'prediction.json', path.parent / 'run_summary.json'
    if not prediction_path.is_file() or not summary_path.is_file():
        raise ValueError('Declared legacy files require prediction.json and run_summary.json; do not guess their identities')
    prediction, summary = read_json(prediction_path), read_json(summary_path)
    identity = _validate_context(prediction, declared)
    if not isinstance(summary, dict) or not isinstance(summary.get('outputs'), dict):
        raise ValueError('The source run summary must contain an output hash mapping')
    if summary.get('contract_sha256') != sha256(ROOT / 'config/contract.json'):
        raise ValueError('Missing or inconsistent source scientific contract SHA-256')
    if declared == 1:
        if summary.get('version') != '0.3.0' or summary.get('output_schema_version', 1) != 1:
            raise ValueError('The result summary conflicts with the declared V1 source')
    else:
        for name in ('output_schema_version', 'classification_policy_id', 'output_contract_sha256'):
            if summary.get(name) != identity[name]:
                raise ValueError('The result summary conflicts with the declared V2 source')
    expected = summary.get('outputs', {}).get(path.name)
    actual = sha256(path)
    if not expected or actual != expected:
        raise ValueError('The declared result does not match its source run-summary SHA-256')
    _check_structure(path, identity)
    return dict(identity, metadata_schema_version=1, result_file=path.name, result_sha256=actual,
                metadata_source='user_declared', source_version=source_version)


def _write_sidecars(directory, identity, files):
    pending = []
    for name in files:
        path = directory / name
        if not path.is_file():
            raise ValueError(f'Missing required result file: {name}')
        metadata = dict(identity, metadata_schema_version=1, result_file=name, result_sha256=sha256(path))
        sidecar = directory / (name + '.metadata.json')
        if sidecar.exists() and read_json(sidecar) != metadata:
            raise ValueError(f'Conflicting existing result metadata: {sidecar.name}')
        _check_structure(path, metadata)
        pending.append((sidecar, metadata))
    for sidecar, metadata in pending:
        write_json(sidecar, metadata)
    for sidecar, metadata in pending:
        _validate_sidecar(directory / metadata['result_file'], read_json(sidecar), CURRENT_SCHEMA_VERSION,
                          output_contract=identity)
    return [name + '.metadata.json' for name in files]


def parse_output_formats(value):
    """Normalize the two supported delivery choices without changing Schema 2."""
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple)) or not all(isinstance(name, str) for name in value):
        raise ValueError('output_formats must be csv,npz or csv,npz,xlsx')
    names = [name.strip() for name in value]
    if len(names) != len(set(names)) or set(names) not in (set(OUTPUT_FORMATS), set(OUTPUT_FORMATS[:2])):
        raise ValueError('output_formats must be csv,npz or csv,npz,xlsx')
    return tuple(name for name in OUTPUT_FORMATS if name in names)


def result_inventory(files):
    return {*files, *(name + '.metadata.json' for name in files), 'prediction.json'}


def delivery_details(formats, report_status, outputs):
    digest = hashlib.sha256()
    for name in sorted(result_inventory(CORE_RESULT_FILES)):
        value = outputs.get(name)
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError(f'Missing computation artifact identity: {name}')
        digest.update(f'{name} {value}\n'.encode('ascii'))
    produced = OUTPUT_FORMATS if report_status == 'succeeded' else OUTPUT_FORMATS[:2]
    return {'requested_formats': list(formats), 'produced_formats': list(produced),
            'report_status': report_status, 'computation_sha256': digest.hexdigest()}


def delivered_result_files(summary):
    if 'delivery' not in summary:
        return RESULT_FILES
    delivery = summary['delivery']
    if not isinstance(delivery, dict):
        raise ValueError('Invalid result delivery declaration')
    formats = parse_output_formats(delivery.get('requested_formats'))
    status = delivery.get('report_status')
    if (not isinstance(status, str) or status not in REPORT_STATES
            or ('xlsx' in formats) == (status == 'not_requested')
            or delivery != delivery_details(formats, status, summary['outputs'])):
        raise ValueError('Invalid result delivery declaration')
    return RESULT_FILES if status == 'succeeded' else CORE_RESULT_FILES


def verify_result_metadata(directory, summary, prediction):
    context = _validate_context(prediction, CURRENT_SCHEMA_VERSION)
    keys = ('output_schema_version', 'classification_policy_id', 'output_contract_sha256')
    if (summary.get('prediction') != prediction or prediction.get('status') != 'PASS'
            or any(summary.get(key) != context[key] for key in keys)
            or summary.get('contract_sha256') != sha256(ROOT / 'config/contract.json')):
        raise ValueError('Published result and prediction contract disagree')
    lineage = summary.get('model_lineage')
    if 'model_lineage' in summary:
        validate_binding(lineage)
        if lineage['run_id'] != summary['run_id'] or lineage['input_sha256'] != summary['input']['sha256']:
            raise ValueError('Model lineage belongs to a different prediction run or input')
    for name in delivered_result_files(summary):
        sidecar = load_output_metadata(directory / name, source_version='v2')
        if (sidecar.get('model_lineage') != lineage
                or any(sidecar.get(key) != value for key, value in context.items())):
            raise ValueError(f'Result metadata conflicts with the prediction context: {name}')
    return context


def write_output_metadata(directory, *, output_contract=None, formats=OUTPUT_FORMATS, lineage=None):
    """Write and read back the sidecars for the declared result files."""
    directory = Path(directory)
    output_contract = load_output_contract() if output_contract is None else output_contract
    identity = _validate_context(read_json(directory / 'prediction.json'),
                                 output_contract['output_schema_version'], output_contract=output_contract)
    if lineage is not None:
        identity['model_lineage'] = validate_binding(lineage)
    files = RESULT_FILES if 'xlsx' in parse_output_formats(formats) else CORE_RESULT_FILES
    return _write_sidecars(directory, identity, files)
