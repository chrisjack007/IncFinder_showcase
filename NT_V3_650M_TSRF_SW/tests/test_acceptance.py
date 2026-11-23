"""Acceptance uses the declared encoder tolerance without weakening frozen RF checks."""
import importlib.util
import json
from pathlib import Path
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceptance_checks', ROOT / 'scripts/compare_deployments.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class AcceptanceTests(unittest.TestCase):
    def difference(self, offset):
        tolerance = json.loads((ROOT / 'config/contract.json').read_text())['reference_tolerance']
        reference = np.zeros((1, 1536), dtype=np.float32)
        actual = reference.copy()
        actual[0, 0] = offset
        return {
            **{key: {'exact': True} for key in checks.ARRAY_KEYS},
            'features': checks.matrix_difference(actual, reference, tolerance['embedding_atol'], tolerance['embedding_rtol']),
            'csv_equal': {str(index): True for index in range(4)}, 'xlsx_cells_equal': True,
        }

    def test_in_contract_encoding_difference_retains_exactness_failure(self):
        result = self.difference(6.103515625e-05)
        self.assertFalse(result['features']['exact'])
        self.assertTrue(checks.format_accepted(result))

    def test_encoding_outside_existing_contract_tolerance_fails(self):
        self.assertFalse(checks.format_accepted(self.difference(0.01)))

    def test_native_state_arrays_require_exact_unicode_values(self):
        a = np.array(['Unclassified', 'Inc_no_call'])
        b = np.array(['Unclassified', 'Inc_called'])
        self.assertTrue(checks.matrix_difference(a, a)['exact'])
        self.assertFalse(checks.matrix_difference(a, b)['exact'])

    def test_probability_gate_label_or_table_changes_still_fail(self):
        for key in checks.ARRAY_KEYS:
            with self.subTest(key=key):
                result = self.difference(0)
                result[key]['exact'] = False
                self.assertFalse(checks.format_accepted(result))
        result = self.difference(0)
        result['xlsx_cells_equal'] = False
        self.assertFalse(checks.format_accepted(result))
