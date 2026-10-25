package cloud

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
	"github.com/Ricori/nonoka-x/desktop/internal/telemetry"
)

const relayStable = `{"segments":[{"id":1,"start":0,"end":1,"text":"fixture"}]}`
const relayLocalTask = "fedcba9876543210fedcba9876543210"

// relayBackend is the cloud as a relay sees it: one upload, jobs that queue,
// run and complete, a published stable.json, and the DELETE that collects it.
type relayBackend struct {
	t       *testing.T
	mu      sync.Mutex
	server  *httptest.Server
	jobs    []string
	polls   map[string]int
	missing map[string]bool
	// The digest the manifest publishes; empty means the true one.
	digest    string
	submitted []map[string]any
	deleted   []string
	cancelled []string
	retried   []string
}

func newRelayBackend(t *testing.T) *relayBackend {
	backend := &relayBackend{t: t, polls: map[string]int{}, missing: map[string]bool{}}
	backend.server = httptest.NewServer(http.HandlerFunc(backend.serve))
	t.Cleanup(backend.server.Close)
	return backend
}

func (b *relayBackend) serve(writer http.ResponseWriter, request *http.Request) {
	b.mu.Lock()
	defer b.mu.Unlock()
	writer.Header().Set("Content-Type", "application/json")
	path := request.URL.Path
	if request.Method == http.MethodPut && path == "/upload" {
		_, _ = io.Copy(io.Discard, request.Body)
		writer.WriteHeader(http.StatusNoContent)
		return
	}
	if request.Header.Get("Authorization") != "Bearer login-key" {
		http.Error(writer, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
		return
	}
	switch {
	case path == "/v1/session":
		_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":2,"running":0}`))
		return
	case path == "/v1/uploads/init":
		_, _ = writer.Write([]byte(`{"objectId":"upl_0123456789abcdef0123456789abcdef","uploadUrl":"` + b.server.URL + `/upload","contentType":"audio/mp4"}`))
		return
	case request.Method == http.MethodPost && path == "/v1/tasks":
		var body map[string]any
		_ = json.NewDecoder(request.Body).Decode(&body)
		b.submitted = append(b.submitted, body)
		job := fmt.Sprintf("job_%024x", len(b.jobs)+1)
		b.jobs = append(b.jobs, job)
		_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + job + `","provider":"cloud","state":"queued"}`))
		return
	}
	job := ""
	for _, candidate := range b.jobs {
		if strings.HasPrefix(path, "/v1/tasks/"+candidate) {
			job = candidate
		}
	}
	if job == "" || b.missing[job] {
		http.Error(writer, `{"detail":"作业不存在"}`, http.StatusNotFound)
		return
	}
	suffix := strings.TrimPrefix(path, "/v1/tasks/"+job)
	switch {
	case request.Method == http.MethodGet && suffix == "":
		// Queued, then running, then done: enough polls to see each.
		b.polls[job]++
		state := map[int]string{1: "queued", 2: "running"}[b.polls[job]]
		if state == "" {
			state = "completed"
		}
		_, _ = fmt.Fprintf(writer, `{"schema":1,"task_id":"%s","provider":"cloud","state":"%s","stage":"aligned","engine":{"version":"0.6.0","commit":"cloud"}}`, job, state)
	case request.Method == http.MethodGet && suffix == "/events":
		after := request.URL.Query().Get("after")
		events := []string{
			`{"cursor":1,"type":"started","timestamp":"t1","payload":{"message":"云端开始"}}`,
			`{"cursor":2,"type":"stage","timestamp":"t2","payload":{"stage":"aligned"}}`,
			`{"cursor":3,"type":"completed","timestamp":"t3","payload":{}}`,
		}
		var page []string
		for index, event := range events {
			if index+1 > atoi(after) {
				page = append(page, event)
			}
		}
		_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + job + `","events":[` + strings.Join(page, ",") + `]}`))
	case request.Method == http.MethodGet && suffix == "/artifacts":
		digest := b.digest
		if digest == "" {
			sum := sha256.Sum256([]byte(relayStable))
			digest = hex.EncodeToString(sum[:])
		}
		_, _ = fmt.Fprintf(writer, `{"schema":1,"task_id":"%s","engine_commit":"cloud","artifacts":{"stable_json":{"uri":"/v1/tasks/%s/artifacts/stable_json","sha256":"%s","bytes":%d}}}`, job, job, digest, len(relayStable))
	case request.Method == http.MethodGet && suffix == "/artifacts/stable_json":
		_, _ = writer.Write([]byte(relayStable))
	case request.Method == http.MethodDelete && suffix == "":
		b.deleted = append(b.deleted, job)
		b.missing[job] = true
		_, _ = writer.Write([]byte(`{"removed":"` + job + `"}`))
	case request.Method == http.MethodPost && suffix == "/cancel":
		b.cancelled = append(b.cancelled, job)
		_, _ = writer.Write([]byte(`{"state":"cancelled"}`))
	case request.Method == http.MethodPost && (suffix == "/retry" || suffix == "/resume"):
		b.retried = append(b.retried, job)
		b.polls[job] = 0
		_, _ = writer.Write([]byte(`{"state":"queued"}`))
	default:
		http.NotFound(writer, request)
	}
}

func atoi(value string) int {
	number := 0
	_, _ = fmt.Sscan(value, &number)
	return number
}

func (b *relayBackend) snapshot() (submitted []map[string]any, deleted, cancelled, retried []string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]map[string]any(nil), b.submitted...), append([]string(nil), b.deleted...), append([]string(nil), b.cancelled...), append([]string(nil), b.retried...)
}

// relayLocal is the sidecar as the local leg sees it.
type relayLocal struct {
	mu       sync.Mutex
	started  []map[string]any
	stable   string
	refusal  error
	state    string
	actions  []string
	syncRead bool
}

func (p *relayLocal) DoJSON(_ context.Context, method, endpoint string, body any, result any) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	respond := func(value any) error {
		encoded, _ := json.Marshal(value)
		return json.Unmarshal(encoded, result)
	}
	switch {
	case method == http.MethodPost && endpoint == "/v1/tasks":
		if p.refusal != nil {
			return p.refusal
		}
		request := body.(map[string]any)
		p.started = append(p.started, request)
		source := request["source"].(map[string]any)
		data, err := os.ReadFile(source["stable_json"].(string))
		if err != nil {
			return err
		}
		p.stable = string(data)
		return respond(map[string]any{"schema": 1, "task_id": relayLocalTask, "provider": "local", "state": "queued"})
	case method == http.MethodGet && endpoint == "/v1/tasks/"+relayLocalTask:
		state := p.state
		if state == "" {
			state = "completed"
		}
		return respond(map[string]any{"state": state, "stage": "final-srt", "engine": map[string]any{"version": "0.6.0", "commit": "local"}})
	case method == http.MethodGet && strings.HasPrefix(endpoint, "/v1/tasks/"+relayLocalTask+"/events"):
		if !strings.HasSuffix(endpoint, "after=0") {
			return respond(map[string]any{"events": []any{}})
		}
		return respond(map[string]any{"events": []any{
			map[string]any{"cursor": 1, "type": "started", "payload": map[string]any{}},
			map[string]any{"cursor": 2, "type": "stage", "payload": map[string]any{"stage": "vocal", "reused": true}},
			map[string]any{"cursor": 3, "type": "stage", "payload": map[string]any{"stage": "stable", "reused": true}},
			map[string]any{"cursor": 4, "type": "stage", "payload": map[string]any{"stage": "raw-srt"}},
			map[string]any{"cursor": 5, "type": "completed", "payload": map[string]any{"artifacts": map[string]any{}}},
		}})
	case method == http.MethodGet && endpoint == "/v1/tasks/"+relayLocalTask+"/artifacts":
		p.syncRead = true
		return respond(map[string]any{"schema": 1, "task_id": relayLocalTask, "engine_commit": "local", "artifacts": map[string]any{}})
	case method == http.MethodPost && strings.HasPrefix(endpoint, "/v1/tasks/"+relayLocalTask+"/"):
		p.actions = append(p.actions, strings.TrimPrefix(endpoint, "/v1/tasks/"+relayLocalTask+"/"))
		return respond(map[string]any{"state": "queued"})
	}
	return fmt.Errorf("unexpected provider call %s %s", method, endpoint)
}

func newRelayService(t *testing.T, provider providerCaller) (*Service, *relayBackend, string) {
	t.Helper()
	root := t.TempDir()
	source := filepath.Join(root, "video.mp4")
	if err := os.WriteFile(source, []byte("video"), 0o600); err != nil {
		t.Fatal(err)
	}
	backend := newRelayBackend(t)
	service, err := New(root, provider, fakeMedia{path: source})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	service.relayPoll = 5 * time.Millisecond
	service.extractor = func(_ context.Context, _, output string) error {
		return os.WriteFile(output, []byte("audio-fixture"), 0o600)
	}
	if _, err := service.Login(backend.server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	return service, backend, root
}

func waitRelay(t *testing.T, service *Service, id string, states ...string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		snapshot, err := service.TaskStatus(id)
		if err != nil {
			t.Fatal(err)
		}
		for _, state := range states {
			if snapshot["state"] == state {
				return snapshot
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	snapshot, _ := service.TaskStatus(id)
	t.Fatalf("relay did not reach %v: %#v", states, snapshot)
	return nil
}

func relayEventList(t *testing.T, service *Service, id string) []relayEvent {
	t.Helper()
	page, err := service.TaskEvents(id, 0)
	if err != nil {
		t.Fatal(err)
	}
	return page["events"].([]relayEvent)
}

func TestRelayRecognisesInTheCloudAndContinuesHere(t *testing.T) {
	local := &relayLocal{}
	service, backend, _ := newRelayService(t, local)
	options := map[string]any{
		"target":     "final-srt",
		"language":   "ja",
		"knowledge":  "update",
		"correction": map[string]any{"enabled": true, "media": "audio", "retrieval": "local", "difficulty": "quality"},
		"diarization": map[string]any{
			"level": "judge-text",
		},
		"knowledge_subjects": []any{map[string]any{"kind": "streamer", "subject": "のの"}},
	}
	snapshot, err := service.StartTask("loc_0123456789ab", options)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := snapshot["task_id"].(string)
	if !validRelayID.MatchString(id) || snapshot["provider"] != "cloud" || snapshot["state"] != "queued" {
		t.Fatalf("start snapshot = %#v", snapshot)
	}
	completed := waitRelay(t, service, id, "completed", "failed")
	if completed["state"] != "completed" || completed["stage"] != "completed" {
		t.Fatalf("relay ended %#v", completed)
	}

	submitted, deleted, _, _ := backend.snapshot()
	// The cloud recognises and nothing more: everything after stable.json is
	// this desktop's, so none of it may reach the job request.
	if len(submitted) != 1 || submitted[0]["target"] != "stable" || submitted[0]["vocal_profile"] != "cost" {
		t.Fatalf("cloud request = %#v", submitted)
	}
	for _, key := range []string{"correction", "knowledge", "knowledge_subjects"} {
		if _, exists := submitted[0][key]; exists {
			t.Fatalf("cloud request carries %s: %#v", key, submitted[0])
		}
	}
	// Collected only after the file was on disk and checked.
	if len(deleted) != 1 || deleted[0] != fmt.Sprintf("job_%024x", 1) {
		t.Fatalf("deleted jobs = %v", deleted)
	}

	local.mu.Lock()
	started := local.started
	stable := local.stable
	local.mu.Unlock()
	if len(started) != 1 || stable != relayStable {
		t.Fatalf("local leg = %#v, stable = %q", started, stable)
	}
	request := started[0]
	source := request["source"].(map[string]any)
	if source["kind"] != "cloud_stable" || source["video_id"] != "loc_0123456789ab" || request["target"] != "final-srt" {
		t.Fatalf("local request = %#v", request)
	}
	if request["knowledge"] != "update" || request["knowledge_subjects"] == nil {
		t.Fatalf("local request lost the knowledge settings: %#v", request)
	}
	if correction := request["correction"].(map[string]any); correction["media"] != "audio" {
		t.Fatalf("local correction = %#v", correction)
	}
	if _, exists := request["diarization"]; exists {
		t.Fatalf("diarization belongs to the cloud leg: %#v", request)
	}

	events := relayEventList(t, service, id)
	var types []string
	for index, event := range events {
		if event.Cursor != index+1 || event.TaskID != id {
			t.Fatalf("event %d = %#v", index, event)
		}
		types = append(types, event.Type)
		if stage, _ := event.Payload["stage"].(string); event.Type == "stage" && (stage == "vocal" || stage == "stable") {
			t.Fatalf("replayed stage leaked into the log: %#v", event)
		}
	}
	joined := strings.Join(types, ",")
	// One "started" from the relay and one from the cloud, the cloud's own
	// "completed" swallowed, two handoffs, and the local "completed" last.
	if joined != "started,started,stage,handoff,log,handoff,stage,completed" {
		t.Fatalf("event types = %s", joined)
	}
	if page, _ := service.TaskEvents(id, len(events)); len(page["events"].([]relayEvent)) != 0 || page["next_cursor"] != len(events) {
		t.Fatalf("tail page = %#v", page)
	}

	manifest, err := service.TaskArtifacts(id)
	if err != nil || manifest["task_id"] != id {
		t.Fatalf("artifacts = %#v, %v", manifest, err)
	}
	listed, _ := service.ListTasks()
	items := listed["tasks"].([]map[string]any)
	if len(items) != 1 || items[0]["media_id"] != "loc_0123456789ab" || items[0]["title"] != "demo.mp4" {
		t.Fatalf("tasks = %#v", items)
	}
	// The sync reads the local leg's artifacts, under the relay's id.
	local.mu.Lock()
	local.syncRead = false
	local.mu.Unlock()
	_, _ = service.SyncLocalTask(id, "loc_0123456789ab", "fingerprint", "demo", 60)
	local.mu.Lock()
	read := local.syncRead
	local.mu.Unlock()
	if !read {
		t.Fatal("syncing a relay did not read its local leg's artifacts")
	}
}

func TestRelayNeverCollectsAJobWhoseResultFailsItsDigest(t *testing.T) {
	local := &relayLocal{}
	service, backend, _ := newRelayService(t, local)
	backend.digest = strings.Repeat("0", 64)
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "raw-srt"})
	if err != nil {
		t.Fatal(err)
	}
	id := snapshot["task_id"].(string)
	failed := waitRelay(t, service, id, "failed", "completed")
	failure, _ := failed["error"].(map[string]any)
	if failed["state"] != "failed" || failure["code"] != "fetch_failed" {
		t.Fatalf("relay = %#v", failed)
	}
	if _, deleted, _, _ := backend.snapshot(); len(deleted) != 0 {
		t.Fatalf("a job was deleted with no verified copy here: %v", deleted)
	}
	if _, err := os.Stat(service.relayStablePath(id)); !os.IsNotExist(err) {
		t.Fatalf("an unverified stable.json was kept: %v", err)
	}

	// Retrying the collection once the published digest is right finishes it.
	backend.mu.Lock()
	backend.digest = ""
	backend.mu.Unlock()
	if _, err := service.RetryTask(id); err != nil {
		t.Fatal(err)
	}
	if done := waitRelay(t, service, id, "completed", "failed"); done["state"] != "completed" {
		t.Fatalf("retried relay = %#v", done)
	}
}

func TestRelayFailsWithTheLocalProvidersRefusalAndRetriesIt(t *testing.T) {
	local := &relayLocal{refusal: &sidecar.HTTPError{StatusCode: 503, Code: "missing_llm_key", Message: "尚未配置模型提供商"}}
	service, backend, _ := newRelayService(t, local)
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "final-srt"})
	if err != nil {
		t.Fatal(err)
	}
	id := snapshot["task_id"].(string)
	failed := waitRelay(t, service, id, "failed", "completed")
	if failure, _ := failed["error"].(map[string]any); failure["code"] != "missing_llm_key" || failure["message"] != "尚未配置模型提供商" {
		t.Fatalf("relay = %#v", failed)
	}
	// The recognition is already here, and the job is already gone: nothing
	// about configuring a model should cost a second cloud run.
	if submitted, deleted, _, _ := backend.snapshot(); len(submitted) != 1 || len(deleted) != 1 {
		t.Fatalf("submitted = %d, deleted = %v", len(submitted), deleted)
	}

	local.mu.Lock()
	local.refusal = nil
	local.mu.Unlock()
	if _, err := service.RetryTask(id); err != nil {
		t.Fatal(err)
	}
	if done := waitRelay(t, service, id, "completed", "failed"); done["state"] != "completed" {
		t.Fatalf("retried relay = %#v", done)
	}
	if submitted, _, _, _ := backend.snapshot(); len(submitted) != 1 {
		t.Fatalf("a local retry went back to the cloud: %d submissions", len(submitted))
	}
}

func TestRelayResubmitsWhenItsJobIsGone(t *testing.T) {
	local := &relayLocal{}
	service, backend, _ := newRelayService(t, local)
	backend.mu.Lock()
	backend.missing[fmt.Sprintf("job_%024x", 1)] = true
	backend.mu.Unlock()
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "raw-srt"})
	if err != nil {
		t.Fatal(err)
	}
	id := snapshot["task_id"].(string)
	failed := waitRelay(t, service, id, "failed", "completed")
	if failure, _ := failed["error"].(map[string]any); failure["code"] != "cloud_job_missing" {
		t.Fatalf("relay = %#v", failed)
	}
	if _, err := service.RetryTask(id); err != nil {
		t.Fatal(err)
	}
	if done := waitRelay(t, service, id, "completed", "failed"); done["state"] != "completed" {
		t.Fatalf("resubmitted relay = %#v", done)
	}
	if submitted, _, _, _ := backend.snapshot(); len(submitted) != 2 {
		t.Fatalf("submissions = %d, want a second job", len(submitted))
	}
}

func TestRelayCancelsTheCloudJob(t *testing.T) {
	local := &relayLocal{}
	service, backend, _ := newRelayService(t, local)
	service.relayPoll = time.Hour // keep it in the cloud leg
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "raw-srt"})
	if err != nil {
		t.Fatal(err)
	}
	id := snapshot["task_id"].(string)
	cancelled, err := service.CancelTask(id)
	if err != nil || cancelled["state"] != "cancelled" {
		t.Fatalf("cancel = %#v, %v", cancelled, err)
	}
	if _, _, jobs, _ := backend.snapshot(); len(jobs) != 1 {
		t.Fatalf("cancelled jobs = %v", jobs)
	}
	events := relayEventList(t, service, id)
	if events[len(events)-1].Type != "cancelled" {
		t.Fatalf("events = %#v", events)
	}
}

// A restart resumes each relay where it was, and the event log does not
// repeat what was already copied even when the record lagged the file.
func TestRelayResumesAfterARestartWithoutReplayingEvents(t *testing.T) {
	local := &relayLocal{state: "running"}
	service, _, root := newRelayService(t, local)
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "final-srt"})
	if err != nil {
		t.Fatal(err)
	}
	id := snapshot["task_id"].(string)
	deadline := time.Now().Add(5 * time.Second)
	for {
		record, _ := service.relay(id)
		if record.LocalCursor == 5 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("local leg not reached: %#v", record)
		}
		time.Sleep(5 * time.Millisecond)
	}
	service.Close()
	before := len(relayEventList(t, service, id))

	// The record on disk forgets how far the local leg was read, as a crash
	// between the event append and the record save would leave it.
	var stored relayRecord
	if !readState(filepath.Join(root, "cloud-relays", id, "relay.json"), &stored) {
		t.Fatal("relay record missing")
	}
	stored.LocalCursor = 0
	encoded, _ := json.Marshal(stored)
	if err := os.WriteFile(filepath.Join(root, "cloud-relays", id, "relay.json"), encoded, 0o600); err != nil {
		t.Fatal(err)
	}

	local.mu.Lock()
	local.state = "completed"
	local.mu.Unlock()
	reopened, err := New(root, local, fakeMedia{path: filepath.Join(root, "video.mp4")})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(reopened.Close)
	reopened.relayPoll = 5 * time.Millisecond
	if done := waitRelay(t, reopened, id, "completed", "failed"); done["state"] != "completed" {
		t.Fatalf("resumed relay = %#v", done)
	}
	if after := len(relayEventList(t, reopened, id)); after != before {
		t.Fatalf("events after restart = %d, before = %d", after, before)
	}
}

func TestRelayRefusesASourceTextAxisBeforeUploading(t *testing.T) {
	service, backend, _ := newRelayService(t, &relayLocal{})
	axis := map[string]any{"kind": "ja", "rows": []any{map[string]any{"t0": 0, "t1": 1, "ja": "x"}}}
	if _, err := service.StartTask("loc_0123456789ab", map[string]any{"axis": axis}); err == nil {
		t.Fatal("an axis started a cloud relay")
	}
	if submitted, _, _, _ := backend.snapshot(); len(submitted) != 0 {
		t.Fatalf("submissions = %d", len(submitted))
	}
}

// History rows from before the relay name ids the backend no longer keeps.
// They settle as failed instead of polling forever.
func TestLegacyCloudTasksSettle(t *testing.T) {
	service, err := New(t.TempDir(), fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	for _, id := range []string{"vid_0123456789abcdef01234567", "job_0123456789abcdef01234567"} {
		snapshot, err := service.TaskStatus(id)
		if err != nil || snapshot["state"] != "failed" || snapshot["task_id"] != id {
			t.Fatalf("%s = %#v, %v", id, snapshot, err)
		}
		if _, err := service.RetryTask(id); err == nil {
			t.Fatalf("%s retried", id)
		}
	}
}

func TestTelemetryReadsTheRelayRecord(t *testing.T) {
	root := t.TempDir()
	service, err := New(root, fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(service.Close)
	id := "rly_0123456789abcdef01234567"
	service.relays[id] = &relayRecord{
		Schema: 1, ID: id, Phase: relayPhaseLocal, State: "failed", Stage: "final-srt",
		Error: &relayError{Code: "engine_failed", Message: "x"},
	}
	diagnostic, err := telemetry.New(root, "0.4.2", nil)
	if err != nil {
		t.Fatal(err)
	}
	SetTelemetry(service, diagnostic)
	diagnostic.TrackCloud(id)
	diagnostic.CollectCloud(id)
	data, err := os.ReadFile(filepath.Join(root, "telemetry-desktop.json"))
	if err != nil {
		t.Fatal(err)
	}
	var state struct {
		Pending []telemetry.Event `json:"pending"`
	}
	if err := json.Unmarshal(data, &state); err != nil {
		t.Fatal(err)
	}
	if len(state.Pending) != 1 || state.Pending[0].Error.Stage != "final-srt" {
		t.Fatalf("failure not recorded from the relay: %s", data)
	}
}
