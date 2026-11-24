"""Compare directed skani edges using both accessions, reported ANI and both AFs."""

import argparse
import csv
import gzip
import hashlib
import io
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path


def component_id(file):
    fields = file.replace("\\", "/").rsplit("/", 1)[-1].split("__", 2)
    if len(fields) != 3:
        raise ValueError("unexpected input filename " + file)
    return fields[0] + "|" + fields[1]


def decimal_string(value):
    if "e" in value.lower():
        return format(Decimal(value).normalize(), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def edge_groups(path):
    groups = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if row["Ref_file"] == row["Query_file"]:
                continue
            values = row["ANI"], row["Align_fraction_ref"], row["Align_fraction_query"]
            if float(values[0]) >= 80 and min(float(values[1]), float(values[2])) >= 90:
                ref = component_id(row["Ref_file"])
                query = component_id(row["Query_file"])
                if ref != query:
                    groups[query].append((ref, *(decimal_string(value) for value in values)))
    return groups


@contextmanager
def output(path, fields):
    pending = path.with_name(path.name + ".partial")
    with (
        pending.open("wb") as f,
        gzip.GzipFile(fileobj=f, filename="", mode="wb", mtime=0, compresslevel=5) as gz,
    ):
        with io.TextIOWrapper(gz, encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            writer.writerow(fields.split())
            yield writer
    pending.replace(path)


def calculate(folder, pattern, destination, details_for=None, details_file=None):
    parts = sorted(folder.glob(pattern))
    if not parts:
        raise ValueError("no search partitions found")
    seen = {}
    with ExitStack() as stack:
        detail = (
            stack.enter_context(output(details_file, "query_id reference_id ani af_reference af_query"))
            if details_file
            else None
        )
        for path in parts:
            for query, values in sorted(edge_groups(path).items()):
                if query in seen:
                    raise ValueError("query in more than one batch: " + query)
                ends = hashlib.sha256()
                numeric = hashlib.sha256()
                for row in sorted(values):
                    ends.update((row[0] + "\n").encode("ascii"))
                    numeric.update(("\t".join(row) + "\n").encode("ascii"))
                    if detail and (details_for is None or query in details_for):
                        detail.writerow((query, *row))
                seen[query] = len(values), ends.hexdigest(), numeric.hexdigest()
    with output(
        destination, "query_id qualifying_rows reference_ids_sha256 reference_ids_ani_af_sha256"
    ) as writer:
        for key, identity in sorted(seen.items()):
            writer.writerow((key, *identity))
    print(
        len(parts),
        "partitions;",
        sum(v[0] for v in seen.values()),
        "qualifying directed non-self rows;",
        len(seen),
        "queries with qualifying rows",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("search_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pattern", default="batch-*.tsv")
    parser.add_argument(
        "--detail-queries", type=Path, help="limit detail to these assembly|sequence keys, one per line"
    )
    parser.add_argument(
        "--detail-output", type=Path, help="write complete directed rows, or just --detail-queries"
    )
    args = parser.parse_args()
    if args.detail_queries and not args.detail_output:
        parser.error("--detail-queries requires --detail-output")
    queries = set(args.detail_queries.read_text().splitlines()) if args.detail_queries else None
    calculate(args.search_directory, args.pattern, args.output, queries, args.detail_output)
