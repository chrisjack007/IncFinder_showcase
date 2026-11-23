"""Check artifact lineage, archive integrity, and retained publication."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from incfinder import export_artifact_archive, load_run_events, reconcile_run, verify_artifacts
from ntv3_tsrf import run_record
from ntv3_tsrf.artifact_manifest import MANIFEST, SEAL, write_run_artifacts
from ntv3_tsrf.common import sha256, write_json
from ntv3_tsrf.output_metadata import RESULT_FILES


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


class ArtifactArchiveTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix='if008 \u6d4b\u8bd5 '))).resolve()
        self.output = self.base / 'result'
        self.input = self.base / 'input.txt'
        self.features = self.base / 'shared features'
        self.run_id = str(uuid4())

    def make_result(self, *, internal=False):
        put(self.input, b'input\n')
        features = self.output / 'features' if internal else self.features
        put(features / 'features.json', b'{}')
        put(features / 'features.npy', b'array')
        for name in ('logs/rf.log', 'logs/report.log', 'logs/worker/details.log', 'nested/item.bin',
                     'report_environment.json', 'prediction.json',
                     *RESULT_FILES, *(name + '.metadata.json' for name in RESULT_FILES)):
            put(self.output / name, name.encode('ascii'))
        if internal:
            put(self.output / 'logs/embedding.log', b'embedding log')
        summary = {
            'run_id': self.run_id, 'execution_status': 'PASS',
            'input': {'sha256': sha256(self.input)},
            'features_sha256': sha256(features / 'features.npy'),
            'assets': {'manifest_sha256': 'a' * 64},
            'outputs': {p.name: sha256(p) for p in self.output.iterdir() if p.is_file()},
        }
        write_json(self.output / 'run_summary.json', summary)
        return write_run_artifacts(self.output, summary, self.input, None if internal else features,
                                   None if internal else sha256(features / 'features.json'))

    def reseal(self, folder, manifest):
        write_json(folder / MANIFEST, manifest)
        (folder / SEAL).write_bytes(f'{sha256(folder / MANIFEST)}  {MANIFEST}\n'.encode('ascii'))

    def archive(self, mode='copy'):
        self.make_result()
        target = self.base / 'archive'
        return target, export_artifact_archive(self.output, target, external_mode=mode)

    def test_recursive_coverage_sizes_and_direct_parent_ids(self):
        expected_digest = self.make_result()
        manifest = verify_artifacts(self.output, expected_manifest_sha256=expected_digest)
        self.assertEqual((manifest['kind'], manifest['run_id'], manifest['model_manifest_sha256']),
                         ('run', self.run_id, 'a' * 64))
        by_path = {e['path']: e for e in manifest['artifacts']}
        expected = {p.relative_to(self.output).as_posix() for p in self.output.rglob('*') if p.is_file()}
        expected -= {MANIFEST, SEAL}
        self.assertEqual({e['path'] for e in by_path.values() if e['storage'] == 'retained'}, expected)
        for name in expected:
            path = self.output / name
            self.assertEqual((by_path[name]['size_bytes'], by_path[name]['sha256']),
                             (path.stat().st_size, sha256(path)))
        def ids(*names):
            return [by_path[name]['artifact_id'] for name in names]
        metadata = str(self.features / 'features.json')
        features = str(self.features / 'features.npy')
        self.assertEqual(by_path[features]['parents'], ids(metadata))
        self.assertEqual(by_path['logs/rf.log']['parents'], ids(str(self.input), metadata, features))
        self.assertEqual(by_path['logs/worker/details.log']['role'], 'log')
        self.assertEqual(by_path['nested/item.bin']['role'], 'intermediate')
        self.assertEqual(by_path['probabilities.npz.metadata.json']['parents'],
                         ids('probabilities.npz', 'prediction.json'))
        self.assertEqual(by_path['inc_predictions.xlsx']['stage'], 'report')

    def test_internal_features_have_relative_paths_and_input_lineage(self):
        self.make_result(internal=True)
        manifest = verify_artifacts(self.output)
        entries = {e['role']: e for e in manifest['artifacts'] if e['role'] in ('input', 'features', 'feature_metadata')}
        array, metadata = entries['features'], entries['feature_metadata']
        input_id = entries['input']['artifact_id']
        self.assertEqual((array['path'], metadata['path']), ('features/features.npy', 'features/features.json'))
        self.assertEqual((array['storage'], metadata['storage']), ('retained', 'retained'))
        self.assertEqual(array['parents'], [input_id])
        self.assertEqual(metadata['parents'], [input_id, array['artifact_id']])
        copy = export_artifact_archive(self.output, self.base / 'archive', external_mode='copy')
        self.assertEqual(sum(e['storage'] == 'copied' for e in copy['artifacts']), 1)

    def test_reference_archive_requires_original_external_paths(self):
        target, expected = self.archive('reference')
        self.assertEqual(expected, verify_artifacts(target))
        refs = [e for e in expected['artifacts'] if e['storage'] == 'reference']
        self.assertEqual({e['path'] for e in refs}, {str(self.input), str(self.features / 'features.npy'),
                                                   str(self.features / 'features.json')})
        self.assertFalse((target / 'dependencies').exists())
        self.features.rename(self.base / 'moved features')
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            verify_artifacts(target)

    def test_equal_size_external_change_and_failed_export(self):
        self.make_result()
        (self.features / 'features.npy').write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, r'SHA-256 mismatch: .*features.npy'):
            verify_artifacts(self.output)
        target = self.base / 'archive'
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            export_artifact_archive(self.output, target, external_mode='copy')
        self.assertFalse(target.exists())

    def test_missing_external_metadata_rejected(self):
        self.make_result()
        (self.features / 'features.json').unlink()
        with self.assertRaisesRegex(ValueError, r'Missing artifact: .*features.json'):
            verify_artifacts(self.output)

    def test_copy_relocates_and_validates_without_external_files(self):
        target, expected = self.archive()
        moved = target.rename(self.base / 'moved archive')
        self.features.rename(self.base / 'moved features')
        self.input.unlink()
        self.assertEqual(expected, verify_artifacts(moved))
        copied = {e['role']: e for e in expected['artifacts'] if e['storage'] == 'copied'}
        self.assertEqual(set(copied), {'input', 'features', 'feature_metadata'})
        self.assertEqual((moved / copied['input']['path']).read_bytes(), b'input\n')
        self.assertEqual((moved / copied['features']['path']).read_bytes(), b'array')
        self.assertEqual((moved / copied['feature_metadata']['path']).read_bytes(), b'{}')
        self.assertTrue(all(not Path(e['path']).is_absolute() for e in expected['artifacts']))

    def test_redacted_reference_requires_opt_in_path_only_for_that_id(self):
        self.input = self.base / 'token=synthetic' / 'input.txt'
        self.make_result()
        raw = (self.output / MANIFEST).read_text(encoding='ascii')
        self.assertNotIn('token=synthetic', raw)
        manifest = json.loads(raw)
        input_entry = next(e for e in manifest['artifacts'] if e['role'] == 'input')
        feature_entry = next(e for e in manifest['artifacts'] if e['role'] == 'features')
        self.assertIsNone(input_entry['path'])
        with self.assertRaisesRegex(ValueError, 'Redacted reference'):
            verify_artifacts(self.output)
        overrides = {input_entry['artifact_id']: self.input}
        self.assertEqual(verify_artifacts(self.output, reference_paths=overrides), manifest)
        with self.assertRaisesRegex(ValueError, 'only for redacted references'):
            verify_artifacts(self.output, reference_paths={feature_entry['artifact_id']: self.features / 'features.npy'})
        target = self.base / 'archive'
        export_artifact_archive(self.output, target, external_mode='copy', reference_paths=overrides)
        self.assertEqual(verify_artifacts(target)['kind'], 'archive')

    def test_repeated_exports_bind_source_and_exclude_seals_and_live_database(self):
        expected_source = self.make_result()
        put(self.output / 'run.sqlite3', b'live')
        put(self.output / 'run.sqlite3-wal', b'journal')
        first, second = (self.base / label for label in ('first', 'second'))
        manifest = export_artifact_archive(self.output, first)
        self.assertEqual(manifest, export_artifact_archive(self.output, second))
        self.assertEqual(manifest['source_manifest_sha256'], expected_source)
        self.assertEqual((first / MANIFEST).read_bytes(), (second / MANIFEST).read_bytes())
        self.assertEqual((first / SEAL).read_bytes(), (second / SEAL).read_bytes())
        self.assertEqual((first / SEAL).read_bytes(), f'{sha256(first / MANIFEST)}  {MANIFEST}\n'.encode('ascii'))
        self.assertTrue(all(e['path'] not in (MANIFEST, SEAL, 'run.sqlite3', 'run.sqlite3-wal')
                            for e in manifest['artifacts']))
        self.assertEqual((self.output / 'run.sqlite3').read_bytes(), b'live')
        self.assertEqual(sha256(self.output / MANIFEST), expected_source)
        self.assertFalse((first / 'run.sqlite3').exists())

    def test_unlisted_file_including_database_rejected(self):
        archive, _ = self.archive()
        for name in ('added.log', 'run.sqlite3'):
            with self.subTest(name=name):
                put(archive / name, b'unlisted')
                with self.assertRaisesRegex(ValueError, 'Unexpected artifact'):
                    verify_artifacts(archive)
                (archive / name).unlink()

    def test_missing_retained_member_rejected_and_original_remains(self):
        archive, _ = self.archive()
        (archive / 'logs/rf.log').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            verify_artifacts(archive)
        self.assertEqual((self.output / 'logs/rf.log').read_bytes(), b'logs/rf.log')

    def test_tampered_copied_member_rejected(self):
        archive, _ = self.archive()
        put(archive / 'dependencies/features/features.npy', b'other')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            verify_artifacts(archive)

    def test_detached_digest_and_independent_expected_digest(self):
        archive, manifest = self.archive()
        with (archive / MANIFEST).open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, 'manifest SHA-256 mismatch'):
            verify_artifacts(archive)
        self.reseal(archive, manifest)
        with self.assertRaisesRegex(ValueError, 'manifest SHA-256 mismatch'):
            verify_artifacts(archive, expected_manifest_sha256='0' * 64)
        self.assertEqual(verify_artifacts(archive, expected_manifest_sha256=sha256(archive / MANIFEST)), manifest)

    def test_path_escape_aliases_rejected_even_with_matching_detached_digest(self):
        archive, manifest = self.archive()
        for bad in ('../outside', '/absolute', 'C:/absolute', 'nested\\item.bin', 'nested/CON',
                    'nested/name:alternate', 'nested/../item.bin', 'nested/ITEM.BIN'):
            with self.subTest(path=bad):
                extra = dict(manifest['artifacts'][-1], artifact_id=self.run_id + ':extra', path=bad)
                self.reseal(archive, {**manifest, 'artifacts': [*manifest['artifacts'], extra]})
                with self.assertRaisesRegex(ValueError, 'relative artifact path|artifact path'):
                    verify_artifacts(archive)

    def test_unknown_and_circular_dependencies_rejected(self):
        archive, manifest = self.archive()
        entry = manifest['artifacts'][-1]
        for parent, message in (('missing', 'Unknown parent'), (entry['artifact_id'], 'cycle')):
            with self.subTest(parent=parent):
                entry['parents'] = [parent]
                self.reseal(archive, manifest)
                with self.assertRaisesRegex(ValueError, message):
                    verify_artifacts(archive)

    def test_manifest_model_identity_must_agree_with_summary(self):
        archive, manifest = self.archive()
        manifest['model_manifest_sha256'] = 'b' * 64
        self.reseal(archive, manifest)
        with self.assertRaisesRegex(ValueError, 'digests do not match'):
            verify_artifacts(archive)

    def test_linked_subdirectory_refused(self):
        self.make_result()
        alias = self.output / 'alias'
        target = self.base / 'outside'
        put(target / 'secret.bin', b'test')
        if os.name == 'nt':
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/j', str(alias), str(target)],
                           check=True, capture_output=True)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(target, target_is_directory=True)
            self.addCleanup(alias.unlink)
        with self.assertRaisesRegex(ValueError, 'contain links'):
            verify_artifacts(self.output)

    def test_hard_linked_retained_file_refused(self):
        self.make_result()
        member = self.output / 'logs/rf.log'
        os.link(member, self.base / 'retained-link')
        with self.assertRaisesRegex(ValueError, 'regular, unlinked file'):
            verify_artifacts(self.output)

    def test_existing_and_nested_destinations_refused(self):
        self.make_result()
        with self.assertRaises(FileExistsError):
            export_artifact_archive(self.output, self.features)
        with self.assertRaisesRegex(ValueError, 'outside the source result'):
            export_artifact_archive(self.output, self.output / 'archive')
        self.assertTrue(verify_artifacts(self.output))

    def test_copy_failure_removes_staging_and_preserves_original(self):
        self.make_result()
        target = self.base / 'archive'
        with patch('ntv3_tsrf.artifact_archive.shutil.copyfileobj', side_effect=OSError('test write failure')):
            with self.assertRaisesRegex(OSError, 'test write failure'):
                export_artifact_archive(self.output, target, external_mode='copy')
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.glob('.incfinder-*')), [])
        self.assertTrue(verify_artifacts(self.output))

    def test_reconciliation_binds_manifest_and_checks_nested_files(self):
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.base / 'runs')))
        with run_record.RunRecord('api') as run:
            self.run_id = run.run_id
            run.data['parameters'] = {'output': {'value': str(self.output), 'source': 'explicit'}}
            for stage in ('paths', 'assets', 'staging', 'result_validation'):
                run.enter_stage(stage)
            digest = self.make_result()
            run.prepare_publication(self.output / 'run_summary.json', artifact_manifest_sha256=digest)
        event = load_run_events(self.run_id)[-1]
        self.assertEqual(event['payload']['artifact_manifest_sha256'], digest)
        self.assertTrue(run.has_verified_publication())
        (self.output / 'logs/worker/details.log').write_bytes(b'changed')
        self.assertEqual(reconcile_run(self.run_id)['status'], 'interrupted')
