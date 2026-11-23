"""Validate external final-21-zero semantics and native-state traceability."""
import ast
import csv
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from ntv3_tsrf.classify import decide, load_thresholds, write_tables
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.report import export_workbook
from ntv3_tsrf.sequence_io import read_records


class OutputPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ROOT / 'tests/fixtures'
        self.contract = read_json(ROOT / 'config/contract.json')
        self.expected = read_json(self.fixture / 'expected_output_rc2.json')
        with np.load(self.fixture / 'expected_rc2.npz', allow_pickle=False) as reference:
            self.stage1, self.stage2 = reference['stage1'], reference['stage2']
            self.frozen_labels, self.frozen_gate = reference['labels'], reference['gate']

    def decision(self, mode):
        thresholds = load_thresholds(self.contract) if mode == 'ct' else [0.5] * 21
        return decide(self.stage1, self.stage2, thresholds)

    def test_reference_identities_are_unchanged(self):
        for name, identity in self.expected['source_files'].items():
            with self.subTest(name=name):
                self.assertEqual(sha256(self.fixture / name), identity['sha256'])
                self.assertEqual((self.fixture / name).stat().st_size, identity['size_bytes'])

    def test_both_threshold_modes_match_independent_reference(self):
        for mode in ('ct', 'u05'):
            with self.subTest(mode=mode):
                result, expected = self.decision(mode), self.expected['modes'][mode]
                for key in ('gate', 'labels', 'internal_status', 'external_unclassified', 'external_classified'):
                    self.assertEqual(result[key].tolist(), expected[key], key)
                self.assertTrue(np.array_equal(result['gate'], self.frozen_gate))
                if mode == 'ct':
                    self.assertTrue(np.array_equal(result['labels'], self.frozen_labels))

    def test_external_union_is_disjoint_and_exhaustive(self):
        for mode in ('ct', 'u05'):
            result = self.decision(mode)
            masks = [result['internal_status'] == name for name in ('Unclassified', 'Inc_no_call', 'Inc_called')]
            self.assertTrue((np.sum(masks, axis=0) == 1).all())
            self.assertTrue(np.array_equal(masks[0] | masks[1], result['external_unclassified']))
            self.assertTrue(np.array_equal(result['external_unclassified'], ~result['labels'].any(axis=1)))
            self.assertTrue(np.array_equal(result['external_classified'], ~result['external_unclassified']))

    def test_equal_thresholds_and_multiple_calls(self):
        stage1 = np.array([0.5], dtype=np.float32)
        stage2 = np.array([[0.5, 0.5] + [0.1] * 19], dtype=np.float32)
        result = decide(stage1, stage2, [0.5] * 21)
        self.assertEqual(result['labels'].sum(), 2)
        self.assertEqual(result['internal_status'].tolist(), ['Inc_called'])
        self.assertEqual(result['external_unclassified'].tolist(), [False])

    def test_rejected_rows_remain_zero_even_with_synthetic_zero_threshold(self):
        result = decide(np.array([0.49]), np.full((1, 21), np.nan), [0.0] * 21)
        self.assertEqual(int(result['labels'].sum()), 0)
        self.assertEqual(result['internal_status'].tolist(), ['Unclassified'])

    def test_invalid_probabilities_and_missing_rows_fail(self):
        for value in (np.nan, np.inf, -0.01, 1.01):
            with self.subTest(stage1=value), self.assertRaises(ValueError):
                decide(np.array([value]), np.full((1, 21), np.nan), [0.5] * 21)
            values = np.zeros((1, 21)); values[0, 0] = value
            with self.subTest(stage2=value), self.assertRaises(ValueError):
                decide(np.array([0.8]), values, [0.5] * 21)
        for stage1, stage2 in ((np.empty(0), np.empty((0, 21))),
                               (np.array([0.8]), np.zeros((2, 21))),
                               (np.array([0.8]), np.zeros((1, 20)))):
            with self.assertRaises(ValueError):
                decide(stage1, stage2, [0.5] * 21)
        with self.assertRaises(ValueError):
            decide(np.array([0.1]), np.zeros((1, 21)), [0.5] * 21)
        for thresholds in ([np.nan] * 21, [0.5] * 20, [-0.1] * 21, [1.1] * 21):
            with self.assertRaises(ValueError):
                decide(np.array([0.8]), np.zeros((1, 21)), thresholds)

    def test_csv_xlsx_and_public_fields_match_the_reference(self):
        from openpyxl import load_workbook
        records = read_records(self.fixture / 'reference.fasta')
        names = self.contract['labels']
        for mode in ('ct', 'u05'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                result = self.decision(mode)
                tables = write_tables(records, self.stage1, self.stage2, result, names, output, mode, 'FROZEN_REFERENCE')
                write_json(output / 'prediction.json', {'tables': tables, 'label_order': names})
                with patch('ntv3_tsrf.report.environment', return_value={'status': 'PASS'}):
                    export_workbook(output)
                workbook = load_workbook(output / 'inc_predictions.xlsx', read_only=True)
                for (filename, fields), sheet in zip(tables.items(), workbook):
                    with (output / filename).open(encoding='utf-8-sig', newline='') as handle:
                        csv_rows = list(csv.DictReader(handle))
                    xlsx_rows = list(sheet.values)
                    self.assertEqual(list(xlsx_rows[0]), fields)
                    self.assertEqual(len(csv_rows), 11)
                    for index, row in enumerate(csv_rows):
                        self.assertEqual(int(row['row_index']), index)
                        self.assertEqual(row['Header'], records[index].header)
                        self.assertEqual(row['Scientific_status'], 'FROZEN_REFERENCE')
                        if filename == 'layer2_probabilities.csv' and not result['gate'][index]:
                            self.assertTrue(all(row['probability__' + name] == 'NA' for name in names))
                        if filename in ('inc_label_predictions.csv', 'inc_results.csv'):
                            self.assertEqual(row['internal_status'], result['internal_status'][index])
                            self.assertEqual(int(row['gate_pass']), int(result['gate'][index]))
                        if filename == 'inc_label_predictions.csv':
                            self.assertEqual(int(row['Inc_unclassified']), int(result['external_unclassified'][index]))
                            self.assertEqual(int(row['Inc_classified']), int(result['external_classified'][index]))
                        if filename == 'inc_results.csv':
                            calls = ';'.join(name for name, active in zip(names, result['labels'][index]) if active)
                            self.assertEqual(row['Result'], calls or 'Inc Unclassified')
                            self.assertEqual(row['final_status'], 'Inc_classified' if calls else 'Inc_unclassified')
                        for column, field in enumerate(fields):
                            value = xlsx_rows[index + 1][column]
                            if isinstance(value, (int, float)):
                                self.assertAlmostEqual(float(row[field]), value, places=14)
                            else:
                                self.assertEqual(row[field], '' if value is None else value)
                workbook.close()

    def test_writer_rejects_record_count_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            write_tables([], self.stage1, self.stage2, self.decision('ct'), self.contract['labels'], Path(directory), 'ct')

    def test_first_party_source_and_executable_examples_use_english(self):
        source_root = Path(__file__).resolve().parents[1]
        chinese = re.compile('[\u3400-\u4dbf\u4e00-\u9fff]')
        for path in source_root.rglob('*'):
            relative = path.relative_to(source_root)
            if not path.is_file() or any(part in {'dist', 'archive', '.git', '__pycache__', 'models'} for part in relative.parts):
                continue
            if path.suffix in {'.py', '.sh', '.ps1'} or path.name == 'Dockerfile':
                text = path.read_text(encoding='utf-8-sig')
                self.assertIsNone(chinese.search(text), str(relative))
                if path.suffix == '.py':
                    ast.parse(text)
            elif path.suffix == '.md':
                for language, block in re.findall(r'```(\w+)\n(.*?)```', path.read_text(encoding='utf-8'), flags=re.S):
                    if language in {'python', 'bash', 'sh', 'shell', 'powershell'}:
                        self.assertIsNone(chinese.search(block), str(relative))


if __name__ == '__main__':
    unittest.main(verbosity=2)
