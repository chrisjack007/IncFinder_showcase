"""A temporary, identity-checked row index for streaming the existing sequence format."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .artifact_manifest import artifact_path
from .common import sha256
from .sequence_io import DUPLICATE_ID, Record, iter_records

_IDENTITY_COLUMNS = 'row_index, record_id, header, length_bp, input_format, sequence_sha256'


def identity_digest(identities):
    """Hash the same canonical JSON list as the original checkpoint contract."""
    digest = hashlib.sha256(b'[')
    separator = b''
    for identity in identities:
        digest.update(separator)
        digest.update(json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                                 allow_nan=False).encode('ascii'))
        separator = b','
    digest.update(b']')
    return digest.hexdigest()


def _rows(source):
    for row_index, record in enumerate(iter_records(source)):
        identity = record.identity(row_index)
        yield (
            row_index, identity['record_id'], record.header, identity['length_bp'], record.input_format,
            identity['sequence_sha256'], ((identity['length_bp'] + 127) // 128) * 128, record.sequence,
        )


def _create(source, path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE records (
            row_index INTEGER PRIMARY KEY, record_id TEXT NOT NULL UNIQUE,
            header TEXT NOT NULL, length_bp INTEGER NOT NULL, input_format TEXT NOT NULL,
            sequence_sha256 TEXT NOT NULL, padded_length INTEGER NOT NULL, sequence TEXT NOT NULL
        )''')
        db.execute('CREATE INDEX batch_order ON records (padded_length, row_index)')
        try:
            db.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)', _rows(source))
        except sqlite3.IntegrityError as exc:
            raise ValueError(DUPLICATE_ID) from exc


class RecordIndex:
    def __init__(self, path, digest, db):
        self.path = path
        self.sha256 = digest
        self.db = db
        self.count = db.execute('SELECT COUNT(*) FROM records').fetchone()[0]

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        row = self.db.execute(
            'SELECT header, sequence, input_format FROM records WHERE row_index = ?', (index,),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return Record(*row)

    def identities(self):
        cursor = self.db.execute(f'SELECT {_IDENTITY_COLUMNS} FROM records ORDER BY row_index')
        names = [column[0] for column in cursor.description]
        for row in cursor:
            yield dict(zip(names, row))

    def batch_identities(self):
        cursor = self.db.execute(f'SELECT {_IDENTITY_COLUMNS}, padded_length FROM records ORDER BY padded_length, row_index')
        names = [column[0] for column in cursor.description[:-1]]
        for row in cursor:
            yield row[-1], dict(zip(names, row[:-1]))

    def headers(self):
        yield from self.db.execute('SELECT header, length_bp FROM records ORDER BY row_index')


@contextmanager
def _open_index(path, expected):
    path = artifact_path(path)
    digest = sha256(path)
    if digest != expected:
        raise ValueError('Temporary record index SHA-256 mismatch')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        yield RecordIndex(path, digest, db)


@contextmanager
def indexed_records(source, *, temp_dir=None, index_path=None, index_sha256=None):
    if index_path is not None:
        with _open_index(index_path, index_sha256) as records:
            yield records
    else:
        with TemporaryDirectory(prefix='incfinder-records-', dir=temp_dir) as temporary:
            path = Path(temporary) / 'input.sqlite3'
            _create(source, path)
            with _open_index(path, sha256(path)) as records:
                yield records
