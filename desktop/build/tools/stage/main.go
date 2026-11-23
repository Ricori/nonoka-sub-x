// Command stage assembles the Python sidecar resources shipped with Nonoka X.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

type stagedFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// enginePackages are the engine packages shipped with the sidecar (plan 4.6 item 33), under `engines/`.
var enginePackages = []string{"finesub-transcribe", "finesub-correct", "finesub-knowledge", "flowocr"}

// packageFiles are what an engine package ships: its manifest, its code and the files that
// vouch for its FineSub sources. Its quality lock and change log are the repository's.
var packageFiles = []string{"PACKAGE.json", "adapter", "src", "LICENSE", "LICENSES.json", "UPSTREAM.json", "PROMPT_LICENSE.md", "LICENSES"}

// optionalPackageFiles may be missing from a package: only one carrying the prompt templates has their licence,
// and only the on-screen text package carries its models' notices (`LICENSES/`).
var optionalPackageFiles = map[string]bool{"PROMPT_LICENSE.md": true, "LICENSES": true}

// coreEntries are the rest of the sidecar resources, relative to the repository root.
var coreEntries = []string{
	"bootstrap-requirements.py312.txt",
	"scripts/run_local_sidecar.py",
	"src/nonoka_x",
}

// engineSources are the engine packages' code, precompiled while staging (plan 4.2 item 17):
// each package's own code and its FineSub sources.
func engineSources() []string {
	var sources []string
	for _, name := range enginePackages {
		sources = append(sources, "engines/"+name+"/adapter", "engines/"+name+"/src")
	}
	return sources
}

// entries are what the sidecar resources consist of, relative to the repository root.
func entries(repo string) []string {
	staged := append([]string{}, coreEntries...)
	for _, name := range enginePackages {
		for _, file := range packageFiles {
			entry := "engines/" + name + "/" + file
			if optionalPackageFiles[file] {
				if _, err := os.Stat(filepath.Join(repo, filepath.FromSlash(entry))); err != nil {
					continue
				}
			}
			staged = append(staged, entry)
		}
	}
	return staged
}

// interpreterManifest pins the interpreter of the Python environment layer. Its bytecode is what
// every engine process reads: the transcription package runs on that interpreter, and the
// launcher that runs the other two is the same minor version (bytecode is per minor version).
const interpreterManifest = "src/nonoka_x/resources/runtime-manifest.windows-x64.json"

func main() {
	repository := flag.String("repository", "..", "repository root")
	output := flag.String("output", "internal/app/bundled/nonoka_x", "staging directory")
	python := flag.String("python", "python", "interpreter that precompiles the engine code; its version must be the pinned one")
	flag.Parse()
	repo, err := filepath.Abs(*repository)
	must(err)
	destination, err := filepath.Abs(*output)
	must(err)
	must(stage(repo, destination, *python))
}

// stage copies the resources into destination, precompiles the engine code and writes STAGED.json.
func stage(repo, destination, python string) error {
	if err := os.RemoveAll(destination); err != nil {
		return err
	}
	if err := os.MkdirAll(destination, 0o755); err != nil {
		return err
	}
	var copied []stagedFile
	for _, entry := range entries(repo) {
		if err := copyEntry(filepath.Join(repo, filepath.FromSlash(entry)), filepath.Join(destination, filepath.FromSlash(entry)), destination, &copied); err != nil {
			return err
		}
	}
	compiled, err := precompile(repo, destination, python)
	if err != nil {
		return err
	}
	copied = append(copied, compiled...)
	sort.Slice(copied, func(i, j int) bool { return copied[i].Path < copied[j].Path })
	manifest, err := json.MarshalIndent(map[string]any{"schema": 1, "files": copied}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(destination, "STAGED.json"), append(manifest, '\n'), 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(destination, ".gitkeep"), nil, 0o644)
}

// pinnedInterpreterVersion is the version of the interpreter the Python environment layer is built on.
func pinnedInterpreterVersion(repo string) (string, error) {
	raw, err := os.ReadFile(filepath.Join(repo, filepath.FromSlash(interpreterManifest)))
	if err != nil {
		return "", err
	}
	var manifest struct {
		Interpreter struct {
			Version string `json:"version"`
		} `json:"interpreter"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return "", fmt.Errorf("parse %s: %w", interpreterManifest, err)
	}
	if manifest.Interpreter.Version == "" {
		return "", fmt.Errorf("%s names no interpreter version", interpreterManifest)
	}
	return manifest.Interpreter.Version, nil
}

// precompile writes the engine packages' bytecode in unchecked-hash mode, so it stays valid
// wherever the resources are extracted and whatever their modification times; the
// engine process then never writes bytecode of its own. Only the pinned interpreter
// version is accepted: its bytecode is exactly what the layer's interpreter would write.
// The recorded source path is the relative one, so no build-machine path ends up in
// a file, and files are compiled one after another, so two stagings are identical.
func precompile(repo, destination, python string) ([]stagedFile, error) {
	pinned, err := pinnedInterpreterVersion(repo)
	if err != nil {
		return nil, err
	}
	version, err := run(python, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])")
	if err != nil {
		return nil, fmt.Errorf("precompile the engine: %s does not run: %w", python, err)
	}
	if version != pinned {
		return nil, fmt.Errorf("precompile the engine: %s is Python %s, the environment layer pins %s", python, version, pinned)
	}
	var compiled []stagedFile
	for _, relative := range engineSources() {
		source := filepath.Join(destination, filepath.FromSlash(relative))
		if _, err := run(python, "-I", "-m", "compileall", "-q", "--invalidation-mode", "unchecked-hash",
			"-d", relative, source); err != nil {
			return nil, fmt.Errorf("precompile the engine: %w", err)
		}
		err = filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".pyc") {
				return nil
			}
			record, err := describe(path, destination)
			if err != nil {
				return err
			}
			compiled = append(compiled, record)
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return compiled, nil
}

func run(name string, arguments ...string) (string, error) {
	command := exec.Command(name, arguments...)
	var output bytes.Buffer
	command.Stdout = &output
	command.Stderr = &output
	err := command.Run()
	text := strings.TrimSpace(output.String())
	if err != nil {
		return "", fmt.Errorf("%w: %s", err, text)
	}
	return text, nil
}

func describe(path, root string) (stagedFile, error) {
	input, err := os.Open(path)
	if err != nil {
		return stagedFile{}, err
	}
	defer input.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, input)
	if err != nil {
		return stagedFile{}, err
	}
	relative, err := filepath.Rel(root, path)
	if err != nil {
		return stagedFile{}, err
	}
	return stagedFile{Path: filepath.ToSlash(relative), SHA256: fmt.Sprintf("%x", hash.Sum(nil)), Size: size}, nil
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
