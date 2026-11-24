"""Rebuild the two representative inputs and the 28 ordered MetaSim inputs."""

import argparse
import csv
import gzip
import hashlib
import json
import shutil
from contextlib import ExitStack
from pathlib import Path

TO_N = bytes(c if c in b"ACGTN" else 78 for c in range(256))


def rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(4 << 20), b""):
            h.update(data)
    return h.hexdigest()


def put(handle, header, seq):
    handle.write(b">" + header.encode("utf-8") + b"\n")
    handle.write(b"\n".join(seq[i : i + 80] for i in range(0, len(seq), 80)) + b"\n")


def build_index(paths, stack):
    index = {}
    for path in paths:
        f = stack.enter_context(path.open("rb"))
        pos = 0
        begin = 0
        previous = None
        for line in f:
            if line.startswith(b">"):
                if previous is not None:
                    index[previous] = (f, begin, pos - begin)
                previous = line[1:].split()[0].decode("ascii")
                if previous in index:
                    raise ValueError(f"duplicate FASTA ID {previous}")
                begin = pos + len(line)
            pos += len(line)
        if previous is not None:
            index[previous] = (f, begin, pos - begin)
    return index


def run(a):
    controls = a.controls.resolve()
    out = a.output.resolve()
    if controls == out or out in controls.parents or controls in out.parents:
        raise ValueError("choose separate controls and output directories")
    out.mkdir(parents=True, exist_ok=True)
    lists = json.loads((controls / "expected-inputs.json").read_text(encoding="utf-8"))
    shutil.copytree(controls / "result", out / "result", dirs_exist_ok=True)
    (out / "00_input").mkdir(exist_ok=True)
    names = {
        (r["assembly_accession"], r["seq_id"]): Path(r["fasta_path"]).name
        for r in rows(controls / "dated.tsv.gz")
    }
    if a.fasta_dir and (a.fasta_dir.parent / "clusters.tsv").is_file():
        selected = {
            (r["representative_assembly"], r["representative_accession"])
            for r in rows(a.fasta_dir.parent / "clusters.tsv")
            if r["is_representative"] == "1"
        }
        required = {
            (r["assembly_accession"], r["record_id"])
            for split in ("train", "test")
            for r in rows(controls / f"{split}-representative-order.tsv.gz")
        }
        if selected != required:
            raise ValueError(
                "replayed representatives differ from the frozen batch plan; preserve both cohorts"
            )
    with ExitStack() as files:
        index = build_index(a.representative_fasta, files) if a.representative_fasta else {}
        for split in ("train", "test"):
            original = list(rows(controls / f"{split}-representative-order.tsv.gz"))
            by_accession = {r["record_id"]: r for r in original}

            def get(key):
                row = by_accession[key]
                if a.fasta_dir:
                    loc = a.fasta_dir / names[(row["assembly_accession"], key)]
                    with loc.open("rb") as handle:
                        if handle.readline()[1:].split()[0].decode() != key:
                            raise ValueError(f"FASTA header: {key}")
                        seq = b"".join(handle.read().split()).upper()
                else:
                    handle, start, length = index[key]
                    handle.seek(start)
                    seq = b"".join(handle.read(length).split()).upper()
                if hashlib.sha256(seq).hexdigest() != row["sequence_sha256"]:
                    raise ValueError(f"representative SHA-256: {key}")
                return seq

            group = f"00_input/{split}.plasmids.ANI80_AF90.fa"
            path = out / group
            if not path.exists() or sha(path) != lists[group]:
                temp = path.with_suffix(".part")
                with temp.open("wb") as dest:
                    for i, row in enumerate(original):
                        if int(row["input_order"]) != i:
                            raise ValueError("representative order changed")
                        put(dest, row["original_header"], get(row["record_id"]))
                if sha(temp) != lists[group]:
                    raise ValueError(f"representative file identity: {split}")
                temp.replace(path)
            batches = (
                controls / "result/05_dedup_chunks_result" / split / f"{split}_metasim_batch_manifest.tsv"
            )
            for batch in rows(batches):
                path = out / batch["bin_fasta"]
                if path.exists() and sha(path) == lists[batch["bin_fasta"]]:
                    continue
                temp = path.with_suffix(".part")
                with temp.open("wb") as dest:
                    for i, row in enumerate(rows(controls / batch["bin_manifest"])):
                        if int(row["input_order"]) != i:
                            raise ValueError("batch input order changed")
                        seq = get(row["original_plasmid_id"])
                        if row["normalization"] == "acgtn":
                            seq = seq.translate(TO_N)
                        elif row["normalization"] != "original":
                            raise ValueError("unknown normalization")
                        put(dest, row["metasim_header"], seq)
                if sha(temp) != lists[batch["bin_fasta"]]:
                    raise ValueError(f"batch FASTA identity: {batch['batch_id']}")
                temp.replace(path)
            print("verified", split, "representatives and ordered MetaSim inputs", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("controls", type=Path)
    p.add_argument("output", type=Path)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fasta-dir", type=Path, help="fasta directory produced by the skani stage")
    src.add_argument(
        "--representative-fasta",
        type=Path,
        nargs="+",
        help="independently preserved uncompressed representatives",
    )
    run(p.parse_args())
