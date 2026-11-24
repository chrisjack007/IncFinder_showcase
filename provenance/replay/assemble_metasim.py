"""Assemble rc2 sequences and full source metadata from fresh MetaSim outputs and short parents."""

import argparse
import csv
import hashlib
import re
from pathlib import Path

from prepare_inputs import TO_N, put, rows, sha
from source_metadata import FIELDS


def fasta(path):
    with path.open("rb") as handle:
        header, pieces = None, []
        for line in handle:
            if line.startswith(b">"):
                if header is not None:
                    yield header, b"".join(pieces)
                header, pieces = line[1:].strip().decode("utf-8"), []
            else:
                pieces.append(line.strip().upper())
        if header is not None:
            yield header, b"".join(pieces)


def write_record(seqout, writer, raw, **meta):
    normalized = raw.translate(TO_N)
    n = normalized.count(b"N")
    meta.update(sequence_length=len(normalized), n_count=n, ambiguous_replaced_count=n - raw.count(b"N"))
    if meta["record_type"] == "full_plasmid_lt1000":
        meta["original_sequence_sha256"] = hashlib.sha256(normalized).hexdigest()
    put(seqout, meta["record_id"], normalized)
    writer.writerow(meta)


def assemble(inputs, runs, out, split):
    stage = "06_run_train_metasim_result" if split == "train" else "07_run_test_metasim_result"
    prefix = "train.final_800k_plus_lt1000" if split == "train" else "test.final_200k_plus_lt1000"
    final = "09_build_train_dataset_result" if split == "train" else "10_build_test_dataset_result"
    dst = out / "result" / final / "data"
    dst.mkdir(parents=True, exist_ok=True)
    batches = list(
        rows(inputs / "result/05_dedup_chunks_result" / split / f"{split}_metasim_batch_manifest.tsv")
    )
    actual = list(rows(runs / "result" / stage / f"{split}_metasim_run_results.tsv"))
    if [r["batch_id"] for r in batches] != [r["batch_id"] for r in actual]:
        raise ValueError("missing or reordered batch results")
    seqfile, metafile = dst / (prefix + ".fna"), dst / (prefix + ".metadata.tsv")
    seq_part, meta_part = seqfile.with_suffix(".fna.partial"), metafile.with_suffix(".metadata.partial")
    count = short_count = 0
    with seq_part.open("wb") as seqout, meta_part.open("w", encoding="utf-8", newline="") as metaout:
        w = csv.DictWriter(metaout, FIELDS, delimiter="\t", lineterminator="\n", restval="")
        w.writeheader()
        for batch, result in zip(batches, actual):
            src = runs / result["fna_files"]
            if sha(src) != result["fna_sha256"]:
                raise ValueError(f"raw batch SHA-256: {src}")
            aliases = {r["metasim_header"]: r for r in rows(inputs / batch["bin_manifest"])}
            size = 0
            for header, seq in fasta(src):
                key = re.search(r'SOURCE_1="(gi\|[^"]+)"', header)
                if not key or key[1] not in aliases:
                    raise ValueError("unresolved source in " + batch["batch_id"])
                parent = aliases[key[1]]
                pieces = re.findall(r",(fw|bw),(\d+)-(\d+)", header)
                ranges = [(int(start), int(stop)) for strand, start, stop in pieces]
                if (
                    not ranges
                    or sum(stop - start for start, stop in ranges) != len(seq)
                    or not all(0 <= start < stop <= int(parent["length_bp"]) for start, stop in ranges)
                ):
                    raise ValueError(f"invalid segment in {header}")
                count += 1
                size += 1
                write_record(
                    seqout,
                    w,
                    seq,
                    record_id=f"{split}_frag_{count:09d}",
                    record_type="metasim_fragment_1000_10000",
                    split=split,
                    plasmid_id=parent["original_plasmid_id"],
                    source_status="metasim_sampled_ge1000_plasmid",
                    source_gi=parent["metasim_gi"],
                    source_ref=parent["metasim_ref"],
                    metasim_read_id=header.split()[0],
                    source_segments=";".join(f"{strand}:{start}-{stop}" for strand, start, stop in pieces),
                    source_strands=";".join(sorted({strand for strand, _, _ in pieces})),
                    source_batch_id=batch["batch_id"],
                    source_fna=f"{batch['batch_id']}_seed{batch['seed']}/{src.name}",
                    raw_metasim_header=header,
                    original_header=parent["original_header"],
                    original_length_bp=parent["length_bp"],
                )
            if size != int(batch["reads_requested"]):
                raise ValueError(f"read count in {src}")
        for header, seq in fasta(inputs / f"00_input/{split}.plasmids.ANI80_AF90.fa"):
            if len(seq) < 1000:
                short_count += 1
                write_record(
                    seqout,
                    w,
                    seq,
                    record_id=f"{split}_short_plasmid_{short_count:06d}",
                    record_type="full_plasmid_lt1000",
                    split=split,
                    plasmid_id=header.split()[0],
                    source_status="not_sampled_lt1000_plasmid_included_full_length",
                    original_header=header,
                    original_length_bp=len(seq),
                )
    seq_part.replace(seqfile)
    meta_part.replace(metafile)
    return dict(sampled=count, whole=short_count, fasta_sha256=sha(seqfile), metadata_sha256=sha(metafile))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "runs", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    for split in ("train", "test"):
        print(
            split,
            assemble(args.inputs.resolve(), args.runs.resolve(), args.output.resolve(), split),
            flush=True,
        )
