"""Copy immutable small resources into wheels; never bundle model weights."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import tomllib
from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent


sys.path.insert(0, str(ROOT))
try:
    from ntv3_tsrf import encoder_source
finally:
    sys.path.remove(str(ROOT))


def external_files():
    with (ROOT / 'pyproject.toml').open('rb') as stream:
        version = tomllib.load(stream)['project']['version']
    return encoder_source.distribution_paths(ROOT, version)


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        excluded = external_files()
        names = [name for name in files if Path(name).as_posix() not in excluded]
        super().make_release_tree(base_dir, names)


class BuildResources(build_py):
    def run(self):
        super().run()
        build = Path(self.build_lib)
        destination = build / 'ntv3_tsrf/_resources'
        excluded = external_files()
        for name in excluded:
            if (destination / name).exists():
                raise ValueError('Stale upstream source in wheel build directory: ' + name)
        for relative in ('config', 'vendor', 'tests/fixtures'):
            for source in sorted((ROOT / relative).rglob('*')):
                if source.name == 'model_sources.json' and (ROOT / 'config/model_delivery.json').is_file():
                    continue
                if source.relative_to(ROOT).as_posix() in excluded:
                    continue
                if source.is_file() and '__pycache__' not in source.parts:
                    if source.suffix not in ('.json', '.tsv', '.py', '.npy', '.npz', '.fasta'):
                        continue
                    target = destination / source.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        entries = []
        for package in ('incfinder', 'ntv3_tsrf'):
            for path in sorted((build / package).rglob('*')):
                if path.is_file() and path.name != 'software_manifest.json' and '__pycache__' not in path.parts:
                    entries.append({'path': path.relative_to(build).as_posix(), 'size_bytes': path.stat().st_size,
                                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (destination / 'software_manifest.json').write_text(json.dumps({'schema': 1, 'files': entries}, indent=2) + '\n', encoding='utf-8')


setup(cmdclass={'build_py': BuildResources, 'sdist': SourceDistribution})
