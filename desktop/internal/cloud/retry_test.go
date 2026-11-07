package cloud

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestIsTransientSeparatesTheNetworkFromAnswers(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"gateway", &httpStatusError{Status: http.StatusBadGateway}, true},
		{"cloudflare origin timeout", &httpStatusError{Status: 524}, true},
		{"rate limited", &httpStatusError{Status: http.StatusTooManyRequests}, true},
		{"job gone", &httpStatusError{Status: http.StatusNotFound}, false},
		{"unauthorized", &httpStatusError{Status: http.StatusUnauthorized}, false},
		{"download gateway", fmt.Errorf("wrapped: %w", &downloadStatusError{Status: http.StatusServiceUnavailable}), true},
		{"download missing", &downloadStatusError{Status: http.StatusNotFound}, false},
		{"body cut short", &transientError{errors.New("stream reset")}, true},
		{"client timeout", fmt.Errorf("call cloud backend: %w", context.DeadlineExceeded), true},
		{"truncated", io.ErrUnexpectedEOF, true},
		{"digest", errors.New("stable.json does not match the digest the cloud published"), false},
		{"nil", nil, false},
	}
	for _, item := range cases {
		if got := isTransient(item.err); got != item.want {
			t.Errorf("%s: isTransient = %v, want %v", item.name, got, item.want)
		}
	}
}

// flakyBackend answers /v1/session, and fails an artifact download the first
// `failures` times: odd attempts with a 502, even ones by dropping the
// connection mid-response.
func flakyBackend(t *testing.T, failures int32, final int) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/v1/session" {
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":1,"running":0}`))
			return
		}
		call := calls.Add(1)
		if call <= failures {
			if call%2 == 1 {
				http.Error(writer, "bad gateway", http.StatusBadGateway)
				return
			}
			writer.Header().Set("Content-Length", "100")
			writer.WriteHeader(http.StatusOK)
			_, _ = writer.Write([]byte("partial"))
			connection, _, err := writer.(http.Hijacker).Hijack()
			if err == nil {
				_ = connection.Close()
			}
			return
		}
		if final != http.StatusOK {
			http.Error(writer, "nope", final)
			return
		}
		_, _ = writer.Write([]byte("subtitles"))
	}))
	t.Cleanup(server.Close)
	return server, &calls
}

func loggedInService(t *testing.T, backend string) *Service {
	t.Helper()
	service, err := New(t.TempDir(), fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	service.fetchBackoff = []time.Duration{time.Millisecond, time.Millisecond, time.Millisecond}
	if _, err := service.Login(backend, "login-key"); err != nil {
		t.Fatal(err)
	}
	return service
}

func TestFetchTextRetriesGatewayFailuresAndDroppedConnections(t *testing.T) {
	server, calls := flakyBackend(t, 3, http.StatusOK)
	service := loggedInService(t, server.URL)
	content, err := service.fetchText(context.Background(), "/v1/tasks/job/artifacts/stable_json")
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "subtitles" || calls.Load() != 4 {
		t.Fatalf("content = %q after %d calls", content, calls.Load())
	}
}

func TestFetchTextGivesUpOnceTheBackoffIsSpent(t *testing.T) {
	server, calls := flakyBackend(t, 100, http.StatusOK)
	service := loggedInService(t, server.URL)
	if _, err := service.fetchText(context.Background(), "/v1/tasks/job/artifacts/stable_json"); err == nil {
		t.Fatal("a download that never succeeds reported success")
	}
	if calls.Load() != 4 {
		t.Fatalf("calls = %d, want one attempt plus three retries", calls.Load())
	}
}

func TestFetchTextDoesNotRetryAnAnswer(t *testing.T) {
	server, calls := flakyBackend(t, 0, http.StatusNotFound)
	service := loggedInService(t, server.URL)
	_, err := service.fetchText(context.Background(), "/v1/tasks/job/artifacts/stable_json")
	var status *downloadStatusError
	if !errors.As(err, &status) || status.Status != http.StatusNotFound {
		t.Fatalf("err = %v", err)
	}
	// Not an httpStatusError: the relay reads that 404 as "the job is gone".
	var job *httpStatusError
	if errors.As(err, &job) {
		t.Fatal("a missing artifact file reads as a missing job")
	}
	if calls.Load() != 1 {
		t.Fatalf("calls = %d, want 1", calls.Load())
	}
}

func TestRetryReadStopsWaitingWhenCancelled(t *testing.T) {
	service := &Service{fetchBackoff: []time.Duration{time.Hour}}
	ctx, cancel := context.WithCancel(context.Background())
	attempts := 0
	done := make(chan error, 1)
	go func() {
		done <- service.retryRead(ctx, func() error {
			attempts++
			return &httpStatusError{Status: http.StatusBadGateway}
		})
	}()
	time.Sleep(20 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err == nil || attempts != 1 {
			t.Fatalf("err = %v after %d attempts", err, attempts)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("a cancelled retry kept waiting out its backoff")
	}
}
