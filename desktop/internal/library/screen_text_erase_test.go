package library

import (
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestErasedOutputPathIsUniquePerEraseAndNamesItsMedia(t *testing.T) {
	dir := t.TempDir()
	now := time.UnixMilli(1700000000000)
	first := erasedOutputPath(dir, "loc_0123456789ab", now)
	if first != filepath.Join(dir, "loc_0123456789ab-1700000000000.mp4") {
		t.Fatalf("first erased path = %q", first)
	}
	if err := os.WriteFile(first, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	second := erasedOutputPath(dir, "loc_0123456789ab", now)
	if second == first {
		t.Fatal("a second erase must not reuse a name the player may have open")
	}
	if erasedFileID(filepath.Base(second)) != "loc_0123456789ab" || erasedFileID("loc_0123456789ab.mp4") != "" {
		t.Fatal("erasedFileID does not tell erased copies apart")
	}
}

func TestBuildEraseMuxArgsCopiesPictureAndTakesOriginalAudio(t *testing.T) {
	copied := strings.Join(buildEraseMuxArgs("cleaned.mp4", "original.mkv", "out.part.mp4", false), " ")
	if !strings.Contains(copied, "-i cleaned.mp4 -i original.mkv -map 0:v:0 -map 1:a? -sn -dn -c:v copy -c:a copy") {
		t.Fatalf("unexpected mux args: %s", copied)
	}
	if reencoded := strings.Join(buildEraseMuxArgs("a", "b", "c", true), " "); !strings.Contains(reencoded, "-c:a aac") {
		t.Fatalf("fallback must re-encode audio: %s", reencoded)
	}
}

func TestUseErasedVideoAndRestoreOriginal(t *testing.T) {
	root := t.TempDir()
	original := filepath.Join(root, "show.mp4")
	if err := os.WriteFile(original, []byte("original"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 12, Width: 1280, Height: 720, HasVideo: true, HasAudio: true}}
	data := filepath.Join(root, "data")
	service, err := newServiceWithTools(data, tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	entry := service.Import([]string{original}).Added[0]
	if err := os.MkdirAll(service.erasedDirectory(), 0o755); err != nil {
		t.Fatal(err)
	}
	earlier := erasedOutputPath(service.erasedDirectory(), entry.ID, time.UnixMilli(1))
	erased := erasedOutputPath(service.erasedDirectory(), entry.ID, time.UnixMilli(2))
	for _, path := range []string{earlier, erased} {
		if err := os.WriteFile(path, []byte("erased picture "+path), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	document := filepath.Join(data, "documents", entry.ID)
	if err := os.MkdirAll(document, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "document.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A stale working copy of the old picture would win over the new source.
	if _, err := service.CacheMedia(entry.ID); err != nil {
		t.Fatal(err)
	}

	if _, err := service.UseErasedVideo(entry.ID, original); err == nil {
		t.Fatal("the original itself must not be accepted as its erased copy")
	}
	used, err := service.UseErasedVideo(entry.ID, erased)
	if err != nil {
		t.Fatal(err)
	}
	if used.SourcePath != erased || used.ErasedFrom != original {
		t.Fatalf("after erase: source %q, erasedFrom %q", used.SourcePath, used.ErasedFrom)
	}
	if used.Fingerprint != entry.Fingerprint {
		t.Fatal("the erased copy must keep the subtitle record's fingerprint")
	}
	if cached := service.cachedPath(entry.ID); cached != "" {
		t.Fatalf("stale cache copy survived: %s", cached)
	}
	path, _, _, _, err := service.ResolveMedia(entry.ID)
	if err != nil || path != erased {
		t.Fatalf("resolve after erase = %q, %v", path, err)
	}
	if fileExists(earlier) {
		t.Fatal("an earlier erased copy of the same video must be dropped")
	}
	// The erased copy is in the cache already: tasks must not copy it a second time.
	if working, err := service.CacheMedia(entry.ID); err != nil || working != erased {
		t.Fatalf("cache media after erase = %q, %v", working, err)
	}
	if cached := service.cachedPath(entry.ID); cached != "" {
		t.Fatalf("erased copy was duplicated into the cache: %s", cached)
	}
	// Clearing the cache must not take the entry's source with it.
	if _, err := service.ClearVideoCache(); err != nil {
		t.Fatal(err)
	}
	if !fileExists(erased) {
		t.Fatal("clearing the video cache removed the erased copy")
	}

	// Moving the cache directory takes the erased copy along.
	moved := filepath.Join(root, "moved-cache")
	if err := os.MkdirAll(filepath.Join(moved, erasedDirName), 0o755); err != nil {
		t.Fatal(err)
	}
	relocated := filepath.Join(moved, erasedDirName, filepath.Base(erased))
	if err := os.Rename(erased, relocated); err != nil {
		t.Fatal(err)
	}
	if err := SetCacheDirectory(service, moved); err != nil {
		t.Fatal(err)
	}
	if got, _ := service.entryByID(entry.ID); got.SourcePath != relocated {
		t.Fatalf("source after cache move = %q, want %q", got.SourcePath, relocated)
	}

	restored, err := service.RestoreOriginalVideo(entry.ID)
	if err != nil {
		t.Fatal(err)
	}
	if restored.SourcePath != original || restored.ErasedFrom != "" {
		t.Fatalf("after restore: source %q, erasedFrom %q", restored.SourcePath, restored.ErasedFrom)
	}
	if fileExists(relocated) {
		t.Fatal("restoring must delete the erased copy from the cache")
	}
	if !fileExists(original) {
		t.Fatal("the original must never be touched")
	}
}

func TestEraseScreenTextRejectsEmptyPlates(t *testing.T) {
	service := &Service{}
	if _, err := service.EraseScreenText("loc_0123456789ab", "  ", "auto", "default"); err == nil {
		t.Fatal("expected an error without cover boxes")
	}
}

// The player keeps what it buffered from an address: when another file takes
// the id's place, the address must change with it.
func TestMediaURLChangesWhenTheServedFileChanges(t *testing.T) {
	root := t.TempDir()
	id := "loc_0123456789ab"
	first := filepath.Join(root, "first.mp4")
	second := filepath.Join(root, "second.mp4")
	if err := os.WriteFile(first, []byte("original picture"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(second, []byte("erased"), 0o600); err != nil {
		t.Fatal(err)
	}
	service := &Service{root: root, cacheDir: filepath.Join(root, "videos"), indexPath: filepath.Join(root, "library.json"),
		entries: []Entry{{ID: id, SourcePath: first}}}
	defer service.Close()
	before, err := service.MediaURL(id)
	if err != nil {
		t.Fatal(err)
	}
	again, _ := service.MediaURL(id)
	if again != before {
		t.Fatalf("the same file must keep its address: %q then %q", before, again)
	}
	service.entries[0].SourcePath = second
	after, err := service.MediaURL(id)
	if err != nil {
		t.Fatal(err)
	}
	if after == before {
		t.Fatal("a different file under the same id kept the old address")
	}
	response, err := http.Get(after)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK || string(body) != "erased" {
		t.Fatalf("versioned address served %d %q", response.StatusCode, body)
	}
}
