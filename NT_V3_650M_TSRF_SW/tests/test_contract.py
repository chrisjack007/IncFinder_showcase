import gzip
from pathlib import Path
import shutil
from unittest.mock import patch
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ntv3_tsrf.sequence_io import read_records, reverse_complement
from ntv3_tsrf.classify import decide, positive_probability, safe_text, validate_features
from ntv3_tsrf.common import ROOT, read_json, write_json, sha256
from ntv3_tsrf.sequence_io import ordered_identity


class InputTests(unittest.TestCase):
    def read(self, text, compressed=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('input.gz' if compressed else 'input.txt')
            if compressed:
                with gzip.open(path, 'wt', encoding='utf-8') as f: f.write(text)
            else: path.write_text(text, encoding='utf-8')
            return read_records(path)

    def test_wrapped_fasta_gzip(self):
        records = self.read('>a description\nacg\ntN\n>b\nAT\n', True)
        self.assertEqual([r.sequence for r in records], ['ACGTN', 'AT'])
        self.assertEqual(records[0].header, 'a description')

    def test_wrapped_fastq(self):
        self.assertEqual(self.read('@a\nAC\nGT\n+a\nII\n@@\n')[0].sequence, 'ACGT')

    def test_duplicate_first_token(self):
        with self.assertRaises(ValueError): self.read('>a one\nAC\n>a two\nGT\n')

    def test_invalid_input(self):
        for content in ['', '>a\n', '>a\nNN\n', '>a\nARY\n', '@a\nAT\n+\nI\n',
                        '@a\nAT\n+\nIII\n', '@a\nAT\n+b\nII\n', '>a\nAC GT\n']:
            with self.subTest(content=content), self.assertRaises(ValueError): self.read(content)

    def test_rc(self):
        self.assertEqual(reverse_complement('ACGTNN'), 'NNACGT')

    def test_fasta_fastq_sequence_identity(self):
        a=self.read('>a\nACGT\n')[0];b=self.read('@a\nACGT\n+\nIIII\n')[0]
        self.assertEqual(a.identity(0)['sequence_sha256'], b.identity(0)['sequence_sha256'])


class DecisionTests(unittest.TestCase):
    def test_gate_threshold_equality_and_no_call(self):
        import numpy as np
        s1=np.array([.49,.5,.7],dtype=np.float32)
        s2=np.array([[np.nan]*21,[.5]+[.1]*20,[.1]*21],dtype=np.float32)
        decisions=decide(s1,s2,[.5]*21)
        gate,labels,status=decisions['gate'],decisions['labels'],decisions['internal_status']
        self.assertEqual(gate.tolist(),[False,True,True])
        self.assertEqual(labels.sum(1).tolist(),[0,1,0])
        self.assertEqual(status.tolist(),['Unclassified','Inc_called','Inc_no_call'])
        self.assertEqual(decisions['external_unclassified'].tolist(),[True,False,True])

    def test_missing_positive_class(self):
        import numpy as np
        class Model:
            classes_=[np.array([0])]+[np.array([0,1])]*20
            def predict_proba(self,x):
                return [np.ones((len(x),1))]+[np.tile([.8,.2],(len(x),1))]*20
        result=positive_probability(Model(),np.zeros((2,1536)),multilabel=True)
        self.assertEqual(result.shape,(2,21)); self.assertTrue((result[:,0]==0).all())

    def test_formula_headers(self):
        self.assertEqual(safe_text('=1+1'),"'=1+1")
        self.assertEqual(safe_text('normal'), 'normal')


class FrozenInputCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / 'config'
        config.mkdir()
        for name in ('contract.json', 'assets.json', 'frozen_feature_inputs.json'):
            shutil.copyfile(ROOT / 'config' / name, config / name)
        self.metadata = self.root / 'features.json'
        shutil.copyfile(ROOT / 'tests/fixtures/features.json', self.metadata)
        self.features = read_json(self.metadata)
        self.registry = config / 'assets.json'
        self.entries = read_json(self.registry)
        for row in self.entries['files']:
            if row['path'].startswith('models/rf/'):
                row['sha256'] = '1' * 64
        write_json(self.registry, self.entries)

    def matches(self, *, registry=None):
        from ntv3_tsrf.feature_metadata import matches_feature_contract
        return matches_feature_contract(self.features, self.metadata, self.root,
                                        sha256(self.root / 'config/contract.json'), registry or sha256(self.registry))

    def test_exact_frozen_metadata_survives_only_rf_identity_changes(self):
        self.assertTrue(self.matches())
        shutil.copyfile(ROOT / 'tests/fixtures/features.npy', self.root / 'features.npy')
        records = read_records(ROOT / 'tests/fixtures/reference.fasta')
        with patch('ntv3_tsrf.classify.ROOT', self.root):
            self.assertEqual(validate_features(self.root, records).shape, (11, 1536))
            with self.assertRaisesRegex(ValueError, 'order'):
                validate_features(self.root, list(reversed(records)))

    def test_rewritten_frozen_metadata_is_rejected(self):
        with self.metadata.open('ab') as stream:
            stream.write(b' ')
        self.assertFalse(self.matches())

    def test_other_encoder_or_vendor_code_is_rejected(self):
        for name in ('models/ntv3/model.safetensors', 'vendor/ntv3/modeling_ntv3_pretrained.py'):
            with self.subTest(name=name):
                entry = next(row for row in self.entries['files'] if row['path'] == name)
                original = entry['sha256']
                entry['sha256'] = '0' * 64
                write_json(self.registry, self.entries)
                self.assertFalse(self.matches())
                entry['sha256'] = original

    def test_extra_encoder_member_is_rejected(self):
        self.entries['files'].append({'path': 'vendor/ntv3/extra.py', 'sha256': '1' * 64})
        write_json(self.registry, self.entries)
        self.assertFalse(self.matches())

    def test_original_contract_registry_and_array_are_all_required(self):
        for field in ('contract_sha256', 'assets_sha256', 'features_sha256'):
            with self.subTest(field=field):
                original = self.features[field]
                self.features[field] = 'f' * 64
                self.assertFalse(self.matches())
                self.features[field] = original
        self.assertFalse(self.matches(registry='2' * 64))


class FeatureTests(unittest.TestCase):
    def setUp(self):
        import shutil
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        for name in ('features.npy', 'features.json'):
            shutil.copyfile(ROOT/'tests/fixtures'/name,self.path/name)
        self.records = read_records(ROOT/'tests/fixtures/reference.fasta')

    def test_frozen_feature_binding(self):
        self.assertEqual(validate_features(self.path,self.records).shape,(11,1536))

    def test_row_permutation_rejected(self):
        with self.assertRaises(ValueError): validate_features(self.path,list(reversed(self.records)))

    def test_feature_tamper_rejected(self):
        with (self.path/'features.npy').open('r+b') as f:
            f.seek(-1,2);f.write(b'\xff')
        with self.assertRaises(ValueError): validate_features(self.path,self.records)

    def test_wrong_contract_rejected(self):
        info=read_json(self.path/'features.json');info['contract_sha256']='0'*64
        write_json(self.path/'features.json',info)
        with self.assertRaises(ValueError): validate_features(self.path,self.records)

    def test_nan_rejected_even_with_matching_file_hash(self):
        import numpy as np
        p=self.path/'features.npy';values=np.load(p);values[0,0]=np.nan;np.save(p,values)
        info=read_json(self.path/'features.json');info['features_sha256']=sha256(p)
        write_json(self.path/'features.json',info)
        with self.assertRaises(ValueError): validate_features(self.path,self.records)


if __name__ == '__main__':
    unittest.main(verbosity=2)
