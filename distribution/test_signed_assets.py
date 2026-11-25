"""Test tamper detection against built candidate bytes without modifying any release asset."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from release import CFG, TRUST, signed_sums, verify


def test(output):
    release = output / CFG["tag"]
    signer = TRUST / "dist-allowed_signers"
    signed_sums(release / "SHA256SUMS", signer)
    system = "windows" if os.name == "nt" else "linux"
    platform = system
    if system == "linux" and "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower():
        platform = "wsl2"
    binary = CFG["launchers"][system]
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        corrupt = temp / "altered"
        corrupt.mkdir()
        for name in ("SHA256SUMS", "SHA256SUMS.sig", binary):
            shutil.copyfile(release / name, corrupt / name)
        with (corrupt / binary).open("ab") as stream:
            stream.write(b"x")
        probe = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("acceptance.py")),
                "run",
                "--platform",
                platform,
                "--work",
                str(temp / "altered-launcher"),
                "--assets",
                str(corrupt),
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 or "Launcher digest mismatch" not in probe.stderr:
            raise AssertionError("Modified launcher was not identified: " + probe.stderr)
        sums = signed_sums(release / "SHA256SUMS", signer)
        name = CFG["packages"][platform]
        if name not in sums and platform == "wsl2":
            name = CFG["packages"]["linux"]
        (corrupt / name).write_bytes(b"modified archive")
        env = dict(
            os.environ,
            INCFINDER_START_HOME=str(temp / "start"),
            INCFINDER_START_ASSETS=str(corrupt),
            INCFINDER_OFFLINE="1",
        )
        probe = subprocess.run(
            [str(release / binary), "download"], env=env, capture_output=True, text=True, encoding="utf8"
        )
        if probe.returncode == 0 or "Offline package SHA-256 mismatch" not in probe.stderr:
            raise AssertionError("Modified portable package was not identified: " + probe.stderr)
        for source in (
            output / CFG["rf_tag"] / "rf-index.json",
            output / "internal/RUNTIME-SHA256SUMS",
            release / "SHA256SUMS",
        ):
            sig = Path(str(source) + ".sig")
            try:
                verify(source.read_bytes() + b"x", sig, signer)
            except subprocess.CalledProcessError:
                continue
            raise AssertionError("Modified signed file accepted: " + source.name)
    print("Tampering rejected: launcher, archive, RF index, inner manifest, outer manifest.")


if __name__ == "__main__":
    test(Path(sys.argv[1]).resolve())
