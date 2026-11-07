// Package netproxy is the one answer to "does this request go through a proxy?"
// for everything the desktop sends off this machine: the launcher bootstrap,
// Nonoka Cloud, self-update, telemetry and the yt-dlp downloads. The Python
// sidecar reads the same preferences.json key and applies the same rule
// (`nonoka_x.download_route`), so the two halves never disagree.
//
// The rule, evaluated per request (cached for a few seconds):
//
//   - a custom proxy saved on the settings page -> that proxy;
//   - otherwise the Windows system proxy browsers use, then the usual
//     environment variables;
//   - a proxy on this machine whose client is not running is skipped, and the
//     request goes direct instead of failing.
//
// Go's own default transport only reads the environment variables, so without
// this a system-proxy client such as Clash would be bypassed entirely.
package netproxy

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Custom is a proxy the user typed in on the settings page. It replaces the
// system proxy outright rather than being merged with it.
type Custom struct {
	// Scheme is "http" or "socks5".
	Scheme string `json:"scheme"`
	Host   string `json:"host"`
	Port   int    `json:"port"`
}

// URL renders the proxy the way transports and child processes take it.
func (c Custom) URL() string {
	return c.Scheme + "://" + net.JoinHostPort(c.Host, strconv.Itoa(c.Port))
}

// Normalize trims and validates a custom proxy, defaulting the scheme to http
// and the host to the loopback address local proxy clients listen on.
func (c Custom) Normalize() (Custom, error) {
	c.Scheme = strings.ToLower(strings.TrimSpace(c.Scheme))
	if c.Scheme == "" {
		c.Scheme = "http"
	}
	if c.Scheme != "http" && c.Scheme != "socks5" {
		return Custom{}, errors.New("代理类型只能是 HTTP 或 SOCKS5")
	}
	c.Host = strings.TrimSpace(c.Host)
	c.Host = strings.TrimSuffix(strings.TrimPrefix(c.Host, "["), "]")
	if c.Host == "" {
		c.Host = "127.0.0.1"
	}
	if strings.ContainsAny(c.Host, "/@?# ") || strings.Contains(c.Host, "://") {
		return Custom{}, errors.New("代理地址只填主机名或 IP，例如 127.0.0.1")
	}
	if c.Port < 1 || c.Port > 65535 {
		return Custom{}, errors.New("代理端口必须在 1 到 65535 之间")
	}
	return c, nil
}

// Status describes the route in effect, for the settings page.
type Status struct {
	// Source is "custom", "system", "environment", or "" when nothing is set.
	Source string `json:"source"`
	// Configured is the proxy the source names, whether or not it answers.
	Configured string `json:"configured"`
	// Active is the proxy requests go through right now, or "" for direct.
	Active string `json:"active"`
	// System is the Windows system proxy, shown as the default.
	System string `json:"system"`
}

const cacheLifetime = 3 * time.Second

var (
	mu       sync.Mutex
	custom   *Custom
	cached   Status
	cachedAt time.Time

	// Seams for tests.
	systemProxyFunc = systemProxy
	listeningFunc   = isListening
	now             = time.Now
)

// SetCustom installs the custom proxy, or nil to follow the system proxy. It
// takes effect on the next request.
func SetCustom(value *Custom) {
	mu.Lock()
	defer mu.Unlock()
	if value == nil {
		custom = nil
	} else {
		copied := *value
		custom = &copied
	}
	cachedAt = time.Time{}
}

// SystemProxy is the proxy set under the system's internet options (what
// browsers use), as a URL, or "" when none is enabled.
func SystemProxy() string { return systemProxyFunc() }

// Resolve returns the proxy requests go through right now, or "" for direct.
func Resolve() string { return CurrentStatus().Active }

// CurrentStatus resolves the route, reusing a result younger than a few
// seconds so a burst of requests probes the local port once.
func CurrentStatus() Status {
	mu.Lock()
	defer mu.Unlock()
	if !cachedAt.IsZero() && now().Sub(cachedAt) < cacheLifetime {
		return cached
	}
	cached = resolveLocked()
	cachedAt = now()
	return cached
}

func resolveLocked() Status {
	status := Status{System: systemProxyFunc()}
	type candidate struct{ source, proxy string }
	var candidates []candidate
	if custom != nil {
		candidates = append(candidates, candidate{"custom", custom.URL()})
	} else {
		candidates = append(candidates, candidate{"system", status.System})
		for _, name := range environmentNames {
			candidates = append(candidates, candidate{"environment", os.Getenv(name)})
		}
	}
	for _, item := range candidates {
		proxy := normalizeProxy(item.proxy)
		if proxy == "" {
			continue
		}
		if status.Source == "" {
			status.Source, status.Configured = item.source, proxy
		}
		if listeningFunc(proxy) {
			status.Source, status.Configured, status.Active = item.source, proxy, proxy
			return status
		}
	}
	return status
}

var environmentNames = []string{"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"}

func normalizeProxy(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if !strings.Contains(value, "://") {
		value = "http://" + value
	}
	return value
}

// isListening reports whether a proxy can be used. Only a proxy on this
// machine is probed: its client not running is the common failure, and a
// remote one is assumed reachable rather than costing a dial per check.
func isListening(proxy string) bool {
	parsed, err := url.Parse(proxy)
	if err != nil || parsed.Hostname() == "" || parsed.Port() == "" {
		return false
	}
	if !isLoopback(parsed.Hostname()) {
		return true
	}
	connection, err := net.DialTimeout("tcp", parsed.Host, 350*time.Millisecond)
	if err != nil {
		return false
	}
	connection.Close()
	return true
}

func isLoopback(host string) bool {
	host = strings.ToLower(strings.TrimSuffix(strings.TrimPrefix(host, "["), "]"))
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// ProxyFunc is an http.Transport Proxy hook that follows Resolve. Requests to
// this machine (a local backend in development) never go through the proxy.
func ProxyFunc(request *http.Request) (*url.URL, error) {
	if request == nil || request.URL == nil || isLoopback(request.URL.Hostname()) {
		return nil, nil
	}
	proxy := Resolve()
	if proxy == "" {
		return nil, nil
	}
	parsed, err := url.Parse(proxy)
	if err != nil {
		return nil, fmt.Errorf("invalid proxy %q: %w", proxy, err)
	}
	return parsed, nil
}

// NewTransport clones the default transport with the proxy hook installed.
func NewTransport() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = ProxyFunc
	return transport
}

// NewClient is an http.Client on NewTransport.
func NewClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, Transport: NewTransport()}
}

// ChildEnvironment pins a child process to proxy ("" for direct) through the
// variables uv, aria2c, Rust and Python clients all read. Direct is spelled out
// with NO_PROXY=* so a child that would fall back to the registry does not
// pick the system proxy up behind the setting's back.
func ChildEnvironment(proxy string) []string {
	environment := make([]string, 0, len(environmentNames)+2)
	// Every spelling gets the same value: Windows treats the names as one
	// variable, and the last duplicate wins.
	for _, name := range environmentNames {
		environment = append(environment, name+"="+proxy)
	}
	if proxy == "" {
		return append(environment, "NO_PROXY=*", "no_proxy=*")
	}
	const local = "localhost,127.0.0.1,::1"
	return append(environment, "NO_PROXY="+local, "no_proxy="+local)
}

// Label names a proxy the way the runtime page shows it, matching the
// sidecar's `proxy_label`: "本地代理 127.0.0.1:7890".
func Label(proxy string) string {
	if proxy == "" {
		return ""
	}
	parsed, err := url.Parse(proxy)
	if err != nil || parsed.Host == "" {
		return "代理 " + proxy
	}
	if isLoopback(parsed.Hostname()) {
		return "本地代理 " + parsed.Host
	}
	return "代理 " + parsed.Host
}
