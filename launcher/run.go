package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

func showChecks(e engine, s settings) {
	version := output(e.science(s, "version"))
	if version != "incfinder 0.5.0rc2" {
		stop("Unexpected software version: %s", version)
	}
	fmt.Println(version)
	for _, args := range [][]string{{"doctor"}, {"models", "verify"}} {
		raw := output(e.science(s, append(append([]string{}, args...), "--model-dir", e.model(s))...))
		var result struct{ Status string }
		check(json.Unmarshal([]byte(raw), &result))
		if result.Status != "PASS" {
			stop("%s: %s", strings.Join(args, " "), raw)
		}
		fmt.Println(strings.Join(args, " ") + ": PASS")
	}
}
func installStep(name, recovery string, action func()) {
	if err := handle(action); err != nil {
		stop("%s: %v\n%s", name, err, recovery)
	}
}

func (a app) install() {
	var e engine
	installStep("Runtime / 安装包与环境", "Check the signed download, available space and runtime, then retry menu 2. / 检查安装包来源、磁盘空间和运行环境，再选菜单 2。", func() { e = a.prepare() })
	s := a.settings(false)
	installStep("Models / 模型准备", "NTv3 authorization / 授权问题: https://huggingface.co/InstaDeepAI/NTv3_650M_pre. Sign in with your approved account. If a transfer stopped, retry menu 2 with the same cache. / 请使用获准账号登录。传输中断时保留缓存，再选菜单 2。", func() { e.prepareModels(s) })
	installStep("Identity / 软件与模型核验", "Check the model directory and the signed rc2 files reported above, then retry menu 2. / 核对上述模型目录与签名文件，再选菜单 2。", func() { showChecks(e, s) })
	demo := a.defaults()
	demo.Models = s.Models
	demo.Results = s.Results
	dir := e.predict(demo, join(e.software, "examples", "sample.fasta"))
	recovery := fmt.Sprintf("Result directory / 结果目录: %s\nCheck the signed package and indicated field before using the demo. / 请按上述项目核对签名包与示例结果。", dir)
	installStep("Example verification / 官方示例核验失败", recovery, func() {
		var summary struct {
			ID string `json:"run_id"`
		}
		readJSON(join(dir, "run_summary.json"), &summary)
		if summary.ID == "" {
			stop("Current run ID missing")
		}
		e.verifyExample(demo, dir, summary.ID)
		displayResult(dir)
	})
	fmt.Printf("官方示例核验通过 / Official example verified: 2/2 · %s · CPU · ct\n", a.target)
	fmt.Println("示例输入、记录顺序、两层概率、21 类判定与 CSV 均与签名参考相符。 / Input, row order, probabilities, 21 calls and CSV match the signed reference.")
	fmt.Println("菜单 3 分析自己的 FASTA/FASTQ / Choose menu 3 to run your own file.")
}

func (e engine) verifyExample(s settings, dir, runID string) {
	reference := join(e.software, "docs", "example-reference.json")
	if !e.a.isp() {
		reference = join(e.software, "examples", "expected_outputs", "raw_cpu.json")
	}
	sha := e.a.signedDigest("rc2", "example-reference.json")
	stage := must(os.MkdirTemp(e.a.root, "verify-example-"))
	defer os.RemoveAll(stage)
	script := materialize(stage, "verify_example.py")
	if e.a.docker() {
		mounts := []string{"--read-only",
			"--mount", mount("bind", dir, "/example-result", true),
			"--mount", mount("bind", reference, "/reference.json", true),
			"--mount", mount("bind", stage, "/helper", true)}
		execute(e.dockerPython(s, false, false, mounts, "/helper/verify_example.py", "/example-result", "/reference.json", sha, e.a.target, "--run-id", runID))
		return
	}
	execute(e.nativePython(false, script, dir, reference, sha, e.a.target, "--run-id", runID))
}
func (a app) runInput(args []string) {
	if len(args) == 0 {
		prompt := "FASTA / FASTQ 文件路径 / Input file:"
		if runtime.GOOS == "windows" {
			prompt += " [Enter: 选择文件 / browse]"
		}
		file := ask(prompt)
		if file == "" && runtime.GOOS == "windows" {
			file = output(launch("", nil, "powershell.exe", "-NoProfile", "-STA", "-Command",
				"[void][Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms'); $d=[Windows.Forms.OpenFileDialog]::new(); $d.Title='IncFinder - FASTA / FASTQ'; $d.Filter='FASTA / FASTQ|*.fa;*.fna;*.fasta;*.fq;*.fastq;*.gz|All files|*.*'; if ($d.ShowDialog() -eq 'OK') { [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [Console]::Write($d.FileName) }; $d.Dispose()"))
			if file == "" {
				return
			}
		}
		args = []string{file}
	}
	sample := false
	if args[0] == "--example" {
		if len(args) < 2 || (args[1] != "fasta" && args[1] != "fastq") {
			stop("run --example fasta|fastq")
		}
		sample = true
		args = args[1:]
	}
	inputPath := args[0]
	args = args[1:]
	if len(args) != 0 && (len(args) != 2 || args[0] != "--threshold") {
		stop("run FILE [--threshold ct|0.5|NUMBER]")
	}
	s, finish := a.claim()
	success := false
	defer func() { finish(success) }()
	if len(args) == 2 {
		s.mode(args[1], false)
	}
	e := a.prepare()
	if sample {
		inputPath = join(e.software, "examples", "sample."+inputPath)
	} else {
		inputPath = abs(inputPath)
	}
	stat := must(os.Stat(inputPath))
	if !stat.Mode().IsRegular() {
		stop("Input is not a regular file: %s", inputPath)
	}
	e.prepareModels(s)
	dir := e.predict(s, inputPath)
	displayResult(dir)
	if s.Mode == "custom" {
		e.writeReview(s, dir)
	}
	success = true
}
func (e engine) predict(s settings, path string) string {
	fmt.Println("Research preview / 科研预览：原始序列运行使用 --allow-encoder-drift；跨平台编码等价性标记为 NOT_ESTABLISHED。")
	dir := join(abs(s.Results), "run-"+unique())
	mkdir(filepath.Dir(dir))
	if exists(dir) {
		stop("Output already exists: %s", dir)
	}
	mode := s.Mode
	if mode == "custom" {
		mode = "ct"
	}
	source := path
	out := dir
	if e.a.docker() {
		source = "/inputs/" + filepath.Base(path)
		out = "/results/out"
	}
	args := []string{"predict", source, "--output", out, "--model-dir", e.model(s), "--device", "cpu", "--batch-size", strconv.Itoa(s.Batch), "--threads", strconv.Itoa(s.Threads), "--jobs", strconv.Itoa(s.Jobs), "--allow-encoder-drift", "--threshold-mode", mode}
	if e.a.docker() {
		e.export(s, []string{"--mount", mount("bind", path, source, true)}, append([]string{"-m", "incfinder"}, args...), dir)
	} else {
		execute(e.science(s, args...))
	}
	return dir
}

func displayResult(dir string) {
	fmt.Println("RESULT_DIR\t" + dir)
	fmt.Println("CSV:", join(dir, "inc_results.csv"))
	fmt.Println("Excel:", join(dir, "inc_predictions.xlsx"))
	f := must(os.Open(join(dir, "inc_results.csv")))
	defer f.Close()
	csvReader := csv.NewReader(f)
	headers := must(csvReader.Read())
	cols := map[string]int{}
	for i, x := range headers {
		cols[strings.TrimPrefix(x, "\uFEFF")] = i
	}
	for _, name := range []string{"Header", "Result"} {
		if _, ok := cols[name]; !ok {
			stop("Unexpected result columns")
		}
	}
	for n := 0; n < 2; n++ {
		row, err := csvReader.Read()
		if err == io.EOF {
			break
		}
		check(err)
		fmt.Printf("  %s  →  %s\n", row[cols["Header"]], row[cols["Result"]])
	}
}
func (a app) review(source string, s settings) { e := a.prepare(); e.writeReview(s, source) }
func (e engine) writeReview(s settings, source string) {
	if !exists(join(source, "artifact_manifest.sha256")) {
		stop("Choose a complete rc2 result directory")
	}
	root := abs(s.Results)
	mkdir(root)
	dest := join(root, "review-"+unique())
	rel, err := filepath.Rel(source, dest)
	if err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		stop("Choose a results directory outside the source run")
	}
	stage := must(os.MkdirTemp(e.a.root, "review-"))
	defer os.RemoveAll(stage)
	script := materialize(stage, "review.py")
	tsv := join(stage, "thresholds.tsv")
	atomicWrite(tsv, s.table())
	if e.a.docker() {
		s.Models = ""
		e.export(s, []string{"--mount", mount("bind", source, "/source", true), "--mount", mount("bind", stage, "/helper", true)}, []string{"/helper/review.py", "/source", "/helper/thresholds.tsv", "/results/out", e.a.tag}, dest)
	} else {
		execute(e.nativePython(false, script, source, tsv, dest, e.a.tag))
	}
	fmt.Println("CUSTOM_DIR\t" + dest)
	fmt.Println("Custom Excel:", join(dest, "custom_results.xlsx"))
}
