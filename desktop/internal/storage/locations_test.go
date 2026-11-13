package storage

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDirectoryFallsBackToTheDataDirectory(t *testing.T) {
	data := t.TempDir()
	if got, want := Directory(data, RuntimeTarget), filepath.Join(data, "runtime"); got != want {
		t.Fatalf("runtime directory = %q, want %q", got, want)
	}
	if got, want := Directory(data, VideoTarget), filepath.Join(data, "videos"); got != want {
		t.Fatalf("video directory = %q, want %q", got, want)
	}
}

func TestSetRecordsAndRestoresLocations(t *testing.T) {
	data := t.TempDir()
	elsewhere := filepath.Join(t.TempDir(), "Nonoka X", "runtime")
	if _, err := Set(data, RuntimeTarget, elsewhere); err != nil {
		t.Fatalf("set runtime location: %v", err)
	}
	if got := RuntimeDirectory(data); got != elsewhere {
		t.Fatalf("runtime directory = %q, want %q", got, elsewhere)
	}
	// The video directory is independent of the runtime one.
	if got, want := VideoDirectory(data), filepath.Join(data, "videos"); got != want {
		t.Fatalf("video directory = %q, want %q", got, want)
	}
	if _, err := Set(data, RuntimeTarget, ""); err != nil {
		t.Fatalf("reset runtime location: %v", err)
	}
	if got, want := RuntimeDirectory(data), filepath.Join(data, "runtime"); got != want {
		t.Fatalf("runtime directory after reset = %q, want %q", got, want)
	}
}

// Recording the default as an absolute path would freeze the data directory in
// place: a user who later moves their profile would leave a record pointing at
// a directory that no longer exists.
func TestSetTreatsTheDefaultAsNoChoice(t *testing.T) {
	data := t.TempDir()
	locations, err := Set(data, VideoTarget, filepath.Join(data, "videos"))
	if err != nil {
		t.Fatalf("set video location: %v", err)
	}
	if locations.VideoDir != "" {
		t.Fatalf("recorded video directory = %q, want it left unset", locations.VideoDir)
	}
}

func TestLoadIgnoresUnusableRecords(t *testing.T) {
	data := t.TempDir()
	record := filepath.Join(data, "storage.json")
	if err := os.WriteFile(record, []byte(`{"schema":1,"runtimeDir":"relative/path","videoDir":""}`), 0o600); err != nil {
		t.Fatalf("write record: %v", err)
	}
	if got := Load(data).RuntimeDir; got != "" {
		t.Fatalf("relative record survived as %q", got)
	}
	if err := os.WriteFile(record, []byte("not json"), 0o600); err != nil {
		t.Fatalf("write record: %v", err)
	}
	if got, want := RuntimeDirectory(data), filepath.Join(data, "runtime"); got != want {
		t.Fatalf("runtime directory = %q, want the default %q", got, want)
	}
}

func TestDestinationForAddsOneContainer(t *testing.T) {
	root := t.TempDir()
	destination, err := DestinationFor(root, RuntimeTarget)
	if err != nil {
		t.Fatalf("destination: %v", err)
	}
	if want := filepath.Join(root, "Nonoka X", "runtime"); destination != want {
		t.Fatalf("destination = %q, want %q", destination, want)
	}
	// Picking the container a previous move made must not nest another one.
	again, err := DestinationFor(filepath.Join(root, "Nonoka X"), VideoTarget)
	if err != nil {
		t.Fatalf("destination: %v", err)
	}
	if want := filepath.Join(root, "Nonoka X", "videos"); again != want {
		t.Fatalf("destination = %q, want %q", again, want)
	}
}

func TestValidateDestinationRejectsUnusableTargets(t *testing.T) {
	data := t.TempDir()
	current := Directory(data, RuntimeTarget)
	if err := os.MkdirAll(current, 0o755); err != nil {
		t.Fatalf("create current: %v", err)
	}
	for name, destination := range map[string]string{
		"the current directory": current,
		"inside itself":         filepath.Join(current, "nested"),
		"the data directory":    data,
		"the video directory":   Directory(data, VideoTarget),
	} {
		if err := ValidateDestination(data, RuntimeTarget, destination); err == nil {
			t.Fatalf("%s was accepted as a destination", name)
		}
	}
	fresh := filepath.Join(t.TempDir(), "Nonoka X", "runtime")
	if err := ValidateDestination(data, RuntimeTarget, fresh); err != nil {
		t.Fatalf("fresh destination rejected: %v", err)
	}
}

func TestValidateDestinationRejectsANonEmptyDirectory(t *testing.T) {
	data := t.TempDir()
	destination := filepath.Join(t.TempDir(), "Nonoka X", "videos")
	if err := os.MkdirAll(destination, 0o755); err != nil {
		t.Fatalf("create destination: %v", err)
	}
	if err := ValidateDestination(data, VideoTarget, destination); err != nil {
		t.Fatalf("empty destination rejected: %v", err)
	}
	if err := os.WriteFile(filepath.Join(destination, "keep.txt"), []byte("x"), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	err := ValidateDestination(data, VideoTarget, destination)
	if err == nil || !strings.Contains(err.Error(), "不为空") {
		t.Fatalf("non-empty destination error = %v, want a not-empty refusal", err)
	}
}

// A relocation that was interrupted leaves a marked, non-empty destination.
// Refusing it would force a fresh copy of everything already carried across.
func TestValidateDestinationAcceptsItsOwnPartialCopy(t *testing.T) {
	data := t.TempDir()
	source := Directory(data, RuntimeTarget)
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatalf("create source: %v", err)
	}
	destination := filepath.Join(t.TempDir(), "Nonoka X", "runtime")
	if err := os.MkdirAll(destination, 0o755); err != nil {
		t.Fatalf("create destination: %v", err)
	}
	if err := os.WriteFile(filepath.Join(destination, "carried.bin"), []byte("weights"), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	if err := ValidateDestination(data, RuntimeTarget, destination); err == nil {
		t.Fatal("an unmarked non-empty directory was accepted")
	}
	if err := writePartial(source, destination); err != nil {
		t.Fatalf("write marker: %v", err)
	}
	if err := ValidateDestination(data, RuntimeTarget, destination); err != nil {
		t.Fatalf("own partial copy rejected: %v", err)
	}
}
