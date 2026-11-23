"""Local, versioned prediction records and exclusive result publication."""
from contextlib import ExitStack, contextmanager, nullcontext
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from . import __version__
from .artifact_manifest import MANIFEST, SEAL, restore_computation, verify_published_artifacts
from .common import read_json, record_path_value, sha256
from .output_metadata import RESULT_FILES
from .run_events import (DETAIL_FIELDS, EXECUTION_STAGES, RECORD_VERSION, RETENTION_EVENTS, TERMINAL,
                         EventJournal, canonical_uuid)


CHECKPOINT_LEASE = '.encoding-writer.lock'
CHECKPOINT_REQUEST = 'encoding_request.json'


def _record_root():
    custom = os.environ.get('INCFINDER_RUN_DIR')
    if custom:
        return Path(custom).expanduser().resolve()
    if os.name == 'nt':
        appdata = os.environ.get('LOCALAPPDATA')
        state = Path(appdata) if appdata else Path.home() / 'AppData/Local'
        return state / 'IncFinder/runs'
    state = Path(os.environ.get('XDG_STATE_HOME', ''))
    return (state if state.is_absolute() else Path.home() / '.local/state') / 'incfinder/runs'


def _is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _run_directory(run_id):
    if not canonical_uuid(run_id):
        raise ValueError('Run ID must be a canonical version-4 UUID')
    directory = _record_root() / run_id
    if _is_link(directory):
        raise ValueError('A run record must not be a link')
    return directory


def _journal(directory, run_id):
    journal = EventJournal(directory, run_id)
    if _is_link(journal.path):
        raise ValueError('A run event database must not be a link')
    return journal


def _require_journal(run_id):
    directory = _run_directory(run_id)
    journal = _journal(directory, run_id)
    if not journal.path.is_file():
        raise FileNotFoundError('This run has no SQLite event journal')
    return directory, journal


def load_run_record(run_id):
    """Read the event-derived state, or the historical IF-006 JSON record."""
    directory = _run_directory(run_id)
    journal = _journal(directory, run_id)
    if journal.path.exists():
        return journal.read()[1]
    path = directory / 'run.json'
    if _is_link(path):
        raise ValueError('A run record must not be a link')
    record = read_json(path)
    if isinstance(record, dict) and 'attempt_id' in record:
        raise FileNotFoundError('The authoritative event history for this run is missing')
    if (not isinstance(record, dict) or type(record.get('record_schema_version')) is not int
            or record['record_schema_version'] != RECORD_VERSION or record.get('run_id') != run_id
            or record.get('status') not in ('running', *TERMINAL)
            or not isinstance(record.get('started_utc'), str)
            or not isinstance(record.get('parameters'), dict)
            or (record['status'] in TERMINAL and not isinstance(record.get('completed_utc'), str))):
        raise ValueError('Invalid run record')
    return record


def load_run_events(run_id):
    """Read the committed, ordered SQLite events for a prediction run."""
    _, journal = _require_journal(run_id)
    return journal.read()[0]


@contextmanager
def _writer_lease(directory, name='.event-writer.lock'):
    # Keep the lease file identity stable. Closing the descriptor releases
    # the OS lock on both normal exit and process termination.
    path = directory / name
    if _is_link(path):
        raise ValueError('A run writer lease must not be a link')
    with path.open('a+b') as lease:
        try:
            if os.name == 'nt':
                import msvcrt
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK):
                raise RuntimeError('The run has an active writer') from None
            raise
        yield


def _write_snapshot(path, state):
    pending = path.with_suffix('.tmp')
    if _is_link(pending):
        raise OSError('Compatibility snapshot staging path must not be a link')
    try:
        with pending.open('w', encoding='ascii') as stream:
            json.dump(state, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def rebuild_run_state(run_id):
    """Regenerate the SQLite projection and run.json from committed events."""
    directory, journal = _require_journal(run_id)
    with _writer_lease(directory):
        state = journal.rebuild()
        _write_snapshot(directory / 'run.json', state)
        return state


def _recorded_output(state):
    parameter = state['parameters'].get('output')
    value = parameter.get('value') if isinstance(parameter, dict) else None
    return Path(value) if isinstance(value, str) and Path(value).is_absolute() else None


def computation_staging(output, run_id):
    return output.parent / f'.ntv3-run-{run_id}'


def _verified_publication(state, directory=None):
    preparation = state.get('publication')
    directory = _recorded_output(state) if directory is None else directory
    if not preparation or directory is None:
        return False
    try:
        if not directory.is_dir() or directory.resolve() != directory:
            return False
        summary_path = directory / 'run_summary.json'
        if _is_link(summary_path) or not summary_path.is_file():
            return False
        content = summary_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != preparation['summary_sha256']:
            return False
        summary = json.loads(content)
        if (not isinstance(summary, dict) or summary.get('run_id') != state['run_id']
                or summary.get('execution_status') != 'PASS'):
            return False
        if ('report_status' in preparation
                and summary.get('delivery', {}).get('report_status') != preparation['report_status']):
            return False
        manifest_hash = preparation.get('artifact_manifest_sha256')
        if manifest_hash is not None:
            verify_published_artifacts(directory, run_id=state['run_id'], manifest_sha256=manifest_hash)
            return True
        hashes = summary.get('outputs')
        required = {*RESULT_FILES, *(name + '.metadata.json' for name in RESULT_FILES), 'prediction.json'}
        if not isinstance(hashes, dict) or not required.issubset(hashes):
            return False
        for name, digest in hashes.items():
            if not name or name in ('.', '..') or any(separator in name for separator in '/\\:'):
                return False
            path = directory / name
            if _is_link(path) or not path.is_file() or sha256(path) != digest:
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def publication_events(root, manifest, digest, *, include_state=False):
    """Bind a native result to its local journal when that journal is available."""
    if manifest['kind'] == 'archive':
        return None
    try:
        _, journal = _require_journal(manifest['run_id'])
        events, state = journal.read()
    except FileNotFoundError:
        return None
    prepared = next((event for event in events if event['event_type'] == 'publication_prepared'), None)
    terminal = next(event for event in reversed(events) if event['event_type'] not in RETENTION_EVENTS)
    if (terminal['event_type'] not in ('succeeded', 'publication_completed', 'publication_recovered')
            or prepared is None or prepared['payload'].get('artifact_manifest_sha256') != digest
            or prepared['payload'].get('summary_sha256') != sha256(root / 'run_summary.json')):
        raise ValueError('Local run event history does not bind a completed result')
    return (events, prepared, state) if include_state else (events, prepared)


def _recover_computation(directory, journal, state):
    output = _recorded_output(state)
    if output is None or output.parent.resolve() != output.parent:
        raise ValueError('The staged computation has no usable recorded output path')
    staging = computation_staging(output, state['run_id'])
    attempt = state['attempt_id']
    with reserve_output(output, state['run_id'], recover=True):
        if 'publication' in state:
            if not _verified_publication(state, staging):
                raise ValueError('Prepared result does not match its recorded artifact digests')
        else:
            identity = state['computation']
            summary = restore_computation(staging, directory / 'computation', state['run_id'], identity)
            if state['stage'] != 'result_validation':
                journal.append(attempt, 'stage_entered', 'stage:result_validation',
                               {'stage': 'result_validation', 'details': {}})
            journal.append(attempt, 'publication_prepared', 'publication.prepare',
                           {**identity, 'report_status': summary['delivery']['report_status'], 'details': {}})
        publish_output(staging, output)


def reconcile_run(run_id):
    """Record the verified outcome of a run whose writer has exited."""
    directory, journal = _require_journal(run_id)
    with _writer_lease(directory):
        encoding_lease = directory / CHECKPOINT_LEASE
        guard = _writer_lease(directory, CHECKPOINT_LEASE) if os.path.lexists(encoding_lease) else nullcontext()
        with guard:
            return _reconcile(directory, journal)


def _reconcile(directory, journal):
    # Open for writing under the lease so SQLite can recover a hot journal.
    state = journal.rebuild()
    if state['status'] not in TERMINAL:
        published = _verified_publication(state)
        if 'computation' in state:
            if not published:
                _recover_computation(directory, journal, state)
                published = True
            else:
                _release_output(_output_reservation(_recorded_output(state)), state['run_id'])
        kind = 'publication_recovered' if published else 'interrupted'
        _, state = journal.append(state['attempt_id'], kind, 'run.terminal', {'details': {}})
    _write_snapshot(directory / 'run.json', state)
    return state


class RunRecord:
    """Own the run's writer lease and record launcher lifecycle events."""

    def __init__(self, entry_point):
        self.run_id = str(uuid4())
        self.attempt_id = str(uuid4())
        directory = _record_root() / self.run_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.path = directory / 'run.json'
        self.data = {
            'parent_run_id': None,
            'software': {'name': 'NT_V3_650M_TSRF_SW', 'version': __version__},
            'parameters': {},
            'interpreters': {
                'launcher': {'path': record_path_value(sys.executable),
                             'python_version': '.'.join(map(str, sys.version_info[:3]))}
            },
        }
        self._lease = ExitStack()
        self._closed = False
        try:
            self._lease.enter_context(_writer_lease(directory))
            self._journal = _journal(directory, self.run_id)
            _, self._state = self._journal.create(self.attempt_id, {'entry_point': entry_point, 'details': self.data})
            self.data['started_utc'] = self._state['started_utc']
            _write_snapshot(self.path, self._state)
        except BaseException:
            self._lease.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._closed = True
        self._lease.close()

    @property
    def stage(self):
        return self._state['stage']

    @property
    def reference(self):
        return f'Run ID {self.run_id} (record: {self.path})'

    def _append(self, kind, key, **payload):
        if self._closed:
            raise RuntimeError('The run writer lease is closed')
        payload['details'] = {name: value for name, value in self.data.items() if name in DETAIL_FIELDS}
        _, self._state = self._journal.append(self.attempt_id, kind, key, payload)
        _write_snapshot(self.path, self._state)

    def enter_stage(self, stage):
        self._append('stage_entered', 'stage:' + stage, stage=stage)

    @property
    def has_computation(self):
        return 'computation' in self._state

    def seal_computation(self, staging, manifest_sha256):
        snapshot = self.path.parent / 'computation'
        snapshot.mkdir(mode=0o700, exist_ok=False)
        for name in ('run_summary.json', MANIFEST, SEAL):
            (snapshot / name).write_bytes((staging / name).read_bytes())
        self._append('computation_sealed', 'computation.seal', summary_sha256=sha256(snapshot / 'run_summary.json'),
                     artifact_manifest_sha256=manifest_sha256)

    def prepare_publication(self, summary, *, artifact_manifest_sha256=None, report_status=None):
        identity = {'summary_sha256': sha256(summary)}
        if artifact_manifest_sha256 is not None:
            identity['artifact_manifest_sha256'] = artifact_manifest_sha256
        if report_status is not None:
            identity['report_status'] = report_status
        self._append('publication_prepared', 'publication.prepare', **identity)

    def prepare_checkpoints(self, identity, resume):
        directory = self.path.parent
        (directory / CHECKPOINT_LEASE).touch(exist_ok=False)
        _write_snapshot(directory / CHECKPOINT_REQUEST, {
            'run_id': self.run_id, 'attempt_id': self.attempt_id, 'identity': identity, 'resume': resume,
        })

    def link_parent(self, parent_run_id):
        if parent_run_id is None:
            return
        if load_run_record(parent_run_id)['status'] not in TERMINAL:
            raise ValueError('parent_run_id must refer to a completed run')
        self.data['parent_run_id'] = parent_run_id

    def failure_details(self, exc):
        reason = 'execution_error' if self._executing() else 'preflight_error'
        for error_type, code in (
            (KeyboardInterrupt, 'cancelled'),
            (FileNotFoundError, 'file_not_found'),
            (FileExistsError, 'destination_occupied'),
            (PermissionError, 'permission_denied'),
            (OSError, 'io_error'),
            (TypeError, 'invalid_type'),
            (ValueError, 'invalid_value'),
            (SystemExit, 'argument_parsing_failed'),
        ):
            if isinstance(exc, error_type):
                reason = code
                break
        return {'stage': self.stage, 'reason': reason}

    def _executing(self):
        return self.stage in EXECUTION_STAGES or self.stage == 'publication'

    def has_verified_publication(self):
        return _verified_publication(self._state)

    def fail(self, exc):
        if isinstance(exc, KeyboardInterrupt):
            self._append('cancelled', 'run.terminal')
        else:
            kind = 'execution_failed' if self._executing() else 'preflight_failed'
            self._append(kind, 'run.terminal', reason=self.failure_details(exc)['reason'])

    def succeed(self):
        status = self._state.get('publication', {}).get('report_status')
        kind = 'publication_completed' if status in ('failed', 'interrupted') else 'succeeded'
        self._append(kind, 'run.terminal')


def output_path(value, base):
    """Resolve parent aliases without following an existing destination's final component."""
    path = (Path.cwd() if base is None else Path(base)) / Path(value).expanduser()
    if path.name and path.name != '..':
        return path.parent.resolve() / path.name
    return path.resolve()


def require_new_output(output):
    if os.path.lexists(output):
        raise FileExistsError(f'Output directory already exists, refusing to overwrite: {output}')


def _output_reservation(output):
    key = hashlib.sha256(os.fsencode(os.path.normcase(output.name))).hexdigest()
    return output.parent / f'.incfinder-output-{key}.lock'


def _owns_output(reservation, run_id):
    return (not _is_link(reservation) and reservation.is_file()
            and reservation.stat().st_size == len(run_id) + len(os.linesep)
            and reservation.read_text(encoding='ascii', errors='replace') == run_id + '\n')


def _release_output(reservation, run_id):
    if _owns_output(reservation, run_id):
        reservation.unlink(missing_ok=True)


@contextmanager
def reserve_output(output, run_id, *, recover=False):
    """Serialize publication, or claim the inactive run's own reservation."""
    require_new_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    reservation = _output_reservation(output)
    try:
        with reservation.open('x', encoding='ascii') as stream:
            stream.write(run_id + '\n')
    except FileExistsError:
        if not recover or not _owns_output(reservation, run_id):
            raise FileExistsError(f'Another prediction has reserved this output: {output} ({reservation})') from None
    try:
        require_new_output(output)
        yield
    finally:
        _release_output(reservation, run_id)


def publish_output(staging, output):
    """Publish one complete result directory without replacing a destination."""
    require_new_output(output)
    if sys.platform != 'linux':
        # On Windows, rename fails when the destination already exists.
        staging.rename(output)
        return

    # Unix rename can replace an empty directory. Request the Linux no-replace
    # operation so an external creator cannot race the final existence check.
    import ctypes

    rename_at2 = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename_at2 is None:
        raise OSError('Atomic output publication requires Linux renameat2 support')
    rename_at2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
    rename_at2.restype = ctypes.c_int
    current_directory, no_replace = -100, 1  # AT_FDCWD, RENAME_NOREPLACE
    if rename_at2(current_directory, os.fsencode(staging),
                  current_directory, os.fsencode(output), no_replace):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(output))
