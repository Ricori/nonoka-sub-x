package preferences

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/Ricori/nonoka-x/desktop/internal/netproxy"
)

func TestLegacyDiagnosticsPreferenceIsDiscarded(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "preferences.json")
	if err := os.WriteFile(path, []byte(`{"shareDiagnostics":false,"diagnosticsEpoch":1,"homeTheme":"dark"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	service, err := New(root)
	if err != nil || service.Get().HomeTheme != "dark" {
		t.Fatalf("legacy preferences = %v", err)
	}
	if _, err := service.Save(map[string]any{"shareDiagnostics": false, "libraryView": "list"}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || strings.Contains(string(data), "shareDiagnostics") || strings.Contains(string(data), "diagnosticsEpoch") {
		t.Fatalf("obsolete diagnostics preference retained: %s, %v", data, err)
	}
	if service.Get().LibraryView != "list" {
		t.Fatal("regular preference update lost")
	}
}

func TestPreferencesPersistValidatedPartialUpdates(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	updated, err := service.Save(map[string]any{"homeTheme": "dark", "editorTheme": "light", "sidebarCollapsed": true})
	if err != nil || updated.HomeTheme != "dark" || updated.EditorTheme != "light" || !updated.SidebarCollapsed || updated.LibraryView != "grid" {
		t.Fatalf("updated = %#v, %v", updated, err)
	}
	reloaded, err := New(root)
	if err != nil || !reflect.DeepEqual(reloaded.Get(), updated) {
		t.Fatalf("reloaded = %#v, %v", reloaded.Get(), err)
	}
	if _, err := service.Save(map[string]any{"libraryView": "tiles"}); err == nil {
		t.Fatal("invalid view was accepted")
	}
	info, err := os.Stat(filepath.Join(root, "preferences.json"))
	if err != nil || (runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0) {
		t.Fatalf("preferences permissions = %v, %v", info.Mode().Perm(), err)
	}
}

func TestPreferencesDefaultsEditorToDarkTheme(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	state := service.Get()
	if state.HomeTheme != "light" || state.EditorTheme != "dark" {
		t.Fatalf("themes = %q / %q, want light / dark", state.HomeTheme, state.EditorTheme)
	}
}

func TestPreferencesSaveWindowThemesIndependently(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	state, err := service.Save(map[string]any{"editorTheme": "dark"})
	if err != nil {
		t.Fatal(err)
	}
	if state.HomeTheme != "light" || state.EditorTheme != "dark" {
		t.Fatalf("themes = %q / %q, want light / dark", state.HomeTheme, state.EditorTheme)
	}
	state, err = service.Save(map[string]any{"homeTheme": "dark"})
	if err != nil {
		t.Fatal(err)
	}
	if state.HomeTheme != "dark" || state.EditorTheme != "dark" {
		t.Fatalf("themes = %q / %q, want dark / dark", state.HomeTheme, state.EditorTheme)
	}
}

// Window bounds and two theme choices are worth one log line and the defaults,
// never a desktop that will not open. Same rule the cloud state files follow.
func TestDamagedPreferencesFallBackToDefaults(t *testing.T) {
	for _, body := range []string{"", "{", `{"homeTheme":`, "not json at all"} {
		root := t.TempDir()
		if err := os.WriteFile(filepath.Join(root, "preferences.json"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		service, err := New(root)
		if err != nil {
			t.Fatalf("New() with %q = %v, want a service that starts", body, err)
		}
		state := service.Get()
		if state.HomeTheme != "light" || state.EditorTheme != "dark" || state.LibraryView != "grid" {
			t.Fatalf("state for %q = %#v, want the first-launch defaults", body, state)
		}
		if state.Bounds == nil {
			t.Fatalf("bounds for %q are nil, want an empty map", body)
		}
	}
}

func TestProxyPreferencePersistsAndClears(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { netproxy.SetCustom(nil) })
	if service.Get().Proxy != nil {
		t.Fatal("a fresh install should follow the system proxy")
	}
	// The page sends numbers as JSON numbers, which arrive as float64.
	updated, err := service.Save(map[string]any{"proxy": map[string]any{"scheme": "SOCKS5", "host": " ", "port": float64(7891)}})
	if err != nil {
		t.Fatal(err)
	}
	want := netproxy.Custom{Scheme: "socks5", Host: "127.0.0.1", Port: 7891}
	if updated.Proxy == nil || *updated.Proxy != want {
		t.Fatalf("saved proxy = %#v", updated.Proxy)
	}
	if status := service.ProxyStatus(); status.Source != "custom" || status.Configured != "socks5://127.0.0.1:7891" {
		t.Fatalf("custom proxy not applied: %#v", status)
	}
	reloaded, err := New(root)
	if err != nil || reloaded.Get().Proxy == nil || *reloaded.Get().Proxy != want {
		t.Fatalf("custom proxy not persisted: %#v, %v", reloaded.Get().Proxy, err)
	}
	if _, err := service.Save(map[string]any{"proxy": map[string]any{"scheme": "http", "host": "127.0.0.1", "port": float64(0)}}); err == nil {
		t.Fatal("port 0 was accepted")
	}
	if service.Get().Proxy == nil || *service.Get().Proxy != want {
		t.Fatal("a rejected proxy replaced the saved one")
	}
	cleared, err := service.Save(map[string]any{"proxy": nil})
	if err != nil || cleared.Proxy != nil {
		t.Fatalf("proxy not cleared: %#v, %v", cleared.Proxy, err)
	}
	if status := service.ProxyStatus(); status.Source == "custom" {
		t.Fatalf("cleared proxy still applied: %#v", status)
	}
}

func TestInvalidStoredProxyFollowsSystem(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "preferences.json"), []byte(`{"proxy":{"scheme":"ftp","host":"x","port":21}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	service, err := New(root)
	t.Cleanup(func() { netproxy.SetCustom(nil) })
	if err != nil || service.Get().Proxy != nil {
		t.Fatalf("invalid stored proxy kept: %#v, %v", service.Get().Proxy, err)
	}
}
