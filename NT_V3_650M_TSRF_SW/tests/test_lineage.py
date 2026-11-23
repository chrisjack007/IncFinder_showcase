"""Exercise sealed result lineage, trust boundaries and bounded offline pages."""
from contextlib import redirect_stderr, redirect_stdout
import csv
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from incfinder import export_artifact_archive, export_lineage, inspect_lineage
from ntv3_tsrf import cli
from ntv3_tsrf.artifact_manifest import MANIFEST, SEAL, write_run_artifacts
from ntv3_tsrf.classify import decide, load_thresholds, write_tables
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.lineage_evidence import load_evidence
from ntv3_tsrf.output_metadata import load_output_contract, write_output_metadata
from ntv3_tsrf.report import export_workbook
from ntv3_tsrf.run_record import RunRecord
from ntv3_tsrf.sequence_io import ordered_identity, read_records


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix='if010 \u6d4b\u8bd5 '))).resolve()
        self.folder = self.base / 'result'
        self.source = self.base / 'source.fasta'
        self.meta = self.base / 'external'
        self.state = self.base / 'runs'
        self.enterContext(patch.dict(os.environ, INCFINDER_RUN_DIR=str(self.state)))
        self.run_id = str(uuid4())

    def make_run(self, *, internal=False, journal=None, mode='u05'):
        self.folder.mkdir()
        self.source.write_text('><script>header\n' + 'A' * 220 + '\n>two\n' + 'C' * 221 +
                               '\n>three\n' + 'G' * 222 + '\n', encoding='ascii')
        records = read_records(self.source)
        self.meta = self.folder / 'features' if internal else self.meta
        self.internal = internal
        self.meta.mkdir()
        np.save(self.meta / 'features.npy', np.zeros((3, 1536), np.float32), allow_pickle=False)
        contract_sha = sha256(ROOT / 'config/contract.json')
        asset_sha = sha256(ROOT / 'config/assets.json')
        feature_meta = {
            'schema': 1, 'shape': [3, 1536], 'dtype': 'float32', 'features_sha256': sha256(self.meta / 'features.npy'),
            'contract_sha256': contract_sha, 'assets_sha256': asset_sha, 'records': ordered_identity(records),
        }
        write_json(self.meta / 'features.json', feature_meta)
        contract = read_json(ROOT / 'config/contract.json')
        names = contract['labels']
        thresholds = load_thresholds(contract) if mode == 'ct' else [0.5] * 21
        p1 = np.array([0.25, 0.5, 0.75], np.float32)
        p2 = np.zeros((3, 21), np.float32)
        p2[0] = np.nan
        p2[2, 0] = 1.0
        calls = decide(p1, p2, thresholds)
        tables = write_tables(records, p1, p2, calls, names, self.folder, mode, 'RESEARCH_PREVIEW')
        np.savez(self.folder / 'probabilities.npz', stage1=p1, stage2=p2, **calls)
        policy = load_output_contract()
        prediction = {
            'status': 'PASS', 'record_count': 3, 'scientific_status': 'RESEARCH_PREVIEW', 'threshold_mode': mode,
            'stage1_threshold': 0.5, 'stage2_thresholds': thresholds, 'label_order': names, **policy,
            'tables': tables, 'internal_counts': {'Unclassified': 1, 'Inc_no_call': 1, 'Inc_called': 1},
            'counts': {'Inc_unclassified': 2, 'Inc_classified': 1}, 'gate_counts': {'passed': 2, 'rejected': 1},
        }
        write_json(self.folder / 'prediction.json', prediction)
        with patch('ntv3_tsrf.report.environment', return_value={'python': 'test'}):
            export_workbook(self.folder)
        write_output_metadata(self.folder, output_contract=policy)
        (self.folder / 'logs').mkdir()
        for name in (('rf', 'report', 'embedding') if internal else ('rf', 'report')):
            (self.folder / f'logs/{name}.log').write_text('complete\n', encoding='ascii')
        if journal:
            self.run_id = journal.run_id
            for stage in ('paths', 'assets', 'staging', 'classification', 'report', 'result_validation'):
                journal.enter_stage(stage)
        self.summary = {
            'run_id': self.run_id, 'execution_status': 'PASS', 'prediction': prediction, **policy,
            'input': {'record_count': 3, 'sha256': sha256(self.source)}, 'contract_sha256': contract_sha,
            'runtime_policy_sha256': sha256(ROOT / 'config/runtime.json'), 'features_sha256': feature_meta['features_sha256'],
            'assets': {'manifest_sha256': asset_sha},
        }
        self.seal()
        if journal:
            journal.prepare_publication(self.folder / 'run_summary.json', artifact_manifest_sha256=self.digest)

    def seal(self):
        for name in ('run_summary.json', MANIFEST, SEAL):
            (self.folder / name).unlink(missing_ok=True)
        self.summary['outputs'] = {p.name: sha256(p) for p in self.folder.iterdir() if p.is_file()}
        write_json(self.folder / 'run_summary.json', self.summary)
        self.digest = write_run_artifacts(self.folder, self.summary, self.source,
                                         None if self.internal else self.meta,
                                         None if self.internal else sha256(self.meta / 'features.json'))

    def relabel_digest(self, name):
        metadata_path = self.folder / (name + '.metadata.json')
        metadata = read_json(metadata_path)
        metadata['result_sha256'] = sha256(self.folder / name)
        write_json(metadata_path, metadata)

    def test_file_graph_binds_expected_digest_and_completed_event(self):
        with RunRecord('api') as run:
            self.make_run(journal=run)
            run.succeed()
        graph = inspect_lineage(self.folder, expected_manifest_sha256=self.digest)
        self.assertEqual((graph['schema_version'], graph['kind'], graph['identity']['run_id']),
                         (1, 'file_lineage', run.run_id))
        self.assertEqual(graph['verification']['manifest_evidence'],
                         ['detached_manifest_sha256', 'caller_manifest_sha256', 'publication_event_sha256'])
        self.assertEqual(graph['status_counts'], {'Unclassified': 1, 'Inc_no_call': 1, 'Inc_called': 1})
        self.assertTrue(all(e['relative_path'] is None for e in graph['entities'] if e.get('storage') == 'reference'))
        self.assertFalse(any(a['stage'] == 'embedding' for a in graph['activities']))
        self.assertIn('stage_recorded_at_utc', next(a for a in graph['activities'] if a['stage'] == 'classification'))
        self.assertEqual({edge['type'] for edge in graph['relations']}, {'used', 'wasGeneratedBy', 'wasDerivedFrom'})

    def test_rejected_row_has_no_stage2_model_activity_or_probability(self):
        self.make_run()
        graph = inspect_lineage(self.folder, row_index=0)
        self.assertEqual(graph['row'], {'internal_status': 'Unclassified', 'gate_pass': False,
                                       'stage1_probability': 0.25, 'stage2_status': 'not_run_gate_rejected',
                                       'called_labels': []})
        self.assertFalse(any('stage2' in item['id'] for item in graph['activities'] + graph['entities']))
        self.assertIn('Compute Stage 1 probability', {a['label'] for a in graph['activities']})

    def test_gate_pass_no_call_has_finite_stage2_and_threshold_policy(self):
        self.make_run(mode='ct')
        graph = inspect_lineage(self.folder, row_index=1)
        self.assertEqual((graph['row']['gate_pass'], graph['row']['internal_status'], graph['row']['called_labels']),
                         (True, 'Inc_no_call', []))
        thresholds = next(e for e in graph['entities'] if e['id'].endswith('/stage2-thresholds'))
        self.assertEqual(thresholds['sha256'], sha256(ROOT / 'config/thresholds.tsv'))
        stage2 = next(e for e in graph['entities'] if e['id'].endswith(':row/1/stage2'))
        self.assertEqual(stage2['probabilities'], [0.0] * 21)

    def test_called_row_proves_feature_index_and_model_identity(self):
        self.make_run()
        graph = inspect_lineage(self.folder, row_index=2)
        first_label = self.summary['prediction']['label_order'][0]
        self.assertEqual((graph['row']['internal_status'], graph['row']['called_labels']), ('Inc_called', [first_label]))
        feature = next(e for e in graph['entities'] if e['id'].endswith(':row/2/feature'))
        self.assertEqual((feature['row_index'], feature['width'], feature['dtype']), (2, 1536, 'float32'))
        model = next(e for e in graph['entities'] if e['id'].endswith(':model/stage2'))
        self.assertEqual(len(model['sha256']), 64)
        self.assertTrue(any(r['type'] == 'used' and r['entity'] == model['id'] for r in graph['relations']))
        self.assertNotIn('<script>', json.dumps(graph))

    def test_retained_features_record_encoder_but_external_features_do_not(self):
        self.make_run(internal=True)
        graph = inspect_lineage(self.folder, row_index=0)
        self.assertTrue(any(e['id'].endswith(':model/encoder') for e in graph['entities']))
        self.assertTrue(any(a['stage'] == 'embedding' for a in graph['activities']))

    def test_copied_archive_verified_after_relocation_without_event_timestamps(self):
        with RunRecord('api') as run:
            self.make_run(journal=run)
            run.succeed()
        archive = self.base / 'archive'
        export_artifact_archive(self.folder, archive, external_mode='copy')
        self.source.unlink()
        archive = archive.rename(self.base / 'relocated')
        report = inspect_lineage(archive, row_index=2, expected_manifest_sha256=sha256(archive / MANIFEST))
        self.assertEqual(report['identity']['source_manifest_sha256'], self.digest)
        self.assertTrue(all('stage_recorded_at_utc' not in node for node in report['activities']))
        self.assertEqual(report['verification']['manifest_evidence'], ['detached_manifest_sha256', 'caller_manifest_sha256'])

    def test_missing_reference_and_trusted_digest_mismatch_are_refused(self):
        self.make_run()
        with self.assertRaisesRegex(ValueError, 'manifest SHA-256 mismatch'):
            inspect_lineage(self.folder, expected_manifest_sha256='0' * 64)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing artifact'):
            inspect_lineage(self.folder)

    def test_incomplete_local_run_is_refused(self):
        with RunRecord('api') as run:
            self.make_run(journal=run)
            with self.assertRaisesRegex(ValueError, 'does not bind a completed result'):
                inspect_lineage(self.folder)

    def test_swapped_csv_rows_are_refused_even_after_new_seals(self):
        self.make_run()
        name = 'inc_results.csv'
        path = self.folder / name
        with path.open(encoding='utf-8-sig', newline='') as stream:
            fields, first, second, third = list(csv.reader(stream))
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            csv.writer(stream).writerows((fields, third, second, first))
        self.relabel_digest(name)
        self.seal()
        with self.assertRaisesRegex(ValueError, r'CSV row conflicts .* row 0'):
            inspect_lineage(self.folder)

    def test_features_with_another_record_order_are_refused(self):
        self.make_run()
        path = self.meta / 'features.json'
        meta = read_json(path)
        meta['records'].reverse()
        write_json(path, meta)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'input IDs, order or sequence hashes'):
            inspect_lineage(self.folder)

    def test_corrupted_probability_semantics_fail_with_valid_checksums(self):
        self.make_run()
        name = 'probabilities.npz'
        with np.load(self.folder / name, allow_pickle=False) as raw:
            arrays = {key: raw[key] for key in raw.files}
        arrays['stage2'][0] = 0.0
        np.savez(self.folder / name, **arrays)
        self.relabel_digest(name)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Gate-rejected Stage2 probabilities must be NaN'):
            inspect_lineage(self.folder)

    def test_sidecar_context_conflict_is_refused(self):
        self.make_run()
        path = self.folder / 'inc_predictions.xlsx.metadata.json'
        meta = read_json(path)
        meta['scientific_status'] = 'FROZEN_REFERENCE'
        write_json(path, meta)
        self.seal()
        with self.assertRaisesRegex(ValueError, 'metadata conflicts with the prediction context'):
            inspect_lineage(self.folder)

    def test_row_bounds_and_page_size_are_enforced(self):
        self.make_run()
        for row in (-1, True, 1.5, 3):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'row_index'):
                inspect_lineage(self.folder, row_index=row)
        for opts in ({'page_size': 101}, {'start': -1}, {'start': 1, 'stop': 1}, {'stop': 4}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                export_lineage(self.folder, self.base / 'export', **opts)
        self.assertFalse((self.base / 'export').exists())

    def test_offline_pages_and_json_preserve_original_files(self):
        self.make_run()
        before = {p: sha256(p) for p in self.folder.rglob('*') if p.is_file()}
        output = self.base / 'export'
        exported = export_lineage(self.folder, output, start=0, stop=3, page_size=2)
        self.assertEqual(exported, read_json(output / 'lineage.json'))
        home = (output / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('class="record"', home)
        self.assertNotIn('<script', home)
        self.assertNotIn('https://', home)
        self.assertIn('href="rows-0.html"', home)
        page = (output / 'rows-0.html').read_text(encoding='utf-8')
        self.assertIn('rel="next" href="rows-2.html"', page)
        self.assertNotIn('<script>', page)
        self.assertEqual([r['row_index'] for r in read_json(output / 'rows-2.json')['records']], [2])
        self.assertEqual(before, {p: sha256(p) for p in self.folder.rglob('*') if p.is_file()})

    def test_output_path_scope_and_atomic_failure(self):
        self.make_run()
        with self.assertRaisesRegex(ValueError, 'outside'):
            export_lineage(self.folder, self.folder / 'extra')
        with self.assertRaises(FileExistsError):
            export_lineage(self.folder, self.base / 'external')
        target = self.base / 'export'
        with patch('ntv3_tsrf.lineage_export._page_html', side_effect=OSError('representative write failure')):
            with self.assertRaisesRegex(OSError, 'write failure'):
                export_lineage(self.folder, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.glob('.incfinder-*')), [])

    def test_cli_query_and_range_validation(self):
        self.make_run()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(['lineage', str(self.folder), '--row', '1']), 0)
        self.assertEqual(json.loads(output.getvalue())['row_index'], 1)
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(cli.main(['lineage', str(self.folder), '--stop', '2']), 2)
        self.assertIn('require --output', errors.getvalue())

    def test_large_overview_has_no_record_nodes_and_reads_only_selected_rows(self):
        from ntv3_tsrf.lineage_export import _write_export

        self.make_run()
        evidence = load_evidence(self.folder)
        access = []

        class SyntheticRecords:
            def __len__(self):
                return 200_000

            def __getitem__(_, index):
                access.append(index)
                return evidence.records[index % 3]

        class SyntheticArray:
            def __init__(self, base):
                self.base = base

            def __getitem__(self, index):
                return self.base[index % 3]

        large = replace(evidence, records=SyntheticRecords(),
                        arrays={name: SyntheticArray(arr) for name, arr in evidence.arrays.items()},
                        status_counts={'Unclassified': 100_000, 'Inc_no_call': 60_000, 'Inc_called': 40_000})
        output = self.base / 'large'
        output.mkdir()
        graph = _write_export(large, output, 120_000, 120_105, 100)
        self.assertEqual(graph['record_count'], 200_000)
        self.assertLess((output / 'index.html').stat().st_size, 25_000)
        self.assertEqual(access, list(range(120_000, 120_105)))
        self.assertEqual(len(read_json(output / 'rows-120000.json')['records']), 100)
        self.assertEqual(len(read_json(output / 'rows-120100.json')['records']), 5)
        self.assertNotIn('class="record"', (output / 'index.html').read_text(encoding='utf-8'))

    def test_redacted_reference_can_be_resolved_without_exporting_real_path(self):
        self.make_run()
        hidden = self.source.rename(self.base / 'token=synthetic.fasta')
        self.source = hidden
        self.seal()
        with self.assertRaisesRegex(ValueError, 'Redacted reference'):
            inspect_lineage(self.folder)
        identifier = next(e['artifact_id'] for e in read_json(self.folder / MANIFEST)['artifacts'] if e['role'] == 'input')
        graph = inspect_lineage(self.folder, reference_paths={identifier: hidden})
        self.assertNotIn('token=synthetic', json.dumps(graph))
