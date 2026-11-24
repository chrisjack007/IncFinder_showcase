"""Recalculate the 2026-05-07 class policy from per-plasmid replicon fields."""

import argparse
import csv
import gzip
import re
from collections import Counter
from pathlib import Path

BRANCH = {
    "IncHI1A": "IncHI1",
    "IncHI1B": "IncHI1",
    "IncHI2A": "IncHI2",
    "IncI1": "IncI1",
    "IncI1/B/O": "IncI1",
}
SMALL = {"IncN2": "IncN", "IncQ2": "IncQ", "IncI2": "IncI"}
OTHER = {"IncX_other", "Inc_other"}


def read(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream, delimiter="\t")


def major(subtype):
    suffix = subtype[3:]
    return "Inc" + suffix[0].upper() if suffix and suffix[0].isalpha() else subtype


def initial(subtype, major_name, n_major, n_subtype, n_types):
    if subtype in BRANCH:
        return BRANCH[subtype], "branch_subtype_merge_to_parent"
    if subtype in SMALL:
        return SMALL[subtype], "small_subtype_below_100_merge_to_major"
    if n_major < 100:
        return "Inc_other", "major_count_below_100_merge_to_Inc_other"
    if n_types == 1:
        return major_name, "major_count_at_least_100_single_subtype_keep_major"
    if n_subtype >= 100:
        return subtype, "major_count_at_least_100_subtype_count_at_least_100_keep_subtype"
    if major_name + "_other" in OTHER:
        return major_name + "_other", "major_count_at_least_100_trainable_other_bucket_kept"
    return major_name, "small_subtype_below_100_merge_to_major"


def derive(rows):
    seen = set()
    by_subtype = Counter()
    by_major = Counter()
    for row in rows:
        acc = row["record_id"]
        if acc in seen:
            raise ValueError("duplicate representative " + acc)
        seen.add(acc)
        subtypes = {
            s.strip()
            for s in re.split(r"[,;|]", row["raw_rep_types"])
            if re.fullmatch(r"Inc[A-Za-z0-9/-]+", s.strip())
        }
        by_subtype.update(subtypes)
        by_major.update({major(s) for s in subtypes})
    types_in_major = Counter(major(s) for s in by_subtype)
    preliminary = {}
    preliminary_support = Counter()
    for subtype, n in by_subtype.items():
        group = major(subtype)
        label, rule = initial(subtype, group, by_major[group], n, types_in_major[group])
        preliminary[subtype] = label, rule
        preliminary_support[group, label] += n
    relabel = {
        g
        for (g, k), n in preliminary_support.items()
        if n < 100 and by_major[g] >= 100 and not g[3].isdigit()
    }
    result = []
    for subtype, (initial_label, rule) in sorted(preliminary.items()):
        group = major(subtype)
        result.append(
            dict(
                raw_subtype=subtype,
                major_group=group,
                major_plasmids=by_major[group],
                subtype_plasmids=by_subtype[subtype],
                preliminary_label=initial_label,
                preliminary_policy_count=preliminary_support[group, initial_label],
                final_label=group if group in relabel else initial_label,
                rule="final_label_below_100_merge_same_capital_major" if group in relabel else rule,
            )
        )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("representatives", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--expected", type=Path, help="compare the six shared fields to subtypes.tsv.gz")
    args = p.parse_args()
    rules = derive(read(args.representatives))
    if not rules:
        raise ValueError("no Inc subtypes in input")
    if args.expected:
        reference = {r["raw_subtype"]: r for r in read(args.expected)}
        if {r["raw_subtype"] for r in rules} != reference.keys():
            raise ValueError("different subtype sets")
        for row in rules:
            if any(str(row[k]) != v for k, v in reference[row["raw_subtype"]].items()):
                raise ValueError("policy differs at " + row["raw_subtype"])
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=list(rules[0]), delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rules)
    print(f"{len(rules)} subtypes, {len({r['final_label'] for r in rules})} positive labels")
