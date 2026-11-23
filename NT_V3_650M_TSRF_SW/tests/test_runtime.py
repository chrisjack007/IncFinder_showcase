"""Runtime ranges are distinct from frozen numerical equivalence."""
import importlib.metadata
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ntv3_tsrf.common import ROOT, runtime_diagnostics, sha256

VERSIONS = {'numpy': '2.4.6', 'scipy': '1.17.1', 'scikit-learn': '1.9.0',
            'joblib': '1.5.3', 'torch': '2.11.0+cpu', 'transformers': '4.57.6',
            'openpyxl': '3.1.5', 'packaging': '26.0', 'safetensors': '0.7.0',
            'huggingface-hub': '0.36.2', 'tokenizers': '0.22.2'}
REQUIREMENTS = ['huggingface-hub>=0.34.0,<1.0', 'safetensors>=0.4.3',
                'tokenizers>=0.22.0,<=0.23.0', 'tokenizers<0.1; extra == "unused"']


class RuntimeTests(unittest.TestCase):
    def diagnose(self, updates=None, missing=None):
        versions = {**VERSIONS, **(updates or {})}
        def version(name):
            if name == missing:
                raise importlib.metadata.PackageNotFoundError(name)
            return versions[name]
        with patch('importlib.metadata.version', side_effect=version), patch('importlib.metadata.requires', return_value=REQUIREMENTS):
            return runtime_diagnostics()

    def test_reference_versions_and_torch_build(self):
        result = self.diagnose()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['versions']['torch'], '2.11.0+cpu')

    def test_allowed_patch_variants(self):
        result = self.diagnose({'numpy': '2.4.3', 'joblib': '1.5.4', 'openpyxl': '3.1.6'})
        self.assertEqual(result['status'], 'PASS', result)

    def test_rf_version_remains_exact(self):
        self.assertIn('scikit-learn: found 1.8.0', ';'.join(self.diagnose({'scikit-learn': '1.8.0'})['errors']))

    def test_minor_family_change_rejected(self):
        for package, version in [('numpy', '2.5.0'), ('torch', '2.12.0'), ('transformers', '5.0.0')]:
            with self.subTest(package=package):
                self.assertEqual(self.diagnose({package: version})['status'], 'FAIL')

    def test_transformers_managed_requirements(self):
        self.assertEqual(self.diagnose({'tokenizers': '0.24.0'})['status'], 'FAIL')

    def test_missing_dependency_has_specific_diagnostic(self):
        self.assertIn('Missing dependency: torch', self.diagnose(missing='torch')['errors'])

    def test_doctor_does_not_import_model_libraries(self):
        command = "from ntv3_tsrf.common import doctor; import sys; doctor(); assert not any(n in sys.modules for n in ('torch','transformers','sklearn','numpy'))"
        result = subprocess.run([sys.executable, '-B', '-c', command], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frozen_contract_identity(self):
        self.assertEqual(sha256(ROOT/'config/contract.json'), '5a409f2ab9f3c573bf0b2c2e78e8e040edeb564162793e1db3cce9557459ec7d')
        self.assertEqual(sha256(ROOT/'config/assets.json'), 'e45f15f534f6a16c0dca9f2197b174e0eb872ff5026c3d6e043ba066a79e75aa')


if __name__ == '__main__':
    unittest.main(verbosity=2)
