"""Export and atomically publish a verified artifact snapshot."""
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from .artifact_manifest import artifact_path, verify_artifacts, write_manifest, _verify
from .resources import ROOT, CODE_ROOT, INSTALLED
from .run_record import publish_output, require_new_output, reserve_output


def _copied_path(role, source):
    if role == 'input':
        with source.open('rb') as stream:
            suffix = '.gz' if stream.read(2) == b'\x1f\x8b' else ''
        return 'dependencies/input/input' + suffix
    return 'dependencies/features/' + ('features.npy' if role == 'features' else 'features.json')


def _stage_artifacts(staged, manifest, source_sha, sources, external_mode, *, portable=False):
    artifacts = []
    for entry in manifest['artifacts']:
        item = dict(entry)
        source = sources.get(entry['artifact_id'])
        if item['storage'] == 'reference':
            if external_mode == 'copy':
                if source is None:
                    raise ValueError(f"Missing {item['role']} reference for copy: {item['artifact_id']}")
                item.update(path=_copied_path(item['role'], source), storage='copied')
            elif portable:
                item['path'] = None
        if item['storage'] != 'reference' and source is not None:
            path = staged / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as incoming, path.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        artifacts.append(item)
    archived = {**manifest, 'kind': 'archive', 'external_mode': external_mode,
                'source_manifest_sha256': source_sha, 'artifacts': artifacts}
    return write_manifest(staged, archived), archived


def export_artifact_archive(directory, destination, *, external_mode='reference', reference_paths=None):
    """Publish a verified directory archive and return its manifest.

    The copy mode includes input and precomputed feature bytes. Reference mode
    requires access to the original external dependencies when verifying.
    """
    if external_mode not in ('reference', 'copy'):
        raise ValueError('External mode must be reference or copy')
    root, target = artifact_path(directory), artifact_path(destination)
    if target.is_relative_to(root):
        raise ValueError('Archive destination must be outside the source result')
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'results'):
        raise ValueError('Archives within the software root must be under results/')
    if INSTALLED and any(target.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Archives must not be written into the installed package')
    require_new_output(target)
    manifest, source_sha, sources = _verify(root, reference_paths=reference_paths)
    if manifest['kind'] != 'run':
        raise ValueError('Archive export requires a prediction result directory')

    with reserve_output(target, manifest['run_id']), TemporaryDirectory(prefix='.incfinder-archive-', dir=target.parent) as tmp:
        staged = Path(tmp) / 'archive'
        staged.mkdir()
        digest, archived = _stage_artifacts(staged, manifest, source_sha, sources, external_mode)
        verify_artifacts(staged, expected_manifest_sha256=digest,
                         reference_paths=reference_paths if external_mode == 'reference' else None)
        publish_output(staged, target)
    return archived
