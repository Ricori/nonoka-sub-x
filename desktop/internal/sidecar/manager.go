package sidecar

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	defaultStartupTimeout = 20 * time.Second
	// How long one provider call may take when its caller names no deadline of
	// its own. It lives here rather than on the shared http.Client because one
	// number cannot serve both a file read and a model round-trip: a call that
	// legitimately runs for minutes (an LLM completion) must be able to say so
	// through its context without also letting a wedged document read hang the
	// window for that long.
	defaultCallTimeout = 30 * time.Second
	maxResponseBytes   = 16 << 20
	maxDiagnosticBytes = 64 << 10
)

var (
	ErrAlreadyRunning = errors.New("sidecar is already running")
	ErrNotRunning     = errors.New("sidecar is not running")
)

// Config describes the exact child process Wails supervises. Executable and
// Args deliberately stay generic so packaged Python and source-checkout Python
// use the same lifecycle implementation.
type Config struct {
	Executable     string
	Args           []string
	Directory      string
	Environment    []string
	StartupTimeout time.Duration
}

// PythonConfig constructs the development/managed-runtime command expected by
// nonoka_x.sidecar. All paths are resolved before the child starts so a later
// working-directory change cannot redirect its data or engine.
//
// installDir is the FineSub install root. It is passed separately from dataDir
// because it is the one that holds gigabytes — the runtime, the models and the
// download caches — and the settings page can move it to another drive while
// the small state files stay put.
func PythonConfig(python, script, dataDir, vendorDir, installDir string) (Config, error) {
	if strings.TrimSpace(python) == "" {
		return Config{}, errors.New("python executable is required")
	}
	resolvedScript, err := filepath.Abs(script)
	if err != nil {
		return Config{}, fmt.Errorf("resolve sidecar script: %w", err)
	}
	resolvedData, err := filepath.Abs(dataDir)
	if err != nil {
		return Config{}, fmt.Errorf("resolve sidecar data directory: %w", err)
	}
	resolvedVendor, err := filepath.Abs(vendorDir)
	if err != nil {
		return Config{}, fmt.Errorf("resolve FineSub vendor directory: %w", err)
	}
	arguments := []string{
		resolvedScript,
		"--data-dir", resolvedData,
		"--vendor", resolvedVendor,
	}
	// Omitted rather than defaulted here: the sidecar derives the same default
	// from --data-dir, so a relocated install is the only thing that has to be
	// spelled out on the command line.
	if strings.TrimSpace(installDir) != "" {
		resolvedInstall, installErr := filepath.Abs(installDir)
		if installErr != nil {
			return Config{}, fmt.Errorf("resolve FineSub install directory: %w", installErr)
		}
		arguments = append(arguments, "--install-dir", resolvedInstall)
	}
	return Config{
		Executable: python,
		Args:       arguments,
		Directory:  filepath.Dir(resolvedScript),
	}, nil
}

type handshake struct {
	Schema int    `json:"schema"`
	Host   string `json:"host"`
	Port   int    `json:"port"`
	Token  string `json:"token"`
}

// Snapshot is safe to expose to the renderer. The session token is
// intentionally absent; authenticated calls stay inside Manager.DoJSON.
type Snapshot struct {
	Running bool   `json:"running"`
	BaseURL string `json:"baseUrl,omitempty"`
	PID     int    `json:"pid,omitempty"`
	Error   string `json:"error,omitempty"`
}

// HTTPError preserves the sidecar's stable error payload and status.
type HTTPError struct {
	StatusCode int
	Code       string
	Message    string
}

func (e *HTTPError) Error() string {
	if e.Code == "" {
		return fmt.Sprintf("sidecar HTTP %d: %s", e.StatusCode, e.Message)
	}
	return fmt.Sprintf("sidecar %s (HTTP %d): %s", e.Code, e.StatusCode, e.Message)
}

type processState struct {
	cmd     *exec.Cmd
	baseURL string
	token   string
	done    chan struct{}
}

// Manager owns exactly one child and serializes all start/stop transitions.
type Manager struct {
	mu         sync.RWMutex
	transition sync.Mutex
	config     Config
	client     *http.Client
	process    *processState
	lastError  string
}

func New(config Config) *Manager {
	return &Manager{
		config: config,
		// No client-wide Timeout: it would cap every call regardless of the
		// context, which is exactly the bug it looks like it prevents. DoJSON
		// applies defaultCallTimeout to callers that bring no deadline.
		client: &http.Client{},
	}
}

// Configure replaces the command used by the next Start call. It is used by
// the native launcher after it has provisioned a private Python interpreter.
func (m *Manager) Configure(config Config) error {
	m.transition.Lock()
	defer m.transition.Unlock()
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.process != nil {
		return ErrAlreadyRunning
	}
	m.config = config
	m.lastError = ""
	return nil
}

// Executable reports the interpreter the next Start would use. The launcher
// compares it with a freshly resolved configuration to decide whether a running
// sidecar has to be swapped for one on the managed Python.
func (m *Manager) Executable() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.config.Executable
}

func (m *Manager) Start(ctx context.Context) (Snapshot, error) {
	m.transition.Lock()
	defer m.transition.Unlock()

	m.mu.RLock()
	running := m.process != nil
	m.mu.RUnlock()
	if running {
		return m.Snapshot(), ErrAlreadyRunning
	}
	if strings.TrimSpace(m.config.Executable) == "" {
		return Snapshot{}, errors.New("sidecar executable is required")
	}

	command := exec.Command(m.config.Executable, m.config.Args...)
	command.Dir = m.config.Directory
	command.Env = append(os.Environ(), m.config.Environment...)
	configureChildProcess(command)
	stdout, err := command.StdoutPipe()
	if err != nil {
		return Snapshot{}, fmt.Errorf("open sidecar stdout: %w", err)
	}
	stderr, err := command.StderrPipe()
	if err != nil {
		return Snapshot{}, fmt.Errorf("open sidecar stderr: %w", err)
	}
	if err := command.Start(); err != nil {
		return Snapshot{}, fmt.Errorf("start sidecar: %w", err)
	}

	diagnostic := newLimitedBuffer(maxDiagnosticBytes)
	go func() { _, _ = io.Copy(diagnostic, stderr) }()

	timeout := m.config.StartupTimeout
	if timeout <= 0 {
		timeout = defaultStartupTimeout
	}
	type startupResult struct {
		value handshake
		err   error
	}
	startup := make(chan startupResult, 1)
	go func() {
		reader := bufio.NewReaderSize(stdout, 16<<10)
		line, readErr := reader.ReadBytes('\n')
		if readErr != nil {
			startup <- startupResult{err: fmt.Errorf("read sidecar handshake: %w", readErr)}
			return
		}
		value, parseErr := parseHandshake(line)
		startup <- startupResult{value: value, err: parseErr}
		// A healthy sidecar is quiet after the handshake, but draining protects
		// against a diagnostic print filling the pipe and stalling the child.
		if parseErr == nil {
			_, _ = io.Copy(diagnostic, reader)
		}
	}()

	var result startupResult
	select {
	case result = <-startup:
	case <-ctx.Done():
		result.err = ctx.Err()
	case <-time.After(timeout):
		result.err = fmt.Errorf("sidecar handshake timed out after %s", timeout)
	}
	if result.err != nil {
		_ = terminateProcessTree(command)
		_ = command.Wait()
		message := strings.TrimSpace(diagnostic.String())
		if message != "" {
			result.err = fmt.Errorf("%w: %s", result.err, message)
		}
		m.setLastError(result.err)
		return Snapshot{}, result.err
	}

	process := &processState{
		cmd:     command,
		baseURL: fmt.Sprintf("http://127.0.0.1:%d", result.value.Port),
		token:   result.value.Token,
		done:    make(chan struct{}),
	}
	m.mu.Lock()
	m.process = process
	m.lastError = ""
	m.mu.Unlock()

	go m.wait(process, diagnostic)
	return m.Snapshot(), nil
}

func (m *Manager) wait(process *processState, diagnostic *limitedBuffer) {
	err := process.cmd.Wait()
	message := ""
	if err != nil {
		message = err.Error()
	}
	if detail := strings.TrimSpace(diagnostic.String()); detail != "" {
		if message != "" {
			message += ": "
		}
		message += detail
	}
	m.mu.Lock()
	if m.process == process {
		m.process = nil
		m.lastError = message
	}
	close(process.done)
	m.mu.Unlock()
}

func (m *Manager) Stop(ctx context.Context) error {
	m.transition.Lock()
	defer m.transition.Unlock()

	m.mu.RLock()
	process := m.process
	m.mu.RUnlock()
	if process == nil {
		return nil
	}
	terminateErr := terminateProcessTree(process.cmd)
	if terminateErr != nil {
		// Windows may report a taskkill/TerminateProcess error after the child
		// has already exited. Let the process waiter settle that race before
		// treating termination as failed.
		select {
		case <-process.done:
			return nil
		case <-ctx.Done():
			return fmt.Errorf("terminate sidecar: %w", errors.Join(terminateErr, ctx.Err()))
		}
	}
	select {
	case <-process.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (m *Manager) Snapshot() Snapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.process == nil {
		return Snapshot{Running: false, Error: m.lastError}
	}
	pid := 0
	if m.process.cmd != nil && m.process.cmd.Process != nil {
		pid = m.process.cmd.Process.Pid
	}
	return Snapshot{
		Running: true,
		BaseURL: m.process.baseURL,
		PID:     pid,
	}
}

// DoJSON proxies one provider call without exposing the bearer token. Endpoint
// must be a relative /v1 path; absolute URLs and traversal are rejected.
func (m *Manager) DoJSON(
	ctx context.Context,
	method string,
	endpoint string,
	requestBody any,
	responseBody any,
) error {
	if err := validateEndpoint(endpoint); err != nil {
		return err
	}
	if _, deadlined := ctx.Deadline(); !deadlined {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, defaultCallTimeout)
		defer cancel()
	}
	m.mu.RLock()
	process := m.process
	if process == nil {
		m.mu.RUnlock()
		return ErrNotRunning
	}
	baseURL, token := process.baseURL, process.token
	m.mu.RUnlock()

	var body io.Reader
	if requestBody != nil {
		encoded, err := json.Marshal(requestBody)
		if err != nil {
			return fmt.Errorf("encode sidecar request: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, baseURL+endpoint, body)
	if err != nil {
		return fmt.Errorf("create sidecar request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Accept", "application/json")
	if requestBody != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := m.client.Do(request)
	if err != nil {
		return fmt.Errorf("call sidecar: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return fmt.Errorf("read sidecar response: %w", err)
	}
	if len(payload) > maxResponseBytes {
		return errors.New("sidecar response exceeds size limit")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var envelope struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		_ = json.Unmarshal(payload, &envelope)
		message := envelope.Error.Message
		if message == "" {
			message = strings.TrimSpace(string(payload))
		}
		return &HTTPError{StatusCode: response.StatusCode, Code: envelope.Error.Code, Message: message}
	}
	if responseBody == nil || len(payload) == 0 {
		return nil
	}
	if err := json.Unmarshal(payload, responseBody); err != nil {
		return fmt.Errorf("decode sidecar response: %w", err)
	}
	return nil
}

func parseHandshake(line []byte) (handshake, error) {
	if len(line) > 16<<10 {
		return handshake{}, errors.New("sidecar handshake is too large")
	}
	var value handshake
	if err := json.Unmarshal(bytes.TrimSpace(line), &value); err != nil {
		return handshake{}, fmt.Errorf("decode sidecar handshake: %w", err)
	}
	if value.Schema != 1 {
		return handshake{}, fmt.Errorf("unsupported sidecar handshake schema %d", value.Schema)
	}
	if value.Host != "127.0.0.1" {
		return handshake{}, fmt.Errorf("sidecar must bind 127.0.0.1, got %q", value.Host)
	}
	if value.Port < 1 || value.Port > 65535 {
		return handshake{}, fmt.Errorf("invalid sidecar port %d", value.Port)
	}
	if len(value.Token) < 40 {
		return handshake{}, errors.New("sidecar session token has insufficient entropy")
	}
	return value, nil
}

func validateEndpoint(endpoint string) error {
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return fmt.Errorf("parse sidecar endpoint: %w", err)
	}
	if parsed.IsAbs() || parsed.Host != "" || !strings.HasPrefix(parsed.Path, "/v1/") {
		return errors.New("sidecar endpoint must be a relative /v1/ path")
	}
	for _, part := range strings.Split(parsed.Path, "/") {
		if part == ".." {
			return errors.New("sidecar endpoint must not contain traversal")
		}
	}
	return nil
}

func (m *Manager) setLastError(err error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.lastError = err.Error()
}

type limitedBuffer struct {
	mu        sync.Mutex
	remaining int
	buffer    bytes.Buffer
}

func newLimitedBuffer(limit int) *limitedBuffer {
	return &limitedBuffer{remaining: limit}
}

func (b *limitedBuffer) Write(value []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	written := len(value)
	if b.remaining > 0 {
		chunk := value
		if len(chunk) > b.remaining {
			chunk = chunk[:b.remaining]
		}
		_, _ = b.buffer.Write(chunk)
		b.remaining -= len(chunk)
	}
	return written, nil
}

func (b *limitedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.String()
}

// loopbackURL is kept small and testable: even if a future handshake parser is
// relaxed, the HTTP client must never be pointed at a non-loopback host.
func loopbackURL(raw string) bool {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "http" {
		return false
	}
	host := parsed.Hostname()
	return host == "localhost" || net.ParseIP(host) != nil && net.ParseIP(host).IsLoopback()
}
