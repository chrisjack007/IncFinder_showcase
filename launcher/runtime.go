package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

type engine struct {
	a                       app
	software, prefix, image string
}
type bundle struct {
	RuntimePlatform string `json:"runtime_platform"`
	RuntimeSHA      string `json:"runtime_sha256"`
	Distribution    string `json:"distribution"`
	Platform        string `json:"platform"`
	PackageSHA      string `json:"scientific_conda_sha256"`
	Tag             string `json:"repo_tag"`
	Index           string `json:"image_index"`
	Manifest        string `json:"linux_amd64_manifest"`
	Config          string `json:"image_config"`
	ArchiveSHA      string `json:"sha256"`
}

func pythonAt(prefix string) string {
	if runtime.GOOS == "windows" {
		return join(prefix, "python.exe")
	}
	return join(prefix, "bin", "python")
}
func nativeEnv(prefix string) []string {
	dirs := []string{join(prefix, "bin")}
	if runtime.GOOS == "windows" {
		dirs = []string{prefix, join(prefix, "Library", "bin"), join(prefix, "Scripts")}
	}
	return []string{"PATH=" + strings.Join(dirs, string(os.PathListSeparator)) + string(os.PathListSeparator) + os.Getenv("PATH"), "CONDA_PREFIX=" + prefix, "CONDA_DEFAULT_ENV=IncFinder_Test1", "PYTHONHOME=", "PYTHONPATH=", "PYTHONNOUSERSITE=1", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUTF8=1", "TOKENIZERS_PARALLELISM=false"}
}
func installed(prefix string) bool {
	records := must(filepath.Glob(join(prefix, "conda-meta", "incfinder-*.json")))
	if len(records) == 0 {
		return false
	}
	if len(records) != 1 {
		stop("Conflicting IncFinder packages in %s", prefix)
	}
	var rec struct{ Sha256, Fn string }
	readJSON(records[0], &rec)
	if rec.Sha256 != packageSHA || rec.Fn != "incfinder-0.5.0rc2-py_0.conda" {
		stop("An incompatible IncFinder package is installed in %s", prefix)
	}
	return exists(pythonAt(prefix))
}
func findConda() string {
	if p := os.Getenv("CONDA_EXE"); p != "" && exists(p) {
		return p
	}
	for _, n := range []string{"conda.exe", "conda"} {
		if p, err := exec.LookPath(n); err == nil {
			return p
		}
	}
	for _, name := range []string{"miniforge3", "miniconda3", "anaconda3"} {
		root := join(must(os.UserHomeDir()), name)
		for _, p := range []string{join(root, "bin", "conda"), join(root, "Scripts", "conda.exe")} {
			if exists(p) {
				return p
			}
		}
	}
	stop("Software download retained. Install Conda first, then restart: https://github.com/conda-forge/miniforge / 请先安装 Conda 后重新打开")
	return ""
}
func portableHome() string {
	if dir := os.Getenv("INCFINDER_PORTABLE_HOME"); dir != "" {
		return abs(dir)
	}
	if runtime.GOOS == "windows" {
		return join(envOr("LOCALAPPDATA", must(os.UserHomeDir())), "IncFinder")
	}
	return join(envOr("XDG_DATA_HOME", join(must(os.UserHomeDir()), ".local", "share")), "incfinder")
}
func (a app) prepare() engine {
	base := a.download(false)
	unlock := lock(join(a.root, "install.lock"))
	defer unlock()
	e := engine{a: a, software: base}
	var b bundle
	if a.isp() {
		readJSON(join(base, "portable", "bundle.json"), &b)
		if b.Distribution != a.tag || b.Platform != a.target || b.PackageSHA != packageSHA {
			stop("Portable package identity mismatch")
		}
	}
	if a.docker() {
		e.prepareImage(b)
		return e
	}
	if a.isp() {
		if len(b.RuntimeSHA) != 64 {
			stop("Invalid bundled runtime identity")
		}
		e.prefix = join(portableHome(), "runtimes", b.RuntimePlatform+"-"+b.RuntimeSHA[:16])
		marker := join(e.prefix, ".incfinder-relocated")
		if !exists(marker) || !exists(pythonAt(e.prefix)) || strings.TrimSpace(string(bytesAt(marker))) != e.prefix+"\n"+b.RuntimeSHA {
			var c *exec.Cmd
			if runtime.GOOS == "windows" {
				rv := join(base, "runtime")
				c = launch(base, nativeEnv(rv), pythonAt(rv), "-I", "-B", "-X", "utf8", join(base, "portable", "bootstrap.py"), "version")
			} else {
				c = launch(base, nil, join(base, "incfinder"), "version")
			}
			execute(c)
		}
		if !installed(e.prefix) {
			stop("Portable runtime identity verification failed: %s", e.prefix)
		}
		return e
	}
	override := os.Getenv("INCFINDER_START_CONDA_PREFIX")
	e.prefix = join(a.root, "envs", "IncFinder_Test1")
	if override != "" {
		e.prefix = abs(override)
	}
	if installed(e.prefix) {
		return e
	}
	if override != "" {
		stop("Selected Conda prefix does not contain the signed IncFinder rc2 package")
	}
	conda := findConda()
	file := "linux-64.lock"
	if runtime.GOOS == "windows" {
		file = "win-64.lock"
	}
	action := "create"
	if exists(join(e.prefix, "conda-meta", "history")) {
		action = "install"
	}
	fmt.Println("Installing pinned Conda environment / 正在安装锁定环境")
	execute(launch(base, []string{"CONDA_OVERRIDE_CUDA="}, conda, action, "--yes", "--override-channels", "-c", "conda-forge", "--prefix", e.prefix, "--file", join(base, "environments", file)))
	artifact := join(base, "dist", "channel", "noarch", "incfinder-0.5.0rc2-py_0.conda")
	if digest(artifact) != packageSHA {
		stop("Scientific Conda archive SHA-256 mismatch")
	}
	execute(launch(base, nil, conda, "install", "--yes", "--offline", "--override-channels", "-c", "conda-forge", "--prefix", e.prefix, artifact))
	if !installed(e.prefix) {
		stop("Conda installation identity mismatch")
	}
	return e
}
func (e engine) model(s settings) string {
	if e.a.docker() {
		return "/store/models"
	}
	if s.Models != "" {
		return abs(s.Models)
	}
	return join(e.a.home, "models", "0.5.0rc2")
}
func (e engine) env(online bool) []string {
	off := "1"
	if online {
		off = "0"
	}
	return append(nativeEnv(e.prefix), "HF_HUB_OFFLINE="+off, "TRANSFORMERS_OFFLINE="+off, "CUDA_VISIBLE_DEVICES=-1",
		"INCFINDER_RUN_DIR="+join(e.a.root, "run-state"))
}
func (e engine) nativePython(online bool, args ...string) *exec.Cmd {
	return launch(e.software, e.env(online), pythonAt(e.prefix), append([]string{"-I", "-B", "-X", "utf8"}, args...)...)
}
func (e engine) science(s settings, args ...string) *exec.Cmd {
	if e.a.docker() {
		return e.dockerPython(s, false, false, nil, append([]string{"-m", "incfinder"}, args...)...)
	}
	return e.nativePython(false, append([]string{"-m", "incfinder"}, args...)...)
}
func (e engine) prepareModels(s settings) {
	target := e.model(s)
	ready := false
	if e.a.docker() && s.Models == "" {
		mounts := e.dockerMounts(s, false)
		// Docker creates the named model volume before the probe.
		args := append([]string{"run", "--rm", "--network", "none", "--entrypoint", containerPython}, mounts...)
		args = append(args, e.image, "-c", "from pathlib import Path; p=Path('/store/models'); print(int((p/'rf/stage1.joblib').is_file() and (p/'rf/stage2.joblib').is_file() and (p/'ntv3/model.safetensors').is_file()))")
		ready = output(launch("", nil, "docker", args...)) == "1"
	} else {
		p := target
		if e.a.docker() {
			p = abs(s.Models)
		}
		ready = modelFiles(p, false)
	}
	if ready {
		return
	}
	if s.Models != "" {
		stop("Selected model cache is incomplete. Select a complete official cache or use 'config models default'.")
	}
	fmt.Println("27.36 GB official model files; allow 60 GB free / 首次模型准备建议预留 60 GB。\nRequest access / 官方授权: https://huggingface.co/InstaDeepAI/NTv3_650M_pre\nEnter the token only in the official login prompt / 只在官方登录提示中输入令牌。")
	if e.a.isp() {
		args := []string{join(e.software, "portable", "app.py"), "models", "init", "--model-dir", target}
		if e.a.docker() {
			execute(e.dockerPython(s, true, true, nil, "/portable/app.py", "models", "init", "--model-dir", target))
		} else {
			execute(e.nativePython(true, args...))
		}
		return
	}
	args := []string{"-m", "incfinder", "models", "install", "--model-dir", target, "--download-rf", "--download-ntv3"}
	if e.a.docker() {
		if e.dockerPython(s, true, false, nil, "-c", "from huggingface_hub import get_token; import sys; sys.exit(get_token() is None)").Run() != nil {
			execute(e.dockerPython(s, true, true, nil, "-m", "huggingface_hub.cli.hf", "auth", "login"))
		}
		execute(e.dockerPython(s, true, false, nil, args...))
	} else {
		if e.nativePython(true, "-c", "from huggingface_hub import get_token; import sys; sys.exit(get_token() is None)").Run() != nil {
			execute(e.nativePython(true, "-m", "huggingface_hub.cli.hf", "auth", "login"))
		}
		execute(e.nativePython(true, args...))
	}
}
