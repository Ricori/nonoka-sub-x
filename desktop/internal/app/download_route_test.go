package app

import (
	"net"
	"testing"
)

func TestResolveRoutePrefersProxyOverMirrors(t *testing.T) {
	t.Setenv(regionEnvironment, "")
	probed := false
	mainland := func() bool { probed = true; return true }

	withProxy := resolveRoute("http://127.0.0.1:7890", mainland)
	if withProxy.Mirror || withProxy.Proxy == "" || probed {
		t.Fatalf("a live proxy should mean official sources without a probe: %#v probed=%v", withProxy, probed)
	}
	if direct := resolveRoute("", mainland); !direct.Mirror || !probed {
		t.Fatalf("no proxy on a mainland exit should prefer mirrors: %#v", direct)
	}
	if overseas := resolveRoute("", func() bool { return false }); overseas.Mirror {
		t.Fatalf("no proxy outside the mainland should use official sources: %#v", overseas)
	}
}

func TestResolveRouteHonoursRegionOverride(t *testing.T) {
	t.Setenv(regionEnvironment, "cn")
	if route := resolveRoute("http://127.0.0.1:7890", func() bool { return false }); !route.Mirror {
		t.Fatalf("FINESUB_DOWNLOAD_REGION=cn should force mirrors: %#v", route)
	}
	t.Setenv(regionEnvironment, "global")
	if route := resolveRoute("", func() bool { return true }); route.Mirror {
		t.Fatalf("FINESUB_DOWNLOAD_REGION=global should force official sources: %#v", route)
	}
}

func TestProxyIsListeningSkipsDeadLocalProxy(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if !proxyIsListening("http://" + address) {
		t.Fatal("a listening local proxy was rejected")
	}
	listener.Close()
	if proxyIsListening("http://" + address) {
		t.Fatal("a closed local proxy was accepted")
	}
	if !proxyIsListening("http://proxy.example.com:8080") {
		t.Fatal("a remote proxy is assumed reachable")
	}
}

func TestRouteEnvironmentCarriesProxy(t *testing.T) {
	if environment := (bootstrapRoute{}).environment(); len(environment) != 0 {
		t.Fatalf("a direct route sets no proxy variables: %#v", environment)
	}
	environment := bootstrapRoute{Proxy: "http://127.0.0.1:7890"}.environment()
	if len(environment) != 3 || environment[0] != "HTTPS_PROXY=http://127.0.0.1:7890" {
		t.Fatalf("unexpected proxy environment: %#v", environment)
	}
}

func TestProxyLabel(t *testing.T) {
	cases := map[string]string{
		"":                                "",
		"http://127.0.0.1:7890":           "本地代理 127.0.0.1:7890",
		"socks5://proxy.example.com:1080": "代理 proxy.example.com:1080",
	}
	for proxy, want := range cases {
		if got := proxyLabel(proxy); got != want {
			t.Errorf("proxyLabel(%q) = %q, want %q", proxy, got, want)
		}
	}
}
