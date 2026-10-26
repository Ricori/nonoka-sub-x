package selfupdate

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync"
	"time"
)

const (
	connectTimeout        = 15 * time.Second
	handshakeTimeout      = 15 * time.Second
	responseHeaderTimeout = 30 * time.Second
	stallTimeout          = 60 * time.Second
	manifestTimeout       = 30 * time.Second
)

// errDownloadStalled is reported when a response body delivers no bytes for
// stallTimeout.
var errDownloadStalled = errors.New("download stalled")

// newHTTPClient builds the client the update provider uses for both the
// manifest and the installer. The provider's default client caps the whole
// request at 30 seconds, body included, which a slow link (mainland China to
// the Cloudflare edge) can never beat for a 30 MB installer, and every retry
// restarts from zero. This one bounds only connecting, the TLS handshake and
// the wait for response headers, then lets the body take as long as it needs
// while bytes keep arriving.
func newHTTPClient(stall time.Duration) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Timeout: connectTimeout, KeepAlive: 30 * time.Second}).DialContext
	transport.TLSHandshakeTimeout = handshakeTimeout
	transport.ResponseHeaderTimeout = responseHeaderTimeout
	return &http.Client{Transport: &stallTransport{base: transport, stall: stall}}
}

// stallTransport cancels a request whose response body goes quiet for longer
// than stall.
type stallTransport struct {
	base  http.RoundTripper
	stall time.Duration
}

func (t *stallTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	ctx, cancel := context.WithCancelCause(req.Context())
	resp, err := t.base.RoundTrip(req.WithContext(ctx))
	if err != nil {
		cancel(nil)
		return nil, err
	}
	body := &stallBody{body: resp.Body, ctx: ctx, cancel: cancel, stall: t.stall}
	body.timer = time.AfterFunc(t.stall, func() { cancel(errDownloadStalled) })
	resp.Body = body
	return resp, nil
}

type stallBody struct {
	body   io.ReadCloser
	ctx    context.Context
	cancel context.CancelCauseFunc
	stall  time.Duration
	timer  *time.Timer
	once   sync.Once
}

func (b *stallBody) Read(p []byte) (int, error) {
	n, err := b.body.Read(p)
	if n > 0 {
		b.timer.Reset(b.stall)
	}
	if err != nil && err != io.EOF && errors.Is(context.Cause(b.ctx), errDownloadStalled) {
		return n, fmt.Errorf("%w: no data for %s", errDownloadStalled, b.stall)
	}
	return n, err
}

func (b *stallBody) Close() error {
	b.once.Do(func() {
		b.timer.Stop()
		b.cancel(nil)
	})
	return b.body.Close()
}
