"""Verify, trace and replay the IncFinder 0.5.0rc2 Inc label provenance."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import mmap
import re
import subprocess
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

MODEL_LABELS = (
    "Inc18",
    "IncC",
    "IncFIA",
    "IncFIB",
    "IncFIC",
    "IncFII",
    "IncHI1",
    "IncHI2",
    "IncI",
    "IncK",
    "IncL",
    "IncN",
    "IncP",
    "IncQ",
    "IncR",
    "IncU",
    "IncX1",
    "IncX3",
    "IncX_other",
    "IncY",
    "Inc_other",
)
HASH = re.compile(r"[a-f0-9]{64}")
SEGMENT = re.compile(r"(fw|bw):(\d+)-(\d+)")
REPLAY = {
    "reconcile": "reconcile_views.py",
    "edges": "edge_fingerprints.py",
    "rules": "derive_rules.py",
    "inputs": "prepare_inputs.py",
    "assemble": "assemble_metasim.py",
    "genbank": "recover_genbank.py",
    "skani": "skani_clusters.py",
    "mob": "mob_representatives.py",
    "metasim": "metasim_batches.py",
    "fragments": "compare_fragments.py",
    "frozen": "build_frozen.py",
}


def records(directory, name):
    with gzip.open(directory / (name + ".tsv.gz"), "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f, delimiter="\t")


def need(ok, message):
    if not ok:
        raise ValueError(message)


def index(rows, key):
    table = {}
    for row in rows:
        k = key(row)
        need(k not in table, f"duplicate key {k}")
        table[k] = row
    return table


def identity(row):
    return row["assembly_accession"], row["sequence_accession"]


def members(group):
    return group.split(";") if group else []


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_downloads(directory, signer):
    checksum = directory / "PROVENANCE-SHA256SUMS"
    signature = directory / "PROVENANCE-SHA256SUMS.sig"
    signer_bytes = signer.read_bytes()
    if (
        hashlib.sha256(signer_bytes.replace(b"\r\n", b"\n")).hexdigest()
        != "25ad764a353930a61b951e12bf431d9a01e7a36a9136acf3331df360e0a47f4b"
    ):
        raise ValueError("release signer differs from the pinned IncFinder key")
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(signer),
            "-I",
            "incfinder-release",
            "-n",
            "incfinder-release",
            "-s",
            str(signature),
        ],
        input=checksum.read_bytes(),
        capture_output=True,
        check=True,
    )
    verified = set()
    for line in checksum.read_text(encoding="ascii").splitlines():
        digest, separator, filename = line.partition("  ")
        need(
            separator
            and HASH.fullmatch(digest)
            and re.fullmatch(r"[A-Za-z0-9_.-]+", filename)
            and filename not in verified,
            "invalid signed manifest entry",
        )
        need(file_hash(directory / filename) == digest, f"download hash {filename}")
        verified.add(filename)
    prefixes = ("frozen", "replay", "controls", "comparison", "tools")
    expected = {"IncFinder-0.5.0rc2-provenance.1-" + p + ".tar.gz" for p in prefixes} | {
        "incfinder_provenance.py"
    }
    need(verified == expected, "unexpected signed release members")
    return dict(status="PASS", verified_files=sorted(verified))


def check_digest_manifest(data):
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    need(
        manifest["schema"] == 1
        and manifest["tag"] == "v0.5.0rc2-provenance.1"
        and manifest["view"] in {"frozen", "replay"},
        "unknown data schema",
    )
    required = {
        "accessions.tsv.gz",
        "clusters.tsv.gz",
        "subtypes.tsv.gz",
        "representatives.tsv.gz",
        "train_records.tsv.gz",
        "holdout_records.tsv.gz",
        "train_source_metadata.tsv.gz",
        "holdout_source_metadata.tsv.gz",
        "labels.tsv.gz",
        "sources.json",
    }
    need(required <= set(manifest["files"]), "missing required tables from manifest")
    for name, expected in manifest["files"].items():
        need(name and name == Path(name).name and "/" not in name and "\\" not in name, "invalid member name")
        member = data / name
        need(
            file_hash(member) == expected["sha256"] and member.stat().st_size == expected["bytes"],
            f"member integrity {name}",
        )
    return manifest


def verify(data, contract=None):
    receipt = check_digest_manifest(data)
    labels = list(MODEL_LABELS)
    if contract:
        need(
            json.loads(contract.read_text(encoding="utf-8"))["labels"] == labels, "rc2 model contract differs"
        )
    sources = index(records(data, "accessions"), identity)
    included = {k for k, r in sources.items() if r["inclusion"] == "included"}
    for key, r in sources.items():
        need(
            re.fullmatch(r"GCA_\d+\.\d+", key[0]) and re.fullmatch(r"[A-Za-z0-9_]+\.\d+", key[1]),
            f"unversioned accession {key}",
        )
        need(HASH.fullmatch(r["sequence_sha256"]) and HASH.fullmatch(r["gbff_sha256"]), f"source hash {key}")
        need(
            int(r["length_bp"]) > 0 and bool(r["assembly_release_date"]) == (key in included),
            f"source eligibility {key}",
        )
        need(
            r["inclusion"] in {"included", "assembly_absent_from_frozen_date_catalog"},
            f"unknown eligibility {key}",
        )
    clusters = index(records(data, "clusters"), identity)
    need(clusters.keys() == included, "cluster membership differs from eligible sources")
    grouped = defaultdict(list)
    for k, r in clusters.items():
        grouped[r["cluster_id"]].append(k)
    rep_ids = set()
    for tree in grouped.values():
        rep = min(
            tree,
            key=lambda k: (
                sources[k]["assembly_release_date"],
                -int(sources[k]["length_bp"]),
                k[0] + "__" + k[1] + "__",
            ),
        )
        for k in tree:
            row = clusters[k]
            need(
                (row["representative_assembly"], row["representative_accession"]) == rep
                and int(row["is_representative"]) == int(k == rep)
                and int(row["cluster_size"]) == len(tree),
                f"cluster {row['cluster_id']}",
            )
        rep_ids.add(rep)
    rules = index(records(data, "subtypes"), lambda r: r["raw_subtype"])
    parents = index(records(data, "representatives"), lambda r: r["record_id"])
    need(
        {(r["assembly_accession"], k) for k, r in parents.items()} == rep_ids, "typed representative universe"
    )
    classes = list(records(data, "labels"))
    need([r["label"] for r in classes] == labels + ["No_Inc"], "rc2 label order")
    counts, type_counts, major_counts = Counter(), Counter(), Counter()
    for k, r in parents.items():
        key = r["assembly_accession"], k
        need(
            clusters[key]["cluster_id"] == r["cluster_id"] and clusters[key]["is_representative"] == "1",
            f"parent cluster {k}",
        )
        need(r["assembly_release_date"] == sources[key]["assembly_release_date"], f"parent date {k}")
        split = "train" if int(r["assembly_release_date"][:4]) <= 2024 else "holdout"
        need(r["split"] == split, f"parent split {k}")
        need(
            r["cv_fold"] in set(map(str, range(1, 11))) if split == "train" else r["cv_fold"] == "",
            f"parent fold {k}",
        )
        sub = {
            t.strip()
            for t in re.split(r"[,;|]", r["raw_rep_types"])
            if re.fullmatch(r"Inc[A-Za-z0-9/-]+", t.strip())
        }
        need(sub == set(members(r["inc_subtypes"])) and sub <= rules.keys(), f"Inc extraction {k}")
        marked = {rules[t]["final_label"] for t in sub} or {"No_Inc"}
        need(
            members(r["final_labels"]) == [lab for lab in labels + ["No_Inc"] if lab in marked],
            f"Inc class mapping {k}",
        )
        counts.update(marked)
        type_counts.update(sub)
        major_counts.update({rules[t]["major_group"] for t in sub})
    for k, r in rules.items():
        need(
            type_counts[k] == int(r["subtype_plasmids"])
            and major_counts[r["major_group"]] == int(r["major_plasmids"]),
            f"rule counts {k}",
        )
    record_ids = set()
    row_counts, class_counts = {}, {}
    observed_groups = {}
    for split in ("train", "holdout"):
        n = whole = min_bp = max_bp = 0
        bucket = Counter()
        groups = set()
        source_rows = records(data, split + "_source_metadata")
        for i, (r, source_row) in enumerate(
            itertools.zip_longest(records(data, split + "_records"), source_rows)
        ):
            need(r is not None and source_row is not None, "unequal record and source metadata lengths")
            need(
                source_row["record_id"] == r["record_id"]
                and source_row["record_type"] == r["record_type"]
                and source_row["split"] == ("train" if split == "train" else "test")
                and source_row["plasmid_id"] == r["parent_accession"]
                and source_row["sequence_length"] == r["length_bp"]
                and source_row["original_header"].split()[0] == r["parent_accession"]
                and all(
                    source_row[k] == r[k] for k in ("source_batch_id", "source_segments", "source_strands")
                ),
                "source metadata join for " + r["record_id"],
            )
            need(
                0
                <= int(source_row["ambiguous_replaced_count"])
                <= int(source_row["n_count"])
                <= int(r["length_bp"]),
                "source ambiguity counts for " + r["record_id"],
            )
            if source_row["source_batch_id"]:
                path = source_row["source_fna"]
                need(
                    re.fullmatch(
                        re.escape(source_row["source_batch_id"]) + r"_seed[0-9]+/train-Exact\.fna", path
                    )
                    and source_row["raw_metasim_header"].startswith(source_row["metasim_read_id"] + " ")
                    and f"gi|{source_row['source_gi']}|ref|{source_row['source_ref']}|"
                    in source_row["raw_metasim_header"],
                    "simulation row identifiers for " + r["record_id"],
                )
            else:
                need(
                    not source_row["source_fna"]
                    and source_row["original_sequence_sha256"] == r["sequence_sha256"],
                    "complete short plasmid source digest for " + r["record_id"],
                )
            k = r["record_id"]
            need(
                k not in record_ids and r["row_index"] == str(i) and r["split"] == split, f"row identity {k}"
            )
            record_ids.add(k)
            p = parents[r["parent_accession"]]
            need(
                (r["assembly_accession"], r["cv_fold"], r["final_labels"], split)
                == (p["assembly_accession"], p["cv_fold"], p["final_labels"], p["split"]),
                f"parent inheritance {k}",
            )
            size = int(r["length_bp"])
            parent = sources[(p["assembly_accession"], p["record_id"])]
            parent_size = int(parent["length_bp"])
            need(size > 0 and HASH.fullmatch(r["sequence_sha256"]), f"fragment identity {k}")
            if r["record_type"] == "full_plasmid_lt1000":
                need(
                    size == parent_size < 1000 and not r["source_segments"] and not r["source_batch_id"],
                    f"whole plasmid {k}",
                )
                whole += 1
            else:
                need(
                    r["record_type"] == "metasim_fragment_1000_10000" and bool(r["source_batch_id"]),
                    f"fragment type {k}",
                )
                parts = [SEGMENT.fullmatch(item) for item in members(r["source_segments"])]
                need(parts and all(parts), f"segment syntax {k}")
                bounds = [(int(p[2]), int(p[3])) for p in parts]
                need(
                    all(0 <= a < b <= parent_size for a, b in bounds)
                    and sum(b - a for a, b in bounds) == size,
                    f"intervals {k}",
                )
                need(
                    {part[1] for part in parts} == set(re.split(r"[;,|]", r["source_strands"])),
                    f"orientation {k}",
                )
            bucket.update(members(r["final_labels"]))
            groups.add(p["record_id"])
            min_bp = min(min_bp, size) if n else size
            max_bp = max(max_bp, size)
            n += 1
        row_counts[split] = {
            "records": n,
            "whole_short": whole,
            "represented_parents": len(groups),
            "length_bp": [min_bp, max_bp],
        }
        class_counts[split] = bucket
        observed_groups[split] = groups
    need(not observed_groups["train"] & observed_groups["holdout"], "a representative spans both cohorts")
    for i, r in enumerate(classes):
        lab = r["label"]
        need(
            r["stage2_index"] == (str(i) if lab != "No_Inc" else "")
            and int(r["representative_plasmids"]) == counts[lab]
            and int(r["training_records"]) == class_counts["train"][lab]
            and int(r["holdout_records"]) == class_counts["holdout"][lab]
            and members(r["raw_subtypes"]) == sorted(k for k, v in rules.items() if v["final_label"] == lab),
            f"label statistics {lab}",
        )
    if receipt["view"] == "frozen":
        need(
            (
                len(sources),
                len(included),
                len(parents),
                len(rules),
                counts["No_Inc"],
                row_counts["train"]["records"],
                row_counts["holdout"]["records"],
            )
            == (97904, 97898, 45911, 39, 35247, 800265, 200095),
            "frozen rc2 dimensions",
        )
    return dict(
        status="PASS",
        view=receipt["view"],
        accessions=len(sources),
        dated=len(included),
        clusters=len(grouped),
        raw_subtypes=len(rules),
        positive_labels=len(labels),
        no_inc=counts["No_Inc"],
        cohorts=row_counts,
    )


def verify_source_sequences(data, inputs):
    """Reconstruct each model record from the two ordered rc2 representative FASTAs."""
    expected = {
        "train": ("train", "0ecb7ddefe4f6b11cbc23eeb9eb66865b5b3e340e8c820b143949a49ddeff953"),
        "holdout": ("test", "05b1a6f5e2cae6345aa2ed110ade83c4d71d444a3d38c9b1cdb46d711c5b1187"),
    }
    normal = bytes(c if c in b"ACGTN" else ord("N") for c in range(256))
    complement = bytes.maketrans(b"ACGT", b"TGCA")
    result = {}
    for split, (name, expected_hash) in expected.items():
        path = inputs / "00_input" / f"{name}.plasmids.ANI80_AF90.fa"
        need(file_hash(path) == expected_hash, f"expected the rc2 ordered representative FASTA for {split}")
        offsets = {}
        with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as genome:
            accession, start, size, pos = None, 0, 0, 0
            for line in source:
                if line.startswith(b">"):
                    if accession is not None:
                        offsets[accession] = (start, size)
                    accession = line[1:].split()[0].decode("ascii")
                    start, size = pos + len(line), 0
                else:
                    size += len(line.rstrip(b"\n"))
                pos += len(line)
            offsets[accession] = (start, size)
            count = multipart = whole = 0
            for row in records(data, split + "_records"):
                start, size = offsets[row["parent_accession"]]
                parts = members(row["source_segments"])
                if not parts:
                    need(row["record_type"] == "full_plasmid_lt1000", "missing sampled intervals")
                    parts = [f"fw:0-{size}"]
                    whole += 1
                segments = []
                for part in parts:
                    match = SEGMENT.fullmatch(part)
                    need(match, f"invalid interval for {row['record_id']}")
                    lo, hi = int(match[2]), int(match[3])
                    need(0 <= lo < hi <= size, f"interval outside {row['parent_accession']}")
                    # The two verified inputs use LF and wrap at 80 nucleotides.
                    left = start + lo + lo // 80
                    right = start + hi + (hi - 1) // 80
                    bases = genome[left:right].replace(b"\n", b"").translate(normal)
                    segments.append(bases if match[1] == "fw" else bases.translate(complement)[::-1])
                reconstructed = b"".join(segments)
                need(
                    len(reconstructed) == int(row["length_bp"])
                    and hashlib.sha256(reconstructed).hexdigest() == row["sequence_sha256"],
                    f"oriented source sequence differs for {row['record_id']}",
                )
                count += 1
                multipart += len(parts) > 1
        result[split] = dict(
            records=count, complete_short=whole, multipart=multipart, parent_fasta_sha256=expected_hash
        )
    return result


def verify_edges(data, rows_path, fingerprints):
    check_digest_manifest(data)
    classes = {"|".join(identity(r)): r["cluster_id"] for r in records(data, "clusters")}
    parent = {key: key for key in classes}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def table(path):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            yield from csv.DictReader(f, delimiter="\t")

    expected = index(table(fingerprints), lambda r: r["query_id"])
    seen = set()
    count = 0
    for query, group in itertools.groupby(table(rows_path), key=lambda r: r["query_id"]):
        need(
            query in classes and query in expected and query not in seen,
            "unknown or repeated edge query " + query,
        )
        ends, metrics = hashlib.sha256(), hashlib.sha256()
        n, previous = 0, None
        for r in group:
            ref = r["reference_id"]
            values = r["ani"], r["af_reference"], r["af_query"]
            ani, af_ref, af_query = map(Decimal, values)
            need(
                ref in classes
                and ref != query
                and all(v.is_finite() for v in (ani, af_ref, af_query))
                and ani >= 80
                and min(af_ref, af_query) >= 90,
                "invalid directed row for " + query,
            )
            row = (ref, *values)
            need(previous is None or row >= previous, "reference rows out of canonical order for " + query)
            previous = row
            ends.update((ref + "\n").encode("ascii"))
            metrics.update(("\t".join(row) + "\n").encode("ascii"))
            a, b = root(query), root(ref)
            if a != b:
                parent[a] = b
            n += 1
        old = expected[query]
        need(
            str(n) == old["qualifying_rows"]
            and ends.hexdigest() == old["reference_ids_sha256"]
            and metrics.hexdigest() == old["reference_ids_ani_af_sha256"],
            "directed-edge digest for " + query,
        )
        seen.add(query)
        count += n
    need(seen == expected.keys(), "coverage of directed edges")
    components, reverse = {}, {}
    for key, label in classes.items():
        group = root(key)
        need(
            components.setdefault(group, label) == label and reverse.setdefault(label, group) == group,
            "graph partition differs at " + key,
        )
    return dict(
        status="PASS",
        records=len(classes),
        components=len(components),
        qualifying_directed_rows=count,
        queries_with_qualifying_rows=len(seen),
    )


def trace_record(data, query):
    fragment = None
    order = (
        ("holdout_records", "train_records")
        if query.startswith("test_")
        else ("train_records", "holdout_records")
    )
    if query.startswith(("train_", "test_")):
        fragment = next(
            (
                r
                for r in itertools.chain.from_iterable(records(data, t) for t in order)
                if r["record_id"] == query
            ),
            None,
        )
        need(fragment is not None, "unknown fragment " + query)
    query_acc = fragment["assembly_accession"] + "/" + fragment["parent_accession"] if fragment else query
    related = [
        r
        for r in records(data, "accessions")
        if query_acc in (r["sequence_accession"], r["assembly_accession"] + "/" + r["sequence_accession"])
    ]
    need(len(related) == 1, "unknown or ambiguous accession; use assembly_accession/sequence_accession")
    src = related[0]
    membership = next((r for r in records(data, "clusters") if identity(r) == identity(src)), None)
    representative = None
    mapping = []
    if membership:
        representative = next(
            r
            for r in records(data, "representatives")
            if r["record_id"] == membership["representative_accession"]
        )
        calls = set(members(representative["inc_subtypes"]))
        mapping = [r for r in records(data, "subtypes") if r["raw_subtype"] in calls]
    intervals = None
    if fragment:
        if fragment["record_type"] == "full_plasmid_lt1000":
            intervals = [dict(start=0, end=int(src["length_bp"]), strand="fw", basis="complete_parent")]
        else:
            intervals = []
            for text in members(fragment["source_segments"]):
                segment = SEGMENT.fullmatch(text)
                need(segment is not None, "invalid source interval " + text)
                intervals.append(
                    dict(
                        start=int(segment[2]),
                        end=int(segment[3]),
                        strand=segment[1],
                        basis="recorded_fragment_interval",
                    )
                )
    original_metadata = None
    if fragment:
        original_metadata = next(
            r
            for r in records(data, fragment["split"] + "_source_metadata")
            if r["record_id"] == fragment["record_id"]
        )
    return dict(
        fragment=fragment,
        source_record_metadata=original_metadata,
        source_intervals_zero_based_half_open=intervals,
        source=src,
        membership=membership,
        representative=representative,
        rules=mapping,
        genbank="https://www.ncbi.nlm.nih.gov/nuccore/" + src["sequence_accession"],
        assembly="https://www.ncbi.nlm.nih.gov/datasets/genome/" + src["assembly_accession"] + "/",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    downloads = commands.add_parser("verify-release")
    downloads.add_argument("directory", type=Path)
    downloads.add_argument("--signer", type=Path, required=True)
    verify_p = commands.add_parser("verify")
    verify_p.add_argument("--contract", type=Path, help="also compare with the unmodified rc2 contract.json")
    verify_p.add_argument(
        "--representative-inputs",
        type=Path,
        help="also reconstruct every record from the two rc2 FASTAs made by replay inputs",
    )
    edges_p = commands.add_parser(
        "verify-edges", help="rebuild the graph using public directed rows and their fingerprints"
    )
    edges_p.add_argument("--data", required=True, type=Path)
    edges_p.add_argument("--rows", required=True, type=Path)
    edges_p.add_argument("--index", required=True, type=Path)
    trace_p = commands.add_parser("trace")
    for command in (verify_p, trace_p):
        command.add_argument(
            "--data", type=Path, default=Path("frozen"), help="extracted data directory (default: frozen)"
        )
    key = trace_p.add_mutually_exclusive_group(required=True)
    key.add_argument("--record")
    key.add_argument("--label")
    replay = commands.add_parser("replay")
    replay.add_argument("stage", choices=REPLAY)
    replay.add_argument("arguments", nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.command == "replay":
        root = Path(__file__).resolve().parent
        tools = root / "tools"
        source = tools if (tools / "incfinder_provenance.py").is_file() else root
        program = source / "replay" / REPLAY[a.stage]
        need(program.is_file(), "download the source bundle to run the replay stages")
        return subprocess.call([sys.executable, "-B", str(program), *a.arguments])
    if a.command == "verify-release":
        result = verify_downloads(a.directory, a.signer)
    elif a.command == "verify-edges":
        result = verify_edges(a.data, a.rows, a.index)
    elif a.command == "verify":
        result = verify(a.data, a.contract)
        if a.representative_inputs:
            result["source_sequences"] = verify_source_sequences(a.data, a.representative_inputs)
    elif a.record:
        result = trace_record(a.data, a.record)
    else:
        record = next((r for r in records(a.data, "labels") if r["label"] == a.label), None)
        need(record is not None, "unknown Inc label")
        result = dict(
            label=record, rules=[r for r in records(a.data, "subtypes") if r["final_label"] == a.label]
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        OSError,
        KeyError,
        ValueError,
        InvalidOperation,
        StopIteration,
        csv.Error,
        subprocess.CalledProcessError,
    ) as error:
        print("provenance: " + str(error), file=sys.stderr)
        sys.exit(1)
