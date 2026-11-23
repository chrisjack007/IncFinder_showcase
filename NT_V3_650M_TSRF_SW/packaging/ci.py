"""Run explicit CI stages in system temp; never create or replace a release."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote, urlparse
import zipfile

from release_gate import contracts, digest, read, verify_candidate

ROOT = Path(__file__).resolve().parents[1]
def git(*arguments):
    return subprocess.check_output(['git', '-C', str(ROOT), *arguments],
        env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'), stderr=subprocess.PIPE)


def checkout_root():
    try:
        return Path(git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    except subprocess.CalledProcessError:
        return ROOT.resolve()


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def output_directory(value, allow_existing=False):
    path = Path(value).expanduser().absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('CI output paths must not contain links')
    path = path.resolve()
    if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()) or path.is_relative_to(checkout_root()):
        raise ValueError('CI outputs must be in system temp, outside the checkout')
    path.mkdir(parents=True, exist_ok=allow_existing)
    return path


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def context(source_sha, kind):
    actual = git('rev-parse', 'HEAD').decode().strip()
    if not re.fullmatch('[0-9a-f]{40}', source_sha) or actual != source_sha:
        raise ValueError('Requested source SHA does not match the checkout')
    dirty = git('status', '--porcelain=v1', '-z')
    in_actions = os.environ.get('GITHUB_ACTIONS') == 'true'
    system = platform.system()
    if system not in ('Windows', 'Linux'):
        raise ValueError('Only the Windows/Linux CI platforms are supported')
    return {'schema': 1, 'kind': kind, 'status': 'RUNNING', 'source_commit': source_sha,
            'source_clean': not bool(dirty), 'source_manifest_sha256': digest(ROOT / 'package_manifest.json'),
            'execution_context': 'github-actions' if in_actions else 'local',
            'repository': os.environ.get('GITHUB_REPOSITORY', 'chrisjack007/IncFinder_engineer'),
            'run_id': os.environ.get('GITHUB_RUN_ID', '0') if in_actions else '0',
            'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')) if in_actions else 1,
            'platform': 'win-64' if system == 'Windows' else 'linux-64',
            'python': sys.version, 'created_at_utc': datetime.now(timezone.utc).isoformat(), **contracts()}


def run(command, cwd, log):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    with Path(log).open('w', encoding='utf-8') as stream:
        subprocess.run([str(x) for x in command], cwd=cwd, env=environment,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


def unit(args, output):
    module_at('release_builder', ROOT / 'packaging/build_release.py').refresh_manifest()
    receipt = context(args.source_sha, 'unit')
    save(output / 'receipt.json', receipt)
    run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
        ROOT, output / 'unit.log')
    text = (output / 'unit.log').read_text(encoding='utf-8')
    match = re.search(r'^Ran ([1-9][0-9]*) tests? in ', text, re.MULTILINE)
    if not match or not text.rstrip().endswith('OK'):
        raise ValueError('Unit regressions did not all complete successfully without skips')
    receipt.update(status='PASS', tests_run=int(match[1]), tests_successful=True)
    return receipt


def git_blob_sha256(object_id):
    with subprocess.Popen(['git', '-C', str(ROOT), 'cat-file', 'blob', object_id],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        checksum = hashlib.file_digest(child.stdout, 'sha256').hexdigest()
        if child.wait():
            raise ValueError('Cannot read Git blob ' + object_id)
    return checksum


def verify_external_git_tree(revision, rows):
    prefix = git('rev-parse', '--show-prefix').decode().strip()
    forbidden_names = {prefix + row['path'] for row in rows}
    hashes_by_size = {}
    for row in rows:
        hashes_by_size.setdefault(row['size_bytes'], set()).add(row['sha256'])
    seen = set()
    tree = git('rev-parse', revision + '^{tree}').decode().strip()
    for member in git('ls-tree', '--full-tree', '-r', '-l', '-z', tree).split(b'\0'):
        if not member:
            continue
        header, name = member.split(b'\t', 1)
        path = name.decode('utf-8')
        if path in forbidden_names:
            raise ValueError('Git source tree includes an upstream-only asset: ' + path)
        _, kind, object_id, size = header.split()
        if kind == b'blob' and int(size) in hashes_by_size and object_id not in seen:
            if git_blob_sha256(object_id.decode('ascii')) in hashes_by_size[int(size)]:
                raise ValueError('Git source tree includes renamed upstream content: ' + path)
            seen.add(object_id)
    return {'revision': revision, 'tree': tree, 'upstream_files': len(rows),
            'compared_blobs': len(seen), 'matches': 0}


def build(args, output):
    builder = module_at('release_builder', ROOT / 'packaging/build_release.py')
    builder.refresh_manifest()
    receipt = context(args.source_sha, 'build')
    external = [row for row in read(ROOT / 'package_manifest.json')['files'] if 'delivery' in row]
    if external:
        receipt['upstream_git_tree'] = verify_external_git_tree(args.source_sha, external)
    save(output / 'receipt.json', receipt)
    copied = output / 'source'
    copied.mkdir()
    for source in builder.members():
        if source.stat().st_size > 10_000_000:
            continue
        destination = copied / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for kind in ('wheel', 'conda'):
        run([sys.executable, '-B', copied / 'packaging/build_release.py', '--' + kind + '-only'],
            copied, output / (kind + '-build.log'))
    if digest(copied / 'package_manifest.json') != receipt['source_manifest_sha256']:
        raise ValueError('Candidate source identity changed during the build')
    packages = output / 'packages'
    packages.mkdir()
    receipt['artifacts'] = []
    for kind, pattern in (('wheel', 'dist/wheels/*.whl'), ('conda', 'dist/channel/noarch/*.conda')):
        matches = list(copied.glob(pattern))
        if len(matches) != 1:
            raise ValueError('Expected exactly one candidate ' + kind)
        target = packages / matches[0].name
        shutil.copyfile(matches[0], target)
        receipt['artifacts'].append({'package_kind': kind, 'file': target.name, 'sha256': digest(target)})
    if getattr(args, 'release_evidence', False):
        from release_evidence import ARCHIVES, archive_records
        from release_sbom import TOOL_SHA256, lock_file

        run([sys.executable, '-B', copied / 'packaging/build_release.py', '--archives-only'],
            copied, output / 'archives-build.log')
        source_manifest = output / 'source-manifest.json'
        shutil.copyfile(copied / 'package_manifest.json', source_manifest)
        candidates = {item['package_kind']: packages / item['file'] for item in receipt['artifacts']}
        for item in receipt['artifacts']:
            item['size_bytes'] = candidates[item['package_kind']].stat().st_size
        archives = output / 'archives'
        archives.mkdir()
        for name in ARCHIVES:
            shutil.copyfile(copied / 'dist' / name, archives / name)
        receipt.update(
            archives=archive_records(list(archives.iterdir()), source_manifest, candidates),
            packaged_source_manifest_sha256=digest(source_manifest),
            lock_sha256={target: digest(lock_file(target)) for target in TOOL_SHA256},
            build_environment_sha256=digest(ROOT / 'environments/ci-build.yml'),
            build_tools={name: importlib.metadata.version(name)
                         for name in ('pip', 'setuptools', 'wheel', 'conda-build', 'conda-index')},
        )
    wheel = packages / receipt['artifacts'][0]['file']
    with zipfile.ZipFile(wheel) as archive:
        receipt['software_manifest_sha256'] = hashlib.sha256(
            archive.read('ntv3_tsrf/_resources/software_manifest.json')).hexdigest()
    receipt['status'] = 'PASS'
    return receipt


def installation_origin(package_kind, artifact):
    """Distinguish actual pip-wheel and Conda installs of identical payloads."""
    import tomllib
    with (ROOT / 'pyproject.toml').open('rb') as project_file:
        record_version = tomllib.load(project_file)['project']['version']
    artifact = Path(artifact)
    records = list((Path(sys.prefix) / 'conda-meta').glob('incfinder-*.json'))
    if package_kind == 'conda':
        if len(records) != 1:
            raise ValueError('Expected the actual installed Conda package record')
        record = read(records[0])
        if (record.get('sha256') != digest(artifact) or record.get('fn') != artifact.name
                or record.get('name') != 'incfinder' or record.get('version') != str(record_version)):
            raise ValueError('Conda installation record does not identify the candidate artifact')
        return {'installation_method': 'conda-package', 'installation_record_sha256': digest(records[0])}
    if records:
        raise ValueError('Conflicting Conda installation remains during a wheel check')
    origin_text = importlib.metadata.distribution('incfinder-ntv3-tsrf').read_text('direct_url.json')
    origin = json.loads(origin_text or '{}')
    url = urlparse(origin.get('url', ''))
    if (url.scheme != 'file' or Path(unquote(url.path)).name != artifact.name
            or origin.get('archive_info', {}).get('hashes', {}).get('sha256') != digest(artifact)):
        raise ValueError('Wheel installation origin does not identify the candidate artifact')
    return {'installation_method': 'pip-wheel',
            'installation_record_sha256': hashlib.sha256(origin_text.encode('utf-8')).hexdigest()}


def installed_context(args, kind):
    repository = checkout_root()
    if Path.cwd().resolve().is_relative_to(repository):
        raise ValueError('Installed checks must run outside the checkout')
    import incfinder
    from ntv3_tsrf import resources
    from ntv3_tsrf.common import verify_package
    from ntv3_tsrf.model_lineage import POINTER, load_pointer
    if (not resources.INSTALLED or resources.CODE_ROOT.is_relative_to(repository)
            or not Path(incfinder.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())):
        raise ValueError('Expected the candidate installed in the active environment, without source shadowing')
    if verify_package().get('status') != 'PASS':
        raise ValueError('Installed package integrity check failed')
    receipt = context(args.source_sha, kind)
    build_receipt = read(args.build_receipt)
    if build_receipt.get('kind') != 'build' or build_receipt.get('status') != 'PASS':
        raise ValueError('A successful candidate build receipt is required')
    for name in ('source_commit', 'source_manifest_sha256', 'run_id', 'run_attempt',
                 'execution_context', 'repository', 'source_clean', *contracts()):
        if build_receipt.get(name) != receipt[name]:
            raise ValueError('Candidate build identity mismatch: ' + name)
    matching = [item for item in build_receipt['artifacts'] if item.get('package_kind') == args.package_kind]
    artifact = Path(args.artifact)
    if (len(matching) != 1 or artifact.name != matching[0]['file']
            or digest(artifact) != matching[0]['sha256']):
        raise ValueError('The installed check must reference the exact original candidate package')
    manifest_hash = digest(resources.ROOT / 'software_manifest.json')
    if manifest_hash != build_receipt.get('software_manifest_sha256'):
        raise ValueError('Installed code/resources differ from the candidate build')
    for name, filename in (('scientific_contract', 'contract.json'), ('assets_manifest', 'assets.json'),
                           ('output_contract', 'output_contract.json')):
        if digest(resources.ROOT / 'config' / filename) != receipt[name + '_sha256']:
            raise ValueError('Installed scientific/output contract identity mismatch')
    pointer = load_pointer(resources.ROOT)
    pointer_hash = digest(resources.ROOT / POINTER) if pointer is not None else None
    if pointer_hash != receipt.get('model_lineage_sha256'):
        raise ValueError('Installed model lineage differs from the candidate build')
    receipt.update(package_kind=args.package_kind, artifact_sha256=digest(artifact),
                   build_receipt_sha256=digest(args.build_receipt), installed_package=True,
                   package_integrity=True, software_manifest_sha256=manifest_hash)
    receipt.update(installation_origin(args.package_kind, artifact))
    return receipt


def metadata_checks(directory):
    from incfinder import load_output_metadata, require_supported_policy
    from ntv3_tsrf.output_metadata import RESULT_FILES
    for name in RESULT_FILES:
        require_supported_policy(load_output_metadata(Path(directory) / name))
    return len(RESULT_FILES)


def trusted_execution(source_sha):
    env = os.environ
    branch = env.get('GITHUB_REF')
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('RUNNER_ENVIRONMENT') != 'self-hosted'
            or env.get('GITHUB_REPOSITORY') != 'chrisjack007/IncFinder_engineer'
            or not env.get('GITHUB_ACTOR') or env.get('GITHUB_ACTOR') != env.get('GITHUB_REPOSITORY_OWNER')):
        return False
    if env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch':
        return branch in ('refs/heads/main', 'refs/heads/codex/if003-ci-validation')
    return (env.get('GITHUB_EVENT_NAME') == 'push' and branch == 'refs/heads/codex/if003-ci-validation'
            and env.get('INCFINDER_CI_APPROVED_SHA') == source_sha)


def installed(args, output):
    import numpy as np
    from incfinder import migrate_v1_results
    receipt = installed_context(args, 'installed')
    save(output / 'receipt.json', receipt)
    fixture = module_at('ci_test_fixtures', ROOT / 'tests/output_helpers.py')
    for mode in ('ct', 'u05'):
        original, migrated = output / ('v1-' + mode), output / ('v2-' + mode)
        fixture.make_result(original, version='v1', mode=mode)
        migrate_v1_results(original, migrated)
        with np.load(original / 'probabilities.npz', allow_pickle=False) as before, \
                np.load(migrated / 'probabilities.npz', allow_pickle=False) as after:
            for name in ('stage1', 'stage2', 'gate', 'labels'):
                if before[name].dtype != after[name].dtype or not np.array_equal(before[name], after[name], equal_nan=True):
                    raise ValueError('Installed conversion changed original arrays')
        if metadata_checks(migrated) != 6:
            raise ValueError('Incomplete installed metadata validation')
    run([sys.executable, '-B', '-m', 'incfinder', '--help'], output, output / 'cli-help.log')
    if getattr(args, 'release_evidence', False):
        from ntv3_tsrf import resources
        from release_sbom import installed_bom

        bom = output / 'sbom.cdx.json'
        save(bom, installed_bom(receipt, ROOT / 'package_manifest.json', resources.ROOT))
        receipt['sbom_sha256'] = digest(bom)
    receipt.update(status='PASS', reference_conversion_exact=True, modes_validated=['ct', 'u05'],
                   sidecars_validated=6, model_inference_performed=False)
    return receipt


def frozen_feature_input(resource_root, output, baseline_manifest):
    """Qualify the original features for a candidate with identical encoder assets."""
    fixture = resource_root / 'tests/fixtures'
    metadata = read(fixture / 'features.json')
    current = resource_root / 'config/assets.json'
    current_sha256 = digest(current)
    if current_sha256 == metadata['assets_sha256']:
        return fixture
    from ntv3_tsrf.model_lineage import MODEL_ROLES, identity, load_pointer

    if load_pointer(resource_root) is None:
        raise ValueError('A different RF asset manifest requires a verified model lineage pointer')
    if (hashlib.sha256(baseline_manifest).hexdigest() != metadata['assets_sha256']
            or digest(fixture / 'features.npy') != metadata['features_sha256']
            or digest(resource_root / 'config/contract.json') != metadata['contract_sha256']):
        raise ValueError('The original frozen feature or baseline manifest identity changed')
    originals = json.loads(baseline_manifest)['files']
    candidates = read(current)['files']
    allowed = set(MODEL_ROLES) - {'models/ntv3/model.safetensors'}
    old = {row['path']: identity(row) for row in originals if row['path'] not in allowed}
    new = {row['path']: identity(row) for row in candidates if row['path'] not in allowed}
    if old != new:
        raise ValueError('Candidate encoder or other frozen assets differ from the original feature contract')
    prepared = output / 'candidate-frozen-features'
    prepared.mkdir(exist_ok=False)
    shutil.copyfile(fixture / 'features.npy', prepared / 'features.npy')
    save(prepared / 'features.json', dict(metadata, assets_sha256=current_sha256))
    return prepared


def frozen(args, output):
    receipt = installed_context(args, 'frozen')
    save(output / 'receipt.json', receipt)
    driver = module_at('frozen_acceptance', ROOT / 'scripts/compare_deployments.py')
    model_dir = Path(args.model_dir).expanduser().resolve()
    if not model_dir.is_dir():
        raise ValueError('An explicit complete read-only model directory is required')
    from ntv3_tsrf.resources import ROOT as installed_root

    result = output / 'inference'
    fixture = installed_root / 'tests/fixtures'
    old_manifest = b''
    if digest(installed_root / 'config/assets.json') != read(fixture / 'features.json')['assets_sha256']:
        old_manifest = git('show', 'aa67fd5219a695c9c1d2600957bddb93421b4ae7:NT_V3_650M_TSRF_SW/config/assets.json')
    frozen_features = frozen_feature_input(installed_root, output, old_manifest)
    driver.run(SimpleNamespace(output=result, model_dir=model_dir, cases=['frozen'], frozen_features=frozen_features,
                               threshold_mode=args.threshold_mode, label='candidate-' + args.package_kind))
    acceptance = read(result / 'acceptance.json')
    if acceptance.get('frozen_rf_pass') is not True or acceptance.get('cases', {}).get('frozen', {}).get('exit_code') != 0:
        raise ValueError('Frozen RF arrays failed their independent reference')
    difference = driver.result_difference(result / 'frozen', ROOT / 'examples/expected_outputs' / args.threshold_mode)
    if (not all(difference[name].get('exact') for name in driver.ARRAY_KEYS)
            or not all(difference['csv_equal'].values()) or difference['xlsx_cells_equal'] is not True):
        raise ValueError('Frozen arrays or CSV/XLSX contents differ from the published reference')
    summary = read(result / 'frozen/run_summary.json')
    if summary.get('scientific_equivalence') != 'FROZEN_REFERENCE':
        raise ValueError('Frozen integration must retain its original scientific scope')
    trusted = trusted_execution(args.source_sha)
    receipt.update(status='PASS', arrays_exact=True, tables_exact=True, sidecars_validated=metadata_checks(result / 'frozen'),
                   threshold_mode=args.threshold_mode, trusted_runner=trusted, device='cpu', encoder_run=False,
                   scientific_equivalence=summary['scientific_equivalence'],
                   reference_comparison=difference, six_case_engineering_status=acceptance['engineering_status'],
                   frozen_features_metadata_sha256=digest(frozen_features / 'features.json'))
    return receipt


def gate(args, output):
    artifacts = list(Path(args.packages).glob('*.whl')) + list(Path(args.packages).glob('*.conda'))
    evidence = list(Path(args.evidence).rglob('receipt.json'))
    try:
        decision = verify_candidate(args.build_receipt, evidence, artifacts,
            source_commit=args.source_sha, run_id=os.environ.get('GITHUB_RUN_ID', '0'),
            run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')),
            repository=os.environ.get('GITHUB_REPOSITORY', 'chrisjack007/IncFinder_engineer'))
    except Exception as error:
        save(output / 'decision.json', {'schema': 1, 'status': 'BLOCKED', 'eligible': False,
                                       'release_published': False, 'error': str(error)})
        raise
    save(output / 'decision.json', decision)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('unit', 'build', 'installed', 'frozen', 'gate'):
        command = commands.add_parser(name)
        command.add_argument('--source-sha', required=True)
        command.add_argument('--output', required=True, type=Path)
        if name in ('installed', 'frozen'):
            command.add_argument('--build-receipt', required=True, type=Path)
            command.add_argument('--artifact', required=True, type=Path)
            command.add_argument('--package-kind', required=True, choices=('wheel', 'conda'))
        if name in ('build', 'installed'):
            command.add_argument('--release-evidence', action='store_true')
        if name == 'frozen':
            command.add_argument('--model-dir', required=True, type=Path)
            command.add_argument('--threshold-mode', required=True, choices=('ct', 'u05'))
        if name == 'gate':
            command.add_argument('--build-receipt', required=True, type=Path)
            command.add_argument('--packages', required=True, type=Path)
            command.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args(argv)
    output = output_directory(args.output, allow_existing=args.command == 'gate')
    if args.command == 'gate':
        save(output / 'decision.json', {'schema': 1, 'status': 'BLOCKED', 'eligible': False,
                                       'release_published': False, 'error': 'Candidate validation has not completed'})
    try:
        receipt = globals()[args.command](args, output)
        if receipt is not None:
            save(output / 'receipt.json', receipt)
    except Exception as error:
        if args.command != 'gate':
            path = output / 'receipt.json'
            value = read(path) if path.is_file() else {'schema': 1, 'kind': args.command}
            save(path, dict(value, status='FAIL', error=str(error)))
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
