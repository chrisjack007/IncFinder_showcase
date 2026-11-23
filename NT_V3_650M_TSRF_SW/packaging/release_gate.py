"""Fail-closed candidate checks; this module never publishes a GitHub release."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('win-64', 'linux-64')
KINDS = ('wheel', 'conda')
MODES = ('ct', 'u05')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Receipt must be an object: {path}')
    return value


def contracts():
    facts = {name + '_sha256': digest(ROOT / 'config' / filename) for name, filename in (
        ('scientific_contract', 'contract.json'), ('assets_manifest', 'assets.json'),
        ('output_contract', 'output_contract.json'))}
    pointer = ROOT / 'config/model_lineage.json'
    if pointer.exists():
        facts['model_lineage_sha256'] = digest(pointer)
    return facts


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def check_receipt(value, kind, identity):
    if value.get('schema') != 1 or type(value.get('schema')) is not int or value.get('kind') != kind:
        raise ValueError(f'Unknown or conflicting receipt format: {kind}')
    if value.get('status') != 'PASS':
        raise ValueError(f'Failed or incomplete receipt: {kind}')
    for key, expected in identity.items():
        if value.get(key) != expected or type(value.get(key)) is not type(expected):
            raise ValueError(f'Receipt identity mismatch: {kind}/{key}')
    if not _hash(value.get('source_manifest_sha256')):
        raise ValueError('Missing source manifest identity')
    if not value.get('created_at_utc') or not value.get('python'):
        raise ValueError('Missing execution context')


def _unique(values, key, expected, kind):
    result = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f'Malformed {kind} evidence')
        name = key(value)
        if name in result:
            raise ValueError(f'Duplicate {kind} evidence: {name}')
        result[name] = value
    if set(result) != set(expected):
        raise ValueError(f'Missing or unexpected {kind} evidence: {sorted(result)}')
    return result


def verify_candidate(build_path, receipt_paths, artifacts, *, source_commit, run_id, run_attempt, repository):
    """Require one current workflow attempt, exact packages and complete evidence.

    Receipts are workflow evidence, not independently authenticated signatures.
    The caller must obtain them from its current trusted GitHub workflow run.
    Local or dirty-tree receipts deliberately cannot authorize publication.
    """
    if not re.fullmatch('[0-9a-f]{40}', source_commit):
        raise ValueError('A complete source commit SHA is required')
    if not str(run_id).isdigit() or int(run_id) <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        raise ValueError('A real workflow run and positive attempt are required')
    identity = dict(source_commit=source_commit, run_id=str(run_id), run_attempt=run_attempt,
                    repository=repository, execution_context='github-actions', source_clean=True,
                    **contracts())
    build = read(build_path)
    check_receipt(build, 'build', identity)
    if not _hash(build.get('software_manifest_sha256')):
        raise ValueError('Missing installed software manifest identity')
    packages = _unique(build.get('artifacts', []), lambda x: x.get('package_kind'), KINDS, 'package')
    if len(artifacts) != 2:
        raise ValueError('Exactly the original wheel and Conda package are required')
    actual = {}
    for path in artifacts:
        path = Path(path)
        if not path.is_file() or path.name in actual:
            raise ValueError('Missing or duplicated candidate file')
        actual[path.name] = digest(path)
    for kind, entry in packages.items():
        name = entry.get('file')
        if (not isinstance(name, str) or Path(name).name != name
                or not _hash(entry.get('sha256')) or actual.get(name) != entry['sha256']
                or not name.endswith('.whl' if kind == 'wheel' else '.conda')):
            raise ValueError(f'Candidate package digest or filename mismatch: {kind}')
    if set(actual) != {entry['file'] for entry in packages.values()}:
        raise ValueError('Unexpected candidate package files')
    build_hash = digest(build_path)
    grouped = {'unit': [], 'installed': [], 'frozen': []}
    seen_paths = set()
    for path in receipt_paths:
        path = Path(path).resolve()
        if path in seen_paths:
            raise ValueError('Duplicated receipt path')
        seen_paths.add(path)
        value = read(path)
        kind = value.get('kind')
        if kind not in grouped:
            raise ValueError(f'Unexpected evidence kind: {kind}')
        check_receipt(value, kind, identity)
        if value['source_manifest_sha256'] != build['source_manifest_sha256']:
            raise ValueError('Evidence belongs to a different source manifest')
        if kind == 'unit':
            if type(value.get('tests_run')) is not int or value['tests_run'] <= 0 or value.get('tests_successful') is not True:
                raise ValueError('Unit regression did not complete')
        else:
            if value.get('build_receipt_sha256') != build_hash:
                raise ValueError('Stale evidence from a different candidate build')
            package = packages.get(value.get('package_kind'))
            if not package or value.get('artifact_sha256') != package['sha256']:
                raise ValueError('Evidence belongs to a different package')
            if (value.get('installed_package') is not True or value.get('package_integrity') is not True
                    or value.get('software_manifest_sha256') != build['software_manifest_sha256']):
                raise ValueError('Installed package identity or integrity failed')
            if (value.get('installation_method') != {'wheel': 'pip-wheel', 'conda': 'conda-package'}[value['package_kind']]
                    or not _hash(value.get('installation_record_sha256'))):
                raise ValueError('Package-format installation proof is missing or inconsistent')
            if value.get('sidecars_validated') != 6 or type(value.get('sidecars_validated')) is not int:
                raise ValueError('Incomplete output metadata validation')
            if kind == 'installed' and value.get('reference_conversion_exact') is not True:
                raise ValueError('Installed reference conversion failed')
            if kind == 'frozen' and (value.get('trusted_runner') is not True
                    or value.get('arrays_exact') is not True or value.get('tables_exact') is not True
                    or value.get('scientific_equivalence') != 'FROZEN_REFERENCE'
                    or value.get('encoder_run') is not False or value.get('device') != 'cpu'):
                raise ValueError('Frozen RF acceptance failed or changed scientific scope')
        grouped[kind].append(value)
    _unique(grouped['unit'], lambda x: x.get('platform'), PLATFORMS, 'platform regression')
    _unique(grouped['installed'], lambda x: (x.get('platform'), x.get('package_kind')),
            [(platform, kind) for platform in PLATFORMS for kind in KINDS], 'installed package')
    frozen = _unique(grouped['frozen'], lambda x: (x.get('package_kind'), x.get('threshold_mode')),
                     [(kind, mode) for kind in KINDS for mode in MODES], 'frozen mode')
    frozen_platforms = {value.get('platform') for value in frozen.values()}
    if len(frozen_platforms) != 1 or not frozen_platforms.issubset(PLATFORMS):
        raise ValueError('Frozen checks must identify one actual trusted platform')
    return {'schema': 1, 'status': 'PASS', 'eligible': True, 'source_commit': source_commit,
            'run_id': str(run_id), 'run_attempt': run_attempt, 'build_receipt_sha256': build_hash,
            'scientific_release_status': 'RESEARCH_PREVIEW', 'release_published': False,
            'scope': 'Candidate eligibility only; raw-encoder equivalence is not established'}
