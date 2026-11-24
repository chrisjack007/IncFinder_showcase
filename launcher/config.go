package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type settings struct {
	Mode                 string
	Thresholds           []float64
	Batch, Threads, Jobs int
	Models, Results      string
}

func decimal(v string) float64 {
	x, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil || math.IsNaN(x) || math.IsInf(x, 0) || x < 0 || x > 1 {
		stop("Threshold must be a finite number in [0,1]: %s", v)
	}
	return x
}
func integer(v string) int {
	x, err := strconv.Atoi(v)
	if err != nil || x < 1 {
		stop("Use a positive integer")
	}
	return x
}
func ct() ([]string, []float64) {
	text := string(must(resources.ReadFile("trust/thresholds.tsv")))
	lines := strings.Split(strings.TrimSpace(text), "\n")[1:]
	var labels []string
	var vals []float64
	for _, s := range lines {
		row := strings.Split(strings.TrimSpace(s), "\t")
		labels = append(labels, row[2])
		vals = append(vals, decimal(row[3]))
	}
	return labels, vals
}
func fromTable(p string) []float64 {
	f := must(os.Open(p))
	defer f.Close()
	rd := csv.NewReader(f)
	rd.Comma = '\t'
	records := must(rd.ReadAll())
	labels, _ := ct()
	if len(records) != 22 || strings.TrimPrefix(strings.Join(records[0], "\t"), "\uFEFF") != "model_key\tlabel_order\tlabel_name\tthreshold" {
		stop("Expected a UTF-8 TSV with the four template columns and exactly 21 labels")
	}
	vals := make([]float64, 21)
	seen := make([]bool, 21)
	for _, r := range records[1:] {
		idx, err := strconv.Atoi(r[1])
		if err != nil || idx < 0 || idx >= 21 || r[0] != "nt_v3" || labels[idx] != r[2] || seen[idx] {
			stop("Invalid, missing or duplicate class: %s", r[2])
		}
		seen[idx] = true
		vals[idx] = decimal(r[3])
	}
	return vals
}
func (s settings) values() []float64 {
	_, v := ct()
	switch s.Mode {
	case "ct":
	case "u05":
		for i := range v {
			v[i] = 0.5
		}
	case "custom":
		v = append([]float64(nil), s.Thresholds...)
	default:
		stop("Unknown saved mode")
	}
	return v
}
func (s settings) table() []byte {
	labels, _ := ct()
	v := s.values()
	var b strings.Builder
	b.WriteString("model_key\tlabel_order\tlabel_name\tthreshold\n")
	for i, l := range labels {
		fmt.Fprintf(&b, "nt_v3\t%d\t%s\t%s\n", i, l, strconv.FormatFloat(v[i], 'g', -1, 64))
	}
	return []byte(b.String())
}
func (s *settings) mode(v string, force bool) {
	if !force && (v == "ct" || v == "u05" || v == "0.5") {
		s.Mode = v
		if v == "0.5" {
			s.Mode = "u05"
		}
		s.Thresholds = nil
		return
	}
	x := decimal(v)
	s.Mode = "custom"
	s.Thresholds = make([]float64, 21)
	for i := range s.Thresholds {
		s.Thresholds[i] = x
	}
}
func (a app) defaults() settings {
	return settings{Mode: "ct", Batch: 1, Threads: 4, Jobs: 1, Models: os.Getenv("INCFINDER_MODEL_DIR"), Results: join(must(os.UserHomeDir()), "IncFinder", "results", a.tag, a.target)}
}
func (a app) settings(next bool) settings {
	s := a.defaults()
	p := join(a.root, "config.json")
	if next && exists(join(a.root, "next.json")) {
		p = join(a.root, "next.json")
	}
	if exists(p) {
		readJSON(p, &s)
	}
	if s.Batch < 1 || s.Threads < 1 || s.Jobs < 1 || s.Results == "" {
		stop("Invalid settings. Use config reset")
	}
	vals := s.values()
	if len(vals) != 21 {
		stop("Invalid stored thresholds")
	}
	for _, x := range vals {
		if x < 0 || x > 1 || math.IsNaN(x) || math.IsInf(x, 0) {
			stop("Invalid stored threshold")
		}
	}
	return s
}
func (a app) effective() settings {
	unlock := lock(join(a.root, "config.lock"))
	defer unlock()
	return a.settings(true)
}
func (a app) info() {
	unlock := lock(join(a.root, "config.lock"))
	defer unlock()
	fmt.Println("Default / 已保存默认设置:", string(must(json.MarshalIndent(a.settings(false), "", "  "))))
	if exists(join(a.root, "next.json")) {
		fmt.Println("Next run / 仅下次:", string(must(json.MarshalIndent(a.settings(true), "", "  "))))
	}
}
func modelFiles(path string, full bool) bool {
	for _, r := range strings.Split(strings.TrimSpace(string(must(resources.ReadFile("trust/model-hashes.tsv")))), "\n") {
		f := strings.Split(r, "\t")
		p := join(path, filepath.FromSlash(f[0]))
		stat, err := os.Stat(p)
		if os.IsNotExist(err) {
			return false
		}
		check(err)
		size := must(strconv.ParseInt(f[1], 10, 64))
		if !stat.Mode().IsRegular() || stat.Size() != size || (full && digest(p) != f[2]) {
			stop("Official model size or SHA-256 mismatch: %s", p)
		}
	}
	return true
}
func (a app) configure(args []string) {
	if len(args) == 0 {
		a.info()
		options := []string{"threshold", "uniform", "label", "import", "export", "batch", "threads", "jobs", "results", "models", "reset", "review"}
		fmt.Println("1 正式模式 ct/0.5 · 2 统一自定义阈值 Uniform · 3 修改单类 Label · 4 导入 Import TSV · 5 导出 Export TSV\n6 批大小 Batch · 7 线程 Threads · 8 并行 Jobs · 9 结果目录 Results · 10 官方模型目录 Models · 11 恢复默认 Reset · 12 对已有结果重新判读 Review")
		n := integer(ask("选择 / Choose:"))
		if n > len(options) {
			stop("Unknown selection")
		}
		key := options[n-1]
		args = []string{key}
		if key == "label" {
			labels, _ := ct()
			fmt.Println(strings.Join(labels, ", "))
			args = append(args, ask("类别名 / Label name:"))
		}
		if key != "reset" {
			args = append(args, ask("值或路径 / Value or path:"))
		}
		if key != "export" && key != "review" && ask("默认保存 / Save default [Enter], 仅下次 / next run [2]:") == "2" {
			args = append([]string{"--once"}, args...)
		}
	}
	once := args[0] == "--once"
	if once {
		args = args[1:]
	}
	if len(args) == 0 {
		stop("Missing configuration action")
	}
	key := args[0]
	switch key {
	case "help", "--help":
		fmt.Println("config [--once] threshold ct|0.5  /  uniform NUMBER  /  label NAME VALUE  /  import FILE  /  export NEW_FILE\nconfig [--once] batch|threads|jobs N  /  results|models PATH  /  reset\nconfig show  /  config review RUN_DIRECTORY")
		return
	case "show":
		a.info()
		return
	case "review":
		if len(args) != 2 {
			stop("config review RUN_DIRECTORY")
		}
		a.review(abs(args[1]), a.effective())
		return
	}
	unlock := lock(join(a.root, "config.lock"))
	defer unlock()
	p := join(a.root, "config.json")
	if once {
		p = join(a.root, "next.json")
	}
	if key == "reset" {
		if len(args) != 1 {
			stop("config reset takes no value")
		}
		if once {
			writeJSON(p, a.defaults())
		} else {
			for _, name := range []string{"config.json", "next.json"} {
				f := join(a.root, name)
				if exists(f) {
					check(os.Remove(f))
				}
			}
		}
		fmt.Println("Defaults restored / 已恢复发行默认设置")
		return
	}
	if (key == "label" && len(args) != 3) || (key != "label" && len(args) != 2) {
		stop("Incorrect parameters; use config help")
	}
	s := a.settings(once)
	v := args[1]
	switch key {
	case "threshold":
		s.mode(v, false)
	case "uniform":
		s.mode(v, true)
	case "import":
		s.Thresholds = fromTable(abs(v))
		s.Mode = "custom"
	case "export":
		f := must(os.OpenFile(abs(v), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600))
		defer f.Close()
		_, err := f.Write(s.table())
		check(err)
		fmt.Println("TSV:", abs(v))
		return
	case "label":
		labels, _ := ct()
		vals := s.values()
		idx := -1
		for i, label := range labels {
			if label == v {
				idx = i
				break
			}
		}
		if idx < 0 {
			stop("Unknown Inc class: %s", v)
		}
		vals[idx] = decimal(args[2])
		s.Mode = "custom"
		s.Thresholds = vals
	case "batch":
		s.Batch = integer(v)
	case "threads":
		s.Threads = integer(v)
	case "jobs":
		s.Jobs = integer(v)
	case "results":
		s.Results = abs(v)
	case "models":
		if v == "default" {
			s.Models = ""
		} else {
			dir := abs(v)
			fmt.Println("Verifying official model SHA-256 / 正在核对模型摘要…")
			if !modelFiles(dir, true) {
				stop("Official model directory incomplete: %s", dir)
			}
			s.Models = dir
		}
	default:
		stop("Unknown setting; use config help")
	}
	writeJSON(p, s)
	fmt.Println("Saved / 已保存:", p)
}
func (a app) claim() (settings, func(bool)) {
	unlock := lock(join(a.root, "config.lock"))
	defer unlock()
	s := a.settings(true)
	pending := join(a.root, "next.json")
	if !exists(pending) {
		return s, func(bool) {}
	}
	claimed := join(a.root, "claimed-"+unique()+".json")
	check(os.Rename(pending, claimed))
	return s, func(success bool) {
		l := lock(join(a.root, "config.lock"))
		defer l()
		if !success && !exists(pending) {
			check(os.Rename(claimed, pending))
		} else {
			check(os.Remove(claimed))
		}
	}
}
