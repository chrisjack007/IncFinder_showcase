"""Small result fixtures derived from the original frozen probabilities."""
import csv
from pathlib import Path

import numpy as np

from ntv3_tsrf.classify import decide, load_thresholds, write_tables
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.report import export_workbook
from ntv3_tsrf.sequence_io import read_records


def make_result(directory, version='v2', mode='ct', scientific_status='FROZEN_REFERENCE'):
    """Generate model-free V1/V2 result fixtures; V1 retains native gate flags."""
    directory = Path(directory)
    directory.mkdir()
    contract = read_json(ROOT / 'config/contract.json')
    policy = read_json(ROOT / 'config/output_contract.json')
    with np.load(ROOT / 'tests/fixtures/expected.npz', allow_pickle=False) as reference:
        stage1, stage2 = reference['stage1'], reference['stage2']
    thresholds = load_thresholds(contract) if mode == 'ct' else [0.5] * 21
    decisions = decide(stage1, stage2, thresholds)
    records = read_records(ROOT / 'tests/fixtures/reference.fasta')
    tables = write_tables(records, stage1, stage2, decisions, contract['labels'], directory, mode, scientific_status)
    prediction = {'status': 'PASS', 'scientific_status': scientific_status, 'record_count': len(records),
                  'threshold_mode': mode, 'stage1_threshold': 0.5, 'stage2_thresholds': thresholds,
                  'label_order': contract['labels'], 'tables': tables,
                  'counts': {'Inc_unclassified': int(decisions['external_unclassified'].sum()),
                             'Inc_classified': int(decisions['external_classified'].sum())},
                  'output_schema_version': 2, 'classification_policy_id': policy['classification_policy_id'],
                  'output_contract_sha256': sha256(ROOT / 'config/output_contract.json')}
    native = {'stage1': stage1, 'stage2': stage2, 'gate': decisions['gate'], 'labels': decisions['labels']}
    if version == 'v1':
        statuses = np.where(~decisions['gate'], 'Inc_unclassified',
                            np.where(decisions['labels'].any(axis=1), 'Inc_classified', 'Inc_classified_no_call'))
        for filename in ('inc_label_predictions.csv', 'inc_results.csv'):
            with (directory / filename).open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            fields = [name for name in tables[filename] if name not in ('gate_pass', 'internal_status')]
            tables[filename] = fields
            for index, row in enumerate(rows):
                row.pop('gate_pass')
                row.pop('internal_status')
                if filename == 'inc_label_predictions.csv':
                    row['Inc_unclassified'] = str(int(not decisions['gate'][index]))
                    row['Inc_classified'] = str(int(decisions['gate'][index]))
                else:
                    row['final_status'] = str(statuses[index])
                    row['Result'] = row['predicted_labels'] or str(statuses[index])
            with (directory / filename).open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
                writer.writeheader()
                writer.writerows(rows)
        for name in ('output_schema_version', 'classification_policy_id', 'output_contract_sha256'):
            prediction.pop(name)
        prediction['counts'] = {name: int((statuses == name).sum()) for name in
                                ('Inc_unclassified', 'Inc_classified_no_call', 'Inc_classified')}
        np.savez(directory / 'probabilities.npz', allow_pickle=False, **native)
    else:
        np.savez(directory / 'probabilities.npz', allow_pickle=False, stage1=stage1, stage2=stage2, **decisions)
    write_json(directory / 'prediction.json', prediction)
    export_workbook(directory)
    summary = {'software': 'NT_V3_650M_TSRF_SW', 'version': '0.3.0' if version == 'v1' else '0.4.0',
               'execution_status': 'PASS', 'status': 'RESEARCH_PREVIEW' if scientific_status == 'RESEARCH_PREVIEW' else 'PASS',
               'scientific_equivalence': 'NOT_ESTABLISHED' if scientific_status == 'RESEARCH_PREVIEW' else 'FROZEN_REFERENCE',
               'contract_sha256': sha256(ROOT / 'config/contract.json'), 'prediction': prediction,
               'outputs': {path.name: sha256(path) for path in directory.iterdir() if path.is_file()}}
    if version == 'v2':
        summary.update({name: prediction[name] for name in
                        ('output_schema_version', 'classification_policy_id', 'output_contract_sha256')})
    write_json(directory / 'run_summary.json', summary)
    return prediction, native, decisions


def rebind_summary(directory):
    """Keep container hashes consistent when testing deeper semantic failures."""
    summary = read_json(directory / 'run_summary.json')
    summary['prediction'] = read_json(directory / 'prediction.json')
    summary['outputs'] = {path.name: sha256(path) for path in directory.iterdir()
                          if path.is_file() and path.name != 'run_summary.json'}
    write_json(directory / 'run_summary.json', summary)
