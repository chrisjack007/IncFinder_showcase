// Hosts the bundled Linux image without requiring Python, Conda or a registry login.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const python = "/opt/conda/envs/IncFinder_Test1/bin/python"
const app = "/portable/app.py"

type imageInfo struct {
	Tag      string `json:"repo_tag"`
	Index    string `json:"image_index"`
	Manifest string `json:"linux_amd64_manifest"`
	Config   string `json:"image_config"`
	Hash     string `json:"sha256"`
}
type host struct {
	base, model, store string
	image              imageInfo
}

func command(args ...string) error {
	c := exec.Command("docker", args...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	c.Stdin = os.Stdin
	return c.Run()
}
func capture(args ...string) (string, error) {
	out, err := exec.Command("docker", args...).Output()
	return strings.TrimSpace(string(out)), err
}
func (h host) known(id string) bool {
	return id == h.image.Index || id == h.image.Manifest || id == h.image.Config
}
func (h host) load() error {
	daemon, err := capture("version", "--format", "{{.Server.Os}}")
	if err != nil || daemon != "linux" {
		return errors.New("start Docker Engine or Docker Desktop in Linux containers mode")
	}
	id, err := capture("image", "inspect", "--format", "{{.Id}}", h.image.Tag)
	if err == nil {
		if h.known(id) {
			return nil
		}
		return errors.New("a different local image already uses the bundled tag")
	}
	archive := filepath.Join(h.base, "image.tar")
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	sum := sha256.New()
	_, err = io.Copy(sum, f)
	f.Close()
	if err != nil {
		return err
	}
	if hex.EncodeToString(sum.Sum(nil)) != h.image.Hash {
		return errors.New("the bundled image.tar failed SHA-256 verification")
	}
	if err = command("image", "load", "--input", archive); err != nil {
		return err
	}
	id, err = capture("image", "inspect", "--format", "{{.Id}}", h.image.Tag)
	if err != nil || !h.known(id) {
		return errors.New("loaded image identity differs from the bundled image")
	}
	return nil
}
func mount(kind, source, dest string, ro bool) string {
	fields := []string{"type=" + kind, "dst=" + dest}
	if source != "" {
		fields = append(fields, "src="+source)
	}
	if ro {
		fields = append(fields, "readonly")
	}
	var b strings.Builder
	w := csv.NewWriter(&b)
	w.Write(fields)
	w.Flush()
	return strings.TrimSuffix(b.String(), "\n")
}
func (h host) opts(online bool) []string {
	args := []string{"--platform", "linux/amd64", "--pull", "never", "--entrypoint", python,
		"--mount", mount("bind", filepath.Join(h.base, "portable"), "/portable", true),
		"--mount", mount("volume", h.store, "/data", !online), "-e", "INCFINDER_PORTABLE_HOME=/data",
		"-e", "INCFINDER_MODEL_DIR=/data/models/0.5.0rc2"}
	if h.model != "" {
		args = append(args, "--mount", mount("bind", h.model, "/model-input", true), "-e", "INCFINDER_MODEL_DIR=/model-input")
	}
	if online {
		args = append(args, "--mount", mount("volume", h.store+"-auth", "/auth", false), "-e", "HF_HOME=/auth", "-e", "HF_HUB_OFFLINE=0", "-e", "TRANSFORMERS_OFFLINE=0")
		if os.Getenv("HF_TOKEN") != "" {
			args = append(args, "-e", "HF_TOKEN")
		}
	} else {
		args = append(args, "--network", "none", "-e", "INCFINDER_OFFLINE=1")
	}
	return args
}
func (h host) call(online, interactive bool, mounts, args []string) error {
	cmd := []string{"run", "--rm"}
	if st, e := os.Stdin.Stat(); interactive && e == nil && st.Mode()&os.ModeCharDevice != 0 {
		cmd = append(cmd, "-it")
	}
	cmd = append(cmd, h.opts(online)...)
	cmd = append(cmd, mounts...)
	cmd = append(cmd, h.image.Tag, "-I", "-B", "-X", "utf8", app)
	return command(append(cmd, args...)...)
}
func (h host) ready() error {
	err := h.call(false, false, nil, []string{"portable", "ready"})
	if err == nil {
		return nil
	}
	var status *exec.ExitError
	if !errors.As(err, &status) || status.ExitCode() != 3 {
		return err
	}
	if h.model != "" {
		return errors.New("the selected read-only model directory is incomplete")
	}
	return h.call(true, true, nil, []string{"models", "init"})
}
func unique() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
func selectFile() (string, error) {
	ps := `[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Filter='FASTA / FASTQ|*.fa;*.fna;*.fasta;*.fq;*.fastq;*.gz|All files|*.*'; if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.FileName)}`
	out, err := exec.Command("powershell.exe", "-NoProfile", "-STA", "-Command", ps).Output()
	return string(out), err
}
func (h host) export(mounts, args []string, output string) error {
	dest, err := filepath.Abs(output)
	if err != nil {
		return err
	}
	if _, e := os.Lstat(dest); e == nil {
		return fmt.Errorf("output already exists: %s", dest)
	} else if !os.IsNotExist(e) {
		return e
	}
	if err = os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
		return err
	}
	cmd := append([]string{"create"}, h.opts(false)...)
	cmd = append(cmd, mounts...)
	cmd = append(cmd, "--mount", mount("volume", "", "/results", false), h.image.Tag, "-I", "-B", "-X", "utf8", app)
	id, err := capture(append(cmd, args...)...)
	if err != nil || len(id) != 64 {
		return fmt.Errorf("could not create the analysis container: %v", err)
	}
	keep := false
	defer func() {
		if !keep {
			_, _ = capture("rm", "-v", id)
		}
	}()
	if err = command("start", "-a", id); err != nil {
		return err
	}
	status, err := capture("inspect", "--format", "{{.State.ExitCode}}", id)
	if err != nil || status != "0" {
		return fmt.Errorf("analysis exited with status %s", status)
	}
	if err = os.Mkdir(dest, 0755); err != nil {
		keep = true
		return fmt.Errorf("results remain in container %s:/results/output: %w", id, err)
	}
	if err = command("cp", id+":/results/output/.", dest); err != nil {
		keep = true
		return fmt.Errorf("copy failed; complete results remain in %s:/results/output", id)
	}
	fmt.Println("Results:", dest)
	return nil
}

type paths struct {
	mounts []string
	count  int
}

func (p *paths) input(value string) (string, error) {
	file, err := filepath.Abs(value)
	if err != nil {
		return "", err
	}
	if _, err = os.Stat(file); err != nil {
		return "", err
	}
	p.count++
	dest := path.Join(fmt.Sprintf("/input%d", p.count), filepath.Base(file))
	p.mounts = append(p.mounts, "--mount", mount("bind", file, dest, true))
	return dest, nil
}
func (h host) dispatch(args []string) error {
	if len(args) == 0 {
		if runtime.GOOS == "windows" {
			file, err := selectFile()
			if err != nil || file == "" {
				return err
			}
			args = []string{file}
		} else {
			args = []string{"--help"}
		}
	}
	raw := append([]string(nil), args...)
	args = nil
	for i := 0; i < len(raw); i++ {
		key, value, with := strings.Cut(raw[i], "=")
		if key == "--model-dir" {
			if !with {
				i++
				if i == len(raw) {
					return errors.New("missing model directory")
				}
				value = raw[i]
			}
			h.model = value
		} else {
			args = append(args, raw[i])
		}
	}
	if h.model != "" {
		d, e := filepath.Abs(h.model)
		if e != nil {
			return e
		}
		st, e := os.Stat(d)
		if e != nil || !st.IsDir() {
			return errors.New("model directory must be an existing directory")
		}
		h.model = d
	}
	if len(args) == 0 {
		return errors.New("missing command")
	}
	if args[0] == "auth" && len(args) > 1 && args[1] == "login" {
		return h.call(true, true, nil, args)
	}
	if args[0] == "models" && len(args) > 1 && args[1] == "init" {
		if h.model != "" {
			return h.call(false, false, nil, []string{"models", "verify"})
		}
		return h.call(true, true, nil, args)
	}
	prediction := args[0] == "predict" || args[0] == "--input" || strings.HasPrefix(args[0], "--input=") || args[0] == "-i"
	quick := false
	if st, err := os.Stat(args[0]); err == nil && !st.IsDir() {
		prediction = true
		quick = true
	}
	positionalDir := len(args) > 1 && (args[0] == "lineage" || args[0] == "report" || args[0] == "share")
	if !prediction && !positionalDir {
		return h.call(false, false, nil, args)
	}
	for _, v := range args {
		if v == "--help" || v == "-h" {
			return h.call(false, false, nil, args)
		}
	}
	var mapped paths
	translated := []string{}
	output := ""
	source := ""
	dry := false
	for i := 0; i < len(args); i++ {
		arg := args[i]
		key, val, with := strings.Cut(arg, "=")
		switch key {
		case "--output", "--o", "-o", "--input", "--i", "-i", "--features-dir":
			if !with {
				i++
				if i == len(args) {
					return errors.New("missing value for " + key)
				}
				val = args[i]
			}
			if key == "--output" || key == "--o" || key == "-o" {
				output = val
				translated = append(translated, "--output", "/results/output")
				continue
			}
			if key != "--features-dir" {
				source = val
			}
			p, e := mapped.input(val)
			if e != nil {
				return e
			}
			translated = append(translated, key, p)
		default:
			if (i == 0 && quick) || (i == 1 && (positionalDir || args[0] == "predict") && !strings.HasPrefix(arg, "-")) {
				p, e := mapped.input(arg)
				if e != nil {
					return e
				}
				translated = append(translated, p)
				source = arg
			} else {
				translated = append(translated, arg)
			}
			if arg == "--dry-run" {
				dry = true
			}
		}
	}
	if prediction {
		if !dry {
			if e := h.ready(); e != nil {
				return e
			}
		}
		if output == "" && quick {
			home, e := os.UserHomeDir()
			if e != nil {
				return e
			}
			base := os.Getenv("INCFINDER_RESULTS_DIR")
			if base == "" {
				base = filepath.Join(home, "IncFinder", "results")
			}
			label := strings.TrimSuffix(filepath.Base(source), filepath.Ext(source))
			output = filepath.Join(base, label+"-"+time.Now().Format("20060102-150405")+"-"+unique()[:6])
			translated = append(translated, "--output", "/results/output")
		}
	}
	if output != "" && !dry {
		return h.export(mapped.mounts, translated, output)
	}
	return h.call(false, false, mapped.mounts, translated)
}
func run() error {
	exe, e := os.Executable()
	if e != nil {
		return e
	}
	base := filepath.Dir(exe)
	data, e := os.ReadFile(filepath.Join(base, "portable", "bundle.json"))
	if e != nil {
		return e
	}
	h := host{base: base, store: os.Getenv("INCFINDER_DOCKER_VOLUME"), model: os.Getenv("INCFINDER_MODEL_DIR")}
	if h.store == "" {
		h.store = "incfinder-portable-rc2-models"
	}
	if strings.ContainsAny(h.store, "/\\,=:\n\r") || strings.HasPrefix(h.store, "-") {
		return errors.New("invalid Docker volume name")
	}
	if e = json.Unmarshal(data, &h.image); e != nil {
		return e
	}
	if e = h.load(); e != nil {
		return e
	}
	return h.dispatch(os.Args[1:])
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "IncFinder:", err)
		os.Exit(1)
	}
}
