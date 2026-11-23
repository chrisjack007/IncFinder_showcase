"""Relocate the bundled Conda environment without requiring host Python or Conda."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

HERE = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"


def data_home():
    override = os.environ.get("INCFINDER_PORTABLE_HOME")
    if override:
        return Path(override).expanduser().absolute()
    if WINDOWS:
        return Path(os.environ["LOCALAPPDATA"]) / "IncFinder"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "incfinder"


@contextmanager
def install_lock(path):
    with path.open("a+b") as handle:
        if WINDOWS:
            import msvcrt
            if not path.stat().st_size:
                handle.write(b"0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (11, 13, 35):
                        raise
                    time.sleep(0.3)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def filesystem_path(path):
    value = str(path.resolve())
    if not WINDOWS or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def runtime_env(prefix):
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    bins = ([prefix, prefix / "Library/bin", prefix / "Scripts"] if WINDOWS else [prefix / "bin"])
    env.update(PATH=os.pathsep.join(map(str, bins)) + os.pathsep + env.get("PATH", ""),
               CONDA_PREFIX=str(prefix), CONDA_DEFAULT_ENV="IncFinder_Test1",
               PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    if WINDOWS:
        for var, name in (("TCL_LIBRARY", "tcl"), ("TK_LIBRARY", "tk")):
            library = next((prefix / "Library/lib").glob(name + "8.*"), None)
            if library:
                env[var] = str(library)
    return env


def main():
    info = json.loads((HERE / "bundle.json").read_text(encoding="utf8"))
    source = HERE.parent / "runtime"
    root = data_home() / "runtimes"
    root.mkdir(parents=True, exist_ok=True)
    key = info["runtime_sha256"]
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid runtime identity")
    prefix = root / (info["runtime_platform"] + "-" + key[:16])
    if prefix.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(prefix.resolve()):
        raise ValueError("Select a data directory outside the extracted runtime.")
    dest_python = prefix / ("python.exe" if WINDOWS else "bin/python")
    marker = prefix / ".incfinder-relocated"
    env = runtime_env(prefix)
    with install_lock(root / (prefix.name + ".lock")):
        if not marker.is_file() or marker.read_text(encoding="utf8") != str(prefix.resolve()) + "\n" + key:
            print("Preparing the bundled runtime in " + str(prefix), flush=True)
            if prefix.is_symlink() or (prefix.exists() and not prefix.is_dir()):
                raise ValueError("Invalid managed runtime directory")
            required = int(info["runtime_unpacked_bytes"])
            if not prefix.exists() and shutil.disk_usage(root).free < required + 64 * 1024**2:
                raise OSError(f"Insufficient space for the runtime: {required:,} bytes required in {root}")
            if not WINDOWS and prefix.exists():
                prefix.rename(root / (prefix.name + ".incomplete-" + str(time.time_ns())))
            # Recover interrupted relocation by restoring the pristine files at their final address.
            shutil.copytree(filesystem_path(source), filesystem_path(prefix), symlinks=True, dirs_exist_ok=True)
            candidates = ([prefix / "Scripts/conda-unpack-script.py", prefix / "Scripts/conda-unpack"]
                          if WINDOWS else [prefix / "bin/conda-unpack"])
            unpack = next((path for path in candidates if path.is_file()), None)
            if unpack is None:
                raise FileNotFoundError("conda-unpack is missing from the distribution.")
            subprocess.run([str(dest_python), "-E", "-s", "-B", str(unpack)], env=env, check=True)
            marker.write_text(str(prefix.resolve()) + "\n" + key, encoding="utf8")
    command = [str(dest_python), "-I", "-B", "-X", "utf8", str(HERE / "app.py"), *sys.argv[1:]]
    return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit("IncFinder: " + str(error))
