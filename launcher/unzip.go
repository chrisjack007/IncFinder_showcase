package main

import (
	"archive/zip"
	"io"
	"os"
	"path/filepath"
)

// The Linux Docker bundle is a ZIP. GNU tar only handles the tar-based native bundles.
func extractPortableZIP(archive, directory string) {
	reader := must(zip.OpenReader(archive))
	defer reader.Close()
	for _, member := range reader.File {
		name := filepath.FromSlash(member.Name)
		if !filepath.IsLocal(name) {
			stop("Unsafe ZIP member: %s", member.Name)
		}
		dst := filepath.Join(directory, name)
		if member.FileInfo().IsDir() {
			mkdir(dst)
			continue
		}
		if !member.Mode().IsRegular() {
			stop("Unsupported ZIP member: %s", member.Name)
		}
		mkdir(filepath.Dir(dst))
		func() {
			src := must(member.Open())
			defer src.Close()
			mode := os.FileMode(0600)
			if member.Mode()&0111 != 0 {
				mode = 0700
			}
			out := must(os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode))
			defer out.Close()
			_, err := io.Copy(out, src)
			check(err)
			check(out.Close())
		}()
	}
}
