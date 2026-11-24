package main

import (
	"encoding/csv"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const containerPython = "/opt/conda/envs/IncFinder_Test1/bin/python"

func docker(args ...string) *exec.Cmd { return launch("", nil, executable("docker"), args...) }
func inspectTag(tag string) string {
	b, err := docker("image", "inspect", "--format", "{{.Id}}", tag).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}
func (e *engine) prepareImage(b bundle) {
	if output(docker("version", "--format", "{{.Server.Os}}")) != "linux" {
		stop("Start a Linux Docker engine / 请启用 Linux 容器")
	}
	if e.a.isp() {
		want := func(id string) bool { return id != "" && (id == b.Index || id == b.Manifest || id == b.Config) }
		if b.Tag == "" || len(b.ArchiveSHA) != 64 {
			stop("Invalid portable Docker image manifest")
		}
		id := inspectTag(b.Tag)
		if id == "" {
			archive := join(e.software, "image.tar")
			if digest(archive) != b.ArchiveSHA {
				stop("Portable image SHA-256 mismatch")
			}
			execute(docker("load", "--input", archive))
			id = inspectTag(b.Tag)
		}
		if !want(id) {
			stop("Existing Docker image differs from the signed portable bundle: %s", b.Tag)
		}
		e.image = id
		return
	}
	receipt := join(e.a.root, "image-id")
	tag := "incfinder-start:0.5.0rc2"
	if exists(receipt) {
		saved := strings.TrimSpace(string(bytesAt(receipt)))
		if strings.HasPrefix(saved, "sha256:") && inspectTag(saved) == saved {
			e.image = saved
			return
		}
	}
	id := inspectTag(tag)
	// Use a temporary tag if an unrelated installation already owns the default tag.
	buildTag := tag
	if id != "" {
		buildTag = "incfinder-start:rc2-" + unique()
	}
	execute(docker("build", "--platform", "linux/amd64", "-t", buildTag, e.software))
	id = inspectTag(buildTag)
	if id == "" {
		stop("Could not identify built Docker image")
	}
	atomicWrite(receipt, []byte(id+"\n"))
	e.image = id
}
func mount(kind, source, target string, readOnly bool) string {
	fields := []string{"type=" + kind}
	if source != "" {
		fields = append(fields, "source="+source)
	}
	fields = append(fields, "target="+target)
	if readOnly {
		fields = append(fields, "readonly")
	}
	var b strings.Builder
	w := csv.NewWriter(&b)
	check(w.Write(fields))
	w.Flush()
	check(w.Error())
	return strings.TrimSuffix(b.String(), "\n")
}
func (e engine) dockerMounts(s settings, online bool) []string {
	args := []string{"--platform", "linux/amd64"}
	if s.Models != "" {
		args = append(args, "--mount", mount("bind", abs(s.Models), "/store/models", true))
	} else {
		args = append(args, "--mount", mount("volume", envOr("INCFINDER_START_MODEL_VOLUME", "incfinder-rc2-model-store"), "/store", !online))
	}
	if e.a.isp() {
		args = append(args, "--mount", mount("bind", join(e.software, "portable"), "/portable", true), "-e", "INCFINDER_PORTABLE_HOME=/store")
	}
	if online {
		args = append(args, "--mount", mount("volume", "incfinder-start-rc2-auth", "/auth", false), "-e", "HF_HOME=/auth", "-e", "HF_HUB_OFFLINE=0", "-e", "TRANSFORMERS_OFFLINE=0")
	} else {
		args = append(args, "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1")
	}
	return append(args, "-e", "INCFINDER_MODEL_DIR=/store/models", "-e", "CONDA_PREFIX=/opt/conda/envs/IncFinder_Test1", "-e", "PATH=/opt/conda/envs/IncFinder_Test1/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin", "-e", "CUDA_VISIBLE_DEVICES=-1", "-e", "PYTHONUTF8=1", "-e", "TOKENIZERS_PARALLELISM=false")
}
func (e engine) dockerArgs(s settings, online, interactive bool, extra []string) []string {
	args := []string{}
	if !online {
		args = append(args, "--network", "none")
	}
	if interactive {
		args = append(args, "-i")
		fi, err := os.Stdin.Stat()
		if err == nil && fi.Mode()&os.ModeCharDevice != 0 {
			args = append(args, "-t")
		}
	}
	args = append(args, e.dockerMounts(s, online)...)
	args = append(args, extra...)
	return append(args, "--entrypoint", containerPython, e.image, "-I", "-B", "-X", "utf8")
}
func (e engine) dockerPython(s settings, online, interactive bool, extra []string, args ...string) *exec.Cmd {
	opt := append([]string{"run", "--rm"}, e.dockerArgs(s, online, interactive, extra)...)
	return docker(append(opt, args...)...)
}

// Use an anonymous Linux volume for the engine's atomic output operations, including on Docker Desktop.
func (e engine) export(s settings, extra, args []string, dest string) {
	parent := filepath.Dir(dest)
	mkdir(parent)
	extra = append(extra, "--mount", mount("volume", "", "/results", false))
	opts := append([]string{"create"}, e.dockerArgs(s, false, false, extra)...)
	id := output(docker(append(opts, args...)...))
	defer func() {
		if err := docker("rm", "-v", id).Run(); err != nil {
			fmt.Fprintln(os.Stderr, "Could not remove the completed container:", id)
		}
	}()
	execute(docker("start", "-a", id))
	if output(docker("inspect", "--format", "{{.State.ExitCode}}", id)) != "0" {
		stop("Docker analysis failed")
	}
	stage := must(os.MkdirTemp(parent, ".incfinder-copy-"))
	defer os.RemoveAll(stage)
	tmp := join(stage, "out")
	execute(docker("cp", id+":/results/out", tmp))
	if exists(dest) {
		stop("Output directory exists: %s", dest)
	}
	check(os.Rename(tmp, dest))
}
