package cloud

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type routeFixture struct {
	transport *failoverTransport
	client    *http.Client
	primary   string // base URL requests are addressed to
	backup    *httptest.Server
	primaryN  *atomic.Int32
	backupN   *atomic.Int32
	clock     *time.Time
}

// newRouteFixture serves the primary with primaryHandler (nil for a primary
// that refuses connections) and the backup with an echo of method and body.
func newRouteFixture(t *testing.T, primaryHandler http.HandlerFunc) *routeFixture {
	t.Helper()
	var primaryN, backupN atomic.Int32
	backup := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		backupN.Add(1)
		body, _ := io.ReadAll(request.Body)
		_, _ = io.WriteString(writer, "backup "+request.Method+" "+string(body))
	}))
	t.Cleanup(backup.Close)
	primary := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		primaryN.Add(1)
		if primaryHandler != nil {
			primaryHandler(writer, request)
			return
		}
	}))
	primaryURL := primary.URL
	if primaryHandler == nil {
		primary.Close()
	} else {
		t.Cleanup(primary.Close)
	}
	clock := time.Unix(1_700_000_000, 0)
	transport := newFailoverTransport(backup.Client().Transport)
	transport.backups = map[string]string{host(t, primaryURL): host(t, backup.URL)}
	transport.headerBudget = 100 * time.Millisecond
	transport.now = func() time.Time { return clock }
	return &routeFixture{
		transport: transport,
		client:    &http.Client{Transport: transport, Timeout: 5 * time.Second},
		primary:   primaryURL,
		backup:    backup,
		primaryN:  &primaryN,
		backupN:   &backupN,
		clock:     &clock,
	}
}

func host(t *testing.T, raw string) string {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Host
}

func (f *routeFixture) send(t *testing.T, method, body string) (string, error) {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	request, err := http.NewRequest(method, f.primary+"/v1/library", reader)
	if err != nil {
		t.Fatal(err)
	}
	response, err := f.client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(response.Body)
	return string(payload), err
}

func TestFailoverUsesTheBackupWhenThePrimaryRefusesAndStaysOnIt(t *testing.T) {
	fixture := newRouteFixture(t, nil)
	// A POST the primary could never have received is safe to send on, body
	// and all.
	answer, err := fixture.send(t, http.MethodPost, `{"a":1}`)
	if err != nil || answer != `backup POST {"a":1}` {
		t.Fatalf("answer = %q, err = %v", answer, err)
	}
	answer, err = fixture.send(t, http.MethodGet, "")
	if err != nil || answer != "backup GET " {
		t.Fatalf("answer = %q, err = %v", answer, err)
	}
	if !fixture.transport.preferBackup(fixture.transport.primaryHost(t)) {
		t.Fatal("the backup is not preferred after it answered for the primary")
	}
}

func (t *failoverTransport) primaryHost(tb testing.TB) string {
	tb.Helper()
	for primary := range t.backups {
		return primary
	}
	tb.Fatal("no primary")
	return ""
}

func TestFailoverCutsAStalledReadOverToTheBackup(t *testing.T) {
	release := make(chan struct{})
	fixture := newRouteFixture(t, func(http.ResponseWriter, *http.Request) {
		<-release
	})
	defer close(release)
	started := time.Now()
	answer, err := fixture.send(t, http.MethodGet, "")
	if err != nil || answer != "backup GET " {
		t.Fatalf("answer = %q, err = %v", answer, err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("the failover took %v, past the header budget", elapsed)
	}
}

func TestFailoverNeverResendsAWriteThePrimaryReceived(t *testing.T) {
	fixture := newRouteFixture(t, func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.ReadAll(request.Body)
		connection, _, err := writer.(http.Hijacker).Hijack()
		if err == nil {
			_ = connection.Close()
		}
	})
	if _, err := fixture.send(t, http.MethodPost, `{"charge":1}`); err == nil {
		t.Fatal("a POST whose connection broke after delivery reported success")
	}
	if fixture.backupN.Load() != 0 {
		t.Fatal("a POST the primary had received was sent again to the backup")
	}
}

func TestFailoverDoesNotTouchAnAnswer(t *testing.T) {
	fixture := newRouteFixture(t, func(writer http.ResponseWriter, _ *http.Request) {
		http.Error(writer, "gone", http.StatusNotFound)
	})
	answer, err := fixture.send(t, http.MethodGet, "")
	if err != nil || !strings.Contains(answer, "gone") || fixture.backupN.Load() != 0 {
		t.Fatalf("answer = %q, err = %v, backup hits = %d", answer, err, fixture.backupN.Load())
	}
}

func TestFailoverReturnsToThePrimaryOnceTheStickinessLapses(t *testing.T) {
	healthy := atomic.Bool{}
	fixture := newRouteFixture(t, func(writer http.ResponseWriter, request *http.Request) {
		if !healthy.Load() {
			hijacked, _, err := writer.(http.Hijacker).Hijack()
			if err == nil {
				_ = hijacked.Close()
			}
			return
		}
		_, _ = io.WriteString(writer, "primary")
	})
	if answer, err := fixture.send(t, http.MethodGet, ""); err != nil || answer != "backup GET " {
		t.Fatalf("answer = %q, err = %v", answer, err)
	}
	healthy.Store(true)
	if answer, _ := fixture.send(t, http.MethodGet, ""); answer != "backup GET " {
		t.Fatalf("left the backup before the stickiness lapsed: %q", answer)
	}
	*fixture.clock = fixture.clock.Add(failoverStickiness + time.Second)
	if answer, err := fixture.send(t, http.MethodGet, ""); err != nil || answer != "primary" {
		t.Fatalf("answer = %q, err = %v", answer, err)
	}
}

func TestFailoverLeavesOtherHostsAlone(t *testing.T) {
	var hits atomic.Int32
	other := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hits.Add(1) }))
	defer other.Close()
	transport := newFailoverTransport(other.Client().Transport)
	response, err := (&http.Client{Transport: transport}).Get(other.URL)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if hits.Load() != 1 {
		t.Fatalf("hits = %d", hits.Load())
	}
}
