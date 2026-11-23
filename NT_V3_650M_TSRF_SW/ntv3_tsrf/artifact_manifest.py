"""Artifact inventories and content verification for predictions and archives."""
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from .common import record_path_value
from .output_metadata import (CORE_RESULT_FILES, CSV_RESULT_FILES, RESULT_FILES, WORKBOOK,
                              delivered_result_files, result_inventory)
from .resources import path_is_link
from .run_events import canonical_uuid, DATABASE_NAME


MANIFEST = 'artifact_manifest.json'
SEAL = 'artifact_manifest.sha256'
_SEALS = {MANIFEST, SEAL}
_DATABASE_FILES = {DATABASE_NAME, *(DATABASE_NAME + suffix for suffix in ('-journal', '-shm', '-wal'))}
_SUMMARY = 'run_summary.json'
_PREDICTION = 'prediction.json'
_ARRAY = 'features/features.npy'
_FEATURE_META = 'features/features.json'
_CORE_INVENTORY = result_inventory(CORE_RESULT_FILES)
_REPORT_RESULT_FILES = result_inventory(RESULT_FILES) - _CORE_INVENTORY
_REPORT_ARTIFACTS = _REPORT_RESULT_FILES | {'report_environment.json', 'logs/report.log'}
_FIELDS = {'schema_version', 'kind', 'run_id', 'external_mode', 'model_manifest_sha256', 'artifacts'}
_ENTRY_FIELDS = {'artifact_id', 'role', 'path', 'storage', 'size_bytes', 'sha256', 'parents', 'stage'}
_ROLES = {'input', 'features', 'feature_metadata', 'log', 'result', 'metadata', 'intermediate'}
_STAGES = {'source', 'embedding', 'classification', 'report', 'publication'}
_EXTERNAL_ROLES = {'input', 'features', 'feature_metadata'}


def artifact_path(value, base=None):
    """Resolve caller paths, rejecting linked components before resolving them."""
    path = (Path.cwd() if base is None else Path(base)) / Path(value).expanduser()
    if any(path_is_link(part) for part in (path, *path.parents)):
        raise ValueError('Artifact paths must not contain links')
    return path.resolve()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _relative(name):
    if (not isinstance(name, str) or
            any(part in ('', '.', '..') or part.endswith(('.', ' ')) or PureWindowsPath(part).is_reserved()
                for part in name.split('/')) or re.search(r'[\\:<>"|?*\x00-\x1f]', name)):
        raise ValueError('Invalid relative artifact path')
    return name


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _facts(path, label, *, stored=False):
    try:
        before = path.lstat()
        if path_is_link(path) or not stat.S_ISREG(before.st_mode) or (stored and before.st_nlink > 1):
            raise ValueError(f'Artifact must be a regular, unlinked file: {label}')
        expected = _file_identity(before)
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(opened) != expected:
                raise ValueError(f'Artifact changed before hashing: {label}')
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            finished = os.fstat(stream.fileno())
        after = path.lstat()
        # Windows can report different ctime values for path and descriptor stats.
        if (_file_identity(finished) != expected or finished.st_ctime_ns != opened.st_ctime_ns
                or _file_identity(after) != expected or after.st_ctime_ns != before.st_ctime_ns
                or after.st_mode != before.st_mode or after.st_nlink != before.st_nlink):
            raise ValueError(f'Artifact changed during hashing: {label}')
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError(f'Missing artifact: {label}') from exc
    return before.st_size, digest


def _files(root, kind):
    """Enumerate physical files below root without following linked directories."""
    result = {}
    if not root.is_dir():
        raise ValueError('Artifact directory does not exist')
    directories = [root]
    while directories:
        directory = directories.pop()
        for path in sorted(directory.iterdir()):
            name = _relative(path.relative_to(root).as_posix())
            mode = path.lstat().st_mode
            if path_is_link(path):
                raise ValueError(f'Artifact paths must not contain links: {name}')
            if stat.S_ISDIR(mode):
                if name in _SEALS or path.name in _DATABASE_FILES:
                    raise ValueError(f'Reserved artifact path is a directory: {name}')
                directories.append(path)
            elif not stat.S_ISREG(mode):
                raise ValueError(f'Artifact is not a regular file: {name}')
            elif name not in _SEALS and not (kind == 'run' and name in _DATABASE_FILES):
                result[name] = path
    return result


def _reference(run_id, identity, role, path, expected, parents):
    artifact_id = f'{run_id}:reference/{identity}'
    size, digest = _facts(artifact_path(path), artifact_id)
    if digest != expected:
        raise ValueError(f'Reference SHA-256 changed during prediction: {artifact_id}')
    visible = record_path_value(path)
    return {
        'artifact_id': artifact_id, 'role': role, 'path': None if visible == '[redacted]' else visible,
        'storage': 'reference', 'size_bytes': size, 'sha256': digest, 'parents': parents, 'stage': 'source',
    }


def _describe(name, ids, inputs, inference, report, summary):
    if name == _ARRAY:
        return 'features', 'embedding', inputs
    if name == _FEATURE_META:
        return 'feature_metadata', 'embedding', [*inputs, ids[_ARRAY]]
    if name == WORKBOOK:
        return 'result', 'report', report
    if name == 'report_environment.json':
        return 'metadata', 'report', [ids[WORKBOOK]]
    if name.endswith('.metadata.json') and name.removesuffix('.metadata.json') in RESULT_FILES:
        return 'metadata', 'publication', [ids[name.removesuffix('.metadata.json')], ids[_PREDICTION]]
    if name == _SUMMARY:
        return 'metadata', 'publication', summary
    if name == _PREDICTION:
        return 'metadata', 'classification', inference
    if name in RESULT_FILES:
        return 'result', 'classification', inference
    if name.startswith('logs/'):
        if name == 'logs/embedding.log':
            return 'log', 'embedding', inputs
        if name == 'logs/rf.log':
            return 'log', 'classification', inference
        if name == 'logs/report.log':
            return 'log', 'report', report
        return 'log', 'publication', []
    return 'intermediate', 'publication', []


def write_run_artifacts(root, summary, source, external_features, features_metadata_sha256):
    """Create the sealed inventory for a staged result and return its digest."""
    run_id = summary['run_id']
    paths = _files(root, 'run')
    required = result_inventory(delivered_result_files(summary)) | {_SUMMARY}
    if not required <= paths.keys():
        raise ValueError('Missing required prediction artifacts')
    if external_features is None and not {_ARRAY, _FEATURE_META}.issubset(paths):
        raise ValueError('Missing retained feature artifacts')
    ids = {name: f'{run_id}:file/{name}' for name in paths}
    input_entry = _reference(run_id, 'input', 'input', source, summary['input']['sha256'], [])
    entries = [input_entry]
    inputs = [input_entry['artifact_id']]
    if external_features is not None:
        metadata = _reference(run_id, 'features.json', 'feature_metadata',
                              external_features / 'features.json', features_metadata_sha256, inputs)
        features = _reference(run_id, 'features.npy', 'features', external_features / 'features.npy',
                              summary['features_sha256'], [metadata['artifact_id']])
        entries.extend((metadata, features))
        feature_ids = [metadata['artifact_id'], features['artifact_id']]
    else:
        feature_ids = [ids[_ARRAY], ids[_FEATURE_META]]
    inference = inputs + feature_ids
    report = [ids[name] for name in (*CSV_RESULT_FILES, _PREDICTION)]
    summary_parents = inference + [ids[name] for name in sorted(summary['outputs'])]
    for name, path in sorted(paths.items()):
        role, stage, parents = _describe(name, ids, inputs, inference, report, summary_parents)
        size, digest = _facts(path, name, stored=True)
        if name in summary['outputs'] and summary['outputs'][name] != digest:
            raise ValueError(f'Result SHA-256 changed after summary: {name}')
        if name == _ARRAY and digest != summary['features_sha256']:
            raise ValueError('Retained feature SHA-256 does not match the result summary')
        entries.append({
            'artifact_id': ids[name], 'role': role, 'path': name, 'storage': 'retained',
            'size_bytes': size, 'sha256': digest, 'parents': parents, 'stage': stage,
        })
    manifest = {
        'schema_version': 1, 'kind': 'run', 'run_id': run_id, 'external_mode': 'reference',
        'model_manifest_sha256': summary['assets'].get('manifest_sha256'), 'artifacts': entries,
    }
    _check_summary(summary, manifest)
    return write_manifest(root, manifest)


def _validate(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('Artifact manifest must be an object')
    kind = manifest.get('kind')
    required = _FIELDS | ({'source_manifest_sha256'} if kind == 'archive' else set())
    if (set(manifest) != required or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or not canonical_uuid(manifest['run_id']) or not isinstance(manifest['artifacts'], list)):
        raise ValueError('Unsupported or malformed artifact manifest')
    mode = manifest['external_mode']
    if (kind not in ('run', 'archive') or mode not in ('reference', 'copy') or (kind == 'run' and mode != 'reference')
            or (kind == 'archive' and not _digest(manifest['source_manifest_sha256']))
            or (manifest['model_manifest_sha256'] is not None and not _digest(manifest['model_manifest_sha256']))):
        raise ValueError('Invalid artifact manifest identity or mode')

    graph, dependencies, portable_paths, names = {}, {}, set(), set()
    for entry in manifest['artifacts']:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise ValueError('Malformed artifact entry')
        aid, role, storage = entry['artifact_id'], entry['role'], entry['storage']
        if (not isinstance(aid, str) or not aid.startswith(manifest['run_id'] + ':') or aid in graph
                or not isinstance(role, str) or role not in _ROLES
                or not isinstance(entry['stage'], str) or entry['stage'] not in _STAGES
                or type(entry['size_bytes']) is not int or entry['size_bytes'] < 0 or not _digest(entry['sha256'])):
            raise ValueError('Invalid or duplicate artifact identity')
        parents, name = entry['parents'], entry['path']
        if (not isinstance(parents, list) or not all(isinstance(item, str) for item in parents)
                or len(set(parents)) != len(parents)):
            raise ValueError('Invalid artifact parents')
        graph[aid] = parents
        if role in _EXTERNAL_ROLES:
            if role in dependencies:
                raise ValueError('Duplicate input or feature role')
            dependencies[role] = storage
        if storage == 'reference':
            if (mode != 'reference' or role not in _EXTERNAL_ROLES
                    or (name is not None and (not isinstance(name, str) or not name
                                              or re.search(r'[\x00-\x1f]', name)))):
                raise ValueError('Invalid external artifact reference')
        elif storage in ('retained', 'copied'):
            name = _relative(name)
            if (name in _SEALS or Path(name).name in _DATABASE_FILES or name.casefold() in portable_paths
                    or (storage == 'copied' and (mode != 'copy' or role not in _EXTERNAL_ROLES))):
                raise ValueError('Duplicate, reserved, or inconsistent artifact path')
            portable_paths.add(name.casefold())
            names.add(name)
        else:
            raise ValueError('Invalid artifact storage mode')
    if (dependencies.keys() != _EXTERNAL_ROLES or not (_CORE_INVENTORY | {_SUMMARY}) <= names
            or dependencies['input'] != ('copied' if mode == 'copy' else 'reference')
            or dependencies['features'] != dependencies['feature_metadata']):
        raise ValueError('Manifest is missing or inconsistent for required artifacts')
    if any(parent not in graph for parents in graph.values() for parent in parents):
        raise ValueError('Unknown parent artifact')
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError('Artifact dependencies contain a cycle') from exc


def write_manifest(root, manifest):
    _validate(manifest)
    content = (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('ascii')
    digest = hashlib.sha256(content).hexdigest()
    with (root / MANIFEST).open('xb') as stream:
        stream.write(content)
    with (root / SEAL).open('xb') as stream:
        stream.write((digest + '  ' + MANIFEST + '\n').encode('ascii'))
    return digest


def _unique_fields(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('Duplicate artifact manifest JSON field')
    return values


def _open_manifest(root, expected):
    for name in _SEALS:
        candidate = root / name
        if path_is_link(candidate) or not candidate.is_file() or candidate.lstat().st_nlink > 1:
            raise ValueError(f'Missing or linked artifact manifest seal: {name}')
    content = (root / MANIFEST).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if ((root / SEAL).read_bytes() != (digest + '  ' + MANIFEST + '\n').encode('ascii')
            or (expected is not None and digest != expected)):
        raise ValueError('Artifact manifest SHA-256 mismatch')
    manifest = json.loads(content, object_pairs_hook=_unique_fields)
    _validate(manifest)
    return manifest, digest


def _resolve_reference(entry, reference_paths):
    name = entry['path']
    if name is None:
        if entry['artifact_id'] not in reference_paths:
            raise ValueError(f'Redacted reference requires an explicit path: {entry["artifact_id"]}')
        return artifact_path(reference_paths[entry['artifact_id']])
    path = Path(name)
    if not path.is_absolute() or artifact_path(path) != path:
        raise ValueError(f'Invalid native reference path: {entry["artifact_id"]}')
    return path


def _check_summary(summary, manifest):
    if (not isinstance(summary, dict) or summary.get('run_id') != manifest['run_id']
            or summary.get('execution_status') != 'PASS'):
        raise ValueError('Artifact manifest does not match the successful prediction summary')
    entries = manifest['artifacts']
    dependencies = {e['role']: e['sha256'] for e in entries if e['role'] in _EXTERNAL_ROLES}
    retained = {e['path']: e['sha256'] for e in entries if e['storage'] == 'retained'}
    source, assets, outputs = (summary.get(name) for name in ('input', 'assets', 'outputs'))
    if (not all(isinstance(value, dict) for value in (source, assets, outputs))
            or source.get('sha256') != dependencies['input']
            or summary.get('features_sha256') != dependencies['features']
            or assets.get('manifest_sha256') != manifest['model_manifest_sha256']
            or any(retained.get(name) != digest for name, digest in outputs.items())):
        raise ValueError('Artifact digests do not match the prediction summary')
    required = result_inventory(delivered_result_files(summary))
    if (not required <= outputs.keys()
            or (retained.keys() & _REPORT_RESULT_FILES) != (required & _REPORT_RESULT_FILES)
            or (WORKBOOK not in required and retained.keys() & _REPORT_ARTIFACTS)):
        raise ValueError('Artifact files do not match the declared result delivery')


def _verify_members(manifest, actual, reference_paths=None, *, external=True):
    entries = manifest['artifacts']
    references = {e['artifact_id'] for e in entries if e['storage'] == 'reference' and e['path'] is None}
    overrides = {} if reference_paths is None else reference_paths
    if not isinstance(overrides, dict) or overrides.keys() - references:
        raise ValueError('Explicit paths are allowed only for redacted references')
    expected = {e['path'] for e in entries if e['storage'] != 'reference'}
    if expected - actual.keys():
        raise ValueError(f'Missing artifact: {sorted(expected - actual.keys())[0]}')
    if actual.keys() - expected:
        raise ValueError(f'Unexpected artifact: {sorted(actual.keys() - expected)[0]}')
    members = {}
    for item in entries:
        identity = item['artifact_id']
        if item['storage'] == 'reference':
            if not external:
                continue
            path = _resolve_reference(item, overrides)
        else:
            path = actual[item['path']]
        if _facts(path, identity, stored=item['storage'] != 'reference') != (item['size_bytes'], item['sha256']):
            raise ValueError(f'Artifact size or SHA-256 mismatch: {identity}')
        members[identity] = path
    return members


def _verify(root, expected_manifest_sha256=None, reference_paths=None, *, external=True):
    manifest, digest = _open_manifest(root, expected_manifest_sha256)
    members = _verify_members(manifest, _files(root, manifest['kind']), reference_paths, external=external)
    _check_summary(json.loads((root / _SUMMARY).read_bytes()), manifest)
    return manifest, digest, members


def restore_computation(root, snapshot, run_id, identity):
    """Restore only the staged envelope and discard uncommitted report files."""
    root, snapshot = artifact_path(root), artifact_path(snapshot)
    manifest, _ = _open_manifest(snapshot, identity['artifact_manifest_sha256'])
    content = (snapshot / _SUMMARY).read_bytes()
    if hashlib.sha256(content).hexdigest() != identity['summary_sha256']:
        raise ValueError('Computation summary SHA-256 mismatch')
    summary = json.loads(content)
    if (manifest['run_id'] != run_id or manifest['kind'] != 'run'
            or delivered_result_files(summary) != CORE_RESULT_FILES):
        raise ValueError('Invalid sealed computation identity')
    _check_summary(summary, manifest)
    actual = _files(root, 'run')
    reports = actual.keys() & _REPORT_ARTIFACTS
    core = {name: path for name, path in actual.items() if name not in reports}
    core[_SUMMARY] = snapshot / _SUMMARY
    _verify_members(manifest, core, external=False)
    for name in reports:
        actual[name].unlink()
    for name in (_SUMMARY, MANIFEST, SEAL):
        target = root / name
        target.unlink(missing_ok=True)
        with target.open('xb') as stream:
            stream.write((snapshot / name).read_bytes())
    return summary


def verify_artifacts(directory, *, expected_manifest_sha256=None, reference_paths=None):
    """Verify a prediction or archive and return its manifest."""
    return _verify(artifact_path(directory), expected_manifest_sha256, reference_paths)[0]


def verify_published_artifacts(directory, *, run_id, manifest_sha256):
    """Verify the sealed result files during launcher reconciliation."""
    manifest, _, _ = _verify(artifact_path(directory), manifest_sha256, external=False)
    if manifest['kind'] != 'run' or manifest['run_id'] != run_id:
        raise ValueError('Publication artifact identity mismatch')
