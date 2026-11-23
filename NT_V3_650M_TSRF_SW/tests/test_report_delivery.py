"""Result delivery, report-only provenance and interrupted-publication regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from incfinder import (IncFinderError, export_artifact_archive, inspect_lineage, load_run_events,
                       load_run_record, predict, rebuild_report, rebuild_run_state, reconcile_run, verify_artifacts)
from ntv3_tsrf import api, cli
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.output_metadata import CORE_RESULT_FILES, RESULT_FILES, WORKBOOK
from ntv3_tsrf.report import export_workbook
from openpyxl import load_workbook

from output_helpers import make_result


FIXTURES = ROOT / 'tests/fixtures'
BLOCK_WORKERS = """
import sys
from importlib.abc import MetaPathFinder
class Guard(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {'torch', 'transformers', 'joblib', 'sklearn', 'ntv3_tsrf.embedding', 'ntv3_tsrf.classify'}
        if fullname in blocked:
            raise AssertionError('Report attempted to import ' + fullname)
sys.meta_path.insert(0, Guard())
"""

STOP_PREDICTION = """
import sys, time, shutil
from pathlib import Path
from unittest.mock import patch
from ntv3_tsrf import api
from ntv3_tsrf.common import ROOT, sha256
from ntv3_tsrf.output_metadata import CORE_RESULT_FILES, WORKBOOK
from ntv3_tsrf.report import export_workbook
fixture, output, marker = map(Path, sys.argv[1:4])
point = sys.argv[4]
seal_artifacts = api.write_run_artifacts
publish = api.publish_output

def stop(core):
    marker.write_text(str(core), encoding='utf-8')
    time.sleep(60)

def worker(executable, module, args, log, device='cpu'):
    if module == 'ntv3_tsrf.classify':
        for name in (*CORE_RESULT_FILES, 'prediction.json'):
            shutil.copyfile(fixture / name, args[2] / name)
    elif module == 'ntv3_tsrf.report':
        if point == 'report':
            (args[-1] / WORKBOOK).write_bytes(b'unfinished')
            stop(args[0])
        export_workbook(args[0], args[-1])
    else:
        raise AssertionError(module)

def seal(staging, summary, *args):
    if point == 'assembly' and summary['delivery']['report_status'] == 'succeeded':
        stop(staging)
    return seal_artifacts(staging, summary, *args)

def publish_at(staging, output):
    if point == 'prepared':
        stop(staging)
    publish(staging, output)
    if point == 'published':
        stop(staging)

with patch.object(api, 'environment', return_value={}), patch.object(api, 'verify_package', return_value={}), \
     patch.object(api, 'verify_assets', return_value={'manifest_sha256': sha256(ROOT / 'config/assets.json')}), \
     patch.object(api, 'worker', side_effect=worker), patch.object(api, 'write_run_artifacts', side_effect=seal), \
     patch.object(api, 'publish_output', side_effect=publish_at):
    api.predict(input=ROOT / 'tests/fixtures/reference.fasta', output=output,
                features_dir=ROOT / 'tests/fixtures', model_dir=output.parent / 'models')
"""


def workbook_cells(path):
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        return [(sheet.title, [tuple((cell.value, cell.data_type) for cell in row)
                               for row in sheet.iter_rows()]) for sheet in book]
    finally:
        book.close()


class ReportDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.samples = {}
        for mode in ('ct', 'u05'):
            cls.samples[mode] = folder / mode
            prediction, _, decisions = make_result(cls.samples[mode], mode=mode)
            gate = decisions['gate']
            prediction['gate_counts'] = {'passed': int(gate.sum()), 'rejected': int((~gate).sum())}
            prediction['internal_counts'] = {
                name: int((decisions['internal_status'] == name).sum())
                for name in ('Unclassified', 'Inc_no_call', 'Inc_called')
            }
            write_json(cls.samples[mode] / 'prediction.json', prediction)

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.base / 'prediction'
        self.state = self.base / 'runs'
        self.models = self.base / 'models'
        for name in ('ntv3', 'rf'):
            (self.models / name).mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.state)))
        self.enterContext(patch.object(api, 'environment', return_value={'python': '3.12.15'}))
        self.enterContext(patch.object(api, 'verify_package', return_value={}))
        self.enterContext(patch.object(api, 'verify_assets',
                                       return_value={'manifest_sha256': sha256(ROOT / 'config/assets.json')}))
        self.mock_worker = self.enterContext(patch.object(api, 'worker', side_effect=self.worker))

    def worker(self, executable, module, args, log, device='cpu'):
        if module == 'ntv3_tsrf.classify':
            mode = args[args.index('--threshold-mode') + 1]
            for name in (*CORE_RESULT_FILES, 'prediction.json'):
                shutil.copyfile(self.samples[mode] / name, args[2] / name)
        elif module == 'ntv3_tsrf.report':
            return export_workbook(args[0], args[-1])
        else:
            raise AssertionError(f'Unexpected process: {module}')

    def run_prediction(self, **options):
        return predict(**{'input': FIXTURES / 'reference.fasta', 'features_dir': FIXTURES,
                          'output': self.output, 'model_dir': self.models, **options})

    def assert_delivery(self, summary, report_status):
        has_excel = report_status == 'succeeded'
        files = RESULT_FILES if has_excel else CORE_RESULT_FILES
        self.assertEqual(summary['delivery']['report_status'], report_status)
        self.assertEqual(summary['delivery']['produced_formats'], ['csv', 'npz', 'xlsx'] if has_excel else ['csv', 'npz'])
        self.assertEqual({path.name for path in self.output.glob('*.metadata.json')},
                         {name + '.metadata.json' for name in files})
        self.assertEqual((self.output / WORKBOOK).is_file(), has_excel)
        self.assertEqual((self.output / 'report_environment.json').is_file(), has_excel)
        verify_artifacts(self.output)
        mode = summary['prediction']['threshold_mode']
        for name in CORE_RESULT_FILES:
            self.assertEqual(sha256(self.output / name), sha256(self.samples[mode] / name))

    def test_default_report_starts_after_computation_event_and_core_files(self):
        def worker(executable, module, args, log, device='cpu'):
            if module == 'ntv3_tsrf.report':
                core, report = args[0], args[-1]
                self.assertNotEqual(core, report)
                checkpoint = read_json(core / 'run_summary.json')
                events = load_run_events(checkpoint['run_id'])
                self.assertEqual(events[-2]['event_type'], 'computation_sealed')
                self.assertEqual(events[-2]['payload']['artifact_manifest_sha256'], sha256(core / 'artifact_manifest.json'))
                self.assertFalse(self.output.exists())
                self.assertFalse((core / WORKBOOK).exists())
            return self.worker(executable, module, args, log, device)
        self.mock_worker.side_effect = worker
        summary = self.run_prediction()
        self.assert_delivery(summary, 'succeeded')
        self.assertEqual(workbook_cells(self.output / WORKBOOK), workbook_cells(self.samples['ct'] / WORKBOOK))
        self.assertEqual(load_run_record(summary['run_id'])['status'], 'succeeded')

    def test_lightweight_run_never_invokes_report(self):
        summary = self.run_prediction(output_formats=('npz', 'csv'))
        self.assert_delivery(summary, 'not_requested')
        self.assertEqual([call.args[1] for call in self.mock_worker.call_args_list], ['ntv3_tsrf.classify'])
        record = load_run_record(summary['run_id'])
        self.assertEqual(record['parameters']['output_formats'], {'source': 'explicit', 'value': ['csv', 'npz']})
        self.assertFalse(record['interpreters']['report']['invoked'])
        self.assertEqual(record, rebuild_run_state(summary['run_id']))

    def test_lightweight_cli(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(['predict', str(FIXTURES / 'reference.fasta'), '--features-dir', str(FIXTURES),
                               '--model-dir', str(self.models), '--output', str(self.output), '--output-formats', 'csv,npz'])
        self.assertEqual(status, 0, stderr.getvalue())
        self.assert_delivery(json.loads(stdout.getvalue()), 'not_requested')

    def test_invalid_delivery_modes_stop_before_workers(self):
        for value in ('xlsx', 'csv', 'csv,npz,csv', '', ('npz', 'pdf'), None, {'csv', 'npz'}):
            with self.subTest(value=value), self.assertRaisesRegex(IncFinderError, 'output_formats'):
                self.run_prediction(output_formats=value)
        self.mock_worker.assert_not_called()
        self.assertFalse(self.output.exists())

    def fail_report(self, executable, module, args, log, device='cpu'):
        if module == 'ntv3_tsrf.report':
            (args[-1] / WORKBOOK).write_bytes(b'partial')
            raise RuntimeError('Injected report failure')
        self.worker(executable, module, args, log, device)

    def test_report_failure_publishes_core_and_allows_retry(self):
        self.mock_worker.side_effect = self.fail_report
        with self.assertRaisesRegex(IncFinderError, 'Requested XLSX report failed') as caught:
            self.run_prediction(threshold_mode='u05')
        failure = caught.exception
        self.assertEqual(failure.computed_output, self.output)
        summary = read_json(self.output / 'run_summary.json')
        self.assertEqual(summary['run_id'], failure.run_id)
        self.assert_delivery(summary, 'failed')
        record = load_run_record(failure.run_id)
        self.assertEqual((record['status'], record['publication']['report_status']), ('execution_failed', 'failed'))
        self.assertEqual(record, rebuild_run_state(failure.run_id))
        self.assertEqual(record, reconcile_run(failure.run_id))
        self.assertTrue(inspect_lineage(self.output)['verification']['source_rows_and_csv_match'])
        rebuilt = self.base / 'rebuilt'
        rebuild_report(failure.computed_output, rebuilt)
        self.assertEqual(workbook_cells(rebuilt / WORKBOOK), workbook_cells(self.samples['u05'] / WORKBOOK))
        self.assertEqual([call.args[1] for call in self.mock_worker.call_args_list],
                         ['ntv3_tsrf.classify', 'ntv3_tsrf.report'])

    def test_failed_cli_reports_the_verified_output(self):
        self.mock_worker.side_effect = self.fail_report
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = cli.main(['predict', str(FIXTURES / 'reference.fasta'), '--features-dir', str(FIXTURES),
                               '--model-dir', str(self.models), '--output', str(self.output)])
        self.assertEqual(status, 2)
        self.assertIn(str(self.output), stderr.getvalue())
        summary = read_json(self.output / 'run_summary.json')
        self.assertIn(summary['run_id'], stderr.getvalue())
        self.assert_delivery(summary, 'failed')

    def test_keyboard_interrupt_during_report_publishes_core(self):
        def worker(executable, module, args, log, device='cpu'):
            if module == 'ntv3_tsrf.report':
                raise KeyboardInterrupt()
            return self.worker(executable, module, args, log, device)
        self.mock_worker.side_effect = worker
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_prediction()
        self.assertTrue(caught.exception.published)
        self.assertEqual(caught.exception.computed_output, self.output)
        self.assert_delivery(read_json(self.output / 'run_summary.json'), 'interrupted')

    def stop_run(self, point, corrupt=False):
        marker = self.base / 'waiting'
        stderr = self.base / 'child.log'
        with stderr.open('wb') as log:
            child = subprocess.Popen([sys.executable, '-B', '-c', STOP_PREDICTION, str(self.samples['ct']),
                                      str(self.output), str(marker), point], cwd=ROOT, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 40
                while not marker.is_file() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.03)
                if not marker.is_file():
                    self.fail(stderr.read_text(encoding='utf-8'))
                staging = next(self.base.glob('.ntv3-run-*'), None)
                if corrupt:
                    (staging / 'probabilities.npz').write_bytes(b'changed computation')
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
        runs = list(self.state.iterdir())
        self.assertEqual(len(runs), 1)
        return runs[0].name

    def check_force_recovery(self, point, status):
        run_id = self.stop_run(point)
        restored = reconcile_run(run_id)
        self.assertEqual(restored['publication']['report_status'], status)
        self.assertEqual(load_run_events(run_id)[-1]['event_type'], 'publication_recovered')
        self.assertEqual(reconcile_run(run_id), restored)
        self.assertEqual(rebuild_run_state(run_id), restored)
        self.assert_delivery(read_json(self.output / 'run_summary.json'), status)
        self.assertFalse(list(self.base.glob('*.lock')))

    def test_hard_stop_during_report_recovers_computation(self):
        self.check_force_recovery('report', 'interrupted')

    def test_hard_stop_during_final_assembly_restores_computation_envelope(self):
        self.check_force_recovery('assembly', 'interrupted')

    def test_hard_stop_before_rename_preserves_prepared_full_result(self):
        self.check_force_recovery('prepared', 'succeeded')

    def test_hard_stop_after_rename_reconciles_and_releases_lease(self):
        self.check_force_recovery('published', 'succeeded')

    def test_recovery_rejects_tampered_computation(self):
        run_id = self.stop_run('report', corrupt=True)
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            reconcile_run(run_id)
        self.assertFalse(self.output.exists())

    def test_recovery_never_overwrites_an_unrelated_destination(self):
        run_id = self.stop_run('report')
        self.output.mkdir()
        sentinel = self.output / 'occupied'
        sentinel.write_bytes(b'existing result')
        with self.assertRaises(FileExistsError):
            reconcile_run(run_id)
        self.assertEqual(sentinel.read_bytes(), b'existing result')
        alternate = self.base / 'occupied-backup'
        self.output.rename(alternate)
        self.assertEqual(reconcile_run(run_id)['status'], 'interrupted')

    def test_report_rebuild_from_reference_archive_with_external_files_gone(self):
        inputs = self.base / 'inputs'
        inputs.mkdir()
        for name in ('features.npy', 'features.json', 'reference.fasta'):
            shutil.copyfile(FIXTURES / name, inputs / name)
        summary = self.run_prediction(input=inputs / 'reference.fasta', features_dir=inputs,
                                      threshold_mode='u05', output_formats='csv,npz')
        archive = self.base / 'archive'
        export_artifact_archive(self.output, archive)
        graph = inspect_lineage(archive)
        self.assertFalse(any(activity['stage'] == 'report' for activity in graph['activities']))
        self.assertEqual(graph['identity']['delivery'], summary['delivery'])
        inputs.rename(self.base / 'moved')
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            verify_artifacts(archive)
        before = {path.name: sha256(path) for path in archive.iterdir() if path.is_file()}
        report = self.base / 'offline-report'
        code = BLOCK_WORKERS + "\nimport incfinder\nfrom ntv3_tsrf.cli import main\nraise SystemExit(main(sys.argv[1:]))\n"
        completed = subprocess.run([sys.executable, '-B', '-c', code, 'report', str(archive),
                                    '--output', str(report), '--manifest-sha256', before['artifact_manifest.json']],
                                   cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt, read_json(report / 'report_receipt.json'))
        self.assertEqual(receipt['source']['manifest_sha256'], before['artifact_manifest.json'])
        self.assertEqual(receipt['source']['run_manifest_sha256'], sha256(self.output / 'artifact_manifest.json'))
        self.assertEqual(receipt['source']['run_id'], summary['run_id'])
        self.assertEqual(len(receipt['relations']), 5)
        self.assertEqual(workbook_cells(report / WORKBOOK), workbook_cells(self.samples['u05'] / WORKBOOK))
        self.assertEqual(before, {path.name: sha256(path) for path in archive.iterdir() if path.is_file()})

    def test_report_rejects_bad_digest_missing_member_and_changed_member(self):
        self.run_prediction(output_formats='csv,npz')
        report = self.base / 'report'
        with self.assertRaisesRegex(ValueError, 'manifest SHA-256'):
            rebuild_report(self.output, report, expected_manifest_sha256='0' * 64)
        path = self.output / 'inc_results.csv'
        content = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            rebuild_report(self.output, report)
        path.write_bytes(content.replace(b'row_index', b'row_indey', 1))
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            rebuild_report(self.output, report)
        self.assertFalse(report.exists())

    def test_report_failure_retry_preserves_existing_and_source_directories(self):
        self.run_prediction(output_formats='csv,npz')
        report = self.base / 'report'
        with patch('ntv3_tsrf.report.export_workbook', side_effect=RuntimeError('report failure')):
            with self.assertRaisesRegex(RuntimeError, 'report failure'):
                rebuild_report(self.output, report)
        self.assertFalse(report.exists())
        valid = rebuild_report(self.output, report)
        self.assertTrue(valid['source']['publication_event_bound'])
        expected = sha256(report / WORKBOOK)
        with self.assertRaises(FileExistsError):
            rebuild_report(self.output, report)
        with self.assertRaisesRegex(ValueError, 'outside'):
            rebuild_report(self.output, self.output / 'nested')
        self.assertEqual(sha256(report / WORKBOOK), expected)
        self.assertFalse(list(self.base.glob('*.lock')))
        verify_artifacts(self.output)


    def test_corrupt_report_worker_output_leaves_only_verified_computation(self):
        def worker(executable, module, args, log, device='cpu'):
            if module == 'ntv3_tsrf.report':
                (args[-1] / WORKBOOK).write_bytes(b'invalid spreadsheet')
            else:
                self.worker(executable, module, args, log, device)
        self.mock_worker.side_effect = worker
        with self.assertRaises(IncFinderError) as caught:
            self.run_prediction()
        self.assertEqual(caught.exception.computed_output, self.output)
        self.assert_delivery(read_json(self.output / 'run_summary.json'), 'failed')

    def test_lightweight_preflight_does_not_require_report_dependencies(self):
        def environment(role):
            if role != 'rf':
                raise AssertionError('Unrelated environment requirements')
            return {'python': '3.12.15'}
        with patch.object(api, 'environment', side_effect=environment):
            summary = self.run_prediction(output_formats='csv,npz')
        self.assert_delivery(summary, 'not_requested')

    def test_two_report_publishers_cannot_replace_the_same_destination(self):
        self.run_prediction(output_formats='csv,npz')
        report = self.base / 'report'
        started, resume = Event(), Event()

        def hold(source, destination):
            started.set()
            if not resume.wait(15):
                raise AssertionError('Report publisher was not resumed')
            export_workbook(source, destination)

        with patch('ntv3_tsrf.report.export_workbook', side_effect=hold), ThreadPoolExecutor(2) as pool:
            first = pool.submit(rebuild_report, self.output, report)
            try:
                self.assertTrue(started.wait(15))
                second = pool.submit(rebuild_report, self.output, report)
                with self.assertRaises(FileExistsError):
                    second.result(timeout=15)
            finally:
                resume.set()
            receipt = first.result(timeout=15)
        self.assertEqual(sha256(report / WORKBOOK), receipt['outputs'][WORKBOOK])

    def test_report_rejects_inputs_changed_while_rendering(self):
        self.run_prediction(output_formats='csv,npz')
        report = self.base / 'report'
        saved = self.output / 'inc_results.csv'
        content = saved.read_bytes()

        def change_source(source, destination):
            export_workbook(source, destination)
            saved.write_bytes(content.replace(b'row_index', b'row_indey', 1))

        with patch('ntv3_tsrf.report.export_workbook', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, 'changed during report'):
                rebuild_report(self.output, report)
        self.assertFalse(report.exists())
