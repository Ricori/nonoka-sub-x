// Package selfupdate keeps the packaged desktop build current from a signed
// Wails update manifest. Downloads happen in the background and the swap runs
// as the application exits, so an in-flight editing session is never
// interrupted and the next launch is already on the new build. A release the
// manifest marks mandatory, or the update button, installs and restarts
// straight away instead.
package selfupdate

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/wailsapp/wails/v3/pkg/application"
	wailsupdater "github.com/wailsapp/wails/v3/pkg/updater"
	"github.com/wailsapp/wails/v3/pkg/updater/providers/endpoint"
)

// Version is the running desktop build. scripts/release-update.ps1 rewrites it
// alongside build/config.yml, so the two never drift.
const Version = "0.3.2"

const (
	manifestEnvironment = "NONOKA_UPDATE_MANIFEST"
	baseEnvironment     = "NONOKA_UPDATE_URL"
	disableEnvironment  = "NONOKA_DISABLE_UPDATE"
	defaultUpdateBase   = "https://livestream.nonoka.online/nonoka-x-updates"
	checkInterval       = 4 * time.Hour
	retryInterval       = time.Minute
)

// Status mirrors the download lifecycle for the renderer. Stage is one of
// idle, checking, download, verify, unpack, ready, waiting-editor, installing
// or retry.
type Status struct {
	Mandatory bool   `json:"mandatory"`
	Version   string `json:"version"`
	Stage     string `json:"stage"`
	Done      int64  `json:"done"`
	Total     int64  `json:"total"`
	Ready     bool   `json:"ready"`
}

// ReleaseNotes is handed to the renderer once, on the first launch after an
// update installed successfully.
type ReleaseNotes struct {
	Version string `json:"version"`
	Notes   string `json:"notes"`
}

type Service struct {
	mu         sync.Mutex
	app        *application.App
	root       string
	status     Status
	busy       bool
	tracking   bool
	handedOff  bool
	editorOpen func() bool
}

func New(dataDirectory string) (*Service, error) {
	root, err := filepath.Abs(dataDirectory)
	if err != nil {
		return nil, err
	}
	return &Service{root: root, status: Status{Stage: "idle"}}, nil
}

// Attach wires the Wails updater to the release manifest and mirrors its
// progress events onto the service status. Package level so the method set
// bound to the renderer stays limited to the four calls below.
func Attach(s *Service, app *application.App) error {
	provider, err := endpoint.New(endpoint.Config{URL: manifestURL(), Channel: "stable"})
	if err != nil {
		return err
	}
	if err := app.Updater.Init(wailsupdater.Config{
		CurrentVersion: Version,
		Providers:      []wailsupdater.Provider{provider},
		Window:         wailsupdater.WindowNone,
	}); err != nil {
		return err
	}
	s.mu.Lock()
	s.app = app
	s.mu.Unlock()

	app.Event.On(wailsupdater.EventDownloadProgress, func(event *application.CustomEvent) {
		progress, ok := event.Data.(wailsupdater.Progress)
		if !ok {
			return
		}
		s.trackProgress(func(status *Status) {
			status.Stage = "download"
			status.Done = progress.Written
			status.Total = progress.Total
		})
	})
	app.Event.On(wailsupdater.EventVerifying, func(_ *application.CustomEvent) {
		s.trackProgress(func(status *Status) { status.Stage = "verify" })
	})
	app.Event.On(wailsupdater.EventInstalling, func(_ *application.CustomEvent) {
		s.trackProgress(func(status *Status) { status.Stage = "unpack" })
	})
	return nil
}

// trackProgress applies a lifecycle event from the updater. Wails dispatches
// listeners on their own goroutine, so an event can land after the download it
// describes has already returned and check has settled on a terminal stage;
// outside that window the event is stale and must not move the status.
func (s *Service) trackProgress(mutate func(*Status)) {
	s.mu.Lock()
	if !s.tracking {
		s.mu.Unlock()
		return
	}
	mutate(&s.status)
	status := s.status
	s.mu.Unlock()
	s.publish(status)
}

// Start begins the background check loop. editorOpen reports whether an editor
// window is holding unsaved work, which defers a mandatory install.
func Start(s *Service, editorOpen func() bool) {
	if os.Getenv(disableEnvironment) == "1" {
		return
	}
	// A development build must not pull a release over itself. Pointing
	// NONOKA_UPDATE_MANIFEST at a test manifest is the deliberate opt-in.
	if !productionBuild && strings.TrimSpace(os.Getenv(manifestEnvironment)) == "" {
		return
	}
	s.mu.Lock()
	s.editorOpen = editorOpen
	s.mu.Unlock()
	s.setStage("checking", false)
	removeLegacyPendingMarker(s.root)
	go func() {
		for {
			s.check(context.Background())
			time.Sleep(checkInterval)
		}
	}()
}

func manifestURL() string {
	if configured := strings.TrimSpace(os.Getenv(manifestEnvironment)); configured != "" {
		return configured
	}
	base := strings.TrimSpace(os.Getenv(baseEnvironment))
	if base == "" {
		base = defaultUpdateBase
	}
	return strings.TrimRight(base, "/") + "/wails/latest.json"
}

func (s *Service) check(ctx context.Context) {
	s.mu.Lock()
	if s.busy || s.status.Ready || s.app == nil {
		s.mu.Unlock()
		return
	}
	s.busy = true
	app := s.app
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.busy = false
		s.mu.Unlock()
	}()

	release, err := app.Updater.Check(ctx)
	if err != nil {
		s.retryOrIdle()
		return
	}
	if release == nil {
		s.mu.Lock()
		s.status = Status{Stage: "idle"}
		status := s.status
		s.mu.Unlock()
		s.publish(status)
		return
	}
	mandatory := releaseMandatory(release, Version)
	s.mu.Lock()
	s.status = Status{Mandatory: mandatory, Version: release.Version, Stage: "download", Total: release.Artifact.Size}
	s.tracking = true
	status := s.status
	s.mu.Unlock()
	s.publish(status)

	err = app.Updater.DownloadAndInstall(ctx)
	s.mu.Lock()
	s.tracking = false
	s.mu.Unlock()
	if err != nil {
		s.retryOrIdle()
		return
	}
	if strings.TrimSpace(release.Notes) != "" {
		_ = writeJSONAtomic(s.releaseNotesPath(), ReleaseNotes{Version: release.Version, Notes: release.Notes})
	}
	s.mu.Lock()
	s.status.Stage = "ready"
	s.status.Ready = true
	status = s.status
	s.mu.Unlock()
	s.publish(status)
	app.Event.Emit("update:ready", map[string]string{"version": release.Version})

	// An optional release stays staged: InstallOnExit performs the swap during
	// shutdown unless the user reaches for the update button first.
	if mandatory {
		go s.waitAndInstallMandatory()
	}
}

func (s *Service) retryOrIdle() {
	s.mu.Lock()
	mandatory := s.status.Mandatory
	s.mu.Unlock()
	if !mandatory {
		s.setStage("idle", false)
		return
	}
	s.setStage("retry", false)
	go func() {
		time.Sleep(retryInterval)
		s.check(context.Background())
	}()
}

// GetStatus reports the current download lifecycle to the renderer.
func (s *Service) GetStatus() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := s.status
	if status.Stage == "" {
		status.Stage = "idle"
	}
	return status
}

// CurrentVersion is the running build, shown in the UI next to the update button.
func (s *Service) CurrentVersion() string { return Version }

// InstallUpdate swaps in the staged build and restarts. It reports false when
// nothing is staged or the restart could not be handed off.
func (s *Service) InstallUpdate() bool {
	s.mu.Lock()
	if !s.status.Ready || s.app == nil {
		s.mu.Unlock()
		return false
	}
	s.status.Stage = "installing"
	s.handedOff = true
	status := s.status
	app := s.app
	s.mu.Unlock()
	s.publish(status)

	if app.Updater.Restart(context.Background()) != nil {
		s.mu.Lock()
		s.handedOff = false
		s.mu.Unlock()
		s.setStage("ready", true)
		return false
	}
	return true
}

// InstallOnExit swaps in a staged build while the application shuts down. It
// is the ordinary path for an optional release: the download already happened
// during this session, so installing here means the next launch comes up on
// the new build without downloading it a second time. Called after the event
// loop has returned, and a no-op when nothing is staged or the update was
// already handed to the Wails restart helper.
func InstallOnExit(s *Service) {
	s.mu.Lock()
	ready, handedOff, app := s.status.Ready, s.handedOff, s.app
	s.mu.Unlock()
	if !ready || handedOff || app == nil {
		return
	}
	staged := app.Updater.DownloadedPath()
	if staged == "" {
		return
	}
	if err := spawnInstaller(staged); err != nil {
		log.Printf("auto update: install on exit: %v", err)
	}
}

// ConsumeReleaseNotes returns the notes for the running build exactly once.
func (s *Service) ConsumeReleaseNotes() (*ReleaseNotes, error) {
	path := s.releaseNotesPath()
	var notes ReleaseNotes
	if err := readJSON(path, &notes); err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	if notes.Version == "" {
		return nil, nil
	}
	if notes.Version == Version && strings.TrimSpace(notes.Notes) != "" {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		return &notes, nil
	}
	// Notes for a build we never reached are stale once it is no longer newer.
	if !newerVersion(notes.Version, Version) {
		_ = os.Remove(path)
	}
	return nil, nil
}

func (s *Service) waitAndInstallMandatory() {
	nextReminder := time.Time{}
	for {
		s.mu.Lock()
		editorOpen := s.editorOpen
		s.mu.Unlock()
		if editorOpen == nil || !editorOpen() {
			s.InstallUpdate()
			return
		}
		if time.Now().After(nextReminder) {
			s.setStage("waiting-editor", true)
			nextReminder = time.Now().Add(time.Minute)
		}
		time.Sleep(time.Second)
	}
}

func (s *Service) setStage(stage string, ready bool) {
	s.mu.Lock()
	s.status.Stage = stage
	s.status.Ready = ready
	status := s.status
	s.mu.Unlock()
	s.publish(status)
}

func (s *Service) publish(status Status) {
	s.mu.Lock()
	app := s.app
	s.mu.Unlock()
	if app != nil {
		app.Event.Emit("update:status", status)
	}
}

func (s *Service) releaseNotesPath() string { return filepath.Join(s.root, "release-notes.json") }

// removeLegacyPendingMarker clears the marker builds up to 0.2.1 wrote to
// schedule an install for the next launch. Installing now happens on the way
// out, so the file is only litter.
func removeLegacyPendingMarker(root string) {
	_ = os.Remove(filepath.Join(root, "update-pending.json"))
}

func releaseMandatory(release *wailsupdater.Release, current string) bool {
	if release == nil || release.Metadata == nil {
		return false
	}
	minimum, _ := release.Metadata["minVersion"].(string)
	return minimum != "" && newerVersion(minimum, current)
}

func newerVersion(candidate, current string) bool {
	parse := func(value string) [3]int {
		var result [3]int
		parts := strings.Split(strings.TrimPrefix(value, "v"), ".")
		for index := 0; index < len(result) && index < len(parts); index++ {
			result[index], _ = strconv.Atoi(strings.SplitN(parts[index], "-", 2)[0])
		}
		return result
	}
	left, right := parse(candidate), parse(current)
	for index := range left {
		if left[index] != right[index] {
			return left[index] > right[index]
		}
	}
	return false
}

func readJSON(path string, target any) error {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	return json.Unmarshal(data, target)
}

func writeJSONAtomic(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o644); err != nil {
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}
