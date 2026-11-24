"""Run stock MOB-typer on independent representative records and retain Inc provenance."""

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def inventory_hash(rows):
    canonical = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def validate_reference(directory, manifest, upstream, output):
    evidence = json.loads(manifest.read_text(encoding="utf-8"))
    frozen = sorted(evidence["members"], key=lambda row: row["path"])
    snapshot_hash = inventory_hash(frozen)
    if (
        snapshot_hash != evidence["sha256"]
        or snapshot_hash != "ad091171da64c538a660531f2011f871835fa981a36ddf63c031b49642642000"
    ):
        raise ValueError("reference manifest differs from the rc2 reference")
    actual = []
    for part in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if part.is_file():
            if directory not in part.resolve().parents:
                raise ValueError("reference member outside database: " + part.name)
            actual.append(
                dict(path=part.relative_to(directory).as_posix(), size=part.stat().st_size, sha256=sha(part))
            )
    actual_hash = inventory_hash(actual)
    if not upstream:
        if actual != frozen:
            raise ValueError("initialized database differs from the exact 44-file rc2 snapshot")
        return actual_hash
    publication = json.loads(upstream.read_text(encoding="utf-8"))
    primary = sorted(publication["upstream_raw_members"], key=lambda row: row["path"])
    if (
        publication["upstream_sha256"] != "531f2124269b13e6fe59d0b01936276b5a47c0c0eebbc787f710cd376f77d3cd"
        or inventory_hash(primary) != "ad3ccc9c1567af471f5075f8c8b08372730876b566289377d9ac57ed430326c9"
    ):
        raise ValueError("reference manifest does not identify the eight published DOI inputs")
    installed = {r["path"]: r for r in actual}
    original = {r["path"]: r for r in frozen}
    for r in primary:
        name = r["path"]
        want = dict(path=name, size=r["bytes"], sha256=r["sha256"])
        if installed.get(name) != want or original.get(name) != want:
            raise ValueError("published primary database input differs: " + name)
    indices = (
        "ncbi_plasmid_full_seqs.fas.msh",
        "ncbi_plasmid_full_seqs.fas.nsq",
        "repetitive.dna.fas.nsq",
        "taxa.sqlite",
    )
    if any(not installed.get(name, {}).get("size") for name in indices) or "status.txt" not in installed:
        raise ValueError("finish MOB-suite 3.1.9 database initialization before the replay")
    receipt = dict(
        profile="initialized_from_public_inputs",
        public_archive_sha256=publication["upstream_sha256"],
        checked_primary_files=len(primary),
        snapshot_manifest_sha256=snapshot_hash,
        installed_manifest_sha256=actual_hash,
        members=actual,
        different_or_missing_from_rc2=sorted(k for k in original if original[k] != installed.get(k)),
        additional_installed_files=sorted(installed.keys() - original.keys()),
    )
    temp = output / "database-readback.json.part"
    temp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(output / "database-readback.json")
    return actual_hash


def records(path):
    with path.open("rb") as handle:
        key, parts = None, []
        for line in handle:
            if line.startswith(b">"):
                if key is not None:
                    yield key, b"".join(parts)
                key = line[1:].split()[0].decode("ascii")
                parts = []
            else:
                parts.append(line.strip())
        if key is not None:
            yield key, b"".join(parts)


def write_table(path, rows, fields):
    tmp = path.with_suffix(".part")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        w = csv.DictWriter(handle, fields, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def output_row(acc, raw, policy):
    types = sorted({t.strip() for t in raw.split(",") if re.fullmatch(r"Inc[A-Za-z0-9/-]+", t.strip())})
    mapped = sorted({policy.get(t, "UNMAPPED:" + t) for t in types})
    return dict(
        record_id=acc,
        raw_rep_types=raw,
        inc_type=";".join(types) or "No_Inc",
        final_labels=";".join(mapped) or "No_Inc",
    )


def run_mob(items, prefix, program, database, threads, single=False):
    prefix.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="work-", dir=prefix))
    inp = attempt / "source.fa"
    with inp.open("wb") as stream:
        for acc, seq in items:
            stream.write(b">" + acc.encode() + b"\n" + seq + b"\n")
    output = attempt / "report.tsv"
    argv = [str(program), "-i", str(inp), "-o", str(output), "-n", str(threads), "-d", str(database)]
    argv += ["-s", items[0][0]] if single else ["--multi"]
    env = dict(os.environ)
    env["PATH"] = str(program.parent) + os.pathsep + env.get("PATH", "")
    with (attempt / "log").open("wb") as log:
        subprocess.run(argv, stdout=log, stderr=log, env=env, check=True)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    result = {row["sample_id"].split()[0]: row["rep_type(s)"] for row in rows}
    if len(rows) != len(result) or set(result) - {k for k, seq in items}:
        raise ValueError(f"unexpected sample IDs in {output}")
    # Keep the replicon field and its source-report digest for the requested audit.
    fields = ["record_id", "raw_rep_types"]
    write_table(
        attempt / "replicons.tsv", [dict(record_id=k, raw_rep_types=v) for k, v in result.items()], fields
    )
    (attempt / "report.sha256").write_text(sha(output) + "\n", encoding="ascii")
    output.unlink()
    return result


def stage(a):
    opener = gzip.open if a.policy.suffix == ".gz" else open
    with opener(a.policy, "rt", encoding="utf-8-sig", newline="") as handle:
        policy = {r["inc_subtype"]: r["final_label"] for r in csv.DictReader(handle, delimiter="\t")}
    if len(policy) != 39:
        raise ValueError("incomplete frozen subtype mapping")
    opener = gzip.open if a.reference.suffix == ".gz" else open
    with opener(a.reference, "rt", encoding="utf-8-sig", newline="") as handle:
        reference = {r["record_id"]: r for r in csv.DictReader(handle, delimiter="\t")}
    target = a.output.resolve()
    target.mkdir(exist_ok=True, parents=True)
    program = a.program.resolve()
    database = a.database.resolve()
    version = subprocess.check_output(
        [str(program), "--version"], stderr=subprocess.STDOUT, text=True
    ).strip()
    if version != "mob_typer 3.1.9":
        raise ValueError("expected MOB-suite 3.1.9, observed " + version)
    db_identity = validate_reference(database, a.database_manifest, a.upstream_reference, target)
    if sha(database / "rep.dna.fas") != "fdc10d866d8fdc3c75db0f945c8b52797392abb7b7bf389c5de36a2429500eb0":
        raise ValueError("replicon reference digest differs")
    selected = {}
    for k, r in reference.items():
        lab = r["final_labels"]
        kind = (
            "negative"
            if lab == "No_Inc"
            else "multiple"
            if re.search(r"[;,|]", lab)
            else "merged"
            if lab in {"IncHI1", "IncX_other"}
            else "positive"
        )
        selected.setdefault(kind, k)
    sample_ids = set(selected.values())
    sample = []
    for path in a.fasta:
        for acc, seq in records(path):
            if acc in sample_ids:
                sample.append((acc, seq))
            if len(sample) == len(sample_ids):
                break
        if len(sample) == len(sample_ids):
            break
    if len(sample) != len(sample_ids) or len(sample) < 3:
        raise ValueError("single-versus-batch control missing")
    group = run_mob(sample, target / "control/group", program, database, a.threads)
    separate = {}
    for item in sample:
        separate.update(
            run_mob([item], target / "control" / item[0], program, database, a.threads, single=True)
        )
    same = group == separate and len(separate) == len(sample)
    control = dict(group=group, single=separate, use_multi=same)
    (target / "control.json").write_text(json.dumps(control, indent=2) + "\n")
    print("controls", control, flush=True)
    fields = ["record_id", "raw_rep_types", "inc_type", "final_labels"]
    context = dict(
        program_sha256=sha(program),
        policy_sha256=sha(a.policy),
        database_manifest_sha256=db_identity,
        control_sha256=sha(target / "control.json"),
        program_version=version,
    )

    def batch(index, items):
        location = target / f"batch-{index:04d}"
        location.mkdir(exist_ok=True)
        report = location / "inc.tsv"
        receipt = location / "complete.json"
        identity = hashlib.sha256()
        for key, seq in items:
            identity.update(key.encode() + b"\0" + hashlib.sha256(seq).digest())
        input_hash = identity.hexdigest()
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved["context"] != context or saved["inputs"] != input_hash or saved["output"] != sha(report):
                raise ValueError(f"changed resume inputs: batch {index}")
            return report
        results = run_mob(items, location, program, database, a.threads) if same else {}
        for item in items:
            if item[0] not in results:
                results.update(run_mob([item], location / item[0], program, database, a.threads, single=True))
        if results.keys() != {key for key, seq in items}:
            raise ValueError(f"missing representatives in batch {index}")
        write_table(report, [output_row(key, results[key], policy) for key, seq in items], fields)
        tmp = receipt.with_suffix(".part")
        tmp.write_text(
            json.dumps(
                dict(context=context, inputs=input_hash, output=sha(report), count=len(items)), indent=2
            )
            + "\n"
        )
        tmp.replace(receipt)
        print("complete batch", index, len(items), flush=True)
        return report

    pending = deque()
    reports = []
    seen = set()
    items = []
    index = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for path in a.fasta:
            for acc, seq in records(path):
                if acc in seen:
                    raise ValueError(f"duplicate {acc}")
                seen.add(acc)
                items.append((acc, seq))
                if len(items) == a.batch_size:
                    index += 1
                    pending.append(pool.submit(batch, index, items))
                    items = []
                    if len(pending) >= a.workers + 1:
                        reports.append(pending.popleft().result())
        if items:
            pending.append(pool.submit(batch, index + 1, items))
        while pending:
            reports.append(pending.popleft().result())
    with (target / "typing.tsv.part").open("wb") as handle:
        for i, path in enumerate(reports):
            with path.open("rb") as part:
                header = part.readline()
                if i == 0:
                    handle.write(header)
                handle.write(part.read())
    (target / "typing.tsv.part").replace(target / "typing.tsv")
    (target / "complete.json").write_text(
        json.dumps(
            dict(
                representatives=len(seen),
                context=context,
                input_fasta_sha256=[sha(p) for p in a.fasta],
                output_sha256=sha(target / "typing.tsv"),
            ),
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fasta", type=Path, nargs="+", required=True)
    for name in ("program", "database", "policy", "reference", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--database-manifest", type=Path, required=True, help="mob-database.json in the release controls"
    )
    parser.add_argument(
        "--upstream-reference",
        type=Path,
        help="verify all eight DOI inputs in a fresh mob_init directory and record its complete actual inventory",
    )
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=96)
    stage(parser.parse_args())
