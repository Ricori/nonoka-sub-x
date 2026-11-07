package cloud

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptrace"
	"sync"
	"time"
)

// backupHosts maps a backend host to the reverse proxy that serves the same
// backend over a different route. cloud-jp is a server in Tokyo whose routes
// from mainland China are far shorter than Cloudflare's: China Telecom
// reaches the Worker via Amsterdam and China Unicom via Los Angeles, and
// either can drop out at peak hours.
//
// Only API calls and subtitle downloads go through it. Audio uploads go
// straight to the presigned R2 URL on their own client, so the proxy never
// pays egress for media.
var backupHosts = map[string]string{
	"cloud.nonoka.online": "cloud-jp.nonoka.online",
}

const (
	// How long a read may wait for response headers on the primary route
	// before the backup is tried. Well past what a healthy Worker takes;
	// short enough that the backup still fits inside the 30-second client
	// timeout.
	failoverHeaderBudget = 12 * time.Second
	// How long requests keep going to the backup once it answered where the
	// primary did not, before the primary is given another chance.
	failoverStickiness = 10 * time.Minute
)

// failoverTransport sends requests for a host in backupHosts to the backup
// when the primary route fails, and keeps using the backup for a while once
// it has worked.
//
// A request is only ever sent a second time when that cannot apply it twice:
// a GET or HEAD, or any request the primary never finished receiving -- the
// connection or the TLS handshake failed, so no body reached the backend.
type failoverTransport struct {
	base         http.RoundTripper
	backups      map[string]string
	headerBudget time.Duration
	stickiness   time.Duration
	now          func() time.Time

	mu         sync.Mutex
	usingUntil map[string]time.Time
}

func newFailoverTransport(base http.RoundTripper) *failoverTransport {
	return &failoverTransport{
		base:         base,
		backups:      backupHosts,
		headerBudget: failoverHeaderBudget,
		stickiness:   failoverStickiness,
		now:          time.Now,
		usingUntil:   map[string]time.Time{},
	}
}

func (t *failoverTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	primary := request.URL.Host
	backup, ok := t.backups[primary]
	if !ok || request.URL.Scheme != "https" {
		return t.base.RoundTrip(request)
	}
	first, second := primary, backup
	sticky := t.preferBackup(primary)
	if sticky {
		first, second = backup, primary
	}

	response, wrote, err := t.attempt(request, first)
	if err == nil || request.Context().Err() != nil || !t.replayable(request, wrote) {
		return response, err
	}
	retried, rewound := rewind(request)
	if !rewound {
		return nil, err
	}
	response, _, secondErr := t.attempt(retried, second)
	if secondErr != nil {
		return nil, err
	}
	t.markWorking(primary, second == backup)
	return response, nil
}

// attempt sends request to host. A read on the primary route gets a header
// budget so the backup is still worth trying; the budget is lifted as soon as
// headers arrive, so it never cuts a body that is still flowing.
func (t *failoverTransport) attempt(request *http.Request, host string) (*http.Response, bool, error) {
	ctx, cancel := context.WithCancel(request.Context())
	wrote := false
	var wroteMu sync.Mutex
	trace := &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) {
		wroteMu.Lock()
		wrote = true
		wroteMu.Unlock()
	}}
	ctx = httptrace.WithClientTrace(ctx, trace)
	outgoing := request.Clone(ctx)
	outgoing.URL.Host = host
	outgoing.Host = ""

	var timer *time.Timer
	if _, onPrimary := t.backups[host]; onPrimary && idempotent(request.Method) && t.headerBudget > 0 {
		timer = time.AfterFunc(t.headerBudget, cancel)
	}
	response, err := t.base.RoundTrip(outgoing)
	if timer != nil {
		timer.Stop()
	}
	wroteMu.Lock()
	sent := wrote
	wroteMu.Unlock()
	if err != nil {
		cancel()
		return nil, sent, err
	}
	response.Body = &cancelOnClose{ReadCloser: response.Body, cancel: cancel}
	return response, sent, nil
}

func (t *failoverTransport) replayable(request *http.Request, wrote bool) bool {
	return idempotent(request.Method) || !wrote
}

func idempotent(method string) bool {
	return method == http.MethodGet || method == http.MethodHead
}

// rewind returns a copy of request that can be sent again, with a fresh body
// when it has one.
func rewind(request *http.Request) (*http.Request, bool) {
	retried := request.Clone(request.Context())
	if request.Body == nil || request.Body == http.NoBody {
		return retried, true
	}
	if request.GetBody == nil {
		return nil, false
	}
	body, err := request.GetBody()
	if err != nil {
		return nil, false
	}
	retried.Body = body
	return retried, true
}

func (t *failoverTransport) preferBackup(primary string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	until, ok := t.usingUntil[primary]
	if !ok {
		return false
	}
	if t.now().Before(until) {
		return true
	}
	delete(t.usingUntil, primary)
	log.Printf("cloud: trying the primary route to %s again", primary)
	return false
}

// markWorking records which route just answered where the other did not.
func (t *failoverTransport) markWorking(primary string, backup bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if backup {
		if _, already := t.usingUntil[primary]; !already {
			log.Printf("cloud: %s is unreachable, using the backup route %s", primary, t.backups[primary])
		}
		t.usingUntil[primary] = t.now().Add(t.stickiness)
		return
	}
	delete(t.usingUntil, primary)
	log.Printf("cloud: the backup route failed but %s answered, back on the primary route", primary)
}

// cancelOnClose releases an attempt's context once its body is done with.
type cancelOnClose struct {
	io.ReadCloser
	cancel context.CancelFunc
	once   sync.Once
}

func (c *cancelOnClose) Close() error {
	err := c.ReadCloser.Close()
	c.once.Do(c.cancel)
	return err
}
