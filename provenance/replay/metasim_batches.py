"""Replay the recorded MetaSim batches with an existing licensed MetaSim 0.9.5 install."""

import argparse
import csv
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def table(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def save(path, data):
    tmp = path.with_suffix(".partial")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def replay(root, out, entry, split):
    segment = "06_run_train_metasim_result" if split == "train" else "07_run_test_metasim_result"
    manifest = root / "result/05_dedup_chunks_result" / split / f"{split}_metasim_batch_manifest.tsv"
    historical = root / "result" / segment / f"{split}_metasim_run_results.tsv"
    original = {r["batch_id"]: r for r in table(historical)}
    batches = table(manifest)
    if len(batches) != len(original) or {r["batch_id"] for r in batches} != set(original):
        raise ValueError("batch keys differ")
    install = entry.parent
    java = shutil.which("java")
    tool_files = [entry] + sorted((install / "jars").glob("*.jar"))
    for directory in ("class", ".install4j"):
        tool_files.extend(
            p
            for p in sorted((install / directory).rglob("*"))
            if p.is_file() and p.suffix in {".class", ".jar", ".cfg", ".conf"}
        )
    tool_id = {str(p.relative_to(install)): digest(p) for p in tool_files}
    # The installation record identifies the release if the local packaging differs.
    if tool_id.get("jars/MetaSim.jar") != "5261a284ba29c66c6474fafa336f5d4766db84279e0561b893dc8d70fe23b4cd":
        log = install / ".install4j/installation.log"
        if not log.is_file() or "itemName: MetaSim 0.9.5" not in log.read_text(errors="replace"):
            raise ValueError("the installed MetaSim 0.9.5 program could not be identified")
    environment = dict(
        metasim_files=tool_id,
        java_on_path=str(Path(java).resolve()) if java else None,
        java_sha256=digest(Path(java)) if java else None,
        java_version=subprocess.run([java, "-version"], capture_output=True, text=True, check=True).stderr
        if java
        else None,
        manifest_sha256=digest(manifest),
        frozen_results_sha256=digest(historical),
    )
    help_out = subprocess.run([str(entry), "cmd", "-h"], capture_output=True)
    environment["help_sha256"] = hashlib.sha256(help_out.stdout + help_out.stderr).hexdigest()
    dst = out / "result" / segment
    dst.mkdir(parents=True, exist_ok=True)
    save(dst / "environment.json", environment)
    rows = []
    for row in batches:
        key = row["batch_id"]
        history = original[key]
        args = shlex.split(row["planned_command"])
        if args != shlex.split(history["command"]) or args[0] != "MetaSim-cmdline":
            raise ValueError(f"inconsistent recorded command: {key}")
        source = root / row["bin_fasta"]
        source_hash = digest(source)
        batch = out / row["output_dir"]
        batch.mkdir(parents=True, exist_ok=True)
        receipt = batch / "complete.json"
        event = json.loads(receipt.read_text()) if receipt.exists() else {}
        if event and (
            event["input_sha256"] != source_hash
            or any(event["environment"][k] != v for k, v in environment.items() if k != "help_sha256")
            or digest(out / event["row"]["fna_files"]) != event["row"]["fna_sha256"]
        ):
            raise ValueError(f"cached batch changed: {key}")
        if not event:
            attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=batch))
            if (
                args[args.index("-d") + 1] != row["output_dir"]
                or args[args.index("-s") + 1] != row["bin_fasta"]
            ):
                raise ValueError(f"incorrect paths for {key}")
            args[args.index("-d") + 1] = str(attempt)
            args[args.index("-s") + 1] = str(source)
            command = [str(entry), "cmd", *args[1:]]
            print(f"start {key}", flush=True)
            with (attempt / "stdout.log").open("wb") as stdout, (attempt / "stderr.log").open("wb") as stderr:
                subprocess.run(command, check=True, stdout=stdout, stderr=stderr)
            files = list(attempt.glob("*.fna"))
            if len(files) != 1:
                raise ValueError(f"unexpected outputs: {key}")
            fasta = files[0]
            output_sha = digest(fasta)
            with fasta.open("rb") as handle:
                records = sum(line.startswith(b">") for line in handle)
            if records != int(row["reads_requested"]):
                raise ValueError(f"incomplete batch: {key} {records}")
            row_out = dict(
                history,
                fna_files=fasta.relative_to(out).as_posix(),
                fna_sha256=output_sha,
                output_dir=attempt.relative_to(out).as_posix(),
                mode=history["mode"],
                stdout_log=(attempt / "stdout.log").relative_to(out).as_posix(),
                stderr_log=(attempt / "stderr.log").relative_to(out).as_posix(),
                command=shlex.join(command),
                metasim_executable_sha256=tool_id[entry.name],
                metasim_help_sha256=environment["help_sha256"],
                java_version=environment["java_version"],
                environment_check_id=digest(dst / "environment.json"),
            )
            event = dict(
                batch=key,
                input_sha256=source_hash,
                input_manifest_sha256=digest(root / row["bin_manifest"]),
                records=records,
                frozen_output_sha256=history["fna_sha256"],
                matches_frozen=output_sha == history["fna_sha256"],
                environment=environment,
                row=row_out,
            )
            save(receipt, event)
            print(f"complete {key} records={records} matches_frozen={event['matches_frozen']}", flush=True)
        save(dst / ("environment-" + event["row"]["environment_check_id"] + ".json"), event["environment"])
        rows.append(dict(event["row"], mode=history["mode"]))
    target = dst / historical.name
    temp = target.with_suffix(".part")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(target)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("frozen_root", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--metasim", required=True, type=Path, help="installed MetaSim executable")
    p.add_argument("--split", required=True, choices=["train", "test"])
    a = p.parse_args()
    replay(a.frozen_root.resolve(), a.output.resolve(), a.metasim.resolve(), a.split)
