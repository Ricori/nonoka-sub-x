package plugins

import (
	"fmt"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestDownloaderReportsWhichToolsAreStillMissing(t *testing.T) {
	// A data directory with no optional tools installed: the page has to be able
	// to name what to install, not just learn that the token path is unavailable.
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	builtin := builtinPlugin(t, service, "dev.nonoka.youtube-downloader")
	settings, err := service.LoadDownloaderSettings(builtin.ID)
	if err != nil {
		t.Fatal(err)
	}
	if settings.POTReady {
		t.Fatal("the PO token path reported ready with nothing installed")
	}
	if !slices.Contains(settings.Missing, "pot-provider") {
		t.Fatalf("missing tools %v do not mention the PO token provider", settings.Missing)
	}
	if settings.CookiesPresent {
		t.Fatal("a fresh profile reported stored cookies")
	}
}

func TestDownloaderReportsARunningDownloadToAFreshPage(t *testing.T) {
	// Navigating away destroys the iframe, so a page that mounts mid-download has
	// no state of its own. It has to be able to ask whether one is still running,
	// or the user sees a reset form and a job they cannot get back to.
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	builtin := builtinPlugin(t, service, "dev.nonoka.youtube-downloader")
	settings, err := service.LoadDownloaderSettings(builtin.ID)
	if err != nil {
		t.Fatal(err)
	}
	if settings.DownloadRunning {
		t.Fatal("an idle service reported a running download")
	}
	service.downloadActive.Store(true)
	settings, err = service.LoadDownloaderSettings(builtin.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !settings.DownloadRunning {
		t.Fatal("a running download was invisible to a newly mounted page")
	}
}

func TestCancelDownloadOnlyFiresAtARunningJob(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	builtin := builtinPlugin(t, service, "dev.nonoka.youtube-downloader")
	// Nothing running: the button must say so rather than silently doing nothing.
	if err := service.CancelDownload(builtin.ID); err == nil {
		t.Fatal("cancelling with no download running was accepted")
	}
	if service.downloadCancelled.Load() {
		t.Fatal("a refused cancel still armed the cancelled flag")
	}

	fired := false
	service.downloadCancelMu.Lock()
	service.downloadCancel = func() { fired = true }
	service.downloadCancelMu.Unlock()
	if err := service.CancelDownload(builtin.ID); err != nil {
		t.Fatalf("cancelling a running download failed: %v", err)
	}
	if !fired {
		t.Fatal("cancel did not reach the running download")
	}
	// The flag is what tells the run apart from a timeout, so it must be set
	// before the process dies, not after.
	if !service.downloadCancelled.Load() {
		t.Fatal("cancel did not mark the run as user-cancelled")
	}
}

func TestDownloadLogSurvivesAPageThatWasTornDown(t *testing.T) {
	root := t.TempDir()
	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	builtin := builtinPlugin(t, service, "dev.nonoka.youtube-downloader")

	for i := 0; i < downloadLogRetained+50; i++ {
		service.recordDownloadLog(fmt.Sprintf("[download] line %d", i))
	}
	lines, err := service.DownloadLogLines(builtin.ID)
	if err != nil {
		t.Fatal(err)
	}
	// Bounded, and it is the tail that is kept: the end of a run is what explains
	// how it went.
	if len(lines) != downloadLogRetained {
		t.Fatalf("retained %d lines, want %d", len(lines), downloadLogRetained)
	}
	if want := fmt.Sprintf("[download] line %d", downloadLogRetained+49); lines[len(lines)-1] != want {
		t.Fatalf("last retained line is %q, want %q", lines[len(lines)-1], want)
	}

	// The returned slice must not alias the buffer, or a caller could scribble
	// over the log a later page still needs.
	lines[0] = "tampered"
	again, err := service.DownloadLogLines(builtin.ID)
	if err != nil {
		t.Fatal(err)
	}
	if again[0] == "tampered" {
		t.Fatal("DownloadLogLines handed out the live buffer")
	}

	// A new run starts from a clean log rather than appending to the last one's.
	service.resetDownloadLog()
	if lines, err = service.DownloadLogLines(builtin.ID); err != nil || len(lines) != 0 {
		t.Fatalf("log survived a reset: %d lines, %v", len(lines), err)
	}

	// Clearing has to reach the host's buffer, not just the page's view: the page
	// refetches on every mount, so a DOM-only clear would undo itself.
	service.recordDownloadLog("[download] still here")
	if err := service.ClearDownloadLog(builtin.ID); err != nil {
		t.Fatal(err)
	}
	if lines, err = service.DownloadLogLines(builtin.ID); err != nil || len(lines) != 0 {
		t.Fatalf("cleared log came back: %d lines, %v", len(lines), err)
	}
}

func TestCookieJarRejectsPastesThatAreNotCookieFiles(t *testing.T) {
	jar := strings.Join([]string{
		"# Netscape HTTP Cookie File",
		".youtube.com\tTRUE\t/\tTRUE\t1799999999\tSID\tvalue",
		".youtube.com\tTRUE\t/\tTRUE\t1799999999\tHSID\tvalue",
	}, "\n")
	count, err := validateCookieJar(jar)
	if err != nil || count != 2 {
		t.Fatalf("valid jar rejected: %d, %v", count, err)
	}
	rejected := map[string]string{
		"empty":       "",
		"html paste":  "<!doctype html><html><body>Sign in</body></html>",
		"comments":    "# Netscape HTTP Cookie File\n# nothing else",
		"space delim": ".youtube.com TRUE / TRUE 1799999999 SID value",
		"oversize":    strings.Repeat("a", maxCookieJarBytes+1),
	}
	for name, content := range rejected {
		if _, err := validateCookieJar(content); err == nil {
			t.Fatalf("%s was accepted as a cookie jar", name)
		}
	}
}

func TestCookiesAreScopedToTheOwningPlugin(t *testing.T) {
	root := t.TempDir()

	service, err := New(root)
	if err != nil {
		t.Fatal(err)
	}
	newPackage := func(id string, withPermission bool) string {
		packageRoot := filepath.Join(t.TempDir(), id)
		manifest := strings.Replace(testManifest, "dev.nonoka.hello", id, 1)
		if withPermission {
			manifest = strings.Replace(
				manifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": ["tools.cookies"],`, 1)
		}
		mustWrite(t, filepath.Join(packageRoot, manifestName), manifest)
		mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
		return packageRoot
	}
	jar := "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1799999999\tSID\tvalue"

	// Declaring the permission is what grants it; being sideloaded does not
	// disqualify a plugin, because the jar never leaves its own directory.
	first, err := service.Install(newPackage("dev.nonoka.one", true))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.SaveCookies(first.ID, jar); err != nil {
		t.Fatalf("a sideloaded plugin with the permission could not store cookies: %v", err)
	}

	// A second plugin must not see the first one's session, or the per-plugin
	// jar would be a shared credential with extra steps.
	second, err := service.Install(newPackage("dev.nonoka.two", true))
	if err != nil {
		t.Fatal(err)
	}
	settings, err := service.LoadDownloaderSettings(second.ID)
	if err != nil {
		t.Fatal(err)
	}
	if settings.CookiesPresent {
		t.Fatal("one plugin's cookies were visible to another")
	}
	if service.cookiesFor(second.ID) == service.cookiesFor(first.ID) {
		t.Fatal("two plugins share one cookie jar")
	}

	// Without the permission it stays refused, whatever the plugin's origin.
	third, err := service.Install(newPackage("dev.nonoka.three", false))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.SaveCookies(third.ID, jar); err == nil {
		t.Fatal("a plugin without tools.cookies stored the user's session")
	}
	if service.cookiesFor(third.ID) != "" {
		t.Fatal("a refused save still wrote a jar")
	}
}
