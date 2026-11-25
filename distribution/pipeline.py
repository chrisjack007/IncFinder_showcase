"""Protected release jobs. Sign locally, reconcile a draft, then publish the verified bytes."""

import argparse
import json
import re
import sys
import platform
import zlib
import subprocess
from pathlib import Path

from release import (
    CFG,
    ROOT,
    TRUST,
    accepted_index,
    build_launchers,
    digest,
    encode,
    prepare_rf,
    signed_sums,
    verify_release,
)


def command(*args):
    return subprocess.check_output(list(map(str, args)), text=True, encoding="utf8").strip()


def api(path):
    return json.loads(command("gh", "api", path))


def sign(path, key):
    sig = Path(str(path) + ".sig")
    if sig.exists():
        from release import verify

        verify(path.read_bytes(), sig, TRUST / "dist-allowed_signers")
        return
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "incfinder-release", str(path)], check=True
    )


def scientific_source(legacy, requested_commit=None, run_id=None):
    sums = signed_sums(TRUST / "rc2-SHA256SUMS", TRUST / "allowed_signers")
    manifest = legacy / "release-manifest.json"
    tree = legacy / "public-source-tree.json"
    if digest(manifest) != sums[manifest.name] or digest(tree) != sums[tree.name]:
        raise ValueError("Signed engineering source evidence differs")
    scientific = json.loads(manifest.read_bytes())
    commit = scientific["source"]["engineering_commit"]
    platforms = scientific["science"]["platforms"]
    if (
        not re.fullmatch("[0-9a-f]{40}", commit)
        or (requested_commit and requested_commit != commit)
        or scientific["source"]["public_source_tree_sha256"] != sums[tree.name]
        or set(platforms) != {"windows", "wsl2", "linux", "docker"}
        or any(
            v["status"] != "PASS" or not v["independent_expected_output_arrays_exact"]
            for v in platforms.values()
        )
    ):
        raise ValueError("Base engineering commit or frozen scientific acceptance mismatch")
    proof = dict(
        engineering_commit=commit,
        scientific_manifest=sums[manifest.name],
        source_tree_sha256=sums[tree.name],
    )
    if run_id is not None:
        run = api(f"repos/chrisjack007/IncFinder_engineer/actions/runs/{run_id}")
        if (
            run["conclusion"] != "success"
            or run["head_sha"] != commit
            or run["path"].split("@")[0] != ".github/workflows/candidate.yml"
            or run["repository"]["full_name"] != "chrisjack007/IncFinder_engineer"
        ):
            raise ValueError("Engineering workflow did not pass for the frozen source commit")
        proof["workflow"] = run["html_url"]
    return proof


def build(args):
    from bundle import build as packages
    from pages import render

    render(check=True)
    sha = command("git", "-C", ROOT, "rev-parse", "HEAD")
    if command("git", "-C", ROOT, "status", "--porcelain"):
        raise ValueError("The signing checkout must be clean")
    source = scientific_source(args.legacy, args.engineering_sha, args.engineering_run)
    prepare_rf(args)
    sign(args.out / CFG["rf_tag"] / "rf-index.json", args.key)
    packages(args)
    sign(args.out / "internal/RUNTIME-SHA256SUMS", args.key)
    build_launchers(args)
    sign(args.out / CFG["tag"] / "SHA256SUMS", args.key)
    verify_release(args)
    subprocess.run(
        [sys.executable, "-B", str(ROOT / "distribution/test_signed_assets.py"), str(args.out)], check=True
    )
    (args.out / "build-receipt.json").write_bytes(
        encode(
            dict(
                tag=CFG["tag"],
                source_commit=sha,
                engineering_source=source,
                go=command(args.go, "version"),
                python=platform.python_version(),
                compression=zlib.ZLIB_RUNTIME_VERSION,
                packager={p.name: digest(p) for p in sorted((ROOT / "distribution").glob("*.py"))},
                input_manifests={
                    p.name: digest(p) for p in (TRUST / "portable-SHA256SUMS", TRUST / "rc2-SHA256SUMS")
                },
                model_index=digest(accepted_index(args.out)),
                manifest=digest(args.out / CFG["tag"] / "SHA256SUMS"),
                layout=args.layout,
            )
        )
    )


def assets(args):
    if args.component == "software":
        verify_release(args)
        root = args.out / CFG["tag"]
        hashes = signed_sums(root / "SHA256SUMS", TRUST / "dist-allowed_signers")
        for name in ("SHA256SUMS", "SHA256SUMS.sig"):
            hashes[name] = digest(root / name)
        return {name: root / name for name in hashes}, hashes
    idx = accepted_index(args.out)
    rows = json.loads(idx.read_bytes())["parts"]
    result = {idx.name: idx, idx.with_suffix(".json.sig").name: idx.with_suffix(".json.sig")}
    hashes = {name: digest(path) for name, path in result.items()}
    for row in rows:
        p = args.legacy / row["name"]
        if p.stat().st_size != row["size_bytes"] or digest(p) != row["sha256"]:
            raise ValueError("RF upload source differs: " + p.name)
        result[p.name] = p
        hashes[p.name] = row["sha256"]
    return result, hashes


def fetch_release(repo, tag):
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    return next((rel for page in pages for rel in page if rel["tag_name"] == tag), None)


def immutability(enable=False):
    repo = CFG["repository"]
    if api(f"repos/{repo}")["permissions"].get("admin") is not True:
        raise ValueError("A repository administrator must verify the immutable release setting")
    endpoint = f"repos/{repo}/immutable-releases"
    read = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if read.returncode == 0 and json.loads(read.stdout).get("enabled") is True:
        print("Immutable releases enabled", repo)
        return
    if "HTTP 404" not in read.stderr:
        raise RuntimeError("Unable to read the repository release setting")
    if not enable:
        raise ValueError("Immutable releases disabled: " + repo)
    command("gh", "api", "--method", "PUT", endpoint)
    if api(endpoint).get("enabled") is not True:
        raise ValueError("Could not verify immutability after enabling it")
    print("Immutable releases enabled", repo)


def check_published_models(out):
    from acceptance import anonymous_release

    index = accepted_index(out)
    signature = index.with_suffix(".json.sig")
    expected = {row["name"]: "sha256:" + row["sha256"] for row in json.loads(index.read_bytes())["parts"]}
    expected.update({p.name: "sha256:" + digest(p) for p in (index, signature)})
    remote = anonymous_release(CFG["rf_tag"])
    if {a["name"]: a["digest"] for a in remote["assets"]} != expected:
        raise ValueError("Published RF assets differ from the signed model index")


def publish(args):
    repo = CFG["repository"]
    tag = CFG["tag"] if args.component == "software" else CFG["rf_tag"]
    receipt = json.loads((args.out / "build-receipt.json").read_bytes())
    sha = command("git", "-C", ROOT, "rev-parse", "HEAD")
    if (
        receipt["tag"] != CFG["tag"]
        or receipt["source_commit"] != sha
        or receipt["manifest"] != digest(args.out / CFG["tag"] / "SHA256SUMS")
        or receipt["model_index"] != digest(accepted_index(args.out))
    ):
        raise ValueError("Release not bound to this build and checkout")
    if api(f"repos/{repo}/immutable-releases").get("enabled") is not True:
        raise ValueError("Enable and authorize immutable releases for this repository first")
    if args.component == "software" and args.command == "publish":
        check_published_models(args.out)
    files, hashes = assets(args)
    current = fetch_release(repo, tag)
    if current is None and args.command == "draft":
        notes = ROOT / "docs/dist.1.md" if args.component == "software" else ROOT / "docs/rf-release.md"
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                repo,
                "--target",
                sha,
                "--draft",
                "--prerelease",
                "--latest=false",
                "--title",
                "IncFinder " + tag,
                "--notes-file",
                str(notes),
            ],
            check=True,
        )
        current = fetch_release(repo, tag)
    if not current or (current["draft"] and current["target_commitish"] != sha):
        raise ValueError("Expected a draft targeting the exact build commit")
    expected = {n: "sha256:" + h for n, h in hashes.items()}
    remote = {a["name"]: a["digest"] for a in current["assets"]}
    if any(name not in expected or expected[name] != value for name, value in remote.items()):
        raise ValueError("Draft contains an unexpected or changed attachment")
    missing = sorted(expected.keys() - remote.keys())
    if missing and args.command == "draft" and current["draft"]:
        for name in missing:
            subprocess.run(["gh", "release", "upload", tag, str(files[name]), "--repo", repo], check=True)
        current = fetch_release(repo, tag)
        remote = {a["name"]: a["digest"] for a in current["assets"]}
    if remote != expected:
        raise ValueError("Incomplete release attachment set")
    if args.command == "publish" and current["draft"]:
        command(
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/releases/{current['id']}",
            "-F",
            "draft=false",
            "-F",
            "prerelease=true",
            "-f",
            "make_latest=false",
        )
        current = fetch_release(repo, tag)
    if not current["draft"]:
        if current.get("immutable") is not True:
            raise ValueError("Public release lacks immutability")
        ref = api(f"repos/{repo}/git/ref/tags/{tag}")["object"]
        while ref["type"] == "tag":
            ref = api(f"repos/{repo}/git/tags/{ref['sha']}")["object"]
        if ref["type"] != "commit" or ref["sha"] != sha:
            raise ValueError("Published tag points to a different commit")
        subprocess.run(["gh", "release", "verify", tag, "--repo", repo], check=True)
        from acceptance import anonymous_release

        actual = anonymous_release(tag)
        if {a["name"]: a["digest"] for a in actual["assets"]} != expected:
            raise ValueError("Public inventory differs from the signed local candidate")
    print(tag, "draft" if current["draft"] else "immutable, anonymously verified", len(files), "assets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    for cmd in ("build", "draft", "publish"):
        sub = cmds.add_parser(cmd)
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument("--legacy", type=Path, required=True)
        if cmd == "build":
            sub.add_argument("--portable", type=Path, required=True)
            sub.add_argument("--layout", choices=("shared", "split"), required=True)
            sub.add_argument("--go", default="go")
            sub.add_argument("--key", type=Path, required=True)
            sub.add_argument("--engineering-sha")
            sub.add_argument("--engineering-run", type=int)
        else:
            sub.add_argument("--component", choices=("rf", "software"), required=True)
    admin = cmds.add_parser(
        "immutability", help="Administrator check for the configured showcase repository"
    )
    admin.add_argument(
        "--enable",
        action="store_true",
        help="Enable future immutable releases with separate owner authorization",
    )
    args = parser.parse_args()
    if args.command == "immutability":
        immutability(args.enable)
        parser.exit()
    args.out = args.out.resolve()
    if args.command == "build":
        if (args.engineering_sha and not re.fullmatch("[a-f0-9]{40}", args.engineering_sha)) or (
            args.engineering_run is not None and args.engineering_run < 1
        ):
            parser.error("Use the engineering commit SHA and candidate workflow run ID")
        build(args)
    else:
        publish(args)
