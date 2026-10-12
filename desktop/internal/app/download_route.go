package app

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/plugins"
)

// bootstrapRoute is where the launcher bootstrap fetches uv, Python and its
// base dependencies from. It follows the same rule as the sidecar's
// `nonoka_x.download_route`:
//
//   - a proxy is configured and answering -> the official sources, through it;
//   - no proxy, mainland exit -> the domestic mirrors first;
//   - no proxy, anywhere else -> the official sources, direct.
//
// FINESUB_DOWNLOAD_REGION set to cn or global overrides the region either way.
type bootstrapRoute struct {
	// Proxy is the proxy URL every download goes through, or "" for direct.
	Proxy string
	// Mirror prefers the mainland mirrors over the official sources.
	Mirror bool
}

const regionEnvironment = "FINESUB_DOWNLOAD_REGION"

// The same public country endpoints FineSub's own probe uses, bounded the same
// way: deciding a route must never cost more than it saves.
var countryEndpoints = []string{
	"https://ipinfo.io/country",
	"https://ifconfig.co/country-iso",
}

const (
	countryProbeBudget  = 3 * time.Second
	countryProbeTimeout = 1500 * time.Millisecond
)

func resolveBootstrapRoute() bootstrapRoute {
	return resolveRoute(liveProxy(), probeMainland)
}

func resolveRoute(proxy string, mainland func() bool) bootstrapRoute {
	route := bootstrapRoute{Proxy: proxy}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(regionEnvironment))) {
	case "cn":
		route.Mirror = true
	case "global":
	default:
		route.Mirror = proxy == "" && mainland()
	}
	return route
}

// liveProxy returns the first configured proxy that is actually reachable:
// the system proxy first, then the usual environment variables -- the order
// FineSub's `network_routes` uses. A proxy on this machine whose client is not
// running is skipped instead of failing every download.
func liveProxy() string {
	candidates := []string{plugins.SystemProxy()}
	for _, name := range []string{"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"} {
		candidates = append(candidates, os.Getenv(name))
	}
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if !strings.Contains(candidate, "://") {
			candidate = "http://" + candidate
		}
		if proxyIsListening(candidate) {
			return candidate
		}
	}
	return ""
}

func proxyIsListening(proxy string) bool {
	parsed, err := url.Parse(proxy)
	if err != nil || parsed.Hostname() == "" || parsed.Port() == "" {
		return false
	}
	switch strings.ToLower(parsed.Hostname()) {
	case "127.0.0.1", "localhost", "::1":
	default:
		return true
	}
	connection, err := net.DialTimeout("tcp", parsed.Host, 350*time.Millisecond)
	if err != nil {
		return false
	}
	connection.Close()
	return true
}

// probeMainland asks where a direct connection comes out. Any failure means
// "not mainland", which is the official source.
func probeMainland() bool {
	client := &http.Client{Transport: directTransport(), Timeout: countryProbeTimeout}
	deadline := time.Now().Add(countryProbeBudget)
	for _, endpoint := range countryEndpoints {
		if time.Now().After(deadline) {
			break
		}
		ctx, cancel := context.WithDeadline(context.Background(), deadline)
		country := fetchCountry(ctx, client, endpoint)
		cancel()
		if country != "" {
			return country == "CN"
		}
	}
	return false
}

func fetchCountry(ctx context.Context, client *http.Client, endpoint string) string {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return ""
	}
	response, err := client.Do(request)
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return ""
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 64))
	if err != nil {
		return ""
	}
	country := strings.ToUpper(strings.TrimSpace(string(body)))
	if len(country) != 2 {
		return ""
	}
	return country
}

func directTransport() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return transport
}

// transport pins every request to this route's proxy. Go's default transport
// only reads the environment variables, so without this a system-proxy client
// such as Clash is bypassed entirely.
func (r bootstrapRoute) transport() *http.Transport {
	transport := directTransport()
	if r.Proxy != "" {
		if proxyURL, err := url.Parse(r.Proxy); err == nil {
			transport.Proxy = http.ProxyURL(proxyURL)
		}
	}
	return transport
}

// environment hands the same proxy to uv, which reads only the environment.
func (r bootstrapRoute) environment() []string {
	if r.Proxy == "" {
		return nil
	}
	return []string{"HTTPS_PROXY=" + r.Proxy, "HTTP_PROXY=" + r.Proxy, "ALL_PROXY=" + r.Proxy}
}

// proxyLabel names the proxy the way the runtime page shows it, matching the
// sidecar's `proxy_label`: "本地代理 127.0.0.1:7890".
func proxyLabel(proxy string) string {
	if proxy == "" {
		return ""
	}
	parsed, err := url.Parse(proxy)
	if err != nil || parsed.Host == "" {
		return "代理 " + proxy
	}
	switch strings.ToLower(parsed.Hostname()) {
	case "127.0.0.1", "localhost", "::1":
		return "本地代理 " + parsed.Host
	}
	return "代理 " + parsed.Host
}
