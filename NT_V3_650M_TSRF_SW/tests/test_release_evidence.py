"""Release evidence checks using inert files and explicitly synthetic CI receipts."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import warnings
import zipfile

import test_release_gate as fixtures
import release_evidence as evidence
import release_references as references
import release_sbom as sbom
from release_gate import digest, verify_candidate


def _archives(folder, contents):
    for name in evidence.ARCHIVES:
        path = folder / name
        if name.endswith('.zip'):
            with zipfile.ZipFile(path, 'w') as archive:
                for member, data in contents.items():
                    archive.writestr(evidence.ARCHIVE_PREFIX + member, data)
        else:
            with tarfile.open(path, 'w:gz') as archive:
                for member, data in contents.items():
                    entry = tarfile.TarInfo(evidence.ARCHIVE_PREFIX + member)
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        # The inert archives in this fixture predate the separate delivery catalog.
        self.enterContext(patch.object(evidence, 'VERSION', '0.4.0'))
        import release_gate
        legacy_contracts = {k: v for k, v in release_gate.contracts().items() if k != 'model_lineage_sha256'}
        self.enterContext(patch.object(fixtures, 'contracts', return_value=legacy_contracts))
        self.enterContext(patch.object(release_gate, 'contracts', return_value=legacy_contracts))
        fixture = fixtures.CandidateGateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.base = fixture.root
        self.root = self.base / 'bundle'
        self.root.mkdir()
        self.identity = dict(release='test-candidate', repository=fixtures.REPOSITORY,
                             source_commit=fixtures.COMMIT, run_id='123', run_attempt=1)
        self.trust = self.base / 'allowed_signers'
        self.trust.write_text('explicit test trust fixture', encoding='utf-8')
        self.tool = self.base / 'pinned-validator'
        self.tool.touch()
        manifest = self.root / 'source-manifest.json'
        source = b'source identity fixture'
        source_hash = hashlib.sha256(source).hexdigest()
        manifest.write_bytes(evidence.json_bytes(dict(schema=1, files=[
            dict(path='vendor/sample.py', size_bytes=len(source), sha256=source_hash),
        ])))
        (self.root / 'packages').mkdir()
        self.packages = []
        for package in fixture.artifacts:
            target = self.root / 'packages' / package.name
            target.write_bytes(package.read_bytes())
            self.packages.append(target)
        self.packages_by_kind = dict(zip(('wheel', 'conda'), self.packages))
        self.archive_dir = self.root / 'archives'
        self.archive_dir.mkdir()
        self.contents = {
            'package_manifest.json': manifest.read_bytes(), 'vendor/sample.py': source,
            'dist/wheels/candidate.whl': self.packages[0].read_bytes(),
            'dist/channel/noarch/candidate.conda': self.packages[1].read_bytes(),
        }
        _archives(self.archive_dir, self.contents)
        build = fixture.build
        build.update(
            source_manifest_sha256=digest(manifest),
            packaged_source_manifest_sha256=digest(manifest),
            archives=evidence.archive_records(list(self.archive_dir.iterdir()), manifest, self.packages_by_kind),
            lock_sha256={target: digest(sbom.lock_file(target)) for target in sbom.TOOL_SHA256},
            build_environment_sha256=digest(evidence.ROOT / 'environments/ci-build.yml'),
            build_tools={name: 'synthetic' for name in ('pip', 'setuptools', 'wheel', 'conda-build', 'conda-index')},
        )
        for item, package in zip(build['artifacts'], self.packages):
            item['size_bytes'] = package.stat().st_size
        self.build = self.root / 'build.json'
        self.build.write_bytes(evidence.json_bytes(build))
        self.receipts = []
        self.boms = []
        for i, entry in enumerate(fixture.receipts):
            dest = self.root / 'evidence' / ('fixture-' + str(i)) / 'receipt.json'
            dest.parent.mkdir(parents=True)
            entry['source_manifest_sha256'] = digest(manifest)
            if entry['kind'] != 'unit':
                entry['build_receipt_sha256'] = digest(self.build)
            if entry['kind'] == 'installed':
                bom_path = dest.with_name('sbom.cdx.json')
                bom_path.write_bytes(evidence.json_bytes(sbom.expected_bom(entry, manifest)))
                entry['sbom_sha256'] = digest(bom_path)
                self.boms.append(bom_path)
            dest.write_bytes(evidence.json_bytes(entry))
            self.receipts.append(dest)
        gate_identity = {k: v for k, v in self.identity.items() if k != 'release'}
        decision = verify_candidate(self.build, self.receipts, self.packages, **gate_identity)
        (self.root / 'decision.json').write_bytes(evidence.json_bytes(decision))
        snapshot = dict(references.v1_reference(), status='PASS',
                        checked_at_utc=datetime.now(timezone.utc).isoformat())
        (self.root / 'v1-assets.json').write_bytes(evidence.json_bytes(snapshot))
        self.validate = self.enterContext(patch.object(evidence, 'validate_boms'))

    def candidate(self):
        return evidence.candidate_manifest(self.root, self.identity, self.tool)

    def test_upstream_policy_and_all_four_sources_bound_to_wheel(self):
        from ntv3_tsrf.encoder_source import CODE_NAMES, CODE_SOURCE, DELIVERY

        policy = evidence.json_bytes({'schema': 2, 'ntv3_code': CODE_SOURCE})
        docs = {name: ('# ' + name).encode() for name in CODE_NAMES}
        def row(name, data):
            return {'path': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        rows = [row('config/model_delivery.json', policy)]
        rows.extend(dict(row('vendor/ntv3/' + name, data), delivery=DELIVERY) for name, data in docs.items())
        source = self.base/'upstream-source.json'
        source.write_bytes(evidence.json_bytes({'files': rows, 'schema': 1}))
        wheel = self.base/'upstream.whl'
        def build(document, extra=False):
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('ntv3_tsrf/_resources/config/model_delivery.json', document)
                if extra:
                    archive.writestr('other/renamed.file', docs['__init__.py'])
        build(policy)
        evidence._wheel_encoder_source(wheel, source)
        self.assertFalse(sbom.vendor_inventory(source))
        build(policy, extra=True)
        with self.assertRaisesRegex(ValueError, 'Wheel embeds'):
            evidence._wheel_encoder_source(wheel, source)
        build(evidence.json_bytes({'schema': 2}))
        with self.assertRaisesRegex(ValueError, 'catalog differs'):
            evidence._wheel_encoder_source(wheel, source)
        build(policy)
        rows[-1].pop('delivery')
        source.write_bytes(evidence.json_bytes({'files': rows, 'schema': 1}))
        with self.assertRaisesRegex(ValueError, 'external NTv3 code inventory'):
            evidence._wheel_encoder_source(wheel, source)

    def test_current_complete_candidate_binds_all_formats_sboms_and_receipts(self):
        data = self.candidate()
        files = {row['path'] for row in data['files']}
        self.assertEqual(len([x for x in files if x.startswith(('packages/', 'archives/'))]), 4)
        self.assertEqual(len([x for x in files if x.endswith('/receipt.json')]), 10)
        self.assertEqual(len([x for x in files if x.endswith('/sbom.cdx.json')]), 4)
        self.validate.assert_called_once()
        self.assertEqual(data['sbom_validation']['spec_version'], '1.6')

    def test_swapped_archive_package_rejected_even_when_source_members_match(self):
        _archives(self.archive_dir, {**self.contents, 'dist/wheels/candidate.whl': b'other package'})
        with self.assertRaisesRegex(ValueError, 'embedded wheel'):
            self.candidate()

    def test_duplicate_archive_member_and_changed_source_are_rejected(self):
        path = self.archive_dir / next(n for n in evidence.ARCHIVES if n.endswith('.zip'))
        with warnings.catch_warnings(), zipfile.ZipFile(path, 'a') as archive:
            warnings.simplefilter('ignore', UserWarning)
            archive.writestr(evidence.ARCHIVE_PREFIX + 'vendor/sample.py', b'other')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.candidate()
        _archives(self.archive_dir, {**self.contents, 'vendor/sample.py': b'changed'})
        with self.assertRaisesRegex(ValueError, 'source member'):
            self.candidate()

    def test_additional_release_file_and_unsafe_names_are_rejected(self):
        (self.root / 'unexpected.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unexpected or missing release files'):
            self.candidate()
        for name in ('../other', '/absolute', 'C:/absolute', 'dir//file', 'dir/con.txt'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                evidence._relative_name(name)

    def test_changed_sbom_rejected_even_if_its_receipt_digest_was_updated(self):
        path = self.boms[0]
        data = evidence.json_object(path)
        data['components'][0]['version'] = 'changed'
        path.write_bytes(evidence.json_bytes(data))
        receipt = path.with_name('receipt.json')
        receipt.write_bytes(evidence.json_bytes(dict(evidence.json_object(receipt), sbom_sha256=digest(path))))
        with self.assertRaisesRegex(ValueError, 'does not match its locked install'):
            self.candidate()

    def test_stale_attempt_and_foreign_source_are_rejected(self):
        receipt = self.receipts[-1]
        original = evidence.json_object(receipt)
        for key, value in (('run_attempt', 4), ('source_commit', 'f' * 40)):
            receipt.write_bytes(evidence.json_bytes(dict(original, **{key: value})))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'identity mismatch'):
                self.candidate()

    def test_verified_signature_checks_files_and_expected_attempt(self):
        manifest = evidence.json_bytes(self.candidate())
        with patch.object(evidence, 'verify_signature', return_value=manifest):
            self.assertEqual(evidence.verify_bundle(self.root, self.trust, self.identity, self.tool)['schema'], 1)
            with self.assertRaisesRegex(ValueError, 'requested release'):
                evidence.verify_bundle(self.root, self.trust, dict(self.identity, run_attempt=2), self.tool)
            self.packages[0].write_bytes(b'altered')
            with self.assertRaisesRegex(ValueError, 'inventory, size or SHA'):
                evidence.verify_bundle(self.root, self.trust, self.identity, self.tool)

    def test_bad_signature_is_checked_before_decoding_manifest(self):
        with patch.object(evidence, 'verify_signature', side_effect=ValueError('Rejected signature')) as verify, \
                patch.object(evidence, '_json_object') as parse:
            with self.assertRaisesRegex(ValueError, 'Rejected signature'):
                evidence.verify_bundle(self.root, self.trust, self.identity, self.tool)
            verify.assert_called_once()
            parse.assert_not_called()

    def test_signature_uses_raw_manifest_bytes_and_disallows_packaged_trust(self):
        raw = b'{"fixture":true}\r\n'
        (self.root / evidence.MANIFEST).write_bytes(raw)
        (self.root / evidence.SIGNATURE).write_bytes(b'synthetic signature')
        with patch.object(evidence.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(evidence.verify_signature(self.root, self.trust), raw)
            self.assertEqual(run.call_args.kwargs['input'], raw)
            self.assertIn(evidence.SIGNER, run.call_args.args[0])
            packaged = self.root / 'allowed_signers'
            packaged.touch()
            with self.assertRaisesRegex(ValueError, 'external'):
                evidence.verify_signature(self.root, packaged)
            run.assert_called_once()

    def test_duplicate_manifest_keys_refused(self):
        with patch.object(evidence, 'verify_signature', return_value=b'{"schema":1,"schema":1}'):
            with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
                evidence.verify_bundle(self.root, self.trust, self.identity, self.tool)

    def test_validator_rejects_wrong_binary_before_execution(self):
        with patch.object(sbom.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'pinned CycloneDX'):
            sbom.validate_boms(self.tool, self.boms)
        run.assert_not_called()


class ReleaseReferenceTests(unittest.TestCase):
    def setUp(self):
        self.expected = references.v1_reference()
        self.release = dict(id=self.expected['release_id'], tag_name=self.expected['tag'],
                            target_commitish=self.expected['commit'], assets=[
            dict(id=a['asset_id'], name=a['name'], size=a['size_bytes'], digest='sha256:' + a['sha256'],
                 browser_download_url=a['download_url'], state='uploaded') for a in self.expected['assets']
        ])
        self.ref = dict(ref='refs/tags/' + self.expected['tag'],
                        object=dict(sha=self.expected['tag_object'], type='tag'))
        self.tag = dict(sha=self.expected['tag_object'],
                        object=dict(sha=self.expected['commit'], type='commit'))

    def test_26_v1_release_assets_and_annotated_tag_are_pinned(self):
        received = references.check_v1_release(self.release, self.ref, self.tag)
        self.assertEqual(len(received['assets']), 26)
        self.assertEqual(received['status'], 'PASS')
        references.check_v1_snapshot(received)

    def test_missing_v1_asset_diagnoses_name_tag_and_expected_id(self):
        gone = self.release['assets'].pop()
        with self.assertRaisesRegex(ValueError, r'Missing V1 release asset: .*' + str(gone['id'])):
            references.check_v1_release(self.release, self.ref, self.tag)

    def test_v1_metadata_mismatch_and_retag_are_rejected(self):
        changed = copy.deepcopy(self.release)
        changed['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'checksum or URL mismatch'):
            references.check_v1_release(changed, self.ref, self.tag)
        bad_ref = dict(self.ref, object=dict(sha='0' * 40, type='tag'))
        with self.assertRaisesRegex(ValueError, 'Git tag identity mismatch'):
            references.check_v1_release(self.release, bad_ref, self.tag)

    def test_v1_http_404_without_release_readback_is_an_access_diagnostic(self):
        with patch.object(references, 'build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(None, 404, 'not found', None, None)
            with self.assertRaisesRegex(ValueError, 'access could not be confirmed.*HTTP 404'):
                references.live_v1_reference('synthetic-test-token')
        with self.assertRaisesRegex(ValueError, 'read credential'):
            references.live_v1_reference('')
