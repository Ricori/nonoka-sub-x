package sidecar

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestParseHandshake(t *testing.T) {
	valid := []byte(`{"schema":1,"host":"127.0.0.1","port":43123,"token":"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"}` + "\n")
	value, err := parseHandshake(valid)
	if err != nil {
		t.Fatalf("parse valid handshake: %v", err)
	}
	if value.Port != 43123 {
		t.Fatalf("port = %d", value.Port)
	}

	for name, input := range map[string]string{
		"schema": `{"schema":2,"host":"127.0.0.1","port":1,"token":"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"}`,
		"host":   `{"schema":1,"host":"0.0.0.0","port":1,"token":"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"}`,
		"port":   `{"schema":1,"host":"127.0.0.1","port":0,"token":"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"}`,
		"token":  `{"schema":1,"host":"127.0.0.1","port":1,"token":"short"}`,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseHandshake([]byte(input)); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestValidateEndpoint(t *testing.T) {
	for _, endpoint := range []string{"/v1/capabilities", "/v1/tasks/abc/events?after=3"} {
		if err := validateEndpoint(endpoint); err != nil {
			t.Fatalf("valid endpoint %q: %v", endpoint, err)
		}
	}
	for _, endpoint := range []string{"https://example.com/v1/tasks", "//example.com/v1/tasks", "/v1/../secret", "/health"} {
		if err := validateEndpoint(endpoint); err == nil {
			t.Fatalf("expected endpoint %q to fail", endpoint)
		}
	}
}

func TestDoJSONKeepsTokenInBackendAndPreservesErrors(t *testing.T) {
	const token = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer "+token {
			http.Error(response, "missing token", http.StatusUnauthorized)
			return
		}
		if request.URL.Path == "/v1/fail" {
			response.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(response).Encode(map[string]any{
				"error": map[string]string{"code": "invalid_state", "message": "cannot resume"},
			})
			return
		}
		_ = json.NewEncoder(response).Encode(map[string]any{"provider": "local"})
	}))
	defer server.Close()
	if !loopbackURL(server.URL) {
		t.Fatalf("httptest URL is not loopback: %s", server.URL)
	}

	manager := New(Config{})
	manager.process = &processState{baseURL: server.URL, token: token}
	var capabilities map[string]any
	if err := manager.DoJSON(context.Background(), http.MethodGet, "/v1/capabilities", nil, &capabilities); err != nil {
		t.Fatalf("capabilities: %v", err)
	}
	if capabilities["provider"] != "local" {
		t.Fatalf("provider = %#v", capabilities["provider"])
	}
	err := manager.DoJSON(context.Background(), http.MethodPost, "/v1/fail", map[string]any{}, nil)
	var httpErr *HTTPError
	if !errors.As(err, &httpErr) || httpErr.Code != "invalid_state" || httpErr.StatusCode != http.StatusConflict {
		t.Fatalf("error = %#v", err)
	}
	if strings.Contains(manager.Snapshot().BaseURL, token) {
		t.Fatal("snapshot exposed the session token")
	}
}

// A call that legitimately runs for minutes -- an LLM completion -- has to be
// able to say so. A Timeout on the shared client capped every call at 30s no
// matter what its context said, which is what an LLM call hit first: the
// caller's own deadline never got a chance to apply.
func TestDoJSONLetsTheCallerOwnTheDeadline(t *testing.T) {
	const token = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		select {
		case <-release:
		case <-request.Context().Done():
			return
		}
		_ = json.NewEncoder(response).Encode(map[string]any{"provider": "local"})
	}))
	defer server.Close()
	defer close(release)

	manager := New(Config{})
	manager.process = &processState{baseURL: server.URL, token: token}
	if manager.client.Timeout != 0 {
		t.Fatal("a client-wide Timeout caps every call regardless of its context")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	err := manager.DoJSON(ctx, http.MethodPost, "/v1/llm/complete", map[string]any{}, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a short caller deadline should end the call: %v", err)
	}

	// And a caller that brings none still gets a bound rather than hanging.
	if defaultCallTimeout <= 0 {
		t.Fatal("a call without a deadline must still be bounded")
	}
}

func TestManagerStartsAndStopsChildProcess(t *testing.T) {
	manager := New(Config{
		Executable: os.Args[0],
		Args:       []string{"-test.run=TestSidecarHelperProcess"},
		Environment: []string{
			"NONOKA_SIDECAR_HELPER=1",
		},
		StartupTimeout: 5 * time.Second,
	})

	started, err := manager.Start(context.Background())
	if err != nil {
		t.Fatalf("start helper sidecar: %v", err)
	}
	if !started.Running || started.PID <= 0 || started.BaseURL != "http://127.0.0.1:43123" {
		t.Fatalf("unexpected running snapshot: %#v", started)
	}

	stopContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := manager.Stop(stopContext); err != nil {
		t.Fatalf("stop helper sidecar: %v", err)
	}
	if stopped := manager.Snapshot(); stopped.Running {
		t.Fatalf("sidecar is still running: %#v", stopped)
	}
}

func TestSidecarHelperProcess(t *testing.T) {
	if os.Getenv("NONOKA_SIDECAR_HELPER") != "1" {
		return
	}
	fmt.Println(`{"schema":1,"host":"127.0.0.1","port":43123,"token":"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNO"}`)
	select {}
}

func TestPythonConfigCarriesTheInstallDirectoryOnlyWhenRelocated(t *testing.T) {
	root := t.TempDir()
	script := filepath.Join(root, "run_local_sidecar.py")
	vendor := filepath.Join(root, "finesub")
	data := filepath.Join(root, "Nonoka X")

	config, err := PythonConfig("python", script, data, vendor, "")
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	if slices.Contains(config.Args, "--install-dir") {
		t.Fatalf("default layout passed --install-dir: %v", config.Args)
	}

	install := filepath.Join(t.TempDir(), "Nonoka X", "finesub")
	config, err = PythonConfig("python", script, data, vendor, install)
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	// The sidecar script and vendor directory keep their positions: SidecarConfig
	// validates them by index.
	if config.Args[0] != script || config.Args[4] != vendor {
		t.Fatalf("argument order changed: %v", config.Args)
	}
	index := slices.Index(config.Args, "--install-dir")
	if index < 0 || config.Args[index+1] != install {
		t.Fatalf("install directory missing from %v", config.Args)
	}
}
