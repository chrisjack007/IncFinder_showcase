"""Assemble one portable distribution from the signed science package and vetted artifacts."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import zipfile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
TAG = "v0.5.0rc2-portable.1"
NAMES = {"windows": "IncFinder-Windows-x64.zip", "wsl2": "IncFinder-WSL2-x64.tar.gz",
         "docker": "IncFinder-Docker-amd64.zip", "linux": "IncFinder-Linux-x64.tar.gz"}
LABELS = {"windows": "Windows x64", "wsl2": "WSL2 x64", "docker": "Docker amd64", "linux": "Linux x64"}
RELEASE = "https://github.com/chrisjack007/IncFinder_showcase"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def jbytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf8")


def readme(platform):
    entry = "IncFinder.cmd" if platform == "windows" else "./incfinder"
    line = {"windows": "双击 IncFinder.cmd 选择序列，或把文件拖到 IncFinder.cmd 上。在命令提示符中也可运行以下命令。",
            "wsl2": "在 WSL2 的 Linux 主目录内解压。请使用发行版的 Linux 文件系统保存结果。",
            "linux": "在 x86-64 Linux 上解压，保留目录结构和执行权限。",
            "docker": "启动 Docker Engine 或 Docker Desktop 的 Linux 容器模式。Windows 运行 IncFinder.cmd，Linux 运行 ./incfinder。"}[platform]
    return f"""# IncFinder · {LABELS[platform]}

{TAG} · inference software 0.5.0rc2

{line}

    {entry} --help
    {entry} version
    {entry} examples/sample.fasta
    {entry} examples/sample.fastq --threshold-mode u05 --output run-u05
    {entry} lineage run-u05 --row 0

首次分析前，使用自己的 Hugging Face 账号申请以下两个项目的访问权。启动器通过官方客户端登录，
下载并校验约 27.36 GB 模型。网络中断后重跑同一条命令可以续传。首次安装建议在模型所在磁盘预留
至少 60 GB 空间，另为结果留足空间。后续分析可离线运行。帮助和版本查询可以离线使用。
快捷分型默认使用 ct 和科研预览编码参数。分型表为 inc_results.csv，概率表和 run_summary.json 与它同目录。
训练序列长度为 200–10,000 bp。第一层拒绝时第二层概率为 NA。两种模式对 Inc18 均检出 1/326，
阴性结果不足以排除 Inc18。

Extract the complete archive, open a terminal in its directory and run the commands above.
Use an approved Hugging Face account for the first analysis. Keep the model data for offline use.
Desktop Docker users need an existing Linux container engine. No host Python or Conda installation is needed.
The Docker launcher loads its bundled image and runs prediction with networking disabled.
Native downloads and the relocated runtime use the current user's data directory. Set INCFINDER_PORTABLE_HOME to
choose another native data directory before first use. Set INCFINDER_MODEL_DIR to reuse an existing verified model
directory. The Docker edition uses its own persistent volume. Use --output to choose a new results directory.

Official upstream access and release verification

- https://huggingface.co/InstaDeepAI/NTv3_650M_pre
- https://huggingface.co/InstaDeepAI/ntv3_base_model
- {RELEASE}/tree/{TAG}/portable/{TAG}
""".encode("utf8")


def assemble(args):
    platform = args.platform
    package = NAMES[platform]
    args.out.mkdir(exist_ok=True, parents=True)
    args.verification.mkdir(exist_ok=True, parents=True)
    files = {}
    def add(name, item):
        if name in files:
            raise ValueError("Duplicate package member " + name)
        files[name] = item
    for name in ("app.py", "models.py", "bootstrap.py"):
        add("portable/" + name, HERE / name)
    for f in sorted((args.stage / "trust").iterdir()):
        add("portable/trust/" + f.name, f)
    for name in ("sample.fasta", "sample.fastq"):
        add("examples/" + name, PROJECT / "examples" / name)
    for name in ("MODEL_CARD.zh-CN.md", "MODEL_CARD.en.md", "per_class_metrics.tsv", "example-reference.json",
                 "release-manifest.json", "MODEL-LICENSE.md"):
        add("docs/" + name, args.legacy / name)
    add("LICENSE", PROJECT / "LICENSE")
    add("README.md", readme(platform))
    runtime = "windows" if platform == "windows" else "linux"
    rt = args.stage / runtime
    inventory = read(rt / "runtime-inventory.json")
    previous = read(args.legacy / f"SBOM-{inventory['platform']}-conda.cdx.json")
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {"type": "application", "name": package, "version": TAG[1:]}},
            "components": previous["components"].copy()}
    records = {r["name"]: r for r in inventory["packages"]}
    for component in sbom["components"]:
        r = records.get(component["name"])
        if r:
            component["licenses"] = [{"license": {"name": r["license"]}}]
            component["externalReferences"] = [{"type": "distribution", "url": r["url"]}]
    if platform != "docker":
        sbom["components"].append({"type": "library", "name": "conda-pack relocation scripts", "version": "0.9.2",
                                   "licenses": [{"license": {"id": "BSD-3-Clause"}}]})
    else:
        sbom["components"].append({"type": "library", "name": "Go standard library",
                                   "version": read(args.stage / "docker/compiler.json")["version"],
                                   "licenses": [{"license": {"id": "BSD-3-Clause"}}]})
    for r in inventory["packages"]:
        for n in r["notice_files"]:
            add("licenses/conda/" + n, rt / "licenses" / n)
    add("licenses/conda-packages.json", jbytes(inventory["packages"]))
    for f in sorted((args.stage / "extra-licenses" / runtime).rglob("*")):
        if f.is_file():
            add("licenses/additional/" + f.relative_to(args.stage / "extra-licenses" / runtime).as_posix(), f)
    marker = {"distribution": TAG, "platform": platform, "scientific_version": "0.5.0rc2",
              "runtime_platform": inventory["platform"], "runtime_sha256": inventory["runtime_sha256"],
              "scientific_conda_sha256": next(r["sha256"] for r in inventory["packages"] if r["name"] == "incfinder")}
    if platform == "docker":
        image = read(args.stage / "docker/image.json")
        if sha(args.image) != image["sha256"]:
            raise ValueError("Image archive checksum mismatch")
        marker.update(image)
        for r in read(args.stage / "docker/base-components.json"):
            sbom["components"].append(r)
        sbom["components"].append({"type": "container", "name": image["repo_tag"], "version": image["image_index"],
            "hashes": [{"alg": "SHA-256", "content": image["sha256"]}]})
        for n in ("image-launch.exe", "incfinder", "IncFinder.cmd", "Go-LICENSE"):
            add(n if n != "Go-LICENSE" else "licenses/Go-LICENSE", args.stage / "docker" / n)
        add("image.tar", args.image)
    else:
        root_entry = "IncFinder.cmd" if platform == "windows" else "incfinder"
        add(root_entry, HERE / root_entry)
        if platform == "windows":
            with zipfile.ZipFile(rt / "runtime.zip") as archive:
                marker["runtime_unpacked_bytes"] = sum(z.file_size for z in archive.infolist())
        else:
            with tarfile.open(rt / "runtime.tar") as archive:
                marker["runtime_unpacked_bytes"] = sum(t.size for t in archive)
    sbom_name = "SBOM-" + platform + ".cdx.json"
    sbom_bytes = jbytes(sbom)
    (args.verification / sbom_name).write_bytes(sbom_bytes)
    add("docs/" + sbom_name, sbom_bytes)
    add("portable/bundle.json", jbytes(marker))
    path = args.out / package
    pending = path.with_suffix(path.suffix + ".building")
    if package.endswith(".zip"):
        if platform == "windows":
            shutil.copyfile(rt / "runtime.zip", pending)
        with zipfile.ZipFile(pending, "a" if platform == "windows" else "w", zipfile.ZIP_DEFLATED) as archive:
            for name, obj in sorted(files.items()):
                if name == "image.tar":
                    archive.write(obj, name, compress_type=zipfile.ZIP_STORED)
                    continue
                info = zipfile.ZipInfo(name, date_time=(2026, 11, 23, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name in ("incfinder", "image-launch.exe") else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, obj if isinstance(obj, bytes) else obj.read_bytes())
    else:
        # The runtime is already a complete POSIX tar, including links and executable bits.
        with tarfile.open(pending, "w:gz", compresslevel=3) as archive:
            with tarfile.open(rt / "runtime.tar") as source:
                for member in source:
                    archive.addfile(member, source.extractfile(member) if member.isfile() else None)
            for name, obj in sorted(files.items()):
                raw = obj if isinstance(obj, bytes) else obj.read_bytes()
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                info.mode = 0o755 if name == "incfinder" else 0o644
                archive.addfile(info, io.BytesIO(raw))
    if not 0 < pending.stat().st_size < 2 * 1024**3:
        raise ValueError("Archive exceeds the single-asset GitHub limit")
    os.replace(pending, path)
    print(package, path.stat().st_size, sha(path), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=NAMES)
    for arg in ("stage", "legacy", "out", "verification"):
        parser.add_argument("--" + arg, type=Path, required=True)
    parser.add_argument("--image", type=Path)
    assemble(parser.parse_args())
