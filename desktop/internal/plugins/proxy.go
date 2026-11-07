package plugins

import (
	"strings"

	"github.com/Ricori/nonoka-x/desktop/internal/netproxy"
)

// downloadProxy is the proxy a yt-dlp download is pinned to, or "" for direct.
// yt-dlp is Python, and Python's urllib would read the Windows system proxy
// from the registry on its own, while aria2c only reads environment
// variables; naming the app's route on yt-dlp's command line keeps both on it.
// yt-dlp forwards its --proxy to aria2c as --all-proxy, which also keeps the
// two on the same exit -- googlevideo URLs are signed for the IP that
// requested them. "" is passed through as `--proxy ""`, yt-dlp's spelling of
// "direct", so a custom proxy that is down cannot fall back to the registry.
func downloadProxy() string { return netproxy.Resolve() }

// aria2cSupportsProxy reports whether aria2c can go through proxy. It speaks
// HTTP proxies only; handed a SOCKS one it would fail every connection, so the
// caller leaves yt-dlp on its own downloader, which does speak SOCKS.
func aria2cSupportsProxy(proxy string) bool {
	lower := strings.ToLower(proxy)
	return proxy == "" || strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://")
}
