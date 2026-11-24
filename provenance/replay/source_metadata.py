"""Preserve all 19 historical record-metadata fields with a portable batch filename."""

import csv
import gzip
import io
import itertools
from collections import Counter
from contextlib import contextmanager

FIELDS = (
    "record_id record_type split plasmid_id sequence_length source_status source_gi source_ref "
    "metasim_read_id source_segments source_strands source_batch_id source_fna raw_metasim_header "
    "n_count ambiguous_replaced_count original_header original_length_bp original_sequence_sha256"
).split()


def rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != FIELDS:
            raise ValueError("unexpected historical metadata columns: " + path.name)
        yield from reader


def portable(row):
    value = row["source_fna"]
    if not value:
        if row["source_batch_id"]:
            raise ValueError("missing MetaSim file for " + row["record_id"])
        return row
    parts = value.replace("\\", "/").split("/")
    prefix = row["source_batch_id"] + "_seed"
    batch = [p for p in parts[:-1] if p.startswith(prefix) and p[len(prefix) :].isdigit()]
    if len(batch) != 1 or parts[-1] != "train-Exact.fna":
        raise ValueError("unrecognized historical source path for " + row["record_id"])
    return {**row, "source_fna": batch[0] + "/" + parts[-1]}


@contextmanager
def writer(path, fields):
    pending = path.with_name(path.name + ".partial")
    with (
        pending.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=5) as gz,
    ):
        with io.TextIOWrapper(gz, encoding="utf-8", newline="") as stream:
            out = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
            out.writeheader()
            yield out
    pending.replace(path)


def export(source, output):
    n = relocated = 0
    with writer(output, FIELDS) as w:
        for row in rows(source):
            record = portable(row)
            relocated += row["source_fna"] != record["source_fna"]
            w.writerow(record)
            n += 1
    return dict(records=n, portable_source_filenames=relocated)


def compare(frozen_file, new_file, output, changes):
    n = differing = 0
    by_field = Counter()
    fields = ["row_index", "frozen_id", "replay_id", "field", "frozen", "replay"]
    with writer(output, FIELDS) as w, writer(changes, fields) as diff:
        for i, (old, new) in enumerate(itertools.zip_longest(rows(frozen_file), rows(new_file))):
            old = old or {}
            new = portable(new) if new else {}
            if new:
                n += 1
                w.writerow(new)
            if old != new:
                differing += 1
                for k in FIELDS:
                    if old.get(k) != new.get(k):
                        by_field[k] += 1
                        diff.writerow(
                            dict(
                                row_index=i,
                                frozen_id=old.get("record_id"),
                                replay_id=new.get("record_id"),
                                field=k,
                                frozen=old.get(k),
                                replay=new.get(k),
                            )
                        )
    return dict(
        records=n, fields_checked=len(FIELDS), differing_rows=differing, differing_fields=dict(by_field)
    )
