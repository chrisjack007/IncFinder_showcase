"""Capture CycloneDX 1.6 SBOMs from the locked candidate install."""
import importlib.metadata
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import urlsplit

from build_release import VERSION, encoder_source
from release_gate import digest, read

ROOT = Path(__file__).resolve().parents[1]
TOOL_VERSION = '0.29.2'
TOOL_SHA256 = {
    'linux-64': '6c5ed6321ee3bb3c268b6d19477dbcd5509a757f118bed4b6508fe9fbbdc2a5b',
    'win-64': '1cebf468eeeb36d518a68eda6e2d67cfa4d20c4c2f3f9defe6d29a736cc7dfd7',
}
SPEC = '1.6'


def lock_file(target):
    if target not in TOOL_SHA256:
        raise ValueError(f'Unexpected SBOM platform: {target}')
    return ROOT / 'environments' / (target + '.lock')


def locked_inventory(lock):
    rows = [x.strip() for x in lock.read_text(encoding='utf-8').splitlines()
            if x.strip() and not x.startswith('#')]
    if not rows or rows.pop(0) != '@EXPLICIT':
        raise ValueError('Expected an explicit Conda lock')
    packages = {}
    for row in rows:
        url = urlsplit(row)
        filename = url.path.rsplit('/', 1)[-1]
        suffix = '.conda' if filename.endswith('.conda') else '.tar.bz2'
        bits = filename.removesuffix(suffix).rsplit('-', 2)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.query
                or not filename.endswith(suffix) or len(bits) != 3 or not all(bits)
                or not re.fullmatch('[0-9a-f]{64}', url.fragment) or filename in packages):
            raise ValueError(f'Invalid or duplicate locked Conda package: {filename}')
        packages[filename] = (*bits, url.fragment)
    if not packages:
        raise ValueError('Empty Conda lock')
    return packages


def _sha256(value):
    return [{'alg': 'SHA-256', 'content': value}]


def _properties(values):
    return [{'name': 'incfinder:' + key, 'value': str(value)} for key, value in sorted(values.items())]


def vendor_inventory(source_manifest):
    manifest = read(source_manifest)
    external = encoder_source.external_manifest_paths(manifest)
    return {row['path']: row['sha256'] for row in manifest['files']
            if row['path'].startswith('vendor/') and row['path'] not in external}


def expected_bom(receipt, source_manifest):
    kind = receipt['package_kind']
    method = {'wheel': 'pip-wheel', 'conda': 'conda-package'}[kind]
    if receipt['installation_method'] != method:
        raise ValueError('SBOM installation method does not match its receipt')
    lock = lock_file(receipt['platform'])
    packages = locked_inventory(lock)
    components = [
        {'type': 'library', 'bom-ref': 'conda:' + filename, 'name': name,
         'version': version, 'hashes': _sha256(checksum), 'properties': _properties({'build': build})}
        for filename, (name, version, build, checksum) in sorted(packages.items())
    ]
    manifest = read(source_manifest)
    external = encoder_source.external_manifest_paths(manifest)
    components.extend(
        {'type': 'file', 'scope': 'required', 'bom-ref': 'upstream:' + row['path'],
         'name': row['path'], 'hashes': _sha256(row['sha256']),
         'properties': _properties({**(encoder_source.CODE_SOURCE if row['path'] in encoder_source.CODE_PATHS
                                       else encoder_source.CHECKPOINT_SOURCE), 'delivery': 'authenticated upstream',
                                    'verification': 'models install or models verify'})}
        for row in sorted(manifest['files'], key=lambda entry: entry['path']) if row['path'] in external
    )
    components.extend(
        {'type': 'file', 'bom-ref': 'file:' + name, 'name': name, 'hashes': _sha256(checksum)}
        for name, checksum in sorted(vendor_inventory(source_manifest).items())
    )
    identity = {key: receipt[key] for key in ('repository', 'source_commit', 'run_id', 'run_attempt',
                'platform', 'package_kind', 'installation_method', 'installation_record_sha256',
                'source_manifest_sha256', 'build_receipt_sha256')}
    identity.update(lock_sha256=digest(lock), scope='locked-installed-environment')
    return {
        '$schema': 'http://cyclonedx.org/schema/bom-1.6.schema.json',
        'bomFormat': 'CycloneDX', 'specVersion': SPEC, 'version': 1,
        'metadata': {
            'component': {'type': 'application', 'bom-ref': 'incfinder-candidate',
                          'name': 'incfinder-ntv3-tsrf' if kind == 'wheel' else 'incfinder',
                          'version': VERSION, 'hashes': _sha256(receipt['artifact_sha256'])},
            'properties': _properties(identity),
        },
        'components': components,
    }


def installed_bom(receipt, source_manifest, resource_root):
    lock = lock_file(receipt['platform'])
    expected = locked_inventory(lock)
    observed = {}
    package_records = Path(sys.prefix) / 'conda-meta'
    for path in sorted(package_records.glob('*.json')):
        record = read(path)
        if record.get('name') == 'incfinder' and receipt['package_kind'] == 'conda':
            if record.get('version') != VERSION or record.get('sha256') != receipt['artifact_sha256']:
                raise ValueError('Conda candidate disagrees with the SBOM subject')
            continue
        filename = record['fn']
        if filename in observed:
            raise ValueError(f'Duplicate installed Conda package: {filename}')
        observed[filename] = tuple(record.get(k) for k in ('name', 'version', 'build', 'sha256'))
    if observed != expected:
        mismatch = sorted(n for n in observed.keys() | expected.keys() if observed.get(n) != expected.get(n))
        raise ValueError(f'Installed Conda package inventory differs from the lock: {mismatch[:8]}')
    distribution = importlib.metadata.distribution('incfinder-ntv3-tsrf')
    if distribution.version != VERSION:
        raise ValueError('Installed distribution version disagrees with the SBOM subject')
    vendor = resource_root / 'vendor'
    installed = {path.relative_to(resource_root).as_posix(): digest(path)
                 for path in vendor.rglob('*') if path.is_file() and path.suffix != '.pyc'}
    if installed != vendor_inventory(source_manifest):
        raise ValueError('Installed vendor inventory differs from the source manifest')
    return expected_bom(receipt, source_manifest)


def validate_boms(tool, bom_paths):
    target = {'Windows': 'win-64', 'Linux': 'linux-64'}.get(platform.system())
    executable = Path(tool).resolve(strict=True)
    if target not in TOOL_SHA256 or digest(executable) != TOOL_SHA256[target]:
        raise ValueError(f'Expected the pinned CycloneDX CLI {TOOL_VERSION} for this platform')
    for path in bom_paths:
        result = subprocess.run([str(executable), 'validate', '--input-file', str(path),
                                 '--input-format', 'json', '--input-version', 'v1_6', '--fail-on-errors'],
                                capture_output=True, timeout=45)
        if result.returncode:
            raise ValueError(f'CycloneDX 1.6 schema validation failed: {path.name}')
