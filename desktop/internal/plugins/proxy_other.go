//go:build !windows

package plugins

// systemProxy has nothing to add elsewhere: proxies there are environment
// variables, which yt-dlp and aria2c already read by themselves.
func systemProxy() string { return "" }
