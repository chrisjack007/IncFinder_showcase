"""Shared identity and package-relative path contracts."""
from pathlib import Path
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import sys

from .encoder_source import CODE_PATHS, code_source
from .resources import ROOT, CODE_ROOT, INSTALLED, checked_child, model_root, model_location


_PRIVATE_PATH = re.compile(
    r'[\x00-\x1f\x7f]|[a-z][a-z0-9+.-]+:/|'
    r'(?:token|api[_-]?key|password|passwd|secret|credential|authorization|signature)\s*[:=]|'
    r'\bbearer\s+|\b(?:gh[pousr]_|github_pat_|hf_|sk-)[a-z0-9_-]{8,}',
    re.IGNORECASE,
)


def record_path_value(path):
    """Keep local paths, dropping a whole field when it resembles credentials."""
    value = os.fsdecode(path)
    return '[redacted]' if _PRIVATE_PATH.search(value) else value


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def resolve_path(value, base=None):
    path = Path(value).expanduser()
    return ((Path.cwd() if base is None else Path(base)) / path).resolve() if not path.is_absolute() else path.resolve()


def asset_path(relative, model_dir=None):
    if Path(relative).as_posix() in CODE_PATHS and code_source(ROOT):
        return checked_child(model_root(model_dir), Path('ntv3') / Path(relative).name)
    parts = Path(relative).parts
    if parts and parts[0] == 'models':
        return checked_child(model_root(model_dir), Path(*parts[1:]))
    return checked_child(ROOT, relative)


def verify_assets(quick=False, model_dir=None):
    entries = read_json(ROOT / 'config/assets.json')['files']
    seen = set()
    for entry in entries:
        name = entry['path']
        if name in seen:
            raise ValueError(f'Duplicate model asset: {name}')
        seen.add(name)
        path = asset_path(name, model_dir)
        if not path.is_file() or path.stat().st_size != entry['size_bytes']:
            raise ValueError(f'Model file missing or size mismatch: {name}')
        if not quick and sha256(path) != entry['sha256']:
            raise ValueError(f'Model SHA-256 mismatch: {name}')
    required = {'models/rf/stage1.joblib', 'models/rf/stage2.joblib',
                'models/ntv3/model.safetensors', 'config/thresholds.tsv',
                'vendor/ntv3/modeling_ntv3_pretrained.py',
                'vendor/ntv3/configuration_ntv3_pretrained.py',
                'vendor/ntv3/tokenization_ntv3.py'}
    if not required.issubset(seen):
        raise ValueError('The asset manifest is missing files required for inference')
    return {'status': 'SIZE_CHECK_ONLY' if quick else 'PASS', 'files': len(entries),
            'bytes': sum(x['size_bytes'] for x in entries), 'manifest_sha256': sha256(ROOT / 'config/assets.json')}


def verify_package(model_dir=None):
    """Installed code/resources and source trees have separate manifests."""
    if INSTALLED:
        manifest = read_json(ROOT / 'software_manifest.json')
        names = set()
        for entry in manifest['files']:
            name = entry['path']
            if Path(name).parts[0] not in ('incfinder', 'ntv3_tsrf'):
                raise ValueError(f'Invalid installed-package manifest path: {name}')
            path = checked_child(CODE_ROOT, name)
            if name in names or not path.is_file() or path.stat().st_size != entry['size_bytes'] or sha256(path) != entry['sha256']:
                raise ValueError(f'Installed-package file missing, duplicated or hash mismatch: {name}')
            names.add(name)
        required = {'ntv3_tsrf/_resources/' + p for p in ('config/contract.json', 'config/assets.json', 'config/runtime.json', 'config/output_contract.json', 'config/thresholds.tsv', 'config/model_lineage.json', 'config/model_delivery.json', 'config/research_index.json', 'tests/fixtures/features.json')}
        if not required.issubset(names):
            raise ValueError('The installed-package manifest is missing required resources')
        return {'status': 'PASS', 'kind': 'installed', 'manifested_files': len(names)}
    manifest = read_json(ROOT / 'package_manifest.json')
    assets = {entry['path']: entry for entry in read_json(ROOT / 'config/assets.json')['files']}
    names = set()
    for entry in manifest['files']:
        path = asset_path(entry['path'], model_dir)
        if entry['path'] in names or not path.is_file() or path.stat().st_size != entry['size_bytes']:
            raise ValueError(f'Software file missing, duplicated or size mismatch: {entry["path"]}')
        names.add(entry['path'])
        if entry['path'] in assets:
            if entry['sha256'] != assets[entry['path']]['sha256']:
                raise ValueError('Software and model asset manifests disagree')
        elif sha256(path) != entry['sha256']:
            raise ValueError(f'Software file SHA-256 mismatch: {entry["path"]}')
    if not set(assets).issubset(names):
        raise ValueError('The software manifest does not cover all model assets')
    return {'status': 'PASS', 'kind': 'source', 'manifested_files': len(names)}


def load_thresholds(contract):
    with (ROOT / 'config/thresholds.tsv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    if ([r['model_key'] for r in rows] != ['nt_v3'] * 21 or
            [int(r['label_order']) for r in rows] != list(range(21)) or
            [r['label_name'] for r in rows] != contract['labels']):
        raise ValueError('Threshold model, order or labels do not match the contract')
    values = [float(r['threshold']) for r in rows]
    if any(not 0 < t <= 1 for t in values):
        raise ValueError('Thresholds must be in (0, 1]')
    return values


def runtime_diagnostics(role='all'):
    """Read distribution metadata only; do not import torch or probe accelerators."""
    policy = read_json(ROOT / 'config/runtime.json')
    roles = list(policy['roles']) if role == 'all' else (role if isinstance(role, tuple) else (role,))
    current = {'python': sys.version.split()[0]}
    errors = []
    if f'{sys.version_info.major}.{sys.version_info.minor}' not in policy['python']:
        errors.append(f"Python {current['python']} is unsupported; requires 3.11 or 3.12")
    required = dict.fromkeys(name for item in roles for name in policy['roles'][item])
    try:
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
    except ImportError:
        errors.append('Missing packaging; install the environment.yml or requirements dependencies')
        return {'status': 'FAIL', 'versions': current, 'errors': errors}
    for name in required:
        try:
            version = importlib.metadata.version(name)
            current[name] = version
            specifier = policy['requirements'][name]
            if version not in SpecifierSet(specifier):
                errors.append(f'{name}: found {version}; requires {specifier}')
        except importlib.metadata.PackageNotFoundError:
            errors.append(f'Missing dependency: {name}')
    if 'embedding' in roles and 'transformers' in current:
        checked = set()
        for text in importlib.metadata.requires('transformers') or []:
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
                continue
            if requirement.name not in policy['transformers_managed']:
                continue
            checked.add(requirement.name)
            actual = current.get(requirement.name)
            if actual and actual not in requirement.specifier:
                errors.append(f'{requirement.name}: found {actual}; Transformers requires {requirement.specifier}')
        if checked != set(policy['transformers_managed']):
            errors.append('Transformers metadata does not cover the tokenizers/safetensors/huggingface-hub dependency contract')
    return {'status': 'FAIL' if errors else 'PASS', 'versions': current, 'errors': errors}


def environment(role):
    result = runtime_diagnostics(role)
    if result['errors']:
        raise ValueError(f"{role} environment is incompatible: " + '; '.join(result['errors']) +
                         '; run incfinder doctor for diagnostics; the environment is not changed automatically')
    return result['versions']


def doctor(model_dir=None):
    """Metadata, source hashes and asset sizes; no model load or GPU probing."""
    result = runtime_diagnostics()
    result.update(software_root=str(ROOT), executable=sys.executable,
                  model_loaded=False, gpu_probed=False,
                  scientific_equivalence='NOT_ESTABLISHED', deployment='RESEARCH_PREVIEW')
    try:
        result['models'] = model_location(model_dir)
        result['package'] = verify_package(model_dir)
        result['assets'] = verify_assets(quick=True, model_dir=model_dir)
    except (ValueError, OSError, KeyError) as exc:
        result['errors'].append(str(exc))
    result['status'] = 'FAIL' if result['errors'] else 'PASS'
    return result


def offline_environment(device='cpu'):
    values = os.environ.copy()
    values.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                  PYTHONDONTWRITEBYTECODE='1', TOKENIZERS_PARALLELISM='false',
                  PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if device == 'cpu':
        values['CUDA_VISIBLE_DEVICES'] = '-1'
    return values
