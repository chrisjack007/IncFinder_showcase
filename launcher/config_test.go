package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestThresholdTableRoundTrip(t *testing.T) {
	root := t.TempDir()
	a := app{tag: original, target: "linux", root: root}
	p := filepath.Join(root, "thresholds.tsv")
	s := a.defaults()
	atomicWrite(p, s.table())
	v := fromTable(p)
	labels, want := ct()
	for i := range labels {
		if v[i] != want[i] {
			t.Fatalf("threshold mismatch for %s", labels[i])
		}
	}
	rows := strings.Split(strings.TrimSpace(string(bytesAt(p))), "\n")
	bad := [][]string{rows[:21], append(append([]string{}, rows[:21]...), rows[1])}
	for _, v := range []string{"NaN", "Inf", "-0.1", "1.1"} {
		clone := append([]string{}, rows...)
		cols := strings.Split(clone[1], "\t")
		cols[3] = v
		clone[1] = strings.Join(cols, "\t")
		bad = append(bad, clone)
	}
	for _, lines := range bad {
		atomicWrite(p, []byte(strings.Join(lines, "\n")+"\n"))
		if err := handle(func() { fromTable(p) }); err == nil {
			t.Fatal("Invalid threshold table accepted")
		}
	}
}
func TestOneRunSettingAndRetry(t *testing.T) {
	a := app{tag: portable, target: "wsl2", root: t.TempDir()}
	a.configure([]string{"threshold", "0.5"})
	a.configure([]string{"--once", "label", "IncFIA", "0.25"})
	checkMode := func(mode string) {
		t.Helper()
		if got := a.effective().Mode; got != mode {
			t.Fatalf("mode %s, wanted %s", got, mode)
		}
	}
	checkMode("custom")
	first, finish := a.claim()
	if first.values()[2] != 0.25 {
		t.Fatal("Custom class was not saved")
	}
	checkMode("u05")
	finish(false)
	checkMode("custom")
	_, finish = a.claim()
	finish(true)
	checkMode("u05")
	a.configure([]string{"reset"})
	checkMode("ct")
}
func TestOfficialModelMismatchBeforeSaving(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "bad")
	mkdir(filepath.Join(dir, "rf"))
	check(os.WriteFile(filepath.Join(dir, "rf", "stage1.joblib"), []byte("bad"), 0600))
	a := app{tag: original, target: "windows", root: root}
	if err := handle(func() { a.configure([]string{"models", dir}) }); err == nil {
		t.Fatal("Accepted damaged model")
	}
	if exists(filepath.Join(root, "config.json")) {
		t.Fatal("Damaged model path became the default")
	}
}
