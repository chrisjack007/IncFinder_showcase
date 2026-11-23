"""Exercise real research jobs and the candidate evidence contract without large assets."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from ntv3_tsrf.model_lineage import POINTER, fact, load, load_pointer, prediction_binding, verify_reference

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import lineage_run as run
from ci import frozen_feature_input
from release_evidence import _wheel_lineage


# Use synthetic numerical data so the tests can exercise two actual fits, OOF,
# evaluation and restoration in the regular Windows and Linux unit jobs.
JOB = r'''
import csv
import json
from pathlib import Path
import shutil
import sys

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

stage = sys.argv[1]
paths = {name: Path(value) for name, value in zip(sys.argv[2::2], sys.argv[3::2])}
config = json.loads(paths['config'].read_text(encoding='utf-8'))


def put(key, value):
    paths[key].write_text(json.dumps(value), encoding='utf-8')


def get(key):
    return json.loads(paths[key].read_text(encoding='utf-8'))


def ids(path, numbers):
    path.write_text('record_id\n' + ''.join(f'r{i}\n' for i in numbers), encoding='utf-8')


def values(model, x):
    prob = model.predict_proba(x)
    if isinstance(prob, list):
        return np.column_stack([item[:, 1] for item in prob]).tolist()
    return prob[:, 1].tolist()


if config.get('fail'):
    raise SystemExit(7)
if stage == 'prepare':
    raw = get('raw_snapshot')
    label_reference = get('reference_labels')
    train_end = get('split_definition')['train_end']
    for split, numbers in (('train', raw[:train_end]), ('holdout', raw[train_end:])):
        put(split + '_sequences', numbers)
        put(split + '_labels', [[n % 2] + [(n + j) % 3 == 0 for j in range(len(label_reference))]
                                for n in numbers])
        ids(paths[split + '_rows'], numbers)
    put('folds', [int(n % 4 < 2) for n in raw[:train_end]])
    put('label_order', label_reference)
    put('row_lineage', {f'r{n}': n for n in raw})
    put('split_audit', {'train_rows': train_end, 'holdout_rows': len(raw) - train_end})
elif stage.startswith('encode_'):
    split = stage.removeprefix('encode_')
    scale = get('encoder_weights')['scale']
    get('encoder_release')
    put(split + '_features', [[scale * n, n % 3, n % 2] + [0] * 1533 for n in get(split + '_sequences')])
    if config.get('reverse_rows'):
        rows = paths[split + '_rows'].read_text(encoding='utf-8').splitlines()
        paths[split + '_feature_rows'].write_text('\n'.join(rows[:1] + rows[:0:-1]) + '\n', encoding='utf-8')
    else:
        shutil.copyfile(paths[split + '_rows'], paths[split + '_feature_rows'])
elif stage.startswith('train_stage'):
    layer = int(stage[-1])
    get('label_order')
    if paths['train_rows'].read_bytes() != paths['train_feature_rows'].read_bytes():
        raise ValueError('Bad train row order')
    x, y, folds = (np.array(get(name)) for name in ('train_features', 'train_labels', 'folds'))
    y = y[:, 0] if layer == 1 else y[:, 1:]
    model = RandomForestClassifier(n_estimators=config['trees'], random_state=101)
    oof = np.empty(y.shape)
    for fold in (0, 1):
        test = folds == fold
        model.fit(x[~test], y[~test])
        oof[test] = values(model, x[test])
    model.fit(x, y)
    joblib.dump(model, paths['weight'])
    put(f'stage{layer}_oof', oof.tolist())
    shutil.copyfile(paths['train_rows'], paths[f'stage{layer}_oof_rows'])
    put('training_report', {'records': len(y), 'stage': layer})
elif stage == 'thresholds':
    assert paths['stage1_oof_rows'].read_bytes() == paths['stage2_oof_rows'].read_bytes()
    count = len(get('train_labels'))
    assert count == len(get('folds')) == len(get('stage1_oof')) == len(get('stage2_oof'))
    with paths['threshold_table'].open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(('model_key', 'label_order', 'label_name', 'threshold'))
        for i, (label, median) in enumerate(zip(get('label_order'), np.median(get('stage2_oof'), axis=0))):
            writer.writerow(('nt_v3', i, label, max(float(median), 0.001)))
    put('selection_report', {'source': 'train_oof', 'rows': count})
elif stage == 'evaluate':
    assert paths['holdout_rows'].read_bytes() == paths['holdout_feature_rows'].read_bytes()
    assert get('evaluation_protocol')['metric'] == 'accuracy'
    x = get('holdout_features')
    labels = get('label_order')
    probabilities = [values(joblib.load(paths[f'stage{i}_weight']), x) for i in (1, 2)]
    put('probabilities', probabilities)
    put('probability_layout', {'labels': labels, 'rows': paths['holdout_rows'].read_text().splitlines()[1:]})
    truth = np.array(get('holdout_labels'))
    with paths['threshold_table'].open(encoding='utf-8', newline='') as stream:
        thresholds = [float(row['threshold']) for row in csv.DictReader(stream, delimiter='\t')]
    called = np.column_stack([np.array(probabilities[0]) >= 0.5,
                              np.array(probabilities[1]) >= np.array(thresholds)])
    put('metrics', {'accuracy': float(np.mean(called == truth))})
else:
    assert get('evaluation_protocol')['metric'] in get('metrics')
    put('report', {'values': get('metrics'), 'layout': get('probability_layout'),
                   'probability_count': sum(map(len, get('probabilities')))})
'''


class ModelLineageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='model-lineage-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_actual_jobs_and_restore_after_source_checkout_moves(self):
        source, records = self.root / 'research', self.root / 'candidate'
        source.mkdir()
        records.mkdir()
        (source / 'job.py').write_text(JOB, encoding='utf-8', newline='\n')
        run.git(source, 'init', '-q')
        run.git(source, 'add', 'job.py')
        run.git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'Fixture research entry')
        commit = run.git(source, 'rev-parse', 'HEAD').decode().strip()
        with self.assertRaisesRegex(ValueError, 'checked-out HEAD'):
            run.bundle_source(source, '0' * 40, records / 'wrong.bundle')
        run.bundle_source(source, commit, records / 'source.bundle')
        source.rename(self.root / 'original-source-moved')
        roots = {'candidate': records}

        def pin(name, content):
            run.write_new(records / (name + '.json'), content)
            return run.reference(roots, 'candidate', name + '.json')

        controls = {
            'environment': pin('environment', run.active_environment()),
            'config': pin('config', {'trees': 500}),
            'source_bundle': run.reference(roots, 'candidate', 'source.bundle'),
            'git_commit': commit, 'entrypoint': 'job.py',
            'schema': 1, 'candidate_id': 'smoke-001', 'run_id': 'smoke-run',
        }
        external = {name: pin(name, value) for name, value in (
            ('raw_snapshot', list(range(24))), ('reference_labels', load(run.ROOT / 'config/contract.json')['labels']),
            ('split_definition', {'train_end': 16}), ('encoder_weights', {'scale': 2}),
            ('encoder_release', {'revision': 'test-encoder-1'}),
            ('evaluation_protocol', {'metric': 'accuracy'}),
        )}
        passed = {}
        for stage, mapping in run.INPUTS.items():
            output_paths = {role: role + ('.joblib' if role == 'weight' else '.data')
                            for role in sorted(run.OUTPUTS[stage])}
            spec = dict(controls, stage=stage, output_paths=output_paths,
                        parents={p: passed[p] for p in sorted(set(mapping.values()) - {None})},
                        external_inputs={name: external[name] for name, parent in mapping.items() if parent is None},
                        arguments=[stage, 'config', '@config'] +
                                  [arg for role in mapping for arg in (role, '@in:' + role)] +
                                  [arg for role in output_paths for arg in (role, '@out:' + role)])
            if stage == 'train_stage2':
                opposing = dict(spec['external_inputs'], holdout_features=external['raw_snapshot'])
                with self.assertRaisesRegex(ValueError, 'unexpected input roles'):
                    run.run_job(dict(spec, external_inputs=opposing), roots)
                failures = list(records.glob('runs/train_stage2/*/failure.json'))
                self.assertEqual(len(failures), 1)
                self.assertFalse(failures[0].with_name('complete.json').exists())
                self.assertEqual(run.load(failures[0])['status'], 'FAIL')
            passed[stage] = run.run_job(spec, roots)

        basis = pin('fixture-rights', {'permission': 'Synthetic fixture only'})
        licenses = {name: {'mode': 'internal', 'basis': basis, 'scope': 'Synthetic fixture'}
                    for name in run.LICENSE_ROLES}
        index = records / 'index.json'
        run.write_new(index, {'schema': 1, 'candidate_id': 'smoke-001', 'stages': passed, 'licenses': licenses})
        recovered = self.root / 'restored'
        records.rename(recovered)
        roots = {'candidate': recovered}
        index = recovered / 'index.json'
        pointer = run.verify_index(index, roots)
        self.assertEqual(run.verify_index(index, roots, pointer), pointer)
        weight = run.load(verify_reference(roots, passed['train_stage1']))['outputs']['weight']
        self.assertEqual(pointer['assets']['models/rf/stage1.joblib'], run.identity(weight))
        self.assertFalse(source.exists())
        self.assertFalse(records.exists())
        report = run.load(verify_reference(roots, passed['report']))['outputs']['report']
        self.assertEqual(run.load(verify_reference(roots, report))['probability_count'], 16)

        swapped = recovered / 'swapped.json'
        run.write_new(swapped, {'schema': 1, 'candidate_id': 'smoke-001',
                                'stages': dict(passed, train_stage1=passed['train_stage2']), 'licenses': licenses})
        with self.assertRaisesRegex(ValueError, 'Mixed completion attempts'):
            run.verify_index(swapped, roots)
        weight_path = verify_reference(roots, weight)
        saved = weight_path.read_bytes()
        weight_path.write_bytes(b'changed model bytes')
        try:
            with self.assertRaisesRegex(ValueError, 'size or SHA-256 mismatch'):
                run.verify_index(index, roots, pointer)
        finally:
            weight_path.write_bytes(saved)

    def test_row_order_diagnostic(self):
        left, right = self.root / 'a.tsv', self.root / 'b.tsv'
        left.write_text('record_id\nr1\nr2\n', encoding='utf-8')
        right.write_text('record_id\nr2\nr1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'row order or record ID mismatch at row 1'):
            run.check_row_order(left, right)

    def test_new_rf_identity_reuses_the_frozen_features_only_with_the_same_encoder(self):
        config = self.root / 'config'
        config.mkdir()
        fixture = self.root / 'tests/fixtures'
        fixture.mkdir(parents=True)
        feature = fixture / 'features.npy'
        feature.write_bytes(b'unchanged fixture')
        contract = config / 'contract.json'
        run.write_new(contract, {})
        assets = {name: dict(size_bytes=12, sha256=char * 64) for name, char in zip(run.MODEL_ROLES, '1234')}
        asset_path = config / 'assets.json'
        run.write_new(asset_path, {'files': [dict(value, path=name) for name, value in assets.items()]})
        baseline = asset_path.read_bytes()
        info = fixture / 'features.json'
        run.write_new(info, {'assets_sha256': fact(asset_path)['sha256'], 'contract_sha256': fact(contract)['sha256'],
                             'features_sha256': fact(feature)['sha256']})
        before = info.read_bytes()
        self.assertEqual(frozen_feature_input(self.root, self.root, baseline), fixture)

        def change(name):
            assets[name] = dict(size_bytes=24, sha256='a' * 64)
            asset_path.write_text(json.dumps({'files': [dict(value, path=key) for key, value in assets.items()]}))
            (self.root / POINTER).write_text(json.dumps({'schema': 1, 'candidate_id': 'tc1',
                                                         'index_sha256': '9' * 64, 'assets': assets}))

        change('models/rf/stage2.joblib')
        prepared = frozen_feature_input(self.root, self.root, baseline)
        self.assertEqual(fact(prepared / 'features.npy'), fact(feature))
        self.assertEqual(info.read_bytes(), before)
        self.assertEqual(load(prepared / 'features.json')['assets_sha256'], fact(asset_path)['sha256'])
        change('models/ntv3/model.safetensors')
        with self.assertRaisesRegex(ValueError, 'encoder or other frozen assets differ'):
            frozen_feature_input(self.root, self.root, baseline)

    def test_installed_pointer_content_matches_the_assets_and_the_manifest(self):
        (self.root / 'config').mkdir()
        pointer_file = self.root / POINTER
        assets = {name: dict(size_bytes=12, sha256=char * 64)
                  for name, char in zip(run.MODEL_ROLES, '1234')}
        pointer = {'schema': 1, 'candidate_id': 'tc1', 'index_sha256': '9' * 64, 'assets': assets}
        run.write_new(pointer_file, pointer)
        assets_path = self.root / 'config/assets.json'
        run.write_new(assets_path, {'files': [dict(value, path=name) for name, value in assets.items()]})
        manifest = self.root / 'package_manifest.json'
        run.write_new(manifest, {'files': [dict(fact(pointer_file), path=POINTER)]})
        self.assertEqual(load_pointer(self.root), pointer)
        binding = prediction_binding(self.root, installed=False, run_id='some-run', input_sha256='0' * 64)
        self.assertEqual(binding['pointer_sha256'], fact(pointer_file)['sha256'])
        self.assertEqual(binding['installation']['sha256'], fact(manifest)['sha256'])
        package = self.root / 'candidate.whl'
        with zipfile.ZipFile(package, 'w') as wheel:
            wheel.write(pointer_file, 'ntv3_tsrf/_resources/' + POINTER)
            wheel.write(assets_path, 'ntv3_tsrf/_resources/config/assets.json')
        self.assertEqual(_wheel_lineage(package, manifest), pointer_file.read_bytes())
        with zipfile.ZipFile(package, 'w') as wheel:
            wheel.writestr('ntv3_tsrf/_resources/' + POINTER, b'{}')
        with self.assertRaisesRegex(ValueError, 'differs from the source identity'):
            _wheel_lineage(package, manifest)
