"""Preview and apply an explicit, idempotent policy to a published local Run."""
import hashlib
import json
import os
import stat

from .artifact_lifecycle import inspect_artifacts
from .artifact_manifest import _ARRAY, _FEATURE_META, _facts, _open_manifest, artifact_path
from .run_record import _recorded_output, _require_journal, _writer_lease


POLICIES = ('minimal', 'standard', 'full')
_LOGS = {'logs/embedding.log', 'logs/rf.log', 'logs/report.log'}
_REGENERABLE = {_ARRAY, _FEATURE_META}


def _candidate(entry, policy):
    if entry['storage'] != 'retained' or policy == 'full':
        return None
    if entry['role'] == 'log' and entry['path'] in _LOGS:
        return 'recreatable_worker_log'
    if (policy == 'minimal' and entry['stage'] == 'embedding' and entry['path'] in _REGENERABLE
            and entry['role'] in ('features', 'feature_metadata')):
        return 'regenerable_encoded_feature'
    return None


def _plan(view, policy):
    if policy not in POLICIES:
        raise ValueError('Unknown retention policy')
    if (view.manifest['kind'] != 'run' or not view.publication
            or _recorded_output(view.publication[2]) != view.root):
        raise ValueError('Retention requires the original published Run bound to its local event journal')
    if 'missing' in view.missing.values():
        raise ValueError('An original local artifact is missing without a retention event')
    targets = []
    for entry in view.manifest['artifacts']:
        reason = _candidate(entry, policy)
        if reason:
            fields = ('artifact_id', 'path', 'size_bytes', 'sha256', 'parents')
            targets.append({**{key: entry[key] for key in fields}, 'reason': reason})
    plan = {'schema_version': 1, 'run_id': view.manifest['run_id'], 'manifest_sha256': view.digest,
            'policy': policy, 'targets': targets}
    packed = json.dumps(plan, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return {**plan, 'plan_sha256': hashlib.sha256(packed).hexdigest()}


def plan_retention(directory, policy):
    """Return a read-only plan whose hash binds the policy and original file identities."""
    return _plan(inspect_artifacts(directory, read_references=False), policy)


def _check_removable(root, entry):
    path = root / entry['path']
    identity = entry['artifact_id']
    if artifact_path(path) != path:
        raise ValueError('Retention target changed location')
    if _facts(path, identity, stored=True) != (entry['size_bytes'], entry['sha256']):
        raise ValueError('Retention target digest changed')
    info = path.lstat()
    if (not info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_READONLY
            or not os.access(path.parent, os.W_OK)):
        raise ValueError('Retention target is read-only')
    return path


def apply_retention(directory, policy, *, expected_plan_sha256):
    """Apply a previously inspected plan, retrying only work bound to the same artifact."""
    root = artifact_path(directory)
    manifest, digest = _open_manifest(root, None)
    if manifest['kind'] != 'run':
        raise ValueError('Retention applies to a native Run directory')
    directory, journal = _require_journal(manifest['run_id'])
    with _writer_lease(directory):
        # The same lease excludes the prediction/recovery writer and competing retention.
        view = inspect_artifacts(root, expected_manifest_sha256=digest, read_references=False)
        plan = _plan(view, policy)
        if expected_plan_sha256 != plan['plan_sha256']:
            raise ValueError('Retention approval does not match the current plan digest')

        for item in plan['targets']:
            if item['artifact_id'] in view.paths:
                _check_removable(root, item)
        attempt = view.publication[2]['attempt_id']
        finished = []
        for item in plan['targets']:
            identity = item['artifact_id']
            prior = view.retention.get(identity)
            if prior and prior['status'] == 'unavailable':
                finished.append(identity)
                continue
            path = view.paths.get(identity)
            if not prior:
                if path is None:
                    raise ValueError('Retention target disappeared before its intent was recorded')
                intent = {key: item[key] for key in ('artifact_id', 'sha256', 'size_bytes', 'parents')}
                intent.update(policy=policy, plan_sha256=plan['plan_sha256'], manifest_sha256=view.digest,
                              details={})
                journal.append(attempt, 'retention_started', 'retention.intent/' + identity, intent)
            if path is not None:
                # Recheck after the intent transaction before unlinking an owned file.
                _check_removable(root, item).unlink()
            result = {'artifact_id': identity, 'plan_sha256': prior['plan_sha256'] if prior else plan['plan_sha256'],
                      'reason': 'removed' if path is not None else 'missing_after_intent', 'details': {}}
            journal.append(attempt, 'artifact_unavailable', 'retention.result/' + identity, result)
            finished.append(identity)
        return {'run_id': plan['run_id'], 'policy': policy, 'plan_sha256': plan['plan_sha256'],
                'unavailable': finished}
