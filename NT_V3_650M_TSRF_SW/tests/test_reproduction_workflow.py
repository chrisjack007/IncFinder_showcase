"""Acceptance tests for replay, numeric comparisons and portable research crates."""
from contextlib import redirect_stdout
from io import StringIO
import json
import shutil
import unittest
from unittest.mock import patch

import numpy as np
import test_lineage as fixtures

from incfinder import (compare_results, export_artifact_archive, export_research_archive, load_run_record,
                       reproduce, verify_research_archive)
from ntv3_tsrf import __version__, cli
from ntv3_tsrf.artifact_manifest import MANIFEST, verify_artifacts
from ntv3_tsrf.classify import decide, write_tables
from ntv3_tsrf.common import ROOT, read_json, sha256, write_json
from ntv3_tsrf.comparison import CPU_PROFILE
from ntv3_tsrf.output_metadata import delivery_details, write_output_metadata
from ntv3_tsrf.report import export_workbook
from ntv3_tsrf.retention import apply_retention, plan_retention
from ntv3_tsrf.run_record import RunRecord
from ntv3_tsrf.sequence_io import read_records
from ntv3_tsrf.sharing import export_share

RUNTIME = {'python': '3.12.15'}


class ReproductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixtures.LineageTests.setUp(self)
        self.models = self.base / 'models'
        (self.models / 'ntv3').mkdir(parents=True)
        (self.models / 'rf').mkdir()
        self.enterContext(patch('ntv3_tsrf.reproduction.environment', return_value=RUNTIME))
        self.enterContext(patch('ntv3_tsrf.reproduction.verify_assets',
                                return_value={'manifest_sha256': sha256(ROOT / 'config/assets.json')}))

    def seal(self):
        if self.shift or self.swap:
            metadata = read_json(self.meta / 'features.json')
            if self.shift:
                matrix = np.load(self.meta / 'features.npy', allow_pickle=False)
                matrix[1, 12] += np.float32(self.shift)
                np.save(self.meta / 'features.npy', matrix, allow_pickle=False)
            if self.swap:
                metadata['records'][0], metadata['records'][1] = metadata['records'][1], metadata['records'][0]
            write_json(self.meta / 'features.json', metadata)
        info = read_json(self.meta / 'features.json')
        info.update(features_sha256=sha256(self.meta / 'features.npy'), device='cpu', batch_size=1, threads=4,
                    environment=RUNTIME)
        write_json(self.meta / 'features.json', info)
        self.summary.update(version=__version__, completed_utc='2026-01-01T00:00:00Z', embedding_device='cpu',
                            embedding_environment=RUNTIME, report_environment=RUNTIME,
                            features_sha256=info['features_sha256'])
        prediction = self.summary['prediction']
        prediction['environment'] = RUNTIME
        if self.gate_change:
            probabilities = self.folder / 'probabilities.npz'
            with np.load(probabilities, allow_pickle=False) as payload:
                p1, p2 = payload['stage1'], payload['stage2']
            p1[0], p2[0] = 0.7, 0.0
            calls = decide(p1, p2, prediction['stage2_thresholds'])
            np.savez(probabilities, stage1=p1, stage2=p2, **calls)
            prediction.update(tables=write_tables(
                read_records(self.source), p1, p2, calls, prediction['label_order'],
                self.folder, prediction['threshold_mode'], 'RESEARCH_PREVIEW'))
            for path in self.folder.glob('*.metadata.json'):
                path.unlink()
            (self.folder / 'inc_predictions.xlsx').unlink()
            with patch('ntv3_tsrf.report.environment', return_value=RUNTIME):
                export_workbook(self.folder)
            write_output_metadata(self.folder, output_contract={key: prediction[key] for key in (
                'output_schema_version', 'classification_policy_id', 'output_contract_sha256')})
        write_json(self.folder / 'prediction.json', prediction)
        formats = ['csv', 'npz', 'xlsx']
        self.summary['delivery'] = delivery_details(
            formats, 'succeeded', {path.name: sha256(path) for path in self.folder.iterdir() if path.is_file()})
        if self.run is not None:
            params = dict(input=str(self.source), output=str(self.folder), features_dir=str(self.meta),
                          model_dir=str(self.models), device='cpu', batch_size=1, threads=4, jobs=1, max_length=10000,
                          threshold_mode=prediction['threshold_mode'], allow_encoder_drift=True,
                          allow_out_of_domain=False, output_formats=formats)
            self.run.data.update(parameters={name: {'value': value, 'source': 'explicit'} for name, value in params.items()},
                                 contract_sha256=self.summary['contract_sha256'],
                                 runtime_policy_sha256=self.summary['runtime_policy_sha256'],
                                 input_sha256=self.summary['input']['sha256'],
                                 model={'manifest_sha256': self.summary['assets']['manifest_sha256']},
                                 features={'external': not self.internal, 'sha256': info['features_sha256']})
        fixtures.LineageTests.seal(self)

    def make(self, name, *, internal=False, shift=0, swap=False, mode='u05', gate_change=False, journal=True, parent=None):
        self.folder = self.base / name
        self.source = self.base / f'{name}-private.fasta'
        self.meta = self.base / f'{name}-features'
        self.shift, self.swap, self.gate_change = shift, swap, gate_change
        self.run = None
        if journal:
            with RunRecord('api') as run:
                self.run = run
                if parent:
                    run.link_parent(parent)
                fixtures.LineageTests.make_run(self, internal=internal, journal=run, mode=mode)
                run.succeed()
        else:
            fixtures.LineageTests.make_run(self, internal=internal, mode=mode)
        return self.folder

    def bindings(self, source):
        manifest = read_json(source / MANIFEST)
        return {row['artifact_id']: row['path'] for row in manifest['artifacts'] if row['storage'] == 'reference'}

    def test_AC1_native_replay_passes_only_recorded_options(self):
        original = self.make('local')
        run_id = self.run_id
        digest = sha256(original / MANIFEST)
        output = self.base / 'new-run'
        with patch('ntv3_tsrf.reproduction._predict', return_value={'run_id': 'next'}) as predict:
            self.assertEqual(reproduce(run_id, output, expected_manifest_sha256=digest), {'run_id': 'next'})
        options = predict.call_args.kwargs
        self.assertEqual((options['parent_run_id'], options['features_dir'], options['threshold_mode']),
                         (run_id, self.meta, 'u05'))
        self.assertEqual(options['reproduction']['source_manifest_sha256'], digest)
        self.assertEqual(options['reproduction']['input_sha256'], sha256(self.source))
        self.assertEqual(load_run_record(run_id)['status'], 'succeeded')
        self.assertEqual(sha256(original / MANIFEST), digest)

    def test_AC1_dry_run_reports_verified_model_and_never_starts_prediction(self):
        self.make('dry')
        with patch('ntv3_tsrf.reproduction._dry_run') as dry, patch('ntv3_tsrf.reproduction._predict') as predict:
            plan = reproduce(self.run_id, self.base / 'output', dry_run=True)
        predict.assert_not_called()
        dry.assert_called_once()
        self.assertEqual(plan['status'], 'READY')
        self.assertEqual(plan['parameters']['output_formats'], ['csv', 'npz', 'xlsx'])

    def test_AC1_missing_input_model_and_wrong_binding(self):
        source = self.make('missing')
        raw = self.source
        archive = self.base / 'ref'
        export_research_archive(source, archive)
        with self.assertRaisesRegex(FileNotFoundError, 'Missing input artifact'):
            reproduce(archive, self.base / 'output', model_dir=self.models)
        bindings = self.bindings(source)
        with self.assertRaisesRegex(ValueError, 'Missing model binding'):
            reproduce(archive, self.base / 'output', reference_paths=bindings)
        raw.write_bytes(raw.read_bytes().replace(b'AAA', b'TTT', 1))
        with self.assertRaisesRegex(ValueError, 'SHA-256.*input'):
            reproduce(archive, self.base / 'output', reference_paths=bindings, model_dir=self.models)
        self.assertFalse((self.base / 'output').exists())

    def test_AC1_physical_model_failure_is_reported_before_execution(self):
        self.make('model')
        with patch('ntv3_tsrf.reproduction.verify_assets',
                   side_effect=ValueError('Model file missing or size mismatch: models/rf/stage1.joblib')):
            with self.assertRaisesRegex(ValueError, 'models/rf/stage1.joblib'):
                reproduce(self.run_id, self.base / 'output')

    def test_AC1_legacy_archive_must_have_recorded_parameters(self):
        source = self.make('history', journal=False)
        archive = self.base / 'history-archive'
        export_artifact_archive(source, archive, external_mode='copy')
        with self.assertRaisesRegex(ValueError, 'Historical archive lacks'):
            reproduce(archive, self.base / 'output', model_dir=self.models)

    def test_AC1_archive_replay_binds_source_and_archive_without_requiring_local_history(self):
        source = self.make('archive-source')
        run_id = self.run_id
        archive = self.base / 'crate'
        receipt = export_research_archive(source, archive, external_mode='copy')
        moved = archive.rename(self.base / 'relocated')
        shutil.rmtree(self.state / run_id)
        with patch('ntv3_tsrf.reproduction._predict', return_value={'run_id': 'new'}) as predict:
            reproduce(moved, self.base / 'output', model_dir=self.models,
                      expected_manifest_sha256=receipt['manifest_sha256'])
        kwargs = predict.call_args.kwargs
        self.assertIsNone(kwargs['parent_run_id'])
        self.assertEqual(kwargs['reproduction']['source_run_id'], run_id)
        self.assertEqual(kwargs['reproduction']['archive_manifest_sha256'], receipt['manifest_sha256'])
        self.assertTrue(kwargs['input'].is_relative_to(moved))

    def test_AC2_reports_separate_digests_arrays_and_tables(self):
        first = self.make('first', internal=True, mode='ct')
        second = self.make('second', internal=True, mode='ct')
        output = self.base / 'comparison'
        result = compare_results(first, second, destination=output)
        self.assertEqual(result['status'], 'identical')
        self.assertEqual(result['artifacts']['run_summary.json']['comparison'], 'different')
        self.assertEqual(len(result['arrays']), 7)
        self.assertTrue(result['arrays']['stage2']['nan_mask_equal'])
        self.assertEqual(result['arrays']['labels']['candidate_shape'], [3, 21])
        self.assertEqual(result['xlsx']['status'], 'identical')
        self.assertEqual(read_json(output / 'comparison.json'), result)
        self.assertIn('Decisions equal: True', (output / 'comparison.md').read_text(encoding='utf-8'))

    def test_AC2_detects_same_length_member_change(self):
        first = self.make('first')
        second = self.make('second')
        member = second / 'logs/rf.log'
        old = member.read_bytes()
        member.write_bytes(b'x' * len(old))
        with self.assertRaisesRegex(ValueError, 'SHA-256.*log'):
            compare_results(first, second)

    def test_AC2_cpu_tolerance_is_explicit_and_bound_to_profile(self):
        first = self.make('first', internal=True)
        second = self.make('second', internal=True, shift=5e-5)
        outside = self.make('outside', internal=True, shift=0.002)
        self.assertEqual(compare_results(first, second)['status'], 'different')
        from ntv3_tsrf import comparison
        profile = comparison.ROOT / 'config/encoder_versions' / (CPU_PROFILE + '.json')
        historical = comparison.read_json(profile)
        # The historical profile binds the original registry. A new registry
        # needs an explicit profile before its synthetic tolerance can be tested.
        with self.assertRaisesRegex(ValueError, 'requested CPU embedding profile'):
            compare_results(first, second, encoder_profile=CPU_PROFILE)
        registry = comparison.sha256(comparison.ROOT / 'config/assets.json')
        load_document = comparison.read_json
        def synthetic_profile(path):
            if path == profile:
                return {**historical, 'frozen_assets_manifest_sha256': registry}
            return load_document(path)
        with patch.object(comparison, 'read_json', side_effect=synthetic_profile):
            inner = compare_results(first, second, encoder_profile=CPU_PROFILE)
            outer = compare_results(first, outside, encoder_profile=CPU_PROFILE)
        self.assertEqual(inner['status'], 'within_tolerance')
        self.assertEqual(inner['embedding']['outside_tolerance_elements'], 0)
        self.assertGreater(inner['embedding']['max_abs_difference_by_row'][1], 0)
        self.assertEqual(outer['status'], 'different')
        self.assertEqual(outer['embedding']['outside_tolerance_rows'], [1])
        with self.assertRaisesRegex(ValueError, 'Unknown declared'):
            compare_results(first, second, encoder_profile='ambiguous')

    def test_AC2_incompatible_order_or_threshold_stops_numeric_comparison(self):
        first = self.make('first')
        second = self.make('reordered', swap=True)
        ct = self.make('other-threshold', mode='ct')
        for candidate, reason in ((second, 'records_sha256'), (ct, 'threshold_mode')):
            report = compare_results(first, candidate)
            self.assertEqual(report['status'], 'not_comparable')
            self.assertIn(reason, report['incomparable_reasons'])
            self.assertNotIn('arrays', report)

    def test_AC2_detects_gate_nan_mask_and_output_differences(self):
        first = self.make('first')
        second = self.make('second', gate_change=True)
        result = compare_results(first, second)
        self.assertEqual(result['status'], 'different')
        self.assertTrue(result['arrays']['labels']['equal'])
        self.assertEqual(result['arrays']['gate']['different_rows'], [0])
        self.assertEqual(result['arrays']['stage2']['nan_mask_rows'], [0])
        self.assertFalse(result['decisions_equal'])
        self.assertEqual(result['xlsx']['status'], 'different')

    def test_AC3_reference_archive_provenance_permissions_and_rebinding(self):
        first = self.make('parent')
        parent_id = self.run_id
        source = self.make('child', parent=parent_id)
        archive = self.base / 'crate'
        result = export_research_archive(source, archive)
        graph = read_json(archive / 'ro-crate-metadata.json')['@graph']
        by_id = {node['@id']: node for node in graph}
        self.assertEqual(by_id['ro-crate-metadata.json']['about'], {'@id': './'})
        self.assertEqual(by_id['#run-' + self.run_id]['prov:wasInformedBy'], {'@id': '#run-' + parent_id})
        self.assertTrue(any('prov:wasDerivedFrom' in node for node in graph))
        self.assertEqual(result['privacy']['scope'], 'private_research')
        self.assertEqual(len(result['licenses']), 3)
        self.assertEqual(set(result['dependencies'].values()), {'external_required'})
        refs = read_json(archive / 'payload' / MANIFEST)['artifacts']
        self.assertTrue(all(entry['path'] is None for entry in refs if entry['storage'] == 'reference'))
        moved = archive.rename(self.base / 'offline')
        rebound = verify_research_archive(moved, expected_manifest_sha256=result['manifest_sha256'],
                                         reference_paths=self.bindings(source))
        self.assertTrue(rebound['parameters_recorded'])
        self.assertEqual(set(rebound['dependencies'].values()), {'verified'})
        self.assertEqual(len(rebound['model_dependencies']), 9)
        self.assertTrue(first.is_dir())

    def test_AC3_stored_members_and_ro_crate_binding_cannot_be_replaced(self):
        src = self.make('source')
        archive = self.base / 'crate'
        result = export_research_archive(src, archive, external_mode='copy')
        verify_artifacts(archive / 'payload')
        metadata = archive / 'ro-crate-metadata.json'
        metadata.write_bytes(metadata.read_bytes().replace(b'private', b'PRIVATE', 1) + b' ')
        with self.assertRaisesRegex(ValueError, 'member.*ro-crate-metadata.json'):
            verify_research_archive(archive, expected_manifest_sha256=result['manifest_sha256'])
        metadata.unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unexpected'):
            verify_research_archive(archive)

    def test_AC3_retention_and_redacted_view_from_crate(self):
        source = self.make('private', internal=True)
        plan = plan_retention(source, 'minimal')
        apply_retention(source, 'minimal', expected_plan_sha256=plan['plan_sha256'])
        crate = self.base / 'archive'
        export_research_archive(source, crate)
        manifest = read_json(crate / 'archive_manifest.json')
        self.assertTrue(manifest['retention'])
        by_id = read_json(crate / 'ro-crate-metadata.json')['@graph']
        self.assertEqual(sum('prov:wasInvalidatedBy' in item for item in by_id), len(manifest['retention']))
        share = self.base / 'shared'
        export_share(crate, share, replay_from='features', html=True)
        self.assertEqual({path.name for path in share.iterdir()}, {'share_manifest.json', 'lineage.json', 'index.html'})
        for path in share.iterdir():
            self.assertNotIn(self.run_id, path.read_text(encoding='utf-8'))
            self.assertNotIn('private.fasta', path.read_text(encoding='utf-8'))

    def test_cli_comparison_and_archive_verify_publish_readable_output(self):
        base = self.make('first', internal=True)
        candidate = self.make('second', internal=True)
        crate = self.base / 'cli-crate'
        with redirect_stdout(StringIO()) as printed:
            self.assertEqual(cli.main(['archive', 'create', str(base), '--output', str(crate)]), 0)
            self.assertEqual(json.loads(printed.getvalue())['status'], 'verified')
        with redirect_stdout(StringIO()) as printed:
            self.assertEqual(cli.main(['archive', 'verify', str(crate)]), 0)
            self.assertEqual(json.loads(printed.getvalue())['model_status'], 'external_required')
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(['compare', str(base), str(candidate), '--output', str(self.base / 'diff')]), 0)
        self.assertTrue((self.base / 'diff/comparison.json').is_file())
