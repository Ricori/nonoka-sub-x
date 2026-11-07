//go:build !windows

package netproxy

// systemProxy has nothing to add elsewhere: proxies there are environment
// variables, which Resolve already reads after the system proxy.
func systemProxy() string { return "" }
