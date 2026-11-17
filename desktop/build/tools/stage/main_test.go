package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// stagingInterpreter is the interpreter the tests stage with: NONOKA_STAGE_PYTHON, else
// `python`. Skipped unless it is the pinned version, which is all `stage` accepts.
func stagingInterpreter(t *testing.T, repo string) string {
	t.Helper()
	python := os.Getenv("NONOKA_STAGE_PYTHON")
	if python == "" {
		python = "python"
	}
	if _, err := exec.LookPath(python); err != nil {
		t.Skipf("no %s to precompile with", python)
	}
	pinned, err := pinnedInterpreterVersion(repo)
	if err != nil {
		t.Fatal(err)
	}
	version, err := run(python, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])")
	if err != nil || version != pinned {
		t.Skipf("%s is %q, not the pinned %s", python, version, pinned)
	}
	return python
}

func repositoryRoot(t *testing.T) string {
	t.Helper()
	repo, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(repo, filepath.FromSlash(engineSource))); err != nil {
		t.Fatalf("not the repository root: %s", repo)
	}
	return repo
}

type manifest struct {
	Files []stagedFile `json:"files"`
}

func readManifest(t *testing.T, destination string) ([]byte, manifest) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(destination, "STAGED.json"))
	if err != nil {
		t.Fatal(err)
	}
	var decoded manifest
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	return raw, decoded
}

func TestStagingTwiceGivesTheSameFiles(t *testing.T) {
	repo := repositoryRoot(t)
	python := stagingInterpreter(t, repo)
	first, second := filepath.Join(t.TempDir(), "a"), filepath.Join(t.TempDir(), "b")
	for _, destination := range []string{first, second} {
		if err := stage(repo, destination, python); err != nil {
			t.Fatal(err)
		}
	}
	one, _ := readManifest(t, first)
	two, _ := readManifest(t, second)
	if !bytes.Equal(one, two) {
		t.Fatal("two stagings of the same tree differ")
	}
}

func TestTheEngineIsStagedPrecompiledAndNothingElse(t *testing.T) {
	repo := repositoryRoot(t)
	python := stagingInterpreter(t, repo)
	destination := t.TempDir()
	if err := stage(repo, destination, python); err != nil {
		t.Fatal(err)
	}
	_, staged := readManifest(t, destination)
	sources := map[string]bool{}
	compiled := map[string]bool{}
	for _, file := range staged.Files {
		switch {
		case strings.HasSuffix(file.Path, ".pyc"):
			if !strings.HasPrefix(file.Path, engineSource+"/") {
				t.Errorf("bytecode outside the engine code: %s", file.Path)
			}
			compiled[file.Path] = true
		case strings.HasPrefix(file.Path, engineSource+"/") && strings.HasSuffix(file.Path, ".py"):
			sources[file.Path] = true
		}
		if strings.HasPrefix(file.Path, "third_party/finesub/") && !strings.HasPrefix(file.Path, engineSource+"/") {
			switch file.Path {
			case "third_party/finesub/LICENSE", "third_party/finesub/LICENSES.json",
				"third_party/finesub/PROMPT_LICENSE.md", "third_party/finesub/UPSTREAM.json":
			default:
				t.Errorf("not a core file of the engine: %s", file.Path)
			}
		}
	}
	if len(sources) == 0 {
		t.Fatal("no engine source staged")
	}
	tag := "cpython-312"
	for source := range sources {
		directory, name := filepath.ToSlash(filepath.Dir(source)), strings.TrimSuffix(filepath.Base(source), ".py")
		bytecode := directory + "/__pycache__/" + name + "." + tag + ".pyc"
		if !compiled[bytecode] {
			t.Errorf("%s was not precompiled", source)
			continue
		}
		raw, err := os.ReadFile(filepath.Join(destination, filepath.FromSlash(bytecode)))
		if err != nil {
			t.Fatal(err)
		}
		// PEP 552: flags 0b01 is a hash-based pyc whose source is never checked.
		if flags := binary.LittleEndian.Uint32(raw[4:8]); flags != 0b01 {
			t.Errorf("%s: flags %#b, want unchecked-hash", bytecode, flags)
		}
		if bytes.Contains(raw, []byte(destination)) || bytes.Contains(raw, []byte(filepath.ToSlash(destination))) {
			t.Errorf("%s records the staging directory", bytecode)
		}
	}
	if len(compiled) != len(sources) {
		t.Errorf("%d bytecode files for %d sources", len(compiled), len(sources))
	}
}

func TestAnotherInterpreterVersionIsRefused(t *testing.T) {
	repo := repositoryRoot(t)
	python := filepath.Join(t.TempDir(), "python")
	script := "#!/bin/sh\necho 3.11.9\n"
	name := python
	if filepath.Separator == '\\' {
		name = python + ".cmd"
		script = "@echo 3.11.9\r\n"
	}
	if err := os.WriteFile(name, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	err := stage(repo, filepath.Join(t.TempDir(), "out"), name)
	if err == nil || !strings.Contains(err.Error(), "3.11.9") {
		t.Fatalf("want a refusal naming the version, got %v", err)
	}
}
