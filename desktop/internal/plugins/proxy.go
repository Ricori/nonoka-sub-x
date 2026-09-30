package plugins

import (
	"os"
	"strings"
)

// downloadProxy is the proxy a download should be pinned to, or "" when the
// children already find it themselves. yt-dlp is Python, and Python's urllib
// reads the Windows system proxy from the registry, so extraction reaches
// YouTube through a system-proxy client such as Clash; aria2c only reads
// environment variables, so the media transfer went direct and timed out. Naming
// the proxy on yt-dlp's command line fixes both at once: yt-dlp forwards its
// --proxy to aria2c as --all-proxy. It also keeps the two on the same exit, which
// matters because googlevideo URLs are signed for the IP that requested them.
func downloadProxy() string {
	// Both children honour these variables on their own, and an explicit --proxy
	// would override whatever the user deliberately set there.
	for _, name := range []string{"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"} {
		if strings.TrimSpace(os.Getenv(name)) != "" {
			return ""
		}
	}
	return systemProxy()
}

// parseSystemProxy turns the registry's ProxyServer value into a proxy URL.
// Windows stores either one "host:port" for every protocol, or a per-protocol
// list such as "http=host:port;https=host:port;socks=host:port". A PAC script
// or ProxyOverride bypass list is not interpreted: the proxy clients users run
// for YouTube set a plain server, and googlevideo is never on a bypass list.
func parseSystemProxy(server string) string {
	server = strings.TrimSpace(server)
	if server == "" {
		return ""
	}
	if !strings.Contains(server, "=") {
		return withProxyScheme(server, "http")
	}
	byProtocol := map[string]string{}
	for _, entry := range strings.Split(server, ";") {
		protocol, address, found := strings.Cut(entry, "=")
		protocol = strings.ToLower(strings.TrimSpace(protocol))
		address = strings.TrimSpace(address)
		if found && address != "" {
			byProtocol[protocol] = address
		}
	}
	for _, protocol := range []string{"https", "http"} {
		if address := byProtocol[protocol]; address != "" {
			return withProxyScheme(address, "http")
		}
	}
	if address := byProtocol["socks"]; address != "" {
		return withProxyScheme(address, "socks5")
	}
	return ""
}

func withProxyScheme(address, scheme string) string {
	if strings.Contains(address, "://") {
		return address
	}
	return scheme + "://" + address
}

// aria2cSupportsProxy reports whether aria2c can go through proxy. It speaks
// HTTP proxies only; handed a SOCKS one it would fail every connection, so the
// caller leaves yt-dlp on its own downloader, which does speak SOCKS.
func aria2cSupportsProxy(proxy string) bool {
	lower := strings.ToLower(proxy)
	return proxy == "" || strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://")
}
