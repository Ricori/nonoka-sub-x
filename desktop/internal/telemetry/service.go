// Package telemetry sends only model configuration and fixed error summaries.
package telemetry

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

const Endpoint = "https://nonoka.online/v1/telemetry"

type Caller interface {
	DoJSON(context.Context, string, string, any, any) error
}
type Route struct {
	Provider string `json:"provider"`
	Model    string `json:"model"`
}
type Models struct {
	Default   *Route           `json:"default"`
	Overrides map[string]Route `json:"overrides"`
}
type Failure struct {
	Phase   string `json:"phase"`
	Stage   string `json:"stage"`
	Code    string `json:"code"`
	Message string `json:"message"`
}
type Event struct {
	Type              string   `json:"type"`
	InstallationID    string   `json:"installation_id"`
	SampleID          string   `json:"sample_id"`
	OccurredAt        string   `json:"occurred_at"`
	AppVersion        string   `json:"app_version"`
	ExecutionProvider string   `json:"execution_provider"`
	Models            *Models  `json:"models"`
	Error             *Failure `json:"error,omitempty"`
}
type cloudRun struct {
	SampleID string    `json:"sample_id"`
	At       time.Time `json:"at"`
}

type diskState struct {
	CloudRuns      map[string]cloudRun `json:"cloud_runs,omitempty"`
	InstallationID string              `json:"installation_id"`
	Pending        []Event             `json:"pending"`
}
type Service struct {
	cloudStatus             func(context.Context, string) (map[string]any, error)
	nextSend                time.Time
	failures                int
	mu                      sync.Mutex
	path, version, endpoint string
	state                   diskState
	caller                  Caller
	client                  *http.Client
	ctx                     context.Context
	cancel                  context.CancelFunc
	done                    chan struct{}
}

func New(root, version string, caller Caller) (*Service, error) {
	ctx, cancel := context.WithCancel(context.Background())
	s := &Service{path: filepath.Join(root, "telemetry-desktop.json"), version: version, endpoint: Endpoint, caller: caller,
		client: &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, ctx: ctx, cancel: cancel, done: make(chan struct{})}
	if b, err := os.ReadFile(s.path); err == nil {
		_ = json.Unmarshal(b, &s.state)
	}
	if s.state.InstallationID == "" {
		s.state.InstallationID = NewID()
	}
	s.trimLocked()
	if err := s.saveLocked(); err != nil {
		cancel()
		return nil, err
	}
	return s, nil
}

func NewID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	v := hex.EncodeToString(b[:])
	return v[:8] + "-" + v[8:12] + "-" + v[12:16] + "-" + v[16:20] + "-" + v[20:]
}

func (s *Service) Enqueue(e Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.enqueueLocked(e)
}

func (s *Service) enqueueLocked(e Event) error {
	e.InstallationID = s.state.InstallationID
	e.AppVersion = s.version
	if e.OccurredAt == "" {
		e.OccurredAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	if e.SampleID == "" {
		e.SampleID = NewID()
	}
	// Error text is a fixed code summary, never the original engine message.
	if e.Error != nil {
		e.Error.Code = safeCode(e.Error.Code)
		e.Error.Stage = safeStage(e.Error.Stage)
		e.Error.Message = "Task failed: " + e.Error.Code
	}
	for _, old := range s.state.Pending {
		if old.SampleID == e.SampleID && old.Type == e.Type {
			return nil
		}
	}
	previous := append([]Event(nil), s.state.Pending...)
	s.state.Pending = append(s.state.Pending, e)
	s.trimLocked()
	if err := s.saveLocked(); err != nil {
		s.state.Pending = previous
		return err
	}
	return nil
}

func (s *Service) trimLocked() {
	next := make([]Event, 0, len(s.state.Pending))
	for _, e := range s.state.Pending {
		at, err := time.Parse(time.RFC3339Nano, e.OccurredAt)
		if err == nil && time.Since(at) < 24*time.Hour {
			next = append(next, e)
		}
	}
	if len(next) > 50 {
		next = next[len(next)-50:]
	}
	s.state.Pending = next
}

func (s *Service) saveLocked() error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0700); err != nil {
		return err
	}
	b, err := json.Marshal(s.state)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(s.path), ".telemetry-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(f.Name(), s.path)
}

// Start drains the authenticated sidecar even without an open task page.
func (s *Service) Start() {
	go func() {
		defer close(s.done)
		timer := time.NewTicker(5 * time.Second)
		defer timer.Stop()
		for {
			s.cycle()
			select {
			case <-s.ctx.Done():
				return
			case <-timer.C:
			}
		}
	}()
}
func (s *Service) Close() { s.cancel(); <-s.done }

func (s *Service) cycle() {
	s.collectLocal()
	s.pollCloud()
	s.sendOne()
}

func (s *Service) collectLocal() {
	if s.caller == nil {
		return
	}
	var page struct {
		Events []Event `json:"events"`
	}
	ctx, cancel := context.WithTimeout(s.ctx, 5*time.Second)
	err := s.caller.DoJSON(ctx, http.MethodGet, "/v1/telemetry", nil, &page)
	cancel()
	if err != nil {
		return
	}
	var ids []string
	for _, e := range page.Events {
		if s.Enqueue(e) == nil {
			ids = append(ids, e.SampleID+":"+e.Type)
		}
	}
	if len(ids) > 0 {
		ctx, cancel := context.WithTimeout(s.ctx, 5*time.Second)
		defer cancel()
		_ = s.caller.DoJSON(ctx, http.MethodPost, "/v1/telemetry/ack", map[string]any{"ids": ids}, nil)
	}
}

func (s *Service) sendOne() {
	s.mu.Lock()
	s.trimLocked()
	if len(s.state.Pending) == 0 || time.Now().Before(s.nextSend) {
		s.mu.Unlock()
		return
	}
	event := s.state.Pending[0]
	s.mu.Unlock()
	body, err := json.Marshal(event)
	if err != nil {
		return
	}
	request, err := http.NewRequestWithContext(s.ctx, http.MethodPost, s.endpoint, bytes.NewReader(body))
	if err != nil {
		return
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := s.client.Do(request)
	if err != nil {
		s.backoff(0)
		return
	}
	response.Body.Close()
	// Permanent schema errors are discarded; throttling and outages keep the item.
	if response.StatusCode != 204 && response.StatusCode != 400 && response.StatusCode != 413 {
		seconds, _ := strconv.Atoi(response.Header.Get("Retry-After"))
		s.backoff(time.Duration(seconds) * time.Second)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failures = 0
	s.nextSend = time.Time{}
	for i, e := range s.state.Pending {
		if e.SampleID == event.SampleID && e.Type == event.Type {
			s.state.Pending = append(s.state.Pending[:i], s.state.Pending[i+1:]...)
			break
		}
	}
	_ = s.saveLocked()
}

// StartFailure is only for definite refusal, never an ambiguous task POST timeout.
func (s *Service) StartFailure(provider, code string) {
	code = safeCode(code)
	_ = s.Enqueue(Event{Type: "task_error", ExecutionProvider: provider, Error: &Failure{Phase: "start", Stage: "unknown", Code: code}})
}

func (s *Service) backoff(delay time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failures++
	if delay <= 0 {
		delay = time.Duration(1<<min(s.failures, 6)) * time.Second
	}
	s.nextSend = time.Now().Add(min(delay, 5*time.Minute))
}

func (s *Service) SetCloudStatus(fn func(context.Context, string) (map[string]any, error)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cloudStatus = fn
}
func (s *Service) TrackCloud(taskID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state.CloudRuns == nil {
		s.state.CloudRuns = map[string]cloudRun{}
	}
	if _, exists := s.state.CloudRuns[taskID]; !exists && len(s.state.CloudRuns) >= 50 {
		return
	}
	s.state.CloudRuns[taskID] = cloudRun{NewID(), time.Now().UTC()}
	_ = s.saveLocked()
}

func (s *Service) pollCloud() {
	s.mu.Lock()
	if s.cloudStatus == nil {
		s.mu.Unlock()
		return
	}
	fn := s.cloudStatus
	runs := maps.Clone(s.state.CloudRuns)
	s.mu.Unlock()
	for id, run := range runs {
		if s.ctx.Err() != nil {
			return
		}
		ctx, cancel := context.WithTimeout(s.ctx, 5*time.Second)
		snapshot, err := fn(ctx, id)
		cancel()
		if err != nil && time.Since(run.At) < 24*time.Hour {
			continue
		}
		s.recordCloud(id, run, snapshot)
	}
}

// CollectCloud captures the previous outcome before a retry replaces it remotely.
func (s *Service) CollectCloud(id string) {
	s.mu.Lock()
	run, exists := s.state.CloudRuns[id]
	fn := s.cloudStatus
	s.mu.Unlock()
	if !exists || fn == nil {
		return
	}
	ctx, cancel := context.WithTimeout(s.ctx, 5*time.Second)
	defer cancel()
	if snapshot, err := fn(ctx, id); err == nil {
		s.recordCloud(id, run, snapshot)
	}
}

func (s *Service) recordCloud(id string, run cloudRun, snapshot map[string]any) {
	state, _ := snapshot["state"].(string)
	switch state {
	case "failed", "completed", "cancelled", "interrupted":
	default:
		if time.Since(run.At) < 24*time.Hour {
			return
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if current, exists := s.state.CloudRuns[id]; !exists || current.SampleID != run.SampleID {
		return
	}
	if state == "failed" {
		failure, _ := snapshot["error"].(map[string]any)
		code, _ := failure["code"].(string)
		stage, _ := snapshot["stage"].(string)
		if failureStage, ok := failure["stage"].(string); ok && safeStage(failureStage) != "unknown" {
			stage = failureStage
		}
		if safeStage(stage) == "unknown" {
			events, _ := snapshot["events"].([]any)
			for i := len(events) - 1; i >= 0; i-- {
				event, _ := events[i].(map[string]any)
				if event["type"] == "started" {
					break
				}
				payload, _ := event["payload"].(map[string]any)
				candidate, _ := payload["stage"].(string)
				if safeStage(candidate) != "unknown" {
					stage = candidate
					break
				}
			}
		}
		at, _ := snapshot["updated_at"].(string)
		if _, err := time.Parse(time.RFC3339Nano, at); err != nil {
			at = ""
		}
		event := Event{Type: "task_error", SampleID: run.SampleID, OccurredAt: at, ExecutionProvider: "cloud", Error: &Failure{Phase: "execution", Stage: stage, Code: code}}
		if s.enqueueLocked(event) != nil {
			return
		}
	}
	delete(s.state.CloudRuns, id)
	_ = s.saveLocked()
}

func safeCode(code string) string {
	switch code {
	case "quota_exceeded", "auth_failed", "missing_llm_key", "missing_gpu", "missing_model", "insufficient_disk", "missing_dependency", "agent_failed", "engine_failed", "invalid_request", "invalid_state", "runtime_not_ready", "sidecar_unavailable", "rate_limited", "timeout", "network_error", "out_of_memory":
		return code
	}
	return "unknown"
}
func safeStage(stage string) string {
	switch stage {
	case "media", "separate", "separation", "vocal", "vad", "asr", "align", "aligned", "stable", "raw-srt", "final-srt", "correction", "planning", "research", "knowledge", "translation":
		return stage
	}
	return "unknown"
}
