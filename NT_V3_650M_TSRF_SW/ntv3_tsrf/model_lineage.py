"""Portable model lineage references and inference installation identity."""
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import unquote, urlparse

from .resources import path_is_link

POINTER = 'config/model_lineage.json'
MODEL_ROLES = {
    'models/rf/stage1.joblib': ('train_stage1', 'weight'),
    'models/rf/stage2.joblib': ('train_stage2', 'weight'),
    'models/ntv3/model.safetensors': ('encode_train', 'encoder_weights'),
    'config/thresholds.tsv': ('thresholds', 'threshold_table'),
}


def _unique(pairs):
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError('Duplicate keys in lineage JSON')
    return document


def load(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError('Expected a lineage JSON object')
    return value


def is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}', value):
        raise ValueError('Invalid lineage identifier')
    return value


def relative(value):
    if not isinstance(value, str) or any(
        not part or part in ('.', '..') or part.endswith((' ', '.'))
        or any(char in part for char in '\\:<>"|?*') or any(ord(char) < 32 for char in part)
        or re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', part.split('.')[0])
        for part in value.split('/')
    ):
        raise ValueError(f'Unsafe lineage path: {value!r}')
    return PurePosixPath(value)


def checked_path(root, name):
    base = Path(root).absolute()
    path = base.joinpath(*relative(name).parts)
    if any(path_is_link(part) for part in (path, *path.parents)) or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f'Linked or out-of-root evidence: {name}')
    return path


def fact(path):
    path = Path(path)
    if not path.is_file() or path_is_link(path):
        raise ValueError(f'Missing regular evidence file: {path}')
    before = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f'Evidence changed during hashing: {path}')
    return {'size_bytes': after.st_size, 'sha256': digest}


def identity(row):
    size, digest = row.get('size_bytes'), row.get('sha256')
    if type(size) is not int or size < 0 or not is_sha256(digest):
        raise ValueError('Invalid evidence size or SHA-256')
    return {'size_bytes': size, 'sha256': digest}


def locate(roots, reference):
    name = identifier(reference['root'])
    if name not in roots:
        raise ValueError(f'No restore location supplied for {name}')
    return checked_path(roots[name], reference['path'])


def verify_reference(roots, reference):
    if set(reference) != {'root', 'path', 'sha256', 'size_bytes'}:
        raise ValueError('Unexpected fields in evidence reference')
    path = locate(roots, reference)
    if fact(path) != identity(reference):
        raise ValueError(f'Lineage file size or SHA-256 mismatch: {reference["root"]}/{reference["path"]}')
    return path


def validate_pointer(value):
    if set(value) != {'schema', 'candidate_id', 'index_sha256', 'assets'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unexpected model lineage pointer schema')
    identifier(value['candidate_id'])
    if not is_sha256(value['index_sha256']) or set(value['assets']) != set(MODEL_ROLES):
        raise ValueError('Model lineage index or asset identities are incomplete')
    if any(identity(row) != row for row in value['assets'].values()):
        raise ValueError('Unexpected fields in model lineage assets')
    return value


def load_pointer(root):
    path = checked_path(root, POINTER)
    if not path.exists():
        return None
    value = validate_pointer(load(path))
    entries = load(checked_path(root, 'config/assets.json'))['files']
    assets = {row['path']: identity(row) for row in entries}
    if len(entries) != len(assets) or any(assets.get(name) != row for name, row in value['assets'].items()):
        raise ValueError('Packaged model lineage disagrees with the inference assets')
    return value


def training_jobs(root, binding):
    """Resolve this run's two RF jobs against its packaged research index."""
    path = checked_path(root, 'config/research_index.json')
    if not path.is_file():
        return None
    pointer = load_pointer(root)
    if (pointer is None or binding['candidate_id'] != pointer['candidate_id']
            or binding['index_sha256'] != pointer['index_sha256']
            or binding['pointer_sha256'] != fact(checked_path(root, POINTER))['sha256']
            or fact(path)['sha256'] != binding['index_sha256']):
        raise ValueError('Research index does not match the recorded prediction')
    index = load(path)
    if index['candidate_id'] != pointer['candidate_id']:
        raise ValueError('Research index has a different candidate identity')
    jobs = {}
    for layer in ('stage1', 'stage2'):
        stage = index['stages']['train_' + layer]
        weight = pointer['assets'][f'models/rf/{layer}.joblib']
        if stage['outputs']['weight'] != weight or stage['research_commit'] != index['research_commit']:
            raise ValueError('Training job does not match the recorded RF')
        jobs[layer] = {key: stage[key] for key in ('run_id', 'attempt_id', 'research_commit')}
        jobs[layer].update(weight_sha256=weight['sha256'], completion_sha256=stage['completion']['sha256'])
    return jobs


def _package_origin(kind, filename, digest):
    suffixes = {'conda': '.conda', 'wheel': '.whl', 'source': '.json'}
    if (kind not in suffixes or not isinstance(filename, str) or relative(filename).name != filename
            or not filename.endswith(suffixes[kind]) or not is_sha256(digest)):
        raise ValueError('Cannot identify the original IncFinder installation artifact')
    return dict(kind=kind, filename=filename, sha256=digest)


def _install_artifact():
    records = list((Path(sys.prefix) / 'conda-meta').glob('incfinder-*.json'))
    if records:
        if len(records) != 1:
            raise ValueError('Conflicting Conda installation records')
        row = load(records[0])
        return _package_origin('conda', row.get('fn'), row.get('sha256'))
    distribution = importlib.metadata.distribution('incfinder-ntv3-tsrf')
    row = json.loads(distribution.read_text('direct_url.json') or '{}', object_pairs_hook=_unique)
    filename = PurePosixPath(unquote(urlparse(row.get('url', '')).path)).name
    return _package_origin('wheel', filename, row.get('archive_info', {}).get('hashes', {}).get('sha256'))


def prediction_binding(root, *, installed, run_id, input_sha256):
    pointer = load_pointer(root)
    if pointer is None:
        return None
    name = 'software_manifest.json' if installed else 'package_manifest.json'
    manifest_hash = fact(checked_path(root, name))['sha256']
    origin = _install_artifact() if installed else _package_origin('source', name, manifest_hash)
    return validate_binding({
        'schema': 1, 'candidate_id': pointer['candidate_id'], 'index_sha256': pointer['index_sha256'],
        'pointer_sha256': fact(checked_path(root, POINTER))['sha256'], 'run_id': run_id,
        'input_sha256': input_sha256, 'software_manifest_sha256': manifest_hash, 'installation': origin,
    })


def validate_binding(value):
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
            or set(value) != {'schema', 'candidate_id', 'index_sha256', 'pointer_sha256', 'run_id',
                              'input_sha256', 'software_manifest_sha256', 'installation'}):
        raise ValueError('Unexpected inference lineage binding')
    identifier(value['candidate_id'])
    identifier(value['run_id'])
    if not all(is_sha256(value[key]) for key in
               ('index_sha256', 'pointer_sha256', 'input_sha256', 'software_manifest_sha256')):
        raise ValueError('Invalid inference lineage digests')
    origin = value['installation']
    if not isinstance(origin, dict) or origin != _package_origin(origin.get('kind'), origin.get('filename'), origin.get('sha256')):
        raise ValueError('Unexpected inference installation identity')
    return value
