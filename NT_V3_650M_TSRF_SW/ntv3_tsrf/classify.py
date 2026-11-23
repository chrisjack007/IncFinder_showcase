"""Frozen grid_0001 RF inference and the four operational tables."""
import argparse
import csv
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import warnings
from .common import ROOT, environment, load_thresholds, read_json, sha256, write_json
from .feature_metadata import matches_feature_contract, read_feature_metadata
from .output_metadata import load_output_contract
from .resources import model_root
from .record_index import RecordIndex, identity_digest, indexed_records



def prepare_model_loading():
    """Budget mmap descriptors in this RF worker, without raising the hard limit."""
    try:
        import resource
    except ImportError:
        return {'status': 'NOT_APPLICABLE'}
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    requested = 32768  # Frozen multioutput trees retain many small mmap arrays.
    effective = soft
    if soft != resource.RLIM_INFINITY and soft < requested:
        effective = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        if effective > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (effective, hard))
    result = {'soft_before': soft, 'soft_effective': effective, 'hard_unchanged': hard,
              'requested_soft': requested, 'scope': 'RF worker only'}
    print(f'RF file descriptor budget: {result}', flush=True)
    return result


def validate_features(directory, records):
    import numpy as np
    metadata = directory / 'features.json'
    info, count, records_sha = read_feature_metadata(metadata)
    if not matches_feature_contract(info, metadata, ROOT, sha256(ROOT / 'config/contract.json'),
                                    sha256(ROOT / 'config/assets.json')):
        raise ValueError('Features are bound to a different encoder or model contract')
    identities = records.identities() if isinstance(records, RecordIndex) else (
        record.identity(index) for index, record in enumerate(records)
    )
    if count != len(records) or records_sha != identity_digest(identities):
        raise ValueError('Precomputed features do not match input IDs, order or sequence hashes')
    path = directory / 'features.npy'
    if info['features_sha256'] != sha256(path):
        raise ValueError('Feature SHA-256 mismatch')
    values = np.load(path, mmap_mode='r', allow_pickle=False)
    if values.shape != (len(records), 1536) or values.dtype != np.float32:
        raise ValueError('Features must be a float32 matrix with shape (record_count, 1536)')
    for start in range(0, len(values), 4096):
        if not np.isfinite(values[start:start + 4096]).all():
            raise ValueError('Features contain NaN/Inf')
    return values


def positive_probability(model, features, multilabel=False):
    import numpy as np
    probabilities = model.predict_proba(features)
    if multilabel:
        if not isinstance(probabilities, list) or len(probabilities) != 21:
            raise ValueError('Stage2 must return 21 multioutput probability arrays')
        columns = []
        for classes, values in zip(model.classes_, probabilities):
            classes = np.asarray(classes)
            if not set(classes.tolist()).issubset({0, 1}):
                raise ValueError('Stage2 classes must be 0 or 1')
            positions = np.flatnonzero(classes == 1)
            columns.append(values[:, positions[0]] if len(positions) == 1 else np.zeros(len(features)))
        result = np.column_stack(columns).astype(np.float32)
    else:
        if isinstance(probabilities, list) or list(model.classes_) != [0, 1]:
            raise ValueError('Stage1 positive class must be 1=Inc_classified')
        result = np.asarray(probabilities[:, 1], dtype=np.float32)
    if not np.isfinite(result).all() or ((result < 0) | (result > 1)).any():
        raise ValueError('Invalid RF probabilities')
    return result


def decide(stage1, stage2, thresholds):
    """Derive native states and final-21-zero public calls exactly once."""
    import numpy as np
    stage1, stage2 = np.asarray(stage1), np.asarray(stage2)
    thresholds = np.asarray(thresholds, dtype=np.float64)
    if stage1.ndim != 1 or not stage1.size or stage2.shape != (stage1.size, 21):
        raise ValueError('Expected Stage1 shape (N,) and Stage2 shape (N, 21), with N > 0')
    if stage1.dtype.kind not in 'fiu' or stage2.dtype.kind not in 'fiu':
        raise ValueError('RF probabilities must be numeric arrays')
    if not np.isfinite(stage1).all() or ((stage1 < 0) | (stage1 > 1)).any():
        raise ValueError('Stage1 probabilities must be finite and in [0, 1]')
    if thresholds.shape != (21,) or not np.isfinite(thresholds).all() or ((thresholds < 0) | (thresholds > 1)).any():
        raise ValueError('Expected 21 finite thresholds in [0, 1]')
    gate = stage1 >= 0.5
    selected = stage2[gate]
    if not np.isfinite(selected).all() or ((selected < 0) | (selected > 1)).any():
        raise ValueError('Gate-passing Stage2 probabilities must be finite and in [0, 1]')
    if not np.isnan(stage2[~gate]).all():
        raise ValueError('Gate-rejected Stage2 probabilities must be NaN')
    labels = np.zeros(stage2.shape, dtype=np.uint8)
    labels[gate] = (selected.astype(np.float64) >= thresholds).astype(np.uint8)
    external_classified = labels.any(axis=1)
    external_unclassified = ~external_classified
    internal_status = np.where(~gate, 'Unclassified',
                               np.where(external_unclassified, 'Inc_no_call', 'Inc_called'))
    return {'gate': gate, 'labels': labels, 'internal_status': internal_status,
            'external_unclassified': external_unclassified, 'external_classified': external_classified}


def safe_text(value):
    # CSV values can be opened by Excel; quote formula-like user headers as text.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def _table_fields(names):
    tables = {
        'layer1_decisions.csv': ['row_index', 'Header', 'Length_bp', 'Stage1_Probability', 'Stage1_threshold', 'Stage1_pred'],
        'layer2_probabilities.csv': ['row_index', 'Header', 'gate_pass'] + ['probability__' + n for n in names],
        'inc_label_predictions.csv': ['row_index', 'Header', 'Inc_unclassified', 'Inc_classified', 'threshold_mode'] + names + ['gate_pass', 'internal_status'],
        'inc_results.csv': ['row_index', 'Header', 'Result', 'final_status', 'predicted_label_count', 'predicted_labels', 'gate_pass', 'internal_status'],
    }
    return {name: fields + ['Scientific_status'] for name, fields in tables.items()}


def _table_row(index, header, length, stage1, stage2, decisions, names, mode, scientific_status, *, first_row=0):
    header = safe_text(header)
    gate = bool(decisions['gate'][index])
    labels = decisions['labels'][index]
    unclassified = bool(decisions['external_unclassified'][index])
    classified = bool(decisions['external_classified'][index])
    status = str(decisions['internal_status'][index])
    stage1_class = 'Inc_classified' if gate else 'Inc_unclassified'
    calls = ';'.join(name for name, active in zip(names, labels) if active)
    final_status = 'Inc_classified' if classified else 'Inc_unclassified'
    result = calls if classified else 'Inc Unclassified'
    row = first_row + index
    return (
        [row, header, length, float(stage1[index]), 0.5, stage1_class, scientific_status],
        [row, header, int(gate)] + [float(v) if gate else 'NA' for v in stage2[index]] + [scientific_status],
        [row, header, int(unclassified), int(classified), mode] + labels.tolist() + [int(gate), status, scientific_status],
        [row, header, result, final_status, int(labels.sum()), calls, int(gate), status, scientific_status],
    )


@contextmanager
def _table_writers(output, tables):
    with ExitStack() as stack:
        writers = []
        for filename, fields in tables.items():
            handle = stack.enter_context((output / filename).open('w', encoding='utf-8-sig', newline=''))
            writer = csv.writer(handle, lineterminator='\n')
            writer.writerow(fields)
            writers.append(writer)
        yield writers


def _write_table_row(writers, rows):
    for writer, row in zip(writers, rows):
        writer.writerow(row)


def write_tables(records, stage1, stage2, decisions, names, output, mode, scientific_status='UNVERIFIED'):
    if len(records) != len(decisions['gate']) or len(names) != 21:
        raise ValueError('Output records and label order must match the decision arrays')
    tables = _table_fields(names)
    with _table_writers(output, tables) as writers:
        for index, record in enumerate(records):
            _write_table_row(writers, _table_row(index, record.header, len(record.sequence), stage1, stage2,
                                                 decisions, names, mode, scientific_status))
    return tables


def _gated_batches(stage1, chunk_size):
    import numpy as np

    pending = np.empty(chunk_size, dtype=np.intp)
    used = 0
    for start in range(0, len(stage1), chunk_size):
        indices = np.flatnonzero(stage1[start:start + chunk_size] >= 0.5) + start
        take = min(chunk_size - used, len(indices))
        pending[used:used + take] = indices[:take]
        used += take
        if used == chunk_size:
            yield pending.copy()
            rest = indices[take:]
            used = len(rest)
            pending[:used] = rest
    if used:
        yield pending[:used].copy()


def _probabilities(stage1, stage2, features, directory, chunk_size, jobs):
    import gc
    from itertools import chain
    import joblib

    model = joblib.load(directory / 'rf/stage1.joblib', mmap_mode='r')
    if model.n_features_in_ != 1536 or model.n_outputs_ != 1 or len(model.estimators_) != 500:
        raise ValueError('Stage1 model structure does not match the frozen contract')
    model.n_jobs = jobs
    for start in range(0, len(stage1), chunk_size):
        stage1[start:start + chunk_size] = positive_probability(model, features[start:start + chunk_size])
    del model
    gc.collect()
    stage2.fill(float('nan'))
    selected = _gated_batches(stage1, chunk_size)
    first = next(selected, None)
    if first is None:
        return
    passed = sum(int((stage1[i:i + chunk_size] >= 0.5).sum()) for i in range(0, len(stage1), chunk_size))
    print(f'Loading Stage2 RF, {passed}/{len(stage1)} records pass Stage1', flush=True)
    model = joblib.load(directory / 'rf/stage2.joblib', mmap_mode='r')
    if model.n_features_in_ != 1536 or model.n_outputs_ != 21 or len(model.estimators_) != 500:
        raise ValueError('Stage2 model structure does not match the frozen contract')
    model.n_jobs = jobs
    for indices in chain((first,), selected):
        stage2[indices] = positive_probability(model, features[indices], multilabel=True)
    del model
    gc.collect()


@contextmanager
def _result_arrays(folder, count):
    import numpy as np

    layout = {'stage1': ((), 'float32'), 'stage2': ((21,), 'float32'), 'gate': ((), 'bool'),
              'labels': ((21,), 'uint8'), 'internal_status': ((), '<U12'),
              'external_unclassified': ((), 'bool'), 'external_classified': ((), 'bool')}
    maps = {}
    try:
        for name, (tail, dtype) in layout.items():
            maps[name] = np.lib.format.open_memmap(folder / (name + '.npy'), mode='w+', dtype=dtype,
                                                  shape=(count, *tail))
        yield maps
    finally:
        # These mappings own their files; the caller's block views are gone.
        # Closing here also releases Windows file handles on a worker failure.
        for array in maps.values():
            array._mmap.close()


def write_block_results(output, scratch, arrays, records, names, thresholds, mode, scientific_status, chunk_size):
    from zipfile import ZipFile

    statuses = {name: 0 for name in ('Unclassified', 'Inc_no_call', 'Inc_called')}
    gate_passed = 0
    headers = iter(records.headers())
    tables = _table_fields(names)
    with _table_writers(output, tables) as writers:
        for start in range(0, len(records), chunk_size):
            window = slice(start, start + chunk_size)
            stage1, stage2 = arrays['stage1'][window], arrays['stage2'][window]
            decisions = decide(stage1, stage2, thresholds)
            for name, values in decisions.items():
                arrays[name][window] = values
            gate_passed += int(decisions['gate'].sum())
            for name in statuses:
                statuses[name] += int((decisions['internal_status'] == name).sum())
            for index in range(len(stage1)):
                header, length = next(headers)
                _write_table_row(writers, _table_row(index, header, length, stage1, stage2, decisions,
                                                     names, mode, scientific_status, first_row=start))
    for array in arrays.values():
        array.flush()
    with ZipFile(output / 'probabilities.npz', 'w', allowZip64=True) as archive:
        for name in arrays:
            filename = name + '.npy'
            archive.write(scratch / filename, filename)
    return tables, statuses, gate_passed


def classify(input_path, features_dir, output, mode='ct', chunk_size=1024, jobs=1, scientific_status='UNVERIFIED',
             model_dir=None, *, index_path=None, index_sha256=None):
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError('chunk_size must be a positive integer')
    output_contract = load_output_contract()
    descriptor_limit = prepare_model_loading()
    directory = model_root(model_dir)
    versions = environment('rf')
    from sklearn.exceptions import InconsistentVersionWarning
    warnings.simplefilter('error', InconsistentVersionWarning)
    contract = read_json(ROOT / 'config/contract.json')
    thresholds = load_thresholds(contract) if mode == 'ct' else [0.5] * 21
    with indexed_records(input_path, index_path=index_path, index_sha256=index_sha256) as records:
        features = validate_features(features_dir, records)
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.incfinder-rf-', dir=output.parent) as folder:
            scratch = Path(folder)
            with _result_arrays(scratch, len(records)) as arrays:
                _probabilities(arrays['stage1'], arrays['stage2'], features, directory, chunk_size, jobs)
                output.mkdir(parents=True, exist_ok=True)
                tables, status, passed = write_block_results(output, scratch, arrays, records, contract['labels'],
                                                             thresholds, mode, scientific_status, chunk_size)
        count = len(records)
    write_json(output / 'prediction.json', {
        'status': 'PASS', 'scientific_status': scientific_status, 'record_count': count, 'threshold_mode': mode,
        **output_contract,
        'stage1_threshold': 0.5, 'stage2_thresholds': thresholds, 'label_order': contract['labels'],
        'counts': {'Inc_unclassified': count - status['Inc_called'], 'Inc_classified': status['Inc_called']},
        'internal_counts': status, 'gate_counts': {'passed': passed, 'rejected': count - passed},
        'environment': versions, 'tables': tables, 'file_descriptor_limit': descriptor_limit,
    })


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('features', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--threshold-mode', choices=['ct', 'u05'], default='ct')
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--scientific-status', choices=['UNVERIFIED', 'RESEARCH_PREVIEW', 'FROZEN_REFERENCE'], default='UNVERIFIED')
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--record-index', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--index-sha256', help=argparse.SUPPRESS)
    args = parser.parse_args()
    classify(args.input, args.features, args.output, args.threshold_mode, jobs=args.jobs,
             scientific_status=args.scientific_status, model_dir=args.model_dir,
             index_path=args.record_index, index_sha256=args.index_sha256)
