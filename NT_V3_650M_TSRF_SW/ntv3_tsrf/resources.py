"""Locate immutable package resources and explicitly configured external models."""
from importlib.resources import files
import json
import os
from pathlib import Path
import stat
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_ROOT = Path(str(files('ntv3_tsrf').joinpath('_resources')))
INSTALLED = PACKAGED_ROOT.is_dir()
ROOT = PACKAGED_ROOT if INSTALLED else CODE_ROOT


def path_is_link(path):
    """Recognize symlinks and Windows reparse points without dereferencing them."""
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    attrs = getattr(info, 'st_file_attributes', 0)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def checked_child(base, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'Resource paths must be relative and remain within their directory: {relative}')
    base = Path(base)
    path = base / relative
    for current in (path, *path.parents):
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError(f'Resource paths must not contain links: {current}')
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f'Resource path escapes its directory: {relative}')
    return path


def binding_path():
    return Path(sys.prefix) / 'share/incfinder/model-location.json'


def model_location(explicit=None, base=None):
    """Fail at an invalid higher-priority location; never silently fall back."""
    if explicit is not None:
        value, source = explicit, 'argument'
    elif 'INCFINDER_MODEL_DIR' in os.environ:
        value, source = os.environ['INCFINDER_MODEL_DIR'], 'INCFINDER_MODEL_DIR'
    elif binding_path().is_file():
        binding = json.loads(binding_path().read_text(encoding='utf-8'))
        if binding.get('schema') != 1 or not Path(binding['model_dir']).is_absolute():
            raise ValueError('Invalid environment model binding; rerun install.sh or specify --model-dir')
        value, source = binding['model_dir'], 'environment_binding'
    elif not INSTALLED:
        value, source = CODE_ROOT / 'models', 'software_directory'
    else:
        raise ValueError('Models are not configured; specify --model-dir or INCFINDER_MODEL_DIR')
    if not str(value).strip():
        raise ValueError('model directory must not be empty')
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() if base is None else Path(base)) / path
    checked_child(path, '.')
    path = path.resolve()
    if not path.is_dir() or not all((path / name).is_dir() for name in ('ntv3', 'rf')):
        raise ValueError(f'model directory must contain ntv3/ and rf/: {path}')
    return {'path': str(path), 'source': source}


def model_root(explicit=None, base=None):
    return Path(model_location(explicit, base)['path'])


def bind_models(directory):
    """Installer helper: write only inside the current Python/Conda environment."""
    selected = model_root(directory)
    from .common import verify_assets
    verify_assets(quick=True, model_dir=selected)
    destination = binding_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'schema': 1, 'model_dir': str(selected)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(destination)
    return str(destination)
