"""Check both candidate training jobs and reject an unrelated prediction binding."""
from pathlib import Path
import tempfile
import unittest
import shutil

from ntv3_tsrf.model_lineage import POINTER, fact, load_pointer, training_jobs
from ntv3_tsrf.resources import ROOT


class PackagedResearchJobs(unittest.TestCase):
    def test_two_weight_identities_and_binding_are_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            for name in ('assets.json', 'research_index.json', 'model_lineage.json'):
                shutil.copyfile(ROOT / 'config' / name, root / 'config' / name)
            pointer = load_pointer(root)
            binding = dict(candidate_id=pointer['candidate_id'], index_sha256=pointer['index_sha256'],
                           pointer_sha256=fact(root / POINTER)['sha256'])
            jobs = training_jobs(root, binding)
            for layer in ('stage1', 'stage2'):
                self.assertEqual(jobs[layer]['weight_sha256'], pointer['assets'][f'models/rf/{layer}.joblib']['sha256'])
                self.assertTrue(jobs[layer]['attempt_id'])
            with self.assertRaisesRegex(ValueError, 'does not match'):
                training_jobs(root, dict(binding, index_sha256='0' * 64))
            with (root / 'config/research_index.json').open('ab') as stream:
                stream.write(b' ')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                training_jobs(root, binding)
