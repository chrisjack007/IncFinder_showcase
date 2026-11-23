"""A clean source checkout builds without downloading external model weights."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'release_builder', Path(__file__).resolve().parents[1] / 'packaging/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        # This synthetic checkout represents the legacy integrated-source format.
        self.enterContext(patch.object(builder, 'VERSION', '0.4.0'))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'vendor').mkdir()
        (self.root / 'vendor/model.py').write_bytes(b'# frozen vendor\n')
        self.assets = [
            {'path': 'models/rf/stage2.joblib', 'size_bytes': 22863415225, 'sha256': 'a' * 64},
            {'path': 'models/ntv3/model.safetensors', 'size_bytes': 2607356620, 'sha256': 'b' * 64},
            {'path': 'vendor/model.py', 'size_bytes': 16,
             'sha256': hashlib.sha256(b'# frozen vendor\n').hexdigest()},
        ]
        (self.root / 'config/assets.json').write_text(json.dumps({'files': self.assets}))

    def write_frozen_asset(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entry = next(item for item in self.assets if item['path'] == name)
        entry.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        (self.root / 'config/assets.json').write_text(json.dumps({'files': self.assets}))
        return path

    def test_full_checkpoint_uses_its_own_fixed_revision(self):
        from ntv3_tsrf.encoder_source import CHECKPOINT_SOURCE, CODE_SOURCE, UPSTREAM_PATHS

        path = self.root/'config/model_delivery.json'
        document = {'schema': 2, 'ntv3_code': CODE_SOURCE, 'ntv3': dict(CHECKPOINT_SOURCE, revision='0' * 40)}
        path.write_text(json.dumps(document))
        with patch.object(builder, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'checkpoint source'):
                builder.excluded_sources()
            document['ntv3'] = CHECKPOINT_SOURCE
            path.write_text(json.dumps(document))
            self.assertEqual(builder.excluded_sources(), UPSTREAM_PATHS)

    def test_new_release_requires_upstream_code_identity_before_packaging(self):
        with patch.object(builder, 'ROOT', self.root), patch.object(builder, 'VERSION', '0.5.0rc1'):
            with self.assertRaisesRegex(ValueError, 'require the pinned NTv3 code source'):
                builder.refresh_manifest()

    def test_pinned_encoder_code_is_external_in_portable_and_source_manifests(self):
        from ntv3_tsrf.encoder_source import (CHECKPOINT_PATHS, CHECKPOINT_SOURCE, CODE_NAMES, CODE_SOURCE,
                                             DELIVERY, UPSTREAM_PATHS, external_manifest_paths)

        (self.root / 'config/model_delivery.json').write_text(
            json.dumps({'schema': 2, 'ntv3_code': CODE_SOURCE, 'ntv3': CHECKPOINT_SOURCE}))
        for relative in sorted(CHECKPOINT_PATHS - {'models/ntv3/model.safetensors'}):
            data = relative.encode()
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.assets.append({'path': relative, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        notice = self.root / 'models/ntv3/MODEL-LICENSE.md'
        notice.write_bytes(b'Licensing notice')
        for name in CODE_NAMES:
            data = ('# ' + name).encode()
            path = self.root / 'vendor/ntv3' / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            self.assets.append({'path': 'vendor/ntv3/' + name, 'size_bytes': len(data),
                                'sha256': hashlib.sha256(data).hexdigest()})
        (self.root / 'config/assets.json').write_text(json.dumps({'files': self.assets}))
        with patch.object(builder, 'ROOT', self.root):
            builder.refresh_manifest()
            included = {p.relative_to(self.root).as_posix() for p in builder.members()}
            self.assertFalse(included & UPSTREAM_PATHS)
            self.assertIn('models/ntv3/MODEL-LICENSE.md', included)
            manifest = json.loads((self.root/'package_manifest.json').read_text())
            self.assertEqual(external_manifest_paths(manifest), UPSTREAM_PATHS)
            self.assertEqual(sum(row.get('delivery') == DELIVERY for row in manifest['files']), 10)
            (self.root/'vendor/ntv3/__init__.py').write_bytes(b'# changed')
            with self.assertRaisesRegex(RuntimeError, 'differs from its signed identity'):
                builder.refresh_manifest()

    def test_clean_checkout_retains_external_weight_identity(self):
        with patch.object(builder, 'ROOT', self.root):
            builder.refresh_manifest()
        manifest = json.loads((self.root / 'package_manifest.json').read_text())
        rows = {entry['path']: entry for entry in manifest['files']}
        self.assertEqual(rows['models/rf/stage2.joblib'], self.assets[0])
        self.assertEqual(rows['models/ntv3/model.safetensors'], self.assets[1])
        self.assertEqual(rows['vendor/model.py']['sha256'],
                         hashlib.sha256((self.root / 'vendor/model.py').read_bytes()).hexdigest())
        self.assertFalse((self.root / 'models').exists())

    def test_missing_vendored_code_cannot_be_treated_as_external_weight(self):
        manifest = self.root / 'package_manifest.json'
        manifest.write_bytes(b'previous manifest\n')
        (self.root / 'vendor/model.py').unlink()
        with patch.object(builder, 'ROOT', self.root), self.assertRaisesRegex(RuntimeError, 'Missing immutable asset'):
            builder.refresh_manifest()
        self.assertEqual(manifest.read_bytes(), b'previous manifest\n')

    def test_same_length_tampering_preserves_previous_manifest(self):
        manifest = self.root / 'package_manifest.json'
        for name in ('vendor/model.py', 'models/rf/stage2.joblib', 'models/ntv3/model.safetensors'):
            with self.subTest(asset=name):
                path = self.write_frozen_asset(name, b'# frozen vendor\n')
                manifest.write_bytes(b'previous manifest\n')
                path.write_bytes(b'# Frozen vendor\n')
                with patch.object(builder, 'ROOT', self.root), self.assertRaisesRegex(
                        RuntimeError, 'Frozen asset SHA-256 changed') as error:
                    builder.refresh_manifest()
                self.assertIn(name, str(error.exception))
                self.assertEqual(manifest.read_bytes(), b'previous manifest\n')
                path.write_bytes(b'# frozen vendor\n')

    def test_size_changes_preserve_previous_manifest(self):
        manifest = self.root / 'package_manifest.json'
        for name in ('vendor/model.py', 'models/rf/stage2.joblib', 'models/ntv3/model.safetensors'):
            with self.subTest(asset=name):
                path = self.write_frozen_asset(name, b'# frozen vendor\n')
                manifest.write_bytes(b'previous manifest\n')
                path.write_bytes(b'# frozen vendor\nX')
                with patch.object(builder, 'ROOT', self.root), self.assertRaisesRegex(
                        RuntimeError, 'Frozen asset size changed') as error:
                    builder.refresh_manifest()
                self.assertIn(name, str(error.exception))
                self.assertEqual(manifest.read_bytes(), b'previous manifest\n')
                path.write_bytes(b'# frozen vendor\n')

    def test_present_external_weights_record_verified_digest(self):
        for name in ('models/rf/stage2.joblib', 'models/ntv3/model.safetensors'):
            self.write_frozen_asset(name, b'frozen model bytes\n')
        with patch.object(builder, 'ROOT', self.root):
            builder.refresh_manifest()
        rows = {entry['path']: entry for entry in
                json.loads((self.root / 'package_manifest.json').read_text())['files']}
        for expected in self.assets:
            self.assertEqual(rows[expected['path']], expected)

    def test_non_frozen_file_digest_tracks_actual_content(self):
        path = self.root / 'notes.txt'
        observed = []
        for content in (b'first\n', b'other\n'):
            path.write_bytes(content)
            with patch.object(builder, 'ROOT', self.root):
                builder.refresh_manifest()
            rows = {entry['path']: entry for entry in
                    json.loads((self.root / 'package_manifest.json').read_text())['files']}
            observed.append(rows['notes.txt']['sha256'])
            self.assertEqual(rows['notes.txt']['sha256'], hashlib.sha256(content).hexdigest())
        self.assertNotEqual(*observed)

    def test_all_build_modes_reject_tampering_before_creating_dist(self):
        (self.root / 'vendor/model.py').write_bytes(b'# Frozen vendor\n')
        manifest = self.root / 'package_manifest.json'
        manifest.write_bytes(b'previous manifest\n')
        for arguments in ([], ['--wheel-only'], ['--conda-only'], ['--archives-only'],
                          ['--refresh-manifest']):
            with self.subTest(arguments=arguments):
                with patch.object(builder, 'ROOT', self.root), \
                        patch.object(builder.sys, 'argv', ['build_release.py', *arguments]), \
                        patch.object(builder.subprocess, 'run') as backend:
                    with self.assertRaisesRegex(RuntimeError, 'Frozen asset SHA-256 changed'):
                        builder.main()
                    backend.assert_not_called()
                self.assertFalse((self.root / 'dist').exists())
                self.assertEqual(manifest.read_bytes(), b'previous manifest\n')

    def test_invalid_asset_preserves_existing_distribution(self):
        dist = self.root / 'dist'
        dist.mkdir()
        files = {'existing.whl': b'existing distribution\n', 'SHA256SUMS': b'previous checksums\n'}
        for name, content in files.items():
            (dist / name).write_bytes(content)
        (self.root / 'vendor/model.py').write_bytes(b'# Frozen vendor\n')
        with patch.object(builder, 'ROOT', self.root), \
                patch.object(builder.sys, 'argv', ['build_release.py', '--archives-only']), \
                patch.object(builder.subprocess, 'run') as backend:
            with self.assertRaisesRegex(RuntimeError, 'Frozen asset SHA-256 changed'):
                builder.main()
            backend.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in dist.iterdir()}, files)

    def test_refresh_only_does_not_start_build_backend(self):
        with patch.object(builder, 'ROOT', self.root), \
                patch.object(builder.sys, 'argv', ['build_release.py', '--refresh-manifest']), \
                patch.object(builder.subprocess, 'run') as backend:
            builder.main()
            backend.assert_not_called()
        self.assertTrue((self.root / 'package_manifest.json').is_file())
        self.assertFalse((self.root / 'dist').exists())

    def test_release_members_omit_internal_evidence(self):
        (self.root / 'docs').mkdir()
        (self.root / 'archive').mkdir()
        (self.root / '.github').mkdir()
        for name in ('AGENTS.md', 'projectprocess.md', 'docs/validation.json', 'docs/validation.md',
                     'archive/receipt.json', '.env', '.github/workflow.yml'):
            (self.root / name).write_text('example')
        with patch.object(builder, 'ROOT', self.root):
            names = {path.relative_to(self.root).as_posix() for path in builder.members()}
        self.assertIn('docs/validation.md', names)
        self.assertFalse({'AGENTS.md', 'projectprocess.md', 'docs/validation.json',
                          'archive/receipt.json', '.env', '.github/workflow.yml'} & names)
