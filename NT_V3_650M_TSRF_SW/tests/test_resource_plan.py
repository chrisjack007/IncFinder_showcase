"""Resource planning, streamed identities and cross-block output regressions."""
from contextlib import redirect_stderr, redirect_stdout
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
from zipfile import ZipFile

import numpy as np

from incfinder import IncFinderError, predict
from ntv3_tsrf import api, cli
from ntv3_tsrf.checkpoints import identity_sha256, plan_batches, plan_index_batches
from ntv3_tsrf.classify import (_gated_batches, _result_arrays, decide, load_thresholds, validate_features,
                                write_block_results, write_tables)
from ntv3_tsrf.common import ROOT, read_json, sha256, verify_assets, write_json
from ntv3_tsrf.feature_metadata import read_feature_metadata, write_feature_metadata
from ntv3_tsrf.output_metadata import _check_npz
from ntv3_tsrf.record_index import identity_digest, indexed_records
from ntv3_tsrf.resource_plan import FEATURE_BYTES_PER_ROW, RESULT_BYTES_PER_ROW, _disk_budget, survey_input
from ntv3_tsrf.sequence_io import iter_records, ordered_identity, read_records

from output_helpers import make_result


FIXTURES = ROOT / 'tests/fixtures'
LIMITS = read_json(ROOT / 'config/contract.json')['validated_length_bp']


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.models = self.base / 'models'
        (self.models / 'ntv3').mkdir(parents=True)
        (self.models / 'rf').mkdir()
        self.output = self.base / 'output'
        self.state = self.base / 'state'
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.state)))
        self.assets = self.enterContext(patch.object(api, 'verify_assets', return_value={'manifest_sha256': '0' * 64}))
        self.worker = self.enterContext(patch.object(api, 'worker'))
        self.enterContext(patch.object(api, 'environment', return_value={'python': 'test'}))
        self.enterContext(patch.object(api, 'verify_package', return_value={}))

    def options(self, **extra):
        return dict({'input': FIXTURES / 'reference.fasta', 'output': self.output,
                     'features_dir': FIXTURES, 'model_dir': self.models}, **extra)

    def call_cli(self, *extra):
        argv = ['predict', str(FIXTURES / 'reference.fasta'), '--output', str(self.output),
                '--features-dir', str(FIXTURES), '--model-dir', str(self.models), '--dry-run', *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_does_not_allocate_files_or_workers(self):
        before = sorted(self.base.rglob('*'))
        with patch.object(api, 'RunRecord', side_effect=AssertionError('registered a run')), \
                patch('tempfile.gettempdir', side_effect=AssertionError('writable temp probe')):
            status, output, err = self.call_cli('--output-formats', 'csv,npz')
        self.assertEqual(status, 0, err)
        receipt = json.loads(output)
        self.assertEqual(receipt['input']['record_count'], 11)
        self.assertEqual(receipt['input']['total_bases'], 28087)
        self.assertEqual(sum(receipt['input']['length_buckets_by_upper_bp'].values()), 11)
        self.assertEqual(receipt['requested_formats'], ['csv', 'npz'])
        self.assertTrue(receipt['external_features'])
        self.assertEqual(receipt['retained_feature_data_bytes'], 0)
        self.assertEqual(receipt['stored_result_array_data_bytes'], 11 * RESULT_BYTES_PER_ROW)
        self.assertGreater(receipt['required_model_store_bytes'], 0)
        self.assertIsNone(receipt['memory']['minimum_memory_required_bytes'])
        self.assertEqual(sorted(self.base.rglob('*')), before)
        self.worker.assert_not_called()
        self.assets.assert_called_once_with(quick=True, model_dir=self.models)

    def test_invalid_dry_run_does_not_create_a_run(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['predict', '--dry-run'])
        self.assertFalse(self.state.exists())

    def test_low_space_detected_in_actual_and_dry_paths_before_expensive_validation(self):
        with patch('ntv3_tsrf.resource_plan.disk_usage', return_value=SimpleNamespace(free=0)):
            code, _, error = self.call_cli()
            self.assertEqual(code, 2)
            self.assertRegex(error, r'insufficient space .*required [1-9]\d* bytes, available 0 bytes')
            self.assertFalse(self.state.exists())
            with self.assertRaisesRegex(IncFinderError, 'insufficient space') as caught:
                predict(**self.options())
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.base.glob('.ntv3-run-*')))
        self.assertIsNotNone(caught.exception.run_id)
        self.assertTrue(all(call.kwargs['quick'] for call in self.assets.call_args_list))
        self.worker.assert_not_called()

    def test_read_only_volume_is_reported_without_trying_to_write(self):
        with patch('ntv3_tsrf.resource_plan.os.access', return_value=False):
            code, _, error = self.call_cli()
        self.assertEqual(code, 2)
        self.assertIn('output directory is not writable', error)
        self.assertFalse(self.state.exists())
        self.worker.assert_not_called()

    def test_missing_assets_use_existing_model_file_resolution(self):
        self.assets.side_effect = verify_assets
        code, _, error = self.call_cli()
        self.assertEqual(code, 2)
        self.assertRegex(error, r'(?i)(missing|size mismatch)')
        self.assertFalse(self.state.exists())
        self.worker.assert_not_called()

    def test_capacity_adds_simultaneous_roles_but_not_successive_phases(self):
        budgets = {'encode': {'a': (2, 8), 'b': (3, 12)}, 'report': {'a': (7, 18)}}
        with patch('ntv3_tsrf.resource_plan.disk_usage', return_value=SimpleNamespace(free=10)):
            volumes, warnings = _disk_budget({'a': self.base, 'b': self.models}, budgets)
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0]['phases']['encode'], {'required_bytes': 5, 'estimated_bytes': 20})
        self.assertEqual(volumes[0]['phases']['report']['required_bytes'], 7)
        self.assertEqual(len(warnings), 2)

    def test_internal_encoding_accounts_for_checkpoint_and_assembled_matrix(self):
        report = api._dry_run(**self.options(features_dir=None, allow_encoder_drift=True))
        self.assertEqual(report['retained_feature_data_bytes'], FEATURE_BYTES_PER_ROW * 11)
        phase = [v['phases']['encoding'] for v in report['volumes'] if 'encoding' in v['phases']]
        self.assertGreaterEqual(sum(v['required_bytes'] for v in phase), 2 * FEATURE_BYTES_PER_ROW * 11)
        self.assertFalse(self.state.exists())

    def test_feature_identity_failure_precedes_model_access(self):
        other = self.base / 'features'
        other.mkdir()
        for name in ('features.json', 'features.npy'):
            shutil.copyfile(FIXTURES / name, other / name)
        info = read_json(other / 'features.json')
        info['records'].reverse()
        write_json(other / 'features.json', info)
        with self.assertRaisesRegex(ValueError, 'IDs, order or sequence hashes'):
            api._dry_run(**self.options(features_dir=other))
        self.assets.assert_not_called()
        self.worker.assert_not_called()

    def test_raw_input_changed_during_worker_cannot_publish(self):
        raw = self.base / 'input.fasta'
        shutil.copyfile(FIXTURES / 'reference.fasta', raw)
        frozen = self.base / 'worker-files'
        make_result(frozen)

        def run_worker(executable, module, arguments, log, device='cpu'):
            self.assertEqual(module, 'ntv3_tsrf.classify')
            with raw.open('ab') as handle:
                handle.write(b'\n')
            target = arguments[2]
            for name in ('prediction.json', 'probabilities.npz', 'layer1_decisions.csv', 'layer2_probabilities.csv',
                         'inc_label_predictions.csv', 'inc_results.csv'):
                shutil.copyfile(frozen / name, target / name)

        self.worker.side_effect = run_worker
        with self.assertRaisesRegex(IncFinderError, 'input file changed during inference'):
            predict(**self.options(input=raw, output_formats='csv,npz'))
        self.assertFalse(self.output.exists())


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_wrapped_formats_and_gzip_keep_normalized_identity(self):
        samples = {'a.fa': '>one first\nacgt\nnACG\n>two\nag\n',
                   'a.fq': '@one first\nacgt\nnACG\n+one\nIIII\nIIII\n@two\nag\n+\nII\n'}
        for name, source in samples.items():
            for compressed in (False, True):
                with self.subTest(name=name, gzip=compressed):
                    path = self.base / (name + ('.gz' if compressed else ''))
                    if compressed:
                        path.write_bytes(gzip.compress(source.encode('ascii')))
                    else:
                        path.write_text(source, encoding='ascii', newline='\n')
                    ordinary = read_records(path)
                    self.assertEqual(list(iter_records(path)), ordinary)
                    observed = survey_input(path, [1, 10], unique=True)
                    self.assertEqual(observed.records_sha256, identity_sha256(ordered_identity(ordinary)))
                    with indexed_records(path) as indexed:
                        self.assertEqual(list(indexed.identities()), ordered_identity(ordinary))
                        self.assertEqual(list(indexed.headers()), [('one first', 8), ('two', 2)])

    def test_duplicate_record_ids_fail_in_read_only_and_disk_paths(self):
        raw = self.base / 'duplicates.fa'
        raw.write_text('>same a\nAC\n>same b\nGC\n', encoding='ascii', newline='\n')
        for read in (lambda: survey_input(raw, [1, 10], unique=True), lambda: read_records(raw)):
            with self.assertRaisesRegex(ValueError, 'Duplicate sequence ID'):
                read()
        with self.assertRaisesRegex(ValueError, 'Duplicate sequence ID'), indexed_records(raw):
            self.fail('Duplicate accepted by the index')

    def test_truncated_gzip_fails_in_survey(self):
        raw = self.base / 'bad.fa.gz'
        raw.write_bytes(gzip.compress(b'>withvalidname\nACGT\n')[:-7])
        with self.assertRaisesRegex(ValueError, 'Truncated gzip'):
            survey_input(raw, [1, 10])

    def test_gzip_raw_digest_is_distinct_from_normalized_row_digest(self):
        raw = self.base / 'a.fa.gz'
        data = b'>one\nAGCT\n'
        raw.write_bytes(gzip.compress(data, mtime=0))
        first = survey_input(raw, [1, 10])
        raw.write_bytes(gzip.compress(data, mtime=1))
        second = survey_input(raw, [1, 10])
        self.assertNotEqual(first.facts['sha256'], second.facts['sha256'])
        self.assertEqual(first.records_sha256, second.records_sha256)

    def test_workers_reject_changed_index_digest(self):
        with indexed_records(FIXTURES / 'reference.fasta') as records:
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'), \
                    indexed_records(FIXTURES / 'reference.fasta', index_path=records.path, index_sha256='0' * 64):
                self.fail('Index accepted the wrong identity')

    def test_disk_index_batch_membership_and_ids_match_checkpoint_contract(self):
        raw = FIXTURES / 'reference.fasta'
        ordinary = read_records(raw)
        identities = ordered_identity(ordinary)
        with indexed_records(raw) as index:
            for size in (1, 2, 4, 11):
                with self.subTest(size=size):
                    self.assertEqual(list(plan_index_batches(index, size)), list(plan_batches(ordinary, identities, size)))

    def test_streamed_feature_json_handles_split_unicode_and_roundtrips(self):
        target = self.base / 'features.json'
        row = read_records(FIXTURES / 'reference.fasta')[0].identity(0)
        row['header'] = 'header ' + '\u6d4b\u8bd5\u5b57\u7b26' * 7500
        rows = [row, {**row, 'row_index': 1, 'header': 'second'}]
        write_feature_metadata(target, {'shape': [2, 1536], 'dtype': 'float32'}, iter(rows))
        info, count, digest = read_feature_metadata(target)
        self.assertEqual((info, count, digest), ({'shape': [2, 1536], 'dtype': 'float32'}, 2, identity_digest(rows)))
        self.assertEqual(read_json(target)['records'], rows)

    def test_streamed_feature_metadata_rejects_malformed_and_duplicate_records(self):
        target = self.base / 'features.json'
        for text in ('{"records":[}', '{"records":[], "records":[]}', '{"records":[]}{ }',
                     '{"records":[1]}', '{"shape":[0,1536]}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                target.write_text(text, encoding='utf-8')
                read_feature_metadata(target)

    def test_frozen_features_are_verified_with_disk_index_and_original_shape(self):
        with indexed_records(FIXTURES / 'reference.fasta') as records:
            result = validate_features(FIXTURES, records)
            self.assertEqual(result.shape, (11, 1536))
            self.assertEqual(result.dtype, np.dtype('float32'))

    def test_selected_rows_keep_original_stage_two_chunk_boundaries(self):
        values = np.array([.1, .7, .2, .5, .1, .9, .5, .5, .5, .2, .9, .2], dtype='float32')
        for size in (1, 2, 3, 5, 20):
            with self.subTest(size=size):
                expected = np.flatnonzero(values >= .5)
                chunks = [array.tolist() for array in _gated_batches(values, size)]
                self.assertEqual(chunks, [expected[start:start+size].tolist() for start in range(0, len(expected), size)])

    def test_chunked_scientific_arrays_and_csv_match_on_both_thresholds(self):
        raw = FIXTURES / 'reference.fasta'
        contract = read_json(ROOT / 'config/contract.json')
        names = contract['labels']
        with np.load(FIXTURES / 'expected.npz', allow_pickle=False) as known, indexed_records(raw) as records:
            for mode, thresholds in (('ct', load_thresholds(contract)), ('u05', [.5] * 21)):
                reference = self.base / ('baseline-' + mode)
                reference.mkdir()
                old = decide(known['stage1'], known['stage2'], thresholds)
                fields = write_tables(read_records(raw), known['stage1'], known['stage2'], old, names, reference, mode)
                for size in (1, 4, 8):
                    with self.subTest(mode=mode, size=size):
                        output = self.base / f'{mode}-{size}'
                        scratch = self.base / f'scratch-{mode}-{size}'
                        output.mkdir()
                        scratch.mkdir()
                        with _result_arrays(scratch, len(records)) as maps:
                            maps['stage1'][:] = known['stage1']
                            maps['stage2'][:] = known['stage2']
                            tables, status, gate = write_block_results(
                                output, scratch, maps, records, names, thresholds, mode, 'UNVERIFIED', size)
                        self.assertEqual(tables, fields)
                        self.assertEqual(sum(status.values()), len(records))
                        self.assertEqual(gate, int(old['gate'].sum()))
                        self.assertTrue(all(sha256(output / name) == sha256(reference / name) for name in fields))
                        with np.load(output / 'probabilities.npz', allow_pickle=False) as observed:
                            self.assertEqual(len(observed.files), 7)
                            for name, value in {**old, 'stage1': known['stage1'], 'stage2': known['stage2']}.items():
                                self.assertEqual(observed[name].dtype, value.dtype)
                                np.testing.assert_array_equal(observed[name], value)

    def test_npz_validation_uses_headers_and_streaming_crc(self):
        pending = self.base / 'result'
        result, _, _ = make_result(pending)
        with patch('numpy.load', side_effect=AssertionError('unbounded array load')):
            _check_npz(pending / 'probabilities.npz', result, 2)

    def test_npz_validation_rejects_duplicate_members(self):
        pending = self.base / 'result'
        result, _, _ = make_result(pending)
        path = pending / 'probabilities.npz'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with ZipFile(path, 'a') as archive:
                archive.writestr('stage1.npy', archive.read('stage1.npy'))
        with self.assertRaisesRegex(ValueError, 'NPZ arrays conflict'):
            _check_npz(path, result, 2)
