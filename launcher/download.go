package main

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

func (a app) archive() string {
	if a.tag == dist.Tag {
		target := a.target
		if target == "wsl2" && !strings.Contains(string(a.inner()), "  "+dist.Packages["wsl2"]+"\n") {
			target = "linux"
		}
		return dist.Packages[target]
	}
	if a.isp() {
		return map[string]string{"windows": "IncFinder-Windows-x64.zip", "linux": "IncFinder-Linux-x64.tar.gz", "wsl2": "IncFinder-WSL2-x64.tar.gz", "docker": "IncFinder-Docker-amd64.zip"}[a.target]
	}
	if a.target == "windows" {
		return "incfinder-0.5.0rc2-win-64.zip"
	}
	return "incfinder-0.5.0rc2-linux-64.tar.gz"
}
func (a app) software() string {
	root := join(a.root, "software")
	if a.isp() {
		return root
	}
	return join(root, "incfinder-0.5.0rc2")
}
func (a app) checksum() string {
	if a.tag == dist.Tag {
		return manifestEntry(a.inner(), a.archive())
	}
	kind := "rc2"
	if a.isp() {
		kind = "portable"
	}
	return a.signedDigest(kind, a.archive())
}

func (a app) signedDigest(kind, filename string) string {
	expected := map[string]string{
		"rc2":      "cc80bd39c4e9a7eb1d0ba223c37b72786b625e322e2e27f3013e08ab7662c20e",
		"portable": "aca9717965b9bbbc5a64e984140979f78baf941c8590e2a436f0f67934153350",
	}[kind]
	sum := must(resources.ReadFile("trust/" + kind + "-SHA256SUMS"))
	if fmt.Sprintf("%x", sha256.Sum256(sum)) != expected {
		stop("Embedded manifest mismatch")
	}
	key := materialize(join(a.root, "trust"), "trust/allowed_signers")
	sig := materialize(join(a.root, "trust"), "trust/"+kind+"-SHA256SUMS.sig")
	verifySignature(key, sig, sum)
	return manifestEntry(sum, filename)
}
func (a app) getArchive(hash string) string {
	name := a.archive()
	if root := os.Getenv("INCFINDER_START_ASSETS"); root != "" {
		for _, candidate := range []string{join(root, a.tag, name), join(root, name)} {
			if exists(candidate) {
				if digest(candidate) != hash {
					stop("Offline package SHA-256 mismatch: %s", candidate)
				}
				return abs(candidate)
			}
		}
		stop("Selected archive missing from offline directory: %s", root)
	}
	cache := join(a.root, "downloads")
	mkdir(cache)
	f := join(cache, name)
	if exists(f) {
		if digest(f) != hash {
			stop("Cached archive failed SHA-256: %s", f)
		}
		return f
	}
	if os.Getenv("INCFINDER_OFFLINE") == "1" {
		stop("Offline archive not available: %s", f)
	}
	url := repository + "/releases/download/" + a.tag + "/" + name
	part := f + ".part"
	curl := "curl"
	if runtime.GOOS == "windows" {
		curl = "curl.exe"
	}
	if exists(part) && digest(part) == hash {
		check(os.Rename(part, f))
		return f
	}
	args := []string{"--fail", "--location", "--show-error", "--retry", "3", "--connect-timeout", "20", "--output", part}
	tool := executable(curl)
	if exists(part) {
		resume := launch("", nil, tool, append(append([]string{}, args...), "--continue-at", "-", url)...)
		resume.Stdout, resume.Stderr = os.Stdout, os.Stderr
		if err := resume.Run(); err != nil {
			failed, ok := err.(*exec.ExitError)
			if !ok || (failed.ExitCode() != 33 && failed.ExitCode() != 22) {
				check(err)
			}
			// The server may not support the requested range. A fresh GET replaces just our partial file.
			execute(launch("", nil, tool, append(args, url)...))
		}
	} else {
		execute(launch("", nil, tool, append(args, url)...))
	}
	if digest(part) != hash {
		check(os.Remove(part))
		stop("Incomplete or incorrect archive; retry download / 请重新下载")
	}
	check(os.Rename(part, f))
	return f
}
func (a app) download(recheck bool) string {
	unlock := lock(join(a.root, "download.lock"))
	defer unlock()
	hash := a.checksum()
	root := join(a.root, "software")
	marker := join(root, ".incfinder-start-archive")
	if exists(root) {
		if !exists(marker) || strings.TrimSpace(string(bytesAt(marker))) != hash || !exists(join(a.software(), "examples", "sample.fasta")) {
			stop("Installed directory is incomplete or belongs to a different package: %s", root)
		}
		if recheck {
			a.getArchive(hash)
		}
		return a.software()
	}
	f := a.getArchive(hash)
	stage := must(os.MkdirTemp(a.root, "unpack-"))
	defer os.RemoveAll(stage)
	tar := "tar"
	if runtime.GOOS == "windows" {
		tar = "tar.exe"
	}
	fmt.Println("Extracting / 正在解压", filepath.Base(f))
	if runtime.GOOS == "linux" && strings.HasSuffix(f, ".zip") {
		extractPortableZIP(f, stage)
	} else {
		execute(launch("", nil, executable(tar), "-xf", f, "-C", stage))
	}
	payload := stage
	if !a.isp() {
		payload = join(stage, "incfinder-0.5.0rc2")
	}
	if !exists(join(payload, "examples", "sample.fasta")) {
		stop("Unexpected archive layout")
	}
	check(os.WriteFile(join(stage, ".incfinder-start-archive"), []byte(hash+"\n"), 0600))
	publishDirectory(stage, root)
	return a.software()
}

// Windows virus scanners can keep a newly extracted executable open briefly.
func publishDirectory(stage, target string) {
	deadline := time.Now().Add(10 * time.Second)
	for {
		err := os.Rename(stage, target)
		if err == nil {
			return
		}
		sharing := errors.Is(err, os.ErrPermission) || errors.Is(err, syscall.Errno(32)) || errors.Is(err, syscall.Errno(33))
		if runtime.GOOS != "windows" || !sharing || exists(target) || time.Now().After(deadline) {
			check(err)
		}
		time.Sleep(200 * time.Millisecond)
	}
}
