"""Build the dist.1 deliveries from the signed rc2 release bytes (Python 3.11+)."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launcher"
CFG = json.loads((LAUNCHER / "distribution.json").read_bytes())
TRUST = LAUNCHER / "trust"
OLD = {
    "windows": "IncFinder-Windows-x64.zip",
    "linux": "IncFinder-Linux-x64.tar.gz",
    "wsl2": "IncFinder-WSL2-x64.tar.gz",
    "docker": "IncFinder-Docker-amd64.zip",
}


def encode(obj):
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def digest(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def verify(body, signature, key):
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(key),
            "-I",
            "incfinder-release",
            "-n",
            "incfinder-release",
            "-s",
            str(signature),
        ],
        input=body,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def signed_sums(path, key):
    raw = path.read_bytes()
    verify(raw, Path(str(path) + ".sig"), key)
    result = {}
    for line in raw.decode().splitlines():
        sha, name = line.split()
        name = name.lstrip("*")
        if name in result or len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError("Invalid signed entry: " + name)
        result[name] = sha
    return result


def write_sums(dest, files):
    names = [p.name for p in files]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate asset name")
    dest.write_bytes(
        "".join(f"{digest(p)}  {p.name}\n" for p in sorted(files, key=lambda p: p.name)).encode()
    )


def rf_index():
    engine = ROOT / "NT_V3_650M_TSRF_SW"
    contract = json.loads((engine / "config/model_delivery.json").read_bytes())
    inventory = json.loads((engine / "config/assets.json").read_bytes())["files"]
    files = {
        row["path"].split("/", 1)[1]: {field: row[field] for field in ("size_bytes", "sha256")}
        for row in inventory
        if row["path"].startswith(("models/", "vendor/ntv3/"))
    }
    for line in (TRUST / "model-hashes.tsv").read_text().splitlines():
        name, size, sha = line.split("\t")
        if files.get(name) != {"size_bytes": int(size), "sha256": sha}:
            raise ValueError("Frozen model identity differs: " + name)
    identity = {k: contract[k] for k in ("ntv3", "ntv3_code")}
    identity["files"] = files
    return dict(
        schema=1,
        repository=CFG["repository"],
        tag=CFG["rf_tag"],
        parts=contract["rf"]["parts"],
        model_set_sha256=hashlib.sha256(encode(identity)).hexdigest(),
        **identity,
    )


def prepare_rf(args):
    index = rf_index()
    sums = signed_sums(TRUST / "rc2-SHA256SUMS", TRUST / "allowed_signers")
    whole = {"stage1.joblib": hashlib.sha256(), "stage2.joblib": hashlib.sha256()}
    for row in index["parts"]:
        name = row["name"]
        source = args.legacy / name
        if source.stat().st_size != row["size_bytes"] or sums[name] != row["sha256"]:
            raise ValueError("RF signed size or digest mismatch: " + name)
        item = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(8 << 20), b""):
                item.update(block)
                whole[name.split(".part-")[0]].update(block)
        if item.hexdigest() != row["sha256"]:
            raise ValueError("RF file changed: " + name)
    for name, state in whole.items():
        if state.hexdigest() != index["files"]["rf/" + name]["sha256"]:
            raise ValueError("Reassembled model differs: " + name)
    path = args.out / CFG["rf_tag"] / "rf-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(index))
    print(
        f"{len(index['parts'])} RF files and two complete models verified: {digest(path)} {path}",
        flush=True,
    )


def accepted_index(out):
    index = out / CFG["rf_tag"] / "rf-index.json"
    raw = index.read_bytes()
    verify(raw, index.with_suffix(".json.sig"), TRUST / "dist-allowed_signers")
    if raw != encode(rf_index()):
        raise ValueError("Signed RF index differs from the engine contract")
    return index


def build_launchers(args):
    root = args.out / CFG["tag"]
    internal = args.out / "internal/RUNTIME-SHA256SUMS"
    sums = signed_sums(internal, TRUST / "dist-allowed_signers")
    index = accepted_index(args.out)
    if sums.get(index.name) != digest(index):
        raise ValueError("RF index differs from the signed runtime manifest")
    needed = {CFG["packages"][key] for key in ("windows", "linux", "docker")}
    optional = CFG["packages"]["wsl2"]
    if set(sums) not in (needed | {index.name}, needed | {index.name, optional}):
        raise ValueError("Incomplete signed runtime set")
    for name in set(sums) - {index.name}:
        if digest(root / name) != sums[name]:
            raise ValueError("Signed runtime differs: " + name)
    values = [base64.b64encode(p.read_bytes()).decode() for p in (internal, internal.with_suffix(".sig"))]
    flags = f"-s -w -buildid= -X main.distManifest={values[0]} -X main.distSignature={values[1]}"
    for system, name in CFG["launchers"].items():
        env = dict(
            os.environ,
            GOOS=system,
            GOARCH="amd64",
            GOAMD64="v1",
            CGO_ENABLED="0",
            GOPROXY="off",
            GOTOOLCHAIN="local",
        )
        subprocess.run(
            [
                args.go,
                "build",
                "-trimpath",
                "-buildvcs=false",
                "-ldflags",
                flags,
                "-o",
                str(root / name),
                ".",
            ],
            cwd=LAUNCHER,
            env=env,
            check=True,
        )
    shutil.copyfile(TRUST / "dist-allowed_signers", root / "allowed_signers")
    names = set(sums) - {index.name} | set(CFG["launchers"].values()) | {CFG["evidence"], "allowed_signers"}
    extras = {p.name for p in root.iterdir()} - names - {"SHA256SUMS", "SHA256SUMS.sig"}
    if extras:
        raise ValueError(f"Unexpected release assets: {sorted(extras)}")
    write_sums(root / "SHA256SUMS", [root / n for n in names])
    print("Final unsigned manifest:", digest(root / "SHA256SUMS"), flush=True)


def verify_release(args):
    root = args.out / CFG["tag"]
    catalog = accepted_index(args.out)
    inner = signed_sums(args.out / "internal/RUNTIME-SHA256SUMS", TRUST / "dist-allowed_signers")
    outer = signed_sums(root / "SHA256SUMS", TRUST / "dist-allowed_signers")
    names = (
        set(CFG["launchers"].values())
        | {CFG["evidence"], "allowed_signers"}
        | (set(inner) - {catalog.name})
    )
    required = {CFG["packages"][k] for k in ("windows", "linux", "docker")} | {catalog.name}
    if set(inner) not in (required, required | {CFG["packages"]["wsl2"]}) or inner[catalog.name] != digest(
        catalog
    ):
        raise ValueError("Wrong signed runtime set")
    if names != set(outer) or {p.name for p in root.iterdir()} != names | {"SHA256SUMS", "SHA256SUMS.sig"}:
        raise ValueError("Release inventory mismatch")
    if (root / "allowed_signers").read_bytes() != (TRUST / "dist-allowed_signers").read_bytes():
        raise ValueError("Unexpected published signer")
    for name, sha in outer.items():
        if digest(root / name) != sha or (name in inner and inner[name] != sha):
            raise ValueError("Changed signed attachment: " + name)
    print(
        f"{CFG['tag']}: {len(outer)} payload hashes, model and both release signatures verified", flush=True
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("rf", "packages", "launchers", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--out", required=True, type=Path)
        if name in ("rf", "packages"):
            sub.add_argument("--legacy", required=True, type=Path)
        if name == "packages":
            sub.add_argument("--portable", required=True, type=Path)
            sub.add_argument("--layout", choices=("split", "shared"), required=True)
        if name == "launchers":
            sub.add_argument("--go", default="go")
    args = parser.parse_args()
    args.out = args.out.resolve()
    if args.command == "packages":
        from bundle import build

        build(args)
    else:
        {"rf": prepare_rf, "launchers": build_launchers, "verify": verify_release}[args.command](args)


if __name__ == "__main__":
    main()
