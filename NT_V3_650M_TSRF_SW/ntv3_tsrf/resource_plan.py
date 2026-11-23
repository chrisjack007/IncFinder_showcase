"""Read-only input survey and per-volume resource estimates for prediction."""
from dataclasses import dataclass
import os
from pathlib import Path
from shutil import disk_usage

from .common import record_path_value, sha256
from .record_index import identity_digest
from .sequence_io import DUPLICATE_ID, iter_records


FEATURE_BYTES_PER_ROW = 1536 * 4
RESULT_BYTES_PER_ROW = 4 + 21 * 4 + 1 + 21 + 12 * 4 + 2
_BINS = (128, 512, 2048, 8192, 10000)


@dataclass
class InputSurvey:
    facts: dict
    records_sha256: str
    outside_rows: list


def _fingerprint(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def survey_input(path, length_range, *, unique=False, keep_outside=False):
    before = _fingerprint(path)
    digest = sha256(path)
    seen = set() if unique else None
    histogram = {label: 0 for label in (*_BINS, 'above_10000')}
    outside = []
    count = bases = header_bytes = id_bytes = outside_count = length_max = 0
    length_min, kind = None, None

    def identities():
        nonlocal count, bases, header_bytes, id_bytes, outside_count, length_min, length_max, kind
        for index, record in enumerate(iter_records(path)):
            size = len(record.sequence)
            identity = record.identity(index)
            record_id = identity['record_id']
            if seen is not None:
                if record_id in seen:
                    raise ValueError(DUPLICATE_ID)
                seen.add(record_id)
            kind = record.input_format
            count += 1
            bases += size
            header_bytes += len(record.header.encode('utf-8'))
            id_bytes += len(record_id.encode('utf-8'))
            length_min = size if length_min is None else min(length_min, size)
            length_max = max(length_max, size)
            key = next((limit for limit in _BINS if size <= limit), 'above_10000')
            histogram[key] += 1
            if not length_range[0] <= size <= length_range[1]:
                outside_count += 1
                if keep_outside:
                    outside.append(index)
            yield identity

    try:
        records_sha = identity_digest(identities())
    except EOFError as exc:
        raise ValueError('Truncated gzip input during resource preflight') from exc
    if _fingerprint(path) != before:
        raise ValueError('Input file changed during resource preflight')
    return InputSurvey({
        'sha256': digest, 'stored_bytes': before[2], 'format': kind,
        'gzip': path.suffix.lower() == '.gz', 'record_count': count, 'total_bases': bases,
        'header_utf8_bytes': header_bytes, 'record_id_utf8_bytes': id_bytes,
        'length_min': length_min, 'length_max': length_max, 'outside_count': outside_count,
        'length_buckets_by_upper_bp': {str(key): value for key, value in histogram.items()},
    }, records_sha, outside)


def check_input_range(facts, length_range, max_length, allow_outside):
    if facts['length_max'] > max_length:
        raise ValueError(f'Sequence length exceeds --max-length={max_length}; sequences are never silently truncated or windowed')
    if facts['outside_count'] and not allow_outside:
        raise ValueError(f'{facts["outside_count"]} sequences fall outside the benchmark length range {length_range}; '
                         'use --allow-out-of-domain to explicitly accept extrapolation')


def temporary_directory():
    """Choose a real temporary directory without the write probe in tempfile.gettempdir."""
    candidates = [os.environ.get(name) for name in ('TMPDIR', 'TEMP', 'TMP')]
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA')
        if local:
            candidates.append(str(Path(local) / 'Temp'))
        candidates.append(r'C:\Temp')
    else:
        candidates.extend(('/tmp', '/var/tmp'))
    for value in candidates:
        if value:
            path = Path(value).expanduser().resolve()
            if path.is_dir():
                return path
    raise FileNotFoundError('No system temporary directory is available for prediction')


def _nearest_directory(path):
    candidate = path.resolve()
    while not candidate.exists():
        candidate = candidate.parent
    if not candidate.is_dir():
        raise NotADirectoryError(f'Resource preflight requires a directory: {candidate}')
    return candidate


def _disk_budget(locations, phases):
    volumes, roles = {}, {}
    for role, path in locations.items():
        existing = _nearest_directory(path)
        if not os.access(existing, os.W_OK | os.X_OK):
            raise PermissionError(f'Resource preflight: {role} directory is not writable: {existing}')
        volume = (existing.stat().st_dev, existing.anchor.casefold() if os.name == 'nt' else '')
        available = disk_usage(existing).free
        row = volumes.setdefault(volume, {'path': record_path_value(existing), 'roles': [],
                                          'available_bytes': available, 'phases': {}})
        row['available_bytes'] = min(row['available_bytes'], available)
        row['roles'].append(role)
        roles[role] = volume

    for phase, budgets in phases.items():
        for role, (minimum, estimated) in budgets.items():
            step = volumes[roles[role]]['phases'].setdefault(phase, {'required_bytes': 0, 'estimated_bytes': 0})
            step['required_bytes'] += minimum
            step['estimated_bytes'] += estimated

    warnings = []
    for volume in volumes.values():
        for phase, budget in volume['phases'].items():
            free = volume['available_bytes']
            floor = budget['required_bytes']
            if free < floor:
                raise OSError(f'Resource preflight: insufficient space for {phase} on {volume["path"]}; '
                              f'required {floor} bytes, available {free} bytes')
            if free < budget['estimated_bytes']:
                warnings.append(f'{phase} estimated workspace exceeds available space on {volume["path"]}')
    return list(volumes.values()), warnings


def _memory_observation():
    try:
        import psutil
        observed = psutil.virtual_memory().available
    except (ImportError, OSError, NotImplementedError):
        observed = None
    return {'available_bytes': observed, 'kind': 'system snapshot', 'minimum_memory_required_bytes': None}


def plan_resources(survey, *, output, run_dir, temporary, external_features, formats, asset_manifest,
                   device, batch_size, threads, jobs):
    """Estimate simultaneous files on each volume, and reject only definite space shortages."""
    facts = survey.facts
    rows = facts['record_count']
    sequence_bytes = facts['total_bases'] + facts['header_utf8_bytes'] + facts['record_id_utf8_bytes']
    array_bytes = RESULT_BYTES_PER_ROW * rows
    feature_bytes = 0 if external_features is not None else FEATURE_BYTES_PER_ROW * rows
    index = (sequence_bytes, sequence_bytes * 2 + rows * 320 + 131072)
    feature_metadata = 0 if external_features is not None else (facts['header_utf8_bytes'] + rows * 192)
    tables = facts['header_utf8_bytes'] * 6 + rows * 1536
    locations = {'output': output.parent, 'temporary': temporary}
    phases = {
        'input index': {'temporary': index},
        'prediction outputs': {'temporary': index,
                               'output': (feature_bytes + 2 * array_bytes,
                                          feature_bytes + feature_metadata + 2 * array_bytes + tables)},
    }
    if external_features is None:
        locations['checkpoints'] = run_dir
        checkpoints = (feature_bytes, feature_bytes + rows * 384)
        phases['encoding'] = {'temporary': index, 'checkpoints': checkpoints,
                              'output': (feature_bytes, feature_bytes + feature_metadata)}
        phases['prediction outputs']['checkpoints'] = checkpoints
    if 'xlsx' in formats:
        # Report XML is temporary and may be larger than the compressed workbook.
        phases['XLSX report'] = {
            'temporary': (index[0], index[1] + tables * 4),
            'output': (feature_bytes + array_bytes, feature_bytes + feature_metadata + array_bytes + tables * 2),
        }
        if external_features is None:
            phases['XLSX report']['checkpoints'] = checkpoints
    volumes, warnings = _disk_budget(locations, phases)
    asset_files = [row for row in asset_manifest['files'] if row['path'].startswith('models/')]
    memory = _memory_observation()
    memory.update(max_padded_bases_per_record=((facts['length_max'] + 127) // 128) * 128,
                  encoding_batch_size=batch_size if external_features is None else None,
                  checkpoint_row_coverage_bytes=rows if external_features is None else 0,
                  feature_validation_window_data_bytes=min(rows, 4096) * FEATURE_BYTES_PER_ROW,
                  dynamic_model_memory='depends on platform, device and active batch; measure with the locked environment')
    return {
        'status': 'PASS', 'input': facts, 'records_sha256': survey.records_sha256,
        'external_features': external_features is not None, 'requested_formats': list(formats),
        'external_feature_files_bytes': ({name: (external_features / name).stat().st_size
                                          for name in ('features.npy', 'features.json')}
                                         if external_features is not None else None),
        'device': device, 'threads': threads, 'jobs': jobs,
        'required_model_files': [{'path': row['path'], 'bytes': row['size_bytes']} for row in asset_files],
        'required_model_store_bytes': sum(row['size_bytes'] for row in asset_files),
        'retained_feature_data_bytes': feature_bytes, 'stored_result_array_data_bytes': array_bytes,
        'volumes': volumes, 'memory': memory, 'warnings': warnings,
    }
