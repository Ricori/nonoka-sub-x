package plugins

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

func TestPluginStatePersistsEmptyValuesAndDeletes(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	installStateTestPlugin(t, service, "dev.nonoka.state", true)

	missing, err := service.StateGet("dev.nonoka.state", "missing")
	if err != nil || missing != nil {
		t.Fatalf("missing state should return nil: %q, %v", valueOrEmpty(missing), err)
	}
	empty := ""
	if err := service.StateSet("dev.nonoka.state", "empty", &empty); err != nil {
		t.Fatal(err)
	}
	answer := "persisted"
	if err := service.StateSet("dev.nonoka.state", "answer", &answer); err != nil {
		t.Fatal(err)
	}
	stored, err := service.StateGet("dev.nonoka.state", "empty")
	if err != nil || stored == nil || *stored != "" {
		t.Fatalf("stored empty string was not preserved: %q, %v", valueOrEmpty(stored), err)
	}
	keys, err := service.StateKeys("dev.nonoka.state")
	if err != nil || !reflect.DeepEqual(keys, []string{"answer", "empty"}) {
		t.Fatalf("unexpected state keys: %#v, %v", keys, err)
	}

	// A fresh service instance reads the same plugin-private state from disk.
	restarted, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	stored, err = restarted.StateGet("dev.nonoka.state", "answer")
	if err != nil || stored == nil || *stored != answer {
		t.Fatalf("state did not survive restart: %q, %v", valueOrEmpty(stored), err)
	}
	if err := restarted.StateSet("dev.nonoka.state", "answer", nil); err != nil {
		t.Fatal(err)
	}
	stored, err = restarted.StateGet("dev.nonoka.state", "answer")
	if err != nil || stored != nil {
		t.Fatalf("deleted state should return nil: %q, %v", valueOrEmpty(stored), err)
	}

	data, err := os.ReadFile(filepath.Join(root, "plugin-data", "dev.nonoka.state", stateFileName))
	if err != nil {
		t.Fatal(err)
	}
	decoded := map[string]string{}
	if err := json.Unmarshal(data, &decoded); err != nil || decoded["empty"] != "" {
		t.Fatalf("state file is not the expected string map: %#v, %v", decoded, err)
	}
}

func TestPluginStateRequiresPermissionAndIsIsolated(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	installStateTestPlugin(t, service, "dev.nonoka.first", true)
	installStateTestPlugin(t, service, "dev.nonoka.second", true)
	installStateTestPlugin(t, service, "dev.nonoka.denied", false)

	value := "first only"
	if err := service.StateSet("dev.nonoka.first", "result", &value); err != nil {
		t.Fatal(err)
	}
	other, err := service.StateGet("dev.nonoka.second", "result")
	if err != nil || other != nil {
		t.Fatalf("state leaked across plugins: %q, %v", valueOrEmpty(other), err)
	}
	if _, err := service.StateGet("dev.nonoka.denied", "result"); err == nil {
		t.Fatal("state.get should require state.persist")
	}
	if err := service.StateSet("dev.nonoka.denied", "result", &value); err == nil {
		t.Fatal("state.set should require state.persist")
	}
	if _, err := service.StateKeys("dev.nonoka.denied"); err == nil {
		t.Fatal("state.keys should require state.persist")
	}
}

func TestPluginStateValidatesKeysAndKeepsPreviousDataOnOverflow(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	installStateTestPlugin(t, service, "dev.nonoka.state", true)
	value := "safe"
	if err := service.StateSet("dev.nonoka.state", "existing", &value); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"", "../escape", "Upper", "two words", strings.Repeat("a", 129)} {
		if err := service.StateSet("dev.nonoka.state", key, &value); err == nil {
			t.Fatalf("invalid state key was accepted: %q", key)
		}
	}

	tooLarge := strings.Repeat("x", maxStateBytes)
	if err := service.StateSet("dev.nonoka.state", "large", &tooLarge); err == nil || !strings.Contains(err.Error(), "10 MiB") {
		t.Fatalf("oversized state should be rejected clearly: %v", err)
	}
	stored, err := service.StateGet("dev.nonoka.state", "existing")
	if err != nil || stored == nil || *stored != value {
		t.Fatalf("overflow changed previously stored state: %q, %v", valueOrEmpty(stored), err)
	}
}

func TestPluginStateConcurrentUpdatesDoNotLoseKeys(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	installStateTestPlugin(t, service, "dev.nonoka.state", true)

	const count = 32
	var wait sync.WaitGroup
	errors := make(chan error, count)
	for index := 0; index < count; index++ {
		wait.Add(1)
		go func(index int) {
			defer wait.Done()
			key := "key-" + strings.Repeat("a", index+1)
			value := key
			errors <- service.StateSet("dev.nonoka.state", key, &value)
		}(index)
	}
	wait.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	keys, err := service.StateKeys("dev.nonoka.state")
	if err != nil || len(keys) != count {
		t.Fatalf("concurrent state updates were lost: %d keys, %v", len(keys), err)
	}
}

func TestPluginStateRejectsMalformedFile(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	installStateTestPlugin(t, service, "dev.nonoka.state", true)
	path := filepath.Join(root, "plugin-data", "dev.nonoka.state", stateFileName)
	mustWrite(t, path, "null\n")
	value := "must not panic"
	if err := service.StateSet("dev.nonoka.state", "key", &value); err == nil || !strings.Contains(err.Error(), "JSON object") {
		t.Fatalf("malformed state file should be rejected: %v", err)
	}
}

func installStateTestPlugin(t *testing.T, service *Service, id string, permission bool) {
	t.Helper()
	manifest := strings.Replace(testManifest, "dev.nonoka.hello", id, 1)
	if permission {
		manifest = strings.Replace(manifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["state.persist"],`, 1)
	}
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	mustWrite(t, filepath.Join(packageRoot, manifestName), manifest)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>State</p>`)
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
