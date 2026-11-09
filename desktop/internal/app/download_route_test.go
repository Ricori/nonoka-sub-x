package app

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"
)

const testProxy = "http://127.0.0.1:7890"

func TestLadderClimbsProxyThenDirectThenMirror(t *testing.T) {
	want := []bootstrapAttempt{{Proxy: testProxy}, {}, {Mirror: true}, {Backup: true}}
	if got := bootstrapAttempts(testProxy); !slices.Equal(got, want) {
		t.Fatalf("with a proxy: %#v", got)
	}
	want = []bootstrapAttempt{{}, {Mirror: true}, {Backup: true}}
	if got := bootstrapAttempts(""); !slices.Equal(got, want) {
		t.Fatalf("without a proxy: %#v", got)
	}
}

func TestOnlyGitHubStepsKeepTheBackupRung(t *testing.T) {
	attempts := bootstrapAttempts(testProxy)
	if got := withoutBackup(attempts); !slices.Equal(got, []bootstrapAttempt{{Proxy: testProxy}, {}, {Mirror: true}}) {
		t.Fatalf("withoutBackup = %#v", got)
	}
	if got := withoutMirror(attempts); !slices.Equal(got, []bootstrapAttempt{{Proxy: testProxy}, {}}) {
		t.Fatalf("a mirror the user chose should drop both copy rungs: %#v", got)
	}
}

func TestLadderIgnoresTheRegionVariable(t *testing.T) {
	t.Setenv("FINESUB_DOWNLOAD_REGION", "cn")
	want := []bootstrapAttempt{{}, {Mirror: true}, {Backup: true}}
	if got := bootstrapAttempts(""); !slices.Equal(got, want) {
		t.Fatalf("the ladder should not depend on the region variable: %#v", got)
	}
}

func TestClimbStopsAtTheFirstRungThatWorks(t *testing.T) {
	var tried []bootstrapAttempt
	err := climbBootstrap([]bootstrapAttempt{{Proxy: testProxy}, {}, {Mirror: true}}, "failed", nil, func(attempt bootstrapAttempt, _ bool) ([]byte, error) {
		tried = append(tried, attempt)
		if !attempt.Mirror && attempt.Proxy == "" {
			return nil, nil
		}
		return []byte("error sending request"), errors.New("exit status 1")
	})
	if err != nil || len(tried) != 2 {
		t.Fatalf("err=%v tried=%#v", err, tried)
	}
}

func TestClimbAsksForAProxyOnceEveryRungFails(t *testing.T) {
	err := climbBootstrap([]bootstrapAttempt{{}, {Mirror: true}}, "failed", nil, func(bootstrapAttempt, bool) ([]byte, error) {
		return []byte("tcp connect error"), errors.New("exit status 1")
	})
	if err == nil || !strings.Contains(err.Error(), bootstrapUnreachableMessage) {
		t.Fatalf("expected the proxy advice, got %v", err)
	}
}

func TestClimbDoesNotBlameTheNetworkForALocalFailure(t *testing.T) {
	calls := 0
	err := climbBootstrap([]bootstrapAttempt{{}, {Mirror: true}}, "failed", nil, func(bootstrapAttempt, bool) ([]byte, error) {
		calls++
		return []byte("磁盘空间不足。 (os error 112)"), errors.New("exit status 1")
	})
	if calls != 1 {
		t.Fatalf("a full disk should stop the climb, tried %d rungs", calls)
	}
	if err == nil || strings.Contains(err.Error(), bootstrapUnreachableMessage) {
		t.Fatalf("a full disk should not ask for a proxy: %v", err)
	}
}

func TestClimbWatchesEveryRungButTheLast(t *testing.T) {
	var watched []bool
	_ = climbBootstrap([]bootstrapAttempt{{Proxy: testProxy}, {}, {Mirror: true}}, "failed", nil, func(_ bootstrapAttempt, isWatched bool) ([]byte, error) {
		watched = append(watched, isWatched)
		return nil, errors.New("exit status 1")
	})
	if !slices.Equal(watched, []bool{true, true, false}) {
		t.Fatalf("watched = %v", watched)
	}
}

func TestClimbFallsBackToASlowRungOnlyWhenTheRestFail(t *testing.T) {
	var tried []bootstrapAttempt
	var watched []bool
	err := climbBootstrap([]bootstrapAttempt{{}, {Mirror: true}}, "failed", nil, func(attempt bootstrapAttempt, isWatched bool) ([]byte, error) {
		tried = append(tried, attempt)
		watched = append(watched, isWatched)
		if isWatched {
			return nil, errBootstrapTooSlow
		}
		if attempt.Mirror {
			return []byte("tcp connect error"), errors.New("exit status 1")
		}
		return nil, nil
	})
	if err != nil {
		t.Fatalf("the slow rung should have been used: %v", err)
	}
	if !slices.Equal(tried, []bootstrapAttempt{{}, {Mirror: true}, {}}) || !slices.Equal(watched, []bool{true, false, false}) {
		t.Fatalf("tried=%#v watched=%v", tried, watched)
	}
}

func TestSpeedWatchJudgesAWholeWindow(t *testing.T) {
	clock := time.Unix(1000, 0)
	watch := speedWatch{now: func() time.Time { return clock }}

	if err := watch.add(0); err != nil {
		t.Fatalf("no bytes yet is not a crawl: %v", err)
	}
	clock = clock.Add(time.Hour)
	if err := watch.add(1); err != nil {
		t.Fatalf("the clock must start at the first byte: %v", err)
	}
	clock = clock.Add(bootstrapSlowWindow - time.Second)
	if err := watch.add(1); err != nil {
		t.Fatalf("a part of a window proves nothing: %v", err)
	}
	clock = clock.Add(time.Second)
	if err := watch.add(1); !errors.Is(err, errBootstrapTooSlow) {
		t.Fatalf("a crawling window should be too slow, got %v", err)
	}
}

func TestSpeedWatchLetsAFastWindowPass(t *testing.T) {
	clock := time.Unix(1000, 0)
	watch := speedWatch{now: func() time.Time { return clock }}
	_ = watch.add(1)
	clock = clock.Add(bootstrapSlowWindow)
	fast := int(bootstrapMinBytesPerSecond * bootstrapSlowWindow.Seconds())
	if err := watch.add(fast); err != nil {
		t.Fatalf("a window at the floor should pass: %v", err)
	}
	if !watch.started.Equal(clock) || watch.bytes != 0 {
		t.Fatalf("a passed window should start the next: %#v", watch)
	}
}

func shrinkBootstrapWindows(t *testing.T, window time.Duration) {
	t.Helper()
	slow, probe := bootstrapSlowWindow, bootstrapProbeWindow
	bootstrapSlowWindow, bootstrapProbeWindow = window, window
	t.Cleanup(func() { bootstrapSlowWindow, bootstrapProbeWindow = slow, probe })
}

func dripServer(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		for range 200 {
			if _, err := writer.Write([]byte("x")); err != nil {
				return
			}
			writer.(http.Flusher).Flush()
			select {
			case <-request.Context().Done():
				return
			case <-time.After(10 * time.Millisecond):
			}
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func TestRouteProbeNoticesACrawl(t *testing.T) {
	shrinkBootstrapWindows(t, 100*time.Millisecond)
	if !bootstrapRouteTooSlow(dripServer(t).URL, bootstrapAttempt{}) {
		t.Fatal("a dripping route should measure too slow")
	}
}

func TestRouteProbePassesAFastRoute(t *testing.T) {
	shrinkBootstrapWindows(t, time.Second)
	body := make([]byte, 1024*1024)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	if bootstrapRouteTooSlow(server.URL, bootstrapAttempt{}) {
		t.Fatal("a fast route should pass")
	}
}

func TestRouteProbeDoesNotBlameAnHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusForbidden)
	}))
	defer server.Close()
	if bootstrapRouteTooSlow(server.URL, bootstrapAttempt{}) {
		t.Fatal("an HTTP error says nothing about speed")
	}
}

func TestAttemptEnvironmentCarriesProxy(t *testing.T) {
	direct := (bootstrapAttempt{}).environment()
	if !contains(direct, "NO_PROXY=*") || !contains(direct, "HTTPS_PROXY=") {
		t.Fatalf("a direct attempt must clear the proxy variables: %#v", direct)
	}
	environment := bootstrapAttempt{Proxy: testProxy}.environment()
	if !contains(environment, "HTTPS_PROXY="+testProxy) || !contains(environment, "ALL_PROXY="+testProxy) {
		t.Fatalf("unexpected proxy environment: %#v", environment)
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
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
