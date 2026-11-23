"""Build small, relocatable distributions; all intermediate files use system temp."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
with (ROOT / 'pyproject.toml').open('rb') as project_file:
    VERSION = tomllib.load(project_file)['project']['version']
EXCLUDED = {'.git', '.github', '__pycache__', '.pytest_cache', '.cache', 'dist', 'build', 'results',
            'AGENTS.md', 'projectprocess.md', 'model_assets', 'archive', '.env'}
PUBLIC_DOCS = {'docs/usage.md', 'docs/scientific_validation.md', 'docs/validation.md',
               'docs/validation_matrix.json', 'docs/output_policy.md', 'docs/changelog.md',
               'docs/MODEL_CARD.en.md', 'docs/MODEL_CARD.zh-CN.md',
               'docs/per_class_metrics.tsv', 'docs/holdout_metrics.json'}


sys.path.insert(0, str(ROOT))
try:
    from ntv3_tsrf import encoder_source
finally:
    sys.path.remove(str(ROOT))


def excluded_sources():
    return encoder_source.distribution_paths(ROOT, VERSION)


def members():
    external = excluded_sources()
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if relative.as_posix() == 'config/model_sources.json' and (ROOT / 'config/model_delivery.json').is_file():
            continue
        if relative.as_posix() in external:
            continue
        if relative.parts[0] == 'docs' and relative.as_posix() not in PUBLIC_DOCS:
            continue
        if path.is_file() and not any(p in EXCLUDED or p.endswith('.egg-info') for p in relative.parts) and path.suffix != '.pyc':
            yield path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def refresh_manifest():
    assets = {x['path']: x for x in json.loads((ROOT / 'config/assets.json').read_text(encoding='utf-8'))['files']}
    # A source checkout deliberately omits weights. Keep their frozen identities
    # so check/predict can validate a separately supplied complete model folder.
    rows = {name: {key: entry[key] for key in ('path', 'size_bytes', 'sha256')}
            for name, entry in assets.items()}
    external = excluded_sources()
    if external - assets.keys():
        raise RuntimeError('Incomplete candidate inventory for the upstream NTv3 sources')
    for name in external:
        rows[name]['delivery'] = encoder_source.DELIVERY
        source = ROOT / name
        if source.exists() and (source.stat().st_size != assets[name]['size_bytes'] or digest(source) != assets[name]['sha256']):
            raise RuntimeError('Upstream NTv3 source differs from its signed identity: ' + name)
    for name in assets:
        external_weight = name.startswith('models/') and Path(name).suffix in {'.joblib', '.safetensors'}
        if not (ROOT / name).is_file() and not external_weight and name not in external:
            raise RuntimeError(f'Missing immutable asset: {name}')
    for path in members():
        name = path.relative_to(ROOT).as_posix()
        if name == 'package_manifest.json':
            continue
        size = path.stat().st_size
        if name in assets and size != assets[name]['size_bytes']:
            raise RuntimeError(f'Frozen asset size changed: {name}')
        actual_hash = digest(path)
        if name in assets and actual_hash != assets[name]['sha256']:
            raise RuntimeError(f'Frozen asset SHA-256 changed: {name}')
        rows[name] = {'path': name, 'size_bytes': size,
                      'sha256': actual_hash}
    (ROOT / 'package_manifest.json').write_text(json.dumps({'schema': 1, 'files': [rows[name] for name in sorted(rows)]}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel-only', action='store_true')
    parser.add_argument('--conda-only', action='store_true')
    parser.add_argument('--archives-only', action='store_true')
    parser.add_argument('--refresh-manifest', action='store_true')
    args = parser.parse_args()
    refresh_manifest()
    if args.refresh_manifest:
        return
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    if not args.archives_only:
        with tempfile.TemporaryDirectory(prefix='incfinder-build-') as temporary:
            temporary = Path(temporary)
            source = temporary / 'source'
            source.mkdir()
            for path in members():
                if path.stat().st_size > 10_000_000:
                    continue
                target = source / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if not args.conda_only:
                subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                                '--wheel-dir', str(dist / 'wheels'), str(source)], check=True)
            if not args.wheel_only:
                subprocess.run([sys.executable, '-m', 'conda', 'build', str(source / 'packaging/conda'),
                                '--croot', str(temporary / 'conda-build'), '--output-folder', str(dist / 'channel'),
                                '--no-anaconda-upload', '--override-channels', '-c', 'conda-forge', '--python', '3.12'], check=True)
                # Empty platform subdirectories allow a local noarch channel on both platforms.
                for platform in ('win-64', 'linux-64', 'noarch'):
                    (dist / 'channel' / platform).mkdir(parents=True, exist_ok=True)
                subprocess.run([sys.executable, '-m', 'conda_index', str(dist / 'channel')], check=True)
    if not args.wheel_only and not args.conda_only:
        selected = [p for p in members() if p.stat().st_size <= 10_000_000]
        selected += [p for folder in ('channel', 'wheels') for p in (dist / folder).rglob('*')
                     if p.is_file() and '.cache' not in p.parts and not p.name.endswith('.lock')]
        prefix = f'incfinder-{VERSION}'
        with zipfile.ZipFile(dist / f'{prefix}-win-64.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in selected:
                archive.write(path, prefix + '/' + path.relative_to(ROOT).as_posix())
        with tarfile.open(dist / f'{prefix}-linux-64.tar.gz', 'w:gz') as archive:
            for path in selected:
                archive.add(path, arcname=prefix + '/' + path.relative_to(ROOT).as_posix(), recursive=False)
    checksums = [f'{digest(p)}  {p.relative_to(dist).as_posix()}' for p in sorted(dist.rglob('*'))
                 if p.is_file() and p.name != 'SHA256SUMS' and '.cache' not in p.parts and not p.name.endswith('.lock')]
    (dist / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
