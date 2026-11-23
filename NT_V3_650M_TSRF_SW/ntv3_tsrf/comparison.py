"""Compare sealed Run results without modifying either source."""
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory

from .artifact_lifecycle import inspect_artifacts
from .artifact_manifest import artifact_path
from .common import ROOT, read_json, sha256, write_json
from .feature_metadata import matches_feature_contract, read_feature_metadata, verify_feature_header
from .output_metadata import CSV_RESULT_FILES, WORKBOOK, verify_result_metadata
from .research_archive import RECEIPT, load_research_archive
from .reproduction import _artifact
from .run_record import output_path, publish_output, reserve_output

CPU_PROFILE = 'ntv3_650m_cpu_fp32_v1'
_IDENTITY = ('output_schema_version', 'classification_policy_id', 'output_contract_sha256', 'threshold_mode',
             'stage1_threshold', 'stage2_thresholds', 'label_order', 'record_count', 'scientific_status')


def _open(path, expected, references):
    if (path / RECEIPT).is_file():
        return load_research_archive(path, expected_manifest_sha256=expected, reference_paths=references)[1]
    return inspect_artifacts(path, expected_manifest_sha256=expected, reference_paths=references)


def _inventory(view):
    entries = {}
    for item in view.manifest['artifacts']:
        if item['role'] in ('input', 'features', 'feature_metadata'):
            name = 'dependency:' + item['role']
        else:
            name = item['path']
        key = item['artifact_id']
        entries[name] = {'sha256': item['sha256'], 'size_bytes': item['size_bytes'],
                         'availability': view.missing.get(key, 'present')}
    return entries


def _bytes(left, right):
    result = {}
    for name in sorted(left.keys() | right.keys()):
        lft, rgt = left.get(name), right.get(name)
        if lft is None or rgt is None:
            comparison = 'missing'
        elif lft['availability'] != 'present' or rgt['availability'] != 'present':
            comparison = 'unavailable'
        else:
            comparison = 'identical' if lft == rgt else 'different'
        result[name] = {'baseline': lft, 'candidate': rgt, 'comparison': comparison}
    return result


def _record_order(view, record_count):
    metadata = view.paths.get(_artifact(view, 'feature_metadata')['artifact_id'])
    if metadata is None:
        return None, None
    info, count, order = read_feature_metadata(metadata)
    if (count != record_count or info.get('shape') != [count, 1536] or info.get('dtype') != 'float32'
            or info.get('features_sha256') != _artifact(view, 'features')['sha256']
            or not matches_feature_contract(info, metadata, ROOT, view.summary.get('contract_sha256'),
                                            view.manifest['model_manifest_sha256'])):
        raise ValueError('Feature identity, shape or contract does not agree with the sealed result')
    return info, order


def _tolerance(profile, infos, views):
    if profile is None:
        return None
    if profile != CPU_PROFILE:
        raise ValueError('Unknown declared embedding profile')
    contract_path = ROOT / 'config/encoder_versions' / (CPU_PROFILE + '.json')
    contract = read_json(contract_path)
    encoding = contract['encoding']
    if (any(info.get(key) != encoding[key] for info in infos for key in ('device', 'batch_size', 'threads', 'dtype'))
            or any(info.get('environment', {}).get('python') != encoding['python'] for info in infos)
            or infos[0]['environment'] != infos[1]['environment']
            or any(v.summary['contract_sha256'] != contract['baseline_scientific_contract_sha256']
                   or v.manifest['model_manifest_sha256'] != contract['frozen_assets_manifest_sha256'] for v in views)):
        raise ValueError('Feature metadata does not support the requested CPU embedding profile')
    settings = contract['validation']['feature_tolerance']
    return {**settings, 'profile': profile, 'contract_sha256': sha256(contract_path),
            'scope': 'Compare recorded CPU FP32 settings. This does not establish cohort or lock attestation.'}


def _array_delta(first, second):
    import numpy as np

    identity = {'baseline_shape': list(first.shape), 'candidate_shape': list(second.shape),
                'baseline_dtype': str(first.dtype), 'candidate_dtype': str(second.dtype)}
    if first.shape != second.shape or first.dtype != second.dtype:
        return {**identity, 'equal': False, 'structure_equal': False}
    float_array = np.issubdtype(first.dtype, np.floating)
    if float_array:
        na1, na2 = np.isnan(first), np.isnan(second)
        same = (first == second) | (na1 & na2)
        finite = np.isfinite(first) & np.isfinite(second)
        max_abs = float(np.abs(first[finite].astype(np.float64) - second[finite]).max(initial=0))
        mask = na1 == na2
        identity.update(nan_mask_equal=bool(mask.all()), max_abs_difference=max_abs,
                        nan_mask_rows=np.flatnonzero(~mask.reshape(len(mask), -1).all(axis=1)).tolist())
    else:
        same = first == second
    rows = np.flatnonzero(~same.reshape(len(same), -1).all(axis=1)).tolist()
    return {**identity, 'structure_equal': True, 'equal': not rows,
            'different_elements': int((~same).sum()), 'different_rows': rows}


def _embeddings(views, infos, count, profile):
    import numpy as np

    tolerance = _tolerance(profile, infos, views)
    paths = [v.paths.get(_artifact(v, 'features')['artifact_id']) for v in views]
    if any(path is None for path in paths):
        return {'status': 'unavailable', 'shape': [count, 1536], 'dtype': 'float32'}
    limits = tolerance or {'atol': 0.0, 'rtol': 0.0}
    for path in paths:
        verify_feature_header(path, count)
    left, right = (np.load(path, mmap_mode='r', allow_pickle=False) for path in paths)
    unequal, outside, row_max, failed_rows = 0, 0, [], []
    for start in range(0, count, 256):
        x, y = (part[start:start+256].astype(np.float64) for part in (left, right))
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError('Embeddings must be finite for numeric comparison')
        difference = np.abs(y - x)
        failed = difference > limits['atol'] + limits['rtol'] * np.abs(x)
        unequal += int((difference != 0).sum())
        outside += int(failed.sum())
        failed_rows.extend((start + np.flatnonzero(failed.any(axis=1))).tolist())
        row_max.extend(difference.max(axis=1).tolist())
    return {'status': 'different' if outside else 'within_tolerance' if unequal else 'identical',
            'shape': [count, 1536], 'dtype': 'float32', 'tolerance': tolerance,
            'max_abs_difference': max(row_max), 'max_abs_difference_by_row': row_max,
            'different_elements': unequal, 'outside_tolerance_elements': outside, 'outside_tolerance_rows': failed_rows}


def _xlsx(left, right, inventory):
    if WORKBOOK not in inventory:
        return {'status': 'not_delivered'}
    if inventory[WORKBOOK]['comparison'] == 'missing':
        return {'status': 'delivery_differs'}
    if inventory[WORKBOOK]['comparison'] == 'identical':
        return {'status': 'identical', 'different_cells': 0}
    from openpyxl import load_workbook

    first = load_workbook(left / WORKBOOK, read_only=True)
    try:
        second = load_workbook(right / WORKBOOK, read_only=True)
        try:
            if first.sheetnames != second.sheetnames:
                return {'status': 'different', 'baseline_sheets': first.sheetnames, 'candidate_sheets': second.sheetnames}
            by_sheet = {}
            for name in first.sheetnames:
                count = 0
                for a, b in zip_longest(first[name].iter_rows(), second[name].iter_rows(), fillvalue=()):
                    for ca, cb in zip_longest(a, b):
                        va, vb = getattr(ca, 'value', None), getattr(cb, 'value', None)
                        if va != vb or type(va) is not type(vb) or (va is not None and ca.data_type != cb.data_type):
                            count += 1
                by_sheet[name] = count
            total = sum(by_sheet.values())
            return {'status': 'different' if total else 'identical', 'different_cells': total, 'by_sheet': by_sheet}
        finally:
            second.close()
    finally:
        first.close()


def _comparison(views, profile):
    identities, contexts, records = [], [], []
    for view in views:
        if 'missing' in view.missing.values():
            raise ValueError('A comparison source has a missing artifact without retention evidence')
        summary = view.summary
        if summary is None:
            raise ValueError('A verified Run summary is required for comparison')
        context = verify_result_metadata(view.root, summary, read_json(view.root / 'prediction.json'))
        meta, order = _record_order(view, context['record_count'])
        records.append(meta)
        contexts.append(context)
        identities.append({'run_id': view.manifest['run_id'], 'manifest_sha256': view.digest,
                           'input_sha256': _artifact(view, 'input')['sha256'], 'records_sha256': order,
                           'scientific_contract_sha256': summary.get('contract_sha256'),
                           'runtime_policy_sha256': summary.get('runtime_policy_sha256'),
                           'model_manifest_sha256': view.manifest['model_manifest_sha256'],
                           **{key: context[key] for key in _IDENTITY}})
    first, second = identities
    inventory = _bytes(*(_inventory(view) for view in views))
    mismatches = [key for key in first if key not in ('run_id', 'manifest_sha256') and first[key] != second[key]]
    if any(item is None for item in records):
        mismatches.append('ordered_feature_metadata_unavailable')
    output = {'schema_version': 1, 'baseline': first, 'candidate': second, 'artifacts': inventory,
              'incomparable_reasons': mismatches}
    if mismatches:
        return dict(output, status='not_comparable')
    count = contexts[0]['record_count']
    embedding = _embeddings(views, records, count, profile)
    import numpy as np

    with np.load(views[0].root / 'probabilities.npz', allow_pickle=False) as left, \
            np.load(views[1].root / 'probabilities.npz', allow_pickle=False) as right:
        arrays = {name: _array_delta(left[name], right[name]) for name in left.files}
    workbook = _xlsx(views[0].root, views[1].root, inventory)
    csv_equal = all(inventory[name]['comparison'] == 'identical' for name in CSV_RESULT_FILES)
    decisions_equal = all(item['equal'] for name, item in arrays.items() if name not in ('stage1', 'stage2'))
    probabilities_equal = all(arrays[name]['equal'] for name in ('stage1', 'stage2'))
    if not (probabilities_equal and decisions_equal and csv_equal and workbook['status'] in ('not_delivered', 'identical')):
        status = 'different'
    else:
        status = embedding['status']
    return dict(output, status=status, embedding=embedding, arrays=arrays, decisions_equal=decisions_equal,
                probabilities_equal=probabilities_equal, csv_equal=csv_equal, xlsx=workbook)


def _summary(result):
    lines = ['# IncFinder comparison', '', f"Result: {result['status']}", '',
             f"Baseline Run: {result['baseline']['run_id']}", f"Candidate Run: {result['candidate']['run_id']}",
             f"Threshold policy: {result['baseline']['threshold_mode']} / {result['candidate']['threshold_mode']}",
             f"Ordered input rows: {result['baseline']['record_count']} / {result['candidate']['record_count']}", '']
    if result['incomparable_reasons']:
        lines.append('Incompatible identities: ' + ', '.join(result['incomparable_reasons']))
    else:
        lines += [f"Embedding: {result['embedding']['status']}",
                  f"Probabilities equal: {result['probabilities_equal']}", f"Decisions equal: {result['decisions_equal']}",
                  f"CSV bytes equal: {result['csv_equal']}", f"XLSX cells: {result['xlsx']['status']}"]
        lines += [f"- {name} ({value['baseline_dtype']}): {value['equal']}" for name, value in result['arrays'].items()]
    lines += ['', 'See comparison.json for file SHA-256, shapes, dtypes and zero-based row differences.', '']
    return '\n'.join(lines)


def compare_results(baseline, candidate, *, destination=None, baseline_reference_paths=None,
                    candidate_reference_paths=None, baseline_manifest_sha256=None, candidate_manifest_sha256=None,
                    encoder_profile=None):
    """Compare results from two verified directories or archives, with a fixed ordering."""
    left, right = artifact_path(baseline), artifact_path(candidate)
    views = (_open(left, baseline_manifest_sha256, baseline_reference_paths),
             _open(right, candidate_manifest_sha256, candidate_reference_paths))
    result = _comparison(views, encoder_profile)
    if destination is not None:
        target = output_path(destination, None)
        if target.is_relative_to(left) or target.is_relative_to(right):
            raise ValueError('Comparison output must be outside both sources')
        with reserve_output(target, views[0].manifest['run_id']), TemporaryDirectory(
                prefix='.incfinder-compare-', dir=target.parent) as tmp:
            staged = Path(tmp) / 'comparison'
            staged.mkdir()
            write_json(staged / 'comparison.json', result)
            (staged / 'comparison.md').write_text(_summary(result), encoding='utf-8', newline='\n')
            if read_json(staged / 'comparison.json') != result:
                raise ValueError('Comparison report failed read-back')
            publish_output(staged, target)
    return result
