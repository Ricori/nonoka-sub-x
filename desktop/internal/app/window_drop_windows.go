//go:build windows

package app

import (
	"log"
	"sync"
	"unsafe"

	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
	"github.com/wailsapp/wails/v3/pkg/w32"
	"golang.org/x/sys/windows"
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

var (
	modOle32             = windows.NewLazySystemDLL("ole32.dll")
	modShell32           = windows.NewLazySystemDLL("shell32.dll")
	procRegisterDragDrop = modOle32.NewProc("RegisterDragDrop")
	procReleaseStgMedium = modOle32.NewProc("ReleaseStgMedium")
	procDragQueryFileW   = modShell32.NewProc("DragQueryFileW")
)

var (
	iidIUnknown    = windows.GUID{Data1: 0x00000000, Data4: [8]byte{0xC0, 0, 0, 0, 0, 0, 0, 0x46}}
	iidIDropTarget = windows.GUID{Data1: 0x00000122, Data4: [8]byte{0xC0, 0, 0, 0, 0, 0, 0, 0x46}}
	hdropFormat    = w32.FORMATETC{CfFormat: w32.CF_HDROP, DwAspect: 1, Lindex: -1, Tymed: w32.TYMED_HGLOBAL}
)

const (
	dropEffectNone = 0
	dropEffectCopy = 1
	eNoInterface   = 0x80004002
	ePointer       = 0x80004003
)

// fileDropTarget is a hand-rolled IDropTarget. Wails' w32.DropTarget accepts
// any drag, so a drag of text or a page element raised the import overlay and
// -- having no CF_HDROP to read on drop -- never cleared it. This one only
// takes drags that carry files and turns everything else away at DragEnter.
type fileDropTarget struct {
	vtbl  *fileDropTargetVtbl
	app   *application.App
	files bool // the drag in progress carries CF_HDROP
}

type fileDropTargetVtbl struct {
	QueryInterface, AddRef, Release      uintptr
	DragEnter, DragOver, DragLeave, Drop uintptr
}

type stgMedium struct {
	tymed         uint32
	handle        uintptr
	unkForRelease uintptr
}

// activeDropTarget keeps the target reachable for the life of the process:
// OLE holds a raw pointer the garbage collector cannot see. All callbacks run
// on the UI thread that registered it, so the one instance needs no locking.
var activeDropTarget *fileDropTarget

func registerFileDrop(app *application.App, window *application.WebviewWindow) {
	hwnd := uintptr(window.NativeWindow())
	if hwnd == 0 {
		log.Printf("file drop: home window has no native handle")
		return
	}
	// RegisterDragDrop needs OLE, not just COM, on the UI thread. A repeat
	// call on an already initialised thread is a harmless S_FALSE.
	w32.OleInitialise()
	target := &fileDropTarget{app: app, vtbl: &fileDropTargetVtbl{
		QueryInterface: windows.NewCallback(dropQueryInterface),
		AddRef:         windows.NewCallback(dropAddRef),
		Release:        windows.NewCallback(dropAddRef),
		DragEnter:      windows.NewCallback(dropDragEnter),
		DragOver:       windows.NewCallback(dropDragOver),
		DragLeave:      windows.NewCallback(dropDragLeave),
		Drop:           windows.NewCallback(dropDrop),
	}}
	activeDropTarget = target
	if hr, _, _ := procRegisterDragDrop.Call(hwnd, uintptr(unsafe.Pointer(target))); hr != 0 {
		activeDropTarget = nil
		log.Printf("file drop: register drop target: HRESULT 0x%08x", uint32(hr))
	}
}

func dropQueryInterface(this uintptr, riid *windows.GUID, out *uintptr) uintptr {
	if out == nil {
		return ePointer
	}
	if riid != nil && (*riid == iidIUnknown || *riid == iidIDropTarget) {
		*out = this
		return 0
	}
	*out = 0
	return eNoInterface
}

// dropAddRef serves AddRef and Release alike: the target is a process-wide
// singleton, so there is no count to keep.
func dropAddRef(_ uintptr) uintptr { return 1 }

func dropDragEnter(_ uintptr, data *w32.IDataObject, _ uintptr, _ uintptr, effect *uint32) uintptr {
	target := activeDropTarget
	target.files = carriesFiles(data)
	setDropEffect(effect, target.files)
	if target.files {
		target.app.Event.Emit("files:dragging", true)
	}
	return 0
}

func dropDragOver(_ uintptr, _ uintptr, _ uintptr, effect *uint32) uintptr {
	setDropEffect(effect, activeDropTarget.files)
	return 0
}

func dropDragLeave(_ uintptr) uintptr {
	target := activeDropTarget
	if target.files {
		target.files = false
		target.app.Event.Emit("files:dragging", false)
	}
	return 0
}

func dropDrop(_ uintptr, data *w32.IDataObject, _ uintptr, _ uintptr, effect *uint32) uintptr {
	target := activeDropTarget
	if !target.files {
		setDropEffect(effect, false)
		return 0
	}
	target.files = false
	target.app.Event.Emit("files:dragging", false)
	paths := droppedPaths(data)
	setDropEffect(effect, len(paths) > 0)
	if len(paths) > 0 {
		target.app.Event.Emit("files:dropped", paths)
	}
	return 0
}

func setDropEffect(effect *uint32, accept bool) {
	if effect == nil {
		return
	}
	if accept {
		*effect = dropEffectCopy
	} else {
		*effect = dropEffectNone
	}
}

func carriesFiles(data *w32.IDataObject) bool {
	if data == nil {
		return false
	}
	hr, _, _ := data.Vtbl.QueryGetData.Call(uintptr(unsafe.Pointer(data)), uintptr(unsafe.Pointer(&hdropFormat)))
	return hr == 0
}

func droppedPaths(data *w32.IDataObject) []string {
	if data == nil {
		return nil
	}
	var medium stgMedium
	hr, _, _ := data.Vtbl.GetData.Call(uintptr(unsafe.Pointer(data)), uintptr(unsafe.Pointer(&hdropFormat)), uintptr(unsafe.Pointer(&medium)))
	if hr != 0 {
		return nil
	}
	defer procReleaseStgMedium.Call(uintptr(unsafe.Pointer(&medium)))
	count, _, _ := procDragQueryFileW.Call(medium.handle, 0xFFFFFFFF, 0, 0)
	paths := make([]string, 0, count)
	for i := uintptr(0); i < count; i++ {
		size, _, _ := procDragQueryFileW.Call(medium.handle, i, 0, 0)
		if size == 0 {
			continue
		}
		buffer := make([]uint16, size+1)
		if n, _, _ := procDragQueryFileW.Call(medium.handle, i, uintptr(unsafe.Pointer(&buffer[0])), size+1); n == 0 {
			continue
		}
		paths = append(paths, windows.UTF16ToString(buffer))
	}
	return paths
}
