package selfupdate

import (
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/wailsapp/wails/v3/pkg/application"
	wailsupdater "github.com/wailsapp/wails/v3/pkg/updater"
)

func TestNewerVersion(t *testing.T) {
	for _, test := range []struct {
		candidate string
		current   string
		want      bool
	}{
		{"0.2.0", "0.1.9", true},
		{"v1.0.1", "1.0.0", true},
		{"1.0.0", "1.0.0", false},
		{"0.9.9", "1.0.0", false},
	} {
		if got := newerVersion(test.candidate, test.current); got != test.want {
			t.Fatalf("newerVersion(%q, %q) = %v", test.candidate, test.current, got)
		}
	}
}

func TestManifestURLPrefersExplicitOverride(t *testing.T) {
	t.Setenv(baseEnvironment, "https://example.test/updates/")
	if got := manifestURL(); got != "https://example.test/updates/latest.json" {
		t.Fatalf("manifestURL() = %q", got)
	}
	t.Setenv(manifestEnvironment, "https://example.test/pinned.json")
	if got := manifestURL(); got != "https://example.test/pinned.json" {
		t.Fatalf("manifestURL() = %q", got)
	}
}

func TestRemoveReplacedExecutables(t *testing.T) {
	directory := t.TempDir()
	executable := filepath.Join(directory, "Nonoka X.exe")
	replaced := []string{
		executable + ".old.1786267404207847300",
		executable + ".old.1786267404207847301",
		executable + ".new.1786267404207847302",
	}
	kept := []string{executable, filepath.Join(directory, "keep.old.1")}
	for _, path := range append(append([]string{}, kept...), replaced...) {
		if err := os.WriteFile(path, []byte("fixture"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	if remaining := removeReplacedExecutables(executable); remaining != 0 {
		t.Fatalf("%d replaced executables remain", remaining)
	}
	for _, path := range replaced {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("replaced executable was not removed: %s", path)
		}
	}
	for _, path := range kept {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("unrelated file was removed: %s", path)
		}
	}
}

func TestReleaseMandatoryUsesManifestMetadata(t *testing.T) {
	release := &wailsupdater.Release{Metadata: map[string]any{"minVersion": "0.2.0"}}
	if !releaseMandatory(release, "0.1.0") || releaseMandatory(release, "0.2.0") {
		t.Fatal("mandatory version comparison is incorrect")
	}
}

func TestConsumeReleaseNotesOnce(t *testing.T) {
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	want := ReleaseNotes{Version: Version, Notes: "fixed playback"}
	if err := writeJSONAtomic(service.releaseNotesPath(), want); err != nil {
		t.Fatal(err)
	}
	got, err := service.ConsumeReleaseNotes()
	if err != nil || got == nil || *got != want {
		t.Fatalf("notes = %#v, err = %v", got, err)
	}
	if _, err := os.Stat(service.releaseNotesPath()); !os.IsNotExist(err) {
		t.Fatal("release notes were not consumed")
	}
	if got, err := service.ConsumeReleaseNotes(); err != nil || got != nil {
		t.Fatalf("second read returned %#v, err = %v", got, err)
	}
}

// The helper that installs on the way out replaces the running executable in
// place and leaves nothing of the staging directory behind. A parent PID of 0
// stands for "nothing left to wait for", which is what the swap-only path
// under test needs.
func TestRunInstallerReplacesTargetAndClearsStaging(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "Nonoka X.exe")
	if err := os.WriteFile(target, []byte("previous build"), 0o755); err != nil {
		t.Fatal(err)
	}
	staging := filepath.Join(directory, "wails-update-123")
	if err := os.Mkdir(staging, 0o755); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(staging, "Nonoka-X-windows-amd64.exe")
	if err := os.WriteFile(source, []byte("staged build"), 0o644); err != nil {
		t.Fatal(err)
	}

	if code := runInstaller(target, source, 0, waitForExit); code != 0 {
		t.Fatalf("installer exited with %d", code)
	}
	installed, err := os.ReadFile(target)
	if err != nil || string(installed) != "staged build" {
		t.Fatalf("target = %q, err = %v", installed, err)
	}
	if _, err := os.Stat(staging); !os.IsNotExist(err) {
		t.Fatal("staging directory was not cleared")
	}
	asides, _ := filepath.Glob(target + ".old.*")
	if len(asides) != 0 {
		t.Fatalf("replaced build was left behind: %v", asides)
	}
}

// A swap that cannot put the new build in place has to leave the running one
// exactly where it was: the retry that follows expects to find it, and so does
// the user if every retry fails.
func TestReplaceExecutableRestoresTargetWhenSwapFails(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "Nonoka X.exe")
	if err := os.WriteFile(target, []byte("previous build"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := replaceExecutable(target, filepath.Join(directory, "Nonoka X.exe.new.1")); err == nil {
		t.Fatal("swap reported success with nothing staged")
	}
	if content, err := os.ReadFile(target); err != nil || string(content) != "previous build" {
		t.Fatalf("target = %q, err = %v", content, err)
	}
	asides, _ := filepath.Glob(target + ".old.*")
	if len(asides) != 0 {
		t.Fatalf("running build was left aside: %v", asides)
	}
}

// A helper that cannot find what it was asked to install must leave the
// running build alone rather than move it aside for a swap that never lands.
func TestRunInstallerKeepsTargetWhenSourceIsMissing(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "Nonoka X.exe")
	if err := os.WriteFile(target, []byte("previous build"), 0o755); err != nil {
		t.Fatal(err)
	}
	if code := runInstaller(target, filepath.Join(directory, "absent"), 0, waitForExit); code == 0 {
		t.Fatal("installer reported success without a staged build")
	}
	if content, err := os.ReadFile(target); err != nil || string(content) != "previous build" {
		t.Fatalf("target = %q, err = %v", content, err)
	}
}

func TestCheckDownloadsAndVerifiesEndpointArtifact(t *testing.T) {
	artifact := []byte("nonoka update fixture")
	digest := sha512.Sum512(artifact)
	parts := strings.Split(Version, ".")
	patch, _ := strconv.Atoi(parts[2])
	updateVersion := fmt.Sprintf("%s.%s.%d", parts[0], parts[1], patch+1)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/latest.json":
			_ = json.NewEncoder(response).Encode(map[string]any{
				"schemaVersion": 1,
				"version":       updateVersion,
				"channel":       "stable",
				"notes":         "update smoke",
				"artifacts": []map[string]any{{
					"url": "/artifact.exe", "platform": runtime.GOOS, "arch": runtime.GOARCH,
					"filename": "Nonoka-X-windows-amd64.exe", "size": len(artifact),
					"digestAlgo": "sha512", "digest": base64.StdEncoding.EncodeToString(digest[:]),
				}},
			})
		case "/artifact.exe":
			_, _ = response.Write(artifact)
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()

	t.Setenv(manifestEnvironment, server.URL+"/latest.json")
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	app := application.New(application.Options{Name: "update-test"})
	if err := Attach(service, app); err != nil {
		t.Fatal(err)
	}
	service.check(t.Context())

	status := service.GetStatus()
	if !status.Ready || status.Version != updateVersion || status.Stage != "ready" {
		t.Fatalf("update status = %#v", status)
	}
	downloaded := app.Updater.DownloadedPath()
	if _, err := os.Stat(downloaded); err != nil {
		t.Fatalf("verified update artifact was not staged: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(filepath.Dir(downloaded)) })
	var notes ReleaseNotes
	if err := readJSON(service.releaseNotesPath(), &notes); err != nil || notes.Version != updateVersion {
		t.Fatalf("release notes = %#v, err = %v", notes, err)
	}
}

// A dev build that has not opted in must leave the running binary alone.
func TestStartSkipsDevelopmentBuildsWithoutManifest(t *testing.T) {
	if productionBuild {
		t.Skip("production builds check on their own")
	}
	service, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	Start(service, nil)
	if stage := service.GetStatus().Stage; stage != "idle" {
		t.Fatalf("development build started an update check: stage = %q", stage)
	}
}
