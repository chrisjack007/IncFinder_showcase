"""Strict FASTA/FASTQ parser, including gzip and wrapped records."""
from dataclasses import dataclass
import gzip
import hashlib
from pathlib import Path


DUPLICATE_ID = 'Duplicate sequence ID (first header token); results cannot be aligned reliably'


@dataclass(frozen=True)
class Record:
    header: str
    sequence: str
    input_format: str

    @property
    def record_id(self):
        return self.header.split()[0]

    def identity(self, index):
        return {'row_index': index, 'record_id': self.record_id, 'header': self.header,
                'length_bp': len(self.sequence), 'input_format': self.input_format,
                'sequence_sha256': hashlib.sha256(self.sequence.encode('ascii')).hexdigest()}


def reverse_complement(sequence):
    return sequence.translate(str.maketrans('ACGTN', 'TGCAN'))[::-1]


def _record(header, sequence, kind):
    header = header.strip()
    if not header or len(header) > 32000 or any(ord(c) < 32 for c in header):
        raise ValueError('Sequence header is empty, too long or contains control characters')
    sequence = sequence.upper()
    if not sequence:
        raise ValueError(f'Empty sequence: {header}')
    invalid = set(sequence) - set('ACGTN')
    if invalid:
        raise ValueError(f'{header}: Only ACGTN is supported by the upstream training contract; found {sorted(invalid)}')
    if set(sequence) == {'N'}:
        raise ValueError(f'{header}: An all-N sequence cannot provide valid typing information')
    return Record(header, sequence, kind)


def iter_records(path):
    """Yield validated records in input order. Callers own the exact ID uniqueness check."""
    path = Path(path)
    opener = gzip.open if path.suffix.lower() == '.gz' else open
    with opener(path, 'rt', encoding='utf-8-sig', newline=None) as stream:
        first = next((line.strip() for line in stream if line.strip()), '')
        if first.startswith('>'):
            header, parts = first[1:], []
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith('>'):
                    yield _record(header, ''.join(parts), 'fasta')
                    header, parts = line[1:], []
                else:
                    parts.append(line)
            yield _record(header, ''.join(parts), 'fasta')
        elif first.startswith('@'):
            line = first
            while line:
                if not line.startswith('@'):
                    raise ValueError('A FASTQ record must begin with @header')
                header = line[1:]
                parts = []
                for raw in stream:
                    sequence_line = raw.rstrip('\r\n')
                    if sequence_line.startswith('+'):
                        plus = sequence_line[1:]
                        if plus and plus not in (header, header.split()[0] if header.split() else ''):
                            raise ValueError('FASTQ + identifier does not match the header')
                        break
                    if not sequence_line:
                        raise ValueError('Blank line in the FASTQ sequence section')
                    parts.append(sequence_line)
                else:
                    raise ValueError('FASTQ + separator is missing')
                sequence = ''.join(parts)
                if not sequence:
                    raise ValueError('FASTQ sequence is empty')
                quality_length = 0
                while quality_length < len(sequence):
                    quality = stream.readline()
                    if not quality:
                        raise ValueError('FASTQ quality values are truncated')
                    quality = quality.rstrip('\r\n')
                    if not quality or any(not 33 <= ord(c) <= 126 for c in quality):
                        raise ValueError('FASTQ quality characters must be ASCII 33-126')
                    quality_length += len(quality)
                if quality_length != len(sequence):
                    raise ValueError('FASTQ sequence and quality lengths differ')
                yield _record(header, sequence, 'fastq')
                line = stream.readline().rstrip('\r\n')
                if not line and stream.read(1):
                    raise ValueError('Blank line between FASTQ records')
        else:
            raise ValueError('The input is empty or is not FASTA/FASTQ')


def read_records(path):
    records = list(iter_records(path))
    identifiers = [record.record_id for record in records]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(DUPLICATE_ID)
    return records


def ordered_identity(records):
    return [record.identity(index) for index, record in enumerate(records)]
