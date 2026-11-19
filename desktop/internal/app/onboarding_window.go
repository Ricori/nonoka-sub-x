package app

import (
	"errors"
	"runtime"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/preferences"
	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

func (s *WindowService) configureHomeStart(hiddenAtStart bool, startState application.WindowState, activateTracking func()) {
	s.mu.Lock()
	s.homeHiddenAtStart = hiddenAtStart
	s.homeStartState = startState
	s.activateHomeTracking = activateTracking
	s.mu.Unlock()
}

// ShowHome reveals the home window when it started hidden for first-launch
// onboarding detection and turned out to belong to an existing user.
func (s *WindowService) ShowHome() {
	s.mu.Lock()
	home := s.home
	hidden := s.homeHiddenAtStart
	startState := s.homeStartState
	activate := s.activateHomeTracking
	s.homeHiddenAtStart = false
	s.mu.Unlock()
	if home == nil {
		return
	}
	if hidden {
		showDeferredWindow(home, startState)
		if activate != nil {
			activate()
		}
	} else {
		home.Show()
	}
	home.Focus()
}

// OpenOnboarding opens the dedicated setup/migration window and hides home
// once it is painted. When reopened is true (opened manually from Settings),
// closing the window restores home; otherwise closing it exits the app.
func (s *WindowService) OpenOnboarding(reopened bool) error {
	s.mu.Lock()
	if s.app == nil || s.home == nil {
		s.mu.Unlock()
		return errors.New("application window is not ready")
	}
	if s.onboarding != nil {
		window := s.onboarding
		visible := s.onboardingVisible
		if reopened {
			s.onboardingReopened = true
		}
		s.mu.Unlock()
		if visible {
			window.Show()
			window.Focus()
		}
		return nil
	}

	window := s.app.Window.NewWithOptions(onboardingWindowOptions(s.preferences))
	s.onboarding = window
	s.onboardingVisible = false
	s.onboardingClosing = false
	s.onboardingReopened = reopened
	s.mu.Unlock()

	s.installOnboardingHooks(window)
	window.OnWindowEvent(events.Common.WindowRuntimeReady, func(_ *application.WindowEvent) {
		time.AfterFunc(400*time.Millisecond, func() {
			s.revealOnboarding(window)
		})
	})
	return nil
}

func onboardingWindowOptions(store *preferences.Service) application.WebviewWindowOptions {
	return applyWindowTheme(store, "home", application.WebviewWindowOptions{
		Name:            "onboarding",
		Title:           "Nonoka Sub X · 配置向导",
		Width:           920,
		Height:          650,
		MinWidth:        840,
		MinHeight:       560,
		InitialPosition: application.WindowCentered,
		StartState:      application.WindowStateNormal,
		Hidden:          true,
		URL:             "/index.html?window=onboarding",
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
	})
}

func (s *WindowService) handleOnboardingReady(event *application.CustomEvent) {
	if event.Sender != "" && event.Sender != "onboarding" {
		return
	}
	s.mu.Lock()
	window := s.onboarding
	s.mu.Unlock()
	if window != nil {
		s.revealOnboarding(window)
	}
}

func (s *WindowService) revealOnboarding(window *application.WebviewWindow) {
	s.mu.Lock()
	if s.onboarding != window || s.onboardingClosing || s.onboardingVisible {
		s.mu.Unlock()
		return
	}
	home := s.home
	s.onboardingVisible = true
	s.mu.Unlock()

	window.Show()
	window.Focus()
	if home != nil {
		home.Hide()
	}
}

// ensureInitialWindowVisible is a safety net when home starts hidden for
// onboarding detection: if neither onboarding nor home has appeared within a
// few seconds, reveal home so the process never sits invisibly with no window.
func (s *WindowService) ensureInitialWindowVisible() {
	s.mu.Lock()
	shouldRevealHome := s.homeHiddenAtStart && s.onboarding == nil
	s.mu.Unlock()
	if shouldRevealHome {
		s.ShowHome()
	}
}

// CompleteOnboarding transitions from the setup window into the main workspace.
func (s *WindowService) CompleteOnboarding() {
	s.mu.Lock()
	window := s.onboarding
	app := s.app
	if window == nil {
		s.mu.Unlock()
		s.ShowHome()
		if app != nil {
			app.Event.Emit("home:refresh", true)
		}
		return
	}
	s.onboardingClosing = true
	s.mu.Unlock()

	s.ShowHome()
	if app != nil {
		app.Event.Emit("home:refresh", true)
	}
	window.Close()
}

func (s *WindowService) installOnboardingHooks(window *application.WebviewWindow) {
	window.RegisterHook(events.Common.WindowClosing, func(_ *application.WindowEvent) {
		s.mu.Lock()
		if s.onboarding != window {
			s.mu.Unlock()
			return
		}
		closing := s.onboardingClosing
		reopened := s.onboardingReopened
		app := s.app
		s.onboarding = nil
		s.onboardingVisible = false
		s.onboardingClosing = false
		s.onboardingReopened = false
		s.mu.Unlock()

		if closing {
			return
		}
		if reopened {
			s.ShowHome()
			if app != nil {
				app.Event.Emit("home:refresh", true)
			}
			return
		}
		// First launch or active migration closed by user: exit cleanly instead
		// of leaving a hidden home window running in the background.
		if app != nil {
			app.Quit()
		}
	})
}
