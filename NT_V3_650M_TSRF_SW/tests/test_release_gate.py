"""Exercise candidate decisions with synthetic receipts, never real model claims."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import ci
from release_gate import contracts, digest, verify_candidate

COMMIT = 'a' * 40
REPOSITORY = 'chrisjack007/IncFinder_engineer'


class CandidateGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='candidate-gate-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = [self.root / 'candidate.whl', self.root / 'candidate.conda']
        for file in self.artifacts:
            file.write_bytes(b'synthetic package identity fixture')
        self.identity = dict(schema=1, status='PASS', source_commit=COMMIT, source_clean=True,
            execution_context='github-actions', repository=REPOSITORY, run_id='123', run_attempt=1,
            source_manifest_sha256='b' * 64, python='synthetic test runtime',
            created_at_utc=datetime.now(timezone.utc).isoformat(), **contracts())
        self.build = dict(self.identity, kind='build', software_manifest_sha256='c' * 64,
            artifacts=[{'package_kind': kind, 'file': file.name, 'sha256': digest(file)}
                       for kind, file in zip(('wheel', 'conda'), self.artifacts)])
        self.build_path = self.root / 'build.json'
        self.write(self.build_path, self.build)
        self.receipts = []
        for platform in ('win-64', 'linux-64'):
            self.receipts.append(dict(self.identity, kind='unit', platform=platform, tests_run=7, tests_successful=True))
            for kind in ('wheel', 'conda'):
                self.receipts.append(self.package_receipt('installed', kind, platform=platform,
                    reference_conversion_exact=True))
        for kind in ('wheel', 'conda'):
            for mode in ('ct', 'u05'):
                self.receipts.append(self.package_receipt('frozen', kind, platform='win-64',
                    threshold_mode=mode, trusted_runner=True, arrays_exact=True, tables_exact=True,
                    scientific_equivalence='FROZEN_REFERENCE', encoder_run=False, device='cpu'))

    def package_receipt(self, kind, package_kind, **extra):
        package = next(x for x in self.build['artifacts'] if x['package_kind'] == package_kind)
        return dict(self.identity, kind=kind, package_kind=package_kind, artifact_sha256=package['sha256'],
            build_receipt_sha256=digest(self.build_path), installed_package=True, package_integrity=True,
            software_manifest_sha256='c' * 64, sidecars_validated=6,
            installation_method='pip-wheel' if package_kind == 'wheel' else 'conda-package',
            installation_record_sha256='e' * 64, **extra)

    def write(self, path, value):
        Path(path).write_text(json.dumps(value), encoding='utf-8')

    def verify(self, receipts=None):
        paths = []
        for index, value in enumerate(self.receipts if receipts is None else receipts):
            path = self.root / f'receipt-{index}.json'
            self.write(path, value)
            paths.append(path)
        return verify_candidate(self.build_path, paths, self.artifacts, source_commit=COMMIT,
                                run_id='123', run_attempt=1, repository=REPOSITORY)

    def test_git_tree_rejects_both_original_and_renamed_upstream_assets(self):
        checksum = 'd' * 64
        rows = [{'path': 'models/ntv3/config.json', 'size_bytes': 3, 'sha256': checksum}]
        tree = b'a' * 40
        oid = b'b' * 40
        def members(path):
            return b'100644 blob ' + oid + b' 3\t' + path + b'\0'
        command = [b'software/', tree, members(b'software/models/ntv3/config.json')]
        with patch.object(ci, 'git', side_effect=command), self.assertRaisesRegex(ValueError, 'upstream-only'):
            ci.verify_external_git_tree(COMMIT, rows)
        command = [b'software/', tree, members(b'other/renamed.json')]
        with patch.object(ci, 'git', side_effect=command), patch.object(ci, 'git_blob_sha256', return_value=checksum):
            with self.assertRaisesRegex(ValueError, 'renamed upstream content'):
                ci.verify_external_git_tree(COMMIT, rows)
        command = [b'software/', tree, members(b'software/notice.txt')]
        with patch.object(ci, 'git', side_effect=command), patch.object(ci, 'git_blob_sha256', return_value='e' * 64):
            result = ci.verify_external_git_tree(COMMIT, rows)
        self.assertEqual((result['matches'], result['compared_blobs'], result['upstream_files']), (0, 1, 1))

    def test_complete_current_evidence_allows_only_candidate_eligibility(self):
        decision = self.verify()
        self.assertTrue(decision['eligible'])
        self.assertFalse(decision['release_published'])
        self.assertEqual(decision['scientific_release_status'], 'RESEARCH_PREVIEW')

    def test_missing_each_required_platform_package_or_mode_is_rejected(self):
        for index in range(len(self.receipts)):
            with self.subTest(missing=index), self.assertRaisesRegex(ValueError, 'Missing'):
                self.verify(self.receipts[:index] + self.receipts[index + 1:])

    def test_duplicate_receipt_cannot_replace_missing_evidence(self):
        changed = self.receipts[:-1] + [copy.deepcopy(self.receipts[-2])]
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.verify(changed)

    def test_wrong_commit_run_attempt_or_repository_is_rejected(self):
        cases = {'source_commit': 'd' * 40, 'run_id': '122', 'run_attempt': 2,
                 'repository': 'someone/another', 'source_manifest_sha256': 'd' * 64}
        for field, value in cases.items():
            changed = copy.deepcopy(self.receipts)
            changed[-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(changed)

    def test_local_or_dirty_receipts_cannot_authorize_publication(self):
        for field, value in [('execution_context', 'local'), ('source_clean', False)]:
            changed = copy.deepcopy(self.build)
            changed[field] = value
            self.write(self.build_path, changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                self.verify()

    def test_package_bytes_are_rehashed_including_equal_length_tampering(self):
        before = self.artifacts[0].read_bytes()
        self.artifacts[0].write_bytes(before.replace(b'synthetic', b'Synthetic'))
        self.assertEqual(len(before), self.artifacts[0].stat().st_size)
        with self.assertRaisesRegex(ValueError, 'package digest'):
            self.verify()

    def test_missing_candidate_cannot_be_replaced_with_a_receipt(self):
        self.artifacts[1].unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            self.verify()

    def test_duplicate_or_malformed_package_declarations_are_rejected(self):
        for entries in ([self.build['artifacts'][0]] * 2, [None], []):
            self.write(self.build_path, {**self.build, 'artifacts': entries})
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.verify()

    def test_stale_receipts_cannot_validate_a_new_build_even_with_identical_packages(self):
        self.write(self.build_path, {**self.build, 'created_at_utc': '2026-01-01T00:00:00+00:00'})
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.verify()

    def test_contract_mismatch_is_rejected(self):
        for field in contracts():
            changed = copy.deepcopy(self.receipts)
            changed[0][field] = '0' * 64
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                self.verify(changed)

    def test_failed_skipped_or_zero_unit_results_are_rejected(self):
        for field, value in [('status', 'FAIL'), ('tests_successful', False), ('tests_run', 0), ('tests_run', True)]:
            changed = copy.deepcopy(self.receipts)
            changed[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.verify(changed)

    def test_uninstalled_or_mismatched_installed_code_cannot_pass(self):
        for field, value in [('installed_package', False), ('package_integrity', False),
                             ('software_manifest_sha256', 'd' * 64), ('reference_conversion_exact', False),
                             ('artifact_sha256', 'd' * 64), ('sidecars_validated', 5),
                             ('installation_method', 'conda-package'), ('installation_record_sha256', None)]:
            changed = copy.deepcopy(self.receipts)
            changed[1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(changed)

    def test_incomplete_untrusted_or_changed_scientific_scope_is_rejected(self):
        for field, value in [('trusted_runner', False), ('arrays_exact', False), ('tables_exact', False),
                             ('scientific_equivalence', 'NOT_ESTABLISHED'), ('encoder_run', True),
                             ('device', 'cuda'), ('threshold_mode', 'other'), ('sidecars_validated', 5)]:
            changed = copy.deepcopy(self.receipts)
            changed[-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(changed)

    def test_frozen_platform_must_match_one_actual_supported_runner(self):
        for value in ('other', 'linux-64'):
            changed = copy.deepcopy(self.receipts)
            changed[-1]['platform'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.verify(changed)

    def test_unknown_schema_receipt_kind_and_boolean_identities_are_rejected(self):
        for field, value in [('schema', True), ('schema', 2), ('kind', 'unknown'),
                             ('run_attempt', True), ('source_clean', 1)]:
            changed = copy.deepcopy(self.receipts)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(changed)

    def test_gate_requires_complete_source_sha_and_real_workflow_identity(self):
        for commit, run_id, attempt in [('short', '123', 1), (COMMIT, '0', 1),
                                        (COMMIT, 'local', 1), (COMMIT, '123', True)]:
            with self.subTest(commit=commit, run_id=run_id, attempt=attempt), self.assertRaises(ValueError):
                verify_candidate(self.build_path, [], self.artifacts, source_commit=commit,
                                 run_id=run_id, run_attempt=attempt, repository=REPOSITORY)


class ControllerTests(unittest.TestCase):
    def test_conda_installation_requires_its_actual_artifact_record(self):
        with tempfile.TemporaryDirectory(prefix='installation-') as directory:
            prefix = Path(directory)
            artifact = prefix / 'incfinder.conda'
            artifact.write_bytes(b'package fixture')
            records = prefix / 'conda-meta'
            records.mkdir()
            with patch.object(sys, 'prefix', directory):
                with self.assertRaisesRegex(ValueError, 'actual installed Conda'):
                    ci.installation_origin('conda', artifact)
                record = {'name': 'incfinder', 'version': '0.5.0rc2', 'fn': artifact.name, 'sha256': digest(artifact)}
                ci.save(records / 'incfinder-0.5.0rc2-py_0.json', record)
                self.assertEqual(ci.installation_origin('conda', artifact)['installation_method'], 'conda-package')
                ci.save(records / 'incfinder-0.5.0rc2-py_0.json', {**record, 'sha256': '0' * 64})
                with self.assertRaisesRegex(ValueError, 'does not identify'):
                    ci.installation_origin('conda', artifact)

    def test_wheel_origin_requires_the_original_local_wheel_hash(self):
        with tempfile.TemporaryDirectory(prefix='installation-') as directory:
            artifact = Path(directory) / 'candidate.whl'
            artifact.write_bytes(b'wheel fixture')
            origin = {'url': artifact.as_uri(), 'archive_info': {'hashes': {'sha256': digest(artifact)}}}
            with patch.object(sys, 'prefix', directory), patch.object(ci.importlib.metadata, 'distribution') as distribution:
                distribution.return_value.read_text.return_value = json.dumps(origin)
                self.assertEqual(ci.installation_origin('wheel', artifact)['installation_method'], 'pip-wheel')
                origin['archive_info']['hashes']['sha256'] = '0' * 64
                distribution.return_value.read_text.return_value = json.dumps(origin)
                with self.assertRaisesRegex(ValueError, 'does not identify'):
                    ci.installation_origin('wheel', artifact)

    def test_wheel_checks_reject_a_conflicting_conda_record(self):
        with tempfile.TemporaryDirectory(prefix='installation-') as directory:
            records = Path(directory) / 'conda-meta'
            records.mkdir()
            ci.save(records / 'incfinder-old.json', {})
            with patch.object(sys, 'prefix', directory), self.assertRaisesRegex(ValueError, 'Conflicting Conda'):
                ci.installation_origin('wheel', Path(directory) / 'candidate.whl')

    def test_trusted_dispatch_requires_owner_private_runner_and_approved_branch(self):
        environment = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'self-hosted',
            'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_ACTOR': 'chrisjack007',
            'GITHUB_REPOSITORY_OWNER': 'chrisjack007', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main'}
        with patch.dict(os.environ, environment):
            self.assertTrue(ci.trusted_execution(COMMIT))
        for field, value in [('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_ACTOR', 'other'),
                             ('GITHUB_REF', 'refs/heads/other'), ('RUNNER_ENVIRONMENT', 'github-hosted'),
                             ('GITHUB_REPOSITORY', 'someone/fork')]:
            with self.subTest(field=field), patch.dict(os.environ, {**environment, field: value}):
                self.assertFalse(ci.trusted_execution(COMMIT))

    def test_first_branch_push_requires_exact_preapproved_commit(self):
        environment = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'self-hosted',
            'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_ACTOR': 'chrisjack007',
            'GITHUB_REPOSITORY_OWNER': 'chrisjack007', 'GITHUB_EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/codex/if003-ci-validation', 'INCFINDER_CI_APPROVED_SHA': COMMIT}
        with patch.dict(os.environ, environment):
            self.assertTrue(ci.trusted_execution(COMMIT))
            self.assertFalse(ci.trusted_execution('d' * 40))
        with patch.dict(os.environ, {**environment, 'INCFINDER_CI_APPROVED_SHA': ''}):
            self.assertFalse(ci.trusted_execution(COMMIT))

    def test_outputs_refuse_checkout_existing_paths_and_links(self):
        with self.assertRaisesRegex(ValueError, 'outside the checkout'):
            ci.output_directory(ROOT / 'bad-ci-output')
        with tempfile.TemporaryDirectory(prefix='controller-') as folder:
            with self.assertRaises(FileExistsError):
                ci.output_directory(folder)
            with patch.object(Path, 'is_symlink', return_value=True), self.assertRaisesRegex(ValueError, 'links'):
                ci.output_directory(Path(folder) / 'new')

    def test_local_context_is_explicit_and_does_not_use_incidental_workflow_ids(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}), \
                patch.object(ci, 'git', side_effect=[COMMIT.encode(), b' M source.py\0']):
            context = ci.context(COMMIT, 'unit')
        self.assertEqual(context['execution_context'], 'local')
        self.assertEqual(context['run_id'], '0')
        self.assertEqual(context['run_attempt'], 1)
        self.assertFalse(context['source_clean'])

    def test_controller_rejects_a_source_sha_other_than_the_checkout(self):
        with patch.object(ci, 'git', return_value=COMMIT.encode()), self.assertRaisesRegex(ValueError, 'does not match'):
            ci.context('0' * 40, 'unit')

    def test_installed_checks_refuse_source_shadowing(self):
        with patch.object(Path, 'cwd', return_value=ROOT), self.assertRaisesRegex(ValueError, 'outside the checkout'):
            ci.installed_context(None, 'installed')

    def test_failed_gate_overwrites_a_stale_positive_decision(self):
        with tempfile.TemporaryDirectory(prefix='controller-') as directory:
            output = Path(directory) / 'gate'
            output.mkdir()
            ci.save(output / 'decision.json', {'eligible': True, 'status': 'PASS'})
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '0'}):
                result = ci.main(['gate', '--source-sha', COMMIT, '--output', str(output),
                    '--build-receipt', str(Path(directory) / 'missing.json'),
                    '--packages', directory, '--evidence', directory])
            self.assertEqual(result, 1)
            decision = json.loads((output / 'decision.json').read_text(encoding='utf-8'))
            self.assertFalse(decision['eligible'])
            self.assertFalse(decision['release_published'])
            self.assertEqual(decision['status'], 'BLOCKED')
