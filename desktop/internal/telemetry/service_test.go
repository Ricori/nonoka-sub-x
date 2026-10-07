package telemetry

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCollectBeforeRetryPreservesFailureAndRejectsStalePoll(t *testing.T) {
	s, err := New(t.TempDir(), "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer s.cancel()
	s.TrackCloud("task")
	old := s.state.CloudRuns["task"]
	failed := map[string]any{"state": "failed", "stage": "failed", "error": map[string]any{"code": "engine_failed", "stage": "asr"}}
	s.SetCloudStatus(func(context.Context, string) (map[string]any, error) { return failed, nil })
	s.CollectCloud("task")
	s.TrackCloud("task")
	s.recordCloud("task", old, failed)
	s.recordCloud("task", s.state.CloudRuns["task"], map[string]any{"state": "completed"})
	if len(s.state.Pending) != 1 || s.state.Pending[0].SampleID != old.SampleID || s.state.Pending[0].Error.Stage != "asr" {
		t.Fatalf("lost, duplicated or misclassified failure: %+v", s.state.Pending)
	}
	recovered, err := New(filepath.Dir(s.path), "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.cancel()
	if len(recovered.state.Pending) != 1 || len(recovered.state.CloudRuns) != 0 {
		t.Fatal("outcomes not durable")
	}
}

func TestStartFailureAllowsSafeCategoriesOnly(t *testing.T) {
	s, err := New(t.TempDir(), "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer s.cancel()
	for _, code := range []string{"quota_exceeded", "auth_failed", "network_error", "timeout", "rate_limited", "private error text"} {
		s.StartFailure("cloud", code)
		e := s.state.Pending[len(s.state.Pending)-1]
		want := code
		if code == "private error text" {
			want = "unknown"
		}
		if e.Error.Code != want || e.Error.Message != "Task failed: "+want {
			t.Fatalf("unsafe or lost category: %+v", e.Error)
		}
	}
}

func TestCloudStageRecoveryStaysWithinTheFailedRun(t *testing.T) {
	for _, test := range []struct {
		name   string
		events []any
		want   string
	}{
		{"last valid stage", []any{
			map[string]any{"type": "started", "payload": map[string]any{"stage": "queued"}},
			map[string]any{"type": "stage", "payload": map[string]any{"stage": "asr"}},
			map[string]any{"type": "progress", "payload": map[string]any{"stage": "correction"}},
			map[string]any{"type": "failed", "payload": map[string]any{"stage": "private path"}},
		}, "correction"},
		{"do not inherit previous attempt", []any{
			map[string]any{"type": "stage", "payload": map[string]any{"stage": "asr"}},
			map[string]any{"type": "started", "payload": map[string]any{"stage": "queued"}},
			map[string]any{"type": "failed", "payload": map[string]any{"stage": "failed"}},
		}, "unknown"},
		{"event history unavailable", nil, "unknown"},
	} {
		t.Run(test.name, func(t *testing.T) {
			s, err := New(t.TempDir(), "0.4.2", nil)
			if err != nil {
				t.Fatal(err)
			}
			defer s.cancel()
			s.TrackCloud("task")
			s.recordCloud("task", s.state.CloudRuns["task"], map[string]any{"state": "failed", "stage": "failed", "events": test.events, "error": map[string]any{"code": "engine_failed"}})
			if len(s.state.Pending) != 1 || s.state.Pending[0].Error.Stage != test.want {
				t.Fatalf("wrong failure stage: %+v", s.state.Pending)
			}
		})
	}
}

func TestRetryPersistenceAndPrivacy(t *testing.T) {
	root := t.TempDir()
	s, err := New(root, "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer s.cancel()
	id := NewID()
	event := Event{Type: "task_error", SampleID: id, ExecutionProvider: "local", Error: &Failure{Phase: "execution", Stage: "correction", Code: "engine_failed", Message: "sk-secret C:/Users/private subtitle"}}
	if err := s.Enqueue(event); err != nil {
		t.Fatal(err)
	}
	_ = s.Enqueue(event)
	if len(s.state.Pending) != 1 {
		t.Fatal("duplicate")
	}
	data, _ := os.ReadFile(s.path)
	if strings.Contains(string(data), "secret") {
		t.Fatal("leaked error text")
	}
	recovered, err := New(root, "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.cancel()
	if recovered.state.InstallationID != s.state.InstallationID || len(recovered.state.Pending) != 1 {
		t.Fatal("lost queue or identity")
	}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(503)
			return
		}
		var e Event
		_ = json.NewDecoder(r.Body).Decode(&e)
		if e.SampleID != id {
			t.Error("retry changed sample")
		}
		w.WriteHeader(204)
	}))
	defer server.Close()
	recovered.endpoint = server.URL
	recovered.sendOne()
	if len(recovered.state.Pending) != 1 {
		t.Fatal("dropped on 503")
	}
	recovered.nextSend = time.Time{}
	recovered.sendOne()
	if len(recovered.state.Pending) != 0 {
		t.Fatal("not acknowledged")
	}
}

func TestCloudFailureWithoutFrontendAndNoFalsePollingError(t *testing.T) {
	s, _ := New(t.TempDir(), "0.4.2", nil)
	defer s.cancel()
	s.TrackCloud("task")
	s.SetCloudStatus(func(context.Context, string) (map[string]any, error) { return nil, context.DeadlineExceeded })
	s.pollCloud()
	if len(s.state.Pending) != 0 || len(s.state.CloudRuns) != 1 {
		t.Fatal("poll timeout treated as failure")
	}
	s.SetCloudStatus(func(context.Context, string) (map[string]any, error) {
		return map[string]any{"state": "failed", "stage": "correction", "error": map[string]any{"code": "quota_exceeded", "message": "secret"}}, nil
	})
	s.pollCloud()
	s.pollCloud()
	if len(s.state.Pending) != 1 || len(s.state.CloudRuns) != 0 {
		t.Fatal("not exactly one final error")
	}
	if s.state.Pending[0].Models != nil {
		t.Fatal("invented cloud models")
	}
}

func TestCloudFailureKeepsOccurrenceTime(t *testing.T) {
	s, err := New(t.TempDir(), "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer s.cancel()
	s.TrackCloud("task")
	at := time.Now().Add(-2 * time.Hour).UTC().Format(time.RFC3339)
	s.SetCloudStatus(func(context.Context, string) (map[string]any, error) {
		return map[string]any{"state": "failed", "updated_at": at}, nil
	})
	s.pollCloud()
	if len(s.state.Pending) != 1 || s.state.Pending[0].OccurredAt != at {
		t.Fatalf("failure timestamp changed: %+v", s.state.Pending)
	}
}

func TestQueueBoundAndExpiry(t *testing.T) {
	s, _ := New(t.TempDir(), "0.4.2", nil)
	defer s.cancel()
	for i := 0; i < 55; i++ {
		_ = s.Enqueue(Event{Type: "task_error", ExecutionProvider: "local"})
	}
	if len(s.state.Pending) != 50 {
		t.Fatal("queue unbounded")
	}
	for i := range s.state.Pending {
		s.state.Pending[i].OccurredAt = time.Now().Add(-25 * time.Hour).UTC().Format(time.RFC3339)
	}
	s.trimLocked()
	if len(s.state.Pending) != 0 {
		t.Fatal("expired events retained")
	}
}

type transferSource struct {
	service            *Service
	event              Event
	persistedBeforeAck bool
}

func (f *transferSource) DoJSON(_ context.Context, method, endpoint string, body, result any) error {
	if method == http.MethodGet {
		data, _ := json.Marshal(map[string]any{"events": []Event{f.event}})
		return json.Unmarshal(data, result)
	}
	data, _ := os.ReadFile(f.service.path)
	var state diskState
	_ = json.Unmarshal(data, &state)
	f.persistedBeforeAck = len(state.Pending) == 1 && state.Pending[0].SampleID == f.event.SampleID
	return nil
}

func TestSidecarAcknowledgedOnlyAfterDurableTransfer(t *testing.T) {
	source := &transferSource{event: Event{Type: "task_error", SampleID: NewID(), OccurredAt: time.Now().UTC().Format(time.RFC3339), ExecutionProvider: "local", Error: &Failure{Phase: "execution", Stage: "unknown", Code: "unknown"}}}
	s, _ := New(t.TempDir(), "0.4.2", source)
	defer s.cancel()
	source.service = s
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	defer server.Close()
	s.endpoint = server.URL
	s.cycle()
	if !source.persistedBeforeAck || len(s.state.Pending) != 1 {
		t.Fatal("ack preceded durable transfer or lost failed send")
	}
}
