"""Verify one CPU ct demo against the signed rc2 reference for this platform."""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from zipfile import BadZipFile

import numpy as np


def require(matches, field):
    if not matches:
        raise ValueError(f"Mismatch / 不一致: {field}")


def verify_example(directory, reference_path, reference_digest, platform, labels, thresholds, run_id=None):
    raw = reference_path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == reference_digest, "example-reference.json SHA-256")
    reference = json.loads(raw)
    require(reference["schema"] == 1 and reference["software_version"] == "0.5.0rc2", "reference version")
    expected = reference["platforms"][platform]["ct"]["arrays"]
    identifiers = reference["record_ids"]
    require(len(identifiers) == 2 and len(labels) == 21, "rc2 example dimensions")

    summary = json.loads((directory / "run_summary.json").read_bytes())
    if run_id is not None:
        require(summary["run_id"] == run_id, "run_summary.run_id")
    require(summary["execution_status"] == "PASS", "run_summary.execution_status")
    require(summary["version"] == reference["software_version"], "run_summary.version")
    require(summary["embedding_device"] == "cpu", "embedding_device")
    plan = summary["resource_plan"]
    require(plan["threads"] == 4 and plan["jobs"] == 1, "resource_plan threads/jobs")
    require(plan["memory"]["encoding_batch_size"] == 1, "resource_plan batch size")
    require(summary["input"]["format"] == "fasta", "input.format")
    require(summary["input"]["sha256"] == reference["input_sha256"]["fasta"], "sample.fasta SHA-256")
    require(summary["input"]["record_count"] == len(identifiers), "input.record_count")
    prediction = summary["prediction"]
    require(prediction["threshold_mode"] == "ct", "prediction.threshold_mode")
    require(prediction["stage1_threshold"] == 0.5, "prediction.stage1_threshold")
    require(prediction["stage2_thresholds"] == thresholds, "prediction.stage2_thresholds")
    require(prediction["scientific_status"] == "RESEARCH_PREVIEW", "prediction.scientific_status")
    require(prediction["label_order"] == labels, "prediction.label_order")

    with np.load(directory / "probabilities.npz", allow_pickle=False) as arrays:
        for name, dtype in (("stage1", "float32"), ("stage2", "float32"), ("gate", "bool"), ("labels", "uint8")):
            actual = arrays[name]
            target = np.asarray(expected[name], dtype=np.float64 if name in ("stage1", "stage2") else None)
            require(actual.shape == target.shape, f"{name}.shape")
            require(actual.dtype == dtype, f"{name}.dtype")
            equal = actual == target
            if name == "stage2":
                mask_difference = np.argwhere(np.isnan(actual) != np.isnan(target))
                if mask_difference.size:
                    i, j = mask_difference[0]
                    raise ValueError(f"stage2 NA mask: {identifiers[i]} / {labels[j]}")
                equal |= np.isnan(target)
            differences = np.argwhere(~equal)
            if differences.size:
                idx = tuple(differences[0])
                where = identifiers[idx[0]] + (f" / {labels[idx[1]]}" if len(idx) == 2 else "")
                raise ValueError(f"{name}: {where}: {actual[idx]} != {target[idx]}")

    with (directory / "inc_results.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        require(reader.fieldnames == ["row_index", "Header", "Result", "final_status", "predicted_label_count",
                                      "predicted_labels", "gate_pass", "internal_status", "Scientific_status"],
                "inc_results.csv header")
        for index, identifier in enumerate(identifiers):
            row = next(reader, None)
            require(row is not None, f"inc_results.csv missing row {index}")
            names = [label for label, on in zip(labels, expected["labels"][index]) if on]
            joined = ";".join(names)
            gate = expected["gate"][index]
            status = "Inc_called" if names else ("Inc_no_call" if gate else "Unclassified")
            fields = {
                "row_index": str(index), "Header": identifier,
                "gate_pass": str(int(gate)), "Result": joined or "Inc Unclassified",
                "predicted_labels": joined, "predicted_label_count": str(len(names)),
                "final_status": "Inc_classified" if names else "Inc_unclassified",
                "internal_status": status, "Scientific_status": "RESEARCH_PREVIEW",
            }
            require(None not in row, f"inc_results.csv extra field in row {index}")
            for field, value in fields.items():
                require(row.get(field) == value, f"inc_results.csv row {index} {field}")
        require(next(reader, None) is None, "inc_results.csv extra row")


def main():
    from ntv3_tsrf.common import ROOT, load_thresholds, read_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("sha256")
    parser.add_argument("platform")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        contract = read_json(ROOT / "config" / "contract.json")
        verify_example(args.directory, args.reference, args.sha256, args.platform,
                       contract["labels"], load_thresholds(contract), args.run_id)
    except (OSError, ValueError, KeyError, TypeError, csv.Error, BadZipFile, EOFError) as error:
        print(f"Official example check / 官方示例核验: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
