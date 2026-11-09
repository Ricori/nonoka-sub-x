package app

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/netproxy"
)

// bootstrapAttempt is one rung of the ladder every launcher download climbs. It
// follows the same rule as the sidecar's `nonoka_x.download_route`:
//
//  1. through the proxy in effect (`netproxy`), if any, from the official source;
//  2. directly from the official source;
//  3. directly from a mirror (gh-proxy.com for GitHub files, TUNA for PyPI);
//  4. for GitHub files, directly from our own copy on R2;
//  5. an error telling the user to turn a proxy on.
//
// A rung that works but crawls under bootstrapMinBytesPerSecond is abandoned
// too. The last rung has nowhere to move to, so it is left to finish, and one
// abandoned for speed is tried again unwatched if the others fail outright.
type bootstrapAttempt struct {
	// Proxy is the proxy URL this attempt goes through, or "" for direct.
	Proxy string
	// Mirror fetches from the mirror instead of the official source.
	Mirror bool
	// Backup fetches from our own copy on R2. Only GitHub files have one.
	Backup bool
}

const (
	bootstrapUnreachableMessage = "连不上下载源，请开启代理后重试"
	bootstrapMinBytesPerSecond  = 512 * 1024
)

// Variables so a test need not wait them out.
var (
	// bootstrapSlowWindow is how long a transfer has to crawl to be abandoned,
	// so a passing dip does not throw away a good route.
	bootstrapSlowWindow = 30 * time.Second
	// bootstrapProbeWindow is how long a sample gets to show the floor.
	bootstrapProbeWindow = 8 * time.Second
)

var errBootstrapTooSlow = errors.New("下载速度过慢")

func resolveBootstrapAttempts() []bootstrapAttempt {
	return bootstrapAttempts(netproxy.Resolve())
}

func bootstrapAttempts(proxy string) []bootstrapAttempt {
	var attempts []bootstrapAttempt
	if proxy != "" {
		attempts = append(attempts, bootstrapAttempt{Proxy: proxy})
	}
	return append(attempts, bootstrapAttempt{}, bootstrapAttempt{Mirror: true}, bootstrapAttempt{Backup: true})
}

// bootstrapRung is one try at a route. Only a rung with another after it is
// watched for speed.
type bootstrapRung[T any] struct {
	item    T
	watched bool
}

func bootstrapRungs[T any](items []T) []bootstrapRung[T] {
	rungs := make([]bootstrapRung[T], len(items))
	for index, item := range items {
		rungs[index] = bootstrapRung[T]{item: item, watched: index < len(items)-1}
	}
	return rungs
}

// speedWatch tells a transfer that crawled for a whole window from one that
// dipped. Its clock starts at the first byte: setup is not transfer.
type speedWatch struct {
	started time.Time
	bytes   int64
	now     func() time.Time
}

func (w *speedWatch) add(count int) error {
	now := w.now()
	if count > 0 && w.started.IsZero() {
		w.started = now
	}
	if w.started.IsZero() {
		return nil
	}
	w.bytes += int64(count)
	elapsed := now.Sub(w.started)
	if elapsed < bootstrapSlowWindow {
		return nil
	}
	if float64(w.bytes)/elapsed.Seconds() < bootstrapMinBytesPerSecond {
		return errBootstrapTooSlow
	}
	w.started, w.bytes = now, 0
	return nil
}

// bootstrapRouteTooSlow samples a route before a step that cannot report its own
// speed: uv prints no byte counts. Only a measured crawl or a timeout counts
// against the route, since any other failure is the step's own to report.
func bootstrapRouteTooSlow(rawURL string, attempt bootstrapAttempt) bool {
	wanted := int64(bootstrapMinBytesPerSecond * bootstrapProbeWindow.Seconds())
	ctx, cancel := context.WithTimeout(context.Background(), 2*bootstrapProbeWindow)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return false
	}
	request.Header.Set("Range", fmt.Sprintf("bytes=0-%d", wanted-1))
	response, err := (&http.Client{Transport: attempt.transport()}).Do(request)
	if err != nil {
		return errors.Is(err, context.DeadlineExceeded)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusPartialContent {
		return false
	}
	buffer := make([]byte, 64<<10)
	var (
		received int64
		started  time.Time
	)
	for {
		count, readErr := response.Body.Read(buffer)
		if count > 0 && started.IsZero() {
			started = time.Now()
		}
		received += int64(count)
		if received >= wanted || errors.Is(readErr, io.EOF) {
			return false
		}
		if readErr != nil {
			return errors.Is(readErr, context.DeadlineExceeded)
		}
		if !started.IsZero() && time.Since(started) >= bootstrapProbeWindow {
			return true
		}
	}
}

// withoutMirror is the ladder for a step whose mirror the user already chose
// through uv's own variables: the copy rungs would only repeat the direct one.
func withoutMirror(attempts []bootstrapAttempt) []bootstrapAttempt {
	official := make([]bootstrapAttempt, 0, len(attempts))
	for _, attempt := range attempts {
		if !attempt.Mirror && !attempt.Backup {
			official = append(official, attempt)
		}
	}
	return official
}

// withoutBackup is the ladder for a step that fetches nothing from GitHub.
func withoutBackup(attempts []bootstrapAttempt) []bootstrapAttempt {
	kept := make([]bootstrapAttempt, 0, len(attempts))
	for _, attempt := range attempts {
		if !attempt.Backup {
			kept = append(kept, attempt)
		}
	}
	return kept
}

// label names the rung in the progress message and in a failure.
func (a bootstrapAttempt) label() string {
	switch {
	case a.Backup:
		return "Nonoka 备用源"
	case a.Mirror:
		return "镜像"
	case a.Proxy != "":
		return proxyLabel(a.Proxy)
	}
	return "直连官方源"
}

func directTransport() *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return transport
}

// transport pins every request to this attempt's proxy. Go's default transport
// only reads the environment variables, so without this a system-proxy client
// such as Clash is bypassed entirely.
func (a bootstrapAttempt) transport() *http.Transport {
	transport := directTransport()
	if a.Proxy != "" {
		if proxyURL, err := url.Parse(a.Proxy); err == nil {
			transport.Proxy = http.ProxyURL(proxyURL)
		}
	}
	return transport
}

// environment hands the same route to uv, which reads only the environment.
// Direct is spelled out too, so uv cannot pick up a proxy the attempt skipped.
func (a bootstrapAttempt) environment() []string {
	return netproxy.ChildEnvironment(a.Proxy)
}

// Windows words these in the system's language, so the OS error numbers carry
// as much of the match as the English does.
var localBootstrapFailures = []string{
	"no space left", "not enough space", "disk full", "permission denied", "access is denied", "out of memory",
	"(os error 5)", "(os error 28)", "(os error 112)",
}

func isLocalBootstrapFailure(output []byte) bool {
	text := strings.ToLower(string(output))
	for _, marker := range localBootstrapFailures {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}

// climbBootstrap runs one uv step on each rung until it works. A failure this
// machine caused stops the climb, since no other source can fix it and telling
// the user to turn a proxy on would send them the wrong way. announce, which may
// be nil, hears each rung and whether an earlier one fell through. step hears
// the rung and whether to give up on it for speed.
func climbBootstrap(attempts []bootstrapAttempt, failure string, announce func(bootstrapAttempt, bool), step func(bootstrapAttempt, bool) ([]byte, error)) error {
	var (
		output []byte
		err    error
	)
	rungs := bootstrapRungs(attempts)
	for index := 0; index < len(rungs); index++ {
		current := rungs[index]
		if announce != nil {
			announce(current.item, index > 0)
		}
		output, err = step(current.item, current.watched)
		if err == nil {
			return nil
		}
		if errors.Is(err, errBootstrapTooSlow) {
			rungs = append(rungs, bootstrapRung[bootstrapAttempt]{item: current.item})
			continue
		}
		if isLocalBootstrapFailure(output) {
			return fmt.Errorf("%s：%w%s", failure, err, commandDetail(output))
		}
	}
	return fmt.Errorf("%s，%s：%w%s", failure, bootstrapUnreachableMessage, err, commandDetail(output))
}

// proxyLabel names the proxy the way the runtime page shows it.
func proxyLabel(proxy string) string { return netproxy.Label(proxy) }
