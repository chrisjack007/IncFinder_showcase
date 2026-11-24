"""Rebuild the dated input, skani database, search batches and connected components."""

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_tsv(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def atomic_json(path, obj):
    part = path.with_suffix(".part.json")
    part.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    part.replace(path)


def run(args):
    if args.profile:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        if args.sketch_options or args.sketch_set:
            raise ValueError("choose the recovered sketch profile or explicit sketch options")
        if (
            profile["k"] != 15
            or profile["use_syncs"]
            or profile["amino_acid"]
            or not profile["dated_query_order_matches_frozen"]
        ):
            raise ValueError("the profile does not match the rc2 DNA search")
        args.sketch_options = [
            "-c",
            str(profile["compression_factor"]),
            "-m",
            str(profile["marker_compression_factor"]),
        ]
        args.sketch_set = profile["sketch_set"]
    args.sketch_set = args.sketch_set or "ge500"
    out = args.output.resolve()
    out.mkdir(exist_ok=True, parents=True)
    fasta_dir = out / "fasta"
    fasta_dir.mkdir(exist_ok=True)
    readback = json.loads((args.shards / "complete.json").read_text())
    archive_digest = readback["archive_sha256"]
    inventory = {(r["assembly_accession"], r["seq_id"]): r for r in read_tsv(args.inventory)}
    dates = {r["Assembly Accession"]: r["Assembly Release Date"] for r in read_tsv(args.catalog)}
    dated = {(r["assembly_accession"], r["seq_id"]): r for r in read_tsv(args.dated)}
    eligible = {key for key in inventory if dates.get(key[0], "").strip()}
    if eligible != dated.keys() or any(
        dates[k[0]] != r["Assembly Release Date"] or inventory[k]["seq_length"] != r["seq_length"]
        for k, r in dated.items()
    ):
        raise ValueError("recovered dates or lengths differ from the frozen membership inputs")
    ids = {k: Path(r["fasta_path"]).name for k, r in dated.items()}
    names = {name: k for k, name in ids.items()}
    if len(names) != len(ids):
        raise ValueError("duplicate historical FASTA basenames")
    seen = set()
    for shard_name, expected in sorted(readback["shard_sha256"].items()):
        shard = args.shards / shard_name
        if sha(shard) != expected:
            raise ValueError(f"transport digest mismatch: {shard_name}")
        with gzip.open(shard, "rb") as source:
            while header := source.readline():
                if not header.startswith(b">"):
                    raise ValueError(f"invalid recovery shard: {shard_name}")
                key = tuple(header[1:].strip().decode("ascii").split("|"))
                seq = source.readline().strip()
                if (
                    key in seen
                    or key not in inventory
                    or hashlib.sha256(seq).hexdigest() != inventory[key]["sequence_sha256"]
                ):
                    raise ValueError(f"invalid GenBank recovery: {key}")
                seen.add(key)
                if key in eligible:
                    path = fasta_dir / ids[key]
                    with path.open("wb") as dest:
                        dest.write(b">" + key[1].encode() + b"\n" + seq + b"\n")
    if seen != inventory.keys():
        raise ValueError("incomplete source recovery")
    order = [v for k, v in ids.items() if int(dated[k]["seq_length"]) >= 500]
    if args.profile and (len(order), len(ids)) != (profile["query_count"], profile["dated_count"]):
        raise ValueError("the query cohort disagrees with the sketch profile")
    paths = [str(fasta_dir / v) for v in order]
    filelist = out / "ge500.list"
    filelist.write_text("\n".join(paths) + "\n")
    version = subprocess.check_output([str(args.program), "--version"], text=True).strip()
    if version != "skani 0.3.1":
        raise ValueError("expected skani 0.3.1, observed " + version)
    context = dict(
        raw_archive_sha256=archive_digest,
        dates=sha(args.dated),
        inventory=sha(args.inventory),
        catalog=sha(args.catalog),
        list_sha256=sha(filelist),
        program=sha(args.program),
        version=version,
        sketch_options=args.sketch_options,
        min_ani=80,
        min_af=90,
        sketch_set=args.sketch_set,
        batch_size=500,
        threads=args.threads,
    )
    db = out / "db"
    sketch_record = out / "sketch.json"
    sketch_list = filelist
    if args.sketch_set == "all-dated":
        sketch_list = out / "all-dated.list"
        sketch_list.write_text("".join(str(fasta_dir / f) + "\n" for f in ids.values()))
    sketch_args = [
        str(args.program),
        "sketch",
        "-l",
        str(sketch_list),
        "-t",
        str(args.threads),
        "-o",
        str(db),
        *args.sketch_options,
    ]
    if sketch_record.exists():
        receipt = json.loads(sketch_record.read_text())
        if (
            receipt["context"] != context
            or {p.relative_to(db).as_posix(): sha(p) for p in db.rglob("*") if p.is_file()} != receipt["db"]
        ):
            raise ValueError("cached database differs")
    else:
        if db.exists():
            raise ValueError("incomplete database exists; choose a fresh output or move the failed database")
        with (out / "sketch.log").open("wb") as log:
            subprocess.run(sketch_args, stdout=log, stderr=log, check=True)
        atomic_json(
            sketch_record,
            dict(
                context=context,
                command=sketch_args,
                db={p.relative_to(db).as_posix(): sha(p) for p in db.rglob("*") if p.is_file()},
            ),
        )
    print("sketch complete", len(paths), "eligible searches", flush=True)
    parent = {name: name for name in ids.values()}

    def root(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def edges(path):
        n = 0
        for r in read_tsv(path):
            ref, query = Path(r["Ref_file"]).name, Path(r["Query_file"]).name
            if ref not in parent or query not in parent:
                raise ValueError(f"unrecognized FASTA: {ref} {query}")
            if (
                ref != query
                and float(r["ANI"]) >= 80
                and min(float(r["Align_fraction_ref"]), float(r["Align_fraction_query"])) >= 90
            ):
                rp, rq = root(ref), root(query)
                if rp != rq:
                    parent[rq] = rp
                n += 1
        return n

    total = 0
    search = out / "search"
    search.mkdir(exist_ok=True)
    sketch_id = sha(sketch_record)
    for i in range(0, len(paths), 500):
        part = search / f"batch-{i // 500 + 1:03d}.tsv"
        receipt = part.with_suffix(".json")
        if receipt.exists():
            evidence = json.loads(receipt.read_text())
            if evidence["sketch"] != sketch_id or evidence["tsv"] != sha(part):
                raise ValueError(f"cached search changed: {part}")
        else:
            temp = part.with_suffix(".partial")
            cmd = [
                str(args.program),
                "search",
                "-d",
                str(db),
                "-t",
                str(args.threads),
                "--both-min-af",
                "90.0",
                "-o",
                str(temp),
                *paths[i : i + 500],
            ]
            with part.with_suffix(".log").open("wb") as log:
                subprocess.run(cmd, stdout=log, stderr=log, check=True)
            temp.replace(part)
        n = edges(part)
        if not receipt.exists():
            atomic_json(
                receipt,
                dict(sketch=sketch_id, tsv=sha(part), accepted_edges=n, queries=len(paths[i : i + 500])),
            )
        elif n != evidence["accepted_edges"]:
            raise ValueError(f"cached edge count changed: {part}")
        total += n
        print("batch", i // 500 + 1, "accepted", n, flush=True)
    clusters = defaultdict(list)
    for name in ids.values():
        clusters[root(name)].append(name)
    observed = {}
    target = out / "clusters.tsv"
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            "cluster_id cluster_size assembly_accession sequence_accession representative_assembly representative_accession is_representative".split()
        )
        for i, members in enumerate(sorted(clusters.values(), key=len, reverse=True), 1):
            rep = min(members, key=lambda s: (dates[names[s][0]], -int(dated[names[s]]["seq_length"]), s))
            cluster = f"source1_ani80p0_af90p0_cluster_{i:06d}"
            signature = hashlib.sha256(("\n".join(sorted(members)) + "\n").encode("utf-8")).hexdigest()
            for name in members:
                writer.writerow((cluster, len(members), *names[name], *names[rep], int(rep == name)))
                observed[name] = rep, signature
    old = defaultdict(list)
    reps = {}
    for r in read_tsv(args.membership):
        key = Path(r["fasta_path"]).name
        old[r["cluster_id"]].append(key)
        reps[key] = Path(r["representative_fasta_path"]).name
    if set(reps) != observed.keys():
        raise ValueError("different record universes for old and new cluster maps")
    changed_members, changed_reps = set(), set()
    orig_rep_set = set(reps.values())
    new_rep_set = {v[0] for v in observed.values()}
    changes = out / "cluster_differences.tsv"
    with changes.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            "assembly_accession sequence_accession frozen_rep replay_rep membership_changed representative_changed".split()
        )
        for members in old.values():
            old_hash = hashlib.sha256(("\n".join(sorted(members)) + "\n").encode("utf-8")).hexdigest()
            for name in members:
                rep, sig = observed[name]
                cm, cr = sig != old_hash, rep != reps[name]
                if cm:
                    changed_members.add(name)
                if cr:
                    changed_reps.add(name)
                if cm or cr:
                    writer.writerow(
                        (*names[name], "|".join(names[reps[name]]), "|".join(names[rep]), int(cm), int(cr))
                    )
    atomic_json(
        out / "complete.json",
        dict(
            context=context,
            raw_records=len(seen),
            dated=len(ids),
            query_count=len(paths),
            excluded_from_query=len(ids) - len(paths),
            clusters=len(clusters),
            accepted_directed_nonself_rows=total,
            frozen_edges=3544354,
            cluster_set_changed_records=len(changed_members),
            representative_changed_records=len(changed_reps),
            frozen_only_representatives=len(orig_rep_set - new_rep_set),
            replay_only_representatives=len(new_rep_set - orig_rep_set),
            clusters_sha256=sha(target),
            changes_sha256=sha(changes),
        ),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("program", "shards", "catalog", "dated", "inventory", "membership", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--threads", type=int, default=32)
    p.add_argument("--profile", type=Path, help="skani-profile.json decoded from the historical database")
    p.add_argument("--sketch-set", choices=["ge500", "all-dated"])
    p.add_argument("--sketch-options", nargs=argparse.REMAINDER, default=[])
    run(p.parse_args())
