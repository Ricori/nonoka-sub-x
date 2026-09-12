package provider

import (
	"context"
	"reflect"
	"testing"

	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
)

const taskID = "0123456789abcdef0123456789abcdef"

type call struct {
	method   string
	endpoint string
	body     any
}

type fakeCaller struct {
	calls []call
}

type fakePythonBootstrap struct {
	installed bool
}

func (f *fakePythonBootstrap) Status() map[string]any {
	return map[string]any{"state": "missing"}
}

func (f *fakePythonBootstrap) Install() (map[string]any, error) {
	f.installed = true
	return map[string]any{"state": "running"}, nil
}

func (f *fakeCaller) Snapshot() sidecar.Snapshot {
	return sidecar.Snapshot{Running: true, BaseURL: "http://127.0.0.1:1234", PID: 42}
}

func (f *fakeCaller) DoJSON(_ context.Context, method, endpoint string, body, response any) error {
	f.calls = append(f.calls, call{method: method, endpoint: endpoint, body: body})
	if target, ok := response.(*map[string]any); ok {
		*target = map[string]any{"ok": true}
	}
	return nil
}

func TestServiceMapsFixedProviderEndpoints(t *testing.T) {
	transport := &fakeCaller{}
	service, err := New(transport)
	if err != nil {
		t.Fatal(err)
	}
	request := map[string]any{"schema": 1, "provider": "local"}
	_, _ = service.Capabilities()
	_, _ = service.RuntimeProvisionStatus()
	_, _ = service.InstallRuntime("media")
	_, _ = service.InstallRuntime("yt-dlp")
	_, _ = service.CancelRuntimeInstall()
	_, _ = service.RemoveRuntime()
	_, _ = service.RemoveRuntimeGroup("video-tools")
	_, _ = service.Settings()
	_, _ = service.SaveKeys(map[string]any{"GEMINI_FREE": "secret"})
	_, _ = service.ListTasks()
	_, _ = service.StartTask(request)
	_, _ = service.TaskStatus(taskID)
	_, _ = service.TaskEvents(taskID, 7)
	_, _ = service.TaskArtifacts(taskID)
	_, _ = service.CancelTask(taskID)
	_, _ = service.RetryTask(taskID)
	_, _ = service.ResumeTask(taskID)

	want := []call{
		{method: "GET", endpoint: "/v1/capabilities"},
		{method: "GET", endpoint: "/v1/runtime/provision"},
		{method: "POST", endpoint: "/v1/runtime/provision", body: map[string]any{"target": "media"}},
		{method: "POST", endpoint: "/v1/runtime/provision", body: map[string]any{"target": "yt-dlp"}},
		{method: "POST", endpoint: "/v1/runtime/provision/cancel", body: map[string]any{}},
		{method: "DELETE", endpoint: "/v1/runtime/provision"},
		{method: "DELETE", endpoint: "/v1/runtime/provision/group", body: map[string]any{"target": "video-tools"}},
		{method: "GET", endpoint: "/v1/settings"},
		{method: "PUT", endpoint: "/v1/settings/keys", body: map[string]any{"keys": map[string]any{"GEMINI_FREE": "secret"}}},
		{method: "GET", endpoint: "/v1/tasks?limit=100"},
		{method: "POST", endpoint: "/v1/tasks", body: request},
		{method: "GET", endpoint: "/v1/tasks/" + taskID},
		{method: "GET", endpoint: "/v1/tasks/" + taskID + "/events?after=7"},
		{method: "GET", endpoint: "/v1/tasks/" + taskID + "/artifacts"},
		{method: "POST", endpoint: "/v1/tasks/" + taskID + "/cancel", body: map[string]any{}},
		{method: "POST", endpoint: "/v1/tasks/" + taskID + "/retry", body: map[string]any{}},
		{method: "POST", endpoint: "/v1/tasks/" + taskID + "/resume", body: map[string]any{}},
	}
	if !reflect.DeepEqual(transport.calls, want) {
		t.Fatalf("calls = %#v\nwant  = %#v", transport.calls, want)
	}
	status := service.SidecarStatus()
	if !status.Running || status.BaseURL == "" {
		t.Fatalf("status = %#v", status)
	}
}

func TestServiceRejectsUnknownRuntimeTarget(t *testing.T) {
	transport := &fakeCaller{}
	service, _ := New(transport)
	if _, err := service.InstallRuntime("unknown"); err == nil {
		t.Fatal("unknown runtime target was accepted")
	}
	if len(transport.calls) != 0 {
		t.Fatalf("invalid runtime target reached transport: %#v", transport.calls)
	}
}

func TestServiceRejectsUnknownRemovableRuntimeGroup(t *testing.T) {
	transport := &fakeCaller{}
	service, _ := New(transport)
	if _, err := service.RemoveRuntimeGroup("runtime"); err == nil {
		t.Fatal("required runtime group was accepted for removal")
	}
	if len(transport.calls) != 0 {
		t.Fatalf("invalid removal target reached transport: %#v", transport.calls)
	}
}

func TestServiceAcceptsOptionalRuntimeTargets(t *testing.T) {
	transport := &fakeCaller{}
	service, _ := New(transport)
	for _, target := range []string{"yt-dlp", "tokcount", "aria2c", "node", "pot-provider", "video-tools", "optional-tools"} {
		if _, err := service.InstallRuntime(target); err != nil {
			t.Fatalf("optional target %q was rejected: %v", target, err)
		}
	}
}

func TestServiceExposesNativePythonBootstrapWithoutCallingSidecar(t *testing.T) {
	transport := &fakeCaller{}
	bootstrap := &fakePythonBootstrap{}
	service, err := New(transport, bootstrap)
	if err != nil {
		t.Fatal(err)
	}
	if status := service.PythonBootstrapStatus(); status["state"] != "missing" {
		t.Fatalf("status = %#v", status)
	}
	if status, err := service.InstallPython(); err != nil || status["state"] != "running" || !bootstrap.installed {
		t.Fatalf("install status = %#v, err = %v", status, err)
	}
	if len(transport.calls) != 0 {
		t.Fatalf("native bootstrap reached sidecar: %#v", transport.calls)
	}
}

func TestServiceRejectsPathInjectionBeforeTransport(t *testing.T) {
	transport := &fakeCaller{}
	service, _ := New(transport)
	for _, value := range []string{"", "../capabilities", taskID + "/cancel", "ABCDEF0123456789ABCDEF0123456789"} {
		if _, err := service.TaskStatus(value); err == nil {
			t.Fatalf("expected task id %q to fail", value)
		}
	}
	if _, err := service.TaskEvents(taskID, -1); err == nil {
		t.Fatal("expected negative cursor to fail")
	}
	if len(transport.calls) != 0 {
		t.Fatalf("invalid input reached transport: %#v", transport.calls)
	}
}

func TestServiceMapsAxisEndpoints(t *testing.T) {
	transport := &fakeCaller{}
	service, err := New(transport)
	if err != nil {
		t.Fatal(err)
	}
	axis := map[string]any{"kind": "empty", "rows": []any{}}
	payload := map[string]any{"video_id": "loc_1", "axis": axis}
	_, _ = service.SetDocumentAxis("loc_1", axis)
	_, _ = service.SetDocumentAxis("loc_1", nil)
	_, _ = service.ImportDocument(payload)

	want := []call{
		{method: "PUT", endpoint: "/v1/documents/loc_1/axis", body: map[string]any{"axis": axis}},
		// Clearing is a write of nothing, not a skipped call: a stale axis left
		// behind would re-seat the next run on this video.
		{method: "PUT", endpoint: "/v1/documents/loc_1/axis", body: map[string]any{"axis": map[string]any(nil)}},
		{method: "POST", endpoint: "/v1/documents/import", body: payload},
	}
	if !reflect.DeepEqual(transport.calls, want) {
		t.Fatalf("calls = %#v\nwant  = %#v", transport.calls, want)
	}
}

func TestServiceRejectsAxisPathInjectionBeforeTransport(t *testing.T) {
	transport := &fakeCaller{}
	service, _ := New(transport)
	for _, value := range []string{"", "../capabilities", "loc_1/axis"} {
		if _, err := service.SetDocumentAxis(value, nil); err == nil {
			t.Fatalf("expected document id %q to fail", value)
		}
	}
	if _, err := service.ImportDocument(nil); err == nil {
		t.Fatal("expected an empty import payload to fail")
	}
	if len(transport.calls) != 0 {
		t.Fatalf("invalid input reached transport: %#v", transport.calls)
	}
}
