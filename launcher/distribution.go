package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Set by the release builder only after the inner manifest is signed.
var distManifest, distSignature string
var dist struct {
	Tag, Engine, Repository, Evidence string
	Packages, Launchers               map[string]string
}

func init() {
	check(json.Unmarshal(must(resources.ReadFile("distribution.json")), &dist))
}

func verifySignature(key, sig string, body []byte) {
	cmd := exec.Command(executable("ssh-keygen"), "-Y", "verify", "-f", key, "-I", "incfinder-release", "-n", "incfinder-release", "-s", sig)
	cmd.Stdin, cmd.Stderr = bytes.NewReader(body), os.Stderr
	if err := cmd.Run(); err != nil {
		stop("Release signature failed / 发行签名无效: %v", err)
	}
}

func manifestEntry(body []byte, filename string) string {
	found := ""
	for _, line := range strings.Split(string(body), "\n") {
		parts := strings.Fields(line)
		if len(parts) == 2 && strings.TrimPrefix(parts[1], "*") == filename {
			if found != "" {
				stop("Duplicate signed entry: %s", filename)
			}
			found = parts[0]
		}
	}
	if len(found) != 64 || strings.Trim(found, "0123456789abcdef") != "" {
		stop("Invalid or missing signed entry: %s", filename)
	}
	return found
}

func (a app) inner() []byte {
	if distManifest == "" || distSignature == "" {
		stop("Distribution manifest missing. Use a signed release launcher.")
	}
	manifest := must(base64.StdEncoding.DecodeString(distManifest))
	signature := must(base64.StdEncoding.DecodeString(distSignature))
	trust := join(a.root, "trust")
	key := materialize(trust, "trust/dist-allowed_signers")
	sig := join(trust, "distribution.sig")
	atomicWrite(sig, signature)
	verifySignature(key, sig, manifest)
	return manifest
}

func (a app) checkModelIndex(base string) {
	trust := join(base, "portable", "trust")
	index := bytesAt(join(trust, "rf-index.json"))
	if fmt.Sprintf("%x", sha256.Sum256(index)) != manifestEntry(a.inner(), "rf-index.json") {
		stop("RF index SHA-256 mismatch")
	}
	key := materialize(join(a.root, "trust"), "trust/dist-allowed_signers")
	verifySignature(key, join(trust, "rf-index.json.sig"), index)
}
