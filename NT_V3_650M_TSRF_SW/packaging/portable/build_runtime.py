"""Build the runtime from an installed, locked IncFinder environment."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from urllib.parse import unquote, urlsplit

VERSION = "0.5.0rc2"
PACKAGE = f"incfinder-{VERSION}-py_0.conda"
PACKAGE_SHA = "34cae07ba79aeded0c844716bbeb72269318f420bbd946aa9aaea54f9f87de3a"
RELEASE = "https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2/"


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def locked_urls(path):
    locked = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("https://"):
            continue
        url, expected = line.strip().rsplit("#", 1)
        name = unquote(urlsplit(url).path.rsplit("/", 1)[1])
        if name in locked or len(expected) != 64:
            raise ValueError(f"Invalid lock record: {name}")
        locked[name] = (url, expected)
    locked[PACKAGE] = (RELEASE + PACKAGE, PACKAGE_SHA)
    return locked


def package_notices(prefix, cache, record, target):
    folder = target / record["name"]
    files = []
    license_dir = cache / "info/licenses"
    if license_dir.is_dir():
        for item in sorted(license_dir.rglob("*")):
            if item.is_file() and item.resolve().is_relative_to(cache.resolve()):
                destination = folder / item.relative_to(license_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
                files.append(destination.relative_to(target).as_posix())
    if not files:
        for relative in record.get("files", []):
            item = prefix / relative
            leaf = item.name.lower()
            if (leaf.startswith(("license", "licence", "copyright", "copying", "notice"))
                    and item.is_file() and item.stat().st_size <= 2_000_000
                    and item.suffix.lower() not in (".py", ".pyc", ".dll", ".pyd")
                    and item.resolve().is_relative_to(prefix.resolve())):
                destination = folder / "installed" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)
                files.append(destination.relative_to(target).as_posix())
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packer-path", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.packer_path.resolve()))
    import conda_pack
    from conda_pack.core import File

    if conda_pack.__version__ != "0.9.2":
        raise ValueError("The build requires conda-pack 0.9.2")
    prefix = args.prefix.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    locked = locked_urls(args.lock)
    records = {r["fn"]: r for r in map(json_file, (prefix / "conda-meta").glob("*.json"))}
    if records.keys() != locked.keys():
        raise ValueError(f"Installed packages differ from lock: {records.keys() ^ locked.keys()}")
    env = conda_pack.CondaEnv.from_prefix(str(prefix))
    inventory = []
    notices = output / "licenses"
    with tempfile.TemporaryDirectory(prefix="incfinder-pack-") as temp:
        clean = Path(temp)
        members = []
        for item in env.files:
            target = PurePosixPath(item.target.replace("\\", "/"))
            if not item.is_conda:
                continue
            if target.parts[:1] != ("conda-meta",):
                members.append(item)
                continue
            if target.suffix != ".json" or target.name == "state":
                continue
            record = json_file(Path(item.source))
            name = record["fn"]
            url, expected = locked[name]
            cache = Path(record["link"]["source"])
            archive = Path(record.get("package_tarball_full_path") or cache.parent / name)
            if archive.is_file():
                verified = sha(archive)
            else:
                verified = record.get("sha256")
            if verified != expected:
                raise ValueError(f"Conda package digest differs: {name}")
            about_file = cache / "info/about.json"
            about = json_file(about_file) if about_file.is_file() else {}
            notices_included = package_notices(prefix, cache, record, notices)
            inventory.append({"name": record["name"], "version": record["version"], "build": record["build"],
                              "filename": name, "sha256": expected, "url": url,
                              "license": record.get("license") or about.get("license") or "See package notices",
                              "upstream": about.get("home") or about.get("dev_url") or url,
                              "notice_files": notices_included})
            for field in ("extracted_package_dir", "package_tarball_full_path", "requested_spec"):
                record.pop(field, None)
            record.update(link=None, url=url, sha256=expected,
                          channel=url.rsplit("/", 2)[0] if name != PACKAGE else RELEASE.rstrip("/"))
            replacement = clean / target.name
            replacement.write_text(json.dumps(record, ensure_ascii=True), encoding="utf8")
            members.append(File(str(replacement), item.target, is_conda=True, file_mode=None))
        if len(inventory) != len(locked):
            raise ValueError("Conda provenance records were lost when packing")
        archive_format = "zip" if os.name == "nt" else "tar"
        archive = output / ("runtime." + archive_format)
        pending = archive.with_name(archive.name + ".building")
        filtered = conda_pack.CondaEnv(str(prefix), members)
        filtered.pack(output=str(pending), format=archive_format, arcroot="runtime",
                      compress_level=6, force=True, verbose=False)
        pending.replace(archive)
    result = {"platform": "win-64" if os.name == "nt" else "linux-64",
              "lock_sha256": sha(args.lock), "packer": "conda-pack 0.9.2",
              "runtime_sha256": sha(archive), "archive_bytes": archive.stat().st_size,
              "packages": sorted(inventory, key=lambda row: row["name"])}
    (output / "runtime-inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf8")
    print(result["platform"], len(inventory), archive.stat().st_size, result["runtime_sha256"], flush=True)


if __name__ == "__main__":
    main()
