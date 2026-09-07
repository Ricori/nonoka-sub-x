// Command stage assembles the Python sidecar resources shipped with Nonoka X.
package main

import (
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type stagedFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

func main() {
	repository := flag.String("repository", "..", "repository root")
	output := flag.String("output", "internal/app/bundled/nonoka_x", "staging directory")
	flag.Parse()
	repo, err := filepath.Abs(*repository)
	must(err)
	destination, err := filepath.Abs(*output)
	must(err)
	must(os.RemoveAll(destination))
	must(os.MkdirAll(destination, 0o755))

	entries := []string{
		"bootstrap-requirements.py312.txt",
		"scripts/run_local_sidecar.py",
		"src/nonoka_x",
		"third_party/finesub/src",
		"third_party/finesub/LICENSE",
		"third_party/finesub/LICENSES.json",
		"third_party/finesub/PROMPT_LICENSE.md",
		"third_party/finesub/UPSTREAM.json",
		"third_party/finesub/pyproject.toml",
		"third_party/finesub/VERSION",
	}
	var copied []stagedFile
	for _, entry := range entries {
		must(copyEntry(filepath.Join(repo, entry), filepath.Join(destination, entry), destination, &copied))
	}
	sort.Slice(copied, func(i, j int) bool { return copied[i].Path < copied[j].Path })
	manifest, err := json.MarshalIndent(map[string]any{"schema": 1, "files": copied}, "", "  ")
	must(err)
	must(os.WriteFile(filepath.Join(destination, "STAGED.json"), append(manifest, '\n'), 0o644))
	must(os.WriteFile(filepath.Join(destination, ".gitkeep"), nil, 0o644))
}

func copyEntry(source, destination, root string, copied *[]stagedFile) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return copyFile(source, destination, root, copied, info.Mode())
	}
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() && entry.Name() == "__pycache__" {
			return filepath.SkipDir
		}
		if entry.IsDir() || strings.HasSuffix(entry.Name(), ".pyc") {
			return nil
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		return copyFile(path, filepath.Join(destination, relative), root, copied, info.Mode())
	})
}

func copyFile(source, destination, root string, copied *[]stagedFile, mode fs.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		return err
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode.Perm())
	if err != nil {
		return err
	}
	hash := sha256.New()
	size, err := io.Copy(io.MultiWriter(output, hash), input)
	if err != nil {
		output.Close()
		return err
	}
	if err = output.Close(); err != nil {
		return err
	}
	relative, err := filepath.Rel(root, destination)
	if err != nil {
		return err
	}
	*copied = append(*copied, stagedFile{Path: filepath.ToSlash(relative), SHA256: fmt.Sprintf("%x", hash.Sum(nil)), Size: size})
	return nil
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
