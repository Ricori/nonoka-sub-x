package plugins

import (
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

type fakeEngine struct {
	llmRequest  map[string]any
	llmCalls    int
	taskRequest map[string]any
	running     bool
	artifacts   map[string]any
}

func (f *fakeEngine) LLMComplete(request map[string]any) (map[string]any, error) {
	f.llmCalls++
	f.llmRequest = request
	return map[string]any{"content": "polished", "model": "fixture-model", "backend": "fixture", "fallback_used": false}, nil
}

func (f *fakeEngine) StartTask(request map[string]any) (map[string]any, error) {
	f.taskRequest = request
	return map[string]any{"task_id": "0123456789abcdef0123456789abcdef", "state": "queued"}, nil
}

func (f *fakeEngine) ListTasks() (map[string]any, error) {
	state := "completed"
	if f.running {
		state = "running"
	}
	return map[string]any{"tasks": []any{map[string]any{"snapshot": map[string]any{"state": state}}}}, nil
}

func (f *fakeEngine) TaskStatus(taskID string) (map[string]any, error) {
	return map[string]any{"task_id": taskID, "state": "completed"}, nil
}

func (f *fakeEngine) TaskEvents(taskID string, afterCursor int) (map[string]any, error) {
	return map[string]any{"task_id": taskID, "next_cursor": afterCursor}, nil
}

func (f *fakeEngine) TaskArtifacts(string) (map[string]any, error) {
	return map[string]any{"artifacts": f.artifacts}, nil
}

func (f *fakeEngine) CancelTask(taskID string) (map[string]any, error) {
	return map[string]any{"task_id": taskID, "state": "cancelled"}, nil
}

// engineService installs a plugin declaring `permissions` and attaches a fake
// engine, which is the whole fixture these tests need: the plugin's identity
// and the provider calls the capabilities are built from.
func engineService(t *testing.T, permissions string) (*Service, *fakeEngine, string) {
	t.Helper()
	root := t.TempDir()
	packageRoot := filepath.Join(t.TempDir(), "plugin")
	manifest := strings.Replace(testManifest, `"apiVersion": 1,`, `"apiVersion": 1, "permissions": [`+permissions+`],`, 1)
	mustWrite(t, filepath.Join(packageRoot, manifestName), manifest)
	mustWrite(t, filepath.Join(packageRoot, "ui", "index.html"), `<p>Hello</p>`)
	media := fakeMediaLibrary{entries: []library.Entry{{
		ID: "media-1", Title: "Example.mp4", SourcePath: filepath.Join(root, "Example.mp4"),
		Fingerprint: "fp-1", Duration: 12.5, Available: true,
	}}}
	service, err := New(root, media)
	if err != nil {
		t.Fatal(err)
	}
	engine := &fakeEngine{}
	SetEngine(service, engine)
	if _, err := service.Install(packageRoot); err != nil {
		t.Fatal(err)
	}
	return service, engine, root
}

func TestEngineCapabilitiesRequireTheirOwnPermissions(t *testing.T) {
	service, _, _ := engineService(t, `"media.list"`)
	call := LLMRequest{Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "hi"}}}
	if _, err := service.LLMComplete("dev.nonoka.hello", call); err == nil {
		t.Fatal("llm.complete must be declared")
	}
	if _, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-1", Target: "vocal"}); err == nil {
		t.Fatal("engine.run must be declared")
	}
	if _, err := service.EngineArtifacts("dev.nonoka.hello", "0123456789abcdef0123456789abcdef"); err == nil {
		t.Fatal("engine.artifacts must be declared")
	}
}

func TestLLMRequestIsBoundedBeforeItReachesTheEngine(t *testing.T) {
	service, engine, _ := engineService(t, `"llm.complete"`)
	oversized := strings.Repeat("x", maxLLMPromptBytes+1)
	for name, request := range map[string]LLMRequest{
		"role":    {Role: "audio_multimodal", Messages: []LLMMessage{{Role: "user", Content: "hi"}}},
		"empty":   {Role: "lightweight"},
		"speaker": {Role: "lightweight", Messages: []LLMMessage{{Role: "tool", Content: "hi"}}},
		"size":    {Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: oversized}}},
		"tokens":  {Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "hi"}}, MaxTokens: maxLLMOutputTokens + 1},
		"blank":   {Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "   "}}},
		"heat":    {Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "hi"}}, Temperature: 9},
	} {
		if _, err := service.LLMComplete("dev.nonoka.hello", request); err == nil {
			t.Fatalf("%s should have been refused", name)
		}
	}
	if engine.llmCalls != 0 {
		t.Fatalf("a refused request still reached the engine %d times", engine.llmCalls)
	}
	answer, err := service.LLMComplete("dev.nonoka.hello", LLMRequest{
		Role:     "general_capable",
		Messages: []LLMMessage{{Role: "system", Content: "be brief"}, {Role: "user", Content: "polish this"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if answer.Content != "polished" || answer.Model != "fixture-model" {
		t.Fatalf("unexpected answer: %#v", answer)
	}
	// Omitted knobs stay omitted, so the engine's own defaults apply rather
	// than a zero value the plugin never chose.
	if _, present := engine.llmRequest["max_tokens"]; present {
		t.Fatalf("an unset max_tokens should not be sent: %#v", engine.llmRequest)
	}
}

func TestLLMCallsAreRateLimitedPerPlugin(t *testing.T) {
	service, engine, _ := engineService(t, `"llm.complete"`)
	call := LLMRequest{Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "hi"}}}
	for i := 0; i < llmCallsPerWindow; i++ {
		if _, err := service.LLMComplete("dev.nonoka.hello", call); err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
	}
	if _, err := service.LLMComplete("dev.nonoka.hello", call); err == nil {
		t.Fatal("the rolling budget should refuse the call after it is spent")
	}
	if engine.llmCalls != llmCallsPerWindow {
		t.Fatalf("engine saw %d calls, want %d", engine.llmCalls, llmCallsPerWindow)
	}
}

func TestStartEngineTaskOwnsTheSwitchesAPluginMustNotSet(t *testing.T) {
	service, engine, _ := engineService(t, `"engine.run"`)
	snapshot, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{
		MediaID:    "media-1",
		Target:     "stable",
		Correction: EngineCorrection{Media: "text"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if snapshot["task_id"] != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("unexpected snapshot: %#v", snapshot)
	}
	request := engine.taskRequest
	// The two that would have side effects the user never asked for.
	if request["knowledge"] != "none" || request["projection"] != "none" {
		t.Fatalf("a plugin run must neither update knowledge nor replace the document: %#v", request)
	}
	if request["separate"] != true {
		t.Fatalf("separation is on unless the plugin opts out: %#v", request)
	}
	if request["device"] != "cuda" || request["vocal_profile"] != "quality" {
		t.Fatalf("the host owns device and profile: %#v", request)
	}
	source, ok := request["source"].(map[string]any)
	if !ok || source["video_id"] != "media-1" || source["fingerprint"] != "fp-1" {
		t.Fatalf("the host resolves the source from its own library: %#v", request["source"])
	}
	if _, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-1", Target: "subtitles"}); err == nil {
		t.Fatal("an unknown target should be refused")
	}
	if _, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-2", Target: "vocal"}); err == nil {
		t.Fatal("a media id outside the library should not start a run")
	}
}

func TestOnlyOneEngineTaskRunsAtATime(t *testing.T) {
	service, engine, _ := engineService(t, `"engine.run"`)
	engine.running = true
	if _, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-1", Target: "vocal"}); err == nil {
		t.Fatal("a second run should be refused while one is in flight")
	}
}

func TestATaskIsVisibleOnlyToThePluginThatStartedIt(t *testing.T) {
	service, _, _ := engineService(t, `"engine.run", "engine.artifacts"`)
	started, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-1", Target: "vocal"})
	if err != nil {
		t.Fatal(err)
	}
	taskID := started["task_id"].(string)
	if _, err := service.EngineTaskStatus("dev.nonoka.hello", taskID); err != nil {
		t.Fatal(err)
	}
	// A task id this plugin never started -- another plugin's, or one read off
	// the task list page -- is not its to ask about.
	if _, err := service.EngineTaskStatus("dev.nonoka.hello", "ffffffffffffffffffffffffffffffff"); err == nil {
		t.Fatal("a task this plugin did not start should not resolve")
	}
	if _, err := service.CancelEngineTask("dev.nonoka.hello", "ffffffffffffffffffffffffffffffff"); err == nil {
		t.Fatal("a task this plugin did not start should not be cancellable")
	}
}

func TestArtifactsAreReadAsTextAndOnlyFromTheTaskDirectory(t *testing.T) {
	service, engine, root := engineService(t, `"engine.run", "engine.artifacts"`)
	started, err := service.StartEngineTask("dev.nonoka.hello", EngineTaskRequest{MediaID: "media-1", Target: "stable"})
	if err != nil {
		t.Fatal(err)
	}
	taskID := started["task_id"].(string)
	workspace := filepath.Join(root, "tasks", taskID, "workspace")
	stable := filepath.Join(workspace, "Example-stable.json")
	vocal := filepath.Join(workspace, "Example-vocal.ogg")
	mustWrite(t, stable, `{"segments":[]}`)
	mustWrite(t, vocal, "OggS")
	outside := filepath.Join(root, "secrets.json")
	mustWrite(t, outside, `{"key":"secret"}`)
	engine.artifacts = map[string]any{
		"stable_json": map[string]any{"uri": fileURI(stable), "bytes": float64(15)},
		"vocal_audio": map[string]any{"uri": fileURI(vocal), "bytes": float64(4)},
		"elsewhere":   map[string]any{"uri": fileURI(outside), "bytes": float64(16)},
	}

	listed, err := service.EngineArtifacts("dev.nonoka.hello", taskID)
	if err != nil {
		t.Fatal(err)
	}
	readable := map[string]bool{}
	for _, item := range listed {
		readable[item.Name] = item.Readable
	}
	if !readable["stable_json"] {
		t.Fatalf("a JSON artifact should be readable: %#v", listed)
	}
	if readable["vocal_audio"] {
		t.Fatalf("an audio artifact reaches disk through the dialog, not the page: %#v", listed)
	}
	if listed[0].Name != "vocal_audio" {
		t.Fatalf("artifacts should list in pipeline order: %#v", listed)
	}

	content, err := service.ReadArtifact("dev.nonoka.hello", taskID, "stable_json")
	if err != nil {
		t.Fatal(err)
	}
	if content != `{"segments":[]}` {
		t.Fatalf("unexpected artifact content: %q", content)
	}
	if _, err := service.ReadArtifact("dev.nonoka.hello", taskID, "vocal_audio"); err == nil {
		t.Fatal("a binary artifact should not be returned to the page")
	}
	// The manifest is the engine's, but a path outside the task directory
	// would turn "read my artifact" into "read any file".
	if _, err := service.ReadArtifact("dev.nonoka.hello", taskID, "elsewhere"); err == nil {
		t.Fatal("an artifact outside the task directory should be refused")
	}
	if _, err := service.ReadArtifact("dev.nonoka.hello", taskID, "missing"); err == nil {
		t.Fatal("an artifact the run never produced should be refused")
	}
}

func TestEngineCapabilitiesFailClearlyWithoutAProvider(t *testing.T) {
	service, _, _ := engineService(t, `"llm.complete"`)
	SetEngine(service, nil)
	_, err := service.LLMComplete("dev.nonoka.hello", LLMRequest{
		Role: "lightweight", Messages: []LLMMessage{{Role: "user", Content: "hi"}},
	})
	if err == nil || !strings.Contains(err.Error(), "local provider") {
		t.Fatalf("expected an unavailable-provider error, got %v", err)
	}
}

func TestArtifactPathRejectsNonLocalURIs(t *testing.T) {
	service, _, _ := engineService(t, `"engine.artifacts"`)
	for _, uri := range []string{"https://example.com/a.json", "file://server/share/a.json", "not a uri"} {
		if _, err := service.artifactPath("0123456789abcdef0123456789abcdef", uri); err == nil {
			t.Fatalf("%q should not resolve to a local path", uri)
		}
	}
}

func TestReadEngineTasksSurvivesACorruptList(t *testing.T) {
	path := filepath.Join(t.TempDir(), engineTasksFile)
	mustWrite(t, path, "{not json")
	tasks, err := readEngineTasks(path)
	if err != nil || len(tasks) != 0 {
		t.Fatalf("a corrupt list should cost the plugin its history, not its next run: %v %#v", err, tasks)
	}
	if _, err := readEngineTasks(filepath.Join(t.TempDir(), "absent.json")); err != nil && !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("a missing list is an empty one: %v", err)
	}
}

func fileURI(path string) string {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return ""
	}
	slashed := filepath.ToSlash(absolute)
	if !strings.HasPrefix(slashed, "/") {
		slashed = "/" + slashed
	}
	return (&url.URL{Scheme: "file", Path: slashed}).String()
}
