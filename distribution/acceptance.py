"""Exercise the downloaded program on its real host and check all four receipts before promotion."""

import argparse
import csv
import json
import os
import platform as host
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from release import CFG, ROOT, TRUST, digest, encode, signed_sums

PLATFORMS = ("windows", "wsl2", "linux", "docker")
BASE = "https://github.com/" + CFG["repository"] + "/releases/download/"


def anonymous_release(tag):
    url = "https://api.github.com/repos/" + CFG["repository"] + "/releases/tags/" + quote(tag, safe="")
    with urlopen(Request(url, headers={"User-Agent": "IncFinder-acceptance"}), timeout=45) as response:
        release = json.load(response)
    if release["tag_name"] != tag or release["draft"] or release.get("immutable") is not True:
        raise ValueError("Expected public immutable candidate " + tag)
    return release


def get(url, dest):
    with (
        urlopen(Request(url, headers={"User-Agent": "IncFinder-acceptance"}), timeout=90) as response,
        dest.open("xb") as file,
    ):
        shutil.copyfileobj(response, file, 2 << 20)


def result(log, expected, source, root):
    paths = re.findall(r"(?m)^RESULT_DIR\t([^\r\n]+)", log)
    if len(paths) != 1:
        raise ValueError("Expected exactly one run directory in launcher output")
    directory = Path(paths[0]).resolve()
    if directory.parent != root.resolve():
        raise ValueError("Unexpected output directory")
    table = directory / "inc_results.csv"
    if str(table) not in log:
        raise ValueError("Launcher did not print the opened CSV path")
    with table.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    summary = json.loads((directory / "run_summary.json").read_bytes())
    if (
        [r["Header"] for r in rows] != expected
        or summary["input"]["record_count"] != len(expected)
        or summary["execution_status"] != "PASS"
        or summary["input"]["sha256"] != source
        or any(r["Scientific_status"] != "RESEARCH_PREVIEW" for r in rows)
    ):
        raise ValueError("CSV or run summary differs from the actual input")
    return dict(
        id=summary["run_id"], csv=str(table), csv_sha256=digest(table), ids=expected, input_sha256=source
    )


def run(args):
    platform = args.platform
    system = "windows" if os.name == "nt" else "linux"
    is_wsl = system == "linux" and "microsoft" in host.release().lower()
    native = "wsl2" if is_wsl else system
    if sys.platform not in ("win32", "linux") or platform not in (native, "docker"):
        raise ValueError("This host cannot certify " + platform)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise ValueError("Select an empty dedicated acceptance directory")
    public = args.assets is None
    root = work / "results"
    binary = CFG["launchers"][system]
    url = BASE + CFG["tag"] + "/"
    if public:
        release = anonymous_release(CFG["tag"])
        for name in ("SHA256SUMS", "SHA256SUMS.sig", binary):
            get(url + name, work / name)
    else:
        for name in ("SHA256SUMS", "SHA256SUMS.sig", binary):
            shutil.copyfile(args.assets / name, work / name)
    sums = signed_sums(work / "SHA256SUMS", TRUST / "dist-allowed_signers")
    exe = work / binary
    if sums.get(binary) != digest(exe):
        raise ValueError("Launcher digest mismatch")
    if public:
        observed = {a["name"]: a["digest"] for a in release["assets"]}
        for name in ("SHA256SUMS", "SHA256SUMS.sig", binary):
            if observed.get(name) != "sha256:" + digest(work / name):
                raise ValueError("Public release metadata mismatch: " + name)
    exe.chmod(0o700)
    env = dict(os.environ)
    for key in (
        "INCFINDER_MODEL_DIR",
        "INCFINDER_START_ASSETS",
        "INCFINDER_START_MODEL_VOLUME",
        "INCFINDER_OFFLINE",
    ):
        env.pop(key, None)
    env.update(INCFINDER_START_HOME=str(work / "start"), INCFINDER_PORTABLE_HOME=str(work / "portable"))
    if not public:
        env["INCFINDER_START_ASSETS"] = str(args.assets.resolve())
        if args.models:
            env["INCFINDER_MODEL_DIR"] = str(args.models.resolve())
        if args.runtime:
            env["INCFINDER_PORTABLE_HOME"] = str(args.runtime.resolve())
    elif args.models or args.runtime:
        raise ValueError("Public cold acceptance requires empty caches")
    if platform == "docker":
        import uuid

        env["INCFINDER_START_MODEL_VOLUME"] = "incfinder-dist-accept-" + uuid.uuid4().hex
    cmd = [str(exe)] + (["--docker"] if platform == "docker" else [])
    subprocess.run([*cmd, "config", "results", str(root)], env=env, check=True)
    sample = ROOT / "NT_V3_650M_TSRF_SW/examples/sample.fastq"
    lines = sample.read_text().splitlines()
    own = work / "own reads.fastq"
    own.write_bytes(("\n".join(["@read-z", *lines[5:8], "@read-a", *lines[1:4]]) + "\n").encode())
    sample_sha = digest(sample.with_suffix(".fasta"))
    runs = []
    for step, choice in (("initial", "2"), ("offline", "2"), ("own", "3")):
        if step != "initial" or (not public and args.models):
            env.update(INCFINDER_OFFLINE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        inputs = (
            ("2" if platform == "docker" else "1")
            + "\n"
            + choice
            + "\n"
            + (str(own) + "\n" if step == "own" else "")
            + "0\n"
        ).encode("utf8")
        path = work / (step + ".log")
        with path.open("wb") as log:
            call = subprocess.run([str(exe)], input=inputs, stdout=log, stderr=subprocess.STDOUT, env=env)
        if call.returncode:
            raise RuntimeError(f"{step} failed ({call.returncode}); see {path}")
        output = path.read_text(encoding="utf8", errors="replace")
        if output.count("Official example verified: 2/2") != (0 if step == "own" else 1):
            raise ValueError("Incorrect verification scope in " + str(path))
        runs.append(
            result(
                output,
                ["read-z", "read-a"] if step == "own" else ["test_frag_000000001", "test_frag_000000002"],
                digest(own) if step == "own" else sample_sha,
                root,
            )
        )
        print(step, platform, runs[-1]["csv"], flush=True)
    if len({r["id"] for r in runs}) != 3 or len({r["csv"] for r in runs}) != 3:
        raise ValueError("A previous result was reused")
    pkg = CFG["packages"][platform]
    if platform == "wsl2" and pkg not in sums:
        pkg = CFG["packages"]["linux"]
    software = Path(env["INCFINDER_START_HOME"]) / CFG["tag"] / platform / "software"
    payload_sha = (software / ".incfinder-start-archive").read_text().strip()
    if sums[pkg] != payload_sha:
        raise ValueError("Executed archive differs from the signed release")
    reference_sha = digest(software / "docs/example-reference.json")
    receipt = dict(
        platform=platform,
        tag=CFG["tag"],
        origin="public" if public else "local",
        launcher_url=url + binary if public else None,
        launcher_sha256=sums[binary],
        manifest_sha256=digest(work / "SHA256SUMS"),
        reference_sha256=reference_sha,
        package=pkg,
        package_sha256=payload_sha,
        initial_cache="empty" if public or not (args.models or args.runtime) else "populated",
        offline=True,
        runs=runs,
    )
    (work / "receipt.json").write_bytes(encode(receipt))
    print(work / "receipt.json")


def gate(root, paths):
    sums = signed_sums(root / "SHA256SUMS", TRUST / "dist-allowed_signers")
    immutable = anonymous_release(CFG["tag"])
    published = {a["name"]: a["digest"] for a in immutable["assets"]}
    expected = {name: "sha256:" + sha for name, sha in sums.items()}
    expected["SHA256SUMS"] = "sha256:" + digest(root / "SHA256SUMS")
    expected["SHA256SUMS.sig"] = "sha256:" + digest(root / "SHA256SUMS.sig")
    if published != expected:
        raise ValueError("Published asset list differs from the signed local candidate")
    seen = set()
    reference = signed_sums(TRUST / "rc2-SHA256SUMS", TRUST / "allowed_signers")["example-reference.json"]
    sample_sha = digest(ROOT / "NT_V3_650M_TSRF_SW/examples/sample.fasta")
    used_ids = set()
    for p in paths:
        r = json.loads(p.read_bytes())
        target = r["platform"]
        pkg = CFG["packages"].get(target)
        if target == "wsl2" and pkg not in sums:
            pkg = CFG["packages"]["linux"]
        launcher = r["launcher_url"].rsplit("/", 1)[-1] if r.get("launcher_url") else ""
        allowed = (
            {CFG["launchers"]["windows" if target == "windows" else "linux"]}
            if target != "docker"
            else set(CFG["launchers"].values())
        )
        runs = r["runs"]
        if (
            target not in PLATFORMS
            or target in seen
            or r["tag"] != CFG["tag"]
            or r["origin"] != "public"
            or r["initial_cache"] != "empty"
            or r["offline"] is not True
            or r["reference_sha256"] != reference
            or r["manifest_sha256"] != digest(root / "SHA256SUMS")
            or pkg != r["package"]
            or sums.get(pkg) != r["package_sha256"]
            or launcher not in allowed
            or r["launcher_url"] != BASE + CFG["tag"] + "/" + launcher
            or sums.get(launcher) != r["launcher_sha256"]
            or len(runs) != 3
            or len({entry["csv"] for entry in runs}) != 3
            or [entry["ids"] for entry in runs]
            != [["test_frag_000000001", "test_frag_000000002"]] * 2 + [["read-z", "read-a"]]
            or runs[0]["input_sha256"] != sample_sha
            or runs[1]["input_sha256"] != sample_sha
            or runs[0]["input_sha256"] == runs[2]["input_sha256"]
            or any(not re.fullmatch("[0-9a-f]{64}", entry["csv_sha256"]) for entry in runs)
        ):
            raise ValueError("Invalid cold acceptance receipt: " + str(p))
        ids = {x["id"] for x in runs}
        if len(ids) != 3 or "" in ids or ids & used_ids:
            raise ValueError("Non-unique run identities")
        seen.add(target)
        used_ids.update(ids)
    if seen != set(PLATFORMS):
        raise ValueError("Missing public platform receipts: " + str(sorted(set(PLATFORMS) - seen)))
    print("Public acceptance PASS 4/4")


def published_pages(home):
    addresses = [
        home,
        "https://raw.githubusercontent.com/" + CFG["repository"] + "/main/README.md",
        "https://raw.githubusercontent.com/" + CFG["repository"] + "/main/README.zh-CN.md",
        "https://raw.githubusercontent.com/" + CFG["repository"] + "/" + CFG["tag"] + "/docs/dist.1.md",
    ]
    urls = [BASE + CFG["tag"] + "/" + n for n in CFG["launchers"].values()]
    for address in addresses:
        with urlopen(
            Request(address, headers={"User-Agent": "IncFinder-acceptance"}), timeout=45
        ) as response:
            text = response.read(8 << 20).decode("utf8")
        if any(value not in text for value in [*urls, "27.36 GB", "60 GB", "2/2", "inc_results.csv"]):
            raise ValueError("Public first-run entry differs: " + address)
    print("Anonymous homepage, both READMEs and release instructions verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    cmd = sub.add_parser("run")
    cmd.add_argument("--platform", choices=PLATFORMS, required=True)
    cmd.add_argument("--work", type=Path, required=True)
    cmd.add_argument("--assets", type=Path, help="Use local candidate bytes; receipt is local only")
    cmd.add_argument("--models", type=Path)
    cmd.add_argument("--runtime", type=Path)
    cmd = sub.add_parser("gate")
    cmd.add_argument("--release-dir", type=Path, required=True)
    cmd.add_argument("receipts", nargs=4, type=Path)
    cmd = sub.add_parser("published-pages")
    cmd.add_argument("--home", required=True)
    args = parser.parse_args()
    if args.action == "published-pages":
        published_pages(args.home)
    elif args.action == "run":
        run(args)
    else:
        gate(args.release_dir, args.receipts)
