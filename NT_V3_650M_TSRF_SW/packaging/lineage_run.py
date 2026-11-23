"""Run a committed research stage and verify a portable candidate evidence index."""
import argparse
import csv
import hashlib
from importlib import metadata
from itertools import zip_longest
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ntv3_tsrf.model_lineage import (MODEL_ROLES, checked_path, fact, identifier, identity, load,
                                    locate, relative, validate_pointer, verify_reference)

# An input is either a pin to an external snapshot or a named output of a specific stage.
INPUTS = {
    'prepare': dict.fromkeys(('raw_snapshot', 'reference_labels', 'split_definition')),
    'encode_train': {'train_sequences': 'prepare', 'train_rows': 'prepare',
                     'encoder_weights': None, 'encoder_release': None},
    'encode_holdout': {'holdout_sequences': 'prepare', 'holdout_rows': 'prepare',
                       'encoder_weights': None, 'encoder_release': None},
    'train_stage1': {'train_features': 'encode_train', 'train_feature_rows': 'encode_train',
                     **dict.fromkeys(('train_labels', 'train_rows', 'folds', 'label_order'), 'prepare')},
    'train_stage2': {'train_features': 'encode_train', 'train_feature_rows': 'encode_train',
                     **dict.fromkeys(('train_labels', 'train_rows', 'folds', 'label_order'), 'prepare')},
    'thresholds': {'stage1_oof': 'train_stage1', 'stage1_oof_rows': 'train_stage1',
                   'stage2_oof': 'train_stage2', 'stage2_oof_rows': 'train_stage2',
                   **dict.fromkeys(('train_labels', 'folds', 'label_order'), 'prepare')},
    'evaluate': {'stage1_weight': 'train_stage1', 'stage2_weight': 'train_stage2',
                 'threshold_table': 'thresholds', 'holdout_features': 'encode_holdout',
                 'holdout_feature_rows': 'encode_holdout', 'evaluation_protocol': None,
                 **dict.fromkeys(('holdout_rows', 'holdout_labels', 'label_order'), 'prepare')},
    'report': {'probabilities': 'evaluate', 'probability_layout': 'evaluate',
               'metrics': 'evaluate', 'evaluation_protocol': None},
}
# Real prepare/evaluate jobs bind the archived inputs to their start receipts.
RESEARCH_EXTERNAL = {
    'prepare': set((
        'raw_inventory', 'original_source1', 'dated_source1', 'ncbi_catalog', 'membership',
        'representatives', 'typing_basis', 'typing_report', 'metasim_manifests', 'fold_source', 'label_source',
        'train_sequence_source', 'train_metadata_source', 'train_label_source', 'train_representative_fasta',
        'holdout_sequence_source', 'holdout_metadata_source', 'holdout_label_source', 'holdout_representative_fasta',
    )),
    'evaluate': {'baseline_stage1', 'baseline_stage2', 'baseline_index', 'baseline_thresholds', 'baseline_ct',
                 'baseline_u05', 'baseline_inference_manifest'},
}
RESEARCH_STAGE = 'research/ntv3_tsrf/stage.py'


ALIASES = {'stage1_weight': 'weight', 'stage2_weight': 'weight'}
LICENSE_ROLES = {'first_party_code', 'stage1_rf', 'stage2_rf', 'ntv3_weights', 'ntv3_output',
                 'vendor_code', 'training_data', 'labels'}
OUTPUTS = {
    'prepare': {'train_sequences', 'train_rows', 'train_labels', 'holdout_sequences', 'holdout_rows',
                'holdout_labels', 'folds', 'label_order', 'row_lineage', 'split_audit'},
    'encode_train': {'train_features', 'train_feature_rows'},
    'encode_holdout': {'holdout_features', 'holdout_feature_rows'},
    'train_stage1': {'weight', 'training_report', 'stage1_oof', 'stage1_oof_rows'},
    'train_stage2': {'weight', 'training_report', 'stage2_oof', 'stage2_oof_rows'},
    'thresholds': {'threshold_table', 'selection_report'},
    'evaluate': {'probabilities', 'probability_layout', 'metrics'},
    'report': {'report'},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def reference(roots, root, name):
    row = {'root': root, 'path': relative(name).as_posix()}
    return dict(row, **fact(locate(roots, row)))


def git(directory, *args):
    # Accept the supplied repository only for this operation. Disable network access,
    # replace refs, hooks and ambient repository overrides for archive recovery.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_ALLOW_PROTOCOL='file', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1')
    cmd = ['git', '-C', str(directory), '-c', 'safe.directory=' + directory.as_posix(),
           '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.autocrlf=false', *map(str, args)]
    process = subprocess.run(cmd, env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode:
        raise ValueError(f'Git {args[0]} failed: {process.stderr.decode(errors="replace").strip()}')
    return process.stdout


def check_commit(commit):
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('A complete research Git commit ID is required')
    return commit


def bundle_source(repository, commit, output):
    repository, output = Path(repository).resolve(), Path(output).absolute()
    check_commit(commit)
    if output.exists() or output.is_relative_to(repository):
        raise ValueError('The source bundle requires a new path outside the source checkout')
    if git(repository, 'rev-parse', 'HEAD').decode().strip() != commit:
        raise ValueError('The requested commit is not the checked-out HEAD')
    if git(repository, 'status', '--porcelain=v1', '--untracked-files=all').strip():
        raise ValueError('The research checkout has changes; commit the reviewed source first')
    if git(repository, 'rev-parse', '--is-shallow-repository').strip() != b'false':
        raise ValueError('The research source must be a complete Git repository')
    with tempfile.TemporaryDirectory(prefix='bundle-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'source.bundle'
        git(repository, 'bundle', 'create', staged, 'HEAD')
        checkout_bundle(staged, Path(temporary) / 'verify', commit)
        staged.rename(output)


def checkout_bundle(bundle, directory, commit):
    git(directory.parent, 'clone', '--quiet', '--no-checkout', '--', bundle, directory)
    git(directory, 'bundle', 'verify', bundle)
    git(directory, 'checkout', '--detach', '--quiet', check_commit(commit))
    if git(directory, 'rev-parse', 'HEAD').decode().strip() != commit:
        raise ValueError('Restored checkout does not match the recorded commit')
    if any(row.startswith((b'120000 ', b'160000 ')) for row in
           git(directory, 'ls-tree', '-r', '-z', commit).split(b'\0')):
        raise ValueError('Source bundles must directly contain their code, without links or submodules')


def entry_fact(checkout, commit, entrypoint):
    path = checked_path(checkout, entrypoint)
    if path.suffix != '.py':
        raise ValueError('Expected a Python research entry point')
    entry = fact(path)
    blob = git(checkout, 'cat-file', 'blob', f'{commit}:{entrypoint}')
    if entry != {'sha256': hashlib.sha256(blob).hexdigest(), 'size_bytes': len(blob)}:
        raise ValueError('Executed entry point differs from the Git blob')
    return entry


def active_environment():
    conda_meta = Path(sys.prefix) / 'conda-meta'
    conda = []
    for path in sorted(conda_meta.glob('*.json')):
        row = load(path)
        conda.append({key: row.get(key) for key in ('name', 'version', 'build', 'subdir', 'sha256', 'md5')})
    return {'schema': 1, 'platform': platform.system(), 'machine': platform.machine(),
            'python': platform.python_version(), 'python_executable': fact(Path(sys.executable).resolve()),
            'distributions': sorted([row.metadata['Name'], row.version] for row in metadata.distributions()),
            'conda_packages': conda}


def read_pass(roots, descriptor, stage, candidate):
    complete_path = verify_reference(roots, descriptor)
    value = load(complete_path)
    if (value.get('schema') != 1 or value.get('status') != 'PASS' or value.get('stage') != stage
            or value.get('candidate_id') != candidate or value.get('returncode') != 0):
        raise ValueError(f'Expected a successful completion receipt for {stage}/{candidate}')
    start = value['start']
    identifier(value['run_id'])
    attempt = identifier(value['attempt_id'])
    attempt_dir = f'runs/{stage}/{attempt}'
    if descriptor['root'] != 'candidate' or descriptor['path'] != attempt_dir + '/complete.json':
        raise ValueError(f'Completion receipt belongs to another attempt: {stage}')
    if start['root'] != 'candidate' or start['path'] != attempt_dir + '/start.json':
        raise ValueError(f'Wrong start receipt for {stage}')
    start_value = load(verify_reference(roots, start))
    for key in ('schema', 'candidate_id', 'stage', 'run_id', 'attempt_id'):
        if value[key] != start_value.get(key):
            raise ValueError(f'Start and completion receipts name different attempts: {stage}')
    if (start_value.get('status') != 'RUNNING'
            or datetime.fromisoformat(start_value['started_at_utc']) > datetime.fromisoformat(value['completed_at_utc'])):
        raise ValueError(f'Invalid execution interval: {stage}')
    if value['log']['root'] != 'candidate' or value['log']['path'] != attempt_dir + '/job.log':
        raise ValueError(f'Wrong execution log for {stage}')
    if not OUTPUTS[stage].issubset(value['outputs']) or set(start_value['output_paths']) != set(value['outputs']):
        raise ValueError(f'Incomplete output roles in {stage}')
    for name, output in value['outputs'].items():
        if output['root'] != 'candidate' or output['path'] != f'{attempt_dir}/output/{relative(start_value["output_paths"][name])}':
            raise ValueError(f'Output receipt belongs to a different attempt: {stage}/{name}')
    return value, start_value


def bindings(stage, parents, external, roots, candidate):
    expected = dict(INPUTS[stage])
    added = set(external) - set(expected)
    if added:
        if added != RESEARCH_EXTERNAL.get(stage):
            raise ValueError(f'Incomplete or unexpected input roles for {stage}')
        expected.update(dict.fromkeys(added))
    if set(parents) != {name for name in expected.values() if name} or set(external) != {
        role for role, producer in expected.items() if producer is None
    }:
        raise ValueError(f'Incomplete or unexpected input roles for {stage}')
    receipts = {name: read_pass(roots, descriptor, name, candidate) for name, descriptor in parents.items()}
    if stage == 'thresholds' and receipts['prepare'][1].get('entrypoint') == RESEARCH_STAGE:
        expected['train_rows'] = 'prepare'
    return {role: (external[role] if producer is None else receipts[producer][0]['outputs'][ALIASES.get(role, role)])
            for role, producer in expected.items()}


def command(arguments, inputs, outputs, config):
    paths = {'@config': config}
    paths.update({'@in:' + key: path for key, path in inputs.items()})
    paths.update({'@out:' + key: path for key, path in outputs.items()})
    if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
        raise ValueError('Job arguments must be a list of strings')
    if not paths.keys() <= set(arguments):
        raise ValueError('The command must explicitly bind every input, output and config path')
    for token in arguments:
        if token not in paths and (token.startswith('@') or any(x in token for x in ('\\', '/', ':', '\0')) or token in ('.', '..')):
            raise ValueError(f'Use declared input/output bindings for paths: {token!r}')
    return [str(paths.get(token, token)) for token in arguments]


def run_job(spec, roots):
    candidate, stage = identifier(spec['candidate_id']), spec['stage']
    run_id = identifier(spec['run_id'])
    if spec.get('schema') != 1 or stage not in INPUTS:
        raise ValueError('Unknown research job schema or stage')
    attempt = uuid.uuid4().hex
    directory_name = f'runs/{stage}/{attempt}'
    directory = checked_path(roots['candidate'], directory_name)
    directory.mkdir(parents=True, exist_ok=False)
    info = {'schema': 1, 'candidate_id': candidate, 'stage': stage, 'run_id': run_id, 'attempt_id': attempt}
    try:
        inputs = bindings(stage, spec['parents'], spec['external_inputs'], roots, candidate)
        paths = {name: verify_reference(roots, row) for name, row in inputs.items()}
        config = verify_reference(roots, spec['config'])
        lock = verify_reference(roots, spec['environment'])
        snapshot = active_environment()
        if snapshot != load(lock):
            raise ValueError('The active environment does not match the frozen environment snapshot')
        bundle = verify_reference(roots, spec['source_bundle'])
        commit = check_commit(spec['git_commit'])
        output_names = {identifier(key): relative(value).as_posix() for key, value in spec['output_paths'].items()}
        if (not OUTPUTS[stage].issubset(output_names) or len(output_names) !=
                len({name.casefold() for name in output_names.values()})):
            raise ValueError(f'Incomplete or conflicting output roles for {stage}')
        output_paths = {key: checked_path(directory, 'output/' + name) for key, name in output_names.items()}
        for path in output_paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        argv = command(spec['arguments'], paths, output_paths, config)
        with tempfile.TemporaryDirectory(prefix='lineage-source-') as temporary:
            checkout = Path(temporary) / 'checkout'
            checkout_bundle(bundle, checkout, commit)
            entry = spec['entrypoint']
            expected_entry = entry_fact(checkout, commit, entry)
            start = dict(info, status='RUNNING', started_at_utc=utc_now(), git_commit=commit,
                         source_bundle=spec['source_bundle'], entrypoint=entry, entrypoint_identity=expected_entry,
                         environment=spec['environment'], config=spec['config'], parents=spec['parents'],
                         external_inputs=spec['external_inputs'], inputs=inputs, output_paths=output_names,
                         command=['python', '-E', '-s', '-B', entry, *spec['arguments']])
            if stage == 'thresholds' and entry == RESEARCH_STAGE:
                if 'train_rows' not in inputs:
                    raise ValueError('Research thresholds require the prepared training rows')
                start['dispatch_contract'] = {'schema': 1, 'bound_role': 'prepare.train_rows',
                                              'control_module': fact(Path(__file__))}
            write_new(directory / 'start.json', start)
            log = directory / 'job.log'
            with log.open('xb') as stream:
                subprocess.run([sys.executable, '-E', '-s', '-B', entry, *argv], cwd=checkout,
                               stdout=stream, stderr=subprocess.STDOUT, check=True)
            if git(checkout, 'status', '--porcelain=v1', '--untracked-files=all').strip():
                raise ValueError('The job modified its source checkout')
            if active_environment() != snapshot:
                raise ValueError('The job changed the execution environment')
            for row in (*inputs.values(), spec['config'], spec['environment'], spec['source_bundle']):
                verify_reference(roots, row)
            outputs = {role: reference(roots, 'candidate', f'{directory_name}/output/{name}')
                       for role, name in output_names.items()}
        complete = dict(info, status='PASS', completed_at_utc=utc_now(), returncode=0, outputs=outputs,
                        start=reference(roots, 'candidate', directory_name + '/start.json'),
                        log=reference(roots, 'candidate', directory_name + '/job.log'))
        write_new(directory / 'complete.tmp', complete)
        (directory / 'complete.tmp').rename(directory / 'complete.json')
        return reference(roots, 'candidate', directory_name + '/complete.json')
    except (Exception, KeyboardInterrupt) as error:
        write_new(directory / 'failure.json', dict(info, status='FAIL', failed_at_utc=utc_now(),
                                                    error=type(error).__name__, message=str(error)))
        raise


def row_ids(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.DictReader(stream, delimiter='\t')
        if 'record_id' not in (rows.fieldnames or ()):
            raise ValueError('A row map requires a record_id column')
        for row in rows:
            yield row['record_id']


def check_row_order(records, feature_rows):
    seen = set()
    for position, (left, right) in enumerate(zip_longest(row_ids(records), row_ids(feature_rows)), 1):
        if not left or left != right or left in seen:
            raise ValueError(f'Feature row order or record ID mismatch at row {position}')
        seen.add(left)
    if not seen:
        raise ValueError('A feature row map must contain records')
    return seen


def verify_index(index_path, roots, pointer=None):
    index_path = Path(index_path)
    index = load(index_path)
    candidate = identifier(index['candidate_id'])
    if (set(index) != {'schema', 'candidate_id', 'stages', 'licenses'} or type(index['schema']) is not int
            or index['schema'] != 1 or set(index['stages']) != set(INPUTS)):
        raise ValueError('Model lineage requires one completion receipt for every stage')
    verified = set()
    restored, receipts, starts = {}, {}, {}

    def verify_file(row):
        key = (row['root'], row['path'], row['size_bytes'], row['sha256'])
        if key not in verified:
            verify_reference(roots, row)
            verified.add(key)
        return locate(roots, row)

    licenses = index['licenses']
    if set(licenses) != LICENSE_ROLES:
        raise ValueError('The candidate requires a reviewed license entry for each asset category')
    for role, row in licenses.items():
        if (set(row) != {'mode', 'basis', 'scope'} or row['mode'] not in ('bundle', 'upstream', 'internal')
                or not isinstance(row['scope'], str) or not row['scope'].strip()):
            raise ValueError(f'Invalid distribution basis or scope: {role}')
        if verify_file(row['basis']).stat().st_size == 0:
            raise ValueError(f'Empty distribution basis: {role}')

    with tempfile.TemporaryDirectory(prefix='verify-lineage-') as temporary:
        for name, descriptor in index['stages'].items():
            end, start = read_pass(roots, descriptor, name, candidate)
            receipts[name], starts[name] = end, start
            if any(index['stages'].get(parent) != ref for parent, ref in start['parents'].items()):
                raise ValueError(f'Mixed completion attempts in the candidate: {name}')
            expected = bindings(name, start['parents'], start['external_inputs'], roots, candidate)
            if start['inputs'] != expected or start['command'][:5] != ['python', '-E', '-s', '-B', start['entrypoint']]:
                raise ValueError(f'Unexpected recorded inputs or command: {name}')
            command(start['command'][5:], expected, end['outputs'], start['config'])
            for row in (*expected.values(), *end['outputs'].values(), start['config'], start['environment'], end['log']):
                verify_file(row)
            bundle = verify_file(start['source_bundle'])
            key = (bundle, start['git_commit'])
            if key not in restored:
                checkout = Path(temporary) / str(len(restored))
                checkout_bundle(bundle, checkout, start['git_commit'])
                restored[key] = checkout
            if entry_fact(restored[key], start['git_commit'], start['entrypoint']) != start['entrypoint_identity']:
                raise ValueError(f'Restored source entry differs from the run receipt: {name}')
    for key in ('encoder_weights', 'encoder_release'):
        if identity(starts['encode_train']['inputs'][key]) != identity(starts['encode_holdout']['inputs'][key]):
            raise ValueError(f'Train and holdout used different encoder identity: {key}')
    train = check_row_order(locate(roots, receipts['prepare']['outputs']['train_rows']),
                            locate(roots, receipts['encode_train']['outputs']['train_feature_rows']))
    holdout = check_row_order(locate(roots, receipts['prepare']['outputs']['holdout_rows']),
                              locate(roots, receipts['encode_holdout']['outputs']['holdout_feature_rows']))
    if train.intersection(holdout):
        raise ValueError('Training and holdout feature rows overlap')
    if starts['evaluate']['inputs']['evaluation_protocol'] != starts['report']['inputs']['evaluation_protocol']:
        raise ValueError('Evaluation and report used different evaluation protocols')
    if any(start['entrypoint'] == RESEARCH_STAGE for start in starts.values()):
        if any(start['entrypoint'] != RESEARCH_STAGE or load(locate(roots, start['config'])).get('mode') != 'formal'
               for start in starts.values()):
            raise ValueError('A formal candidate cannot mix research and pilot entry points')
        evaluation = load(locate(roots, receipts['evaluate']['outputs']['metrics']))
        policy = load(locate(roots, starts['evaluate']['config']))
        if (policy.get('mode') != 'formal' or evaluation.get('rows') != 200095
                or evaluation.get('acceptance') != 'zero_holdout_decision_differences'
                or evaluation.get('status') != 'PASS'
                or set(evaluation.get('modes', {})) != {'ct', 'u05'}
                or any(row.get('changed_records') != 0 for row in evaluation['modes'].values())):
            raise ValueError('The candidate failed its locked 200,095-row holdout decision check')
    assets = {}
    for path, (stage, role) in MODEL_ROLES.items():
        rows = starts[stage]['inputs'] if role == 'encoder_weights' else receipts[stage]['outputs']
        assets[path] = identity(rows[role])
    value = validate_pointer({'schema': 1, 'candidate_id': candidate, 'index_sha256': fact(index_path)['sha256'],
                              'assets': assets})
    if pointer is not None and value != validate_pointer(pointer):
        raise ValueError('Restored evidence does not match the packaged model lineage pointer')
    return value


def assignments(values):
    result = {}
    for value in values:
        key, found, target = value.partition('=')
        if not found or not target or identifier(key) in result:
            raise ValueError('Specify unique NAME=VALUE mappings')
        result[key] = target
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    bundle = modes.add_parser('bundle')
    bundle.add_argument('--repo', required=True, type=Path)
    bundle.add_argument('--commit', required=True)
    bundle.add_argument('--output', required=True, type=Path)
    freeze = modes.add_parser('environment')
    freeze.add_argument('--output', required=True, type=Path)
    run = modes.add_parser('run')
    run.add_argument('--spec', required=True, type=Path)
    verify = modes.add_parser('verify')
    verify.add_argument('--index', required=True, type=Path)
    verify.add_argument('--pointer', type=Path)
    verify.add_argument('--write-pointer', type=Path)
    for mode in (run, verify):
        mode.add_argument('--location', action='append', default=[], metavar='NAME=ROOT')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'bundle':
            bundle_source(args.repo, args.commit, args.output)
        elif args.mode == 'environment':
            write_new(args.output, active_environment())
        elif args.mode == 'run':
            print(json.dumps(run_job(load(args.spec), assignments(args.location)), ensure_ascii=False))
        else:
            value = verify_index(args.index, assignments(args.location), load(args.pointer) if args.pointer else None)
            if args.write_pointer:
                write_new(args.write_pointer, value)
            print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
