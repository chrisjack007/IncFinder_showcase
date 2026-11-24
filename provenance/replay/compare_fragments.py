"""Compare rebuilt MetaSim FASTA and metadata against every frozen analysis record."""

import argparse
import csv
import gzip
import hashlib
import io
import itertools
import json
from collections import Counter
from pathlib import Path


def read(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream, delimiter="\t")


def fasta(path):
    with path.open("rb") as stream:
        key, parts = None, []
        for line in stream:
            if line.startswith(b">"):
                if key is not None:
                    yield key, b"".join(parts)
                key = line[1:].split()[0].decode()
                parts = []
            else:
                parts.append(line.strip().upper())
        if key is not None:
            yield key, b"".join(parts)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(8 << 20), b""):
            h.update(part)
    return h.hexdigest()


def compare(split, src, frozen, parents, dst):
    prefix = "train.final_800k_plus_lt1000" if split == "train" else "test.final_200k_plus_lt1000"
    data = src / (
        "result/09_build_train_dataset_result/data"
        if split == "train"
        else "result/10_build_test_dataset_result/data"
    )
    seqfile, metafile = data / (prefix + ".fna"), data / (prefix + ".metadata.tsv")
    base = frozen / (split + "_records.tsv.gz")
    index = {r["record_id"]: r for r in read(frozen / "representatives.tsv.gz")}
    if parents:
        index = {r["record_id"]: r for r in read(parents)}
    seen = set()
    counts = Counter()
    total = 0
    lengths = []
    whole = 0
    differences = 0
    out_file = dst / base.name
    evidence = dst / (split + "_record_differences.tsv.gz")
    fields = "record_id split row_index record_type parent_accession assembly_accession cv_fold source_batch_id source_segments source_strands length_bp sequence_sha256 final_labels".split()
    with (
        out_file.with_suffix(".part").open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=5, mtime=0) as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as stream,
        gzip.open(evidence, "wt", encoding="utf-8", newline="") as diff,
    ):
        w = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        w.writeheader()
        changes = csv.writer(diff, delimiter="\t", lineterminator="\n")
        changes.writerow(["row_index", "frozen_id", "replay_id", "field", "frozen", "replay"])
        for i, (meta, seq, old) in enumerate(
            itertools.zip_longest(read(metafile), fasta(seqfile), read(base))
        ):
            if (meta is None) != (seq is None) or (
                seq is not None and (seq[0] != meta["record_id"] or seq[0] in seen)
            ):
                raise ValueError(f"invalid replay FASTA or metadata: {split}/{i}")
            new = {}
            if meta is not None:
                acc = meta["plasmid_id"]
                parent = index[acc]
                if (
                    parent["split"] != split
                    or (seq[0] in seen)
                    or int(meta["sequence_length"]) != len(seq[1])
                ):
                    raise ValueError(f"invalid replay group or length: {split}/{i}")
                seen.add(seq[0])
                length = len(seq[1])
                lengths.append(length)
                whole += meta["record_type"] == "full_plasmid_lt1000"
                new = dict(
                    record_id=seq[0],
                    split=split,
                    row_index=str(i),
                    record_type=meta["record_type"],
                    parent_accession=acc,
                    assembly_accession=parent["assembly_accession"],
                    cv_fold=parent["cv_fold"],
                    source_batch_id=meta["source_batch_id"],
                    source_segments=meta["source_segments"],
                    source_strands=meta["source_strands"],
                    length_bp=str(length),
                    sequence_sha256=hashlib.sha256(seq[1]).hexdigest(),
                    final_labels=parent["final_labels"],
                )
                w.writerow(new)
                total += 1
            old = old or {}
            if old != new:
                differences += 1
                for field in fields:
                    if old.get(field) != new.get(field):
                        counts[field] += 1
                        changes.writerow(
                            [
                                i,
                                old.get("record_id"),
                                new.get("record_id"),
                                field,
                                old.get(field),
                                new.get(field),
                            ]
                        )
        if not lengths:
            raise ValueError("no rebuilt records")
    out_file.with_suffix(".part").replace(out_file)
    return dict(
        records=total,
        full_short_plasmids=whole,
        sampled_fragments=total - whole,
        min_bp=min(lengths),
        max_bp=max(lengths),
        differing_rows=differences,
        differing_fields=dict(counts),
        replay_fasta_sha256=sha(seqfile),
        replay_metadata_sha256=sha(metafile),
        frozen_table_sha256=sha(base),
        replay_table_sha256=sha(out_file),
        difference_table_sha256=sha(evidence),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_root", type=Path)
    p.add_argument("frozen_tables", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--parents", type=Path, help="replayed representatives in the release schema")
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    results = {s: compare(s, a.run_root, a.frozen_tables, a.parents, a.output) for s in ("train", "holdout")}
    path = a.output / "metasim-comparison.json"
    path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
