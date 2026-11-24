"""Recover the frozen plasmid inventory independently from NCBI genomic.gbff.gz members."""

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import tarfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SHA = hashlib.sha256
DROP = b" \t\r\n0123456789"
FIELD = re.compile(rb"\n[A-Z]")
FEATURE = re.compile(rb"\n     \S")
PLASMID = re.compile(rb"\n                     /plasmid(?:=|[\r\n])")
RECORD_VERSION = re.compile(rb"^VERSION\s+(\S+)", re.M)


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = SHA()

    def read(self, size):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


def recover(data, expected, name, check_container=True):
    if check_container and expected and SHA(data).hexdigest() != expected[0]["member_sha256"]:
        raise ValueError(f"member digest: {name}")
    gb = gzip.decompress(data)
    rows = []
    start = 0
    while start < len(gb):
        end = gb.find(b"\n//", start)
        if end < 0:
            if gb[start:].strip():
                raise ValueError(f"unterminated GenBank record: {name}")
            break
        definition = gb.find(b"DEFINITION  ", start, end)
        stop = FIELD.search(gb, definition + 12, end) if definition >= 0 else None
        desc = gb[definition : stop.start()].lower() if stop else b""
        source = gb.find(b"\n     source ", start, end)
        feature_end = FEATURE.search(gb, source + 7, end) if source >= 0 else None
        if source >= 0 and not feature_end:
            field_end = FIELD.search(gb, source + 7, end)
            stop_source = field_end.start() if field_end else end
        else:
            stop_source = feature_end.start() if feature_end else end
        qualified = source >= 0 and PLASMID.search(gb, source, stop_source)
        if qualified or (b"plasmid" in desc and b"chromosome" not in desc):
            version = RECORD_VERSION.search(gb, start, end)
            origin = gb.find(b"\nORIGIN", start, end)
            if not version or origin < 0:
                raise ValueError(f"missing accession or sequence: {name}")
            seq = gb[gb.index(b"\n", origin + 1, end) + 1 : end].translate(None, DROP).upper()
            rows.append((version[1].decode("ascii"), seq))
        start = end + 3
    observed = {acc: (len(seq), SHA(seq).hexdigest()) for acc, seq in rows}
    frozen = {r["seq_id"]: (int(r["seq_length"]), r["sequence_sha256"]) for r in expected}
    if len(rows) != len(observed) or observed != frozen:
        raise ValueError(f"plasmid inventory differs: {name} {set(observed) ^ set(frozen)}")
    return rows


def ledger_rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream, delimiter="\t")


def stage(archive, ledger, out, archive_hash, workers, member_index=None, allow_repacked=False):
    out.mkdir(parents=True, exist_ok=True)
    if (out / "complete.json").exists():
        raise ValueError("choose a fresh destination or verify the existing receipt")
    entries = defaultdict(list)
    for row in ledger_rows(ledger):
        entries[row["archive_member"]].append(row)
    expected_records = sum(map(len, entries.values()))
    pending = deque()
    seq_count = 0
    seen = set()
    shard = None
    shards = []
    processed = 0

    def accept(item):
        nonlocal seq_count, processed, shard
        index, member, task = item
        rows = task.result()
        if (index - 1) % 1000 == 0:
            if shard:
                shard.close()
                shards[-1].with_suffix(".part").replace(shards[-1])
            filename = out / f"plasmids-{index:06d}.fa.gz"
            shards.append(filename)
            shard = gzip.open(filename.with_suffix(".part"), "wb", compresslevel=1)
        asm = member.split("/")[-2]
        for acc, seq in rows:
            shard.write(b">" + asm.encode("ascii") + b"|" + acc.encode("ascii") + b"\n" + seq + b"\n")
        seq_count += len(rows)
        processed += 1
        if processed % 1000 == 0:
            print(f"assemblies={processed} plasmids={seq_count}", flush=True)

    digest = None
    repacked = []

    def enqueue(name, data, pool):
        if name in seen:
            raise ValueError(f"duplicate member: {name}")
        seen.add(name)
        pending.append(
            (len(seen), name, pool.submit(recover, data, entries.get(name, ()), name, not allow_repacked))
        )
        if len(pending) >= 2 * workers:
            accept(pending.popleft())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        if archive.is_dir():
            if not member_index:
                raise ValueError("an expanded snapshot requires the frozen archive-member index")
            for record in ledger_rows(member_index):
                name = record["archive_member"]
                path = (archive / name).resolve()
                if archive.resolve() not in path.parents:
                    raise ValueError(f"invalid member path: {name}")
                data = path.read_bytes()
                actual_md5 = hashlib.md5(data).hexdigest()
                if actual_md5 != record["observed_md5"]:
                    if not allow_repacked:
                        raise ValueError(f"compressed source member differs: {name}")
                    repacked.append(
                        {
                            "member": name,
                            "frozen_md5": record["observed_md5"],
                            "observed_md5": actual_md5,
                            "observed_sha256": SHA(data).hexdigest(),
                        }
                    )
                enqueue(name, data, pool)
        else:
            if allow_repacked:
                raise ValueError("container equivalence is only available for a directory of GenBank members")
            if not archive_hash:
                raise ValueError("supply the expected SHA-256 of the tar archive")
            with archive.open("rb") as raw:
                reader = HashReader(raw)
                with tarfile.open(fileobj=reader, mode="r|", bufsize=2**21) as tar:
                    for member in tar:
                        name = member.name.removeprefix("./")
                        if member.isfile() and name.endswith("/genomic.gbff.gz"):
                            enqueue(name, tar.extractfile(member).read(), pool)
                while reader.read(2**23):
                    pass
                digest = reader.hash.hexdigest()
        while pending:
            accept(pending.popleft())
    if shard:
        shard.close()
        shards[-1].with_suffix(".part").replace(shards[-1])
    if (
        (not archive.is_dir() and digest != archive_hash)
        or set(entries) - seen
        or seq_count != expected_records
    ):
        raise ValueError(f"source archive or coverage differs: {digest} {seq_count}/{expected_records}")
    receipt = dict(
        archive_sha256=digest,
        archive_bytes=None if archive.is_dir() else archive.stat().st_size,
        member_index_sha256=file_hash(member_index) if member_index else None,
        members=processed,
        repacked_members=repacked,
        records=seq_count,
        ledger_sha256=SHA(ledger.read_bytes()).hexdigest(),
        shard_sha256={path.name: file_hash(path) for path in shards},
    )
    part = out / "complete.json.part"
    part.write_text(json.dumps(receipt, indent=2) + "\n")
    os.replace(part, out / "complete.json")


def file_hash(path):
    h = SHA()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive-sha256", help="required for tar input")
    parser.add_argument(
        "--member-index", type=Path, help="archive_members.tsv.gz for independently restored member files"
    )
    parser.add_argument(
        "--allow-repacked-members",
        action="store_true",
        help="record compressed-member differences and require every extracted accession, length and sequence digest to match",
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    stage(
        args.archive,
        args.inventory,
        args.output,
        args.archive_sha256,
        max(1, args.workers),
        args.member_index,
        args.allow_repacked_members,
    )
