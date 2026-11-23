"""Stream the records member of the existing feature metadata JSON format."""
import json
from pathlib import Path

from .common import read_json, sha256
from .record_index import identity_digest


def matches_feature_contract(info, metadata, root, contract_digest, registry_digest):
    """Verify the producing registry or an exact IF-014 input under the same encoder."""
    if info.get('contract_sha256') != contract_digest:
        return False
    if info.get('assets_sha256') == registry_digest:
        return True

    catalog_path = root / 'config/frozen_feature_inputs.json'
    if not catalog_path.is_file():
        return False
    catalog = read_json(catalog_path)
    if (catalog.get('schema') != 1 or catalog['contract_sha256'] != contract_digest
            or catalog['source_registry_sha256'] != info.get('assets_sha256')):
        return False
    case = next((row for row in catalog['cases'] if row['features_sha256'] == info.get('features_sha256')), None)
    if case is None or sha256(metadata) != case['metadata_sha256']:
        return False

    registry = root / 'config/assets.json'
    if sha256(registry) != registry_digest:
        return False
    encoder = [row for row in read_json(registry)['files']
               if row['path'].startswith(('models/ntv3/', 'vendor/ntv3/'))]
    expected = catalog['encoder_assets']
    return (len(encoder) == len(expected)
            and {row['path']: row['sha256'] for row in encoder} == expected)


class _Tokens:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = ''
        self.cursor = 0
        self.decoder = json.JSONDecoder()
        self.eof = False

    def _fill(self):
        chunk = self.stream.read(65536)
        self.buffer = self.buffer[self.cursor:] + chunk
        self.cursor = 0
        self.eof = not chunk

    def peek(self):
        while True:
            while self.cursor < len(self.buffer):
                char = self.buffer[self.cursor]
                if char not in ' \t\r\n':
                    return char
                self.cursor += 1
            if self.eof:
                return ''
            self._fill()

    def consume(self, mark):
        if self.peek() != mark:
            raise ValueError(f'Invalid feature metadata JSON: expected {mark!r}')
        self.cursor += 1

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.cursor)
                if self.eof or (end < len(self.buffer) and self.buffer[end] in ',]}: \t\r\n'):
                    self.cursor = end
                    return value
            except json.JSONDecodeError:
                if self.eof:
                    raise
            if self.eof:
                raise ValueError('Invalid feature metadata JSON value')
            self._fill()

    def elements(self, start, end):
        self.consume(start)
        if self.peek() != end:
            while True:
                yield
                if self.peek() == end:
                    break
                self.consume(',')
        self.consume(end)


def read_feature_metadata(path):
    """Return other metadata and the canonical digest of ordered record identities."""
    result = {}
    count = 0

    def identities(tokens):
        nonlocal count
        for _ in tokens.elements('[', ']'):
            row = tokens.value()
            if not isinstance(row, dict):
                raise ValueError('Malformed feature record identity')
            count += 1
            yield row

    with Path(path).open(encoding='utf-8') as stream:
        tokens = _Tokens(stream)
        for _ in tokens.elements('{', '}'):
            field = tokens.value()
            if not isinstance(field, str) or field in result:
                raise ValueError('Invalid or duplicate feature metadata key')
            tokens.consume(':')
            result[field] = identity_digest(identities(tokens)) if field == 'records' else tokens.value()
        if tokens.peek() or 'records' not in result:
            raise ValueError('Feature metadata must declare ordered records')
    return result, count, result.pop('records')


def write_feature_metadata(path, metadata, identities):
    encoded = json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False)
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded[:-2] + ',\n  "records": [')
        separator = '\n'
        for identity in identities:
            stream.write(separator + '    ' + json.dumps(identity, ensure_ascii=False, allow_nan=False))
            separator = ',\n'
        stream.write('\n  ]\n}\n')



def verify_feature_header(path, count):
    import numpy as np

    with path.open('rb') as stream:
        magic = np.lib.format.read_magic(stream)
        if magic == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif magic in ((2, 0), (3, 0)):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError('Unsupported precomputed feature NPY header')
        payload_bytes = path.stat().st_size - stream.tell()
    if shape != (count, 1536) or dtype != np.dtype('float32') or payload_bytes != count * 1536 * 4:
        raise ValueError('Precomputed features must provide a complete float32 matrix with shape (record_count, 1536)')
