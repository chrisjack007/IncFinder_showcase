"""Repeatable 11-record CPU acceptance. Run payloads belong in system temp."""
import argparse
import csv
import gzip
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import numpy as np
from ntv3_tsrf.common import ROOT, read_json, write_json, sha256, runtime_diagnostics
from ntv3_tsrf.sequence_io import read_records, ordered_identity

CASES = ('frozen', 'fasta_first', 'fasta_repeat', 'fastq', 'fasta_gz', 'fastq_gz')
ARRAY_KEYS = ('stage1', 'stage2', 'gate', 'labels', 'external_unclassified', 'external_classified', 'internal_status')


def matrix_difference(actual, expected, atol=0.0, rtol=0.0):
    if actual.shape != expected.shape or actual.dtype != expected.dtype:
        return {'shape_dtype_equal': False, 'allclose': False, 'exact': False}
    if actual.dtype.kind in 'US':
        exact = bool(np.array_equal(actual, expected))
        return {'shape_dtype_equal': True, 'nan_mask_equal': True,
                'exact': exact, 'allclose': exact, 'max_abs': None}
    finite = np.isfinite(actual) & np.isfinite(expected)
    return {'shape_dtype_equal': True,
            'nan_mask_equal': bool(np.array_equal(np.isnan(actual), np.isnan(expected))),
            'exact': bool(np.array_equal(actual, expected, equal_nan=True)),
            'allclose': bool(np.allclose(actual, expected, atol=atol, rtol=rtol, equal_nan=True)),
            'max_abs': float(np.max(np.abs(actual[finite].astype(float)-expected[finite].astype(float)))) if finite.any() else 0.0}


def result_difference(actual, expected):
    with np.load(actual/'probabilities.npz', allow_pickle=False) as a, np.load(expected/'probabilities.npz', allow_pickle=False) as b:
        difference = {key: matrix_difference(a[key], b[key]) for key in ARRAY_KEYS}
        difference['label_disagreements'] = int(np.count_nonzero(a['labels'] != b['labels']))
    csv_equal = {}
    for filename in ('layer1_decisions.csv','layer2_probabilities.csv','inc_label_predictions.csv','inc_results.csv'):
        def rows(path):
            with path.open(encoding='utf-8-sig', newline='') as handle: return list(csv.reader(handle))
        csv_equal[filename] = rows(actual/filename) == rows(expected/filename)
    from openpyxl import load_workbook
    def cells(path):
        workbook = load_workbook(path, read_only=True, data_only=False)
        result = {sheet.title: list(sheet.values) for sheet in workbook}
        workbook.close()
        return result
    difference['csv_equal'] = csv_equal
    difference['xlsx_cells_equal'] = cells(actual/'inc_predictions.xlsx') == cells(expected/'inc_predictions.xlsx')
    return difference


def format_accepted(difference):
    """RF outputs/tables stay exact; re-encodings use the frozen contract tolerance."""
    return (difference['features']['allclose']
            and all(difference[key]['exact'] for key in ARRAY_KEYS)
            and all(difference['csv_equal'].values()) and difference['xlsx_cells_equal'])


def measured(command, cwd, log):
    import psutil
    environment = os.environ.copy()
    environment.update(CUDA_VISIBLE_DEVICES='-1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    start = time.perf_counter()
    peak = 0
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        monitored = psutil.Process(process.pid)
        while process.poll() is None:
            try:
                total = 0
                for item in [monitored, *monitored.children(recursive=True)]:
                    try: total += item.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                peak = max(peak, total)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            time.sleep(0.1)
    return {'exit_code': process.returncode, 'wall_seconds': time.perf_counter()-start,
            'sampled_process_tree_peak_rss_bytes': peak, 'sampling_seconds': 0.1,
            'memory_definition': 'sum of process-tree RSS; shared pages may be counted more than once'}


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = ROOT/'tests/fixtures'
    records = read_records(fixture/'reference.fasta')
    inputs = output/'inputs'; inputs.mkdir(exist_ok=True)
    fasta = ''.join('>'+r.header+'\n'+r.sequence+'\n' for r in records).encode()
    fastq = ''.join('@'+r.header+'\n'+r.sequence+'\n+\n'+'I'*len(r.sequence)+'\n' for r in records).encode()
    for name, data in (('reference.fasta',fasta),('reference.fastq',fastq),
                       ('reference.fasta.gz',gzip.compress(fasta,mtime=0)),('reference.fastq.gz',gzip.compress(fastq,mtime=0))):
        path=inputs/name
        if path.exists() and path.read_bytes() != data:
            raise ValueError(f'Existing acceptance input differs: {path}')
        if not path.exists(): path.write_bytes(data)
        assert [(r.header,r.sequence) for r in read_records(path)] == [(r.header,r.sequence) for r in records]
    receipt_path = output/'acceptance.json'
    current_runtime = runtime_diagnostics()
    installed_manifest = sha256(ROOT/'software_manifest.json')
    conda_records = list((Path(sys.prefix)/'conda-meta').glob('incfinder-*.json'))
    conda_record = read_json(conda_records[0]) if len(conda_records)==1 else {}
    if receipt_path.exists():
        previous = read_json(receipt_path)
        if (previous['label'] != args.label or previous['executable'] != sys.executable
                or previous['runtime'] != current_runtime
                or previous['parameters']['threshold_mode'] != args.threshold_mode
                or previous.get('software_manifest_sha256') != installed_manifest
                or previous.get('acceptance_script_sha256') != sha256(Path(__file__))):
            raise ValueError('Acceptance environment changed; use a new output directory')
    receipt = read_json(receipt_path) if receipt_path.exists() else {
        'schema':1,'label':args.label,'platform':platform.platform(),'machine':platform.machine(),
        'cpu_count':os.cpu_count(),'executable':sys.executable,'resource_root':str(ROOT),
        'runtime':current_runtime,'software_manifest_sha256':installed_manifest,
        'conda_package':{key:conda_record.get(key) for key in ('name','version','build','sha256')},
        'acceptance_script_sha256':sha256(Path(__file__)),'contract_sha256':sha256(ROOT/'config/contract.json'),
        'assets_manifest_sha256':sha256(ROOT/'config/assets.json'),
        'parameters':{'device':'cpu','batch_size':1,'threads':4,'jobs':1,'threshold_mode':args.threshold_mode},
        'records':ordered_identity(records),'input_hashes':{p.name:sha256(p) for p in sorted(inputs.iterdir())},'cases':{}}
    with np.load(fixture/'expected.npz',allow_pickle=False) as frozen:
        expected = {key: frozen[key] for key in ('stage1', 'stage2', 'gate', 'labels')}
    output_reference = read_json(fixture/'expected_output_v2.json')['modes'][args.threshold_mode]
    expected['labels'] = np.asarray(output_reference['labels'], dtype=np.uint8)
    for key in ('external_unclassified', 'external_classified', 'internal_status'):
        expected[key] = np.asarray(output_reference[key])
    tolerance=read_json(ROOT/'config/contract.json')['reference_tolerance']
    for case in args.cases:
        if receipt['cases'].get(case,{}).get('exit_code') == 0 and (output/case/'run_summary.json').is_file():
            continue
        name={'fastq':'reference.fastq','fasta_gz':'reference.fasta.gz','fastq_gz':'reference.fastq.gz'}.get(case,'reference.fasta')
        command=[sys.executable,'-B','-m','incfinder','predict',str(inputs/name),'-o',str(output/case),
                 '--model-dir',str(args.model_dir.resolve()),'--threads','4','--jobs','1','--batch-size','1','--device','cpu',
                 '--threshold-mode',args.threshold_mode]
        if case=='frozen': command += ['--features-dir',str(getattr(args, 'frozen_features', fixture))]
        else: command += ['--allow-encoder-drift']
        print('START '+args.label+' '+case,flush=True)
        result=measured(command,output,output/(case+'.log'))
        receipt['cases'][case]=result
        if result['exit_code']:
            receipt['execution_status']='FAIL';write_json(receipt_path,receipt)
            raise RuntimeError((output/(case+'.log')).read_text(encoding='utf-8',errors='replace')[-3000:])
        summary=read_json(output/case/'run_summary.json')
        result['timings']=summary['timings'];result['asset_check']=summary['assets']
        result['scientific_equivalence']=summary['scientific_equivalence']
        result['rf_file_descriptor_limit']=summary['prediction'].get('file_descriptor_limit')
        with np.load(output/case/'probabilities.npz',allow_pickle=False) as actual:
            result['reference_rf']={key:matrix_difference(actual[key],expected[key]) for key in ARRAY_KEYS}
        result['output_policy'] = {
            'schema': summary['output_schema_version'], 'policy': summary['classification_policy_id'],
            'counts': summary['prediction']['counts'],
            'internal_counts': summary['prediction']['internal_counts'],
            'gate_counts': summary['prediction']['gate_counts']}
        if case!='frozen':
            result['original_embedding']=matrix_difference(np.load(output/case/'features/features.npy',allow_pickle=False),
                np.load(fixture/'features.npy',allow_pickle=False),tolerance['embedding_atol'],tolerance['embedding_rtol'])
        receipt['execution_status']='PASS'
        write_json(receipt_path,receipt)
        print('DONE '+case+' '+json.dumps(result,ensure_ascii=False),flush=True)
    if (output/'fasta_first/run_summary.json').exists():
        receipt['format_comparisons']={}
        for case in CASES[2:]:
            if (output/case/'run_summary.json').exists():
                result=result_difference(output/case,output/'fasta_first')
                result['features']=matrix_difference(np.load(output/case/'features/features.npy'),np.load(output/'fasta_first/features/features.npy'),
                    tolerance['embedding_atol'],tolerance['embedding_rtol'])
                receipt['format_comparisons'][case]=result
    receipt['all_cases_completed']=all(receipt['cases'].get(case,{}).get('exit_code')==0 for case in CASES)
    receipt['frozen_rf_pass'] = all(value['exact'] for value in receipt['cases'].get('frozen',{}).get('reference_rf',{}).values()) and 'reference_rf' in receipt['cases'].get('frozen',{})
    receipt['format_features_bitwise_equal'] = all(value['features']['exact'] for value in receipt.get('format_comparisons',{}).values()) and len(receipt.get('format_comparisons',{})) == 4
    receipt['format_pass'] = all(format_accepted(value) for value in receipt.get('format_comparisons',{}).values()) and len(receipt.get('format_comparisons',{})) == 4
    receipt['engineering_status'] = 'PASS' if receipt['all_cases_completed'] and receipt['frozen_rf_pass'] and receipt['format_pass'] else 'INCOMPLETE_OR_FAIL'
    write_json(receipt_path,receipt)
    if receipt['all_cases_completed'] and receipt['engineering_status'] != 'PASS':
        raise RuntimeError('Frozen RF or input-format regression failed; inspect acceptance.json')


def compare(args):
    directories={read_json(path/'acceptance.json')['label']:path for path in args.runs}
    receipts={label:read_json(path/'acceptance.json') for label,path in directories.items()}
    pairs={}
    tolerance=read_json(ROOT/'config/contract.json')['reference_tolerance']
    labels=list(directories)
    for i,left in enumerate(labels):
        for right in labels[i+1:]:
            a,b=directories[left]/'fasta_first',directories[right]/'fasta_first'
            result=result_difference(a,b)
            result['features']=matrix_difference(np.load(a/'features/features.npy'),np.load(b/'features/features.npy'),
                tolerance['embedding_atol'],tolerance['embedding_rtol'])
            pairs[left+'__'+right]=result
    payload={'scope':'11 fixed sequences; engineering acceptance, not population accuracy or throughput benchmark',
             'scientific_state':'RESEARCH_PREVIEW','receipts':receipts,'pairwise':pairs}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    write_json(args.output,payload)
    print(json.dumps({'output':str(args.output),'pairwise':pairs},ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='mode',required=True)
    run_parser=sub.add_parser('run')
    run_parser.add_argument('--label',required=True)
    run_parser.add_argument('--model-dir',type=Path,required=True)
    run_parser.add_argument('--output',type=Path,required=True)
    run_parser.add_argument('--cases',nargs='+',choices=CASES,default=list(CASES))
    run_parser.add_argument('--threshold-mode', choices=['ct', 'u05'], default='ct')
    compare_parser=sub.add_parser('compare')
    compare_parser.add_argument('--runs',type=Path,nargs='+',required=True)
    compare_parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    (run if args.mode=='run' else compare)(args)


if __name__=='__main__':
    main()
