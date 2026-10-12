//go:build !windows

package app

import "github.com/wailsapp/wails/v3/pkg/application"

// attachFileDrop is a no-op off Windows: the native webviews there deliver
// file drops through Wails' WindowFilesDropped event.
func attachFileDrop(_ *application.App, _ *application.WebviewWindow) {}
