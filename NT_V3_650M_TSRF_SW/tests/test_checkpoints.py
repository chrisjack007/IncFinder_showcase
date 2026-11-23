"""Checkpoint durability, resume identity, and real process ownership."""
import _thread
from contextlib import closing, contextmanager
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from incfinder import IncFinderError, load_run_record, predict, reconcile_run
from ntv3_tsrf import checkpoints, cli, run_record
from ntv3_tsrf.checkpoints import DATABASE, FEATURE_WIDTH, CheckpointStore, identity_sha256, managed_checkpoints, plan_batches
from ntv3_tsrf.common import ROOT, sha256
from ntv3_tsrf.sequence_io import ordered_identity, read_records
from ntv3_tsrf.worker import run_process_tree


def sample_inputs(root):
    source = root / 'test.fasta'
    source.write_text('>x\n' + 'A' * 257 + '\n>y\n' + 'C' * 250 + '\n>z\n' + 'G' * 251 + '\n', encoding='ascii')
    records = read_records(source)
    identities = ordered_identity(records)
    expected = {
        'input_sha256': sha256(source), 'records_sha256': identity_sha256(identities),
        'model_manifest_sha256': sha256(ROOT / 'config/assets.json'),
        'contract_sha256': sha256(ROOT / 'config/contract.json'),
        'runtime_policy_sha256': sha256(ROOT / 'config/runtime.json'),
        'output_contract_sha256': sha256(ROOT / 'config/output_contract.json'),
        'parameters': {'device': 'cpu', 'threads': 4, 'batch_size': 1, 'jobs': 1, 'threshold_mode': 'ct'},
    }
    return source, records, identities, expected


def prepare_run(run, identity, resume=None):
    for stage in ('paths', 'assets', 'staging', 'embedding'):
        run.enter_stage(stage)
    run.prepare_checkpoints(identity, resume)


def values(batch):
    return np.full((len(batch.rows), FEATURE_WIDTH), batch.rows[0] + 1, dtype=np.float32)


def ready(run_id):
    print(run_id, flush=True)
    sys.stdin.readline()
    raise AssertionError('Parent must terminate this test process')


def checkpoint_child(folder, point):
    source, records, identities, identity = sample_inputs(folder)
    batches = list(plan_batches(records, identities, 1))
    first, second = batches[:2]
    with run_record.RunRecord('api') as run:
        prepare_run(run, identity)
        with managed_checkpoints(run.run_id, source, identities, device='cpu', batch_size=1, threads=4,
                                 runtime={'build': 'synthetic'}) as store:
            store.save(first, values(first))
            if point == 'partial_file':
                with (store.folder / (second.id + '.tmp')).open('xb') as stream:
                    stream.write(b'partial float block')
                    stream.flush()
                    ready(run.run_id)
            if point == 'before_ledger':
                with patch.object(store, '_register', side_effect=lambda *args: ready(run.run_id)):
                    store.save(second, values(second))
            if point == 'in_transaction':
                def uncommitted(batch, size, digest, reused):
                    with store.db:
                        store.db.execute('INSERT INTO batches VALUES (?, ?, ?, ?)', (batch.id, size, digest, reused))
                        ready(run.run_id)
                with patch.object(store, '_register', side_effect=uncommitted):
                    store.save(second, values(second))
            store.save(second, values(second))
            ready(run.run_id)


def is_running(pid):
    if sys.platform == 'linux':
        try:
            fields = (Path('/proc') / str(pid) / 'stat').read_text(encoding='ascii').rsplit(')', 1)[1]
        except FileNotFoundError:
            return False
        return fields.split()[0] != 'Z'
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = (w.DWORD, w.BOOL, w.DWORD), w.HANDLE
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = (w.HANDLE, w.DWORD), w.DWORD
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        if c.get_last_error() == 87:  # ERROR_INVALID_PARAMETER for a gone PID
            return False
        raise c.WinError()
    try:
        return k.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
    finally:
        k.CloseHandle(handle)


def tree_child(marker):
    grandchild = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(120)'])
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps([os.getpid(), grandchild.pid]), encoding='ascii')
    temporary.replace(marker)
    grandchild.wait(timeout=120)


def tree_launcher(marker):
    existing = os.environ.get('PYTHONPATH')
    paths = [str(Path(__file__).resolve().parent)]
    if existing:
        paths.append(existing)
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(paths)}
    command = [sys.executable, '-B', '-m', 'ntv3_tsrf.worker', 'test_checkpoints', 'tree', str(marker)]
    with TemporaryDirectory() as temporary, (Path(temporary) / 'worker.log').open('w+b') as output:
        return run_process_tree(command, cwd=Path.cwd(), env=env, stdout=output)


def wait_ready(marker, timeout=10):
    expires = time.monotonic() + timeout
    while time.monotonic() < expires:
        if marker.is_file():
            return json.loads(marker.read_bytes())
        time.sleep(0.02)
    raise AssertionError('The test worker tree did not start')


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix='if009 \u6d4b\u8bd5 '))).resolve()
        self.runs = self.base / 'runs'
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.runs)))
        self.source, records, self.identities, self.identity = sample_inputs(self.base)
        self.batches = list(plan_batches(records, self.identities, 1))
        self.runtime = {'build': 'synthetic'}

    def previous(self, run_id):
        state = reconcile_run(run_id)
        return {key: state[key] for key in ('run_id', 'attempt_id')}

    @contextmanager
    def fresh(self, *, resume=None, runtime=None, identity=None, source=None, observed=None):
        with run_record.RunRecord('api') as run:
            prepare_run(run, self.identity if identity is None else identity, resume)
            with managed_checkpoints(run.run_id, self.source if source is None else source,
                                     self.identities if observed is None else observed,
                                     device='cpu', batch_size=1, threads=4,
                                     runtime=self.runtime if runtime is None else runtime) as store:
                yield run, store
            run.fail(KeyboardInterrupt())

    def prior_finished(self):
        with self.fresh() as (run, store):
            for batch in self.batches:
                store.save(batch, values(batch))
        return self.previous(run.run_id), store.folder

    def crashed(self, point):
        script = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                  'from test_checkpoints import checkpoint_child; checkpoint_child(Path(sys.argv[2]),sys.argv[3])')
        with subprocess.Popen([sys.executable, '-B', '-c', script, str(Path(__file__).resolve().parent),
                               str(self.base), point], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf-8') as process:
            try:
                run_id = process.stdout.readline().strip()
                self.assertTrue(run_id and (self.runs / run_id).is_dir(), 'Child failed before its crash boundary')
                with self.assertRaisesRegex(RuntimeError, 'active writer'):
                    reconcile_run(run_id)
            finally:
                if process.poll() is None:
                    process.kill()
                _stdout, errors = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, errors)
        return self.previous(run_id)

    def check_recovery(self, point, reused):
        resume = self.crashed(point)
        self.assertEqual(load_run_record(resume['run_id'])['status'], 'interrupted')
        with self.fresh(resume=resume) as (run, store):
            observed = []
            for batch in self.batches:
                if store.reuse(batch):
                    observed.append(batch.id)
                else:
                    store.save(batch, values(batch))
            self.assertEqual(observed, [batch.id for batch in self.batches[:reused]])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM batches WHERE reused_from_run_id = ?',
                                              (resume['run_id'],)).fetchone()[0], reused)
            result = self.base / 'assembled.npy'
            store.assemble(iter(self.batches), 3, result)
            expected = np.repeat(np.arange(1, 4, dtype=np.float32)[:, None], FEATURE_WIDTH, axis=1)
            np.testing.assert_array_equal(np.load(result, allow_pickle=False), expected)
        self.assertNotEqual(run.run_id, resume['run_id'])

    def test_stable_batches_preserve_bucket_and_original_rows(self):
        records = [SimpleNamespace(sequence='A' * length) for length in (255, 129, 128, 129)]
        identity = [{'row_index': row, 'sha256': str(row)} for row in range(4)]
        def plan(entries):
            return list(plan_batches(records, entries, 2))
        first = plan(identity)
        self.assertEqual([(batch.padded_length, batch.rows) for batch in first],
                         [(128, (2,)), (256, (0, 1)), (256, (3,))])
        self.assertEqual(first, plan(identity))
        self.assertNotEqual(first[1].id, plan([*identity[:1], {'sha256': 'changed'}, *identity[2:]])[1].id)

    def test_killed_while_partial_block_was_open(self):
        self.check_recovery('partial_file', 1)

    def test_killed_after_atomic_rename_before_registration(self):
        self.check_recovery('before_ledger', 1)

    def test_killed_in_batch_transaction(self):
        self.check_recovery('in_transaction', 1)

    def test_killed_after_transaction_commit(self):
        self.check_recovery('after_commit', 2)

    def test_equal_size_tamper_missing_and_linked_blocks_are_recomputed(self):
        resume, old = self.prior_finished()
        first, second, third = [old / (batch.id + '.f32le') for batch in self.batches]
        digest = sha256(old / DATABASE)
        with first.open('r+b') as stream:
            initial = stream.read(1)
            stream.seek(0)
            stream.write(bytes((initial[0] ^ 1,)))
        second.unlink()
        alias = self.base / 'block-copy'
        os.link(third, alias)
        with self.fresh(resume=resume) as (_, store):
            for batch in self.batches:
                self.assertFalse(store.reuse(batch))
                store.save(batch, values(batch))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM batches WHERE reused_from_run_id IS NULL')
                             .fetchone()[0], 3)
        self.assertEqual(sha256(old / DATABASE), digest)

    def test_mismatched_effective_settings_and_environment_are_rejected(self):
        resume, old = self.prior_finished()
        previous = sha256(old / DATABASE)
        for param, value in (('jobs', 2), ('threshold_mode', 'u05')):
            with self.subTest(parameter=param), self.assertRaisesRegex(ValueError, 'identity mismatch: parameters'):
                settings = {**self.identity['parameters'], param: value}
                with self.fresh(resume=resume, identity={**self.identity, 'parameters': settings}):
                    self.fail('The different settings were accepted')
        with self.assertRaisesRegex(ValueError, 'identity mismatch: environment'):
            with self.fresh(resume=resume, runtime={'build': 'changed'}):
                self.fail('A different environment was accepted')
        self.assertEqual(sha256(old / DATABASE), previous)

    def test_batch_size_threads_and_device_arguments_must_match_request(self):
        for field, unexpected in (('batch_size', 2), ('threads', 2), ('device', 'cuda')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'worker arguments differ'):
                with self.fresh(identity={**self.identity, 'parameters': {**self.identity['parameters'], field: unexpected}}):
                    self.fail('The child accepted unexpected arguments')

    def test_changed_input_bytes_and_order_are_rejected(self):
        resume, _old = self.prior_finished()
        data = self.source.read_text(encoding='ascii').splitlines(keepends=True)
        self.source.write_text(''.join(data[2:] + data[:2]), encoding='ascii')
        reordered = ordered_identity(read_records(self.source))
        changed = {**self.identity, 'input_sha256': sha256(self.source), 'records_sha256': identity_sha256(reordered)}
        with self.assertRaisesRegex(ValueError, 'identity mismatch: input_sha256, records_sha256'):
            with self.fresh(resume=resume, identity=changed, observed=reordered):
                self.fail('The reordered input was accepted')

    def test_actual_asset_identity_and_saved_encoder_identity_are_checked(self):
        resume, old = self.prior_finished()
        with self.assertRaisesRegex(ValueError, 'Input or contract changed'):
            with self.fresh(resume=resume, identity={**self.identity, 'model_manifest_sha256': '0' * 64}):
                self.fail('Unverified model identity was accepted')
        with closing(sqlite3.connect(old / DATABASE)) as db, db:
            previous = json.loads(db.execute('SELECT value FROM identity').fetchone()[0])
            previous['encoder']['embedding.py'] = '0' * 64
            db.execute('UPDATE identity SET value = ?', (json.dumps(previous),))
        with self.assertRaisesRegex(ValueError, 'identity mismatch: encoder'):
            with self.fresh(resume=resume):
                self.fail('A different encoder was accepted')

    def test_wrong_attempt_is_rejected(self):
        resume, _old = self.prior_finished()
        with self.assertRaisesRegex(ValueError, 'matching .* run'):
            with self.fresh(resume={**resume, 'attempt_id': str(uuid4())}):
                self.fail('An unrelated attempt was accepted')

    def test_report_and_reuse_reject_bad_shape_dtype_and_nonfinite(self):
        first = self.batches[0]
        with self.fresh() as (_, store):
            for array in (np.zeros((1, 2), dtype=np.float32), values(first).astype(np.float64),
                          np.full((1, FEATURE_WIDTH), np.nan, dtype=np.float32)):
                with self.subTest(array=array.shape, dtype=array.dtype), self.assertRaisesRegex(ValueError, 'Invalid encoding'):
                    store.save(first, array)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 0)

    def test_assembly_rejects_missing_and_duplicate_rows_and_modified_block(self):
        first, second, third = self.batches
        with self.fresh() as (_, store):
            store.save(first, values(first))
            output = self.base / 'failed.npy'
            with self.assertRaisesRegex(ValueError, 'failed final verification'):
                store.assemble((first, second, third), 3, output)
            with self.assertRaisesRegex(ValueError, 'repeated rows'):
                store.assemble((first, first), 3, output)
            (store.folder / (first.id + '.f32le')).write_bytes(b'incomplete')
            with self.assertRaisesRegex(ValueError, 'failed final verification'):
                store.assemble((first,), 3, output)

    def test_reconciliation_rejects_active_encoding_lease_after_launcher_exit(self):
        resume, _old = self.prior_finished()
        directory = self.runs / resume['run_id']
        with run_record._writer_lease(directory, run_record.CHECKPOINT_LEASE):
            with self.assertRaisesRegex(RuntimeError, 'active writer'):
                reconcile_run(resume['run_id'])

    def test_public_resume_rejects_active_and_missing_records(self):
        dest = self.base / 'prediction'
        with run_record.RunRecord('api') as parent:
            with self.assertRaisesRegex(IncFinderError, 'active writer'):
                predict(input=self.source, output=dest, resume_run_id=parent.run_id)
            parent.fail(KeyboardInterrupt())
        with self.assertRaisesRegex(IncFinderError, 'no trustworthy checkpoint database'):
            predict(input=self.source, output=dest, resume_run_id=parent.run_id)
        self.assertFalse(dest.exists())

    def test_public_resume_rejects_external_features_and_conflicting_parent(self):
        dest = self.base / 'prediction'
        for extra, message in (({'features_dir': self.base}, 'external precomputed features'),
                               ({'parent_run_id': str(uuid4())}, 'must match resume_run_id')):
            with self.subTest(extra=extra), self.assertRaisesRegex(IncFinderError, message):
                predict(input=self.source, output=dest, resume_run_id=str(uuid4()), **extra)
        self.assertFalse(dest.exists())

    def test_cli_passes_resume_identifier_and_dedicated_cancel_exit(self):
        unique = str(uuid4())
        args = ['predict', str(self.source), '-o', str(self.base / 'prediction'), '--resume-run-id', unique]
        for legacy in (False, True):
            with self.subTest(legacy=legacy), patch.object(cli, '_predict', side_effect=KeyboardInterrupt) as adapter:
                self.assertEqual(cli.main(args, legacy=legacy), 130)
                self.assertEqual(adapter.call_args.kwargs['resume_run_id'], unique)
        self.assertIn('resume_run_id', inspect.signature(predict).parameters)


class WorkerTreeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.marker = self.base / 'worker-pids.json'

    def assert_reaped(self, pids):
        expires = time.monotonic() + 8
        while time.monotonic() < expires and any(is_running(pid) for pid in pids):
            time.sleep(0.02)
        self.assertFalse(any(is_running(pid) for pid in pids), f'Worker processes survived: {pids}')

    def test_real_interrupt_reaps_worker_and_grandchild(self):
        identifiers = []
        def interrupt_when_started():
            identifiers.extend(wait_ready(self.marker))
            _thread.interrupt_main()
        timer = Thread(target=interrupt_when_started, daemon=True)
        timer.start()
        with self.assertRaises(KeyboardInterrupt):
            tree_launcher(self.marker)
        timer.join(timeout=5)
        self.assertEqual(len(identifiers), 2)
        self.assert_reaped(identifiers)

    def test_forced_launcher_death_reaps_worker_and_grandchild(self):
        script = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                  'from test_checkpoints import tree_launcher; tree_launcher(Path(sys.argv[2]))')
        with subprocess.Popen([sys.executable, '-B', '-c', script, str(Path(__file__).resolve().parent),
                               str(self.marker)]) as launcher:
            try:
                pids = wait_ready(self.marker)
                self.assertTrue(all(is_running(pid) for pid in pids))
            finally:
                launcher.kill()
                launcher.wait(timeout=10)
        self.assert_reaped(pids)


if __name__ == '__main__':
    if sys.argv[1:2] == ['tree']:
        tree_child(Path(sys.argv[2]))
    else:
        unittest.main()
