package netproxy

import (
	"net"
	"net/http"
	"net/url"
	"testing"
	"time"
)

// stub replaces the system proxy and the liveness probe, and clears custom
// and cache state, for one test.
func stub(t *testing.T, system string, live map[string]bool) {
	t.Helper()
	for _, name := range environmentNames {
		t.Setenv(name, "")
	}
	previousSystem, previousListening := systemProxyFunc, listeningFunc
	systemProxyFunc = func() string { return system }
	listeningFunc = func(proxy string) bool { return live[proxy] }
	SetCustom(nil)
	t.Cleanup(func() {
		systemProxyFunc, listeningFunc = previousSystem, previousListening
		SetCustom(nil)
	})
}

func TestResolveFollowsSystemProxyByDefault(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", map[string]bool{"http://127.0.0.1:7890": true})
	status := CurrentStatus()
	if status.Source != "system" || status.Active != "http://127.0.0.1:7890" || status.System != "http://127.0.0.1:7890" {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestResolveSkipsDeadSystemProxyForEnvironment(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", map[string]bool{"http://127.0.0.1:1080": true})
	t.Setenv("HTTPS_PROXY", "127.0.0.1:1080")
	SetCustom(nil)
	status := CurrentStatus()
	if status.Source != "environment" || status.Active != "http://127.0.0.1:1080" {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestResolveDirectWhenNothingAnswers(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", nil)
	status := CurrentStatus()
	if status.Active != "" || status.Source != "system" || status.Configured != "http://127.0.0.1:7890" {
		t.Fatalf("a dead proxy should be reported but not used: %#v", status)
	}
}

func TestCustomProxyReplacesSystemAndEnvironment(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", map[string]bool{"http://127.0.0.1:7890": true, "http://127.0.0.1:1080": true})
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1080")
	SetCustom(&Custom{Scheme: "socks5", Host: "127.0.0.1", Port: 7891})
	status := CurrentStatus()
	if status.Source != "custom" || status.Configured != "socks5://127.0.0.1:7891" || status.Active != "" {
		t.Fatalf("a dead custom proxy must not fall back to the system one: %#v", status)
	}
	listeningFunc = func(string) bool { return true }
	SetCustom(&Custom{Scheme: "socks5", Host: "127.0.0.1", Port: 7891})
	if got := Resolve(); got != "socks5://127.0.0.1:7891" {
		t.Fatalf("Resolve() = %q", got)
	}
}

func TestResolveIsCachedBriefly(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", map[string]bool{"http://127.0.0.1:7890": true})
	clock := time.Unix(1000, 0)
	previousNow := now
	now = func() time.Time { return clock }
	t.Cleanup(func() { now = previousNow })
	probes := 0
	listeningFunc = func(string) bool { probes++; return true }
	SetCustom(nil)
	Resolve()
	Resolve()
	if probes != 1 {
		t.Fatalf("a burst of requests probed %d times", probes)
	}
	clock = clock.Add(cacheLifetime)
	Resolve()
	if probes != 2 {
		t.Fatalf("an expired result was reused (%d probes)", probes)
	}
}

func TestProxyFuncBypassesLoopback(t *testing.T) {
	stub(t, "http://127.0.0.1:7890", map[string]bool{"http://127.0.0.1:7890": true})
	for _, target := range []string{"http://127.0.0.1:8787/v1", "http://localhost:3000/", "http://[::1]:9/"} {
		parsed, _ := url.Parse(target)
		proxy, err := ProxyFunc(&http.Request{URL: parsed})
		if err != nil || proxy != nil {
			t.Fatalf("%s went through %v (%v)", target, proxy, err)
		}
	}
	parsed, _ := url.Parse("https://api.example.com/v1")
	proxy, err := ProxyFunc(&http.Request{URL: parsed})
	if err != nil || proxy == nil || proxy.String() != "http://127.0.0.1:7890" {
		t.Fatalf("remote request proxy = %v (%v)", proxy, err)
	}
}

func TestCustomNormalize(t *testing.T) {
	got, err := Custom{Scheme: " HTTP ", Host: "", Port: 7890}.Normalize()
	if err != nil || got != (Custom{Scheme: "http", Host: "127.0.0.1", Port: 7890}) {
		t.Fatalf("Normalize() = %#v, %v", got, err)
	}
	if got, err := (Custom{Scheme: "socks5", Host: "[::1]", Port: 1080}).Normalize(); err != nil || got.URL() != "socks5://[::1]:1080" {
		t.Fatalf("IPv6 host: %#v %v", got, err)
	}
	for _, bad := range []Custom{
		{Scheme: "ftp", Host: "127.0.0.1", Port: 21},
		{Scheme: "http", Host: "127.0.0.1", Port: 0},
		{Scheme: "http", Host: "127.0.0.1", Port: 70000},
		{Scheme: "http", Host: "http://127.0.0.1", Port: 7890},
		{Scheme: "http", Host: "user@host", Port: 7890},
	} {
		if _, err := bad.Normalize(); err == nil {
			t.Fatalf("accepted %#v", bad)
		}
	}
}

func TestChildEnvironment(t *testing.T) {
	has := func(values []string, want string) bool {
		for _, value := range values {
			if value == want {
				return true
			}
		}
		return false
	}
	direct := ChildEnvironment("")
	if !has(direct, "NO_PROXY=*") || !has(direct, "HTTPS_PROXY=") || !has(direct, "https_proxy=") {
		t.Fatalf("direct environment: %#v", direct)
	}
	proxied := ChildEnvironment("http://127.0.0.1:7890")
	if !has(proxied, "HTTPS_PROXY=http://127.0.0.1:7890") || !has(proxied, "https_proxy=http://127.0.0.1:7890") || has(proxied, "NO_PROXY=*") {
		t.Fatalf("proxied environment: %#v", proxied)
	}
}

func TestIsListeningSkipsDeadLocalProxy(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if !isListening("http://" + address) {
		t.Fatal("a listening local proxy was rejected")
	}
	listener.Close()
	if isListening("http://" + address) {
		t.Fatal("a closed local proxy was accepted")
	}
	if !isListening("http://proxy.example.com:8080") {
		t.Fatal("a remote proxy is assumed reachable")
	}
}

func TestSystemProxyParsing(t *testing.T) {
	cases := map[string]string{
		"":                      "",
		"127.0.0.1:7890":        "http://127.0.0.1:7890",
		"http://127.0.0.1:7890": "http://127.0.0.1:7890",
		"http=127.0.0.1:8080;https=127.0.0.1:8443": "http://127.0.0.1:8443",
		"http=127.0.0.1:8080;socks=127.0.0.1:1080": "http://127.0.0.1:8080",
		"socks=127.0.0.1:1080":                     "socks5://127.0.0.1:1080",
		"ftp=127.0.0.1:21":                         "",
	}
	for server, want := range cases {
		if got := parseSystemProxy(server); got != want {
			t.Fatalf("parseSystemProxy(%q) = %q, want %q", server, got, want)
		}
	}
}

func TestLabel(t *testing.T) {
	cases := map[string]string{
		"":                                "",
		"http://127.0.0.1:7890":           "本地代理 127.0.0.1:7890",
		"socks5://proxy.example.com:1080": "代理 proxy.example.com:1080",
	}
	for proxy, want := range cases {
		if got := Label(proxy); got != want {
			t.Errorf("Label(%q) = %q, want %q", proxy, got, want)
		}
	}
}
