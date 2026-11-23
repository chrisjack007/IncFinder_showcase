"""Append-only SQLite events and the deterministic projection of a prediction run."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
from urllib.parse import quote
from uuid import UUID

from .output_metadata import REPORT_STATES


RECORD_VERSION = 1
DATABASE_NAME = 'run.sqlite3'
EVENT_VERSION = 1
TERMINAL = frozenset({'succeeded', 'preflight_failed', 'execution_failed', 'interrupted', 'cancelled'})
PREFLIGHT_STAGES = (
    'paths', 'arguments', 'parent', 'output_contract', 'feature_paths', 'input',
    'environment', 'model_location', 'package', 'scientific_contract', 'features',
    'input_policy', 'output_reservation', 'assets',
)
EXECUTION_STAGES = ('staging', 'embedding', 'classification', 'prediction_contract', 'report', 'result_validation')
STAGES = PREFLIGHT_STAGES + EXECUTION_STAGES
DETAIL_FIELDS = frozenset({
    'software', 'parent_run_id', 'parameters', 'interpreters', 'output_contract', 'input_sha256',
    'model', 'contract_sha256', 'runtime_policy_sha256', 'package', 'features',
    'research_preview', 'out_of_domain_count', 'diagnostic_directory', 'reproduction',
})
FAILURE_REASONS = frozenset({
    'preflight_error', 'execution_error', 'interrupted', 'file_not_found', 'destination_occupied',
    'permission_denied', 'io_error', 'invalid_type', 'invalid_value', 'argument_parsing_failed',
})

_EVENT_COLUMNS = ('sequence', 'run_id', 'attempt_id', 'event_type', 'event_version', 'event_key', 'recorded_utc', 'payload')
_PAYLOAD_FIELDS = {
    'run_created': {'entry_point'},
    'stage_entered': {'stage'},
    'computation_sealed': {'summary_sha256', 'artifact_manifest_sha256'},
    'publication_prepared': {'summary_sha256'},
    'publication_completed': set(),
    'succeeded': set(),
    'preflight_failed': {'reason'},
    'execution_failed': {'reason'},
    'interrupted': set(),
    'cancelled': set(),
    'publication_recovered': set(),
    'retention_started': {'artifact_id', 'plan_sha256', 'manifest_sha256', 'sha256', 'size_bytes', 'parents', 'policy'},
    'artifact_unavailable': {'artifact_id', 'plan_sha256', 'reason'},
}
RETENTION_EVENTS = {'retention_started', 'artifact_unavailable'}

_EVENT_TABLE = """
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    recorded_utc TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""
_PROJECTION_TABLE = """
CREATE TABLE IF NOT EXISTS projection (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL
)
"""
# Protect committed rows from accidental UPDATE/DELETE through the connection as well.
_UPDATE_GUARD = """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'Events are immutable'); END"""
_DELETE_GUARD = """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Events are immutable'); END"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_uuid(value):
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        return False
    return parsed is not None and parsed.version == 4 and str(parsed) == value


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash_is_valid(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _payload_fields(kind, payload):
    fields = _PAYLOAD_FIELDS.get(kind)
    if kind == 'publication_prepared' and isinstance(payload, dict):
        fields = fields | ({'artifact_manifest_sha256', 'report_status'} & payload.keys())
    if (fields is None or not isinstance(payload, dict) or set(payload) != fields | {'details'}
            or not isinstance(payload['details'], dict) or payload['details'].keys() - DETAIL_FIELDS
            or ('parameters' in payload['details'] and not isinstance(payload['details']['parameters'], dict))):
        raise ValueError('Invalid run event payload')
    return payload['details']


def _retention_state(state, event):
    """Replay a post-run change to file availability without changing the Run outcome."""
    payload = event['payload']
    identity = payload['artifact_id']
    if (not state or state['status'] == 'running' or event['attempt_id'] != state['attempt_id']
            or not state.get('publication', {}).get('artifact_manifest_sha256') or payload['details']
            or not isinstance(identity, str) or not identity.startswith(state['run_id'] + ':file/')
            or not _hash_is_valid(payload['plan_sha256'])):
        raise ValueError('Invalid retention event identity or Run state')

    entries = dict(state.get('artifact_retention', {}))
    recorded = entries.get(identity)
    timestamp = event['recorded_utc']
    if event['event_type'] == 'retention_started':
        parents = payload['parents']
        if (recorded is not None or not _hash_is_valid(payload['sha256'])
                or type(payload['size_bytes']) is not int or payload['size_bytes'] < 0
                or not isinstance(parents, list) or not all(isinstance(p, str) and p.startswith(state['run_id'] + ':')
                                                             for p in parents)
                or len(parents) != len(set(parents)) or payload['policy'] not in ('minimal', 'standard')
                or payload['manifest_sha256'] != state['publication']['artifact_manifest_sha256']):
            raise ValueError('Invalid retention intent or original artifact identity')
        entries[identity] = {key: payload[key] for key in ('sha256', 'size_bytes', 'parents', 'policy', 'plan_sha256')}
        entries[identity].update(status='pending', intent_utc=timestamp)
    else:
        if (not recorded or recorded['status'] != 'pending' or recorded['plan_sha256'] != payload['plan_sha256']
                or payload['reason'] not in ('removed', 'missing_after_intent')):
            raise ValueError('Unavailable artifact has no matching retention intent')
        entries[identity] = {**recorded, 'status': 'unavailable', 'reason': payload['reason'], 'recorded_utc': timestamp}
    return {**state, 'artifact_retention': entries}


def _reduce(state, event):
    """Apply one validated event using recorded values and time only."""
    kind, payload = event['event_type'], event['payload']
    details = _payload_fields(kind, payload)
    if (event['event_version'] != EVENT_VERSION or not canonical_uuid(event['attempt_id'])
            or not isinstance(event['event_key'], str) or not event['event_key']
            or datetime.fromisoformat(event['recorded_utc']).utcoffset() != timezone.utc.utcoffset(None)):
        raise ValueError('Invalid run event identity')
    if kind in RETENTION_EVENTS:
        return _retention_state(state, event)

    if state is None:
        if kind != 'run_created' or payload['entry_point'] not in ('api', 'cli', 'legacy_cli', 'reproduce'):
            raise ValueError('The first event must create the run')
        if not isinstance(details.get('parameters'), dict):
            raise ValueError('Run creation requires a parameter snapshot')
        return {
            'record_schema_version': RECORD_VERSION,
            'run_id': event['run_id'], 'attempt_id': event['attempt_id'],
            'entry_point': payload['entry_point'], 'started_utc': event['recorded_utc'],
            'status': 'running', 'stage': 'arguments', **details,
        }

    if kind == 'run_created' or state['status'] != 'running' or event['attempt_id'] != state['attempt_id']:
        raise ValueError('Invalid run event transition')
    result = {**state, **details}
    stage = state['stage']
    executing = stage in EXECUTION_STAGES or stage == 'publication'

    if kind == 'stage_entered':
        next_stage = payload['stage']
        if next_stage not in STAGES or stage == 'publication':
            raise ValueError('Invalid run stage')
        # Creation describes argument parsing. The first execution checkpoint
        # enters paths before the caller's argument values are validated.
        if not (event['sequence'] == 2 and next_stage == 'paths'):
            if STAGES.index(next_stage) <= STAGES.index(stage):
                raise ValueError('Run stages must advance in order')
        if next_stage in EXECUTION_STAGES and not executing and (stage, next_stage) != ('assets', 'staging'):
            raise ValueError('Execution must start after the asset preflight')
        result['stage'] = next_stage
    elif kind in ('computation_sealed', 'publication_prepared'):
        identity = {name: payload[name] for name in ('summary_sha256', 'artifact_manifest_sha256') if name in payload}
        if not all(_hash_is_valid(value) for value in identity.values()):
            raise ValueError('Publication requires validated artifact digests')
        if kind == 'computation_sealed':
            if stage != 'prediction_contract' or 'computation' in state:
                raise ValueError('Invalid computation checkpoint')
            result['computation'] = identity
        else:
            if stage != 'result_validation':
                raise ValueError('Publication requires result validation')
            if 'report_status' in payload:
                report_status = payload['report_status']
                if ('computation' not in state or not isinstance(report_status, str)
                        or report_status not in REPORT_STATES or 'artifact_manifest_sha256' not in identity):
                    raise ValueError('Invalid report publication status')
                identity['report_status'] = report_status
            elif 'computation' in state:
                raise ValueError('A sealed computation requires an explicit report status')
            result.update(stage='publication', publication=identity)
    else:
        if kind in ('succeeded', 'publication_completed', 'publication_recovered'):
            if stage != 'publication':
                raise ValueError('Completion requires a publication preparation event')
            report_status = state['publication'].get('report_status')
            if kind == 'succeeded' and report_status in ('failed', 'interrupted'):
                raise ValueError('A partial result is not a successful requested report')
            if kind == 'publication_completed' and report_status not in ('failed', 'interrupted'):
                raise ValueError('A partial publication requires a failed or interrupted report')
            if report_status == 'failed':
                result.update(status='execution_failed', stage='complete',
                              error={'stage': 'report', 'reason': 'execution_error'})
            elif report_status == 'interrupted':
                result.update(status='interrupted', stage='complete',
                              error={'stage': 'report', 'reason': 'interrupted'})
            else:
                result.update(status='succeeded', stage='complete')
        elif kind in ('interrupted', 'cancelled'):
            result.update(status=kind, error={'stage': stage, 'reason': kind})
        else:
            reason = payload['reason']
            if (not isinstance(reason, str) or reason not in FAILURE_REASONS
                    or (kind == 'execution_failed') != executing):
                raise ValueError('Failure event does not match the run phase')
            result.update(status=kind, error={'stage': stage, 'reason': reason})
        result['completed_utc'] = event['recorded_utc']
    return result


class EventJournal:
    """Read, append, or rebuild one run using a single short SQLite transaction."""

    def __init__(self, directory, run_id):
        self.path = directory / DATABASE_NAME
        self.run_id = run_id

    @contextmanager
    def _connect(self, mode, initialize=False):
        # Four slashes also preserve a Windows UNC path as a filename rather
        # than treating the server name as a SQLite URI authority.
        name = self.path.absolute().as_posix()
        uri = 'file:' + ('//' if name.startswith('//') else '') + quote(name, safe='/:') + '?mode=' + mode
        try:
            with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                if mode != 'ro':
                    db.execute('PRAGMA synchronous = FULL')
                    db.execute('PRAGMA recursive_triggers = ON')
                with db:
                    db.execute('BEGIN' if mode == 'ro' else 'BEGIN IMMEDIATE')
                    version = db.execute('PRAGMA user_version').fetchone()[0]
                    if version != (0 if initialize else EVENT_VERSION):
                        raise ValueError('Unsupported run event database version')
                    yield db
        except sqlite3.Error as exc:
            raise RuntimeError(f'Cannot access run event database {self.path.name}') from exc

    @staticmethod
    def _decode(row):
        event = dict(row)
        event['payload'] = json.loads(event['payload'])
        return event

    def _replay(self, db):
        state = None
        events = []
        for row in db.execute('SELECT * FROM events ORDER BY sequence'):
            event = self._decode(row)
            if event['run_id'] != self.run_id or event['sequence'] != len(events) + 1:
                raise ValueError('Unexpected run ID or event sequence')
            state = _reduce(state, event)
            events.append(event)
        if state is None:
            raise ValueError('The run event database has no committed creation event')
        return events, state

    @staticmethod
    def _save_projection(db, sequence, state):
        db.execute(_PROJECTION_TABLE)
        db.execute('INSERT INTO projection VALUES (1, ?, ?) '
                   'ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence, state=excluded.state',
                   (sequence, _json(state)))

    def _append(self, db, events, state, attempt_id, event_type, event_key, payload):
        row = db.execute('SELECT * FROM events WHERE event_key = ?', (event_key,)).fetchone()
        packed = _json(payload)
        if row is not None:
            if (row['attempt_id'], row['event_type'], row['payload']) != (attempt_id, event_type, packed):
                raise ValueError('Conflicting payload for an existing run event key')
            return self._decode(row), state

        event = dict(zip(_EVENT_COLUMNS, (
            len(events) + 1, self.run_id, attempt_id, event_type, EVENT_VERSION, event_key, utc_now(), json.loads(packed),
        )))
        updated = _reduce(state, event)
        db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (*[event[name] for name in _EVENT_COLUMNS[:-1]], packed))
        self._save_projection(db, event['sequence'], updated)
        return event, updated

    def create(self, attempt_id, payload):
        with self._connect('rwc', initialize=True) as db:
            for sql in (_EVENT_TABLE, _UPDATE_GUARD, _DELETE_GUARD):
                db.execute(sql)
            db.execute(f'PRAGMA user_version = {EVENT_VERSION}')
            return self._append(db, [], None, attempt_id, 'run_created', 'run.created', payload)

    def append(self, attempt_id, event_type, event_key, payload):
        with self._connect('rw') as db:
            events, state = self._replay(db)
            return self._append(db, events, state, attempt_id, event_type, event_key, payload)

    def read(self):
        with self._connect('ro') as db:
            return self._replay(db)

    def rebuild(self):
        with self._connect('rw') as db:
            events, state = self._replay(db)
            self._save_projection(db, events[-1]['sequence'], state)
            return state
