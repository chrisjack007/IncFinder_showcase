"""Regression checks for subcommands, external models and path selection."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from ntv3_tsrf.classify import prepare_model_loading
from ntv3_tsrf import cli, resources


class DistributionTests(unittest.TestCase):
    def test_positional_and_legacy_alias_agree(self):
        for args in (['predict', 'input.fa', '-o', 'result'], ['--i', 'input.fa', '--o', 'result']):
            with patch.object(cli, '_predict', return_value={}) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(run.call_args.kwargs['input'], 'input.fa')

    def test_duplicate_or_missing_input_rejected(self):
        for args in (['predict','a.fa','-i','b.fa','-o','out'], ['predict','-o','out'], ['predict','-','-o','out']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                cli.main(args)
            self.assertEqual(result.exception.code, 2)

    def test_no_arguments_help_and_version_commands(self):
        for args, expected in (([], 'Commands'), (['help','predict'], '--model-dir'), (['version'], '0.5.0rc2')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args), 0)
            self.assertIn(expected, output.getvalue())

    def test_explicit_model_error_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'INCFINDER_MODEL_DIR': str(resources.CODE_ROOT/'models')}):
                with self.assertRaisesRegex(ValueError, 'model directory'):
                    resources.model_root(Path(directory)/'missing')

    def test_environment_binding_and_explicit_precedence(self):
        with tempfile.TemporaryDirectory(prefix='incfinder \u6a21\u578b ') as directory:
            base = Path(directory)
            for name in ('argument', 'variable', 'binding'):
                for child in ('ntv3', 'rf'):
                    (base/name/child).mkdir(parents=True)
            record=base/'binding.json'
            record.write_text(json.dumps({'schema':1,'model_dir':str(base/'binding')}),encoding='utf-8')
            with patch.object(resources,'binding_path',return_value=record), patch.dict(os.environ,{'INCFINDER_MODEL_DIR':str(base/'variable')}):
                self.assertEqual(resources.model_location(base/'argument')['source'], 'argument')
                self.assertEqual(resources.model_location()['source'], 'INCFINDER_MODEL_DIR')
            with patch.object(resources,'binding_path',return_value=record), patch.dict(os.environ,{},clear=True):
                self.assertEqual(resources.model_root(),base/'binding')

    def test_rf_descriptor_budget_respects_hard_limit(self):
        for soft, hard, wanted in ((1024,1048576,32768),(1024,16384,16384),(-1,-1,-1)):
            fake=SimpleNamespace(RLIMIT_NOFILE=7,RLIM_INFINITY=-1,
                                 getrlimit=Mock(return_value=(soft,hard)),setrlimit=Mock())
            with patch.dict('sys.modules',{'resource':fake}), contextlib.redirect_stdout(io.StringIO()):
                result=prepare_model_loading()
            self.assertEqual(result['soft_effective'],wanted)
            if wanted != soft: fake.setrlimit.assert_called_once_with(7,(wanted,hard))
            else: fake.setrlimit.assert_not_called()

    def test_resource_traversal_rejected(self):
        with self.assertRaises(ValueError):
            resources.checked_child(resources.ROOT, '../outside')


if __name__ == '__main__':
    unittest.main(verbosity=2)
