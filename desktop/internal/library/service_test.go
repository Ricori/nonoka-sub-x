package library

import (
	"reflect"
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fixtureTools struct {
	metadata Metadata
	err      error
}

func (f fixtureTools) Probe(context.Context, string) (Metadata, error) {
	return f.metadata, f.err
}

func (f fixtureTools) Generate(_ context.Context, _, output string, _ float64) error {
	if f.err != nil {
		return f.err
	}
	return os.WriteFile(output, []byte("jpeg-fixture"), 0o600)
}

func TestImportPersistsLocalPathDeduplicatesAndCreatesThumbnail(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "source.mp4")
	if err := os.WriteFile(source, []byte("media-fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 42.5, Width: 1920, Height: 1080, HasVideo: true, HasAudio: true}}
	service, err := newServiceWithTools(filepath.Join(root, "data"), tools, tools)
	if err != nil {
		t.Fatal(err)
	}

	result := service.Import([]string{source, source})
	if len(result.Failed) != 0 || len(result.Added) != 1 {
		t.Fatalf("unexpected import result: %#v", result)
	}
	entries := service.List()
	if len(entries) != 1 {
		t.Fatalf("library size = %d", len(entries))
	}
	entry := entries[0]
	if entry.SourcePath != source || entry.Duration != 42.5 || !entry.ThumbnailAvailable {
		t.Fatalf("unexpected entry: %#v", entry)
	}
	if _, err := os.Stat(filepath.Join(root, "data", filepath.Base(source))); !os.IsNotExist(err) {
		t.Fatal("source media was copied into the application data directory")
	}
	thumbnail, err := service.ThumbnailDataURL(entry.ID)
	if err != nil || !strings.HasPrefix(thumbnail, "data:image/jpeg;base64,") {
		t.Fatalf("thumbnail = %q, %v", thumbnail, err)
	}

	reloaded, err := New(filepath.Join(root, "data"))
	if err != nil {
		t.Fatal(err)
	}
	if got := reloaded.List(); len(got) != 1 || got[0].Fingerprint != entry.Fingerprint {
		t.Fatalf("reloaded entries: %#v", got)
	}
	var disk []Entry
	data, err := os.ReadFile(filepath.Join(root, "data", "library.json"))
	if err != nil || json.Unmarshal(data, &disk) != nil || len(disk) != 1 {
		t.Fatalf("invalid persisted library: %s, %v", data, err)
	}
}

// Subtitles adopted from the cloud need somewhere to live before the video is
// anywhere on this machine. The placeholder is an ordinary entry with no source
// file, and importing that file later has to fill the same entry in rather than
// bounce off the fingerprint match.
func TestPlaceholderRecordsMediaWithoutAFileAndImportAttachesIt(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "source.mp4")
	if err := os.WriteFile(source, []byte("media-fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 42.5, Width: 1920, Height: 1080, HasVideo: true, HasAudio: true}}
	service, err := newServiceWithTools(filepath.Join(root, "data"), tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	stat, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	fingerprint, err := fileFingerprint(source, stat.Size())
	if err != nil {
		t.Fatal(err)
	}

	placeholder, err := service.AddPlaceholder("demo", fingerprint, 42.5)
	if err != nil || !validID(placeholder.ID) {
		t.Fatalf("placeholder = %#v, %v", placeholder, err)
	}
	listed := service.List()
	if len(listed) != 1 || listed[0].Available || listed[0].SourcePath != "" {
		t.Fatalf("listed placeholder = %#v", listed)
	}
	if again, err := service.AddPlaceholder("demo", fingerprint, 42.5); err != nil || again.ID != placeholder.ID {
		t.Fatalf("second placeholder = %#v, %v", again, err)
	}

	result := service.Import([]string{source})
	entries := service.List()
	if len(entries) != 1 || entries[0].ID != placeholder.ID {
		t.Fatalf("import created a second entry: %#v (%#v)", entries, result)
	}
	if !entries[0].Available || entries[0].SourcePath != source || entries[0].Width != 1920 || !entries[0].ThumbnailAvailable {
		t.Fatalf("attached entry = %#v", entries[0])
	}
}

// The cover of media this machine has no file of can only come from the cloud
// entry the subtitles were adopted from. It must never displace a frame cut
// from a real video, though: import and relink write one the moment a file is
// attached, and that is the better picture of the two.
func TestEnsureThumbnailCoversPlaceholdersWithoutReplacingLocalFrames(t *testing.T) {
	root := t.TempDir()
	tools := fixtureTools{metadata: Metadata{Duration: 42.5, Width: 1920, Height: 1080, HasVideo: true, HasAudio: true}}
	service, err := newServiceWithTools(filepath.Join(root, "data"), tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	jpegDataURL := func(image string) string {
		return "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString([]byte(image))
	}
	cover := jpegDataURL("\xFF\xD8\xFFcover")

	placeholder, err := service.AddPlaceholder("demo", "cloud-fingerprint", 42.5)
	if err != nil {
		t.Fatal(err)
	}
	if err := service.EnsureThumbnail(placeholder.ID, cover); err != nil {
		t.Fatal(err)
	}
	if stored, err := service.ThumbnailDataURL(placeholder.ID); err != nil || stored != cover {
		t.Fatalf("stored cover = %q, %v", stored, err)
	}
	if listed := service.List(); len(listed) != 1 || !listed[0].ThumbnailAvailable {
		t.Fatalf("listed placeholder = %#v", listed)
	}
	// Re-adopting the same subtitles, or adopting a second entry onto media that
	// already has its own frame, leaves the cover that is there.
	if err := service.EnsureThumbnail(placeholder.ID, jpegDataURL("\xFF\xD8\xFFother")); err != nil {
		t.Fatal(err)
	}
	if stored, _ := service.ThumbnailDataURL(placeholder.ID); stored != cover {
		t.Fatalf("cover was replaced: %q", stored)
	}

	// ThumbnailDataURL labels whatever is on disk as image/jpeg, so anything the
	// backend answered with that is not one has to be refused here.
	second, err := service.AddPlaceholder("other", "other-fingerprint", 1)
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{
		"not a data URL": "https://example.invalid/cover.jpg",
		"not a JPEG URL": "data:image/png;base64," + base64.StdEncoding.EncodeToString([]byte("\x89PNG\r\n")),
		"not JPEG bytes": jpegDataURL("<html>error</html>"),
		"empty":          jpegDataURL(""),
	} {
		if err := service.EnsureThumbnail(second.ID, value); err == nil {
			t.Fatalf("%s was accepted as a cover", name)
		}
	}
	if err := service.EnsureThumbnail("loc_not-an-id", cover); err == nil {
		t.Fatal("invalid media id was accepted")
	}
	if listed := service.List(); len(listed) != 2 {
		t.Fatalf("library size = %d", len(listed))
	} else {
		for _, entry := range listed {
			if entry.ID == second.ID && entry.ThumbnailAvailable {
				t.Fatal("a rejected cover was written anyway")
			}
		}
	}
}

func TestImportAcceptsOverlongLocalMediaAndRejectsInvalidSources(t *testing.T) {
	root := t.TempDir()
	textPath := filepath.Join(root, "notes.txt")
	videoPath := filepath.Join(root, "long.mp4")
	for _, path := range []string{textPath, videoPath} {
		if err := os.WriteFile(path, []byte("fixture"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	const duration = 2*60*60 + 1
	tools := fixtureTools{metadata: Metadata{Duration: duration, HasVideo: true}}
	service, err := newServiceWithTools(filepath.Join(root, "data"), tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	result := service.Import([]string{textPath, videoPath, filepath.Join(root, "missing.mp4")})
	if len(result.Added) != 1 || len(result.Failed) != 2 {
		t.Fatalf("unexpected result: %#v", result)
	}
	if result.Added[0].SourcePath != videoPath || result.Added[0].Duration != duration {
		t.Fatalf("overlong local video was not imported intact: %#v", result.Added[0])
	}
	if listed := service.List(); len(listed) != 1 || listed[0].ID != result.Added[0].ID {
		t.Fatalf("library = %#v", listed)
	}
}

func TestRenameRelinkAndRemoveManageOnlyIndexedData(t *testing.T) {
	root := t.TempDir()
	first := filepath.Join(root, "first.mp4")
	second := filepath.Join(root, "second.mp4")
	for _, path := range []string{first, second} {
		if err := os.WriteFile(path, []byte("same-media-fixture"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	tools := fixtureTools{metadata: Metadata{Duration: 12, Width: 1280, Height: 720, HasVideo: true, HasAudio: true}}
	data := filepath.Join(root, "data")
	service, err := newServiceWithTools(data, tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	result := service.Import([]string{first})
	entry := result.Added[0]
	renamed, err := service.Rename(entry.ID, "新的标题")
	if err != nil || renamed.Title != "新的标题" {
		t.Fatalf("rename = %#v, %v", renamed, err)
	}
	if err := os.Remove(first); err != nil {
		t.Fatal(err)
	}
	if service.List()[0].Available {
		t.Fatal("missing source should be reported unavailable")
	}
	relinked, err := service.Relink(entry.ID, second)
	if err != nil || relinked.SourcePath != second {
		t.Fatalf("relink = %#v, %v", relinked, err)
	}
	document := filepath.Join(data, "documents", entry.ID)
	if err := os.MkdirAll(document, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "document.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := service.Remove(entry.ID, true); err != nil {
		t.Fatal(err)
	}
	if len(service.List()) != 0 {
		t.Fatal("removed entry remains in library")
	}
	if _, err := os.Stat(second); err != nil {
		t.Fatal("removing library data must not remove source media")
	}
	if _, err := os.Stat(document); !os.IsNotExist(err) {
		t.Fatal("requested document deletion did not occur")
	}
}

func TestDeleteDocumentKeepsMediaInLibrary(t *testing.T) {
	root := t.TempDir()
	video := filepath.Join(root, "video.mp4")
	if err := os.WriteFile(video, []byte("media-fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 12, Width: 1280, Height: 720, HasVideo: true, HasAudio: true}}
	data := filepath.Join(root, "data")
	service, err := newServiceWithTools(data, tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	entry := service.Import([]string{video}).Added[0]
	document := filepath.Join(data, "documents", entry.ID)
	if err := os.MkdirAll(filepath.Join(document, "history"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "document.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "history", "00000000.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := service.DeleteDocument(entry.ID); err != nil {
		t.Fatal(err)
	}
	entries := service.List()
	if len(entries) != 1 || entries[0].ID != entry.ID || entries[0].DocumentAvailable || !entries[0].DocumentRemoved {
		t.Fatalf("media entry after subtitle deletion = %#v", entries)
	}
	if _, err := os.Stat(video); err != nil {
		t.Fatal("deleting subtitles must not remove source media")
	}
	if _, err := os.Stat(document); !os.IsNotExist(err) {
		t.Fatal("subtitle document and history were not removed")
	}
	if err := os.MkdirAll(document, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "document.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	restored := service.List()[0]
	if !restored.DocumentAvailable || restored.DocumentRemoved {
		t.Fatalf("new subtitle document did not restore editable state: %#v", restored)
	}
}

func TestRelinkAcceptsDifferentMediaFingerprint(t *testing.T) {
	root := t.TempDir()
	first := filepath.Join(root, "first.mp4")
	second := filepath.Join(root, "second.mp4")
	if err := os.WriteFile(first, []byte("first"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(second, []byte("different"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 1, HasVideo: true, HasAudio: true}}
	service, err := newServiceWithTools(filepath.Join(root, "data"), tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	entry := service.Import([]string{first}).Added[0]
	relinked, err := service.Relink(entry.ID, second)
	if err != nil {
		t.Fatalf("relink different media: %v", err)
	}
	if relinked.SourcePath != second {
		t.Fatalf("relinked source path = %q, want %q", relinked.SourcePath, second)
	}
	if relinked.Fingerprint == entry.Fingerprint {
		t.Fatal("relinked entry kept the original media fingerprint")
	}
}

func TestRelinkSubtitleOnlyEntryKeepsSubtitleFingerprint(t *testing.T) {
	root := t.TempDir()
	video := filepath.Join(root, "associated.mp4")
	if err := os.WriteFile(video, []byte("unrelated-video"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 8, HasVideo: true, HasAudio: true}}
	data := filepath.Join(root, "data")
	service, err := newServiceWithTools(data, tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := service.AddPlaceholder("subtitles", "subtitle-fingerprint", 8)
	if err != nil {
		t.Fatal(err)
	}
	document := filepath.Join(data, "documents", entry.ID)
	if err := os.MkdirAll(document, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(document, "document.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}

	relinked, err := service.Relink(entry.ID, video)
	if err != nil {
		t.Fatalf("associate unrelated video: %v", err)
	}
	if relinked.SourcePath != video {
		t.Fatalf("associated source path = %q, want %q", relinked.SourcePath, video)
	}
	if relinked.Fingerprint != entry.Fingerprint {
		t.Fatalf("subtitle fingerprint changed from %q to %q", entry.Fingerprint, relinked.Fingerprint)
	}
	entries := service.List()
	if len(entries) != 1 || entries[0].ID != entry.ID {
		t.Fatalf("association created another local card: %#v", entries)
	}
}

func TestVideoEditIsNormalizedAndPersisted(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "source.mp4")
	if err := os.WriteFile(source, []byte("media-fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	tools := fixtureTools{metadata: Metadata{Duration: 42, HasVideo: true, HasAudio: true}}
	data := filepath.Join(root, "data")
	service, err := newServiceWithTools(data, tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	entry := service.Import([]string{source}).Added[0]
	ok, err := service.SetVideoEdit(entry.ID, VideoEdit{Pieces: []Range{
		{T0: 20, T1: 50},
		{T0: 2, T1: 8},
		{T0: 9, T1: 4},
	}})
	if err != nil || !ok {
		t.Fatalf("set video edit = %v, %v", ok, err)
	}
	want := []Range{{T0: 2, T1: 8}, {T0: 20, T1: 42}}
	if got := service.GetVideoEdit(entry.ID).Pieces; !reflect.DeepEqual(got, want) {
		t.Fatalf("pieces = %#v", got)
	}
	reloaded, err := New(data)
	if err != nil {
		t.Fatal(err)
	}
	if got := reloaded.GetVideoEdit(entry.ID).Pieces; !reflect.DeepEqual(got, want) {
		t.Fatalf("reloaded pieces = %#v", got)
	}
	if ok, err := reloaded.SetVideoEdit(entry.ID, VideoEdit{}); err != nil || !ok {
		t.Fatalf("clear video edit = %v, %v", ok, err)
	}
	if got := reloaded.GetVideoEdit(entry.ID).Pieces; len(got) != 0 {
		t.Fatalf("cleared pieces = %#v", got)
	}
}

func TestBundledEditorFontsAreCopiedForVideoExport(t *testing.T) {
	tools := fixtureTools{metadata: Metadata{Duration: 1, Width: 16, Height: 16, HasVideo: true}}
	service, err := newServiceWithTools(t.TempDir(), tools, tools)
	if err != nil {
		t.Fatal(err)
	}
	SetBundledFonts(service, map[string][]byte{
		"FZZhunYuan.woff2":     []byte("fzy-font"),
		"JingNanBoBoHei.woff2": []byte("jnb-font"),
	})
	directory := filepath.Join(t.TempDir(), "fonts")
	copied, err := service.copyBundledFonts(directory)
	if err != nil {
		t.Fatal(err)
	}
	if !copied {
		t.Fatal("expected bundled fonts to be copied")
	}
	for name, want := range map[string]string{
		"FZZhunYuan.woff2": "fzy-font", "JingNanBoBoHei.woff2": "jnb-font",
	} {
		data, readErr := os.ReadFile(filepath.Join(directory, name))
		if readErr != nil || string(data) != want {
			t.Fatalf("font %s = %q, %v", name, data, readErr)
		}
	}
}
