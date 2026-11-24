// A host-side launcher for the two signed IncFinder rc2 distributions.
package main

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

//go:embed trust/* review.py
var resources embed.FS

const original = "v0.5.0rc2"
const portable = "v0.5.0rc2-portable.1"
const repository = "https://github.com/chrisjack007/IncFinder_showcase"
const packageSHA = "34cae07ba79aeded0c844716bbeb72269318f420bbd946aa9aaea54f9f87de3a"

var input = bufio.NewReader(os.Stdin)

type problem struct{ error }

func stop(format string, args ...any) { panic(problem{fmt.Errorf(format, args...)}) }
func check(err error) {
	if err != nil {
		panic(problem{err})
	}
}
func must[T any](value T, err error) T { check(err); return value }
func handle(f func()) (err error) {
	defer func() {
		if caught := recover(); caught != nil {
			if p, ok := caught.(problem); ok {
				err = p.error
			} else {
				panic(caught)
			}
		}
	}()
	f()
	return nil
}
func ask(question string) string {
	fmt.Print(question + " ")
	s, err := input.ReadString('\n')
	if err != nil && len(s) == 0 {
		stop("Input closed. Use a menu or pass a command / 输入结束")
	}
	return strings.TrimSpace(s)
}
func envOr(key, alt string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return alt
}
func join(p ...string) string { return filepath.Join(p...) }
func abs(p string) string {
	p = strings.TrimSpace(p)
	if len(p) >= 2 && p[0] == '"' && p[len(p)-1] == '"' {
		p = p[1 : len(p)-1]
	}
	if p == "" {
		stop("Empty path / 路径为空")
	}
	if p == "~" {
		p = must(os.UserHomeDir())
	} else if strings.HasPrefix(p, "~/") || strings.HasPrefix(p, "~\\") {
		p = join(must(os.UserHomeDir()), p[2:])
	}
	return must(filepath.Abs(p))
}
func exists(p string) bool {
	_, err := os.Stat(p)
	if errors.Is(err, os.ErrNotExist) {
		return false
	}
	check(err)
	return true
}
func mkdir(p string)          { check(os.MkdirAll(p, 0700)) }
func bytesAt(p string) []byte { return must(os.ReadFile(p)) }
func digest(p string) string {
	f := must(os.Open(p))
	defer f.Close()
	h := sha256.New()
	_, err := io.Copy(h, f)
	check(err)
	return hex.EncodeToString(h.Sum(nil))
}
func atomicWrite(p string, data []byte) {
	mkdir(filepath.Dir(p))
	f := must(os.CreateTemp(filepath.Dir(p), ".pending-*"))
	name := f.Name()
	defer os.Remove(name)
	defer f.Close()
	_, err := f.Write(data)
	check(err)
	check(f.Sync())
	check(f.Close())
	check(os.Rename(name, p))
}
func readJSON(p string, dest any) { check(json.Unmarshal(bytesAt(p), dest)) }
func writeJSON(p string, src any) {
	b := must(json.MarshalIndent(src, "", "  "))
	atomicWrite(p, append(b, '\n'))
}
func materialize(root, path string) string {
	b := must(resources.ReadFile(path))
	p := join(root, filepath.Base(path))
	if !exists(p) || string(bytesAt(p)) != string(b) {
		atomicWrite(p, b)
	}
	return p
}
func unique() string {
	var b [6]byte
	_, err := rand.Read(b[:])
	check(err)
	return time.Now().Format("20060102-150405") + "-" + hex.EncodeToString(b[:])
}
func executable(name string) string {
	p, err := exec.LookPath(name)
	if err != nil {
		stop("Required tool missing / 需要安装或启用 %s", name)
	}
	return p
}
func launch(dir string, env []string, name string, args ...string) *exec.Cmd {
	c := exec.Command(name, args...)
	c.Dir = dir
	c.Stdin = os.Stdin
	c.Env = append(os.Environ(), env...)
	return c
}
func execute(c *exec.Cmd) {
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Run(); err != nil {
		stop("%s: %v", filepath.Base(c.Path), err)
	}
}
func output(c *exec.Cmd) string {
	c.Stderr = os.Stderr
	b, err := c.Output()
	if err != nil {
		stop("%s: %v", filepath.Base(c.Path), err)
	}
	return strings.TrimSpace(string(b))
}

type app struct{ tag, target, home, root string }

func newApp(tag string, docker bool) app {
	if tag != original && tag != portable {
		stop("Unknown release / 发行版无效")
	}
	if runtime.GOARCH != "amd64" {
		stop("IncFinder rc2 requires x86-64")
	}
	target := runtime.GOOS
	if docker {
		target = "docker"
	} else if target == "linux" {
		b, _ := os.ReadFile("/proc/sys/kernel/osrelease")
		if strings.Contains(strings.ToLower(string(b)), "microsoft") {
			target = "wsl2"
		}
	}
	if target != "docker" && target != "linux" && target != "wsl2" && target != "windows" {
		stop("Unsupported operating system")
	}
	home := os.Getenv("INCFINDER_START_HOME")
	if home == "" {
		if runtime.GOOS == "windows" {
			home = join(envOr("LOCALAPPDATA", must(os.UserHomeDir())), "IncFinder", "start")
		} else {
			home = join(envOr("XDG_DATA_HOME", join(must(os.UserHomeDir()), ".local", "share")), "incfinder", "start")
		}
	}
	home = abs(home)
	root := join(home, tag, target)
	mkdir(root)
	return app{tag, target, home, root}
}
func (a app) isp() bool    { return a.tag == portable }
func (a app) docker() bool { return a.target == "docker" }
func (a app) entry(args []string) {
	if len(args) == 0 {
		stop("Missing action")
	}
	switch args[0] {
	case "download":
		fmt.Println("Software:", a.download(true))
	case "install":
		a.install()
	case "run":
		a.runInput(args[1:])
	case "config":
		a.configure(args[1:])
	case "review":
		if len(args) != 2 {
			stop("review RUN_DIRECTORY")
		}
		a.review(abs(args[1]), a.effective())
	case "help", "--help", "-h":
		help()
	default:
		stop("Unknown action / 未知操作: %s", args[0])
	}
}
func help() {
	fmt.Println("IncFinder Start  |  download · install · run FILE [--threshold ct|0.5] · config · review RUN\nAdd --docker for Docker. Use config help for settings. / 不带参数打开菜单。")
}
func start() {
	args := os.Args[1:]
	if len(args) < 2 || args[0] != "--release" {
		stop("Start from the .cmd or .sh release script")
	}
	tag := args[1]
	args = args[2:]
	docker := false
	if len(args) > 0 && args[0] == "--docker" {
		docker = true
		args = args[1:]
	}
	if len(args) > 0 && (args[0] == "--help" || args[0] == "help" || args[0] == "-h") {
		help()
		return
	}
	if len(args) == 0 && !docker {
		answer := strings.ToLower(ask("运行方式 / Runtime: [1] 本机 Native (default)   [2] Docker   [0] Exit"))
		switch answer {
		case "2", "docker":
			docker = true
		case "1", "", "native":
		case "0", "q":
			return
		default:
			stop("Unknown runtime selection")
		}
	}
	a := newApp(tag, docker)
	if len(args) > 0 {
		a.entry(args)
		return
	}
	for {
		fmt.Printf("\nIncFinder %s · %s\n", a.tag, a.target)
		choice := ask("1 下载 Download   2 安装并试跑 Install + demo   3 使用 Run   4 配置 Config   0 退出 Exit\n>")
		options := map[string]string{"1": "download", "2": "install", "3": "run", "4": "config"}
		if choice == "0" || strings.EqualFold(choice, "q") {
			return
		}
		cmd, ok := options[choice]
		if !ok {
			fmt.Println("Select 0–4 / 请输入 0–4")
			continue
		}
		if err := handle(func() { a.entry([]string{cmd}) }); err != nil {
			fmt.Fprintln(os.Stderr, "IncFinder:", err)
		}
	}
}
func main() {
	if err := handle(start); err != nil {
		fmt.Fprintln(os.Stderr, "IncFinder:", err)
		os.Exit(1)
	}
}
