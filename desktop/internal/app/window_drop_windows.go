//go:build windows

package app

import (
	"log"
	"sync"

	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
	"github.com/wailsapp/wails/v3/pkg/w32"
)

// attachFileDrop registers the home window itself as the OLE drop target.
// With WebView2CompositionHosting the webview is a DirectComposition visual,
// not a child HWND, so WebView2 never sees an Explorer drag unless the host
// forwards it -- and Wails' composition host does not. Reading CF_HDROP here
// gives the frontend the same "files:dropped" event the webview path would.
func attachFileDrop(app *application.App, window *application.WebviewWindow) {
	var once sync.Once
	window.OnWindowEvent(events.Common.WindowRuntimeReady, func(_ *application.WindowEvent) {
		once.Do(func() {
			application.InvokeSync(func() { registerFileDrop(app, window) })
		})
	})
}

// fileDropTarget is kept reachable for the life of the process: OLE holds
// only the COM wrapper, not the Go value behind it.
var fileDropTarget *w32.DropTarget

func registerFileDrop(app *application.App, window *application.WebviewWindow) {
	hwnd := w32.HWND(window.NativeWindow())
	if hwnd == 0 {
		log.Printf("file drop: home window has no native handle")
		return
	}
	// RegisterDragDrop needs OLE, not just COM, on the UI thread. A repeat
	// call on an already initialised thread is a harmless S_FALSE.
	w32.OleInitialise()
	target := w32.NewDropTarget()
	target.OnEnter = func() { app.Event.Emit("files:dragging", true) }
	target.OnLeave = func() { app.Event.Emit("files:dragging", false) }
	target.OnDrop = func(filenames []string, _ int, _ int) {
		app.Event.Emit("files:dragging", false)
		app.Event.Emit("files:dropped", filenames)
	}
	if err := w32.RegisterDragDrop(hwnd, target); err != nil {
		log.Printf("file drop: register drop target: %v", err)
		return
	}
	fileDropTarget = target
}
