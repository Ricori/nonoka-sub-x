package plugins

import (
	"archive/zip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

const newlineForTest = "\n"

const testManifest = `{
  "id": "dev.nonoka.hello",
  "name": "Hello tools",
  "version": "1.2.3",
  "apiVersion": 1,
  "description": "A test plugin",
  "contributes": {
    "tools": [{"id":"hello","title":"Hello","page":"ui/index.html","order":10}]
  }
}`

type fakeMediaLibrary struct {
	entries  []library.Entry
	exported *videoExport
	subtitle *subtitleExport
}

type videoExport struct {
	id, name, ass string
	t0, t1        float64
	scaleH        int
}

type subtitleExport struct {
	name, content string
}

func (f fakeMediaLibrary) List() []library.Entry { return append([]library.Entry(nil), f.entries...) }
func (f fakeMediaLibrary) Get(id string) (library.Entry, error) {
	for _, entry := range f.entries {
		if entry.ID == id {
			return entry, nil
		}
	}
	return library.Entry{}, os.ErrNotExist
}
func (f fakeMediaLibrary) Import(paths []string) library.ImportResult {
	return library.ImportResult{Added: []library.Entry{}, Failed: []library.ImportFailure{}}
}
func (f fakeMediaLibrary) SaveSubtitle(name, content string) (string, error) {
	if f.subtitle != nil {
		*f.subtitle = subtitleExport{name: name, content: content}
	}
	return "/exports/" + name, nil
}
func (f fakeMediaLibrary) ExportVideoRange(id, name, ass string, t0, t1 float64, crf int, preset string, scaleH int, abr string) (library.ExportResult, error) {
	if f.exported != nil {
		*f.exported = videoExport{id: id, name: name, ass: ass, t0: t0, t1: t1, scaleH: scaleH}
	}
	return library.ExportResult{Path: "/exports/" + name, Size: 1024}, nil
}

type fakeDocuments struct {
	stored map[string]map[string]any
	saved  map[string]any
}

func (f *fakeDocuments) Document(videoID string) (map[string]any, error) {
	document, ok := f.stored[videoID]
	if !ok {
		return nil, os.ErrNotExist
	}
	return document, nil
}

func (f *fakeDocuments) SaveDocument(videoID string, document map[string]any) (map[string]any, error) {
	if _, ok := f.stored[videoID]; !ok {
		return nil, os.ErrNotExist
	}
	f.saved = document
	saved := map[string]any{}
	for key, value := range document {
		saved[key] = value
	}
	saved["rev"] = document["rev"].(float64) + 1
	return saved, nil
}

func TestInstallEnablePageAndUninstall(t *testing.T) {
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	mustWrite(t, filepath.Join(packageRoot, manifestName), testManifest)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<!doctype html><html><head><title>Hello</title></head><body>works</body></html>`)
	mustWrite(t, filepath.Join(root, "plugin-data", "dev.nonoka.hello", "settings.json"), `{}`)

	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	installed, err := service.Install(packageRoot)
	if err != nil {
		t.Fatal(err)
	}
	if installed.ID != "dev.nonoka.hello" || !installed.Enabled || len(installed.Contributes.Tools) != 1 {
		t.Fatalf("unexpected installed plugin: %#v", installed)
	}
	listed := userPlugins(service.List())
	if len(listed) != 1 || listed[0].Version != "1.2.3" {
		t.Fatalf("unexpected plugin list: %#v", listed)
	}
	html, err := service.PageHTML("dev.nonoka.hello", "hello")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(html, "Content-Security-Policy") || !strings.Contains(html, "window.nonoka") || !strings.Contains(html, "works") {
		t.Fatalf("page policy was not injected: %s", html)
	}
	if _, err := service.SetEnabled("dev.nonoka.hello", false); err != nil {
		t.Fatal(err)
	}
	if _, err := service.PageHTML("dev.nonoka.hello", "hello"); err == nil {
		t.Fatal("disabled plugin page should not load")
	}
	if err := service.Uninstall("dev.nonoka.hello", false); err != nil {
		t.Fatal(err)
	}
	if len(userPlugins(service.List())) != 0 {
		t.Fatal("uninstalled plugin is still listed")
	}
	if _, err := os.Stat(filepath.Join(root, "plugin-data", "dev.nonoka.hello", "settings.json")); err != nil {
		t.Fatalf("plugin data should be preserved: %v", err)
	}
}

func TestInstallZipRejectsTraversal(t *testing.T) {
	root := t.TempDir()
	packagePath := filepath.Join(t.TempDir(), "unsafe.nonoka-plugin")
	file, err := os.Create(packagePath)
	if err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	item, err := archive.Create("../outside.txt")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := item.Write([]byte("unsafe")); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(packagePath); err == nil {
		t.Fatal("path traversal package should be rejected")
	}
	if _, err := os.Stat(filepath.Join(root, "outside.txt")); !os.IsNotExist(err) {
		t.Fatalf("unsafe file escaped staging directory: %v", err)
	}
}

func TestInstallNonokaPluginArchive(t *testing.T) {
	packagePath := filepath.Join(t.TempDir(), "hello.nonoka-plugin")
	file, err := os.Create(packagePath)
	if err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	for name, content := range map[string]string{
		manifestName:    testManifest,
		"ui/index.html": `<p>archive plugin</p>`,
	} {
		item, createErr := archive.Create(name)
		if createErr != nil {
			t.Fatal(createErr)
		}
		if _, writeErr := item.Write([]byte(content)); writeErr != nil {
			t.Fatal(writeErr)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	installed, err := service.Install(packagePath)
	if err != nil {
		t.Fatal(err)
	}
	if installed.ID != "dev.nonoka.hello" || !installed.Enabled {
		t.Fatalf("unexpected archive install: %#v", installed)
	}
}

func TestInstallNewVersionSwitchesCurrent(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	first := filepath.Join(t.TempDir(), "first")
	mustWrite(t, filepath.Join(first, manifestName), testManifest)
	mustWrite(t, filepath.Join(first, "ui", "index.html"), `<p>version one</p>`)
	if _, err := service.Install(first); err != nil {
		t.Fatal(err)
	}
	second := filepath.Join(t.TempDir(), "second")
	mustWrite(t, filepath.Join(second, manifestName), strings.Replace(testManifest, "1.2.3", "2.0.0", 1))
	mustWrite(t, filepath.Join(second, "ui", "index.html"), `<p>version two</p>`)
	if _, err := service.Install(second); err != nil {
		t.Fatal(err)
	}
	listed := userPlugins(service.List())
	if len(listed) != 1 || listed[0].Version != "2.0.0" {
		t.Fatalf("new version was not activated: %#v", listed)
	}
	html, err := service.PageHTML("dev.nonoka.hello", "hello")
	if err != nil || !strings.Contains(html, "version two") {
		t.Fatalf("active page did not switch versions: %v, %s", err, html)
	}
}

func TestManifestRejectsDuplicateTools(t *testing.T) {
	root := t.TempDir()
	manifest := strings.Replace(testManifest,
		`{"id":"hello","title":"Hello","page":"ui/index.html","order":10}`,
		`{"id":"hello","title":"Hello","page":"ui/index.html"},{"id":"hello","title":"Again","page":"ui/index.html"}`,
		1,
	)
	mustWrite(t, filepath.Join(root, manifestName), manifest)
	mustWrite(t, filepath.Join(root, "ui", "index.html"), `<p>Hello</p>`)
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(root); err == nil {
		t.Fatal("duplicate tool ids should be rejected")
	}
}

func TestMediaListRequiresDeclaredPermission(t *testing.T) {
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	withPermission := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["media.list"],`, 1)
	mustWrite(t, filepath.Join(packageRoot, manifestName), withPermission)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
	media := fakeMediaLibrary{entries: []library.Entry{{
		ID: "media-1", Title: "Example.mp4", SourcePath: `C:\private\Example.mp4`, Duration: 12.5,
		Width: 1920, Height: 1080, Available: true, DocumentAvailable: true,
	}}}
	service, err := New(root, media)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
	items, err := service.MediaList("dev.nonoka.hello")
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].ID != "media-1" || items[0].Title != "Example.mp4" {
		t.Fatalf("unexpected media summaries: %#v", items)
	}
	if _, err := service.SetEnabled("dev.nonoka.hello", false); err != nil {
		t.Fatal(err)
	}
	if _, err := service.MediaList("dev.nonoka.hello"); err == nil {
		t.Fatal("disabled plugin should not use host capabilities")
	}
}

func TestManifestRejectsUnknownPermission(t *testing.T) {
	root := t.TempDir()
	manifest := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["process.execute"],`, 1)
	mustWrite(t, filepath.Join(root, manifestName), manifest)
	mustWrite(t, filepath.Join(root, "ui", "index.html"), `<p>Hello</p>`)
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(root); err == nil {
		t.Fatal("unknown permission should be rejected")
	}
}

func TestManifestRejectsTrailingJSON(t *testing.T) {
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, manifestName), testManifest+` {"unexpected":true}`)
	mustWrite(t, filepath.Join(root, "ui", "index.html"), `<p>Hello</p>`)
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(root); err == nil {
		t.Fatal("trailing JSON should be rejected")
	}
}

func TestAudioFormatIsClosedSet(t *testing.T) {
	for _, format := range []string{"wav", "flac", "mp3", "m4a"} {
		if _, _, _, err := audioFormat(format); err != nil {
			t.Fatalf("expected %s to be supported: %v", format, err)
		}
	}
	if _, _, _, err := audioFormat("custom"); err == nil {
		t.Fatal("custom FFmpeg formats must not be accepted")
	}
}

func TestVideoDownloadURLAllowlist(t *testing.T) {
	tests := []struct {
		url      string
		platform string
	}{
		{"https://www.youtube.com/watch?v=example", "youtube"},
		{"https://youtu.be/example", "youtube"},
		{"https://www.twitch.tv/videos/123", "twitch"},
		{"https://clips.twitch.tv/Example", "twitch"},
	}
	for _, test := range tests {
		platform, normalized, err := validateVideoURL(test.url)
		if err != nil || platform != test.platform || normalized == "" {
			t.Fatalf("expected %s to be accepted as %s: %s, %v", test.url, test.platform, platform, err)
		}
	}
	for _, value := range []string{
		"http://youtube.com/watch?v=example",
		"https://youtube.com.evil.example/watch?v=example",
		"https://user:password@twitch.tv/videos/123",
		"https://example.com/video",
	} {
		if _, _, err := validateVideoURL(value); err == nil {
			t.Fatalf("unsafe or unsupported URL was accepted: %s", value)
		}
	}
}

func TestTailBufferKeepsBoundedSuffix(t *testing.T) {
	buffer := newTailBuffer(5)
	if _, err := buffer.Write([]byte("1234")); err != nil {
		t.Fatal(err)
	}
	if _, err := buffer.Write([]byte("56789")); err != nil {
		t.Fatal(err)
	}
	if buffer.String() != "56789" {
		t.Fatalf("unexpected tail: %q", buffer.String())
	}
}

func TestYTDLPArgumentsUseClosedSafeSet(t *testing.T) {
	allowed := []string{
		"--no-playlist",
		"-f", "bv*[height<=1080]+ba/b[height<=1080]",
		"--merge-output-format", "mp4",
		"--remux-video", "mp4",
		"-N", "4",
	}
	if err := validateYTDLPArguments(allowed, false); err != nil {
		t.Fatalf("safe yt-dlp arguments were rejected: %v", err)
	}
	blocked := [][]string{
		{"--exec", "calc.exe"},
		{"--cookies-from-browser", "firefox"},
		{"-o", `C:\outside.mp4`},
		{"--config-locations", `C:\config.txt`},
		{"-f", "best[protocol*=http]"},
		{"-N", "64"},
	}
	for _, arguments := range blocked {
		if err := validateYTDLPArguments(arguments, false); err == nil {
			t.Fatalf("unsafe yt-dlp arguments were accepted: %#v", arguments)
		}
		if err := validateYTDLPArguments(arguments, true); err == nil {
			t.Fatalf("unsafe yt-dlp arguments were accepted for a system plugin: %#v", arguments)
		}
	}
	// The wider set is reachable from built-ins only.
	trusted := [][]string{
		{"-f", "bv*[height<=2160]+ba/b[height<=2160]"},
		{"-f", "bv*[height<=1440]+ba/b[height<=1440]"},
		{"-N", "16"},
		{"--no-part"},
	}
	for _, arguments := range trusted {
		if err := validateYTDLPArguments(arguments, true); err != nil {
			t.Fatalf("built-in yt-dlp arguments were rejected: %#v: %v", arguments, err)
		}
		if err := validateYTDLPArguments(arguments, false); err == nil {
			t.Fatalf("built-in-only arguments were accepted for a UI plugin: %#v", arguments)
		}
	}
}

func TestDownloadFailureExplainsTheForbiddenCase(t *testing.T) {
	// yt-dlp's own spelling and aria2c's wrapped exit code are the same refusal,
	// and the plugin page must not depend on which downloader happened to run.
	spellings := []string{
		"[download] 0.9% of 1.02GiB" + newlineForTest + "ERROR: unable to download video data: HTTP Error 403: Forbidden",
		"[#efe8d4 0B/0B CN:1 DL:0B] errorCode=22 The response status is not successful. status=403" +
			newlineForTest + "ERROR: aria2c exited with code 22",
	}
	for _, output := range spellings {
		explained := explainDownloadFailure(output)
		if explained != forbiddenDownloadHelp {
			t.Fatalf("403 was not explained: %q", explained)
		}
	}
}

func TestAria2cIsAnOptInRequestFromBuiltInPlugins(t *testing.T) {
	request := []string{"-N", "16", "--downloader", "aria2c"}
	if err := validateYTDLPArguments(request, true); err != nil {
		t.Fatalf("built-in plugin could not ask for aria2c: %v", err)
	}
	if err := validateYTDLPArguments(request, false); err == nil {
		t.Fatal("a UI plugin was allowed to ask for an external downloader")
	}
	for _, named := range [][]string{{"--downloader", "C:\\evil.exe"}, {"--downloader", "ffmpeg"}, {"--downloader"}} {
		if err := validateYTDLPArguments(named, true); err == nil {
			t.Fatalf("downloader other than the aria2c request was accepted: %#v", named)
		}
	}
	kept, requested := takeAria2cRequest(request)
	if !requested || strings.Join(kept, " ") != "-N 16" {
		t.Fatalf("request was not taken out: %#v %v", kept, requested)
	}
	if _, requested := takeAria2cRequest([]string{"-N", "16"}); requested {
		t.Fatal("aria2c was used without being asked for")
	}
}

func TestDownloadFailureExplainsTheUnreachableCase(t *testing.T) {
	// aria2c's own report of the timeout, as it reaches us when the media
	// transfer bypassed the proxy that the extraction went through.
	output := "09/29 23:56:38 [ERROR] CUID#7 - Download aborted. URI=https://rr3---sn-i3belnls.googlevideo.com/videoplayback?expire=1" +
		newlineForTest + "Exception: [AbstractCommand.cc:312] errorCode=1 Network problem has occurred. cause:A connection attempt failed" +
		newlineForTest + "ERROR: aria2c exited with code 1"
	if explained := explainDownloadFailure(output); explained != unreachableDownloadHelp {
		t.Fatalf("timeout was not explained: %q", explained)
	}
}

func TestSystemProxyParsing(t *testing.T) {
	cases := map[string]string{
		"":                      "",
		"127.0.0.1:7890":        "http://127.0.0.1:7890",
		"http://127.0.0.1:7890": "http://127.0.0.1:7890",
		"http=127.0.0.1:8080;https=127.0.0.1:8443": "http://127.0.0.1:8443",
		"http=127.0.0.1:8080;socks=127.0.0.1:1080": "http://127.0.0.1:8080",
		"socks=127.0.0.1:1080":                     "socks5://127.0.0.1:1080",
		"ftp=127.0.0.1:21":                         "",
	}
	for server, want := range cases {
		if got := parseSystemProxy(server); got != want {
			t.Fatalf("parseSystemProxy(%q) = %q, want %q", server, got, want)
		}
	}
	if !aria2cSupportsProxy("") || !aria2cSupportsProxy("http://127.0.0.1:7890") {
		t.Fatal("aria2c was refused a direct or HTTP-proxied transfer")
	}
	if aria2cSupportsProxy("socks5://127.0.0.1:1080") {
		t.Fatal("aria2c was handed a SOCKS proxy it cannot speak")
	}
}

func TestDownloadFailureKeepsActionableDetailBounded(t *testing.T) {
	// A real failed run is mostly progress redraws and very long signed URLs;
	// only the ERROR lines are worth putting in front of a user.
	noise := strings.Repeat("[download]  12.3% of 1.02GiB at 8.00MiB/s ETA 02:04"+newlineForTest, 400)
	explained := explainDownloadFailure(noise + "ERROR: Requested format is not available")
	if !strings.Contains(explained, "Requested format is not available") {
		t.Fatalf("actionable line was dropped: %q", explained)
	}
	if strings.Contains(explained, "[download]") {
		t.Fatalf("progress noise reached the message: %q", explained)
	}
	if count := len([]rune(explained)); count > 500 {
		t.Fatalf("message is unbounded at %d runes", count)
	}
	if explainDownloadFailure("") == "" {
		t.Fatal("an empty log still needs a message")
	}
}

func userPlugins(list []InstalledPlugin) []InstalledPlugin {
	result := []InstalledPlugin{}
	for _, plugin := range list {
		if !plugin.System {
			result = append(result, plugin)
		}
	}
	return result
}

func TestDocumentCapabilitiesFollowDeclaredPermissions(t *testing.T) {
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	withPermission := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["document.read"],`, 1)
	mustWrite(t, filepath.Join(packageRoot, manifestName), withPermission)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
	media := fakeMediaLibrary{entries: []library.Entry{{
		ID: "media-1", Title: "Example.mp4", Duration: 12.5, Available: true, DocumentAvailable: true,
	}}}
	documents := &fakeDocuments{stored: map[string]map[string]any{
		"media-1": {"schema": float64(1), "rev": float64(3), "subtitles": []any{}},
	}}
	service, err := New(root, media)
	if err != nil {
		t.Fatal(err)
	}
	SetDocuments(service, documents)
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
	document, err := service.Document("dev.nonoka.hello", "media-1")
	if err != nil {
		t.Fatal(err)
	}
	if document["rev"] != float64(3) {
		t.Fatalf("unexpected document: %#v", document)
	}
	if _, err := service.Document("dev.nonoka.hello", "media-unknown"); err == nil {
		t.Fatal("a media id outside the library should not resolve to a document")
	}
	// Reading is declared, writing is not: the two capabilities stay separate.
	if _, err := service.SaveDocument("dev.nonoka.hello", "media-1", map[string]any{"rev": float64(3)}); err == nil {
		t.Fatal("document.write must be declared separately from document.read")
	}
}

func TestSaveDocumentKeepsOnlyWritableFields(t *testing.T) {
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	withPermission := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["document.write"],`, 1)
	mustWrite(t, filepath.Join(packageRoot, manifestName), withPermission)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
	media := fakeMediaLibrary{entries: []library.Entry{{ID: "media-1", Title: "Example.mp4", Duration: 12.5, Available: true}}}
	documents := &fakeDocuments{stored: map[string]map[string]any{"media-1": {"rev": float64(7)}}}
	service, err := New(root, media)
	if err != nil {
		t.Fatal(err)
	}
	SetDocuments(service, documents)
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
	saved, err := service.SaveDocument("dev.nonoka.hello", "media-1", map[string]any{
		"rev":       float64(7),
		"title":     "Example",
		"video_id":  "somewhere-else",
		"subtitles": []any{map[string]any{"t0": float64(1), "t1": float64(2), "ja": "こんにちは", "zh": "你好", "words": []any{}}},
		"tracks":    []any{map[string]any{"id": "t1", "name": "注释", "segs": []any{}}},
		"effects":   []any{map[string]any{"id": "fade", "templateId": "fade", "target": map[string]any{"scope": "all"}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if saved["rev"] != float64(8) {
		t.Fatalf("save should return the stored revision: %#v", saved)
	}
	if _, present := documents.saved["video_id"]; present {
		t.Fatalf("plugin fields outside the writable set reached the store: %#v", documents.saved)
	}
	segment := documents.saved["subtitles"].([]any)[0].(map[string]any)
	if _, present := segment["words"]; !present {
		t.Fatal("segment fields the plugin did not understand should survive a round trip")
	}
	if len(documents.saved["effects"].([]any)) != 1 {
		t.Fatalf("effect bindings should survive a plugin round trip: %#v", documents.saved)
	}
	for _, broken := range []map[string]any{
		{"subtitles": []any{}},
		{"rev": float64(7), "subtitles": []any{map[string]any{"t0": float64(2), "t1": float64(1)}}},
		{"rev": float64(7), "subtitles": []any{map[string]any{"t0": float64(0), "t1": float64(1), "zh": float64(9)}}},
		{"rev": float64(7), "subtitles": "not-a-list"},
		{"rev": float64(7), "subtitles": []any{}, "effects": "not-a-list"},
	} {
		if _, err := service.SaveDocument("dev.nonoka.hello", "media-1", broken); err == nil {
			t.Fatalf("malformed document was accepted: %#v", broken)
		}
	}
}

func TestExportVideoUsesHostOwnedRangeAndName(t *testing.T) {
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	withPermission := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["media.export-video", "subtitle.export"],`, 1)
	mustWrite(t, filepath.Join(packageRoot, manifestName), withPermission)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
	exported := &videoExport{}
	subtitle := &subtitleExport{}
	media := fakeMediaLibrary{
		entries:  []library.Entry{{ID: "media-1", Title: "Example.mp4", Duration: 12.5, Available: true}},
		exported: exported,
		subtitle: subtitle,
	}
	service, err := New(root, media)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
	artifact, err := service.ExportVideo("dev.nonoka.hello", "media-1", `..\..\escape.mkv`, "[Events]\n", 3, 0, 720)
	if err != nil {
		t.Fatal(err)
	}
	if artifact.Format != "mp4" || artifact.Size != 1024 {
		t.Fatalf("unexpected artifact: %#v", artifact)
	}
	if exported.name != "escape.mkv.mp4" {
		t.Fatalf("plugin file name should be reduced to a bare mp4 name: %q", exported.name)
	}
	if exported.t0 != 3 || exported.t1 != 12.5 || exported.scaleH != 720 {
		t.Fatalf("unexpected export range: %#v", exported)
	}
	if _, err := service.ExportVideo("dev.nonoka.hello", "media-1", "", "[Events]", 12.5, 12.5, 0); err == nil {
		t.Fatal("an empty range should be rejected")
	}
	if _, err := service.ExportVideo("dev.nonoka.hello", "media-1", "", "", 0, 0, 0); err == nil {
		t.Fatal("an empty subtitle track should be rejected")
	}
	if _, err := service.ExportVideo("dev.nonoka.hello", "media-1", "", "[Events]", 0, 0, 100); err == nil {
		t.Fatal("an out-of-range export height should be rejected")
	}
	if _, err := service.SaveSubtitleFile("dev.nonoka.hello", `C:\Windows\system32\drivers\etc\hosts`, "Dialogue: 0"); err != nil {
		t.Fatal(err)
	}
	if subtitle.name != "hosts.ass" {
		t.Fatalf("subtitle name should be reduced to a bare file name: %q", subtitle.name)
	}
}

func TestExamplePluginsMatchAPIV1(t *testing.T) {
	for _, name := range []string{"hello-tool", "video-downloader", "subtitle-studio"} {
		root := filepath.Join("..", "..", "examples", "plugins", name)
		manifest, err := readManifest(root)
		if err != nil {
			t.Fatalf("read %s example: %v", name, err)
		}
		if err := validateManifest(manifest, root); err != nil {
			t.Fatalf("validate %s example: %v", name, err)
		}
	}
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
