package screentext

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

type fakeEngine struct {
	mu       sync.Mutex
	started  []map[string]any
	statuses []map[string]any
	calls    int
	listing  map[string]any
}

func (f *fakeEngine) StartTask(request map[string]any) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.started = append(f.started, request)
	return map[string]any{"task_id": "abc", "state": "queued", "kind": Target}, nil
}

func (f *fakeEngine) ListTasks() (map[string]any, error) {
	if f.listing == nil {
		return map[string]any{"tasks": []any{}}, nil
	}
	return f.listing, nil
}

func (f *fakeEngine) TaskStatus(string) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	index := f.calls
	if index >= len(f.statuses) {
		index = len(f.statuses) - 1
	}
	f.calls++
	return f.statuses[index], nil
}

func (f *fakeEngine) TaskEvents(string, int) (map[string]any, error) {
	return map[string]any{"events": []any{
		map[string]any{"type": "stage", "payload": map[string]any{}},
		map[string]any{"type": "completed", "payload": map[string]any{"document_rev": float64(7)}},
	}}, nil
}

func (f *fakeEngine) CancelTask(string) (map[string]any, error) {
	return map[string]any{"state": "cancelled"}, nil
}

type fakeMedia map[string]library.Entry

func (m fakeMedia) Get(id string) (library.Entry, error) {
	entry, ok := m[id]
	if !ok {
		return library.Entry{}, errors.New("unknown media")
	}
	return entry, nil
}

type recorder struct {
	mu     sync.Mutex
	events []string
	data   []any
}

func (r *recorder) emit(name string, data any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, name)
	r.data = append(r.data, data)
}

func (r *recorder) wait(t *testing.T, name string) any {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		for index, event := range r.events {
			if event == name {
				data := r.data[index]
				r.mu.Unlock()
				return data
			}
		}
		r.mu.Unlock()
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("no %s event", name)
	return nil
}

func newService(engine *fakeEngine) (*Service, *recorder) {
	pollInterval = time.Millisecond
	service := New(engine, fakeMedia{"vid": {ID: "vid", SourcePath: `D:\v.mp4`, Title: "v", Available: true}})
	events := &recorder{}
	service.emit = events.emit
	return service, events
}

func TestStartSendsAScreenTextRequestAndAnnouncesTheDocument(t *testing.T) {
	engine := &fakeEngine{statuses: []map[string]any{{"state": "running"}, {"state": "completed"}}}
	service, events := newService(engine)
	if _, err := service.Start("vid", Options{SkipMain: true}); err != nil {
		t.Fatal(err)
	}
	request := engine.started[0]
	if request["target"] != Target || request["device"] != "cuda" {
		t.Fatalf("request = %v", request)
	}
	options := request["screen_text"].(map[string]any)
	if options["translate"] != "zh" || options["cover"] != "plate" || options["on_edited"] != "replace" || options["skip_main"] != true {
		t.Fatalf("options = %v", options)
	}
	changed := events.wait(t, DocumentChangedEvent).(map[string]any)
	if changed["videoId"] != "vid" || changed["rev"] != 7 {
		t.Fatalf("document:changed = %v", changed)
	}
	events.wait(t, ProgressEvent)
}

func TestATrialChangesNoDocument(t *testing.T) {
	engine := &fakeEngine{statuses: []map[string]any{{"state": "completed"}}}
	service, events := newService(engine)
	if _, err := service.Start("vid", Options{Trial: true, Start: 10, End: 70}); err != nil {
		t.Fatal(err)
	}
	events.wait(t, ProgressEvent)
	time.Sleep(20 * time.Millisecond)
	events.mu.Lock()
	defer events.mu.Unlock()
	for _, name := range events.events {
		if name == DocumentChangedEvent {
			t.Fatal("a trial announced a document change")
		}
	}
	span := engine.started[0]["screen_text"].(map[string]any)["range"].(map[string]any)
	if span["start"] != 10.0 || span["end"] != 70.0 {
		t.Fatalf("range = %v", span)
	}
}

func TestBadOptionsAreRefused(t *testing.T) {
	service, _ := newService(&fakeEngine{statuses: []map[string]any{{"state": "completed"}}})
	for _, options := range []Options{{Translate: "en"}, {Cover: "blur"}, {Start: 5, End: 2}, {ROI: []float64{0.1}}} {
		if _, err := service.Start("vid", options); err == nil {
			t.Fatalf("accepted %+v", options)
		}
	}
	if _, err := service.Start("missing", Options{}); err == nil {
		t.Fatal("accepted an unknown video")
	}
}

func TestStatusFindsTheLatestTaskAfterARestart(t *testing.T) {
	engine := &fakeEngine{
		statuses: []map[string]any{{"state": "completed", "task_id": "new"}},
		listing: map[string]any{"tasks": []any{
			map[string]any{"media_id": "vid", "snapshot": map[string]any{"task_id": "old", "kind": Target, "created_at": "2026-01-01"}},
			map[string]any{"media_id": "vid", "snapshot": map[string]any{"task_id": "new", "kind": Target, "created_at": "2026-02-01"}},
			map[string]any{"media_id": "vid", "snapshot": map[string]any{"task_id": "asr", "created_at": "2026-03-01"}},
		}},
	}
	service, _ := newService(engine)
	if _, err := service.Status("vid"); err != nil {
		t.Fatal(err)
	}
	if service.tasks["vid"] != "new" {
		t.Fatalf("latest = %q", service.tasks["vid"])
	}
}

func TestRecognitionReadsTheOriginalOfAnErasedVideo(t *testing.T) {
	dir := t.TempDir()
	original := filepath.Join(dir, "show.mp4")
	if err := os.WriteFile(original, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	erased := library.Entry{SourcePath: filepath.Join(dir, "show - 抹字.mp4"), ErasedFrom: original}
	if got := recognitionSource(erased); got != original {
		t.Fatalf("recognition source = %q, want the original", got)
	}
	erased.ErasedFrom = filepath.Join(dir, "gone.mp4")
	if got := recognitionSource(erased); got != erased.SourcePath {
		t.Fatalf("a missing original must fall back to the source, got %q", got)
	}
}
