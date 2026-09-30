//go:build windows

package plugins

import winregistry "golang.org/x/sys/windows/registry"

// systemProxy reads the proxy set under Windows "Internet Options", which is
// where system-proxy clients write theirs.
func systemProxy() string {
	key, err := winregistry.OpenKey(winregistry.CURRENT_USER,
		`Software\Microsoft\Windows\CurrentVersion\Internet Settings`, winregistry.QUERY_VALUE)
	if err != nil {
		return ""
	}
	defer key.Close()
	if enabled, _, err := key.GetIntegerValue("ProxyEnable"); err != nil || enabled == 0 {
		return ""
	}
	server, _, err := key.GetStringValue("ProxyServer")
	if err != nil {
		return ""
	}
	return parseSystemProxy(server)
}
