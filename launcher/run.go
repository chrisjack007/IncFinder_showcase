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
func (a app) install() {
	e := a.prepare()
	s := a.settings(false)
	e.prepareModels(s)
	showChecks(e, s)
	demo := a.defaults()
	demo.Models = s.Models
	demo.Results = s.Results
	e.predict(demo, join(e.software, "examples", "sample.fasta"))
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
	e.predict(s, inputPath)
	success = true
}
func (e engine) predict(s settings, path string) {
	fmt.Println("Research preview / 科研预览：原始序列运行使用 --allow-encoder-drift；跨平台编码等价性标记为 NOT_ESTABLISHED。")
	dir := join(abs(s.Results), "run-"+unique())
	mkdir(filepath.Dir(dir))
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
	if s.Mode == "custom" {
		e.writeReview(s, dir)
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
