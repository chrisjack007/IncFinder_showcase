"""Run identity, parameter provenance, and no-overwrite regression tests."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from incfinder import IncFinderError, load_run_record, predict
from ntv3_tsrf import api, cli, resources, run_record
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.output_metadata import RESULT_FILES, WORKBOOK
from ntv3_tsrf.report import export_workbook
from output_helpers import make_result


FROZEN = ROOT / 'tests/fixtures'
SAMPLE = FROZEN / 'reference.fasta'
SECRET = 'ghp_testingOnly_0123456789'
MODERN_ARGUMENTS = ('--input', str(SAMPLE))


class RunRecordTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.base / 'state'
        self.models = self.base / 'models'
        for name in ('ntv3', 'rf'):
            (self.models / name).mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.state)))
        os.environ.pop('INCFINDER_MODEL_DIR', None)
        self.manifest_hash = sha256(ROOT / 'config/assets.json')
        self.enterContext(patch.object(api, 'environment', return_value={'python': '3.12.15'}))
        self.enterContext(patch.object(api, 'verify_package', return_value={'kind': 'source'}))
        self.enterContext(patch.object(api, 'verify_assets', return_value={'manifest_sha256': self.manifest_hash}))
        self.real_worker = api.worker
        self.enterContext(patch.object(api, 'worker', side_effect=self.fake_worker))

    def fake_worker(self, executable, module, arguments, log, device='cpu'):
        if module == 'ntv3_tsrf.classify':
            result = self.base / str(uuid4())
            mode = arguments[arguments.index('--threshold-mode') + 1]
            status = arguments[arguments.index('--scientific-status') + 1]
            make_result(result, mode=mode, scientific_status=status)
            for path in result.iterdir():
                if path.name not in ('run_summary.json', 'report_environment.json', WORKBOOK):
                    shutil.copyfile(path, arguments[2] / path.name)
        elif module == 'ntv3_tsrf.report':
            export_workbook(arguments[0], arguments[-1])
        else:
            self.fail(f'Unexpected worker: {module}')

    def run_api(self, output=None, **overrides):
        options = {'input': SAMPLE, 'output': output or self.base / 'result',
                   'model_dir': self.models, 'features_dir': FROZEN, **overrides}
        return predict(**options)

    def records(self):
        return sorted(self.state.glob('*/run.json'))

    def assert_failure(self, output, stage, status='preflight_failed', **options):
        with self.assertRaises(IncFinderError) as caught:
            self.run_api(output, **options)
        exc = caught.exception
        self.assertIn(exc.run_id, str(exc))
        self.assertEqual(UUID(exc.run_id).version, 4)
        self.assertEqual(exc.record_path, self.state / exc.run_id / 'run.json')
        record = load_run_record(exc.run_id)
        self.assertEqual(record['status'], status)
        self.assertEqual(record['error']['stage'], stage)
        self.assertGreaterEqual(record['completed_utc'], record['started_utc'])
        self.assertFalse(any(self.state.glob('*/run.tmp')))
        return record

    def test_success_binds_summary_to_record_and_contracts(self):
        output = self.base / 'result'
        summary = self.run_api(output)
        run_id = summary['run_id']
        self.assertEqual(read_json(output / 'run_summary.json'), summary)
        self.assertEqual(UUID(run_id).version, 4)
        record = load_run_record(run_id)
        self.assertEqual((record['entry_point'], record['status'], record['stage']), ('api', 'succeeded', 'complete'))
        self.assertEqual(record['input_sha256'], sha256(SAMPLE))
        self.assertEqual(record['contract_sha256'], summary['contract_sha256'])
        self.assertEqual(record['runtime_policy_sha256'], summary['runtime_policy_sha256'])
        self.assertEqual(record['output_contract']['output_schema_version'], 2)
        self.assertEqual(record['output_contract']['output_contract_sha256'], summary['output_contract_sha256'])
        self.assertEqual(record['model']['manifest_sha256'], self.manifest_hash)
        self.assertEqual(record['features'], {'external': True, 'sha256': sha256(FROZEN / 'features.npy')})
        self.assertEqual(record['interpreters']['launcher']['path'], sys.executable)
        self.assertFalse(record['interpreters']['embedding']['invoked'])
        self.assertEqual(len(list(output.glob('*.metadata.json'))), len(RESULT_FILES))
        self.assertEqual(list(self.base.glob('*.lock')), [])

    def test_api_explicit_default_values_keep_explicit_sources(self):
        first = load_run_record(self.run_api(self.base / 'first')['run_id'])['parameters']
        second = load_run_record(self.run_api(self.base / 'second', device='cpu', batch_size=1, threads=4, jobs=1,
                                             threshold_mode='ct', max_length=10000,
                                             allow_encoder_drift=False, allow_out_of_domain=False)['run_id'])['parameters']
        names = ('device', 'batch_size', 'threads', 'jobs', 'threshold_mode',
                 'max_length', 'allow_encoder_drift', 'allow_out_of_domain')
        self.assertEqual({name: first[name]['value'] for name in names}, {name: second[name]['value'] for name in names})
        self.assertTrue(all(first[name]['source'] == 'default' for name in names))
        self.assertTrue(all(second[name]['source'] == 'explicit' for name in names))

    def test_invalid_api_binding_keeps_type_error_and_has_record(self):
        with self.assertRaises(TypeError) as caught:
            predict(output='out', password=SECRET)
        exc = caught.exception
        record = load_run_record(exc.run_id)
        self.assertEqual(record['parameters'], {})
        self.assertEqual(record['error'], {'stage': 'arguments', 'reason': 'invalid_type'})
        self.assertNotIn(SECRET, exc.record_path.read_text(encoding='ascii'))

    def test_argument_and_input_failures_have_unique_ids(self):
        runs = [
            self.assert_failure(self.base / 'bad-jobs', 'arguments', jobs=0),
            self.assert_failure(self.base / 'bad-input', 'input', input=self.base / 'missing.fa'),
            self.assert_failure(self.base / 'bad-device', 'arguments', device=SECRET),
        ]
        self.assertEqual(len({r['run_id'] for r in runs}), 3)
        self.assertEqual(runs[0]['parameters']['jobs'], {'value': 0, 'source': 'explicit'})
        self.assertEqual(runs[1]['error']['reason'], 'file_not_found')
        self.assertIsNone(runs[2]['parameters']['device']['value'])
        self.assertEqual(list(self.base.glob('.ntv3-run-*')), [])

    def test_output_contract_failure_precedes_model_resolution(self):
        target = self.base / 'new'
        with patch.object(api, 'load_output_contract', side_effect=ValueError(f'contract failed {SECRET}')), \
             patch.object(api, 'model_location') as select:
            record = self.assert_failure(target, 'output_contract')
        select.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertFalse(target.exists())

    def test_model_preflight_fails_with_id(self):
        record = self.assert_failure(self.base / 'result', 'model_location', model_dir=self.base / 'missing-models')
        self.assertEqual(record['parameters']['model_dir']['source'], 'explicit')
        self.assertEqual(record['input_sha256'], sha256(SAMPLE))

    def test_asset_failure_creates_no_result_staging(self):
        with patch.object(api, 'verify_assets', side_effect=ValueError(f'asset failed {SECRET}')):
            record = self.assert_failure(self.base / 'out', 'assets')
        self.assertEqual(record['model']['source'], 'argument')
        self.assertFalse(list(self.base.glob('.ntv3-run-*')))
        self.assertFalse(list(self.base.glob('*.lock')))

    def test_worker_failure_marker_and_run_record_drop_diagnostic_text(self):
        output = self.base / 'result'
        with patch.object(api, 'worker', side_effect=RuntimeError(f'worker failed {SECRET}')):
            record = self.assert_failure(output, 'classification', 'execution_failed')
        self.assertFalse(output.exists())
        diagnostic = Path(record['diagnostic_directory']) / 'failure.json'
        self.assertEqual(read_json(diagnostic), {'status': 'FAILED', 'run_id': record['run_id'], 'error': record['error']})
        self.assertFalse(any(SECRET in p.read_text(encoding='utf-8') for p in [diagnostic, *self.records()]))
        self.assertEqual(load_run_record(self.run_api(output)['run_id'])['status'], 'succeeded')

    def test_keyboard_interrupt_finalizes_record_and_releases_reservation(self):
        with patch.object(api, 'worker', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_api()
        record = load_run_record(caught.exception.run_id)
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['error']['reason'], 'cancelled')
        self.assertFalse(list(self.base.glob('*.lock')))

    def test_path_secrets_and_environment_payload_are_not_snapshotted(self):
        with patch.dict(os.environ, INCFINDER_TEST_SECRET=SECRET):
            record = self.assert_failure(self.base / f'token={SECRET}', 'input',
                                         input=self.base / f'password={SECRET}.fa')
        self.assertEqual(record['parameters']['input']['value'], '[redacted]')
        self.assertEqual(record['parameters']['output']['value'], '[redacted]')
        text = self.records()[0].read_text(encoding='ascii')
        self.assertNotIn(SECRET, text)
        self.assertNotIn('INCFINDER_TEST_SECRET', text)

    def test_real_worker_capture_only_keeps_exit_status(self):
        def process(command, *, stdout, **kwargs):
            stdout.write(('Authorization: Bearer ' + SECRET).encode('utf-8'))
            return 11
        log = self.base / 'worker.log'
        with patch.object(api, 'run_process_tree', side_effect=process):
            with self.assertRaisesRegex(RuntimeError, 'exit code 11'):
                self.real_worker(sys.executable, 'test.worker', [], log)
        self.assertEqual(log.read_text(encoding='utf-8'), 'test.worker exit code 11\n')

    def test_model_choice_sources(self):
        binding = self.base / 'model-location.json'
        write_json(binding, {'schema': 1, 'model_dir': str(self.models)})
        with patch.object(resources, 'binding_path', return_value=binding):
            bound_id = self.run_api(self.base / 'bound', model_dir=None)['run_id']
            with patch.dict(os.environ, INCFINDER_MODEL_DIR=str(self.models)):
                env_id = self.run_api(self.base / 'env', model_dir=None)['run_id']
                explicit_id = self.run_api(self.base / 'explicit')['run_id']
        for identifier, source in ((bound_id, 'environment_binding'), (env_id, 'INCFINDER_MODEL_DIR'),
                                   (explicit_id, 'argument')):
            record = load_run_record(identifier)
            self.assertEqual(record['model']['source'], source)
            self.assertEqual(record['parameters']['model_dir']['value'], str(self.models))

    def test_cli_shorthand_records_flags_and_omissions(self):
        result = self.base / 'modern'
        args = [*MODERN_ARGUMENTS, '--output', str(result), '--features-dir', str(FROZEN),
                '--model-dir', str(self.models), '--device=cpu', '--batch-size', '1', '--threshold-mode=u05']
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        record = load_run_record(json.loads(stdout.getvalue())['run_id'])
        self.assertEqual(record['entry_point'], 'cli')
        for label, value in (('device', 'cpu'), ('batch_size', 1), ('threshold_mode', 'u05')):
            self.assertEqual(record['parameters'][label], {'value': value, 'source': 'explicit'})
        self.assertEqual(record['parameters']['threads'], {'value': 4, 'source': 'default'})

    def test_legacy_entry_records_explicit_interpreter_and_root_relative_input(self):
        result = self.base / 'legacy'
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['predict', 'tests/fixtures/reference.fasta', '-o', str(result),
                                       '--features-dir', 'tests/fixtures', '--model-dir', str(self.models),
                                       '--rf-python', sys.executable, '--threads', '4'], legacy=True), 0)
        record = load_run_record(json.loads(stdout.getvalue())['run_id'])
        self.assertEqual(record['entry_point'], 'legacy_cli')
        self.assertEqual(record['parameters']['input']['value'], str(SAMPLE))
        self.assertEqual(record['parameters']['threads'], {'value': 4, 'source': 'explicit'})
        self.assertEqual(record['interpreters']['rf']['source'], 'explicit')
        self.assertEqual(record['interpreters']['report']['source'], 'default')

    def test_parse_errors_are_logged_for_both_commands(self):
        for args, legacy in ((['--device', SECRET], False), (['predict', '--input', 'a.fa'], True),
                             (['predict', 'a.fa', '-i', 'b.fa', '-o', 'c'], False),
                             (['predict', '-', '-o', 'c'], True)):
            with self.subTest(args=args, legacy=legacy), redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as caught:
                    cli.main(args, legacy=legacy)
                self.assertEqual(caught.exception.code, 2)
            match = re.search(r'Run ID ([a-f0-9-]+)', stderr.getvalue())
            self.assertIsNotNone(match)
            record = load_run_record(match.group(1))
            self.assertEqual(record['entry_point'], 'legacy_cli' if legacy else 'cli')
            self.assertEqual(record['error'], {'stage': 'arguments', 'reason': 'argument_parsing_failed'})
            self.assertNotIn(SECRET, json.dumps(record))
        self.assertEqual(len(self.records()), 4)

    def test_help_version_and_doctor_create_no_record(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
             patch.object(cli, 'doctor', return_value={'status': 'PASS'}):
            for args in ([], ['help', 'predict'], ['version'], ['doctor']):
                self.assertEqual(cli.main(args), 0)
            for args in (['--help'], ['predict', '--help'], ['--version']):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(args)
                self.assertEqual(caught.exception.code, 0)
        self.assertFalse(self.state.exists())

    def test_preview_and_out_of_domain_flags_are_snapshotted(self):
        features = self.base / 'preview-features'
        features.mkdir()
        metadata = read_json(FROZEN / 'features.json')
        write_json(features / 'features.json', {**metadata, 'assets_sha256': sha256(ROOT / 'config/assets.json'),
                                                  'scientific_equivalence': 'NOT_ESTABLISHED'})
        shutil.copyfile(FROZEN / 'features.npy', features / 'features.npy')
        def contents(path):
            result = read_json(path)
            if path == ROOT / 'config/contract.json':
                result['validated_length_bp'] = [10000, 10000]
            return result
        with patch.object(api, 'read_json', side_effect=contents):
            denied = self.assert_failure(self.base / 'denied', 'input_policy', features_dir=features,
                                         allow_encoder_drift=True)
            summary = self.run_api(features_dir=features, allow_encoder_drift=True, allow_out_of_domain=True)
        record = load_run_record(summary['run_id'])
        self.assertEqual(summary['status'], 'RESEARCH_PREVIEW')
        self.assertTrue(record['research_preview'])
        self.assertGreater(record['out_of_domain_count'], 0)
        self.assertEqual(record['out_of_domain_count'], denied['out_of_domain_count'])
        for name in ('allow_encoder_drift', 'allow_out_of_domain'):
            self.assertEqual(record['parameters'][name], {'value': True, 'source': 'explicit'})

    def test_independent_runs_and_replay_keep_distinct_ids(self):
        first = self.run_api(self.base / 'first')['run_id']
        second = self.run_api(self.base / 'second')['run_id']
        result = self.base / 'replay'
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['predict', str(SAMPLE), '-o', str(result), '--features-dir', str(FROZEN),
                                       '--model-dir', str(self.models), '--parent-run-id', first]), 0)
        third = json.loads(stdout.getvalue())['run_id']
        self.assertEqual(len({first, second, third}), 3)
        self.assertIsNone(load_run_record(second)['parent_run_id'])
        self.assertEqual(load_run_record(third)['parent_run_id'], first)
        self.assertEqual(load_run_record(third)['parameters']['parent_run_id'], {'value': first, 'source': 'explicit'})

    def test_replay_rejects_unknown_invalid_and_running_parent(self):
        running = self.enterContext(run_record.RunRecord('api'))
        for parent in ('../secret', str(uuid4()), running.run_id):
            with self.subTest(parent=parent):
                record = self.assert_failure(self.base / 'result', 'parent', parent_run_id=parent)
                self.assertIsNone(record['parent_run_id'])
        with self.assertRaises(ValueError):
            load_run_record('..')

    def test_every_existing_destination_is_rejected(self):
        output = self.base / 'existing'
        output.mkdir()
        record = self.assert_failure(output, 'paths')
        self.assertEqual(record['error']['reason'], 'destination_occupied')
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])
        plain = self.base / 'plain'
        plain.write_text('keep', encoding='utf-8')
        self.assert_failure(plain, 'paths')
        self.assertEqual(plain.read_text(encoding='utf-8'), 'keep')

    def test_output_reservation_excludes_a_different_process(self):
        output = self.base / 'shared'
        expectation = ("from pathlib import Path\nimport sys\nfrom ntv3_tsrf.run_record import reserve_output\n"
                       "with reserve_output(Path(sys.argv[1]), sys.argv[2]):\n"
                       "    print('reserved', flush=True)\n    sys.stdin.readline()\n")
        with subprocess.Popen([sys.executable, '-s', '-B', '-c', expectation, str(output), str(uuid4())],
                              cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf-8') as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), 'reserved')
                self.assert_failure(output, 'output_reservation')
                self.assertFalse(output.exists())
            finally:
                _, error = process.communicate('\n', timeout=10)
                self.assertEqual(process.returncode, 0, error)
        self.assertTrue(self.run_api(output)['run_id'])

    @unittest.skipUnless(sys.platform in ('win32', 'linux'), 'Windows and Linux publication semantics')
    def test_no_replace_even_if_target_appears_after_final_check(self):
        staged, output = self.base / 'staged', self.base / 'late'
        staged.mkdir()
        (staged / 'payload').write_bytes(b'candidate')
        def create_after_check(path):
            self.assertFalse(path.exists())
            path.mkdir()
        with patch.object(run_record, 'require_new_output', side_effect=create_after_check):
            with self.assertRaises(FileExistsError):
                run_record.publish_output(staged, output)
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual((staged / 'payload').read_bytes(), b'candidate')

    def test_default_state_directory_selection(self):
        with patch.dict(os.environ), patch.object(Path, 'home', return_value=self.base):
            os.environ.pop('INCFINDER_RUN_DIR', None)
            if os.name == 'nt':
                local = self.base / 'app-data'
                os.environ['LOCALAPPDATA'] = str(local)
                self.assertEqual(run_record._record_root(), local / 'IncFinder/runs')
                os.environ.pop('LOCALAPPDATA')
                self.assertEqual(run_record._record_root(), self.base / 'AppData/Local/IncFinder/runs')
            else:
                local = self.base / 'xdg'
                os.environ['XDG_STATE_HOME'] = str(local)
                self.assertEqual(run_record._record_root(), local / 'incfinder/runs')
                os.environ['XDG_STATE_HOME'] = 'relative-value'
                self.assertEqual(run_record._record_root(), self.base / '.local/state/incfinder/runs')
