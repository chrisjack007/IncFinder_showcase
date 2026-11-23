"""Durable per-Run encoding batches, verified before reuse or assembly."""
from collections import defaultdict
from itertools import groupby, islice
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
from urllib.parse import quote

from .artifact_manifest import artifact_path
from .common import ROOT, read_json, sha256
from .resources import path_is_link
from .run_record import CHECKPOINT_LEASE, CHECKPOINT_REQUEST, _run_directory, _writer_lease, load_run_record


FEATURE_WIDTH = 1536
_BATCH_DTYPE = '<f4'
_SCHEMA = 1
DATABASE = 'checkpoints.sqlite3'
RESUMABLE = frozenset({'cancelled', 'interrupted', 'execution_failed'})


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def identity_sha256(value):
    return hashlib.sha256(packed(value).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class Batch:
    id: str
    padded_length: int
    rows: tuple[int, ...]


def plan_batches(records, identities, batch_size):
    """Keep the encoder's existing bucket order, batch composition and original rows."""
    grouped = defaultdict(list)
    for index, record in enumerate(records):
        grouped[((len(record.sequence) + 127) // 128) * 128].append(index)
    for padded_length, indices in sorted(grouped.items()):
        for offset in range(0, len(indices), batch_size):
            rows = tuple(indices[offset:offset + batch_size])
            yield Batch(identity_sha256([padded_length, [identities[index] for index in rows]]), padded_length, rows)


def plan_index_batches(records, batch_size):
    for padded_length, group in groupby(records.batch_identities(), key=lambda row: row[0]):
        while identities := [identity for _, identity in islice(group, batch_size)]:
            rows = tuple(identity['row_index'] for identity in identities)
            yield Batch(identity_sha256([padded_length, identities]), padded_length, rows)


# Exact published IF-009 through IF-011 encoder hashes. IF-012 preserves the
# parser identities, bucket composition and FP32 forward/pooling computation.
_PRE_STREAM_ENCODER = {
    'embedding.py': '826f117334902312d3de075102ece87047f412e84ac282b56b378c7fc04df528',
    'sequence_io.py': 'bf75469cb8bd25cb5e448545178a82c14f068c9e36edd6d20359f2adefe38758',
    'checkpoints.py': '8b7a72fd9905980d3723a4cf20fd0d6a82b4d6a0cb24f22dc86d87eb2ce7eb72',
}


def _lock_identity():
    metadata = Path(sys.prefix) / 'conda-meta'
    if metadata.is_dir():
        packages = [read_json(path) for path in metadata.glob('*.json')]
        return identity_sha256(sorted((entry['name'], entry['version'], entry['build'], entry.get('sha256') or '')
                                      for entry in packages))
    return identity_sha256(sorted((dist.metadata.get('Name') or '', dist.version)
                                   for dist in importlib.metadata.distributions()))


def encoding_runtime(versions, torch, device):
    """Record effective package builds and hardware used by the encoder."""
    cuda = None
    if device == 'cuda':
        cuda = {
            'name': torch.cuda.get_device_name(0), 'capability': list(torch.cuda.get_device_capability(0)),
            'runtime': torch.version.cuda, 'cudnn': torch.backends.cudnn.version(),
            'matmul_tf32': bool(torch.backends.cuda.matmul.allow_tf32), 'cudnn_tf32': bool(torch.backends.cudnn.allow_tf32),
        }
    return {
        'versions': versions, 'python_build': sys.version, 'locked_packages_sha256': _lock_identity(),
        'system': platform.system(), 'machine': platform.machine(), 'host_sha256': identity_sha256(platform.node()),
        'torch_build': torch.__version__, 'cpu_capability': torch.backends.cpu.get_cpu_capability(), 'cuda': cuda,
    }


def _regular(path):
    return not path_is_link(path) and path.is_file() and path.stat().st_nlink == 1


@contextmanager
def _database(path, *, create=False):
    path = artifact_path(path)
    if create:
        if os.path.lexists(path):
            raise FileExistsError('Checkpoint database already exists')
    elif not _regular(path):
        raise ValueError('Previous run has no trustworthy checkpoint database')
    for suffix in ('-journal', '-wal', '-shm'):
        sidecar = path.with_name(path.name + suffix)
        if os.path.lexists(sidecar) and not _regular(sidecar):
            raise ValueError('Invalid checkpoint database sidecar')
    name = path.absolute().as_posix()
    uri = 'file:' + ('//' if name.startswith('//') else '') + quote(name, safe='/:') + ('?mode=rwc' if create else '?mode=rw')
    # Only access the previous database with the run's encoding-writer lease held.
    # Read-write mode permits SQLite to recover a hot rollback journal after a kill.
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.execute('PRAGMA trusted_schema = OFF')
            db.execute('PRAGMA synchronous = FULL')
            if not create and db.execute('PRAGMA user_version').fetchone()[0] != _SCHEMA:
                raise ValueError('Unsupported checkpoint database version')
            yield db
    except sqlite3.Error as exc:
        raise ValueError('Cannot access encoding checkpoint database') from exc


def _load_block(path, rows, entry):
    import numpy as np

    if entry is None:
        return None
    size = len(rows) * FEATURE_WIDTH * np.dtype(_BATCH_DTYPE).itemsize
    expected_size, expected_sha256 = entry
    if expected_size != size:
        return None
    try:
        if not _regular(path) or path.stat().st_size != size:
            return None
        with path.open('rb') as stream:
            data = stream.read(size + 1)
    except OSError:
        return None
    if len(data) != size or hashlib.sha256(data).hexdigest() != expected_sha256:
        return None
    values = np.frombuffer(data, dtype=_BATCH_DTYPE).reshape(len(rows), FEATURE_WIDTH)
    return values if np.isfinite(values).all() else None


class CheckpointStore:
    """Only rows committed in SQLite are candidates for reuse."""

    def __init__(self, folder, db, run, identity, previous=None):
        self.folder, self.db, self.previous = folder, db, previous
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE identity (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, value TEXT NOT NULL)')
            db.execute('CREATE TABLE batches (id TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, '
                       'sha256 TEXT NOT NULL, reused_from_run_id TEXT)')
            db.execute('INSERT INTO identity VALUES (?, ?, ?)', (run['run_id'], run['attempt_id'], packed(identity)))
            db.execute(f'PRAGMA user_version = {_SCHEMA}')

    @staticmethod
    def _read(folder, db, batch):
        row = db.execute('SELECT size_bytes, sha256 FROM batches WHERE id = ?', (batch.id,)).fetchone()
        return _load_block(folder / (batch.id + '.f32le'), batch.rows, row)

    def _register(self, batch, size, digest, reused_from):
        with self.db:
            self.db.execute('INSERT INTO batches VALUES (?, ?, ?, ?)', (batch.id, size, digest, reused_from))

    def save(self, batch, values, *, reused_from=None):
        import numpy as np

        if (values.shape != (len(batch.rows), FEATURE_WIDTH) or values.dtype != np.dtype('float32')
                or not np.isfinite(values).all()):
            raise ValueError('Invalid encoding checkpoint values')
        content = values.astype(_BATCH_DTYPE, copy=False).tobytes(order='C')
        result = self.folder / (batch.id + '.f32le')
        pending = result.with_suffix('.tmp')
        if os.path.lexists(result):
            raise FileExistsError('A checkpoint block already exists in the new run')
        digest = hashlib.sha256(content).hexdigest()
        with pending.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _load_block(pending, batch.rows, (len(content), digest)) is None:
            raise ValueError('Checkpoint block failed write verification')
        pending.replace(result)
        if sys.platform == 'linux':
            directory = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self._register(batch, len(content), digest, reused_from)

    def reuse(self, batch):
        if self.previous is None:
            return False
        folder, db, run_id = self.previous
        values = self._read(folder, db, batch)
        if values is None:
            return False
        self.save(batch, values, reused_from=run_id)
        return True

    def assemble(self, batches, rows, destination):
        import numpy as np

        expected_count = self.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0]
        covered = np.zeros(rows, dtype=bool)
        count = 0
        values = np.lib.format.open_memmap(destination, mode='w+', dtype=np.float32, shape=(rows, FEATURE_WIDTH))
        try:
            for batch in batches:
                indices = list(batch.rows)
                if (not indices or len(set(indices)) != len(indices) or min(indices) < 0 or max(indices) >= rows
                        or covered[indices].any()):
                    raise ValueError('Checkpoint plan contains invalid or repeated rows')
                data = self._read(self.folder, self.db, batch)
                if data is None:
                    raise ValueError('Committed checkpoint block failed final verification')
                values[indices] = data
                covered[indices] = True
                count += 1
            if not covered.all() or count != expected_count:
                raise ValueError('Checkpoint ledger does not cover the complete batch plan')
            values.flush()
        finally:
            del values


def _previous(stack, resume, identity):
    if resume is None:
        return None
    run_id = resume['run_id']
    directory = _run_directory(run_id)
    stack.enter_context(_writer_lease(directory, CHECKPOINT_LEASE))
    state = load_run_record(run_id)
    if state['status'] not in RESUMABLE or state['attempt_id'] != resume['attempt_id']:
        raise ValueError('Checkpoint parent is not a matching interrupted, cancelled or failed run')
    folder = artifact_path(directory / 'checkpoints')
    db = stack.enter_context(_database(folder / DATABASE))
    rows = db.execute('SELECT run_id, attempt_id, value FROM identity').fetchall()
    if len(rows) != 1 or rows[0][:2] != (run_id, resume['attempt_id']):
        raise ValueError('Checkpoint database is not bound to the parent run attempt')
    previous_identity = json.loads(rows[0][2])
    if not isinstance(previous_identity, dict) or previous_identity.keys() != identity.keys():
        raise ValueError('Checkpoint identity has an unsupported structure')
    different = {key for key in identity if packed(previous_identity[key]) != packed(identity[key])}
    if previous_identity['encoder'] == _PRE_STREAM_ENCODER:
        different.discard('encoder')
    if different:
        raise ValueError('Checkpoint identity mismatch: ' + ', '.join(sorted(different)))
    return folder, db, run_id


@contextmanager
def managed_checkpoints(run_id, input_path, identities, *, device, batch_size, threads, runtime):
    directory = _run_directory(run_id)
    with _writer_lease(directory, CHECKPOINT_LEASE), ExitStack() as stack:
        state = load_run_record(run_id)
        request_file = artifact_path(directory / CHECKPOINT_REQUEST)
        if not _regular(request_file):
            raise ValueError('Missing or linked encoding checkpoint request')
        request = read_json(request_file)
        if (state['status'] != 'running' or state['stage'] != 'embedding'
                or request['run_id'] != run_id or request['attempt_id'] != state['attempt_id']):
            raise ValueError('Encoding checkpoints require a live, matching run attempt')
        identity = request['identity']
        from .record_index import identity_digest

        actual = {
            'input_sha256': sha256(input_path), 'records_sha256': identity_digest(identities),
            'model_manifest_sha256': sha256(ROOT / 'config/assets.json'),
            'contract_sha256': sha256(ROOT / 'config/contract.json'),
            'runtime_policy_sha256': sha256(ROOT / 'config/runtime.json'),
            'output_contract_sha256': sha256(ROOT / 'config/output_contract.json'),
        }
        if any(identity[key] != value for key, value in actual.items()):
            raise ValueError('Input or contract changed before the encoding worker started')
        if any(identity['parameters'][name] != value for name, value in (
                ('device', device), ('batch_size', batch_size), ('threads', threads))):
            raise ValueError('Encoding worker arguments differ from the run parameters')
        code = Path(__file__).parent
        files = ('embedding.py', 'sequence_io.py', 'checkpoints.py', 'record_index.py', 'feature_metadata.py')
        identity = {**identity, 'encoder': {name: sha256(code / name) for name in files}, 'environment': runtime}
        previous = _previous(stack, request['resume'], identity)
        folder = directory / 'checkpoints'
        folder.mkdir(mode=0o700, exist_ok=False)
        db = stack.enter_context(_database(folder / DATABASE, create=True))
        yield CheckpointStore(folder, db, request, identity, previous)
