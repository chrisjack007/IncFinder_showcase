"""V1 migration must preserve scientific arrays and reject inconsistent sources."""
from contextlib import chdir
import csv
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from incfinder import load_output_metadata, migrate_v1_results, require_supported_policy
from ntv3_tsrf import migration
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.output_metadata import RESULT_FILES
from output_helpers import make_result, rebind_summary


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='incfinder-migration-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'v1'
        self.prediction, self.arrays, self.decisions = make_result(self.source, version='v1')
        self.output = self.base / 'v2'

    def hashes(self, directory):
        return {path.relative_to(directory).as_posix(): sha256(path) for path in directory.rglob('*') if path.is_file()}

    def assert_not_published(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.base.glob('.incfinder-migrate-*')), [])

    def test_both_modes_preserve_raw_inputs_array_values_dtypes_and_native_tables(self):
        for mode in ('ct', 'u05'):
            with self.subTest(mode=mode):
                source, output = self.base / ('v1-' + mode), self.base / ('v2-' + mode)
                prediction, arrays, decisions = make_result(source, version='v1', mode=mode)
                (source / 'nested').mkdir()
                (source / 'nested/notes.txt').write_bytes(b'complete original input\n')
                before = self.hashes(source)
                summary = migrate_v1_results(source, output)
                self.assertFalse(summary['inference_performed'])
                self.assertEqual(self.hashes(source), before)
                self.assertEqual(self.hashes(output / '00_raw_original'), before)
                for path in (output / '00_raw_original').rglob('*'):
                    if path.is_file():
                        self.assertFalse(path.stat().st_mode & stat.S_IWRITE)
                with np.load(output / 'probabilities.npz', allow_pickle=False) as migrated:
                    self.assertEqual(set(migrated.files), set(arrays) | {
                        'external_unclassified', 'external_classified', 'internal_status'})
                    for name, values in arrays.items():
                        self.assertEqual(migrated[name].dtype, values.dtype)
                        self.assertTrue(np.array_equal(migrated[name], values, equal_nan=True), name)
                    for name in ('external_unclassified', 'external_classified', 'internal_status'):
                        self.assertTrue(np.array_equal(migrated[name], decisions[name]))
                for name in ('layer1_decisions.csv', 'layer2_probabilities.csv'):
                    self.assertEqual(sha256(source / name), sha256(output / name))
                for name in RESULT_FILES:
                    metadata = require_supported_policy(load_output_metadata(output / name))
                    self.assertEqual(metadata['threshold_mode'], mode)
                manifest = read_json(output / 'migration_source_manifest.json')
                self.assertEqual({entry['path']: entry['sha256'] for entry in manifest['files']}, before)
                self.assertIn('probabilities.npz', before)

    def test_gate_passing_no_call_changes_only_external_flags_and_preserves_reason(self):
        migrate_v1_results(self.source, self.output)
        with (self.source / 'inc_label_predictions.csv').open(encoding='utf-8-sig', newline='') as stream:
            before = list(csv.DictReader(stream))
        with (self.output / 'inc_label_predictions.csv').open(encoding='utf-8-sig', newline='') as stream:
            after = list(csv.DictReader(stream))
        indices = np.flatnonzero(self.arrays['gate'] & ~self.arrays['labels'].any(axis=1))
        self.assertGreater(len(indices), 0)
        for index in indices:
            self.assertEqual(before[index]['Inc_classified'], '1')
            self.assertEqual(after[index]['Inc_classified'], '0')
            self.assertEqual(after[index]['Inc_unclassified'], '1')
            self.assertEqual(after[index]['gate_pass'], '1')
            self.assertEqual(after[index]['internal_status'], 'Inc_no_call')
            for label in self.prediction['label_order']:
                self.assertEqual(before[index][label], after[index][label])

    def test_research_preview_is_not_upgraded_by_output_migration(self):
        source = self.base / 'preview'
        make_result(source, version='v1', scientific_status='RESEARCH_PREVIEW')
        summary = migrate_v1_results(source, self.output)
        self.assertEqual(summary['status'], 'RESEARCH_PREVIEW')
        self.assertEqual(summary['scientific_equivalence'], 'NOT_ESTABLISHED')
        self.assertEqual(load_output_metadata(self.output / 'inc_results.csv')['scientific_status'], 'RESEARCH_PREVIEW')

    def test_existing_destination_is_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_bytes(b'keep original\n')
        before = self.hashes(self.source)
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            migrate_v1_results(self.source, self.output)
        self.assertEqual(sentinel.read_bytes(), b'keep original\n')
        self.assertEqual(self.hashes(self.source), before)

    def test_unicode_relative_paths_preserve_caller_working_directory(self):
        source = self.base / '\u539f\u59cb v1'
        destination = self.base / '\u8fc1\u79fb v2'
        make_result(source, version='v1')
        # Relative paths must use a caller on the same Windows volume as its data.
        with chdir(self.base):
            before = Path.cwd()
            migrate_v1_results(source.name, destination.name)
            self.assertEqual(Path.cwd(), before)
        self.assertEqual(load_output_metadata(destination / 'inc_results.csv')['output_schema_version'], 2)

    def test_destination_inside_source_or_software_is_rejected(self):
        for destination in (self.source / 'nested-output', ROOT / 'config/migration-output'):
            with self.subTest(destination=str(destination)), self.assertRaises(ValueError):
                migrate_v1_results(self.source, destination)
            self.assertFalse(destination.exists())

    def test_missing_required_parents_are_rejected(self):
        for name in ('probabilities.npz', 'inc_predictions.xlsx', 'run_summary.json', 'prediction.json'):
            path = self.source / name
            original = path.read_bytes()
            path.unlink()
            with self.subTest(missing=name), self.assertRaisesRegex(ValueError, 'Incomplete V1'):
                migrate_v1_results(self.source, self.output)
            self.assert_not_published()
            path.write_bytes(original)

    def test_source_hash_mismatch_is_rejected(self):
        with (self.source / 'inc_results.csv').open('ab') as stream:
            stream.write(b'corrupt\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            migrate_v1_results(self.source, self.output)
        self.assert_not_published()

    def test_numeric_and_gate_inconsistencies_are_not_converted_to_valid_no_calls(self):
        mutations = [
            ('stage1_nan', lambda a: a['stage1'].__setitem__(0, np.nan)),
            ('stage1_range', lambda a: a['stage1'].__setitem__(0, 2)),
            ('stage2_nan', lambda a: a['stage2'].__setitem__((int(np.flatnonzero(a['gate'])[0]), 0), np.nan)),
            ('rejected_not_nan', lambda a: a['stage2'].__setitem__((int(np.flatnonzero(~a['gate'])[0]), 0), 0)),
            ('wrong_gate', lambda a: a['gate'].__setitem__(0, not a['gate'][0])),
            ('wrong_label', lambda a: a['labels'].__setitem__((0, 0), 1 - a['labels'][0, 0])),
        ]
        for name, change in mutations:
            with self.subTest(name=name):
                arrays = {key: value.copy() for key, value in self.arrays.items()}
                change(arrays)
                np.savez(self.source / 'probabilities.npz', allow_pickle=False, **arrays)
                rebind_summary(self.source)
                with self.assertRaises(ValueError):
                    migrate_v1_results(self.source, self.output)
                self.assert_not_published()

    def test_wrong_array_shape_dtype_and_missing_array_are_rejected(self):
        cases = [dict(self.arrays, gate=self.arrays['gate'].astype('uint8')),
                 dict(self.arrays, stage2=self.arrays['stage2'][:, :20]),
                 {key: value for key, value in self.arrays.items() if key != 'labels'}]
        for arrays in cases:
            np.savez(self.source / 'probabilities.npz', allow_pickle=False, **arrays)
            rebind_summary(self.source)
            with self.assertRaises(ValueError):
                migrate_v1_results(self.source, self.output)
            self.assert_not_published()

    def test_object_arrays_are_not_unpickled(self):
        arrays = dict(self.arrays, labels=np.array([['not executable'] * 21] * 11, dtype=object))
        np.savez(self.source / 'probabilities.npz', **arrays)
        rebind_summary(self.source)
        with self.assertRaises(ValueError):
            migrate_v1_results(self.source, self.output)
        self.assert_not_published()

    def test_unknown_labels_thresholds_and_contracts_are_rejected(self):
        prediction = read_json(self.source / 'prediction.json')
        for field, value in [('label_order', list(reversed(prediction['label_order']))),
                             ('stage2_thresholds', [0.1] * 21), ('threshold_mode', 'unknown')]:
            with self.subTest(field=field):
                write_json(self.source / 'prediction.json', {**prediction, field: value})
                rebind_summary(self.source)
                with self.assertRaises(ValueError):
                    migrate_v1_results(self.source, self.output)
                self.assert_not_published()
        write_json(self.source / 'prediction.json', prediction)
        rebind_summary(self.source)
        summary = read_json(self.source / 'run_summary.json')
        summary['contract_sha256'] = '0' * 64
        write_json(self.source / 'run_summary.json', summary)
        with self.assertRaisesRegex(ValueError, 'frozen scientific contract'):
            migrate_v1_results(self.source, self.output)

    def test_counts_and_csv_rows_must_match_original_arrays(self):
        prediction = read_json(self.source / 'prediction.json')
        prediction['counts']['Inc_unclassified'] += 1
        write_json(self.source / 'prediction.json', prediction)
        rebind_summary(self.source)
        with self.assertRaisesRegex(ValueError, 'counts'):
            migrate_v1_results(self.source, self.output)
        self.assert_not_published()

    def test_csv_and_xlsx_disagreement_is_rejected_even_with_matching_container_hashes(self):
        from openpyxl import load_workbook
        workbook = load_workbook(self.source / 'inc_predictions.xlsx')
        workbook['Inc_result']['B2'] = 'different record'
        workbook.save(self.source / 'inc_predictions.xlsx')
        workbook.close()
        rebind_summary(self.source)
        with self.assertRaisesRegex(ValueError, 'XLSX/CSV'):
            migrate_v1_results(self.source, self.output)
        self.assert_not_published()

    def test_v2_cannot_be_migrated_as_v1(self):
        current = self.base / 'already-v2'
        make_result(current, version='v2')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            migrate_v1_results(current, self.output)
        self.assert_not_published()

    def test_sidecar_failure_does_not_publish_or_change_source(self):
        before = self.hashes(self.source)
        with patch.object(migration, 'write_output_metadata', side_effect=ValueError('metadata failure')):
            with self.assertRaisesRegex(ValueError, 'metadata failure'):
                migrate_v1_results(self.source, self.output)
        self.assertEqual(self.hashes(self.source), before)
        self.assert_not_published()

    def test_external_source_change_prevents_publication(self):
        original_writer = migration.write_output_metadata
        def change_source(output, **options):
            original_writer(output, **options)
            (self.source / 'external-change.txt').write_text('external change')
        with patch.object(migration, 'write_output_metadata', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                migrate_v1_results(self.source, self.output)
        self.assert_not_published()
