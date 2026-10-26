package selfupdate

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHTTPClientLetsSlowSteadyDownloadFinish(t *testing.T) {
	// Twelve chunks 25ms apart run well past the 100ms stall window in total,
	// but no single gap reaches it.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		flusher := w.(http.Flusher)
		for range 12 {
			_, _ = w.Write([]byte("chunk"))
			flusher.Flush()
			time.Sleep(25 * time.Millisecond)
		}
	}))
	defer server.Close()

	resp, err := newHTTPClient(100 * time.Millisecond).Get(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("slow but steady download failed: %v", err)
	}
	if len(body) != 12*len("chunk") {
		t.Fatalf("read %d bytes", len(body))
	}
}

func TestHTTPClientAbortsStalledDownload(t *testing.T) {
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("first"))
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer server.Close()
	defer close(release)

	resp, err := newHTTPClient(100 * time.Millisecond).Get(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	started := time.Now()
	_, err = io.ReadAll(resp.Body)
	if !errors.Is(err, errDownloadStalled) {
		t.Fatalf("err = %v, want errDownloadStalled", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("stall took %s to detect", elapsed)
	}
}
