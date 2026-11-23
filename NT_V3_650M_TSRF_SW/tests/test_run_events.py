"""Test persistence, replay, and real process termination at run boundaries."""
from contextlib import closing, redirect_stderr
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from incfinder import IncFinderError, load_run_events, load_run_record, predict, rebuild_run_state, reconcile_run
from ntv3_tsrf import cli, run_record
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.output_metadata import RESULT_FILES
from ntv3_tsrf.run_events import EventJournal, utc_now


def prepare_result(run, folder):
    output = folder / 'published'
    run.data['parameters'] = {'output': {'value': str(output), 'source': 'explicit'}}
    for stage in ('paths', 'assets', 'staging', 'result_validation'):
        run.enter_stage(stage)
    staging = folder / 'staging'
    staging.mkdir()
    outputs = {}
    for name in (*RESULT_FILES, *(name + '.metadata.json' for name in RESULT_FILES), 'prediction.json'):
        path = staging / name
        path.write_text(name + '\n', encoding='ascii')
        outputs[name] = sha256(path)
    summary = staging / 'run_summary.json'
    write_json(summary, {'run_id': run.run_id, 'execution_status': 'PASS', 'outputs': outputs})
    run.prepare_publication(summary)
    return staging, output


def crash_child(point, folder):
    def wait_for_termination(*args):
        print(f'READY {run.run_id}', flush=True)
        sys.stdin.readline()
        raise AssertionError('The parent must terminate this subprocess')

    with run_record.RunRecord('api') as run:
        if point == 'before_commit':
            save_projection = EventJournal._save_projection

            def hold_transaction(db, sequence, state):
                save_projection(db, sequence, state)
                wait_for_termination()

            with patch.object(EventJournal, '_save_projection', side_effect=hold_transaction):
                run.enter_stage('paths')
        elif point == 'after_commit':
            with patch.object(run_record, '_write_snapshot', side_effect=wait_for_termination):
                run.enter_stage('paths')
        else:
            staging, output = prepare_result(run, folder)
            if point == 'after_publish':
                run_record.publish_output(staging, output)
            wait_for_termination()


class RunEventTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='if007 \u6d4b\u8bd5 '))).resolve()
        self.state = self.root / 'records # test'
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.state)))

    def journal(self, run_id):
        return EventJournal(self.state / run_id, run_id)

    def projection(self, run_id):
        with closing(sqlite3.connect(self.journal(run_id).path)) as db:
            sequence, state = db.execute('SELECT sequence, state FROM projection').fetchone()
        return sequence, json.loads(state)

    def crash(self, point, while_alive=None):
        folder = self.root / str(uuid4())
        folder.mkdir()
        script = ('import sys; from pathlib import Path; sys.path.insert(0, "tests"); '
                  'from test_run_events import crash_child; crash_child(sys.argv[1], Path(sys.argv[2]))')
        command = [sys.executable, '-s', '-B', '-c', script, point, str(folder)]
        with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf-8') as proc:
            try:
                message = proc.stdout.readline().rstrip()
                if not message.startswith('READY '):
                    proc.wait(timeout=10)
                    self.fail(f'The subprocess did not reach {point}: {proc.stderr.read()}')
                run_id = message.split()[1]
                with self.assertRaisesRegex(RuntimeError, 'active writer'):
                    reconcile_run(run_id)
                with self.assertRaisesRegex(RuntimeError, 'active writer'):
                    rebuild_run_state(run_id)
                if while_alive:
                    while_alive(run_id)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        return run_id, folder

    def test_ordered_events_bind_a_single_run_and_attempt(self):
        with run_record.RunRecord('api') as run:
            staging, output = prepare_result(run, self.root)
            run_record.publish_output(staging, output)
            run.succeed()
        events = load_run_events(run.run_id)
        self.assertEqual([e['event_type'] for e in events],
                         ['run_created', 'stage_entered', 'stage_entered', 'stage_entered',
                          'stage_entered', 'publication_prepared', 'succeeded'])
        self.assertEqual([e['sequence'] for e in events], list(range(1, 8)))
        self.assertTrue(all(e['event_version'] == 1 and e['run_id'] == run.run_id for e in events))
        self.assertEqual({e['attempt_id'] for e in events}, {run.attempt_id})
        self.assertEqual(len({e['event_key'] for e in events}), len(events))
        self.assertTrue(all(datetime.fromisoformat(e['recorded_utc']).utcoffset().total_seconds() == 0 for e in events))
        self.assertEqual(events[-2]['payload']['summary_sha256'], sha256(output / 'run_summary.json'))
        state = load_run_record(run.run_id)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(self.projection(run.run_id), (len(events), state))
        self.assertEqual(read_json(run.path), state)

    def test_idempotent_resubmission_returns_the_original_event(self):
        with run_record.RunRecord('api') as run:
            run.enter_stage('paths')
            journal = self.journal(run.run_id)
            event = load_run_events(run.run_id)[-1]
            replayed, state = journal.append(run.attempt_id, event['event_type'], event['event_key'], event['payload'])
            self.assertEqual(replayed, event)
            self.assertEqual(state['stage'], 'paths')
            self.assertEqual(len(load_run_events(run.run_id)), 2)
            conflict = {**event['payload'], 'details': {}}
            with self.assertRaisesRegex(ValueError, 'Conflicting payload'):
                journal.append(run.attempt_id, event['event_type'], event['event_key'], conflict)
            self.assertEqual(load_run_events(run.run_id)[-1], event)

    def test_repeated_completion_appends_only_one_terminal_event(self):
        with run_record.RunRecord('api') as run:
            staging, output = prepare_result(run, self.root)
            run_record.publish_output(staging, output)
            run.succeed()
            before = load_run_events(run.run_id)
            run.succeed()
            self.assertEqual(load_run_events(run.run_id), before)
            self.assertEqual(before[-1]['event_key'], 'run.terminal')

    def test_invalid_transitions_and_payloads_leave_events_unchanged(self):
        with run_record.RunRecord('api') as run:
            with self.assertRaisesRegex(ValueError, 'preparation'):
                run.succeed()
            with self.assertRaisesRegex(ValueError, 'preflight'):
                run.enter_stage('classification')
            with self.assertRaisesRegex(ValueError, 'payload'):
                self.journal(run.run_id).append(run.attempt_id, 'stage_entered', 'bad-key',
                                                {'stage': 'paths', 'details': {'argv': ['private']}})
            run.fail(ValueError('preflight failed'))
            before = load_run_events(run.run_id)
            with self.assertRaisesRegex(ValueError, 'transition'):
                run.enter_stage('input')
            with self.assertRaisesRegex(ValueError, 'Conflicting payload'):
                run.succeed()
            self.assertEqual(load_run_events(run.run_id), before)

    def test_committed_events_reject_update_and_delete(self):
        with run_record.RunRecord('api') as run:
            run.fail(ValueError('before execution'))
        with closing(sqlite3.connect(self.journal(run.run_id).path)) as db:
            for statement in ("UPDATE events SET event_type='succeeded'", "DELETE FROM events"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                    db.execute(statement)
        self.assertEqual([e['event_type'] for e in load_run_events(run.run_id)], ['run_created', 'preflight_failed'])

    def test_projection_write_failure_rolls_back_the_insert(self):
        with run_record.RunRecord('api') as run:
            before = load_run_events(run.run_id)
            projection = self.projection(run.run_id)
            with patch.object(EventJournal, '_save_projection', side_effect=sqlite3.OperationalError('test rollback')):
                with self.assertRaisesRegex(RuntimeError, 'event database'):
                    run.enter_stage('paths')
            self.assertEqual(load_run_events(run.run_id), before)
            self.assertEqual(self.projection(run.run_id), projection)
            run.enter_stage('paths')
            self.assertEqual(len(load_run_events(run.run_id)), 2)

    def test_each_terminal_state_rebuilds_after_projection_removal(self):
        for terminal in ('succeeded', 'preflight_failed', 'execution_failed'):
            with self.subTest(terminal=terminal), run_record.RunRecord('api') as run:
                if terminal == 'succeeded':
                    folder = self.root / 'success'
                    folder.mkdir()
                    staging, output = prepare_result(run, folder)
                    run_record.publish_output(staging, output)
                    run.succeed()
                else:
                    if terminal == 'execution_failed':
                        for stage in ('paths', 'assets', 'staging', 'classification'):
                            run.enter_stage(stage)
                    run.fail(RuntimeError('test failure'))
            snapshot = read_json(run.path)
            self.assertEqual(snapshot['status'], terminal)
            before = load_run_events(run.run_id)
            with closing(sqlite3.connect(self.journal(run.run_id).path)) as db, db:
                db.execute('DROP TABLE projection')
            run.path.unlink()
            self.assertEqual(load_run_record(run.run_id), snapshot)
            self.assertFalse(run.path.exists())
            self.assertEqual(rebuild_run_state(run.run_id), snapshot)
            self.assertEqual(self.projection(run.run_id), (len(before), snapshot))
            self.assertEqual(read_json(run.path), snapshot)
            self.assertEqual(load_run_events(run.run_id), before)

    def test_derived_cache_cannot_override_the_event_history(self):
        with run_record.RunRecord('api') as run:
            run.fail(ValueError('rejected'))
        before = load_run_record(run.run_id)
        fake = {**before, 'status': 'succeeded'}
        write_json(run.path, fake)
        with closing(sqlite3.connect(self.journal(run.run_id).path)) as db, db:
            db.execute('UPDATE projection SET state=?', (json.dumps(fake),))
        self.assertEqual(load_run_record(run.run_id), before)
        self.assertEqual(rebuild_run_state(run.run_id), before)
        self.assertEqual(read_json(run.path), before)

    def test_historical_if006_record_is_read_without_migration(self):
        run_id = str(uuid4())
        directory = self.state / run_id
        directory.mkdir(parents=True)
        record = {'record_schema_version': 1, 'run_id': run_id, 'entry_point': 'api',
                  'started_utc': utc_now(), 'completed_utc': utc_now(), 'parameters': {},
                  'status': 'succeeded', 'stage': 'complete'}
        record_path = directory / 'run.json'
        write_json(record_path, record)
        self.assertEqual(load_run_record(run_id), record)
        for operation in (load_run_events, rebuild_run_state, reconcile_run):
            with self.assertRaises(FileNotFoundError):
                operation(run_id)
        self.assertEqual(list(directory.iterdir()), [record_path])

    def test_missing_event_database_does_not_make_a_new_snapshot_authoritative(self):
        with run_record.RunRecord('api') as run:
            run.fail(ValueError('rejected'))
        self.journal(run.run_id).path.unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'event history'):
            load_run_record(run.run_id)

    def test_known_sensitive_values_do_not_enter_the_database(self):
        credential = 'ghp_if007_testing_0123456789'
        with self.assertRaises(IncFinderError) as caught:
            predict(input=self.root / f'password={credential}.fa', output=self.root / 'new')
        run_id = caught.exception.run_id
        events = load_run_events(run_id)
        self.assertEqual(events[-1]['event_type'], 'preflight_failed')
        self.assertEqual(events[-1]['payload']['reason'], 'file_not_found')
        self.assertEqual(load_run_record(run_id)['parameters']['input']['value'], '[redacted]')
        self.assertNotIn(credential, json.dumps(events))
        self.assertNotIn(credential.encode(), self.journal(run_id).path.read_bytes())

    def test_cli_parse_failure_only_records_defined_event_fields(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['predict', '--bad-option', 'token=testing123'])
        self.assertEqual(caught.exception.code, 2)
        run_id = next(self.state.iterdir()).name
        events = load_run_events(run_id)
        self.assertEqual([e['event_type'] for e in events], ['run_created', 'preflight_failed'])
        self.assertEqual(events[-1]['payload']['reason'], 'argument_parsing_failed')
        self.assertNotIn('testing123', json.dumps(events))

    def test_termination_before_commit_rolls_back_and_reconciles(self):
        run_id, _ = self.crash('before_commit')
        state = reconcile_run(run_id)
        events = load_run_events(run_id)
        self.assertEqual([e['event_type'] for e in events], ['run_created', 'interrupted'])
        self.assertEqual(state['error'], {'stage': 'arguments', 'reason': 'interrupted'})
        self.assertEqual(reconcile_run(run_id), state)
        self.assertEqual(load_run_events(run_id), events)

    def test_termination_after_commit_uses_events_ahead_of_run_json(self):
        def verify_stale_json(run_id):
            self.assertEqual(read_json(self.state / run_id / 'run.json')['stage'], 'arguments')
            self.assertEqual(load_run_record(run_id)['stage'], 'paths')
        run_id, _ = self.crash('after_commit', verify_stale_json)
        state = reconcile_run(run_id)
        self.assertEqual([e['event_type'] for e in load_run_events(run_id)],
                         ['run_created', 'stage_entered', 'interrupted'])
        self.assertEqual(state['error'], {'stage': 'paths', 'reason': 'interrupted'})

    def test_termination_before_publication_preserves_staging_and_records_interruption(self):
        run_id, folder = self.crash('before_publish')
        self.assertTrue((folder / 'staging').is_dir())
        self.assertFalse((folder / 'published').exists())
        state = reconcile_run(run_id)
        self.assertEqual(state['status'], 'interrupted')
        self.assertEqual(state['stage'], 'publication')
        self.assertTrue((folder / 'staging' / 'run_summary.json').is_file())

    def test_termination_after_publication_recovers_actual_success_once(self):
        run_id, folder = self.crash('after_publish')
        output = folder / 'published'
        before = {p.name: sha256(p) for p in output.iterdir()}
        state = reconcile_run(run_id)
        events = load_run_events(run_id)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(events[-1]['event_type'], 'publication_recovered')
        self.assertEqual(reconcile_run(run_id), state)
        self.assertEqual(load_run_events(run_id), events)
        self.assertEqual({p.name: sha256(p) for p in output.iterdir()}, before)

    def test_corrupt_publication_is_not_recovered_as_success(self):
        for filename in ('run_summary.json', RESULT_FILES[0]):
            with self.subTest(filename=filename):
                run_id, folder = self.crash('after_publish')
                target = folder / 'published' / filename
                target.write_bytes(b'corrupted')
                state = reconcile_run(run_id)
                self.assertEqual(state['status'], 'interrupted')
                self.assertEqual(load_run_events(run_id)[-1]['event_type'], 'interrupted')
                self.assertEqual(target.read_bytes(), b'corrupted')
