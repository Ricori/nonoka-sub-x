package app

import (
	"errors"
	"fmt"
	"net/url"
	"strings"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/preferences"
	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

// VideoExportDraft describes the immutable snapshot held for an independent
// export window. The potentially large ASS text stays in Go and is never sent
// back through the webview bridge.
type VideoExportDraft struct {
	JobID        string   `json:"jobId"`
	ProgressID   string   `json:"progressId"`
	MediaID      string   `json:"mediaId"`
	DefaultName  string   `json:"defaultName"`
	T0           float64  `json:"t0"`
	T1           float64  `json:"t1"`
	RangeLabel   string   `json:"rangeLabel"`
	MissingFonts []string `json:"missingFonts"`
}

type VideoExportResult struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
}

// OpenVideoExport creates one modeless native export window. One window is
// enough for now: while it exists a second request focuses it instead of
// replacing the subtitle snapshot of a running encode.
func (s *WindowService) OpenVideoExport(mediaID, defaultName, ass string, t0, t1 float64, rangeLabel string, missingFonts []string) error {
	if strings.TrimSpace(mediaID) == "" || strings.TrimSpace(ass) == "" || t1 <= t0 {
		return errors.New("invalid video export draft")
	}
	if _, err := s.library.Get(mediaID); err != nil {
		return err
	}

	s.mu.Lock()
	if s.app == nil {
		s.mu.Unlock()
		return errors.New("application window is not ready")
	}
	if s.export != nil {
		window := s.export
		visible := s.exportVisible
		s.mu.Unlock()
		if visible {
			window.Show()
			window.Focus()
		}
		return nil
	}

	s.exportSerial++
	jobID := fmt.Sprintf("video_export_%d", s.exportSerial)
	draft := VideoExportDraft{
		JobID: jobID, ProgressID: "exp_" + mediaID,
		MediaID: mediaID, DefaultName: defaultName,
		T0: t0, T1: t1, RangeLabel: rangeLabel,
		MissingFonts: append([]string(nil), missingFonts...),
	}
	options, deferredState := deferWindowStart(exportWindowOptions(s.preferences, jobID, defaultName))
	window := s.app.Window.NewWithOptions(options)
	s.export = window
	s.exportDraft = draft
	s.exportASS = ass
	s.exportRunning = false
	s.exportClosing = false
	s.exportVisible = false
	s.exportStartState = deferredState
	tracker := trackWindowState(s.preferences, "export", s.app, window)
	s.activateExportTracking = tracker.Activate
	s.flushExportWindowState = tracker.Flush
	s.mu.Unlock()

	library.SetExportWindow(s.library, window)
	s.installExportHooks(window)
	return nil
}

func exportWindowOptions(store *preferences.Service, jobID, defaultName string) application.WebviewWindowOptions {
	title := strings.TrimSuffix(defaultName, ".mp4")
	if title == "" {
		title = "视频"
	}
	return applyWindowOptions(store, "export", applyWindowTheme(store, "editor", application.WebviewWindowOptions{
		Name:            "export",
		Title:           "导出视频 · " + title,
		Width:           560,
		Height:          650,
		MinWidth:        480,
		MinHeight:       560,
		InitialPosition: application.WindowCentered,
		StartState:      application.WindowStateNormal,
		Hidden:          true,
		URL:             "/export.html?job=" + url.QueryEscape(jobID),
	}))
}

func (s *WindowService) handleExportReady(event *application.CustomEvent) {
	jobID, ok := event.Data.(string)
	if !ok || (event.Sender != "" && event.Sender != "export") {
		return
	}
	s.mu.Lock()
	if s.export == nil || s.exportDraft.JobID != jobID || s.exportClosing || s.exportVisible {
		s.mu.Unlock()
		return
	}
	window := s.export
	startState := s.exportStartState
	activateTracking := s.activateExportTracking
	s.exportVisible = true
	s.mu.Unlock()
	showDeferredWindow(window, startState)
	if activateTracking != nil {
		activateTracking()
	}
	window.Focus()
}

func (s *WindowService) VideoExportDraft(jobID string) (VideoExportDraft, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.export == nil || s.exportDraft.JobID != jobID {
		return VideoExportDraft{}, errors.New("video export draft is unavailable")
	}
	draft := s.exportDraft
	draft.MissingFonts = append([]string(nil), draft.MissingFonts...)
	return draft, nil
}

// RunVideoExport owns the long-running call on behalf of the export window.
// The source, subtitle text and output range come only from the stored draft;
// the renderer can change encoding knobs but cannot silently swap the project.
func (s *WindowService) RunVideoExport(jobID string, crf int, preset string, scaleH int, abr string) (VideoExportResult, error) {
	s.mu.Lock()
	if s.export == nil || s.exportDraft.JobID != jobID {
		s.mu.Unlock()
		return VideoExportResult{}, errors.New("video export draft is unavailable")
	}
	if s.exportRunning {
		s.mu.Unlock()
		return VideoExportResult{}, errors.New("video export is already running")
	}
	draft := s.exportDraft
	ass := s.exportASS
	s.exportRunning = true
	s.mu.Unlock()

	defer func() {
		s.mu.Lock()
		if s.exportDraft.JobID == jobID {
			s.exportRunning = false
		}
		s.mu.Unlock()
	}()
	result, err := s.library.ExportVideoRange(
		draft.MediaID, draft.DefaultName, ass, draft.T0, draft.T1,
		crf, preset, scaleH, abr,
	)
	return VideoExportResult{Path: result.Path, Size: result.Size}, err
}

func (s *WindowService) CloseVideoExport() {
	s.mu.Lock()
	window := s.export
	if window == nil {
		s.mu.Unlock()
		return
	}
	if s.exportClosing {
		s.mu.Unlock()
		return
	}
	s.exportClosing = true
	running := s.exportRunning
	mediaID := s.exportDraft.MediaID
	flush := s.flushExportWindowState
	s.mu.Unlock()

	if running {
		_ = s.library.CancelExport(mediaID)
	}
	if flush != nil {
		flush()
	}
	window.Close()
}

func (s *WindowService) installExportHooks(window *application.WebviewWindow) {
	window.RegisterHook(events.Common.WindowClosing, func(event *application.WindowEvent) {
		s.mu.Lock()
		if s.export != window {
			s.mu.Unlock()
			return
		}
		if !s.exportClosing {
			app := s.app
			jobID := s.exportDraft.JobID
			s.mu.Unlock()
			event.Cancel()
			if app != nil {
				app.Event.Emit("export:request-close", jobID)
			}
			return
		}
		s.export = nil
		s.exportDraft = VideoExportDraft{}
		s.exportASS = ""
		s.exportRunning = false
		s.exportClosing = false
		s.exportVisible = false
		s.exportStartState = application.WindowStateNormal
		s.activateExportTracking = nil
		s.flushExportWindowState = nil
		s.mu.Unlock()
		library.SetExportWindow(s.library, nil)
	})
}
