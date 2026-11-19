package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

func TestSidecarConfigHonoursExplicitPaths(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "run.py")
	engines := filepath.Join(root, "engines")
	data := filepath.Join(root, "tasks")
	if err := os.WriteFile(script, []byte("# fixture\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(engines, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv(managedtools.PythonEnvironment, "fixture-python")
	t.Setenv(scriptEnvironment, script)
	t.Setenv(enginesEnvironment, engines)
	t.Setenv(dataEnvironment, data)

	config, err := SidecarConfig()
	if err != nil {
		t.Fatalf("resolve config: %v", err)
	}
	if config.Executable != "fixture-python" || config.Args[0] != script || config.Args[2] != data ||
		config.Args[3] != "--engines" || config.Args[4] != engines {
		t.Fatalf("unexpected config: %#v", config)
	}
}

func TestSidecarConfigStartsWithoutEnginePackages(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "run.py")
	if err := os.WriteFile(script, []byte("# fixture\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)
	t.Setenv(managedtools.PythonEnvironment, "fixture-python")
	t.Setenv(scriptEnvironment, script)
	t.Setenv(enginesEnvironment, "")
	t.Setenv(dataEnvironment, filepath.Join(root, "tasks"))

	config, err := SidecarConfig()
	if err != nil {
		t.Fatalf("a missing engines directory must not stop the sidecar: %v", err)
	}
	for _, argument := range config.Args {
		if argument == "--engines" || argument == "--vendor" {
			t.Fatalf("passed %s with no engines directory: %v", argument, config.Args)
		}
	}
}

func TestFirstUsableExecutablePrefersBundledRuntime(t *testing.T) {
	root := t.TempDir()
	bundled := filepath.Join(root, "python")
	if err := os.WriteFile(bundled, []byte("fixture"), 0o700); err != nil {
		t.Fatal(err)
	}
	usable := func(string) bool { return true }
	if got := firstUsableExecutable(usable, filepath.Join(root, "missing"), bundled); got != bundled {
		t.Fatalf("expected bundled runtime %q, got %q", bundled, got)
	}
}

// An interpreter without httpx/pydantic starts a sidecar that answers requests
// but cannot build its provisioner, which is how the runtime page ended up with
// every button disabled and no way to install the managed Python.
func TestFirstUsableExecutableSkipsInterpretersMissingDependencies(t *testing.T) {
	root := t.TempDir()
	bare := filepath.Join(root, "bare-python")
	equipped := filepath.Join(root, "equipped-python")
	for _, path := range []string{bare, equipped} {
		if err := os.WriteFile(path, []byte("fixture"), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	usable := func(candidate string) bool { return candidate == equipped }
	if got := firstUsableExecutable(usable, bare, equipped); got != equipped {
		t.Fatalf("expected the equipped interpreter %q, got %q", equipped, got)
	}
	if got := firstUsableExecutable(func(string) bool { return false }, bare); got != "" {
		t.Fatalf("expected no interpreter, got %q", got)
	}
}

func TestInterpreterProbeRejectsSomethingThatIsNotAnInterpreter(t *testing.T) {
	fake := filepath.Join(t.TempDir(), "python")
	if err := os.WriteFile(fake, []byte("not an interpreter"), 0o700); err != nil {
		t.Fatal(err)
	}
	if interpreterCanRunSidecar(fake) {
		t.Fatal("a file that cannot be executed was accepted as an interpreter")
	}
}

// Only 3.12.x is accepted. Any other interpreter that happens to carry httpx and
// pydantic — 3.11 as much as 3.13 — must still hand over to the managed Python.
func TestInterpreterProbeScriptPinsTheExactVersion(t *testing.T) {
	for _, fragment := range []string{"sys.version_info[:2] == (3, 12)", "import httpx, pydantic"} {
		if !strings.Contains(interpreterProbeScript, fragment) {
			t.Fatalf("probe script lost %q: %s", fragment, interpreterProbeScript)
		}
	}
}

// External interpreters (PATH / bundled) are not topped up by TopUpLauncher, so
// they must carry opencc and (on Windows) tzdata as well; otherwise LLM test /
// correction fails on ZoneInfo("America/Los_Angeles").
func TestFullInterpreterProbeScriptRequiresHarnessPackages(t *testing.T) {
	for _, fragment := range []string{
		"sys.version_info[:2] == (3, 12)",
		"import httpx, pydantic, opencc",
		"tzdata",
	} {
		if !strings.Contains(fullInterpreterProbeScript, fragment) {
			t.Fatalf("full probe script lost %q: %s", fragment, fullInterpreterProbeScript)
		}
	}
}

func TestFirstConfiguredPathPreservesAbsoluteCandidate(t *testing.T) {
	root := t.TempDir()
	candidate := filepath.Join(root, "resource.py")
	if err := os.WriteFile(candidate, []byte("fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	const environment = "NONOKA_TEST_RESOURCE_PATH"
	t.Setenv(environment, "")
	if got := firstConfiguredPath(environment, filepath.Join(root, "working"), candidate); got != candidate {
		t.Fatalf("expected absolute candidate %q, got %q", candidate, got)
	}
}

func TestPlatformDataDirectoryUsesNonokaXApplicationDataRoot(t *testing.T) {
	windowsRoot := filepath.Join("C:", "Users", "chika", "AppData", "Roaming")
	windows, err := platformDataDirectory("windows", windowsRoot, "")
	if err != nil || windows != filepath.Join(windowsRoot, "Nonoka X") {
		t.Fatalf("windows data directory = %q, %v", windows, err)
	}
	mac, err := platformDataDirectory("darwin", "", "/Users/chika")
	expectedMac := filepath.Join("/Users/chika", "Library", "Application Support", "Nonoka X")
	if err != nil || mac != expectedMac {
		t.Fatalf("macOS data directory = %q, %v", mac, err)
	}
	if _, err := platformDataDirectory("linux", "", "/home/chika"); err == nil {
		t.Fatal("unsupported desktop platform was accepted")
	}
}

func TestMigrateDataDirectoryMovesFinokaRootAndRequiresRestart(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "Finoka")
	if err := os.MkdirAll(filepath.Join(source, "system-plugins", "dev.finoka.test"), 0o755); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(source, "library.json")
	if err := os.WriteFile(marker, []byte("[]"), 0o600); err != nil {
		t.Fatal(err)
	}
	legacyRegistry := filepath.Join(source, "plugin-registry.json")
	if err := os.WriteFile(legacyRegistry, []byte(`{"enabled":{"dev.finoka.test":false}}`), 0o600); err != nil {
		t.Fatal(err)
	}

	migration, err := migrateDataDirectory("windows", root, "")
	if err != nil {
		t.Fatal(err)
	}
	if migration == nil || migration.Source != source {
		t.Fatalf("migration = %#v", migration)
	}
	destination := filepath.Join(root, "Nonoka X")
	if migration.Destination != destination {
		t.Fatalf("destination = %q, want %q", migration.Destination, destination)
	}
	if _, err := os.Stat(filepath.Join(destination, "library.json")); err != nil {
		t.Fatalf("migrated marker: %v", err)
	}
	if _, err := os.Stat(filepath.Join(destination, "plugin-registry.json")); !os.IsNotExist(err) {
		t.Fatalf("plugin-registry.json should be removed on migration: %v", err)
	}
	if _, err := os.Stat(filepath.Join(destination, "system-plugins")); !os.IsNotExist(err) {
		t.Fatalf("system-plugins should be removed on migration: %v", err)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatalf("legacy source still exists: %v", err)
	}

	again, err := migrateDataDirectory("windows", root, "")
	if err != nil || again != nil {
		t.Fatalf("second migration = %#v, %v", again, err)
	}
}

func TestMigrateDataDirectoryDoesNotMergeExistingDestination(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{"Finoka", "Nonoka X"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	migration, err := migrateDataDirectory("windows", root, "")
	if err != nil || migration != nil {
		t.Fatalf("migration = %#v, %v", migration, err)
	}
	if _, err := os.Stat(filepath.Join(root, "Finoka")); err != nil {
		t.Fatalf("source should remain untouched: %v", err)
	}
}

func TestMigrateDataDirectoryRetriesWhileFileIsLocked(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "Finoka")
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(source, "library.json")
	if err := os.WriteFile(marker, []byte("[]"), 0o600); err != nil {
		t.Fatal(err)
	}

	file, err := os.OpenFile(marker, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = file.Close()
	}()

	migration, err := migrateDataDirectory("windows", root, "")
	if err != nil {
		t.Fatalf("migration failed with retry: %v", err)
	}
	if migration == nil || migration.Source != source {
		t.Fatalf("migration = %#v", migration)
	}
	destination := filepath.Join(root, "Nonoka X")
	if _, err := os.Stat(filepath.Join(destination, "library.json")); err != nil {
		t.Fatalf("migrated marker: %v", err)
	}
}

