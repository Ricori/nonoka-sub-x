package taskhistory

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestHistoryPersistsOutsidePreferences(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	saved, err := service.Save([]any{map[string]any{"taskId": "task-1", "provider": "local"}})
	if err != nil || len(saved) != 1 || saved[0]["taskId"] != "task-1" {
		t.Fatalf("saved = %#v, %v", saved, err)
	}
	if _, err := os.Stat(filepath.Join(root, "preferences.json")); !os.IsNotExist(err) {
		t.Fatalf("history touched preferences.json: %v", err)
	}
	info, err := os.Stat(filepath.Join(root, "task-history.json"))
	if err != nil || (runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0) {
		t.Fatalf("history permissions = %v, %v", info.Mode().Perm(), err)
	}
	reloaded, err := New(root)
	if err != nil || len(reloaded.Get()) != 1 || reloaded.Get()[0]["taskId"] != "task-1" {
		t.Fatalf("reloaded = %#v, %v", reloaded.Get(), err)
	}
	cleared, err := reloaded.Save([]any{})
	if err != nil || len(cleared) != 0 {
		t.Fatalf("cleared = %#v, %v", cleared, err)
	}
}

// The tasks page reads the engine a task ran on from its snapshot (WP6). The
// history keeps every entry as the renderer wrote it, so the field survives a
// save and a reload untouched.
func TestHistoryKeepsTheTranscriptionEngine(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	entry := map[string]any{
		"taskId": "task-1", "provider": "local", "mediaId": "loc_0123456789ab", "title": "clip",
		"snapshot": map[string]any{"schema": 1, "task_id": "task-1", "provider": "local", "state": "completed", "transcription_engine": "mai2"},
	}
	if _, err := service.Save([]any{entry}); err != nil {
		t.Fatal(err)
	}
	reloaded, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	entries := reloaded.Get()
	snapshot, _ := entries[0]["snapshot"].(map[string]any)
	if len(entries) != 1 || snapshot["transcription_engine"] != "mai2" {
		t.Fatalf("reloaded = %#v", entries)
	}
}

func TestHistoryMigratesFromLegacyPreferences(t *testing.T) {
	root := t.TempDir()
	legacy := map[string]any{
		"homeTheme":   "dark",
		"taskHistory": []any{map[string]any{"taskId": "task-legacy", "provider": "cloud"}},
	}
	data, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "preferences.json"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	entries := service.Get()
	if len(entries) != 1 || entries[0]["taskId"] != "task-legacy" {
		t.Fatalf("migrated = %#v", entries)
	}
	// An empty history must not resurrect the legacy entries on the next launch.
	if _, err := service.Save([]any{}); err != nil {
		t.Fatal(err)
	}
	reloaded, err := New(root)
	if err != nil || len(reloaded.Get()) != 0 {
		t.Fatalf("reloaded = %#v, %v", reloaded.Get(), err)
	}
}

func TestHistoryRejectsOversizedAndInvalidEntries(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Save([]any{"not-a-record"}); err == nil {
		t.Fatal("a non-object entry was accepted")
	}
	oversized := make([]any, 0, Limit)
	for index := 0; index < Limit; index++ {
		oversized = append(oversized, map[string]any{"taskId": string(make([]byte, 32<<10))})
	}
	if _, err := service.Save(oversized); err == nil {
		t.Fatal("an oversized history was accepted")
	}
	overLimit := make([]any, 0, Limit+5)
	for index := 0; index < Limit+5; index++ {
		overLimit = append(overLimit, map[string]any{"taskId": "task"})
	}
	saved, err := service.Save(overLimit)
	if err != nil || len(saved) != Limit {
		t.Fatalf("saved %d entries, %v", len(saved), err)
	}
}
