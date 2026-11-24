package main

import (
	"os"
	"syscall"
)

func lock(p string) func() {
	f := must(os.OpenFile(p, os.O_CREATE|os.O_RDWR, 0600))
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		check(err)
	}
	return func() { f.Close() }
}
