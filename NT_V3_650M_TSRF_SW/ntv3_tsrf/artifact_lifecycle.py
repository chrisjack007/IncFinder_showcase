"""Verify an artifact inventory with explicit availability and publication evidence."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .artifact_manifest import _SUMMARY, _check_summary, _facts, _files, _open_manifest, artifact_path
from .run_record import _recorded_output, publication_events


@dataclass
class ArtifactView:
    root: Path
    manifest: dict
    digest: str
    summary: dict | None
    publication: tuple | None
    paths: dict
    missing: dict

    @property
    def retention(self):
        return self.publication[2].get('artifact_retention', {}) if self.publication else {}


def _read_view(root, manifest, digest, reference_paths, *, read_references):
    files = _files(root, manifest['kind'])
    local = {item['path'] for item in manifest['artifacts'] if item['storage'] != 'reference'}
    if files.keys() - local:
        raise ValueError('Unexpected file in sealed artifact directory')
    mappings = {} if reference_paths is None else reference_paths
    references = {item['artifact_id'] for item in manifest['artifacts'] if item['storage'] == 'reference'}
    if not isinstance(mappings, dict) or mappings.keys() - references:
        raise ValueError('Reference bindings must identify existing external artifacts')

    publication = publication_events(root, manifest, digest, include_state=True) if _SUMMARY in files else None
    view = ArtifactView(root, manifest, digest, None, publication, {}, {})
    native_output = publication is not None and _recorded_output(publication[2]) == root
    for entry in manifest['artifacts']:
        identity = entry['artifact_id']
        recorded = view.retention.get(identity)
        if recorded and any(recorded[key] != entry[key] for key in ('sha256', 'size_bytes', 'parents')):
            raise ValueError('Retention event does not match the sealed artifact inventory')
        if entry['storage'] == 'reference':
            if not read_references:
                continue
            supplied = identity in mappings
            # An archive or a detached Run can carry unrelated absolute paths.
            original = entry['path'] if native_output else None
            name = mappings.get(identity, original)
            if name is None and not supplied:
                view.missing[identity] = 'external_required'
                continue
            if not isinstance(name, (str, Path)) or not name or (not supplied and not Path(name).is_absolute()):
                raise ValueError('Invalid external artifact binding')
            target = artifact_path(name)
            if not target.exists() and not supplied:
                view.missing[identity] = 'external_required'
                continue
        else:
            target = files.get(entry['path'])
            if target is None:
                view.missing[identity] = 'retention' if recorded else 'missing'
                continue
            if recorded and recorded['status'] == 'unavailable':
                raise ValueError('A retired artifact path has been occupied')
        if _facts(target, identity, stored=entry['storage'] != 'reference') != (entry['size_bytes'], entry['sha256']):
            raise ValueError(f"Artifact size or SHA-256 mismatch in the sealed inventory: {entry['role']} ({identity})")
        view.paths[identity] = target

    if _SUMMARY in files:
        summary = files[_SUMMARY].read_bytes()
        entry = next(item for item in manifest['artifacts'] if item['path'] == _SUMMARY)
        if hashlib.sha256(summary).hexdigest() != entry['sha256']:
            raise ValueError('Run summary SHA-256 changed during the read')
        view.summary = json.loads(summary)
        _check_summary(view.summary, manifest)
    return view


def inspect_artifacts(directory, *, expected_manifest_sha256=None, reference_paths=None, read_references=True):
    root = artifact_path(directory)
    manifest, digest = _open_manifest(root, expected_manifest_sha256)
    return _read_view(root, manifest, digest, reference_paths, read_references=read_references)


def retained_view(directory, *, expected_manifest_sha256=None, reference_paths=None):
    """Read a lifecycle-aware view when the native publication records a cleanup."""
    root = artifact_path(directory)
    manifest, digest = _open_manifest(root, expected_manifest_sha256)
    publication = publication_events(root, manifest, digest, include_state=True) if (root / _SUMMARY).is_file() else None
    if publication and publication[2].get('artifact_retention'):
        return _read_view(root, manifest, digest, reference_paths, read_references=True)
    return None
