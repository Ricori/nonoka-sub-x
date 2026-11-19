package app

import (
	"sync"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/preferences"
	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

const windowStateDebounce = 400 * time.Millisecond

type windowStateTracker struct {
	Activate func()
	Flush    func()
}

func applyWindowOptions(store *preferences.Service, kind string, options application.WebviewWindowOptions) application.WebviewWindowOptions {
	bounds, ok := store.GetWindowBounds(kind)
	if !ok || !validWindowBounds(bounds, options.MinWidth, options.MinHeight) {
		return options
	}
	width, height := bounds.Width, bounds.Height
	if options.MaxWidth > 0 && width > options.MaxWidth {
		width = options.MaxWidth
	}
	if options.MaxHeight > 0 && height > options.MaxHeight {
		height = options.MaxHeight
	}
	options.Width, options.Height = width, height
	options.X, options.Y = bounds.X, bounds.Y
	options.InitialPosition = application.WindowXY
	options.StartState = application.WindowStateNormal
	if bounds.FullScreen {
		options.StartState = application.WindowStateFullscreen
	} else if bounds.Maximized {
		options.StartState = application.WindowStateMaximised
	}
	return options
}

func deferWindowStart(options application.WebviewWindowOptions) (application.WebviewWindowOptions, application.WindowState) {
	state := application.WindowStateNormal
	if options.StartState == application.WindowStateMaximised || options.StartState == application.WindowStateFullscreen {
		state = options.StartState
		options.Hidden = true
		options.StartState = application.WindowStateNormal
	}
	return options, state
}

func showDeferredWindow(window *application.WebviewWindow, state application.WindowState) {
	if state == application.WindowStateMaximised {
		window.Maximise()
	} else if state == application.WindowStateFullscreen {
		window.Fullscreen()
	}
	window.Show()
}

func trackWindowState(store *preferences.Service, kind string, app *application.App, window *application.WebviewWindow) windowStateTracker {
	normal, _ := store.GetWindowBounds(kind)
	var mu sync.Mutex
	var timer *time.Timer
	var initialTimer *time.Timer
	active := false
	runtimeReady := false
	initialised := false
	capture := func() {
		mu.Lock()
		maximized, fullscreen := window.IsMaximised(), window.IsFullscreen()
		if !maximized && !fullscreen {
			normal.X, normal.Y = window.Position()
			normal.Width, normal.Height = window.Size()
		}
		bounds := normal
		bounds.Maximized, bounds.FullScreen = maximized, fullscreen
		mu.Unlock()
		if validWindowBounds(bounds, 200, 160) {
			_ = store.SetWindowBounds(kind, bounds)
		}
	}
	schedule := func(_ *application.WindowEvent) {
		mu.Lock()
		if !active {
			mu.Unlock()
			return
		}
		if timer != nil {
			timer.Stop()
		}
		timer = time.AfterFunc(windowStateDebounce, capture)
		mu.Unlock()
	}
	startInitialCapture := func() {
		mu.Lock()
		if !active || !runtimeReady || initialised {
			mu.Unlock()
			return
		}
		initialised = true
		initialTimer = time.AfterFunc(150*time.Millisecond, func() {
			mu.Lock()
			initialTimer = nil
			stillActive := active
			mu.Unlock()
			if !stillActive {
				return
			}
			bounds := preferences.WindowBounds{}
			bounds.X, bounds.Y = window.Position()
			bounds.Width, bounds.Height = window.Size()
			if validWindowBounds(bounds, 200, 160) && !windowBoundsOnScreens(bounds, app.Screen.GetAll()) {
				if primary := app.Screen.GetPrimary(); primary != nil {
					area := primary.WorkArea
					window.SetPosition(area.X+(area.Width-bounds.Width)/2, area.Y+(area.Height-bounds.Height)/2)
				}
			}
			capture()
		})
		mu.Unlock()
	}
	window.OnWindowEvent(events.Common.WindowRuntimeReady, func(_ *application.WindowEvent) {
		mu.Lock()
		runtimeReady = true
		mu.Unlock()
		startInitialCapture()
	})
	for _, eventType := range []events.WindowEventType{events.Common.WindowDidMove, events.Common.WindowDidResize, events.Common.WindowMaximise, events.Common.WindowUnMaximise, events.Common.WindowFullscreen, events.Common.WindowUnFullscreen} {
		window.OnWindowEvent(eventType, schedule)
	}
	return windowStateTracker{
		Activate: func() {
			mu.Lock()
			active = true
			mu.Unlock()
			startInitialCapture()
		},
		Flush: func() {
			mu.Lock()
			if timer != nil {
				timer.Stop()
				timer = nil
			}
			if initialTimer != nil {
				initialTimer.Stop()
				initialTimer = nil
			}
			shouldCapture := active
			active = false
			mu.Unlock()
			if shouldCapture {
				capture()
			}
		},
	}
}

func validWindowBounds(bounds preferences.WindowBounds, minWidth, minHeight int) bool {
	return bounds.Width >= minWidth && bounds.Height >= minHeight && bounds.Width <= 16384 && bounds.Height <= 16384 && bounds.X >= -65536 && bounds.X <= 65536 && bounds.Y >= -65536 && bounds.Y <= 65536
}

func windowBoundsOnScreens(bounds preferences.WindowBounds, screens []*application.Screen) bool {
	if len(screens) == 0 {
		return true
	}
	for _, screen := range screens {
		if screen == nil {
			continue
		}
		area := screen.WorkArea
		left, top := max(bounds.X, area.X), max(bounds.Y, area.Y)
		right, bottom := min(bounds.X+bounds.Width, area.X+area.Width), min(bounds.Y+bounds.Height, area.Y+area.Height)
		if right-left >= 64 && bottom-top >= 64 {
			return true
		}
	}
	return false
}
