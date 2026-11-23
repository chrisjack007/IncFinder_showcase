"""Exercise frozen volume validation, interrupted delivery and immutable publication."""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ntv3_tsrf import cli, common, model_download, model_install
from ntv3_tsrf.run_record import _writer_lease


def sha(data):
    return hashlib.sha256(data).hexdigest()


def entry(path, data):
    return {'path':path, 'size_bytes':len(data), 'sha256':sha(data)}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root/'source'
        for name in ('vendor/ntv3','config'):
            (self.source/name).mkdir(parents=True)
        self.parts = self.root/'parts'
        self.parts.mkdir()
        self.donor = self.root/'ntv3'
        self.donor.mkdir()
        self.target = self.root/'model output'
        self.payload = {'rf/stage1.joblib':b'RF one','rf/stage2.joblib':b'aaaabbbbcc',
                        'ntv3/model.safetensors':b'encoder','ntv3/MODEL-LICENSE.md':b'terms'}
        volumes = {'stage1.joblib':b'RF one','stage2.joblib.part-001':b'aaaa',
                   'stage2.joblib.part-002':b'bbbb','stage2.joblib.part-003':b'cc'}
        for name, content in volumes.items():
            (self.parts/name).write_bytes(content)
        for name, content in self.payload.items():
            if name.startswith('ntv3/'):
                (self.donor/Path(name).name).write_bytes(content)
        scientific = ['config/thresholds.tsv',*(f'vendor/ntv3/{prefix}_ntv3{suffix}.py'
                       for prefix,suffix in (('modeling','_pretrained'),('configuration','_pretrained'),('tokenization','')))]
        assets = [entry('models/'+name,value) for name,value in self.payload.items()]
        for name in scientific:
            (self.source/name).write_bytes(b'frozen')
            assets.append(entry(name,b'frozen'))
        self.freeze = {'files':assets}
        self.sources = {'schema':1,
                        'rf':{'repository':'chrisjack007/IncFinder_engineer','tag':'NT_V3_650M_TSRF_SW_V1',
                              'release_id':55,'part_size_bytes':4,
                              'parts':[{'name':name,'asset_id':i,'size_bytes':len(data),'sha256':sha(data)}
                                       for i,(name,data) in enumerate(volumes.items(),1)]},
                        'ntv3':{'repository':'InstaDeepAI/NTv3_650M_pre','revision':'a'*40}}
        common.write_json(self.source/'config/assets.json',self.freeze)
        self.write_sources()
        for module in (model_install,common):
            patched = patch.object(module,'ROOT',self.source)
            patched.start()
            self.addCleanup(patched.stop)

    def write_sources(self):
        filename = 'model_delivery.json' if self.sources['schema'] == 2 else 'model_sources.json'
        common.write_json(self.source/'config'/filename, self.sources)

    def install(self, **options):
        return model_install.install_models(self.target,self.parts,self.donor,**options)

    def workspace(self):
        key = hashlib.sha256(os.fsencode(os.path.normcase(self.target.name))).hexdigest()[:24]
        return self.target.parent/('.incfinder-model-'+key)

    def seed(self, name, content):
        file = self.target/name
        file.parent.mkdir(parents=True,exist_ok=True)
        file.write_bytes(content)
        return file

    def assert_payload(self):
        for name, value in self.payload.items():
            self.assertEqual((self.target/name).read_bytes(),value)
            self.assertEqual((self.target/name).stat().st_nlink,1)

    def candidate_catalog(self):
        from ntv3_tsrf.model_lineage import MODEL_ROLES

        assets = {row['path']: {'size_bytes': row['size_bytes'], 'sha256': row['sha256']}
                  for row in self.freeze['files'] if row['path'] in MODEL_ROLES}
        index = sha(b'test-only evidence index')
        common.write_json(self.source / 'config/model_lineage.json',
                          {'schema': 1, 'candidate_id': 'installation-test', 'index_sha256': index, 'assets': assets})
        parts = []
        for layer, contents in (('stage1.joblib', [b'RF ', b'one']), ('stage2.joblib', [b'aaaa', b'bbbb', b'cc'])):
            for number, content in enumerate(contents, 1):
                name = f'{layer}.part-{number:03d}'
                (self.parts / name).write_bytes(content)
                parts.append({'name': name, 'size_bytes': len(content), 'sha256': sha(content)})
        self.sources.update(schema=2, rf={'index_sha256': index, 'sources': {name: {'kind': 'archive'} for name in ('stage1.joblib', 'stage2.joblib')},
                                         'parts': parts})
        self.write_sources()
        return self.sources['rf']

    def upstream_code_catalog(self):
        from ntv3_tsrf.encoder_source import CODE_SOURCE

        self.candidate_catalog()
        self.sources['ntv3_code'] = CODE_SOURCE.copy()
        self.write_sources()
        modules = {'__init__.py': b'"""Encoder fixture."""\n',
                   'configuration_ntv3_pretrained.py': b'class Ntv3PreTrainedConfig: pass\n',
                   'modeling_ntv3_pretrained.py': b'from .configuration_ntv3_pretrained import Ntv3PreTrainedConfig\nclass NTv3PreTrained: pass\n',
                   'tokenization_ntv3.py': b'class NTv3Tokenizer: pass\n'}
        self.freeze['files'] = [row for row in self.freeze['files'] if not row['path'].startswith('vendor/')]
        for name, data in modules.items():
            (self.donor / name).write_bytes(data)
            self.freeze['files'].append(entry('vendor/ntv3/' + name, data))
        common.write_json(self.source / 'config/assets.json', self.freeze)
        return modules

    def test_upstream_code_local_install_and_private_offline_import(self):
        from ntv3_tsrf.encoder_source import CODE_SOURCE, encoder_classes

        modules = self.upstream_code_catalog()
        receipt = self.install()
        self.assertEqual(receipt['ntv3_code']['revision'], CODE_SOURCE['revision'])
        self.assertEqual({row['path'] for row in receipt['ntv3_code']['files']}, {'vendor/ntv3/' + name for name in modules})
        self.assertEqual(model_install.verify_models(self.target)['ntv3_code'], receipt['ntv3_code'])
        with encoder_classes(self.source, self.target) as classes:
            self.assertEqual([cls.__name__ for cls in classes], ['Ntv3PreTrainedConfig', 'NTv3PreTrained', 'NTv3Tokenizer'])
            self.assertTrue(all(cls.__module__.startswith('_incfinder_encoder_') for cls in classes))
        path = self.target/'ntv3/__init__.py'
        path.write_bytes(b' ' * path.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'NTv3 source SHA-256'):
            with encoder_classes(self.source, self.target):
                self.fail('encoders cannot load with an incorrect upstream source')

    def test_upstream_code_download_selects_each_pinned_repository(self):
        from ntv3_tsrf.encoder_source import CODE_SOURCE

        modules = self.upstream_code_catalog()
        calls = {}
        def fetch(cache, source, name):
            calls[name] = (source['repository'], source['revision'])
            return self.donor / name

        with patch.object(model_download, 'download_hub', side_effect=fetch):
            receipt = model_install.install_models(self.target, self.parts, download_ntv3=True)
        self.assertEqual(set(calls), set(modules) | {'model.safetensors', 'MODEL-LICENSE.md'})
        for name, observed in calls.items():
            selected = CODE_SOURCE if name in modules else self.sources['ntv3']
            self.assertEqual(observed, (selected['repository'], selected['revision']))
        self.assertEqual(len(receipt['ntv3_code']['files']), 4)

    def test_upstream_code_rejects_wrong_revision_and_missing_inventory(self):
        self.upstream_code_catalog()
        self.sources['ntv3_code']['revision'] = 'f' * 40
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'pinned NTv3 code source'):
            self.install()
        from ntv3_tsrf.encoder_source import CODE_SOURCE
        self.sources['ntv3_code'] = CODE_SOURCE.copy()
        self.write_sources()
        self.freeze['files'] = [row for row in self.freeze['files'] if not row['path'].endswith('/__init__.py')]
        common.write_json(self.source / 'config/assets.json', self.freeze)
        with self.assertRaisesRegex(ValueError, 'Incomplete NTv3'):
            self.install()
        self.assertFalse((self.target/'rf/stage1.joblib').exists())

    def test_candidate_catalog_preserves_the_historical_v1_reference(self):
        prior = self.source / 'config/model_sources.json'
        retained = prior.read_bytes()
        self.candidate_catalog()
        self.assertEqual(prior.read_bytes(), retained)
        delivery = self.source / 'config/model_delivery.json'
        self.assertEqual(model_install._sources()[-1], sha(delivery.read_bytes()))
        delivery.write_bytes(retained)
        with self.assertRaisesRegex(ValueError, 'layout'):
            self.install()

    def test_candidate_installs_two_split_layers_without_network(self):
        rf = self.candidate_catalog()
        with patch.object(model_download, '_credential', side_effect=AssertionError('unexpected authentication')):
            result = self.install(download_rf=True)
            verified = model_install.verify_models(self.target, self.parts)
        self.assert_payload()
        self.assertEqual(result['rf_lineage_index_sha256'], rf['index_sha256'])
        self.assertEqual(verified['rf_files_verified'], [row['name'] for row in rf['parts']])
        self.assertEqual(verified['rf_layer_sources'], rf['sources'])

    def test_internal_staging_can_exceed_legacy_windows_path_limit(self):
        self.candidate_catalog()
        padding = 'p' * max(1, 132 - len(str(self.root)) - 1)
        parent = self.root / padding
        parent.mkdir()
        self.target = parent / 'model output'
        source = common.sha256(self.source / 'config/model_delivery.json')
        pending = self.workspace() / source / 'ready/rf/stage1.joblib.building'
        self.assertGreaterEqual(len(str(pending)), 260)
        self.install()
        self.assert_payload()
        verified = model_install.verify_models(self.target, self.parts)
        self.assertEqual(verified['status'], 'PASS')

    def test_candidate_accepts_a_whole_local_layer_and_reuses_existing_other_layer(self):
        rf = self.candidate_catalog()
        name = 'stage2.joblib'
        whole = self.payload['rf/' + name]
        (self.parts / name).write_bytes(whole)
        rf['parts'][2:] = [{'name': name, 'sha256': sha(whole), 'size_bytes': len(whole)}]
        self.write_sources()
        self.seed('rf/stage1.joblib', self.payload['rf/stage1.joblib'])
        (self.parts / 'stage1.joblib.part-001').unlink()
        result = self.install()
        self.assert_payload()
        self.assertEqual(result['rf_files_verified'], [name])

    def test_candidate_rejects_wrong_index_and_crossed_part_order(self):
        rf = self.candidate_catalog()
        index = rf['index_sha256']
        rf['index_sha256'] = 'c' * 64
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'lineage'):
            self.install()
        rf['index_sha256'] = index
        rf['parts'][0], rf['parts'][1] = rf['parts'][1], rf['parts'][0]
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'order'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_candidate_rejects_an_alias_or_corrupt_bytes_in_stage_one(self):
        self.candidate_catalog()
        alias = self.parts / 'stage1.joblib.part-01'
        alias.write_bytes(b'RF ')
        with self.assertRaisesRegex(ValueError, 'Duplicate RF volume number'):
            self.install()
        alias.unlink()
        (self.parts / 'stage1.joblib.part-001').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_candidate_requires_an_explicit_source_for_each_layer(self):
        rf = self.candidate_catalog()
        rf['sources']['stage1.joblib'] = None
        self.write_sources()
        with self.assertRaisesRegex(ValueError, 'explicit archive or GitHub'):
            self.install()

    def test_candidate_requires_local_input_for_a_layer_without_a_published_source(self):
        self.candidate_catalog()
        (self.parts / 'stage1.joblib.part-001').unlink()
        with patch.object(model_download, 'GitHubRF', side_effect=AssertionError('network used')), \
             self.assertRaisesRegex(FileNotFoundError, 'parts-dir'):
            self.install(download_rf=True)

    def test_candidate_selects_the_release_of_each_missing_layer(self):
        rf = self.candidate_catalog()
        first = {'kind': 'github', 'repository': 'chrisjack007/IncFinder_showcase', 'tag': 'model-test', 'access': 'public'}
        second = {'kind': 'github', 'repository': 'chrisjack007/IncFinder_engineer', 'tag': 'model-reference', 'access': 'authenticated',
                  'release_id': 50}
        rf['sources'] = {'stage1.joblib': first, 'stage2.joblib': second}
        self.write_sources()
        donor = self.root / 'delivery-fixture'
        donor.mkdir()
        requests = []
        for row in rf['parts']:
            (self.parts / row['name']).rename(donor / row['name'])

        def fetch(receiver, cache, row):
            requests.append((row['name'], receiver.supply))
            return donor / row['name'], {'name': row['name']}

        with patch.object(model_download.GitHubRF, 'fetch', fetch):
            self.install(download_rf=True)
        self.assert_payload()
        self.assertEqual(requests, [(row['name'], rf['sources'][row['name'].partition('.part-')[0]])
                                    for row in rf['parts']])

    def test_offline_install_verify_and_reuse(self):
        with patch.object(model_download,'_credential',side_effect=AssertionError('network used')), \
             patch.object(model_download,'download_hub',side_effect=AssertionError('network used')):
            result = self.install()
            verify = model_install.verify_models(self.target,self.parts)
            reused = self.install(download_rf=True,download_ntv3=True)
        self.assert_payload()
        self.assertEqual(result['installed'],list(self.payload))
        self.assertEqual(verify['models']['status'],'PASS')
        self.assertEqual(len(verify['rf_files_verified']),4)
        self.assertEqual(reused['retained'],list(self.payload))
        self.assertFalse(reused['installed'])
        self.assertEqual(reused['downloads'],{'rf':[],'ntv3':[]})

    def test_cli_model_install_and_verify(self):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['models','install','--model-dir',str(self.target),
                                       '--parts-dir',str(self.parts),'--ntv3-dir',str(self.donor)]),0)
            self.assertEqual(cli.main(['models','verify','--model-dir',str(self.target)]),0)
        self.assertIn('"status": "PASS"',out.getvalue())
        self.assert_payload()

    def test_absent_rf_volume_is_identified_before_publication(self):
        (self.parts/'stage2.joblib.part-002').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'part-002'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_alias_for_same_number_is_rejected(self):
        (self.parts/'stage2.joblib.part-01').write_bytes(b'aaaa')
        with self.assertRaisesRegex(ValueError,'Duplicate RF volume number'):
            self.install()

    def test_unlisted_extra_volume_is_rejected(self):
        (self.parts/'stage2.joblib.part-004').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError,'Unexpected RF volume name.*004'):
            self.install()

    def test_manifest_order_and_duplicate_ids_are_validated(self):
        records = self.sources['rf']['parts']
        records[1],records[2] = records[2],records[1]
        self.write_sources()
        with self.assertRaisesRegex(ValueError,'out of order'):
            self.install()
        records[1],records[2] = records[2],records[1]
        records[2]['asset_id'] = records[1]['asset_id']
        self.write_sources()
        with self.assertRaisesRegex(ValueError,'Duplicate model volume'):
            self.install()

    def test_swapped_volume_bytes_fail_expected_digest(self):
        (self.parts/'stage2.joblib.part-001').write_bytes(b'bbbb')
        (self.parts/'stage2.joblib.part-002').write_bytes(b'aaaa')
        with self.assertRaisesRegex(ValueError,'SHA-256.*part-001'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_truncated_volume_reports_size(self):
        (self.parts/'stage2.joblib.part-003').write_bytes(b'c')
        with self.assertRaisesRegex(ValueError,'size mismatch.*003'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_full_expected_digest_is_checked_after_part_hashes(self):
        self.freeze['files'][1]['sha256'] = 'a'*64
        common.write_json(self.source/'config/assets.json',self.freeze)
        with self.assertRaisesRegex(ValueError,'Complete model size or SHA-256 mismatch.*stage2'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_wrong_existing_target_is_kept(self):
        existing = self.seed('rf/stage1.joblib',b'RF two')
        with self.assertRaisesRegex(ValueError,'SHA-256 mismatch.*stage1'):
            self.install(download_rf=True)
        self.assertEqual(existing.read_bytes(),b'RF two')
        self.assertFalse((self.target/'rf/stage2.joblib').exists())

    def test_model_space_is_checked_before_assembly_or_download(self):
        with patch('ntv3_tsrf.resource_plan.disk_usage',return_value=SimpleNamespace(free=0)), \
             patch.object(model_install,'_assemble',side_effect=AssertionError('assembled before preflight')):
            with self.assertRaisesRegex(OSError,'model installation.*required.*available'):
                self.install()
        self.assertFalse(self.target.exists())

    def test_model_install_lock_rejects_concurrent_writer(self):
        workspace = self.workspace()
        workspace.mkdir()
        with _writer_lease(workspace,'.install-writer.lock'):
            with self.assertRaisesRegex(RuntimeError,'active writer'):
                self.install()
        self.assertEqual(self.install()['status'],'PASS')

    def test_no_replace_even_if_name_appears_before_publication(self):
        source = self.root/'staged'
        source.write_bytes(b'candidate')
        existing = self.root/'existing'
        existing.write_bytes(b'verified')
        with self.assertRaises(FileExistsError):
            model_install._publish(source,existing)
        self.assertEqual(existing.read_bytes(),b'verified')
        self.assertEqual(source.read_bytes(),b'candidate')

    def test_recovers_interrupted_assembly_and_keeps_ready_assets(self):
        recorded = self.seed('rf/stage1.joblib',self.payload['rf/stage1.joblib']).stat()
        _,_,_,source_sha = model_install._sources()
        ready = self.workspace()/source_sha/'ready'
        good = ready/'ntv3/MODEL-LICENSE.md'
        good.parent.mkdir(parents=True)
        good.write_bytes(self.payload['ntv3/MODEL-LICENSE.md'])
        stopped = ready/'rf/stage2.joblib.building'
        stopped.parent.mkdir(parents=True)
        stopped.write_bytes(b'partial')
        result = self.install()
        self.assert_payload()
        self.assertEqual(result['verified_staged_reused'],['ntv3/MODEL-LICENSE.md'])
        current = (self.target/'rf/stage1.joblib').stat()
        self.assertEqual((current.st_ino,current.st_mtime_ns),(recorded.st_ino,recorded.st_mtime_ns))
        self.assertFalse(stopped.exists())

    def test_missing_ntv3_is_not_downloaded_with_rf_permission(self):
        (self.donor/'model.safetensors').unlink()
        with patch.object(model_download,'download_hub',side_effect=AssertionError('unexpected Hub access')):
            with self.assertRaisesRegex(FileNotFoundError,'download-ntv3'):
                self.install(download_rf=True)

    def test_explicit_hub_download_only_requests_the_missing_file(self):
        weight = self.donor/'model.safetensors'
        weight.unlink()

        def download(cache,supply,name):
            self.assertEqual(name,'model.safetensors')
            self.assertEqual(supply,self.sources['ntv3'])
            material = self.root/'hub-response'
            material.write_bytes(self.payload['ntv3/model.safetensors'])
            return material

        with patch.object(model_download,'download_hub',side_effect=download) as mock:
            result = self.install(download_ntv3=True)
        mock.assert_called_once()
        self.assertEqual(result['downloads']['ntv3'],['ntv3/model.safetensors'])
        self.assert_payload()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name).resolve()
        self.content = b'abcdefghijklmnopqrstuvwxyz'
        self.row = {'name':'stage2.joblib.part-001','size_bytes':len(self.content),
                    'sha256':sha(self.content),'asset_id':42}
        self.supply = {'repository':'chrisjack007/IncFinder_engineer','tag':'NT_V3_650M_TSRF_SW_V1','release_id':99}
        self.receiver = model_download.GitHubRF(self.supply)

    def http(self, modes):
        data, calls = self.content, []
        remaining = iter(modes)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                mode = next(remaining)
                range_header = self.headers.get('Range')
                calls.append(range_header)
                offset = int(range_header.split('=')[1][:-1]) if range_header else 0
                start = offset if mode in ('resume','bad-range','changed-etag') else 0
                self.send_response(206 if start else 200)
                self.send_header('ETag','"second-object"' if mode == 'changed-etag' else '"frozen-object"')
                if start:
                    declared = start - 1 if mode == 'bad-range' else start
                    self.send_header('Content-Range',f'bytes {declared}-{len(data)-1}/{len(data)}')
                self.send_header('Content-Length',str(len(data)-start))
                self.end_headers()
                try:
                    self.wfile.write(data[:7] if mode == 'stop' else data[start:])
                except ConnectionError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.05},daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join,2)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}/asset'
        def binary(_row, offset):
            return urlopen(Request(url,headers={'Range':f'bytes={offset}-'} if offset else {}),timeout=5)
        return calls, binary

    def check_retry(self, modes, expected_calls, reused):
        calls, binary = self.http(modes)
        with patch.object(self.receiver,'_binary',side_effect=binary), patch.object(self.receiver,'_validate_asset'):
            with self.assertRaisesRegex(RuntimeError,'download interrupted'):
                self.receiver.fetch(self.cache,self.row)
            self.assertEqual(model_download.rf_required_bytes(self.cache,self.row),len(self.content)-7)
            path, proof = self.receiver.fetch(self.cache,self.row)
            cached, reused_proof = self.receiver.fetch(self.cache,self.row)
        self.assertEqual(path.read_bytes(),self.content)
        self.assertEqual(cached,path)
        self.assertEqual(proof['resumed_from_bytes'],reused)
        self.assertEqual(reused_proof['source'],'verified_cache')
        self.assertEqual(calls,expected_calls)

    def test_real_loopback_interruption_resumes_matching_range(self):
        self.check_retry(('stop','resume'),[None,'bytes=7-'],7)

    def test_wrong_range_restarts_current_volume(self):
        self.check_retry(('stop','bad-range','full'),[None,'bytes=7-',None],0)

    def test_etag_change_prevents_appending(self):
        self.check_retry(('stop','changed-etag','full'),[None,'bytes=7-',None],0)

    def test_server_ignores_range_and_restarts_seamlessly(self):
        self.check_retry(('stop','full'),[None,'bytes=7-'],0)

    def test_verified_full_partial_recovers_without_authentication(self):
        complete, pending, state = model_download._paths(self.cache,self.row)
        pending.parent.mkdir()
        pending.write_bytes(self.content)
        with patch.object(model_download,'_credential',side_effect=AssertionError('unexpected authentication')):
            path, proof = self.receiver.fetch(self.cache,self.row)
        self.assertEqual(path,complete)
        self.assertEqual(proof['source'],'verified_cache')
        self.assertFalse(state.exists())

    def test_public_content_pinned_release_records_actual_ids_without_credentials(self):
        row = {'name': 'stage1.joblib.part-001', 'size_bytes': len(self.content), 'sha256': sha(self.content)}
        supply = {'repository': 'chrisjack007/IncFinder_showcase', 'tag': 'model-test', 'access': 'public'}
        receiver = model_download.GitHubRF(supply)
        release = {'id': 77, 'tag_name': supply['tag'], 'draft': False}
        asset = {'id': 78, 'name': row['name'], 'size': row['size_bytes'], 'digest': 'sha256:' + row['sha256']}
        calls, binary = self.http(['full'])
        with patch.object(receiver, '_json', side_effect=[release, [asset]]), \
             patch.object(receiver, '_binary', side_effect=binary), \
             patch.object(model_download, '_credential', side_effect=AssertionError('unexpected authentication')):
            actual, proof = receiver.fetch(self.cache, row)
        self.assertEqual(actual.read_bytes(), self.content)
        self.assertEqual(calls, [None])
        self.assertEqual((proof['repository'], proof['tag'], proof['release_id'], proof['asset_id']),
                         (supply['repository'], supply['tag'], 77, 78))
        self.assertIsNone(receiver._request('/releases/77', 'application/json').get_header('Authorization'))
        with patch.object(receiver, '_json', side_effect=AssertionError('unexpected network')):
            _, cached = receiver.fetch(self.cache, row)
        self.assertEqual(cached['source'], 'verified_cache')
        self.assertNotIn('asset_id', cached)

    def test_content_pinned_remote_checks_digest_and_unambiguous_asset_name(self):
        row = {'name': 'stage2.joblib', 'size_bytes': len(self.content), 'sha256': sha(self.content)}
        supply = {'repository': 'chrisjack007/IncFinder_showcase', 'tag': 'model-test', 'access': 'public'}
        release = {'id': 31, 'tag_name': supply['tag'], 'draft': False}
        valid = {'id': 44, 'name': row['name'], 'size': len(self.content), 'digest': 'sha256:' + row['sha256']}
        for entries in ([dict(valid, digest='sha256:' + '0' * 64)], [valid, dict(valid, id=45)]):
            receiver = model_download.GitHubRF(supply)
            with self.subTest(entries=entries), patch.object(receiver, '_json', side_effect=[release, entries]), \
                 self.assertRaisesRegex(ValueError, 'metadata mismatch'):
                receiver._validate_asset(row)

    def test_content_pinned_remote_checks_all_asset_pages(self):
        supply = {'repository': 'chrisjack007/IncFinder_showcase', 'tag': 'model-test', 'access': 'public'}
        receiver = model_download.GitHubRF(supply)
        release = {'id': 31, 'tag_name': supply['tag'], 'draft': False}
        row = {'name': 'stage2.joblib', 'size_bytes': len(self.content), 'sha256': sha(self.content)}
        other = [{'id': i, 'name': f'unrelated-{i}'} for i in range(1, 101)]
        wanted = {'id': 301, 'name': row['name'], 'size': row['size_bytes'], 'digest': 'sha256:' + row['sha256']}
        with patch.object(receiver, '_json', side_effect=[release, other, [wanted]]) as lookups:
            self.assertEqual(receiver._validate_asset(row), 301)
        self.assertIn('page=2', lookups.call_args_list[-1].args[0])

    def test_release_metadata_must_match_pinned_asset(self):
        release = {'id':99,'tag_name':self.supply['tag'],'draft':False}
        asset = {'id':42,'name':self.row['name'],'size':self.row['size_bytes'],'digest':'sha256:'+'0'*64}
        with patch.object(model_download,'_credential',return_value=secrets.token_hex()), \
             patch.object(self.receiver,'_json',side_effect=[release,[asset]]):
            with self.assertRaisesRegex(ValueError,'metadata mismatch'):
                self.receiver.fetch(self.cache,self.row)

    def test_binary_redirect_never_forwards_authorization_header(self):
        token = secrets.token_hex()
        query = secrets.token_hex()
        self.receiver.token = token
        reply = HTTPError('https://api.github.com/asset',302,'Found',
                          {'Location':'https://release-assets.githubusercontent.com/download?signature='+query},None)
        with patch.object(self.receiver.opener,'open',side_effect=[reply,object()]) as open_request:
            self.receiver._binary(self.row,4)
        first, second = [call.args[0] for call in open_request.call_args_list]
        self.assertEqual(first.get_header('Authorization'),'Bearer '+token)
        self.assertIsNone(second.get_header('Authorization'))
        self.assertEqual(second.get_header('Range'),'bytes=4-')
        self.assertNotIn(token,second.full_url)

    def test_auth_failures_never_report_raw_response(self):
        sentinel = secrets.token_hex()
        self.receiver.token = sentinel
        reply = HTTPError('https://api.github.com/private?nonce='+sentinel,403,sentinel,{},None)
        with patch.object(self.receiver.opener,'open',side_effect=reply):
            with self.assertRaisesRegex(RuntimeError,'HTTP 403') as caught:
                self.receiver._json('/releases')
        self.assertNotIn(sentinel,str(caught.exception))

    def test_hub_errors_hide_child_output_and_keep_secret_out_of_arguments(self):
        sentinel = secrets.token_hex()
        supply = {'repository':'InstaDeepAI/NTv3_650M_pre','revision':'a'*40}
        with patch.dict(os.environ,{'HF_TOKEN':sentinel}), \
             patch.object(model_download.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as child:
            with self.assertRaisesRegex(RuntimeError,'hf auth login') as caught:
                model_download.download_hub(self.cache,supply,'model.safetensors')
        self.assertNotIn(sentinel,str(caught.exception))
        self.assertNotIn(sentinel,' '.join(child.call_args.args[0]))
        self.assertEqual(child.call_args.kwargs['stderr'],model_download.subprocess.DEVNULL)
        self.assertEqual(child.call_args.kwargs['stdout'],model_download.subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main(verbosity=2)
