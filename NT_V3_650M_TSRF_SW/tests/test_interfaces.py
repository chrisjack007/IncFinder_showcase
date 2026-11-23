"""Public interface contracts; heavy inference is covered by recorded integration runs."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
from incfinder import IncFinderError, predict
from ntv3_tsrf import api, cli
from ntv3_tsrf.common import ROOT, resolve_path


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=directory))

    def test_all_aliases(self):
        for incoming, outgoing in [('--input', '--output'), ('--i', '--o'), ('-i', '-o')]:
            with self.subTest(incoming=incoming), patch.object(cli, '_predict', return_value={'status': 'PASS'}) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main([incoming, '\u4e2d\u6587 input.fa', outgoing, '\u4e2d\u6587 output']), 0)
                self.assertEqual(run.call_args.kwargs['input'], '\u4e2d\u6587 input.fa')
                self.assertEqual(run.call_args.kwargs['output'], '\u4e2d\u6587 output')
                self.assertIsNone(run.call_args.kwargs['path_base'])

    def test_legacy_path_contract(self):
        with patch.object(cli, '_predict', return_value={}) as run, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['predict', '--input', 'a.fa', '--output', 'results/a'], legacy=True)
        self.assertEqual(run.call_args.kwargs['path_base'], ROOT)

    def test_caller_and_resource_paths_are_distinct(self):
        with tempfile.TemporaryDirectory(prefix='incfinder \u4e2d\u6587 ') as directory:
            with patch('ntv3_tsrf.common.Path.cwd', return_value=Path(directory)):
                self.assertEqual(resolve_path('\u8f93\u5165 a.fa'), Path(directory, '\u8f93\u5165 a.fa').resolve())
                self.assertEqual(resolve_path('examples/sample.fasta', ROOT), ROOT / 'examples/sample.fasta')

    def test_help_and_version_without_site_packages(self):
        for arguments in (['-m', 'incfinder', '--help'], ['-m', 'incfinder', '--version'], ['run.py', 'predict', '--help']):
            result = subprocess.run([sys.executable, '-B', '-S', *arguments], cwd=SOURCE_ROOT,
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('incfinder' if arguments[-1] == '--version' else '--input', result.stdout)

    def test_import_does_not_load_compute_libraries(self):
        result = subprocess.run([sys.executable, '-B', '-S', '-c',
            "import incfinder,sys; assert not any(n in sys.modules for n in ('torch','numpy','sklearn','transformers'))"],
            cwd=SOURCE_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_api_validation_raises_without_changing_cwd(self):
        before = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(IncFinderError, 'already exists'):
                predict(input='missing.fa', output=directory)
            with self.assertRaisesRegex(IncFinderError, 'device'):
                predict(input='missing.fa', output=Path(directory)/'new', device='typo')
        self.assertEqual(Path.cwd(), before)

    def test_failed_worker_does_not_publish_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'result'
            with patch.object(api, 'model_location', return_value={'path': str(ROOT/'models'), 'source': 'test'}), \
                 patch.object(api, 'environment', return_value={}), patch.object(api, 'verify_package', return_value={}), \
                 patch.object(api, 'verify_assets', return_value={}), patch.object(api, 'worker', side_effect=RuntimeError('worker failure')):
                with self.assertRaisesRegex(IncFinderError, 'worker failure'):
                    predict(input=ROOT/'tests/fixtures/reference.fasta', output=output,
                            features_dir=ROOT/'tests/fixtures')
            self.assertFalse(output.exists())
            self.assertEqual(len(list(Path(directory).glob('.ntv3-run-*/failure.json'))), 1)

    def test_new_encoding_requires_explicit_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(api, 'model_location', return_value={'path': str(ROOT/'models'), 'source': 'test'}), \
                 patch.object(api, 'environment', return_value={}), patch.object(api, 'verify_package', return_value={}):
                with self.assertRaisesRegex(IncFinderError, 'allow-encoder-drift'):
                    predict(input=ROOT/'tests/fixtures/reference.fasta', output=Path(directory)/'new')


if __name__ == '__main__':
    unittest.main(verbosity=2)
