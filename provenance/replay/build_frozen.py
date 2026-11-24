"""Export the immutable rc2 Inc lineage tables and reconcile all fragment labels and folds."""

from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import hashlib
import io
import itertools
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath

import numpy as np
from openpyxl import load_workbook

from source_metadata import export as export_source_metadata


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(8 << 20), b""):
            h.update(part)
    return h.hexdigest()


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream, delimiter="\t")


def bookrows(book, sheet):
    table = book[sheet].values
    keys = next(table)
    yield from (dict(zip(keys, row)) for row in table)


@contextlib.contextmanager
def output(path, fields):
    part = path.with_suffix(".partial")
    with (
        part.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=5) as gz,
    ):
        with io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
            writer = csv.writer(text, delimiter="\t", lineterminator="\n")
            writer.writerow(fields.split())
            yield writer
    part.replace(path)


def fasta(path):
    with path.open("rb") as stream:
        key = None
        pieces = []
        for line in stream:
            if line.startswith(b">"):
                if key is not None:
                    yield key, b"".join(pieces)
                key, pieces = line[1:].split()[0].decode("ascii"), []
            else:
                pieces.append(line.strip().upper())
        if key is not None:
            yield key, b"".join(pieces)


def unique(records, key):
    result = {}
    for row in records:
        name = key(row)
        if name in result:
            raise ValueError(f"duplicate key {name}")
        result[name] = row
    return result


def export(config, dst):
    files = {key: Path(value) for key, value in config["paths"].items()}
    expected = config["hashes"]
    for key, checksum in expected.items():
        if digest(files[key]) != checksum:
            raise ValueError(f"frozen input digest: {key}")
    dst.mkdir(parents=True, exist_ok=True)
    labels = json.loads(files["contract"].read_text(encoding="utf-8-sig"))["labels"]
    matrix_names = [r["label_name"] for r in rows(files["label_source"])]
    if matrix_names != labels + ["No_Inc"]:
        raise ValueError("model and matrix column orders differ")
    ledger_key = lambda r: (r["assembly_accession"], r["seq_id"])
    inventory = unique(rows(files["raw_inventory"]), ledger_key)
    originals = unique(rows(files["original_source1"]), ledger_key)
    dated = unique(rows(files["dated_source1"]), ledger_key)
    catalog = {r["Assembly Accession"]: r["Assembly Release Date"] for r in rows(files["ncbi_catalog"])}
    if set(inventory) != set(originals) or not dated.keys() <= inventory.keys():
        raise ValueError("source inventory coverage")
    with output(
        dst / "accessions.tsv.gz",
        "assembly_accession sequence_accession length_bp sequence_sha256 gbff_sha256 assembly_release_date inclusion",
    ) as out:
        for key, row in sorted(inventory.items()):
            date = dated[key]["Assembly Release Date"] if key in dated else ""
            if (date and catalog.get(key[0]) != date) or (not date and key[0] in catalog):
                raise ValueError(f"source date not explained: {key}")
            if originals[key]["seq_length"] != row["seq_length"]:
                raise ValueError(f"inventory length: {key}")
            out.writerow(
                (
                    *key,
                    row["seq_length"],
                    row["sequence_sha256"],
                    row["member_sha256"],
                    date,
                    "included" if key in dated else "assembly_absent_from_frozen_date_catalog",
                )
            )
    members = unique(rows(files["membership"]), ledger_key)
    if set(members) != set(dated):
        raise ValueError("cluster inputs differ")
    path_ids = {r["fasta_path"]: key for key, r in dated.items()}
    representatives = {}
    with output(
        dst / "clusters.tsv.gz",
        "cluster_id cluster_size assembly_accession sequence_accession representative_assembly representative_accession is_representative",
    ) as out:
        for key, r in sorted(members.items()):
            rep = path_ids[r["representative_fasta_path"]]
            if (rep == key) != (r["is_representative"] == "1"):
                raise ValueError(f"representative flag: {key}")
            if rep == key:
                if key[1] in representatives:
                    raise ValueError(f"ambiguous representative accession: {key[1]}")
                representatives[key[1]] = (key[0], r["cluster_id"], PurePosixPath(r["fasta_path"]).stem)
            out.writerow((r["cluster_id"], r["cluster_size"], *key, *rep, r["is_representative"]))
    wb = load_workbook(files["policy_workbook"], read_only=True, data_only=True)
    try:
        mapping = unique(bookrows(wb, "category_policy"), lambda r: r["inc_subtype"])
    finally:
        wb.close()
    locked = {r["inc_subtype"]: r["final_label"] for r in rows(files["category_policy"])}
    if len(locked) != 39 or locked != {k: v["final_label"] for k, v in mapping.items()}:
        raise ValueError("frozen rule exports disagree")
    with output(
        dst / "subtypes.tsv.gz", "raw_subtype major_group major_plasmids subtype_plasmids final_label rule"
    ) as out:
        for subtype, r in sorted(mapping.items()):
            out.writerow(
                (
                    subtype,
                    r["inc_major"],
                    r["major_plasmid_count"],
                    r["subtype_plasmid_count"],
                    r["final_label"],
                    r["rule"],
                )
            )
    wb = load_workbook(files["reference_labels"], read_only=True, data_only=True)
    try:
        plan = unique(bookrows(wb, "plasmid_train_test"), lambda r: r["plasmid_id"])
        cv = {r["plasmid_id"]: int(r["CV"].removeprefix("CV")) for r in bookrows(wb, "train_cv_plasmids")}
    finally:
        wb.close()
    typing = unique(rows(files["typing_report"]), lambda r: r["record_id"])
    historical = unique(rows(files["historical_mob"]), lambda r: r["sample_name_from_path"])
    if set(typing) != set(plan) or set(plan) != set(representatives) or len(historical) != len(plan):
        raise ValueError("representative sources do not agree")
    ref_counts, sub_counts, group_counts = Counter(), Counter(), Counter()
    groups = {}
    with output(
        dst / "representatives.tsv.gz",
        "record_id assembly_accession cluster_id assembly_release_date split cv_fold raw_rep_types inc_subtypes final_labels",
    ) as out:
        for acc, (asm, cluster, original_id) in sorted(representatives.items()):
            r = plan[acc]
            raw = historical[original_id]["rep_type(s)"]
            parsed = {
                t.strip() for t in re.split(r"[,;|]", raw) if re.fullmatch(r"Inc[A-Za-z0-9/-]+", t.strip())
            }
            calls = {locked[t] for t in parsed} or {"No_Inc"}
            if (
                (parsed or {"No_Inc"}) != set(typing[acc]["inc_type"].split(";"))
                or calls != set(r["final_labels"].split(";"))
                or calls != set(typing[acc]["final_labels"].split(";"))
            ):
                raise ValueError(f"historical field, rc2 and workbook disagree: {acc}")
            date = dated[(asm, acc)]["Assembly Release Date"]
            split = "train" if int(date[:4]) <= 2024 else "holdout"
            fold = cv.get(acc)
            if (int(r["Train"]), int(r["Test"]), fold is not None) != (
                int(split == "train"),
                int(split == "holdout"),
                split == "train",
            ):
                raise ValueError(f"split/fold mismatch: {acc}")
            if fold is not None and fold not in range(1, 11):
                raise ValueError(f"fold out of range: {acc}")
            values = ";".join(k for k in matrix_names if k in calls)
            groups[acc] = asm, split, fold, values, bytes(int(k in calls) for k in matrix_names)
            ref_counts.update(calls)
            sub_counts.update(parsed)
            group_counts.update({mapping[k]["inc_major"] for k in parsed})
            out.writerow((acc, asm, cluster, date, split, fold, raw, ";".join(sorted(parsed)), values))
    for subtype, r in mapping.items():
        if (sub_counts[subtype], group_counts[r["inc_major"]]) != (
            r["subtype_plasmid_count"],
            r["major_plasmid_count"],
        ):
            raise ValueError(f"rule counts: {subtype}")
    totals = {}
    fragments_by_label = {}
    for split in ("train", "holdout"):
        count = Counter()
        matrix = np.load(files[split + "_label_source"], allow_pickle=False, mmap_mode="r")
        folds = np.load(files["fold_source"], allow_pickle=False, mmap_mode="r") if split == "train" else None
        seen = set()
        n = 0
        metadata = rows(files[split + "_metadata_source"])
        seqs = fasta(files[split + "_sequence_source"])
        with output(
            dst / (split + "_records.tsv.gz"),
            "record_id split row_index record_type parent_accession assembly_accession cv_fold source_batch_id source_segments source_strands length_bp sequence_sha256 final_labels",
        ) as out:
            for i, pair in enumerate(itertools.zip_longest(metadata, seqs)):
                meta, seq = pair
                if meta is None or seq is None or seq[0] != meta["record_id"] or seq[0] in seen:
                    raise ValueError(f"FASTA/metadata order or cardinality: {split} {i}")
                seen.add(seq[0])
                acc = meta["plasmid_id"]
                asm, subset, fold, values, bits = groups[acc]
                if (
                    subset != split
                    or bytes(matrix[i]) != bits
                    or (folds is not None and fold != int(folds[i]))
                ):
                    raise ValueError(f"parent or label/fold drift: {seq[0]}")
                length = len(seq[1])
                if int(meta["sequence_length"]) != length:
                    raise ValueError(f"length: {seq[0]}")
                out.writerow(
                    (
                        seq[0],
                        split,
                        i,
                        meta["record_type"],
                        acc,
                        asm,
                        fold,
                        meta["source_batch_id"],
                        meta["source_segments"],
                        meta["source_strands"],
                        length,
                        hashlib.sha256(seq[1]).hexdigest(),
                        values,
                    )
                )
                n += 1
                count.update(values.split(";"))
        if n != len(matrix) or (folds is not None and n != len(folds)):
            raise ValueError(f"matrix cardinality: {split}")
        totals[split] = n
        fragments_by_label[split] = count
        print(split, n, flush=True)
    with output(
        dst / "labels.tsv.gz",
        "label stage2_index representative_plasmids training_records holdout_records raw_subtypes",
    ) as out:
        for i, label in enumerate(matrix_names):
            out.writerow(
                (
                    label,
                    i if label != "No_Inc" else "",
                    ref_counts[label],
                    fragments_by_label["train"][label],
                    fragments_by_label["holdout"][label],
                    ";".join(sorted(k for k, v in locked.items() if v == label)),
                )
            )
    summary = dict(
        source_accessions=len(inventory),
        dated_accessions=len(dated),
        representatives=len(groups),
        train_plasmids=len(cv),
        holdout_plasmids=len(groups) - len(cv),
        records=totals,
        multi_label_plasmids=sum(";" in v[3] for v in groups.values()),
        input_sha256=expected,
    )
    (dst / "sources.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for split in ("train", "holdout"):
        export_source_metadata(files[split + "_metadata_source"], dst / (split + "_source_metadata.tsv.gz"))
    names = [
        "accessions.tsv.gz",
        "clusters.tsv.gz",
        "representatives.tsv.gz",
        "subtypes.tsv.gz",
        "labels.tsv.gz",
        "train_records.tsv.gz",
        "holdout_records.tsv.gz",
        "train_source_metadata.tsv.gz",
        "holdout_source_metadata.tsv.gz",
        "sources.json",
    ]
    manifest = dict(
        schema=1,
        tag="v0.5.0rc2-provenance.1",
        view="frozen",
        files={n: dict(bytes=(dst / n).stat().st_size, sha256=digest(dst / n)) for n in names},
    )
    (dst / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf8")
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "input_sha256"}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "bindings", type=Path, help="JSON with paths and pinned SHA-256 for the frozen source files"
    )
    p.add_argument("output", type=Path)
    args = p.parse_args()
    export(json.loads(args.bindings.read_text(encoding="utf-8-sig")), args.output)
