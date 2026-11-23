"""Prevent accidental V1/V2 identity mixing without changing result payloads."""
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from incfinder import load_output_metadata, migrate_v1_results, require_supported_policy
from ntv3_tsrf import api, classify, cli, migration, output_metadata
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.output_metadata import RESULT_FILES, write_output_metadata
from output_helpers import make_result


class OutputMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='incfinder-meta-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.base / 'runs')))
        self.result = self.base / 'result'
        make_result(self.result)

    def test_output_identity_comes_from_one_contract_read(self):
        path = ROOT / 'config/output_contract.json'
        payload = path.read_bytes() + b'\n'
        with patch.object(Path, 'read_bytes', autospec=True, return_value=payload) as read:
            identity = output_metadata.load_output_contract()
        read.assert_called_once_with(path)
        self.assertEqual(identity, {
            'output_schema_version': 2,
            'classification_policy_id': json.loads(payload)['classification_policy_id'],
            'output_contract_sha256': hashlib.sha256(payload).hexdigest(),
        })

    def test_malformed_or_inconsistent_output_contract_is_rejected(self):
        path = self.base / 'config/output_contract.json'
        path.parent.mkdir()
        original = read_json(ROOT / 'config/output_contract.json')
        with patch.object(output_metadata, 'ROOT', self.base):
            with self.assertRaisesRegex(ValueError, 'Cannot read output contract'):
                output_metadata.load_output_contract()
            for data in (b'{broken', b'\xff', b'{"schema_version": 2, "schema_version": 2}',
                         b'{"schema_version": 2, "bad": NaN}', b'[]'):
                with self.subTest(data=data):
                    path.write_bytes(data)
                    with self.assertRaisesRegex(ValueError, 'output contract|Output contract'):
                        output_metadata.load_output_contract()
            for name, value in (
                ('schema_version', 3), ('schema_version', 2.0), ('schema_version', True),
                ('classification_policy_id', 'unsupported'), ('gate_operator', '>'),
                ('external_unclassified', 'at least one call is one'), ('prediction_is_ground_truth', 0),
                ('external_unclassified_continuous_score', 0.0), ('npz_additions', {}),
                ('internal_status_definitions', {}), ('stage1_pred_scope', ''),
            ):
                with self.subTest(name=name, value=value):
                    write_json(path, {**original, name: value})
                    with self.assertRaisesRegex(ValueError, name.replace('schema_version', 'schema version')):
                        output_metadata.load_output_contract()
            del original['external_unclassified_continuous_score']
            write_json(path, original)
            with self.assertRaisesRegex(ValueError, 'external_unclassified_continuous_score'):
                output_metadata.load_output_contract()

    def test_unknown_contract_fails_at_prediction_and_migration_preflight(self):
        path = self.base / 'config/output_contract.json'
        path.parent.mkdir()
        shutil.copyfile(ROOT / 'config/contract.json', path.with_name('contract.json'))
        write_json(path, {'schema_version': 3})
        fixture = ROOT / 'tests/fixtures'
        output = self.base / 'uncreated' / 'result'
        legacy = self.base / 'legacy'
        make_result(legacy, version='v1')
        with (patch.object(output_metadata, 'ROOT', self.base),
              patch.object(api, 'environment') as environment,
              patch.object(api, 'verify_assets') as verify,
              patch.object(api, 'worker') as worker,
              patch.object(classify, 'prepare_model_loading') as model_loading,
              patch.object(migration, '_input_files') as inputs):
            with self.assertRaisesRegex(api.IncFinderError, 'Unsupported output contract schema version'):
                api.predict(input=fixture / 'reference.fasta', output=output, features_dir=fixture)
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(cli.main(['predict', str(fixture / 'reference.fasta'), '-o', str(output),
                                           '--features-dir', str(fixture)]), 2)
            self.assertIn('Unsupported output contract schema version', errors.getvalue())
            with self.assertRaisesRegex(ValueError, 'Unsupported output contract schema version'):
                classify.classify(fixture / 'reference.fasta', fixture, output)
            with self.assertRaisesRegex(ValueError, 'Unsupported output contract schema version'):
                migrate_v1_results(legacy, output)
            # Explicit V1 reading has no dependency on the target V2 contract.
            self.assertEqual(load_output_metadata(legacy / 'inc_results.csv', source_version='v1')['output_schema_version'], 1)
        for call in (environment, verify, worker, model_loading, inputs):
            call.assert_not_called()
        self.assertFalse(output.parent.exists())

    def test_six_sidecars_preserve_original_file_bytes_and_container_structure(self):
        before = {name: sha256(self.result / name) for name in RESULT_FILES}
        names = write_output_metadata(self.result)
        self.assertEqual(len(names), 6)
        for name in RESULT_FILES:
            metadata = require_supported_policy(load_output_metadata(self.result / name))
            self.assertEqual(metadata['result_file'], name)
            self.assertEqual(metadata['result_sha256'], before[name])
            self.assertEqual(metadata['output_schema_version'], 2)
            self.assertEqual(metadata['threshold_mode'], 'ct')
            self.assertEqual(metadata['metadata_source'], 'sidecar')
            self.assertEqual(sha256(self.result / name), before[name])
        self.assertEqual(write_output_metadata(self.result), names)

    def test_detached_file_and_sidecar_remain_readable(self):
        write_output_metadata(self.result)
        detached = self.base / 'detached'
        detached.mkdir()
        for name in RESULT_FILES:
            with self.subTest(name=name):
                for suffix in ('', '.metadata.json'):
                    shutil.copyfile(self.result / (name + suffix), detached / (name + suffix))
                self.assertEqual(load_output_metadata(detached / name)['output_schema_version'], 2)

    def test_missing_sidecar_requires_explicit_version_and_does_not_guess_context(self):
        path = self.result / 'inc_results.csv'
        with self.assertRaisesRegex(ValueError, 'explicitly declare'):
            load_output_metadata(path)
        with self.assertRaisesRegex(ValueError, 'source_version'):
            load_output_metadata(path, source_version='v3')
        metadata = load_output_metadata(path, source_version='v2')
        self.assertEqual(metadata['metadata_source'], 'user_declared')
        self.assertEqual(require_supported_policy(metadata)['output_schema_version'], 2)
        (self.result / 'run_summary.json').unlink()
        with self.assertRaisesRegex(ValueError, 'prediction.json and run_summary.json'):
            load_output_metadata(path, source_version='v2')

    def test_v1_is_identified_but_cannot_be_consumed_as_v2(self):
        legacy = self.base / 'legacy'
        make_result(legacy, version='v1')
        metadata = load_output_metadata(legacy / 'inc_label_predictions.csv', source_version='v1')
        self.assertEqual(metadata['output_schema_version'], 1)
        self.assertIsNone(metadata['output_contract_sha256'])
        with self.assertRaisesRegex(ValueError, 'migrate_v1_results'):
            require_supported_policy(metadata)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            load_output_metadata(legacy / 'inc_results.csv', source_version='v2')

    def test_metadata_cannot_be_swapped_between_files(self):
        write_output_metadata(self.result)
        source = self.result / 'layer1_decisions.csv.metadata.json'
        target = self.result / 'inc_results.csv.metadata.json'
        shutil.copyfile(source, target)
        with self.assertRaisesRegex(ValueError, 'different result file'):
            load_output_metadata(self.result / 'inc_results.csv')

    def test_equal_size_file_changes_fail_hash_validation(self):
        write_output_metadata(self.result)
        path = self.result / 'inc_results.csv'
        original = path.read_bytes()
        path.write_bytes(original.replace(b'Inc Unclassified', b'Inc unclassified', 1))
        self.assertEqual(path.stat().st_size, len(original))
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            load_output_metadata(path)

    def test_unknown_or_conflicting_policy_fields_are_rejected(self):
        write_output_metadata(self.result)
        path = self.result / 'inc_results.csv'
        sidecar = path.with_name(path.name + '.metadata.json')
        baseline = read_json(sidecar)
        cases = {'metadata_schema_version': 99, 'output_schema_version': 99,
                 'classification_policy_id': 'unknown', 'output_contract_sha256': '0' * 64,
                 'threshold_mode': 'unknown', 'record_count': 0, 'stage1_threshold': 0.7,
                 'label_order': list(reversed(baseline['label_order'])), 'stage2_thresholds': [0.1] * 21}
        for name, value in cases.items():
            with self.subTest(field=name):
                write_json(sidecar, {**baseline, name: value})
                with self.assertRaises(ValueError):
                    load_output_metadata(path)
        write_json(sidecar, baseline)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            load_output_metadata(path, source_version='v1')

    def test_missing_threshold_or_contract_is_not_filled_from_defaults(self):
        path = self.result / 'inc_results.csv'
        prediction = read_json(self.result / 'prediction.json')
        for field in ('threshold_mode', 'output_contract_sha256', 'stage2_thresholds', 'label_order'):
            with self.subTest(field=field):
                changed = dict(prediction)
                changed.pop(field)
                write_json(self.result / 'prediction.json', changed)
                with self.assertRaises(ValueError):
                    load_output_metadata(path, source_version='v2')
        write_json(self.result / 'prediction.json', prediction)

    def test_schema_label_cannot_relabel_v1_payload_as_v2(self):
        write_output_metadata(self.result)
        legacy = self.base / 'legacy'
        make_result(legacy, version='v1')
        for name in ('inc_results.csv', 'inc_label_predictions.csv', 'probabilities.npz', 'inc_predictions.xlsx'):
            with self.subTest(name=name):
                path = self.result / name
                shutil.copyfile(legacy / name, path)
                sidecar = path.with_name(path.name + '.metadata.json')
                metadata = read_json(sidecar)
                metadata['result_sha256'] = sha256(path)
                write_json(sidecar, metadata)
                with self.assertRaisesRegex(ValueError, 'declared output version'):
                    load_output_metadata(path)

    def test_incomplete_results_fail_before_writing_any_sidecars(self):
        (self.result / 'inc_predictions.xlsx').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing required'):
            write_output_metadata(self.result)
        self.assertEqual(list(self.result.glob('*.metadata.json')), [])

    def test_metadata_failure_prevents_api_result_publication(self):
        output = self.base / 'api-result'
        def worker(executable, module, arguments, log, device='cpu'):
            if module == 'ntv3_tsrf.classify':
                write_json(arguments[2] / 'prediction.json', {'record_count': 11})
            elif module == 'ntv3_tsrf.report':
                write_json(arguments[0] / 'report_environment.json', {})
        with patch.object(api, 'environment', return_value={}), patch.object(api, 'verify_package', return_value={}), \
                patch.object(api, 'verify_assets', return_value={}), \
                patch.object(api, 'model_location', return_value={'path': str(ROOT / 'models')}), \
                patch.object(api, 'worker', side_effect=worker):
            with self.assertRaisesRegex(api.IncFinderError, 'threshold_mode'):
                api.predict(input=ROOT / 'tests/fixtures/reference.fasta', output=output,
                            features_dir=ROOT / 'tests/fixtures')
        self.assertFalse(output.exists())
        self.assertEqual(len(list(self.base.glob('.ntv3-run-*/failure.json'))), 1)

    def test_conflicting_worker_output_identity_stops_before_reporting(self):
        output = self.base / 'api-result'
        for name, value, error in (
            ('output_schema_version', 2.0, 'source version conflicts'),
            ('classification_policy_id', 'other', 'classification policy'),
            ('output_contract_sha256', '0' * 64, 'output contract SHA-256'),
        ):
            with self.subTest(name=name):
                def run_classifier(executable, module, arguments, log, device='cpu'):
                    self.assertEqual(module, 'ntv3_tsrf.classify')
                    original = read_json(self.result / 'prediction.json')
                    write_json(arguments[2] / 'prediction.json', {**original, name: value})
                with (patch.object(api, 'environment', return_value={}),
                      patch.object(api, 'verify_package', return_value={}),
                      patch.object(api, 'verify_assets', return_value={}),
                      patch.object(api, 'model_location', return_value={'path': str(ROOT / 'models')}),
                      patch.object(api, 'worker', side_effect=run_classifier) as worker):
                    with self.assertRaisesRegex(api.IncFinderError, error):
                        api.predict(input=ROOT / 'tests/fixtures/reference.fasta', output=output,
                                    features_dir=ROOT / 'tests/fixtures')
                worker.assert_called_once()
                self.assertFalse(output.exists())

    def test_successful_api_registers_all_six_sidecars_in_summary(self):
        output = self.base / 'published'
        def worker(executable, module, arguments, log, device='cpu'):
            if module == 'ntv3_tsrf.classify':
                for path in self.result.iterdir():
                    if path.is_file() and path.name not in ('run_summary.json', 'report_environment.json',
                                                               'inc_predictions.xlsx'):
                        shutil.copyfile(path, arguments[2] / path.name)
            elif module == 'ntv3_tsrf.report':
                for name in ('inc_predictions.xlsx', 'report_environment.json'):
                    shutil.copyfile(self.result / name, arguments[-1] / name)
        with patch.object(api, 'environment', return_value={}), patch.object(api, 'verify_package', return_value={}), \
                patch.object(api, 'verify_assets', return_value={}), \
                patch.object(api, 'model_location', return_value={'path': str(ROOT / 'models')}), \
                patch.object(api, 'worker', side_effect=worker), \
                patch.object(api, 'load_output_contract', wraps=output_metadata.load_output_contract) as load, \
                patch.object(output_metadata, 'load_output_contract', side_effect=AssertionError('Unexpected contract reread')):
            summary = api.predict(input=ROOT / 'tests/fixtures/reference.fasta', output=output,
                                  features_dir=ROOT / 'tests/fixtures')
        load.assert_called_once_with()
        identity = output_metadata.load_output_contract()
        self.assertEqual({name: summary[name] for name in identity}, identity)
        self.assertEqual({name: summary['prediction'][name] for name in identity}, identity)
        for name in RESULT_FILES:
            metadata_file = name + '.metadata.json'
            self.assertEqual(summary['outputs'][metadata_file], sha256(output / metadata_file))
            metadata = require_supported_policy(load_output_metadata(output / name))
            self.assertEqual({key: metadata[key] for key in identity}, identity)

    def test_valid_policy_cannot_contradict_csv_mode_or_record_count(self):
        write_output_metadata(self.result)
        path = self.result / 'inc_label_predictions.csv'
        sidecar = path.with_name(path.name + '.metadata.json')
        metadata = read_json(sidecar)
        write_json(sidecar, {**metadata, 'threshold_mode': 'u05', 'stage2_thresholds': [0.5] * 21})
        with self.assertRaisesRegex(ValueError, 'CSV threshold mode'):
            load_output_metadata(path)
        write_json(sidecar, {**metadata, 'record_count': 10})
        with self.assertRaisesRegex(ValueError, 'CSV record count'):
            load_output_metadata(path)

    def test_npz_shape_and_dtype_must_match_its_metadata(self):
        import numpy as np
        write_output_metadata(self.result)
        path = self.result / 'probabilities.npz'
        with np.load(path, allow_pickle=False) as archive:
            original = {name: archive[name] for name in archive.files}
        sidecar = path.with_name(path.name + '.metadata.json')
        metadata = read_json(sidecar)
        for replacement in (dict(original, gate=original['gate'].astype('uint8')),
                            dict(original, stage1=original['stage1'][:-1])):
            np.savez(path, allow_pickle=False, **replacement)
            write_json(sidecar, {**metadata, 'result_sha256': sha256(path)})
            with self.assertRaisesRegex(ValueError, 'shape or dtype'):
                load_output_metadata(path)

    def test_importing_new_interfaces_remains_lightweight(self):
        command = 'from incfinder import load_output_metadata,require_supported_policy,migrate_v1_results; import sys; assert not any(n in sys.modules for n in ("numpy","torch","sklearn","transformers","openpyxl"))'
        result = subprocess.run([sys.executable, '-B', '-S', '-c', command], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
