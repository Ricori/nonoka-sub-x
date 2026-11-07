package app

import (
	"context"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/netproxy"
)

// bootstrapRoute is where the launcher bootstrap fetches uv, Python and its
// base dependencies from. It follows the same rule as the sidecar's
// `nonoka_x.download_route`:
//
//   - a proxy is in effect (`netproxy`) -> the official sources, through it;
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
	return resolveRoute(netproxy.Resolve(), probeMainland)
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

// environment hands the same route to uv, which reads only the environment.
// Direct is spelled out too, so uv cannot pick up a proxy the route skipped.
func (r bootstrapRoute) environment() []string {
	return netproxy.ChildEnvironment(r.Proxy)
}

// proxyLabel names the proxy the way the runtime page shows it.
func proxyLabel(proxy string) string { return netproxy.Label(proxy) }
