"""Join actual stage outputs into the replay view and compare them with frozen rc2 data."""

import argparse
import csv
import gzip
import io
import json
import re
from collections import Counter
from pathlib import Path

from compare_fragments import compare, sha
from derive_rules import derive, read
from source_metadata import compare as compare_source_metadata


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    pending = path.with_name(path.name + ".part")
    pending.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    pending.replace(path)


def table(path, fields, rows):
    fields = fields.split()
    pending = path.with_name(path.name + ".part")
    with (
        pending.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0, compresslevel=5) as gz,
    ):
        with io.TextIOWrapper(gz, encoding="utf-8", newline="") as stream:
            out = csv.DictWriter(stream, fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
            out.writeheader()
            out.writerows(rows)
    pending.replace(path)


def indexed(rows, fields):
    result = {}
    for row in rows:
        key = tuple(row[name] for name in fields)
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = row
    return result


def by_id(path):
    return {k[0]: r for k, r in indexed(read(path), ["record_id"]).items()}


def tokenize(raw):
    return sorted(
        {t.strip() for t in re.split("[,;|]", raw) if re.fullmatch(r"Inc[A-Za-z0-9/-]+", t.strip())}
    )


def labels(raw):
    return {s for s in raw.split(";") if s and s != "No_Inc"}


def run(a):
    if a.output.resolve() == a.frozen.resolve() or a.comparison.resolve() == a.frozen.resolve():
        raise ValueError("choose a fresh output directory outside the frozen view")
    a.output.mkdir(parents=True, exist_ok=True)
    a.comparison.mkdir(parents=True, exist_ok=True)
    genbank = load(a.genbank / "complete.json")
    skani = load(a.skani / "complete.json")
    mob = load(a.mob / "complete.json")
    if skani["context"]["raw_archive_sha256"] != genbank["archive_sha256"]:
        raise ValueError("skani and GenBank refer to different input recoveries")
    if (
        sha(a.skani / "clusters.tsv") != skani["clusters_sha256"]
        or sha(a.mob / "typing.tsv") != mob["output_sha256"]
    ):
        raise ValueError("an upstream stage output differs from its completion receipt")
    order = [r["label"] for r in read(a.frozen / "labels.tsv.gz")]
    old_parents = by_id(a.frozen / "representatives.tsv.gz")
    catalog = indexed(read(a.controls / "catalog.tsv.gz"), ["Assembly Accession"])
    src = indexed(read(a.controls / "source_records.tsv.gz"), ["assembly_accession", "seq_id"])
    if len(src) != genbank["records"]:
        raise ValueError("recovered GenBank cohort differs from the source index")
    accessions = {}
    for (asm, acc), r in sorted(src.items()):
        date = catalog.get((asm,), {}).get("Assembly Release Date", "")
        accessions[asm, acc] = dict(
            assembly_accession=asm,
            sequence_accession=acc,
            length_bp=r["seq_length"],
            sequence_sha256=r["sequence_sha256"],
            gbff_sha256=r["member_sha256"],
            assembly_release_date=date,
            inclusion="included" if date else "assembly_absent_from_frozen_date_catalog",
        )
    table(
        a.output / "accessions.tsv.gz",
        "assembly_accession sequence_accession length_bp sequence_sha256 gbff_sha256 assembly_release_date inclusion",
        accessions.values(),
    )
    clusters = indexed(read(a.skani / "clusters.tsv"), ["assembly_accession", "sequence_accession"])
    if set(clusters) != {k for k, r in accessions.items() if r["inclusion"] == "included"}:
        raise ValueError("different eligible cohort between GenBank and skani")
    table(
        a.output / "clusters.tsv.gz",
        "cluster_id cluster_size assembly_accession sequence_accession representative_assembly representative_accession is_representative",
        (clusters[k] for k in sorted(clusters)),
    )
    rep = {acc: r for (asm, acc), r in clusters.items() if r["is_representative"] == "1"}
    typed = by_id(a.mob / "typing.tsv")
    if set(rep) != set(typed) or len(rep) != skani["clusters"] or len(rep) != mob["representatives"]:
        raise ValueError("MOB-suite must run on all representatives selected by this skani run")
    if set(rep) == set(old_parents):
        inputs = load(a.controls / "expected-inputs.json")
        expected_fastas = [inputs[f"00_input/{split}.plasmids.ANI80_AF90.fa"] for split in ("train", "test")]
        if mob["input_fasta_sha256"] != expected_fastas:
            raise ValueError("the MOB-suite inputs differ from the dated and ordered rc2 representatives")
    rc2 = by_id(a.controls / "rc2-typing.tsv.gz")
    fold_rows = by_id(a.folds) if a.folds else old_parents
    policy_rows = derive(typed.values())
    policy = {r["raw_subtype"]: r["final_label"] for r in policy_rows}
    if not set(policy.values()) <= set(order) - {"No_Inc"}:
        raise ValueError("the new policy contains a class outside the rc2 output contract")
    rule_fields = "raw_subtype major_group major_plasmids subtype_plasmids final_label rule"
    table(a.output / "subtypes.tsv.gz", rule_fields, policy_rows)
    table(
        a.comparison / "replay-policy-calculation.tsv.gz",
        rule_fields + " preliminary_label preliminary_policy_count",
        policy_rows,
    )
    parents, changes, differences = {}, [], Counter()
    for acc in sorted(set(rep) | set(old_parents)):
        old, adopted, new = old_parents.get(acc), rc2.get(acc), typed.get(acc)
        if new:
            asm = rep[acc]["assembly_accession"]
            date = accessions[(asm, acc)]["assembly_release_date"]
            split = "train" if int(date[:4]) <= 2024 else "holdout"
            subs = tokenize(new["raw_rep_types"])
            if set(subs) != labels(new["inc_type"]):
                raise ValueError("raw field and extracted subtypes disagree for " + acc)
            calls = {policy[t] for t in subs} or {"No_Inc"}
            joined = ";".join(name for name in order if name in calls)
            fold = fold_rows.get(acc, {}).get("cv_fold", "")
            if split == "train" and fold not in set(map(str, range(1, 11))):
                raise ValueError("missing training fold for the new cohort, provide --folds: " + acc)
            parents[acc] = dict(
                record_id=acc,
                assembly_accession=asm,
                cluster_id=rep[acc]["cluster_id"],
                assembly_release_date=date,
                split=split,
                cv_fold=fold if split == "train" else "",
                raw_rep_types=new["raw_rep_types"],
                inc_subtypes=";".join(subs),
                final_labels=joined,
            )
        merged, adopted, replay = old or {}, adopted or {}, parents.get(acc, {})
        flags = {
            "cohort": bool(old) != bool(new),
            "raw_rep_types": merged.get("raw_rep_types") != replay.get("raw_rep_types"),
            "inc_subtypes": labels(merged.get("inc_subtypes", "")) != labels(replay.get("inc_subtypes", "")),
            "final_labels": labels(merged.get("final_labels", "")) != labels(replay.get("final_labels", "")),
            "rc2_inc_subtypes": labels(adopted.get("inc_type", "")) != labels(replay.get("inc_subtypes", "")),
            "rc2_final_labels": labels(adopted.get("final_labels", ""))
            != labels(replay.get("final_labels", "")),
        }
        differing = [k for k, v in flags.items() if v]
        if differing:
            differences.update(differing)
            changes.append(
                dict(
                    record_id=acc,
                    differing_fields=";".join(differing),
                    historical_raw=merged.get("raw_rep_types", ""),
                    replay_raw=replay.get("raw_rep_types", ""),
                    historical_subtypes=merged.get("inc_subtypes", ""),
                    rc2_subtypes=adopted.get("inc_type", ""),
                    replay_subtypes=replay.get("inc_subtypes", ""),
                    historical_labels=merged.get("final_labels", ""),
                    rc2_labels=adopted.get("final_labels", ""),
                    replay_labels=replay.get("final_labels", ""),
                )
            )
    table(
        a.comparison / "typing-differences.tsv.gz",
        "record_id differing_fields historical_raw replay_raw historical_subtypes rc2_subtypes replay_subtypes historical_labels rc2_labels replay_labels",
        changes,
    )
    table(
        a.output / "representatives.tsv.gz",
        "record_id assembly_accession cluster_id assembly_release_date split cv_fold raw_rep_types inc_subtypes final_labels",
        parents.values(),
    )
    save(
        a.comparison / "typing-comparison.json",
        dict(
            frozen_representatives=len(old_parents),
            replay_representatives=len(parents),
            compared_to_rc2=len(rc2),
            differing_records=len(changes),
            differing_fields=dict(differences),
            historical_raw_field="historical merged MOB-suite replicon text",
            rc2_columns=["record_id", "inc_type", "final_labels"],
        ),
    )
    compare_fragment_results = {}
    class_counts = {}
    for split in ("train", "holdout"):
        result = compare(split, a.assembled, a.frozen, a.output / "representatives.tsv.gz", a.output)
        (a.output / (split + "_record_differences.tsv.gz")).replace(
            a.comparison / (split + "_record_differences.tsv.gz")
        )
        stage = "09_build_train_dataset_result" if split == "train" else "10_build_test_dataset_result"
        stem = "train.final_800k_plus_lt1000" if split == "train" else "test.final_200k_plus_lt1000"
        detail_file = split + "_source_metadata.tsv.gz"
        result["source_metadata"] = compare_source_metadata(
            a.frozen / detail_file,
            a.assembled / "result" / stage / "data" / (stem + ".metadata.tsv"),
            a.output / detail_file,
            a.comparison / (split + "_source_metadata_differences.tsv.gz"),
        )
        compare_fragment_results[split] = result
        class_counts[split] = Counter(
            label
            for r in read(a.output / (split + "_records.tsv.gz"))
            for label in r["final_labels"].split(";")
        )
    save(a.comparison / "metasim-comparison.json", compare_fragment_results)
    rep_counts = Counter(label for r in parents.values() for label in r["final_labels"].split(";"))
    table(
        a.output / "labels.tsv.gz",
        "label stage2_index representative_plasmids training_records holdout_records raw_subtypes",
        (
            dict(
                label=lab,
                stage2_index="" if lab == "No_Inc" else i,
                representative_plasmids=rep_counts[lab],
                training_records=class_counts["train"][lab],
                holdout_records=class_counts["holdout"][lab],
                raw_subtypes=";".join(sorted(t for t, v in policy.items() if v == lab)),
            )
            for i, lab in enumerate(order)
        ),
    )
    save(
        a.output / "sources.json",
        dict(
            genbank_archive_sha256=genbank["archive_sha256"],
            raw_records=len(src),
            eligible=len(clusters),
            representatives=len(parents),
            training_fold_source="supplied assignment" if a.folds else "rc2 per-representative assignment",
            skani_program_sha256=skani["context"]["program"],
            skani_clusters_tsv_sha256=skani["clusters_sha256"],
            mob_program_sha256=mob["context"]["program_sha256"],
            mob_typing_tsv_sha256=mob["output_sha256"],
            mob_input_fasta_sha256=mob["input_fasta_sha256"],
            fragment_outputs=compare_fragment_results,
        ),
    )
    members = [
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
    save(
        a.output / "manifest.json",
        dict(
            schema=1,
            tag="v0.5.0rc2-provenance.1",
            view="replay",
            files={
                name: dict(bytes=(a.output / name).stat().st_size, sha256=sha(a.output / name))
                for name in members
            },
        ),
    )
    print(
        json.dumps(
            dict(
                representatives=len(parents),
                typing_differences=dict(differences),
                fragment_differences={k: v["differing_rows"] for k, v in compare_fragment_results.items()},
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("frozen", "controls", "genbank", "skani", "mob", "assembled", "output", "comparison"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--folds", type=Path, help="assigned record_id and cv_fold for a changed training cohort"
    )
    run(parser.parse_args())
