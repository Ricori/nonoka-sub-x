package app

import (
	"errors"
	"net/url"
	"runtime"
	"sync"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/preferences"
	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

// WindowService coordinates the home, editor and independent video-export
// windows. The editor owns its unsaved-change UI; native close requests are
// forwarded to it and only CloseEditor is allowed to destroy that window.
type WindowService struct {
	mu                     sync.Mutex
	app                    *application.App
	home                   *application.WebviewWindow
	editor                 *application.WebviewWindow
	editorID               string
	closing                bool
	editorVisible          bool
	editorStartState       application.WindowState
	activateEditorTracking func()
	flushEditorWindowState func()
	export                 *application.WebviewWindow
	exportDraft            VideoExportDraft
	exportASS              string
	exportSerial           uint64
	exportRunning          bool
	exportClosing          bool
	exportVisible          bool
	exportStartState       application.WindowState
	activateExportTracking func()
	flushExportWindowState func()
	readyListenerInstalled bool
	preferences            *preferences.Service
	library                *library.Service
}

func NewWindowService(preferencesService *preferences.Service, libraryService *library.Service) *WindowService {
	return &WindowService{preferences: preferencesService, library: libraryService}
}

func (s *WindowService) attach(app *application.App, home *application.WebviewWindow) {
	s.mu.Lock()
	s.app = app
	s.home = home
	installReadyListener := !s.readyListenerInstalled
	s.readyListenerInstalled = true
	s.mu.Unlock()
	if installReadyListener {
		app.Event.On("editor:ready", s.handleEditorReady)
		app.Event.On("export:ready", s.handleExportReady)
	}
}

func (s *WindowService) OpenEditor(id string) error {
	entry, err := s.library.Get(id)
	if err != nil {
		return err
	}

	s.mu.Lock()
	if s.app == nil || s.home == nil {
		s.mu.Unlock()
		return errors.New("application window is not ready")
	}
	if s.editor != nil {
		editor := s.editor
		visible := s.editorVisible
		s.mu.Unlock()
		if visible {
			editor.Show()
			editor.Focus()
		}
		return nil
	}

	options := editorWindowOptions(s.preferences, id, entry.Title)
	options, deferredState := deferWindowStart(options)
	editor := s.app.Window.NewWithOptions(options)
	s.editor = editor
	s.editorID = id
	s.closing = false
	s.editorVisible = false
	s.editorStartState = deferredState
	app := s.app
	tracker := trackWindowState(s.preferences, "editor", app, editor)
	s.activateEditorTracking = tracker.Activate
	s.flushEditorWindowState = tracker.Flush
	s.mu.Unlock()

	library.SetEditorWindow(s.library, editor)
	_ = s.library.SetActiveMedia(id)
	s.installEditorHooks(editor)
	return nil
}

func (s *WindowService) handleEditorReady(event *application.CustomEvent) {
	id, ok := event.Data.(string)
	if !ok || (event.Sender != "" && event.Sender != "editor") {
		return
	}
	s.mu.Lock()
	if s.editor == nil || s.editorID != id || s.closing || s.editorVisible {
		s.mu.Unlock()
		return
	}
	editor, home := s.editor, s.home
	startState := s.editorStartState
	activateTracking := s.activateEditorTracking
	s.editorVisible = true
	s.mu.Unlock()

	showDeferredWindow(editor, startState)
	if activateTracking != nil {
		activateTracking()
	}
	editor.Focus()
	if home != nil {
		home.Hide()
	}
}

func editorWindowOptions(store *preferences.Service, id, title string) application.WebviewWindowOptions {
	return applyWindowOptions(store, "editor", applyWindowTheme(store, "editor", application.WebviewWindowOptions{
		Name:            "editor",
		Title:           "Nonoka X · " + title,
		Width:           1440,
		Height:          900,
		MinWidth:        960,
		MinHeight:       620,
		InitialPosition: application.WindowCentered,
		StartState:      application.WindowStateNormal,
		Hidden:          true,
		URL:             "/editor.html?id=" + url.QueryEscape(id),
		Frameless:       runtime.GOOS == "windows",
		Mac: application.MacWindow{
			TitleBar:                application.MacTitleBarHiddenInsetUnified,
			InvisibleTitleBarHeight: 48,
			TabbingMode:             application.MacWindowTabbingModeDisallowed,
		},
		Windows: application.WindowsWindow{
			NonClientRegionSupport:     true,
			WebView2CompositionHosting: true,
		},
	}))
}

// CloseEditor is called only after the editor has resolved its unsaved state.
func (s *WindowService) CloseEditor() {
	s.mu.Lock()
	editor := s.editor
	if editor == nil {
		home := s.home
		s.mu.Unlock()
		if home != nil {
			home.Show()
			home.Focus()
		}
		return
	}
	s.closing = true
	flushWindowState := s.flushEditorWindowState
	s.mu.Unlock()
	if flushWindowState != nil {
		flushWindowState()
	}
	editor.Close()
}

func (s *WindowService) installEditorHooks(editor *application.WebviewWindow) {
	editor.RegisterHook(events.Common.WindowClosing, func(event *application.WindowEvent) {
		s.mu.Lock()
		if s.editor != editor {
			s.mu.Unlock()
			return
		}
		if !s.closing {
			app := s.app
			editorID := s.editorID
			s.mu.Unlock()
			event.Cancel()
			if app != nil {
				app.Event.Emit("editor:request-close", editorID)
			}
			return
		}
		s.editor = nil
		s.editorID = ""
		s.closing = false
		s.editorVisible = false
		s.editorStartState = application.WindowStateNormal
		s.activateEditorTracking = nil
		s.flushEditorWindowState = nil
		home := s.home
		app := s.app
		s.mu.Unlock()

		library.SetEditorWindow(s.library, nil)
		_ = s.library.SetActiveMedia("")
		if home != nil {
			home.Show()
			home.Focus()
		}
		if app != nil {
			app.Event.Emit("home:refresh", true)
		}
	})
}

// editorIsOpen reports whether the editor window is holding a session, which
// defers a mandatory update install until the user closes it.
func (s *WindowService) editorIsOpen() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.editor != nil
}
