"""Assemble unsigned release evidence and verify an independently signed copy."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from build_release import VERSION, encoder_source
from lineage_run import assignments, verify_index
from ntv3_tsrf.model_lineage import POINTER, identity as file_identity, validate_pointer
from release_gate import digest, verify_candidate
from release_references import REPOSITORY, check_v1_snapshot, live_v1_reference
from release_sbom import SPEC, TOOL_SHA256, TOOL_VERSION, expected_bom, lock_file, validate_boms

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'release-manifest.json'
SIGNATURE = MANIFEST + '.sig'
SIGNER = 'incfinder-release'
ARCHIVES = (f'incfinder-{VERSION}-win-64.zip', f'incfinder-{VERSION}-linux-64.tar.gz')
ARCHIVE_PREFIX = f'incfinder-{VERSION}/'
TOP_FILES = {'build.json', 'decision.json', 'source-manifest.json', 'v1-assets.json'}
MAX_SOURCE_FILE = 10_000_000


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('Duplicate JSON object keys in release evidence')
    return value


def _json_object(payload):
    value = json.loads(payload.decode('utf-8'), object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object in release evidence')
    return value


def json_object(path):
    return _json_object(path.read_bytes())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _ordinary_path(path):
    path = Path(path).absolute()
    if any(_link(part) for part in (path, *path.parents)):
        raise ValueError(f'Links are not allowed in release paths: {path.name}')
    return path.resolve()


def _relative_name(name):
    if not isinstance(name, str) or any(
        part in ('.', '..') or part.endswith('.') or
        not re.fullmatch('[A-Za-z0-9_.-]+', part) or
        re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', part.split('.')[0])
        for part in name.split('/')
    ):
        raise ValueError(f'Unsafe release member name: {name!r}')
    return name


def _fact(path):
    if not path.is_file() or _link(path):
        raise ValueError(f'Missing or non-regular release file: {path.name}')
    return {'size_bytes': path.stat().st_size, 'sha256': digest(path)}


def _file_inventory(directory, *, signed=False):
    rows = {}
    seen = set()
    for parent, dirs, names in os.walk(directory):
        parent = Path(parent)
        if any(_link(parent / name) for name in dirs + names):
            raise ValueError('Links are not allowed in a release bundle')
        for name in dirs + names:
            relative = _relative_name((parent / name).relative_to(directory).as_posix())
            if relative.casefold() in seen:
                raise ValueError(f'Case-colliding release member: {relative}')
            seen.add(relative.casefold())
        for name in names:
            path = parent / name
            relative = path.relative_to(directory).as_posix()
            if signed and relative in (MANIFEST, SIGNATURE):
                continue
            rows[relative] = _fact(path)
    return rows


def _archive_rows(path):
    rows = {}

    def add(name, size, entry):
        if not name.startswith(ARCHIVE_PREFIX):
            raise ValueError(f'Unexpected archive root: {name}')
        relative = _relative_name(name.removeprefix(ARCHIVE_PREFIX))
        if relative.casefold() in rows or size < 0 or size > MAX_SOURCE_FILE or len(rows) >= 10000:
            raise ValueError(f'Duplicate or oversized archive member: {relative}')
        with entry() as stream:
            data = stream.read(size + 1)
        if len(data) != size:
            raise ValueError(f'Wrong archive member size: {relative}')
        rows[relative.casefold()] = (relative, {'size_bytes': size, 'sha256': hashlib.sha256(data).hexdigest()})

    if path.name.endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                mode = stat.S_IFMT(member.external_attr >> 16)
                if member.is_dir() or mode not in (0, stat.S_IFREG) or member.orig_filename != member.filename:
                    raise ValueError(f'Non-regular ZIP member: {member.filename}')
                add(member.filename, member.file_size, lambda: archive.open(member))
    else:
        with tarfile.open(path, mode='r:gz') as archive:
            for member in archive:
                if not member.isreg() or member.issparse():
                    raise ValueError(f'Non-regular tar member: {member.name}')
                add(member.name, member.size, lambda: archive.extractfile(member))
    return dict(rows.values())


def archive_members(path, source_manifest, packages):
    sources = {}
    names = set()
    manifest = json_object(source_manifest)
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), list):
        raise ValueError('Unexpected source manifest format')
    external = encoder_source.external_manifest_paths(manifest)
    for row in manifest['files']:
        name = _relative_name(row['path'])
        if name.casefold() in names:
            raise ValueError(f'Duplicate source manifest member: {name}')
        names.add(name.casefold())
        if name in external or (name.startswith('models/') and name.endswith(('.joblib', '.safetensors'))):
            continue
        sources[name] = {key: row[key] for key in ('size_bytes', 'sha256')}
    sources['package_manifest.json'] = _fact(source_manifest)
    rows = _archive_rows(path)
    forbidden = {(r['size_bytes'], r['sha256']) for r in manifest['files'] if r['path'] in external}
    for name, fact in rows.items():
        if (fact['size_bytes'], fact['sha256']) in forbidden:
            raise ValueError('Portable archive embeds an upstream-only asset: ' + name)
    for name, facts in sources.items():
        if rows.get(name) != facts:
            raise ValueError(f'Archive source member differs or is missing: {name}')
    for kind, package in packages.items():
        folder = 'dist/wheels/' if kind == 'wheel' else 'dist/channel/noarch/'
        if rows.get(folder + package.name) != _fact(package):
            raise ValueError(f'Archive embedded {kind} does not match the candidate package')
    for name in rows.keys() - sources.keys():
        if not name.startswith(('dist/wheels/', 'dist/channel/')) or (
            name.endswith(('.whl', '.conda', '.tar.bz2')) and
            name not in {'dist/wheels/' + packages['wheel'].name, 'dist/channel/noarch/' + packages['conda'].name}
        ):
            raise ValueError(f'Unexpected portable archive member: {name}')
    return rows


def archive_records(paths, source_manifest, packages):
    if sorted(path.name for path in paths) != sorted(ARCHIVES):
        raise ValueError('Expected the original Windows ZIP and Linux tar.gz')
    inventories = [archive_members(path, source_manifest, packages) for path in paths]
    if inventories[0] != inventories[1]:
        raise ValueError('Windows and Linux portable archives contain different files')
    members_sha256 = hashlib.sha256(json_bytes(inventories[0])).hexdigest()
    return [dict(_fact(path), file=path.name, member_inventory_sha256=members_sha256)
            for path in sorted(paths)]


def _packages(root, rows):
    grouped = {kind: [root / name for name in rows if name.startswith('packages/') and
                       name.count('/') == 1 and name.endswith(suffix)]
               for kind, suffix in (('wheel', '.whl'), ('conda', '.conda'))}
    if any(len(group) != 1 for group in grouped.values()):
        raise ValueError('Expected exactly one wheel and one Conda package')
    return {kind: group[0] for kind, group in grouped.items()}


def _validate_identity(identity):
    if (identity['repository'] != REPOSITORY or not re.fullmatch('[0-9a-f]{40}', identity['source_commit'])
            or not re.fullmatch('[1-9][0-9]*', identity['run_id']) or type(identity['run_attempt']) is not int
            or identity['run_attempt'] < 1 or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identity['release'])):
        raise ValueError('Expected an explicit release, repository, commit, run and attempt')


def _wheel_encoder_source(wheel, source_manifest):
    manifest = json_object(source_manifest)
    external = encoder_source.external_manifest_paths(manifest)
    policies = [row for row in manifest['files'] if row['path'] == 'config/model_delivery.json']
    if not policies:
        if VERSION.startswith('0.5.'):
            raise ValueError('IncFinder 0.5 requires a signed candidate model catalog')
        return
    if len(policies) != 1:
        raise ValueError('Expected one candidate model catalog')
    with zipfile.ZipFile(wheel) as archive:
        name = 'ntv3_tsrf/_resources/config/model_delivery.json'
        matches = [item for item in archive.infolist() if item.filename == name]
        if len(matches) != 1 or matches[0].file_size > MAX_SOURCE_FILE:
            raise ValueError('Expected one candidate model catalog in the wheel')
        payload = archive.read(matches[0])
        if len(payload) != policies[0]['size_bytes'] or hashlib.sha256(payload).hexdigest() != policies[0]['sha256']:
            raise ValueError('Wheel model catalog differs from the signed source')
        catalog = _json_object(payload)
        source = encoder_source.catalog_source(catalog)
        if external == encoder_source.UPSTREAM_PATHS and catalog.get('ntv3') != encoder_source.CHECKPOINT_SOURCE:
            raise ValueError('Checkpoint revision differs from the external NTv3 inventory')
        if (VERSION.startswith('0.5.') and source is None) or bool(source) != bool(external):
            raise ValueError('Upstream code delivery differs between the wheel and source inventory')
        if not external:
            return
        by_size = {}
        for row in manifest['files']:
            if row['path'] in external:
                by_size.setdefault(row['size_bytes'], set()).add(row['sha256'])
        for item in archive.infolist():
            if item.file_size in by_size:
                with archive.open(item) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() in by_size[item.file_size]:
                        raise ValueError('Wheel embeds an asset designated for authenticated upstream delivery')


def _wheel_lineage(wheel, source_manifest):
    expected = [row for row in json_object(source_manifest)['files'] if row['path'] == POINTER]
    if not expected:
        return None
    with zipfile.ZipFile(wheel) as archive:
        name = 'ntv3_tsrf/_resources/' + POINTER
        entries = [entry for entry in archive.infolist() if entry.filename == name]
        if len(entries) != 1 or len(expected) != 1 or entries[0].file_size > MAX_SOURCE_FILE:
            raise ValueError('Expected one model lineage pointer in the wheel and the source manifest')
        payload = archive.read(entries[0])
        facts = {'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        if facts != file_identity(expected[0]):
            raise ValueError('Wheel model lineage differs from the source identity')
        pointer = validate_pointer(_json_object(payload))
        assets = _json_object(archive.read('ntv3_tsrf/_resources/config/assets.json'))['files']
        asset_facts = {row['path']: file_identity(row) for row in assets}
        if len(asset_facts) != len(assets) or any(asset_facts.get(name) != row for name, row in pointer['assets'].items()):
            raise ValueError('Wheel model lineage is bound to different assets')
        return payload


def candidate_manifest(root, identity, validator, *, signed=False):
    _validate_identity(identity)
    facts = _file_inventory(root, signed=signed)
    if not TOP_FILES.issubset(facts):
        raise ValueError('Missing release evidence documents')
    build = json_object(root / 'build.json')
    manifest_path = root / 'source-manifest.json'
    if facts['source-manifest.json']['sha256'] != build.get('source_manifest_sha256'):
        raise ValueError('Build source manifest digest mismatch')
    locks = {target: digest(lock_file(target)) for target in TOOL_SHA256}
    tools = build.get('build_tools')
    if (build.get('lock_sha256') != locks or
            build.get('build_environment_sha256') != digest(ROOT / 'environments/ci-build.yml') or
            not isinstance(tools, dict) or set(tools) != {'pip', 'setuptools', 'wheel', 'conda-build', 'conda-index'}
            or any(not isinstance(v, str) or not v for v in tools.values())):
        raise ValueError('Build lock or toolchain identity missing or different')
    if build.get('packaged_source_manifest_sha256') != facts['source-manifest.json']['sha256']:
        raise ValueError('Build source identity changed from the packaged source')
    external = encoder_source.external_manifest_paths(json_object(manifest_path))
    if external == encoder_source.UPSTREAM_PATHS:
        proof = build.get('upstream_git_tree', {})
        if (not isinstance(proof, dict) or proof.get('revision') != identity['source_commit']
                or not re.fullmatch('[0-9a-f]{40}', str(proof.get('tree', '')))
                or proof.get('upstream_files') != len(external) or proof.get('matches') != 0):
            raise ValueError('Build receipt does not verify the release commit against the upstream-only assets')
    packages = _packages(root, facts)
    _wheel_encoder_source(packages['wheel'], manifest_path)
    for entry in build.get('artifacts', []):
        kind = entry.get('package_kind')
        if kind in packages and entry.get('size_bytes') != _fact(packages[kind])['size_bytes']:
            raise ValueError(f'Candidate {kind} size differs from its build receipt')
    receipts = [root / name for name in sorted(facts) if name.startswith('evidence/') and name.endswith('/receipt.json')]
    expected_files = TOP_FILES | {p.relative_to(root).as_posix() for p in (*packages.values(), *receipts)}
    payload = _wheel_lineage(packages['wheel'], manifest_path)
    if payload is None and build.get('model_lineage_sha256') is not None:
        raise ValueError('Build declares a model lineage absent from its source manifest')
    if payload is not None:
        model = root / 'model-lineage.json'
        if (model.read_bytes() != payload
                or json_object(root / 'lineage-verification.json') != dict(_json_object(payload), status='PASS')
                or build.get('model_lineage_sha256') != facts[model.name]['sha256']):
            raise ValueError('Model lineage in the release differs from its verification or build')
        expected_files.update(('model-lineage.json', 'lineage-verification.json'))
    gate_identity = {key: value for key, value in identity.items() if key != 'release'}
    decision = verify_candidate(root / 'build.json', receipts, list(packages.values()), **gate_identity)
    if json_object(root / 'decision.json') != decision:
        raise ValueError('Candidate gate decision differs from the verified run')
    paths = [root / 'archives' / name for name in ARCHIVES]
    expected_files.update(p.relative_to(root).as_posix() for p in paths)
    if archive_records(paths, manifest_path, packages) != build.get('archives'):
        raise ValueError('Archive identity differs from the candidate build')
    bom_paths = []
    for receipt_path in receipts:
        receipt = json_object(receipt_path)
        if receipt['kind'] != 'installed':
            continue
        bom = receipt_path.with_name('sbom.cdx.json')
        relative = bom.relative_to(root).as_posix()
        if relative not in facts or receipt.get('sbom_sha256') != facts[relative]['sha256']:
            raise ValueError(f'Missing or changed SBOM for {receipt["platform"]}/{receipt["package_kind"]}')
        if json_object(bom) != expected_bom(receipt, manifest_path):
            raise ValueError(f'SBOM does not match its locked install: {receipt["platform"]}/{receipt["package_kind"]}')
        bom_paths.append(bom)
        expected_files.add(relative)
    if set(facts) != expected_files:
        raise ValueError(f'Unexpected or missing release files: {sorted(set(facts) ^ expected_files)}')
    snapshot = json_object(root / 'v1-assets.json')
    check_v1_snapshot(snapshot)
    built = datetime.fromisoformat(build['created_at_utc'])
    if built.tzinfo is None or datetime.fromisoformat(snapshot['checked_at_utc']) < built:
        raise ValueError('V1 asset check predates the candidate build')
    validate_boms(validator, bom_paths)
    return {'schema': 1, **identity, 'lock_sha256': locks,
            'sbom_validation': {'spec_version': SPEC, 'cyclonedx_cli': TOOL_VERSION,
                                'binary_sha256': TOOL_SHA256},
            'files': [dict(fact, path=name) for name, fact in sorted(facts.items())]}


def _ssh_keygen():
    if platform.system() == 'Windows':
        return Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/OpenSSH/ssh-keygen.exe'
    return Path('/usr/bin/ssh-keygen')


def verify_signature(bundle, trust_file):
    bundle = _ordinary_path(bundle)
    trust = _ordinary_path(trust_file)
    if trust.is_relative_to(bundle) or not trust.is_file():
        raise ValueError('An external allowed_signers trust file is required')
    manifest, signature = (bundle / name for name in (MANIFEST, SIGNATURE))
    for path in (manifest, signature):
        if not path.is_file() or _link(path):
            raise ValueError(f'Missing or non-regular release signature input: {path.name}')
    payload = manifest.read_bytes()
    result = subprocess.run([str(_ssh_keygen()), '-Y', 'verify', '-n', SIGNER, '-I', SIGNER,
                             '-f', str(trust), '-s', str(signature)], input=payload, capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError('Release manifest signature was not accepted by the external trust file')
    return payload


def verify_bundle(bundle, trust_file, identity, validator):
    bundle = _ordinary_path(bundle)
    validator = _ordinary_path(validator)
    if validator.is_relative_to(bundle):
        raise ValueError('The pinned SBOM validator must be external to the release bundle')
    recorded = _json_object(verify_signature(bundle, trust_file))
    _validate_identity(identity)
    if (type(recorded.get('schema')) is not int or recorded['schema'] != 1 or
            any(recorded.get(key) != value or type(recorded.get(key)) is not type(value)
                for key, value in identity.items())):
        raise ValueError('Signed manifest does not identify the requested release and attempt')
    facts = _file_inventory(bundle, signed=True)
    declared = [dict(fact, path=name) for name, fact in sorted(facts.items())]
    if recorded.get('files') != declared:
        raise ValueError('Signed release file inventory, size or SHA-256 mismatch')
    if json_bytes(recorded) != json_bytes(candidate_manifest(bundle, identity, validator, signed=True)):
        raise ValueError('Verified candidate provenance does not match the signed manifest')
    return recorded


def prepare(candidate, evidence, decision, output, identity, validator, token, *, lineage_index=None, locations=None):
    _validate_identity(identity)
    candidate, evidence, decision = map(_ordinary_path, (candidate, evidence, decision))
    output = _ordinary_path(output)
    if output.exists() or output.is_relative_to(ROOT) or not output.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('Expected a new release evidence output in system temp, outside the source')
    with tempfile.TemporaryDirectory(prefix='release-evidence-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'bundle'
        staged.mkdir()
        for name, source in (('build.json', candidate / 'receipt.json'),
                             ('source-manifest.json', candidate / 'source-manifest.json'),
                             ('decision.json', decision)):
            shutil.copyfile(_ordinary_path(source), staged / name)
        for group in ('packages', 'archives'):
            source = candidate / group
            shutil.copytree(source, staged / group, symlinks=True)
        for relative in _file_inventory(evidence):
            path = evidence / relative
            if path.name in ('receipt.json', 'sbom.cdx.json'):
                destination = staged / 'evidence' / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        facts = _file_inventory(staged)
        wheel = _packages(staged, facts)['wheel']
        payload = _wheel_lineage(wheel, staged / 'source-manifest.json')
        if payload is not None:
            if lineage_index is None:
                raise ValueError('A traced release requires the actual research lineage index and restore locations')
            pointer = verify_index(_ordinary_path(lineage_index), locations or {}, _json_object(payload))
            (staged / 'model-lineage.json').write_bytes(payload)
            (staged / 'lineage-verification.json').write_bytes(json_bytes(dict(pointer, status='PASS')))
        elif lineage_index is not None:
            raise ValueError('This build has no packaged model lineage pointer')
        (staged / 'v1-assets.json').write_bytes(json_bytes(live_v1_reference(token)))
        value = candidate_manifest(staged, identity, _ordinary_path(validator))
        (staged / MANIFEST).write_bytes(json_bytes(value))
        staged.rename(output)
    return value


def verify_result(result, bundle, *, lineage_index=None, locations=None):
    """Match a result originating from an already verified signed release."""
    from ntv3_tsrf.output_metadata import load_output_metadata

    binding = load_output_metadata(result).get('model_lineage')
    model = bundle / 'model-lineage.json'
    pointer = validate_pointer(json_object(model))
    build = json_object(bundle / 'build.json')
    if (binding is None or binding['candidate_id'] != pointer['candidate_id']
            or binding['index_sha256'] != pointer['index_sha256']
            or binding['pointer_sha256'] != digest(model)):
        raise ValueError('Result does not identify this release model lineage')
    origin = binding['installation']
    if origin['kind'] == 'source':
        if (origin['sha256'] != build['source_manifest_sha256']
                or binding['software_manifest_sha256'] != build['source_manifest_sha256']):
            raise ValueError('Result does not identify the verified release source')
    elif (binding['software_manifest_sha256'] != build['software_manifest_sha256']
          or not any((row['package_kind'], row['file'], row['sha256']) ==
                     (origin['kind'], origin['filename'], origin['sha256']) for row in build['artifacts'])):
        raise ValueError('Result does not identify an installed package of this release')
    if lineage_index is not None:
        verify_index(_ordinary_path(lineage_index), locations or {}, pointer)
    return binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    generate = modes.add_parser('prepare', help='Read the current gate and assemble an unsigned candidate')
    for opt in ('candidate', 'evidence', 'decision', 'output'):
        generate.add_argument('--' + opt, type=Path, required=True)
    verify = modes.add_parser('verify', help='Verify a downloaded signed evidence bundle offline')
    verify.add_argument('--result', type=Path)
    for mode in (generate, verify):
        mode.add_argument('--lineage-index', type=Path)
        mode.add_argument('--location', action='append', default=[], metavar='NAME=ROOT')
    for opt in ('bundle', 'trust-file'):
        verify.add_argument('--' + opt, type=Path, required=True)
    for mode in (generate, verify):
        for opt in ('release', 'repository', 'source-commit', 'run-id'):
            mode.add_argument('--' + opt, required=True)
        mode.add_argument('--run-attempt', type=int, required=True)
        mode.add_argument('--cyclonedx', type=Path, required=True)
    args = parser.parse_args(argv)
    identity = {key: getattr(args, key) for key in ('release', 'repository', 'source_commit', 'run_id', 'run_attempt')}
    locations = assignments(args.location)
    if args.mode == 'prepare':
        prepare(args.candidate, args.evidence, args.decision, args.output, identity, args.cyclonedx, os.getenv('GITHUB_TOKEN'),
                lineage_index=args.lineage_index, locations=locations)
        print(f'Unsigned candidate evidence prepared: {args.output}')
    else:
        verify_bundle(args.bundle, args.trust_file, identity, args.cyclonedx)
        if args.result:
            value = verify_result(args.result, args.bundle, lineage_index=args.lineage_index, locations=locations)
            print(json.dumps(value, ensure_ascii=False))
        elif args.lineage_index:
            verify_index(_ordinary_path(args.lineage_index), locations, json_object(args.bundle / 'model-lineage.json'))
        print('Signed release evidence verified')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
