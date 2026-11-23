"""Explicit V1-to-V2 result migration; inference and source files stay untouched."""
from contextlib import ExitStack
import csv
from itertools import zip_longest
import os
from pathlib import Path
import shutil
import stat
import tempfile

from . import __version__
from .common import ROOT, read_json, sha256, write_json
from .output_metadata import RESULT_FILES, _validate_context, load_output_contract, write_output_metadata
from .resources import CODE_ROOT


def _unlinked_path(value):
    path = Path(value).expanduser().absolute()
    for item in (path, *path.parents):
        try:
            attributes = getattr(item.lstat(), 'st_file_attributes', 0)
        except FileNotFoundError:
            continue
        if item.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError(f'Migration paths must not contain links: {item}')
    return path.resolve()


def _input_files(source):
    files = {}
    for base, directories, names in os.walk(source, followlinks=False):
        for name in directories + names:
            path = Path(base) / name
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                raise ValueError(f'Migration inputs must not contain links: {path}')
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f'Migration inputs must be regular files: {path}')
            files[path.relative_to(source).as_posix()] = {'size_bytes': info.st_size, 'sha256': sha256(path)}
    return files


def _snapshot(source, raw, identities):
    raw.mkdir()
    for name, expected in identities.items():
        target = raw / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        if target.stat().st_size != expected['size_bytes'] or sha256(target) != expected['sha256']:
            raise ValueError(f'Source changed while preserving raw input: {name}')
        target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _legacy_fields(names):
    return {
        'layer1_decisions.csv': ['row_index', 'Header', 'Length_bp', 'Stage1_Probability', 'Stage1_threshold', 'Stage1_pred', 'Scientific_status'],
        'layer2_probabilities.csv': ['row_index', 'Header', 'gate_pass'] + ['probability__' + name for name in names] + ['Scientific_status'],
        'inc_label_predictions.csv': ['row_index', 'Header', 'Inc_unclassified', 'Inc_classified', 'threshold_mode'] + names + ['Scientific_status'],
        'inc_results.csv': ['row_index', 'Header', 'Result', 'final_status', 'predicted_label_count', 'predicted_labels', 'Scientific_status'],
    }


def _load_source(raw):
    import numpy as np
    from .classify import decide
    prediction, summary = read_json(raw / 'prediction.json'), read_json(raw / 'run_summary.json')
    identity = _validate_context(prediction, 1)
    if not isinstance(summary, dict) or not isinstance(summary.get('outputs'), dict):
        raise ValueError('V1 run summary must contain an output hash mapping')
    if (summary.get('version') != '0.3.0' or summary.get('output_schema_version', 1) != 1
            or summary.get('contract_sha256') != sha256(ROOT / 'config/contract.json')
            or summary.get('execution_status') != 'PASS' or prediction.get('status') != 'PASS'
            or summary.get('status') not in ('PASS', 'RESEARCH_PREVIEW')
            or summary.get('scientific_equivalence') not in ('FROZEN_REFERENCE', 'NOT_ESTABLISHED')):
        raise ValueError('A complete, successful V1 run with its frozen scientific contract is required')
    if summary.get('prediction') != prediction:
        raise ValueError('V1 prediction and run summary disagree')
    for name in (*RESULT_FILES, 'prediction.json'):
        if summary.get('outputs', {}).get(name) != sha256(raw / name):
            raise ValueError(f'V1 source run-summary SHA-256 mismatch: {name}')
    with np.load(raw / 'probabilities.npz', allow_pickle=False) as archive:
        if set(archive.files) != {'stage1', 'stage2', 'gate', 'labels'} or len(archive.files) != 4:
            raise ValueError('Expected exactly the four original V1 arrays')
        arrays = {name: archive[name] for name in archive.files}
    expected_dtypes = {'stage1': np.dtype('float32'), 'stage2': np.dtype('float32'),
                       'gate': np.dtype('bool'), 'labels': np.dtype('uint8')}
    if any(arrays[name].dtype != dtype for name, dtype in expected_dtypes.items()):
        raise ValueError('V1 array dtypes do not match the frozen output contract')
    count = identity['record_count']
    if (arrays['stage1'].shape != (count,) or arrays['stage2'].shape != (count, 21)
            or arrays['gate'].shape != (count,) or arrays['labels'].shape != (count, 21)):
        raise ValueError('V1 array shapes and record count disagree')
    decisions = decide(arrays['stage1'], arrays['stage2'], identity['stage2_thresholds'])
    if not np.array_equal(arrays['gate'], decisions['gate']) or not np.array_equal(arrays['labels'], decisions['labels']):
        raise ValueError('V1 probabilities, gate or 21 calls are inconsistent')
    statuses = np.where(~arrays['gate'], 'Inc_unclassified',
                        np.where(arrays['labels'].any(axis=1), 'Inc_classified', 'Inc_classified_no_call'))
    if prediction.get('counts') != {name: int((statuses == name).sum()) for name in
                                     ('Inc_unclassified', 'Inc_classified_no_call', 'Inc_classified')}:
        raise ValueError('V1 native counts disagree with its original arrays')
    fields = _legacy_fields(identity['label_order'])
    if prediction.get('tables') != fields:
        raise ValueError('V1 CSV field definitions do not match the supported version')
    if prediction.get('scientific_status') not in ('UNVERIFIED', 'RESEARCH_PREVIEW', 'FROZEN_REFERENCE'):
        raise ValueError('Missing or invalid V1 scientific status')
    return prediction, summary, arrays, decisions, statuses, fields


def _validate_workbook(raw, fields, labels):
    import numpy as np
    from openpyxl import load_workbook
    numeric = {'row_index', 'Length_bp', 'Stage1_Probability', 'Stage1_threshold',
               'gate_pass', 'Inc_unclassified', 'Inc_classified', 'predicted_label_count', *labels}
    handles = ExitStack()
    titles = ('Stage1', 'Stage2_probability', 'Inc_prediction', 'Inc_result')
    position = 0
    try:
        workbook = load_workbook(handles.enter_context((raw / 'inc_predictions.xlsx').open('rb')),
                                 read_only=True, data_only=False)
        handles.callback(workbook.close)
        for (filename, columns), title in zip(fields.items(), titles):
            with (raw / filename).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                part = 1
                while position < len(workbook.sheetnames):
                    expected_title = title if part == 1 else f'{title}_{part}'
                    if workbook.sheetnames[position] != expected_title:
                        if part == 1:
                            raise ValueError(f'Missing V1 XLSX sheet: {expected_title}')
                        break
                    sheet = workbook[expected_title]
                    sheet.reset_dimensions()
                    rows = iter(sheet.values)
                    if list(next(rows, ())) != columns:
                        raise ValueError(f'V1 XLSX header mismatch: {expected_title}')
                    for values in rows:
                        expected = next(reader, None)
                        if expected is None or len(values) != len(columns):
                            raise ValueError(f'V1 XLSX/CSV row count mismatch: {expected_title}')
                        for field, value in zip(columns, values):
                            original = expected[field]
                            if (field in numeric or field.startswith('probability__')) and original != 'NA':
                                if type(value) not in (int, float):
                                    raise ValueError(f'V1 XLSX numeric type mismatch: {field}')
                                equal = (np.float32(value) == np.float32(float(original)) if
                                         field.startswith('probability__') or field == 'Stage1_Probability'
                                         else float(value) == float(original))
                            else:
                                equal = ('' if value is None else value) == original
                            if not equal:
                                raise ValueError(f'V1 XLSX/CSV value mismatch: {expected_title}/{field}')
                    position += 1
                    part += 1
                if next(reader, None) is not None:
                    raise ValueError(f'V1 XLSX is missing CSV rows: {filename}')
        if position != len(workbook.sheetnames):
            raise ValueError('Unexpected V1 XLSX sheets')
    finally:
        handles.close()


def _rewrite_tables(raw, output, prediction, arrays, decisions, statuses, fields):
    names = prediction['label_order']
    target_fields = dict(fields)
    for name in ('inc_label_predictions.csv', 'inc_results.csv'):
        target_fields[name] = fields[name][:-1] + ['gate_pass', 'internal_status', 'Scientific_status']
    for name in ('layer1_decisions.csv', 'layer2_probabilities.csv'):
        shutil.copyfile(raw / name, output / name)
    with ExitStack() as stack:
        readers, writers = [], {}
        for name, columns in fields.items():
            handle = stack.enter_context((raw / name).open(encoding='utf-8-sig', newline=''))
            reader = csv.DictReader(handle)
            if reader.fieldnames != columns:
                raise ValueError(f'V1 CSV header mismatch: {name}')
            readers.append(reader)
        for name in ('inc_label_predictions.csv', 'inc_results.csv'):
            handle = stack.enter_context((output / name).open('w', encoding='utf-8-sig', newline=''))
            writers[name] = csv.DictWriter(handle, fieldnames=target_fields[name], lineterminator='\n')
            writers[name].writeheader()
        count = 0
        for index, rows in enumerate(zip_longest(*readers)):
            if index >= prediction['record_count'] or any(
                    row is None or None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError('V1 CSV row count or columns disagree with the arrays')
            stage1, stage2, calls, result = rows
            if any(int(row['row_index']) != index or row['Header'] != stage1['Header']
                   or row['Scientific_status'] != prediction['scientific_status'] for row in rows):
                raise ValueError(f'V1 CSV row identity or scientific status mismatch at row {index}')
            gate, labels = arrays['gate'][index], arrays['labels'][index]
            joined = ';'.join(name for name, active in zip(names, labels) if active)
            native_class = 'Inc_classified' if gate else 'Inc_unclassified'
            if (int(stage1['Length_bp']) <= 0 or float(stage1['Stage1_Probability']) != float(arrays['stage1'][index])
                    or float(stage1['Stage1_threshold']) != 0.5 or stage1['Stage1_pred'] != native_class
                    or int(stage2['gate_pass']) != int(gate)):
                raise ValueError(f'V1 CSV Stage1/gate mismatch at row {index}')
            for column, label in enumerate(names):
                probability = stage2['probability__' + label]
                if ((not gate and probability != 'NA') or
                        (gate and float(probability) != float(arrays['stage2'][index, column]))):
                    raise ValueError(f'V1 CSV Stage2 mismatch at row {index}')
            if (int(calls['Inc_unclassified']) != int(not gate) or int(calls['Inc_classified']) != int(gate)
                    or calls['threshold_mode'] != prediction['threshold_mode']
                    or [int(calls[name]) for name in names] != labels.tolist()):
                raise ValueError(f'V1 CSV labels or native flags mismatch at row {index}')
            if (result['final_status'] != str(statuses[index]) or int(result['predicted_label_count']) != int(labels.sum())
                    or result['predicted_labels'] != joined or result['Result'] != (joined or str(statuses[index]))):
                raise ValueError(f'V1 CSV final state mismatch at row {index}')
            called = bool(decisions['external_classified'][index])
            trace = {'gate_pass': str(int(gate)), 'internal_status': str(decisions['internal_status'][index])}
            calls.update(Inc_unclassified=str(int(not called)), Inc_classified=str(int(called)), **trace)
            result.update(Result=joined or 'Inc Unclassified', final_status='Inc_classified' if called else 'Inc_unclassified', **trace)
            writers['inc_label_predictions.csv'].writerow(calls)
            writers['inc_results.csv'].writerow(result)
            count += 1
        if count != prediction['record_count']:
            raise ValueError('V1 CSV files are missing records')
    return target_fields


def migrate_v1_results(source_dir, destination_dir):
    """Validate a complete V1 run and publish a new V2 directory without models.

    Preserve every source file read-only under 00_raw_original, including the
    complete probability matrix. Scientific arrays keep their values and dtypes;
    only V2-derived display fields and metadata are added. Existing destinations,
    incomplete sources, links, inconsistent files and source changes are rejected.
    Raises ValueError for incompatible results; returns the migration summary.
    """
    import numpy as np
    from .report import export_workbook
    source, destination = _unlinked_path(source_dir), _unlinked_path(destination_dir)
    if not source.is_dir():
        raise ValueError('V1 source must be a complete result directory')
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('Migration destination already exists or is inside the source; refusing to overwrite')
    for protected in (ROOT, CODE_ROOT):
        if destination.is_relative_to(protected) and not destination.is_relative_to(protected / 'results'):
            raise ValueError('Migration output must not replace software, models or immutable resources')
    output_contract = load_output_contract()
    identities = _input_files(source)
    required = {*RESULT_FILES, 'prediction.json', 'run_summary.json'}
    if not required.issubset(identities):
        raise ValueError('Incomplete V1 source; all six results and both JSON records are required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.incfinder-migrate-', dir=destination.parent) as temporary:
        output = Path(temporary) / 'result'
        output.mkdir()
        raw = output / '00_raw_original'
        _snapshot(source, raw, identities)
        prediction, source_summary, arrays, decisions, statuses, fields = _load_source(raw)
        _validate_workbook(raw, fields, prediction['label_order'])
        tables = _rewrite_tables(raw, output, prediction, arrays, decisions, statuses, fields)
        # Reuse the four original arrays, rather than rounding or re-encoding them.
        np.savez(output / 'probabilities.npz', allow_pickle=False, **arrays,
                 **{name: decisions[name] for name in ('external_unclassified', 'external_classified', 'internal_status')})
        updated = {**prediction, **output_contract, 'tables': tables,
                   'counts': {name: int(decisions[key].sum()) for name, key in
                              (('Inc_unclassified', 'external_unclassified'), ('Inc_classified', 'external_classified'))},
                   'internal_counts': {name: int((decisions['internal_status'] == name).sum()) for name in
                                       ('Unclassified', 'Inc_no_call', 'Inc_called')},
                   'gate_counts': {'passed': int(arrays['gate'].sum()), 'rejected': int((~arrays['gate']).sum())}}
        write_json(output / 'prediction.json', updated)
        export_workbook(output)
        write_output_metadata(output, output_contract=output_contract)
        write_json(output / 'migration_source_manifest.json', {
            'schema': 1, 'source_directory': str(source), 'storage': 'read-only physical copies',
            'source_version': 'v1', 'files': [{'path': name, **entry} for name, entry in sorted(identities.items())],
            'array_semantics': {'stage1': 'float32 (N,)', 'stage2': 'float32 (N,21), rejected rows NaN',
                                'gate': 'bool (N,)', 'labels': 'uint8 (N,21), original label order'},
            'transformation': 'V1 native gate flags to V2 final-call flags and trace fields; no model inference',
        })
        summary = {'software': 'NT_V3_650M_TSRF_SW', 'version': __version__, 'operation': 'v1_to_v2_output_migration',
                   'execution_status': 'PASS', 'inference_performed': False,
                   'status': source_summary['status'], 'scientific_equivalence': source_summary['scientific_equivalence'],
                   'source_version': source_summary['version'], 'source_scientific_status': prediction['scientific_status'],
                   'contract_sha256': source_summary['contract_sha256'], **output_contract,
                   'prediction': updated, 'raw_original': '00_raw_original',
                   'source_manifest': 'migration_source_manifest.json',
                   'outputs': {path.name: sha256(path) for path in output.iterdir() if path.is_file()}}
        write_json(output / 'run_summary.json', summary)
        if _input_files(source) != identities:
            raise ValueError('V1 source changed during migration; results were not published')
        if destination.exists():
            raise ValueError('Migration destination appeared during processing; refusing to overwrite')
        output.rename(destination)
    return summary
