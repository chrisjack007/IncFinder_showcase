package main

import (
	"syscall"
	"time"
)

func lock(p string) func() {
	name := must(syscall.UTF16PtrFromString(p))
	for {
		h, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
		if err == nil {
			return func() { syscall.CloseHandle(h) }
		}
		if err != syscall.Errno(32) {
			check(err)
		}
		time.Sleep(250 * time.Millisecond)
	}
}
