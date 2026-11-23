"""Use isolated, sealed Runs to exercise sharing and artifact retention."""
import csv
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_lineage as fixtures
from incfinder import (export_artifact_archive, export_lineage, inspect_lineage, load_run_events, load_run_record,
                       rebuild_run_state)
from ntv3_tsrf import __version__, cli, sharing
from ntv3_tsrf.artifact_manifest import MANIFEST, SEAL, verify_artifacts
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.report import rebuild_report
from ntv3_tsrf.retention import apply_retention, plan_retention
from ntv3_tsrf.run_events import DATABASE_NAME
from ntv3_tsrf.run_record import RunRecord, _writer_lease


PRIVATE = (r'C:\Users\private\donor-A', r'\\private-host\restricted\chart', '/home/private/subject-data',
           'ghp_example_redaction_sentinel_000000', '<script>header', 'A' * 80, 'private-name-825')


class ShareRetentionTests(unittest.TestCase):
    def setUp(self):
        fixtures.LineageTests.setUp(self)
        self.source = self.base / 'private-name-825.fasta'
        self.private = False

    def seal(self):
        self.summary['version'] = __version__
        self.summary['prediction']['environment'] = {'python': '3.12.15'}
        write_json(self.folder / 'prediction.json', self.summary['prediction'])
        if self.private:
            self.summary['model_location'] = {'private': list(PRIVATE[:4])}
            (self.folder / 'logs/rf.log').write_text('\n'.join(PRIVATE[:4]), encoding='utf-8')
            (self.folder / 'private-name-825.txt').write_text(PRIVATE[3], encoding='utf-8')
        fixtures.LineageTests.seal(self)

    def make(self, *, internal=False, journal=True):
        if not journal:
            return fixtures.LineageTests.make_run(self, internal=internal)
        with RunRecord('api') as run:
            run.data['parameters']['output'] = {'value': str(self.folder)}
            fixtures.LineageTests.make_run(self, internal=internal, journal=run)
            run.succeed()
            return run.path.parent

    def apply(self, policy, plan=None):
        plan = plan if plan is not None else plan_retention(self.folder, policy)
        return apply_retention(self.folder, policy, expected_plan_sha256=plan['plan_sha256'])

    def share(self, name='shared', **options):
        return sharing.export_share(self.folder, self.base / name, **options)

    def test_AC1_allowlist_every_share_member(self):
        self.private = True
        self.make()
        summary_before = sha256(self.folder / 'run_summary.json')
        manifest_before = sha256(self.folder / MANIFEST)
        private_ids = [self.run_id, *(e['sha256'] for e in read_json(self.folder / MANIFEST)['artifacts']
                                      if e['role'] in ('input', 'features', 'feature_metadata'))]
        result = self.share(replay_from='report', html=True, result_fields=('gate_pass', 'internal_status'))
        shared = self.base / 'shared'
        self.assertEqual({p.name for p in shared.iterdir()},
                         {'share_manifest.json', 'lineage.json', 'index.html', 'result-fields.csv'})
        for path in shared.iterdir():
            content = path.read_text(encoding='utf-8')
            for sentinel in (*PRIVATE, *private_ids):
                self.assertNotIn(sentinel, path.name + content)
                self.assertNotIn(json.dumps(sentinel)[1:-1], content)
        self.assertEqual(read_json(shared / 'share_manifest.json'), result)
        for name, digest in result['files'].items():
            self.assertEqual(sha256(shared / name), digest)
        with (shared / 'result-fields.csv').open(encoding='ascii', newline='') as stream:
            self.assertEqual(list(csv.reader(stream)), [
                ['gate_pass', 'internal_status'], ['0', 'Unclassified'], ['1', 'Inc_no_call'], ['1', 'Inc_called'],
            ])
        self.assertEqual(sha256(self.folder / 'run_summary.json'), summary_before)
        self.assertEqual(sha256(self.folder / MANIFEST), manifest_before)

    def test_AC1_default_is_file_graph_and_manifest_only(self):
        self.private = True
        self.make()
        self.share()
        self.assertEqual({p.name for p in (self.base / 'shared').iterdir()}, {'share_manifest.json', 'lineage.json'})
        self.assertNotIn('records', read_json(self.base / 'shared/lineage.json'))

    def test_AC1_arbitrary_columns_and_typed_injection_fail_without_publication(self):
        self.make(journal=False)
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.share(replay_from='report', result_fields=('Header',))
        table = self.folder / 'inc_results.csv'
        with table.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        rows[1][rows[0].index('internal_status')] = PRIVATE[3]
        with table.open('w', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows(rows)
        fixtures.LineageTests.relabel_digest(self, table.name)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'declared domain'):
            self.share(replay_from='report', result_fields=('internal_status',))
        self.assertFalse((self.base / 'shared').exists())

    def test_AC1_share_refuses_tampering_and_source_destination(self):
        self.make()
        with self.assertRaisesRegex(ValueError, 'outside the sealed source'):
            sharing.export_share(self.folder, self.folder / 'shared')
        with (self.folder / 'logs/rf.log').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.share()
        self.assertFalse((self.base / 'shared').exists())

    def test_AC1_detached_archive_uses_explicit_bindings_instead_of_embedded_paths(self):
        self.make()
        archive = self.base / 'reference-archive'
        export_artifact_archive(self.folder, archive, external_mode='reference')
        result = sharing.export_share(archive, self.base / 'shared', replay_from='features')
        verified_references = [item for item in result['reproducibility']['requirements']
                               if item['role'] in ('input', 'features', 'feature_metadata')]
        self.assertTrue(verified_references)
        self.assertTrue(all(item['state'] == 'external_required' for item in verified_references))

    def test_AC1_archive_share_can_be_moved_and_read_offline(self):
        self.make(journal=False)
        copy = self.base / 'archive'
        export_artifact_archive(self.folder, copy, external_mode='copy')
        moved = self.base / 'moved'
        copy.rename(moved)
        output = self.base / 'share'
        share = sharing.export_share(moved, output, replay_from='report', html=True)
        self.assertEqual(share['reproducibility']['status'], 'complete')
        self.assertEqual(share['source_kind'], 'archive')
        self.assertFalse(any('source.fasta' in p.name for p in output.iterdir()))

    def test_AC2_policy_scopes_and_full_is_noop(self):
        self.make(internal=True)
        names = {policy: [row['path'] for row in plan_retention(self.folder, policy)['targets']]
                 for policy in ('minimal', 'standard', 'full')}
        self.assertEqual(set(names['standard']), {'logs/embedding.log', 'logs/rf.log', 'logs/report.log'})
        self.assertEqual(set(names['minimal']) - set(names['standard']), {'features/features.npy', 'features/features.json'})
        self.assertEqual(names['full'], [])
        before = load_run_events(self.run_id)
        self.assertEqual(self.apply('full')['unavailable'], [])
        self.assertEqual(load_run_events(self.run_id), before)

    def test_AC2_standard_preserves_reports_record_lineage_and_identity(self):
        self.make(internal=True)
        original = sha256(self.folder / MANIFEST)
        probability = sha256(self.folder / 'probabilities.npz')
        self.apply('standard')
        self.assertEqual(sha256(self.folder / MANIFEST), original)
        self.assertEqual(sha256(self.folder / 'probabilities.npz'), probability)
        self.assertTrue((self.folder / 'features/features.npy').is_file())
        self.assertEqual(inspect_lineage(self.folder, row_index=0)['row_index'], 0)
        exported = export_lineage(self.folder, self.base / 'private-lineage', start=0, stop=1)
        self.assertEqual(sum(e.get('availability') == 'unavailable' for e in exported['entities']), 3)
        self.assertIn('unavailable', (self.base / 'private-lineage/index.html').read_text(encoding='utf-8'))
        report = rebuild_report(self.folder, self.base / 'derived')
        self.assertTrue(report['source']['publication_event_bound'])
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            verify_artifacts(self.folder)

    def test_AC2_minimal_replays_availability_without_altering_original_digests_and_edges(self):
        journal = self.make(internal=True)
        plan = plan_retention(self.folder, 'minimal')
        recorded = {item['artifact_id']: item for item in plan['targets']}
        original = (self.folder / MANIFEST).read_bytes(), (self.folder / SEAL).read_bytes()
        old_time = load_run_record(self.run_id)['completed_utc']
        answer = self.apply('minimal', plan)
        after_events = load_run_events(self.run_id)
        self.assertEqual(set(answer['unavailable']), set(recorded))
        self.assertEqual(self.apply('minimal', plan), answer)
        self.assertEqual(load_run_events(self.run_id), after_events)
        self.assertEqual(((self.folder / MANIFEST).read_bytes(), (self.folder / SEAL).read_bytes()), original)
        self.assertTrue(self.source.is_file())
        state = load_run_record(self.run_id)
        self.assertEqual(state['completed_utc'], old_time)
        self.assertEqual(state['status'], 'succeeded')
        with closing(sqlite3.connect(journal / DATABASE_NAME)) as db, db:
            db.execute('DELETE FROM projection')
        self.assertEqual(rebuild_run_state(self.run_id), state)
        graph = inspect_lineage(self.folder)
        statuses = {item['id']: item for item in graph['entities']}
        for key, item in recorded.items():
            self.assertFalse((self.folder / item['path']).exists())
            self.assertEqual(statuses[key]['availability'], 'unavailable')
            self.assertEqual(statuses[key]['sha256'], item['sha256'])
            self.assertEqual(state['artifact_retention'][key]['parents'], item['parents'])
            for parent in item['parents']:
                self.assertIn({'type': 'wasDerivedFrom', 'generated_entity': key, 'used_entity': parent}, graph['relations'])
        with self.assertRaisesRegex(ValueError, 'original verified dependencies'):
            inspect_lineage(self.folder, row_index=0)
        self.share(replay_from='report', html=True)
        shared = read_json(self.base / 'shared/lineage.json')
        self.assertEqual(sum(a.get('reason') == 'retention' for a in shared['artifacts']), len(recorded))
        self.assertIn('unavailable', (self.base / 'shared/index.html').read_text(encoding='utf-8'))

    def interrupt(self, stage):
        plan = plan_retention(self.folder, 'standard')
        runner = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from ntv3_tsrf.retention import apply_retention
from ntv3_tsrf.run_events import EventJournal
before_unlink = sys.argv[3] == "before_unlink"
append = EventJournal.append
unlink = Path.unlink
def append_event(self, attempt, kind, key, payload):
    if not before_unlink and kind == "artifact_unavailable":
        os._exit(76)
    return append(self, attempt, kind, key, payload)
def delete(self, *args, **kwargs):
    if before_unlink and self.suffix == ".log":
        os._exit(76)
    return unlink(self, *args, **kwargs)
with patch.object(EventJournal, "append", append_event), patch.object(Path, "unlink", delete):
    apply_retention(sys.argv[1], "standard", expected_plan_sha256=sys.argv[2])
'''
        result = subprocess.run([sys.executable, '-B', '-c', runner, str(self.folder), plan['plan_sha256'], stage],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 76, result.stderr.decode(errors='replace'))
        first = plan['targets'][0]
        old = load_run_record(self.run_id)['artifact_retention'][first['artifact_id']]
        self.assertEqual(old['status'], 'pending')
        self.assertEqual((self.folder / first['path']).is_file(), stage == 'before_unlink')
        self.apply('standard', plan)
        final = load_run_record(self.run_id)['artifact_retention'][first['artifact_id']]
        self.assertEqual(final['status'], 'unavailable')
        self.assertEqual(final['reason'], 'removed' if stage == 'before_unlink' else 'missing_after_intent')
        self.assertEqual(final['intent_utc'], old['intent_utc'])

    def test_AC2_real_process_stops_after_intent_before_unlink(self):
        self.make()
        self.interrupt('before_unlink')

    def test_AC2_real_process_stops_after_unlink_before_result(self):
        self.make()
        self.interrupt('after_unlink')

    def test_AC2_active_writer_and_unapproved_digest_are_rejected(self):
        directory = self.make()
        plan = plan_retention(self.folder, 'standard')
        with _writer_lease(directory):
            with self.assertRaisesRegex(RuntimeError, 'active writer'):
                self.apply('standard', plan)
        with self.assertRaisesRegex(ValueError, 'approval'):
            apply_retention(self.folder, 'standard', expected_plan_sha256='0' * 64)
        self.assertTrue((self.folder / 'logs/rf.log').is_file())

    def test_AC2_unreported_move_and_changed_bytes_stop_before_any_deletion(self):
        self.make()
        plan = plan_retention(self.folder, 'standard')
        target, other = (self.folder / item['path'] for item in plan['targets'])
        moved = self.base / 'moved.log'
        target.replace(moved)
        with self.assertRaisesRegex(ValueError, 'missing without a retention event'):
            self.apply('standard', plan)
        self.assertTrue(other.exists() and moved.exists())
        moved.replace(target)
        target.write_bytes(b'altered\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.apply('standard', plan)
        self.assertTrue(other.exists())
        self.assertNotIn('artifact_retention', load_run_record(self.run_id))

    def test_AC2_read_only_and_owned_run_binding_are_required(self):
        self.make()
        unmapped = self.base / 'different-run-path'
        shutil.copytree(self.folder, unmapped)
        with self.assertRaisesRegex(ValueError, 'original published Run'):
            plan_retention(unmapped, 'standard')
        target = self.folder / 'logs/rf.log'
        modes = target.stat().st_mode
        try:
            target.chmod(stat.S_IREAD)
            with self.assertRaisesRegex(ValueError, 'read-only'):
                self.apply('standard')
            self.assertTrue((self.folder / 'logs/report.log').exists())
        finally:
            target.chmod(modes)

    def test_AC2_linked_targets_protect_bytes_outside_the_run(self):
        self.make()
        target = self.folder / 'logs/rf.log'
        outside = self.base / 'outside.log'
        target.replace(outside)
        os.link(outside, target)
        with self.assertRaisesRegex(ValueError, 'unlinked'):
            self.apply('minimal')
        self.assertEqual(outside.read_bytes(), target.read_bytes())
        self.assertNotIn('artifact_retention', load_run_record(self.run_id))

    def test_AC2_rebound_retired_path_is_reported_as_conflict(self):
        self.make()
        target = self.folder / 'logs/rf.log'
        contents = target.read_bytes()
        self.apply('standard')
        target.write_bytes(contents)
        with self.assertRaisesRegex(ValueError, 'retired artifact path'):
            self.apply('standard')
        self.assertEqual(target.read_bytes(), contents)

    def ready_model_binding(self):
        model_root = self.base / 'model-binding'
        cfg = model_root / 'config'
        cfg.mkdir(parents=True)
        names = ('config/thresholds.tsv', 'models/rf/stage1.joblib', 'models/rf/stage2.joblib')
        files = []
        for name in names:
            path = model_root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text(name, encoding='ascii')
            files.append({'path': name, 'size_bytes': path.stat().st_size, 'sha256': sha256(path)})
        for name in ('contract.json', 'runtime.json'):
            shutil.copyfile(ROOT / 'config' / name, cfg / name)
        write_json(cfg / 'assets.json', {'files': files})
        self.summary['assets']['manifest_sha256'] = sha256(cfg / 'assets.json')
        self.seal()
        self.enterContext(patch.object(sharing, 'ROOT', model_root))
        self.enterContext(patch.object(sharing, 'asset_path', side_effect=lambda name, _: model_root / name))
        return model_root

    def test_AC3_external_rebinding_verifies_original_bytes_and_changes_classification(self):
        self.make(journal=False)
        model_root = self.ready_model_binding()
        old = self.meta
        new = self.base / 'relocated-features'
        old.rename(new)
        def status(result):
            return result['reproducibility']['status']
        self.assertEqual(status(self.share(model_dir=model_root)), 'external_required')
        locations = {'features': new / 'features.npy', 'feature_metadata': new / 'features.json', 'input': self.source}
        references = {entry['artifact_id']: locations[entry['role']]
                      for entry in read_json(self.folder / MANIFEST)['artifacts'] if entry['role'] in locations}
        bad = self.base / 'same-size-mismatch'
        contents = (new / 'features.npy').read_bytes()
        bad.write_bytes(b'\x00' + contents[1:])
        wrong = {item:bad if path.name == 'features.npy' else path for item,path in references.items()}
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.share('bad', reference_paths=wrong, model_dir=model_root)
        self.assertFalse((self.base / 'bad').exists())
        self.assertEqual(status(self.share('rebound', reference_paths=references, model_dir=model_root)), 'complete')
        self.assertNotIn(str(new), (self.base / 'rebound/share_manifest.json').read_text(encoding='utf-8'))

    def test_AC3_required_local_asset_removed_is_audit_only(self):
        self.make(internal=True)
        self.apply('minimal')
        replay = self.share()['reproducibility']
        self.assertEqual((replay['status'], replay['reason']), ('audit_only', 'missing_required_artifact'))
        report = self.share('report', replay_from='report')['reproducibility']
        self.assertEqual(report['status'], 'complete')

    def test_AC3_absent_original_metadata_and_unknown_encoding_context_only_audit(self):
        self.make(journal=False)
        result = self.share(replay_from='sequence')['reproducibility']
        self.assertEqual((result['status'], result['reason']), ('audit_only', 'missing_equivalent_encoding_context'))
        (self.folder / 'run_summary.json').unlink()
        result = self.share('missing', replay_from='report')['reproducibility']
        self.assertEqual((result['status'], result['reason']), ('audit_only', 'incomplete_software_or_contract_identity'))

    def test_AC3_formal_result_cli_and_explicit_retention_cli(self):
        self.make()
        output = self.base / 'public'
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main(['export-share', str(self.folder), '--output', str(output),
                                      '--replay-from', 'report', '--html']), 0)
        self.assertEqual(json.loads(stdout.getvalue())['reproducibility']['status'], 'complete')
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main(['retention', 'plan', str(self.folder), '--policy', 'standard']), 0)
        plan = json.loads(stdout.getvalue())
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main(['retention', 'apply', str(self.folder), '--policy', 'standard',
                                      '--plan-sha256', plan['plan_sha256']]), 0)
        self.assertEqual(len(json.loads(stdout.getvalue())['unavailable']), 2)
