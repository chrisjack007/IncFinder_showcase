"""Produce a separate threshold report from the sealed, stored outputs of an rc2 run."""
import argparse
import csv
import json
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from ntv3_tsrf.artifact_manifest import _verify, artifact_path
from ntv3_tsrf.classify import decide
from ntv3_tsrf.common import ROOT, read_json, sha256
from ntv3_tsrf.output_metadata import verify_result_metadata


def read_thresholds(path, labels):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    if len(rows) != len(labels) or [(r["model_key"], r["label_order"], r["label_name"])
                                    for r in rows] != [("nt_v3", str(i), n) for i, n in enumerate(labels)]:
        raise ValueError("Threshold table does not match the 21 rc2 labels")
    return [float(row["threshold"]) for row in rows]


def add_sheet_row(sheet, values):
    cells = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        cells.append(cell)
    sheet.append(cells)


def write_report(source, thresholds, destination, tag):
    source = artifact_path(source)
    # All retained artifacts are checked. A portable copy can be reviewed after the input was moved.
    manifest, manifest_sha, _ = _verify(source, external=False)
    summary = read_json(source / "run_summary.json")
    prediction = read_json(source / "prediction.json")
    context = verify_result_metadata(source, summary, prediction)
    labels = list(context["label_order"])
    if (manifest["kind"] != "run" or summary.get("version") != "0.5.0rc2"
            or manifest["model_manifest_sha256"] != sha256(ROOT / "config/assets.json")
            or labels != read_json(ROOT / "config/contract.json")["labels"]):
        raise ValueError("The source is not an rc2 run with the frozen model and label order")
    values = read_thresholds(thresholds, labels)
    with np.load(source / "probabilities.npz", allow_pickle=False) as data:
        s1, s2 = data["stage1"], data["stage2"]
        official = decide(s1, s2, context["stage2_thresholds"])
        if any(not np.array_equal(data[key], value) for key, value in official.items()):
            raise ValueError("Original labels and gate disagree with the recorded probabilities")
        custom = decide(s1, s2, values)
    if len(s1) != context["record_count"]:
        raise ValueError("Probability row count does not match the source report")
    fields = ["row_index", "Header", "Result", "final_status", "predicted_label_count",
              "predicted_labels", "gate_pass", "internal_status"]
    call_fields = ["row_index", "Header", "gate_pass", *labels]
    workbook = Workbook(write_only=True)
    results_sheet, calls_sheet, settings_sheet = [workbook.create_sheet(name) for name in
                                                 ("分型结果", "21项判定", "使用阈值")]
    add_sheet_row(results_sheet, fields)
    add_sheet_row(calls_sheet, call_fields)
    add_sheet_row(settings_sheet, ["model_key", "label_order", "label_name", "threshold"])
    for idx, (label, value) in enumerate(zip(labels, values)):
        add_sheet_row(settings_sheet, ["nt_v3", idx, label, value])
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("Choose a new report directory outside the source run")
    with tempfile.TemporaryDirectory(prefix=".incfinder-review-", dir=destination.parent) as scratch:
        stage = Path(scratch) / "report"
        stage.mkdir()
        with ((source / "inc_results.csv").open(encoding="utf-8-sig", newline="") as inputs,
              (stage / "custom_results.csv").open("w", encoding="utf-8-sig", newline="") as results,
              (stage / "custom_label_predictions.csv").open("w", encoding="utf-8-sig", newline="") as calls):
            reader, result_writer, call_writer = csv.DictReader(inputs), csv.writer(results), csv.writer(calls)
            result_writer.writerow(fields)
            call_writer.writerow(call_fields)
            for i in range(len(s1)):
                row = next(reader, None)
                gate = int(custom["gate"][i])
                if row is None or int(row["row_index"]) != i or int(row["gate_pass"]) != gate:
                    raise ValueError("Source row identity or gate mismatch")
                header = row["Header"]
                flags = custom["labels"][i].tolist()
                called = [name for name, yes in zip(labels, flags) if yes]
                joined = ";".join(called)
                record = [i, header, joined or "Inc Unclassified",
                          "Inc_classified" if called else "Inc_unclassified",
                          len(called), joined, gate, str(custom["internal_status"][i])]
                bits = [i, header, gate, *flags]
                result_writer.writerow(record)
                call_writer.writerow(bits)
                add_sheet_row(results_sheet, record)
                add_sheet_row(calls_sheet, bits)
            if next(reader, None) is not None:
                raise ValueError("Unexpected additional input row")
        workbook.save(stage / "custom_results.xlsx")
        shutil.copyfile(thresholds, stage / "thresholds.tsv")
        receipt = {"kind": "user_threshold_review", "release_tag": tag, "source_run_id": manifest["run_id"],
                   "source_manifest_sha256": manifest_sha, "source_probability_sha256": sha256(source / "probabilities.npz"),
                   "thresholds_sha256": sha256(stage / "thresholds.tsv"), "record_count": len(s1),
                   "source_threshold_mode": context["threshold_mode"], "source_scientific_status": context["scientific_status"],
                   "stage1_gate": 0.5, "operator": ">=", "label_order": labels,
                   "outputs_sha256": {f.name: sha256(f) for f in sorted(stage.iterdir())}}
        (stage / "review.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.rename(stage, destination)
    print("Custom report:", destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "thresholds", "destination"):
        parser.add_argument(name, type=Path)
    parser.add_argument("tag", choices=["v0.5.0rc2", "v0.5.0rc2-portable.1"])
    args = parser.parse_args()
    write_report(args.source, args.thresholds, args.destination, args.tag)
