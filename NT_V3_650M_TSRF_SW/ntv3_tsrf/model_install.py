"""Install or verify the exact published model files without loading them."""

from collections import defaultdict
import hashlib
import os
from pathlib import Path
import re
import stat
import sys

from .artifact_manifest import _facts, artifact_path
from .common import read_json, sha256, verify_assets
from .encoder_source import CODE_PATHS, code_source
from .resource_plan import _disk_budget, _nearest_directory
from .resources import ROOT, checked_child, model_root, path_is_link
from .run_record import _writer_lease

RF_STAGE1 = 'rf/stage1.joblib'
RF_STAGE2 = 'rf/stage2.joblib'
BUFFER_SIZE = 8 * 1024 * 1024


def _is_sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _layer_parts(rf, filename):
    leaf = Path(filename).name
    return [row for row in rf['parts'] if row['name'].partition('.part-')[0] == leaf]


def _release_for(rf, filename):
    if 'sources' not in rf:
        return rf
    source = rf['sources'][filename.partition('.part-')[0]]
    return source if source['kind'] == 'github' else None


def _source_receipt(rf, *, include_id=False):
    if 'sources' in rf:
        return {'rf_lineage_index_sha256': rf['index_sha256'], 'rf_layer_sources': rf['sources']}
    result = {'rf_release': rf['tag']}
    if include_id:
        result['rf_release_id'] = rf['release_id']
    return result


def _candidate_rf(rf, model):
    from .model_lineage import load_pointer

    pointer = load_pointer(ROOT)
    if (not isinstance(rf, dict) or set(rf) != {'index_sha256', 'sources', 'parts'}
            or not pointer or rf['index_sha256'] != pointer['index_sha256']):
        raise ValueError('RF source catalog does not match the packaged model lineage')
    sources, parts = rf['sources'], rf['parts']
    leaves = [Path(name).name for name in (RF_STAGE1, RF_STAGE2)]
    if not isinstance(sources, dict) or set(sources) != set(leaves) or not isinstance(parts, list):
        raise ValueError('RF catalog must declare both model layers')
    for row in parts:
        if (not isinstance(row, dict) or set(row) - {'name', 'sha256', 'size_bytes', 'asset_id'}
                or not isinstance(row.get('name'), str) or not _is_sha(row.get('sha256'))
                or type(row.get('size_bytes')) is not int or row['size_bytes'] <= 0):
            raise ValueError('Invalid RF volume declaration')
    ordered, asset_ids = [], set()
    for model_name, leaf in zip((RF_STAGE1, RF_STAGE2), leaves):
        source, group = sources[leaf], _layer_parts(rf, leaf)
        if not isinstance(source, dict):
            raise ValueError('An explicit archive or GitHub source is required: ' + leaf)
        release = None if source == {'kind': 'archive'} else source
        if release is not None:
            if (release.get('kind') != 'github' or set(release) - {'kind', 'repository', 'tag', 'access', 'release_id'}
                    or release.get('repository') not in ('chrisjack007/IncFinder_engineer', 'chrisjack007/IncFinder_showcase')
                    or not isinstance(release.get('tag'), str) or not release['tag'].strip()
                    or release.get('access') not in ('public', 'authenticated')
                    or ('release_id' in release and (type(release['release_id']) is not int or release['release_id'] <= 0))):
                raise ValueError('Invalid GitHub release for ' + leaf)
        if not group or sum(row['size_bytes'] for row in group) != model[model_name]['size_bytes']:
            raise ValueError('Incomplete RF layer: ' + leaf)
        names = [row['name'] for row in group]
        numbered = [f'{leaf}.part-{i:03d}' for i in range(1, len(group) + 1)]
        if names != [leaf] and names != numbered:
            raise ValueError('RF volumes are missing or out of order: ' + leaf)
        if len(group) == 1 and group[0]['sha256'] != model[model_name]['sha256']:
            raise ValueError('Single RF volume differs from the complete layer: ' + leaf)
        ordered.extend(names)
        for row in group:
            if release is not None and row['size_bytes'] >= 2 * 1024**3:
                raise ValueError('RF release asset exceeds the size limit: ' + row['name'])
            if 'asset_id' in row:
                if release is None or type(row['asset_id']) is not int or row['asset_id'] <= 0:
                    raise ValueError('Invalid RF release asset ID')
                key = (release['repository'], row['asset_id'])
                if key in asset_ids:
                    raise ValueError('Duplicate RF release asset ID')
                asset_ids.add(key)
    if ordered != [row['name'] for row in parts]:
        raise ValueError('Unexpected RF layer or volume order')


def _sources():
    from .model_lineage import checked_path, load as load_catalog

    # IF-016 also reads the V1 catalog to verify the retained historical release.
    delivery = checked_path(ROOT, 'config/model_delivery.json')
    candidate = delivery.exists()
    if not candidate and (ROOT / 'config/research_index.json').exists():
        raise ValueError('Missing release model delivery catalog')
    path = delivery if candidate else checked_path(ROOT, 'config/model_sources.json')
    document = load_catalog(path)
    assets = read_json(ROOT / 'config/assets.json')['files']
    declared = [row for row in assets if row['path'].startswith('models/')]
    model = {row['path'].removeprefix('models/'): row for row in declared}
    if code_source(ROOT):
        code = [row for row in assets if row['path'] in CODE_PATHS]
        if len(code) != len(CODE_PATHS) or {row['path'] for row in code} != CODE_PATHS:
            raise ValueError('Incomplete NTv3 source file inventory')
        declared.extend(code)
        model.update({row['path'].removeprefix('vendor/'): row for row in code})
    rf, ntv3 = document['rf'], document['ntv3']
    if (type(document['schema']) is not int or document['schema'] != (2 if candidate else 1) or len(model) != len(declared) or
            not {RF_STAGE1, RF_STAGE2, 'ntv3/model.safetensors'} <= model.keys() or
            any(not re.fullmatch(r'(?:rf|ntv3)/[A-Za-z0-9_.-]+', name) or name.split('/')[-1] in ('.', '..')
                for name in model)):
        raise ValueError('Invalid frozen model layout')
    if ntv3['repository'] != 'InstaDeepAI/NTv3_650M_pre' or not re.fullmatch(r'[0-9a-f]{40}', ntv3['revision']):
        raise ValueError('Invalid pinned model source')
    if any(not _is_sha(row['sha256']) or type(row['size_bytes']) is not int or row['size_bytes'] <= 0 for row in declared):
        raise ValueError('Invalid model size or SHA-256 declaration')
    if document['schema'] == 2:
        _candidate_rf(rf, model)
        return model, rf, ntv3, sha256(path)
    if (rf['repository'] != 'chrisjack007/IncFinder_engineer' or rf['tag'] != 'NT_V3_650M_TSRF_SW_V1'
            or type(rf['release_id']) is not int or rf['release_id'] <= 0):
        raise ValueError('Invalid pinned model source')
    parts = rf['parts']
    names = [row['name'] for row in parts]
    identifiers = [row['asset_id'] for row in parts]
    if len(set(names)) != len(names) or len(set(identifiers)) != len(identifiers):
        raise ValueError('Duplicate model volume name or release asset ID')
    nominal = rf['part_size_bytes']
    if type(nominal) is not int or nominal <= 0 or type(model[RF_STAGE2]['size_bytes']) is not int:
        raise ValueError('Invalid RF volume size declaration')
    count = (model[RF_STAGE2]['size_bytes'] + nominal - 1) // nominal
    if count < 1 or len(parts) != count + 1:
        raise ValueError('RF volume records are incomplete')
    expected = ['stage1.joblib', *(f'stage2.joblib.part-{number:03d}' for number in range(1, count + 1))]
    if len(parts) < 2 or names != expected:
        raise ValueError('RF volume records are missing or out of order')
    if any(not _is_sha(row['sha256']) or type(row['size_bytes']) is not int or row['size_bytes'] <= 0 for row in parts):
        raise ValueError('Invalid model size or SHA-256 declaration')
    if any(type(value) is not int or value <= 0 for value in identifiers):
        raise ValueError('Invalid release asset ID')
    if (any(parts[0][key] != model[RF_STAGE1][key] for key in ('size_bytes', 'sha256')) or
            any(row['size_bytes'] != nominal for row in parts[1:-1]) or
            not 0 < parts[-1]['size_bytes'] <= nominal or
            sum(row['size_bytes'] for row in parts[1:]) != model[RF_STAGE2]['size_bytes']):
        raise ValueError('RF volumes do not match the complete frozen model sizes')
    return model, rf, ntv3, sha256(path)


def _regular(path):
    if any(path_is_link(item) for item in (path, *path.parents)):
        raise ValueError(f'Model locations must not contain links: {path}')
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise ValueError(f'Model asset must be a regular file with one directory entry: {path}')
    return entry


def _check(path, identity, label):
    stat_result = _regular(path)
    if stat_result is None:
        raise FileNotFoundError(f'Missing model asset: {label}')
    if stat_result.st_size != identity['size_bytes']:
        raise ValueError(f'Model asset size mismatch: {label}')
    size, checksum = _facts(path, label, stored=True)
    if size != identity['size_bytes'] or checksum != identity['sha256']:
        raise ValueError(f'Model asset SHA-256 mismatch: {label}')


def _local_parts(folder, required):
    if folder is None:
        return {}
    if not folder.is_dir():
        raise NotADirectoryError(f'RF parts directory is invalid: {folder}')
    known = {row['name']: row for row in required}
    split_layers = {name.partition('.part-')[0] for name in known if '.part-' in name}
    if split_layers:
        indexes = {}
        observed = []
        for path in folder.iterdir():
            leaf, separator, number_text = path.name.lower().partition('.part-')
            if not separator or leaf not in split_layers:
                continue
            observed.append(path.name)
            if not re.fullmatch(r'[0-9]{1,6}', number_text):
                raise ValueError(f'Unexpected RF volume name: {path.name}')
            key = (leaf, int(number_text))
            if key in indexes:
                raise ValueError(f'Duplicate RF volume number: {indexes[key]} and {path.name}')
            indexes[key] = path.name
        for name in observed:
            if name not in known:
                raise ValueError(f'Unexpected RF volume name: {name}')
    return {name: folder / name for name in known if _regular(folder / name) is not None}


def _code_receipt():
    source = code_source(ROOT)
    if source is None:
        return {}
    rows = read_json(ROOT / 'config/assets.json')['files']
    return {'ntv3_code': {**source, 'files': [row for row in rows if row['path'] in CODE_PATHS]}}


def verify_models(model_dir=None, parts_dir=None, *, base=None):
    directory = model_root(model_dir, base)
    _, rf, ntv3, source_sha = _sources()
    parts = []
    if parts_dir is not None:
        found = _local_parts(artifact_path(parts_dir, base), rf['parts'])
        for row in rf['parts']:
            name = row['name']
            if name not in found:
                raise FileNotFoundError(f'Missing RF volume: {name}')
            _check(found[name], row, name)
            parts.append(name)
    return {'status': 'PASS', 'source_manifest_sha256': source_sha, 'models': verify_assets(model_dir=directory),
            **_source_receipt(rf), **_code_receipt(), 'ntv3_revision': ntv3['revision'], 'rf_files_verified': parts}


def _publish(source, target):
    """Atomically move a verified file without replacing an occupied name."""
    if os.name == 'nt':
        source.rename(target)
        return
    if sys.platform != 'linux':
        raise OSError('Atomic model installation is supported on Windows and Linux')
    import ctypes

    rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename is None:
        raise OSError('This Linux system does not provide a no-replace rename')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    at_fdcwd, rename_noreplace = -100, 1
    if rename(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(target), rename_noreplace) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def _assemble(destination, fragments, expected):
    """Stream the fragments and require both their individual and complete hashes."""
    whole = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + '.building')
    if _regular(pending) is not None:
        pending.unlink()
    try:
        with pending.open('xb') as output:
            for source, row, label in fragments:
                count = 0
                digest = hashlib.sha256()
                before = _regular(source)
                if before is None or before.st_size != row['size_bytes']:
                    raise ValueError(f'Model asset missing or size mismatch: {label}')
                with source.open('rb') as handle:
                    while chunk := handle.read(BUFFER_SIZE):
                        count += len(chunk)
                        if count > row['size_bytes']:
                            raise ValueError(f'Model asset exceeded its declared size: {label}')
                        digest.update(chunk)
                        whole.update(chunk)
                        output.write(chunk)
                if (count, digest.hexdigest()) != (row['size_bytes'], row['sha256']):
                    raise ValueError(f'Model asset SHA-256 or size mismatch: {label}')
            if output.tell() != expected['size_bytes'] or whole.hexdigest() != expected['sha256']:
                raise ValueError(f'Complete model size or SHA-256 mismatch: {expected["path"]}')
            output.flush()
            os.fsync(output.fileno())
        _publish(pending, destination)
    finally:
        # An uncatchable termination can leave this private file. A new install
        # discards it before reassembling from fully checked fragments.
        pending.unlink(missing_ok=True)


def _prepared_inputs(todo, model, rf, parts_dir, ntv3_dir, download_rf, download_ntv3):
    needed = [part for layer in (RF_STAGE1, RF_STAGE2) if layer in todo for part in _layer_parts(rf, layer)]
    local = _local_parts(parts_dir, needed)
    missing_rf = []
    for row in needed:
        name = row['name']
        if name not in local:
            if download_rf and _release_for(rf, name) is None:
                raise FileNotFoundError(f'RF volume {name} is supplied by the candidate archive. Supply --parts-dir.')
            if not download_rf:
                raise FileNotFoundError(f'Missing RF volume: {name}. Supply --parts-dir or explicitly use --download-rf.')
            missing_rf.append(row)
    remote_ntv3 = []
    for name in todo:
        if not name.startswith('ntv3/'):
            continue
        leaf = Path(name).name
        donor = ntv3_dir / leaf if ntv3_dir is not None else None
        if donor is not None and _regular(donor) is not None:
            local[name] = donor
        elif download_ntv3:
            remote_ntv3.append(name)
        else:
            raise FileNotFoundError(f'Missing model asset: {name}. Supply --ntv3-dir or explicitly use --download-ntv3.')
    for name, row in [(row['name'], row) for row in needed] + [(name, model[name]) for name in todo if name in local]:
        if name in local and local[name].stat().st_size != row['size_bytes']:
            raise ValueError(f'Model asset size mismatch: {name}')
    return needed, local, missing_rf, remote_ntv3


def _staging_path(path):
    """Keep the full source identity in internal staging paths on Windows."""
    if os.name != 'nt':
        return path
    name = os.fspath(path)
    if name.startswith('\\\\?\\'):
        return path
    if name.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + name[2:])
    return Path('\\\\?\\' + name)


def _install(target, workspace, cache, parts_dir, ntv3_dir, download_rf, download_ntv3, progress):
    model, rf, ntv3, source_sha = _sources()
    versioned = _staging_path(workspace / source_sha)
    network_cache = _staging_path(cache / source_sha)
    targets, retained, reusable = {}, [], {}
    # Check all pre-existing files before download or publication.
    for name, identity in model.items():
        destination = checked_child(target, name)
        if _regular(destination) is not None:
            _check(destination, identity, name)
            retained.append(name)
        else:
            targets[name] = destination
            ready = checked_child(versioned / 'ready', name)
            if _regular(ready) is not None:
                _check(ready, identity, name)
                reusable[name] = ready

    todo = [name for name in targets if name not in reusable]
    needed, local, missing_rf, remote_ntv3 = _prepared_inputs(
        todo, model, rf, parts_dir, ntv3_dir, download_rf, download_ntv3)
    for destination in targets.values():
        if _nearest_directory(destination.parent).stat().st_dev != workspace.stat().st_dev:
            raise OSError(f'Model staging and destination must share a filesystem: {destination}')
    for name in todo:
        staged = checked_child(versioned / 'ready', name)
        pending = staged.with_name(staged.name + '.building')
        if _regular(pending) is not None:
            pending.unlink()
    downloads = {'rf': [], 'ntv3': []}
    locations = {'model staging': workspace}
    staged = sum(model[name]['size_bytes'] for name in todo)
    budgets = {'model staging': (staged, staged)}
    if missing_rf or remote_ntv3:
        from .model_download import rf_required_bytes

        incoming = sum(rf_required_bytes(network_cache, row) for row in missing_rf)
        # The authenticated Hub client retains its download until the complete
        # model is verified and published. Allow a second copy in its cache.
        incoming += 2 * sum(model[name]['size_bytes'] for name in remote_ntv3)
        locations['download cache'] = network_cache
        budgets['download cache'] = (incoming, incoming)
    volumes, _ = _disk_budget(locations, {'model installation': budgets})
    if missing_rf:
        from .model_download import GitHubRF

        receivers = {}
        for row in missing_rf:
            release = _release_for(rf, row['name'])
            key = (release['repository'], release['tag'], release.get('release_id'), release.get('access'))
            if key not in receivers:
                receivers[key] = GitHubRF(release)
            path, proof = receivers[key].fetch(network_cache, row)
            local[row['name']] = path
            downloads['rf'].append(proof)
    if remote_ntv3:
        from .model_download import download_hub

        for name in remote_ntv3:
            leaf = Path(name).name
            source = code_source(ROOT) if model[name]['path'] in CODE_PATHS else ntv3
            local[name] = download_hub(network_cache, source, leaf)
            downloads['ntv3'].append(name)

    ready = dict(reusable)
    for name in todo:
        progress(f'Verifying and staging {name}')
        relevant = [row for row in needed if row['name'].startswith(Path(name).name)]
        fragments = ([(local[row['name']], row, row['name']) for row in relevant] if relevant
                     else [(local[name], model[name], name)])
        path = checked_child(versioned / 'ready', name)
        _assemble(path, fragments, model[name])
        ready[name] = path
    # No final model name is exposed until every missing file is complete and
    # verified. Each move is no-replace, including races with external writers.
    for name, destination in targets.items():
        checked_child(target, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _publish(ready[name], destination)

    assets = verify_assets(model_dir=target)
    return {'status': 'PASS', 'source_manifest_sha256': source_sha, 'models': assets,
            **_source_receipt(rf, include_id=True), **_code_receipt(), 'ntv3_revision': ntv3['revision'],
            'installed': list(targets), 'retained': retained, 'verified_staged_reused': list(reusable),
            'rf_files_verified': [row['name'] for row in needed], 'downloads': downloads, 'volumes': volumes}


def install_models(model_dir, parts_dir=None, ntv3_dir=None, cache_dir=None, *, download_rf=False,
                   download_ntv3=False, base=None, progress=None):
    if not str(model_dir).strip():
        raise ValueError('A model installation directory is required')
    target = artifact_path(model_dir, base)
    if not target.name or (target.exists() and not target.is_dir()):
        raise ValueError(f'Invalid model installation directory: {target}')
    key = hashlib.sha256(os.fsencode(os.path.normcase(target.name))).hexdigest()[:24]
    workspace = artifact_path(target.parent / ('.incfinder-model-' + key))
    parts = artifact_path(parts_dir, base) if parts_dir is not None else None
    ntv3 = artifact_path(ntv3_dir, base) if ntv3_dir is not None else None
    if ntv3 is not None and not ntv3.is_dir():
        raise NotADirectoryError(f'NTv3 input directory is invalid: {ntv3}')
    # Include the parent path in the cache key so installations with the same
    # last component cannot race on a shared optional download volume.
    cache_key = hashlib.sha256(os.fsencode(os.path.normcase(target))).hexdigest()[:24]
    cache = (workspace / 'downloads' if cache_dir is None else
             artifact_path(cache_dir, base) / ('incfinder-model-' + cache_key))
    cache = artifact_path(cache)
    if cache.is_relative_to(target) or any(
        folder is not None and (workspace.is_relative_to(folder) or cache.is_relative_to(folder))
        for folder in (parts, ntv3)
    ):
        raise ValueError('The model staging and cache must be outside the model directory and read-only input directories')
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    _regular(workspace / '.install-writer.lock')
    if progress is None:
        progress = lambda _: None
    with _writer_lease(workspace, '.install-writer.lock'):
        return _install(target, workspace, cache, parts, ntv3, download_rf, download_ntv3, progress)
