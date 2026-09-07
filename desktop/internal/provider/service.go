// Package provider exposes the versioned Local ExecutionProvider contract to
// Wails without exposing sidecar transport details or its bearer token.
package provider

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
)

var validTaskID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// runtimeTargets is the closed set InstallRuntime forwards to the sidecar: the
// bulk targets plus OPTIONAL_TOOLS from nonoka_x/provision.py. The rejection
// message is derived from it rather than written out again, because the two
// drifted apart once already -- aria2c was added to the Python side and this
// list still refused it, naming the old tools in the error.
var runtimeTargets = []string{"media", "runtime", "models", "all", "git", "yt-dlp", "tokcount", "aria2c", "node", "pot-provider", "video-tools", "optional-tools"}
var removableRuntimeGroups = []string{"video-tools", "optional-tools"}

type caller interface {
	DoJSON(context.Context, string, string, any, any) error
	Snapshot() sidecar.Snapshot
}

type pythonBootstrap interface {
	Status() map[string]any
	Install() (map[string]any, error)
}

type Service struct {
	provider        caller
	pythonBootstrap pythonBootstrap
}

func New(local caller, bootstrap ...pythonBootstrap) (*Service, error) {
	if local == nil {
		return nil, errors.New("local provider is required")
	}
	service := &Service{provider: local}
	if len(bootstrap) > 0 {
		service.pythonBootstrap = bootstrap[0]
	}
	return service, nil
}

func (s *Service) SidecarStatus() sidecar.Snapshot {
	return s.provider.Snapshot()
}

func (s *Service) PythonBootstrapStatus() map[string]any {
	if s.pythonBootstrap == nil {
		return map[string]any{"schema": 1, "supported": false, "state": "unavailable", "message": "Python bootstrap is unavailable"}
	}
	return s.pythonBootstrap.Status()
}

func (s *Service) InstallPython() (map[string]any, error) {
	if s.pythonBootstrap == nil {
		return nil, errors.New("Python bootstrap is unavailable")
	}
	return s.pythonBootstrap.Install()
}

func (s *Service) Capabilities() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodGet, "/v1/capabilities", nil, &result)
	return result, err
}

func (s *Service) RuntimeProvisionStatus() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodGet, "/v1/runtime/provision", nil, &result)
	return result, err
}

func (s *Service) InstallRuntime(target string) (map[string]any, error) {
	if !slices.Contains(runtimeTargets, target) {
		return nil, fmt.Errorf("runtime target must be one of: %s", strings.Join(runtimeTargets, ", "))
	}
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/runtime/provision", map[string]any{"target": target}, &result)
	return result, err
}

// RemoveRuntime removes replaceable managed environments and caches. The
// sidecar preserves tasks, subtitle documents, and user settings.
func (s *Service) RemoveRuntime() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodDelete, "/v1/runtime/provision", nil, &result)
	return result, err
}

// RemoveRuntimeGroup removes one managed optional-tool group without touching
// the required runtime, models, tasks, subtitles, or user settings.
func (s *Service) RemoveRuntimeGroup(target string) (map[string]any, error) {
	if !slices.Contains(removableRuntimeGroups, target) {
		return nil, fmt.Errorf("removable runtime group must be one of: %s", strings.Join(removableRuntimeGroups, ", "))
	}
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodDelete, "/v1/runtime/provision/group", map[string]any{"target": target}, &result)
	return result, err
}

// CancelRuntimeInstall stops the running provisioning job. The sidecar cancels
// cooperatively and keeps whatever it already downloaded, so this is a stop
// rather than a rollback.
func (s *Service) CancelRuntimeInstall() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/runtime/provision/cancel", map[string]any{}, &result)
	return result, err
}

func (s *Service) Settings() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodGet, "/v1/settings", nil, &result)
	return result, err
}

func (s *Service) SaveKeys(keys map[string]any) (map[string]any, error) {
	if keys == nil {
		return nil, errors.New("keys are required")
	}
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodPut, "/v1/settings/keys", map[string]any{"keys": keys}, &result)
	return result, err
}

// llmCallTimeout outlives the sidecar's own LLM_CALL_TIMEOUT_SEC on purpose.
// The sidecar is the side that knows a call went nowhere and can say so --
// naming the model, the endpoint's own words -- so it must be the one whose
// clock runs out first; this deadline only exists so a sidecar that stopped
// answering at all cannot hold the window forever.
//
// The default 30s that covers every other provider call is far too short here:
// the first call also spawns the LLM worker and imports the engine, and the
// call itself waits on a model, or on a local agent CLI's whole session.
const llmCallTimeout = 330 * time.Second

// LLMComplete runs one routed LLM call on the user's configured models. The
// sidecar owns the whole request shape -- roles, prompt size, output budget --
// so this is a forwarder rather than a second validator that could disagree
// with the first.
func (s *Service) LLMComplete(request map[string]any) (map[string]any, error) {
	if request == nil {
		return nil, errors.New("LLM request is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), llmCallTimeout)
	defer cancel()
	var result map[string]any
	err := s.provider.DoJSON(ctx, http.MethodPost, "/v1/llm/complete", request, &result)
	return result, err
}

func (s *Service) Document(videoID string) (map[string]any, error) {
	endpoint, err := documentEndpoint(videoID, "")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) DocumentPeaks(videoID string) (map[string]any, error) {
	endpoint, err := documentEndpoint(videoID, "peaks")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) SaveDocument(videoID string, document map[string]any) (map[string]any, error) {
	if document == nil {
		return nil, errors.New("document is required")
	}
	endpoint, err := documentEndpoint(videoID, "")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodPut, endpoint, document, &result)
	return result, err
}

// SetDocumentAxis records the imported subtitle axis a run on this video must
// land on, or clears it when axis is nil. It is stored beside the document
// rather than carried in the TaskRequest because the projection consumes it,
// and a cloud task's artifacts are projected locally without its request ever
// reaching the sidecar.
func (s *Service) SetDocumentAxis(videoID string, axis map[string]any) (map[string]any, error) {
	endpoint, err := documentEndpoint(videoID, "axis")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodPut, endpoint, map[string]any{"axis": axis}, &result)
	return result, err
}

// ImportDocument turns a finished subtitle axis into an editable document
// without running a task: a bilingual or translated file has nothing left for
// the engine to compute.
func (s *Service) ImportDocument(payload map[string]any) (map[string]any, error) {
	if payload == nil {
		return nil, errors.New("import payload is required")
	}
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/documents/import", payload, &result)
	return result, err
}

func (s *Service) StartTask(request map[string]any) (map[string]any, error) {
	if request == nil {
		return nil, errors.New("task request is required")
	}
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/tasks", request, &result)
	return result, err
}

func (s *Service) ListTasks() (map[string]any, error) {
	var result map[string]any
	err := s.provider.DoJSON(context.Background(), http.MethodGet, "/v1/tasks?limit=100", nil, &result)
	return result, err
}

func (s *Service) TaskStatus(taskID string) (map[string]any, error) {
	endpoint, err := taskEndpoint(taskID, "")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) TaskEvents(taskID string, afterCursor int) (map[string]any, error) {
	if afterCursor < 0 {
		return nil, errors.New("event cursor must not be negative")
	}
	endpoint, err := taskEndpoint(taskID, "events")
	if err != nil {
		return nil, err
	}
	endpoint += "?after=" + strconv.Itoa(afterCursor)
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) TaskArtifacts(taskID string) (map[string]any, error) {
	endpoint, err := taskEndpoint(taskID, "artifacts")
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) CancelTask(taskID string) (map[string]any, error) {
	return s.taskAction(taskID, "cancel")
}

func (s *Service) RetryTask(taskID string) (map[string]any, error) {
	return s.taskAction(taskID, "retry")
}

func (s *Service) ResumeTask(taskID string) (map[string]any, error) {
	return s.taskAction(taskID, "resume")
}

func (s *Service) taskAction(taskID, action string) (map[string]any, error) {
	endpoint, err := taskEndpoint(taskID, action)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = s.provider.DoJSON(context.Background(), http.MethodPost, endpoint, map[string]any{}, &result)
	return result, err
}

func taskEndpoint(taskID, suffix string) (string, error) {
	if !validTaskID.MatchString(taskID) {
		return "", fmt.Errorf("invalid task id %q", taskID)
	}
	endpoint := "/v1/tasks/" + taskID
	if suffix != "" {
		endpoint += "/" + suffix
	}
	return endpoint, nil
}

func documentEndpoint(videoID, suffix string) (string, error) {
	if len(videoID) == 0 || len(videoID) > 80 {
		return "", errors.New("invalid document id")
	}
	for _, character := range videoID {
		if (character < 'a' || character > 'z') && (character < 'A' || character > 'Z') && (character < '0' || character > '9') && character != '_' && character != '-' {
			return "", errors.New("invalid document id")
		}
	}
	endpoint := "/v1/documents/" + videoID
	if suffix != "" {
		endpoint += "/" + suffix
	}
	return endpoint, nil
}
