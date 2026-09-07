package plugins

// The FineSub engine, reached from a plugin page. Three capabilities, each
// with its own permission, because each carries a different cost:
//
//   - llm.complete spends the user's model quota, so the host bounds prompt
//     size, output budget, concurrency and call rate. The plugin never sees a
//     key, an endpoint, or the engine's own prompt templates.
//   - engine.run occupies the GPU for minutes. The host owns the source
//     resolution, the device, and the two switches a plugin has no business
//     setting: a run started from here never writes the user's knowledge base
//     and never replaces the subtitle document they are editing.
//   - engine.artifacts reads what a run produced. Text comes back as text;
//     everything else can only be written through the save dialog, so the
//     rule that a plugin never learns a local path survives here too.
//
// A plugin sees only the tasks it started. The ownership list is on disk
// rather than in the page, because the page is an iframe that the user
// destroys by navigating away.

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

const (
	// Mirrors the sidecar's own caps (`nonoka_x.local_provider`). Checked here
	// as well so a refusal is immediate and says which limit was crossed,
	// rather than arriving as an HTTP error from two processes away.
	maxLLMPromptBytes  = 200_000
	maxLLMOutputTokens = 32_768
	maxLLMMessages     = 64
	// A rolling budget, not a queue: a plugin that has spent it is told so and
	// decides what to do, because the alternative is a page that appears to
	// hang while the user's quota drains behind it.
	llmCallWindow      = 10 * time.Minute
	llmCallsPerWindow  = 60
	maxArtifactBytes   = 8 << 20
	maxOwnedTasks      = 50
	maxCorrectionNotes = 4_000
	engineTasksFile    = "engine-tasks.json"
)

// engineTargets is the engine's pipeline, in order. Restated rather than
// derived because the desktop is not the process that runs it; the sidecar
// refuses anything it does not know, so this list is the earlier, clearer
// refusal, not the authority.
var engineTargets = []string{"vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt"}

// The roles a plugin may ask for: the text-only ones. The multimodal roles
// exist to put a clip in front of a model, and this surface has no way to
// pass one.
var llmRoles = []string{"lightweight", "general_capable"}

// Artifacts a page can read as text. Everything else -- the vocal track, the
// VAD energy array -- is bytes the page has no use for and would only be a
// way to move a large file through postMessage; those go to the save dialog.
var textArtifactExtensions = []string{".json", ".srt", ".csv", ".md", ".txt"}

// A BCP 47-ish tag, which is what the engine's `--language` takes. Narrow
// enough that nothing else can travel in that field.
var languagePattern = regexp.MustCompile(`^[a-z]{2,3}(-[A-Za-z]{2,4})?$`)

// engineHost names exactly the Local Provider calls these capabilities are
// built from. Written as an interface for the same reason documentStore is:
// so the plugin service cannot reach settings, the runtime installer or the
// document store through the same dependency.
type engineHost interface {
	LLMComplete(request map[string]any) (map[string]any, error)
	StartTask(request map[string]any) (map[string]any, error)
	ListTasks() (map[string]any, error)
	TaskStatus(taskID string) (map[string]any, error)
	TaskEvents(taskID string, afterCursor int) (map[string]any, error)
	TaskArtifacts(taskID string) (map[string]any, error)
	CancelTask(taskID string) (map[string]any, error)
}

// SetEngine attaches the local provider's engine surface. Separate from
// SetDocuments so the two grants stay legible at the call site, even though
// today one object satisfies both.
func SetEngine(s *Service, engine engineHost) {
	s.mu.Lock()
	s.engine = engine
	s.mu.Unlock()
}

// LLMMessage is one turn of a plugin-authored prompt.
type LLMMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// LLMRequest is a plugin's whole say over a call. There is deliberately no
// model field: which model answers is the user's configuration, and a plugin
// that could name one could also route around the user's choice.
type LLMRequest struct {
	Role     string       `json:"role"`
	Messages []LLMMessage `json:"messages"`
	// 0 leaves the sidecar's default.
	MaxTokens   int     `json:"maxTokens"`
	Temperature float64 `json:"temperature"`
}

// LLMAnswer reports the answer and which model gave it. The endpoint chain,
// the key that paid for it and the routing trace stay inside the engine.
type LLMAnswer struct {
	Content      string `json:"content"`
	Model        string `json:"model"`
	Backend      string `json:"backend"`
	FallbackUsed bool   `json:"fallbackUsed"`
}

// EngineCorrection is the LLM stages' knobs. Empty fields are left out of the
// request so the engine's own defaults apply, rather than being overwritten
// with a value the plugin never chose.
type EngineCorrection struct {
	Media      string `json:"media"`
	Retrieval  string `json:"retrieval"`
	Difficulty string `json:"difficulty"`
	Fast       string `json:"fast"`
	ExtraInfo  string `json:"extraInfo"`
	ExtraStyle string `json:"extraStyle"`
}

// EngineTaskRequest is what a plugin may say about a run. The source, the
// device, the GPU tier and the vocal profile are the host's -- a plugin names
// media by the id media.list gave it, exactly as everywhere else.
type EngineTaskRequest struct {
	MediaID string `json:"mediaId"`
	Target  string `json:"target"`
	// Empty means Japanese, the engine's own default for this pipeline.
	Language string `json:"language"`
	// Opt-in rather than opt-out: separation is on for every input that is not
	// already a clean vocal track, and a zero value that silently turned it off
	// would cost the whole recognition quality without saying anything.
	SkipSeparation bool             `json:"skipSeparation"`
	Correction     EngineCorrection `json:"correction"`
}

// EngineArtifact is one file a run produced. No path: `readable` says whether
// ReadArtifact will return its content, and everything else reaches disk only
// through SaveArtifact's dialog.
type EngineArtifact struct {
	Name     string `json:"name"`
	Bytes    int64  `json:"bytes"`
	Readable bool   `json:"readable"`
}

// LLMComplete runs one call on the user's configured models.
func (s *Service) LLMComplete(pluginID string, request LLMRequest) (LLMAnswer, error) {
	if err := s.requirePermission(pluginID, "llm.complete"); err != nil {
		return LLMAnswer{}, err
	}
	payload, err := llmPayload(request)
	if err != nil {
		return LLMAnswer{}, err
	}
	engine, err := s.engineTarget()
	if err != nil {
		return LLMAnswer{}, err
	}
	release, err := s.claimLLMCall(pluginID)
	if err != nil {
		return LLMAnswer{}, err
	}
	defer release()
	result, err := engine.LLMComplete(payload)
	if err != nil {
		return LLMAnswer{}, err
	}
	return LLMAnswer{
		Content:      stringField(result, "content"),
		Model:        stringField(result, "model"),
		Backend:      stringField(result, "backend"),
		FallbackUsed: result["fallback_used"] == true,
	}, nil
}

// StartEngineTask runs the pipeline up to one stage on a library media entry.
func (s *Service) StartEngineTask(pluginID string, request EngineTaskRequest) (map[string]any, error) {
	if err := s.requirePermission(pluginID, "engine.run"); err != nil {
		return nil, err
	}
	engine, err := s.engineTarget()
	if err != nil {
		return nil, err
	}
	s.mu.RLock()
	media := s.media
	s.mu.RUnlock()
	if media == nil {
		return nil, errors.New("media library is unavailable")
	}
	entry, err := media.Get(request.MediaID)
	if err != nil {
		return nil, err
	}
	if !entry.Available {
		return nil, errors.New("local media is unavailable")
	}
	payload, err := engineTaskPayload(request, entry)
	if err != nil {
		return nil, err
	}
	// One engine run at a time. The pipeline holds the GPU for minutes, and a
	// second run started behind the user's own would not fail -- it would make
	// both slower, on a machine the user is watching.
	running, err := s.engineTaskRunning(engine)
	if err != nil {
		return nil, err
	}
	if running {
		return nil, errors.New("another engine task is already running")
	}
	snapshot, err := engine.StartTask(payload)
	if err != nil {
		return nil, err
	}
	taskID := stringField(snapshot, "task_id")
	if taskID == "" {
		return nil, errors.New("the engine did not report a task id")
	}
	if err := s.recordEngineTask(pluginID, taskID); err != nil {
		return nil, err
	}
	return snapshot, nil
}

// EngineTaskStatus, EngineTaskEvents and CancelEngineTask are the page's
// polling surface, the same one the app's own pipeline view uses.
func (s *Service) EngineTaskStatus(pluginID, taskID string) (map[string]any, error) {
	engine, err := s.ownedTask(pluginID, taskID)
	if err != nil {
		return nil, err
	}
	return engine.TaskStatus(taskID)
}

func (s *Service) EngineTaskEvents(pluginID, taskID string, afterCursor int) (map[string]any, error) {
	engine, err := s.ownedTask(pluginID, taskID)
	if err != nil {
		return nil, err
	}
	return engine.TaskEvents(taskID, afterCursor)
}

func (s *Service) CancelEngineTask(pluginID, taskID string) (map[string]any, error) {
	engine, err := s.ownedTask(pluginID, taskID)
	if err != nil {
		return nil, err
	}
	return engine.CancelTask(taskID)
}

// EngineArtifacts lists what a run produced, by name and size.
func (s *Service) EngineArtifacts(pluginID, taskID string) ([]EngineArtifact, error) {
	if err := s.requirePermission(pluginID, "engine.artifacts"); err != nil {
		return nil, err
	}
	entries, err := s.artifactManifest(pluginID, taskID)
	if err != nil {
		return nil, err
	}
	listed := make([]EngineArtifact, 0, len(entries))
	for name, item := range entries {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		path, pathErr := s.artifactPath(taskID, stringField(record, "uri"))
		size, _ := record["bytes"].(float64)
		listed = append(listed, EngineArtifact{
			Name:     name,
			Bytes:    int64(size),
			Readable: pathErr == nil && readableArtifact(path),
		})
	}
	sortArtifacts(listed)
	return listed, nil
}

// ReadArtifact returns a text artifact's content.
func (s *Service) ReadArtifact(pluginID, taskID, name string) (string, error) {
	if err := s.requirePermission(pluginID, "engine.artifacts"); err != nil {
		return "", err
	}
	path, err := s.resolveArtifact(pluginID, taskID, name)
	if err != nil {
		return "", err
	}
	if !readableArtifact(path) {
		return "", fmt.Errorf("artifact %s is not text; save it to a file instead", name)
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if info.Size() > maxArtifactBytes {
		return "", fmt.Errorf("artifact %s exceeds %d MB", name, maxArtifactBytes>>20)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(content), nil
}

// SaveArtifact writes an artifact wherever the save dialog says. This is the
// only way a binary artifact leaves the host, and the plugin's file name is a
// suggestion: the directory is the user's, as with every other export.
func (s *Service) SaveArtifact(pluginID, taskID, name, fileName string) (ExportedArtifact, error) {
	if err := s.requirePermission(pluginID, "engine.artifacts"); err != nil {
		return ExportedArtifact{}, err
	}
	path, err := s.resolveArtifact(pluginID, taskID, name)
	if err != nil {
		return ExportedArtifact{}, err
	}
	s.mu.RLock()
	app, home := s.app, s.home
	s.mu.RUnlock()
	if app == nil || home == nil {
		return ExportedArtifact{}, errors.New("application media services are unavailable")
	}
	// Every artifact the pipeline names has an extension; an unnamed one still
	// has to reach a dialog that offers something, so it lands as .bin rather
	// than as a filter of "*" and a file the user cannot open.
	extension := strings.ToLower(filepath.Ext(path))
	if extension == "" {
		extension = ".bin"
	}
	output, err := app.Dialog.SaveFile().
		SetFilename(artifactFileName(fileName, name, extension)).
		AddFilter(strings.TrimPrefix(extension, ".")+" 文件", "*"+extension).
		AttachToWindow(home).
		PromptForSingleSelection()
	if err != nil && !dialogCancelled(err) {
		return ExportedArtifact{}, err
	}
	if output == "" {
		return ExportedArtifact{}, nil
	}
	if !strings.EqualFold(filepath.Ext(output), extension) {
		output += extension
	}
	output, err = filepath.Abs(output)
	if err != nil {
		return ExportedArtifact{}, err
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return ExportedArtifact{}, err
	}
	if err := os.WriteFile(output, content, 0o600); err != nil {
		return ExportedArtifact{}, err
	}
	return ExportedArtifact{Path: output, Format: strings.TrimPrefix(extension, "."), Size: int64(len(content))}, nil
}

func (s *Service) engineTarget() (engineHost, error) {
	s.mu.RLock()
	engine := s.engine
	s.mu.RUnlock()
	if engine == nil {
		return nil, errors.New("engine capabilities are unavailable until the local provider is running")
	}
	return engine, nil
}

// ownedTask is the whole of task isolation: a plugin may ask about a task only
// if this host started it on that plugin's behalf.
func (s *Service) ownedTask(pluginID, taskID string) (engineHost, error) {
	if err := s.requirePermission(pluginID, "engine.run"); err != nil {
		return nil, err
	}
	engine, err := s.engineTarget()
	if err != nil {
		return nil, err
	}
	owned, err := s.engineTasks(pluginID)
	if err != nil {
		return nil, err
	}
	for _, candidate := range owned {
		if candidate == taskID {
			return engine, nil
		}
	}
	return nil, errors.New("this plugin did not start that task")
}

func (s *Service) engineTaskRunning(engine engineHost) (bool, error) {
	listing, err := engine.ListTasks()
	if err != nil {
		return false, err
	}
	tasks, ok := listing["tasks"].([]any)
	if !ok {
		return false, nil
	}
	for _, item := range tasks {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		snapshot, ok := record["snapshot"].(map[string]any)
		if !ok {
			continue
		}
		switch stringField(snapshot, "state") {
		case "queued", "running":
			return true, nil
		}
	}
	return false, nil
}

func (s *Service) artifactManifest(pluginID, taskID string) (map[string]any, error) {
	engine, err := s.ownedTask(pluginID, taskID)
	if err != nil {
		return nil, err
	}
	manifest, err := engine.TaskArtifacts(taskID)
	if err != nil {
		return nil, err
	}
	entries, ok := manifest["artifacts"].(map[string]any)
	if !ok {
		return nil, errors.New("the task reported no artifacts")
	}
	return entries, nil
}

func (s *Service) resolveArtifact(pluginID, taskID, name string) (string, error) {
	entries, err := s.artifactManifest(pluginID, taskID)
	if err != nil {
		return "", err
	}
	record, ok := entries[name].(map[string]any)
	if !ok {
		return "", fmt.Errorf("the task produced no artifact named %q", name)
	}
	return s.artifactPath(taskID, stringField(record, "uri"))
}

// artifactPath turns a manifest URI into a path inside this task's directory.
// The containment check is the point: the manifest is written by the engine,
// but a path that escaped the task directory would turn "read my artifact"
// into "read any file", so it is verified here rather than trusted.
func (s *Service) artifactPath(taskID, uri string) (string, error) {
	parsed, err := url.Parse(uri)
	if err != nil || parsed.Scheme != "file" || parsed.Host != "" {
		return "", errors.New("artifact is not a local file")
	}
	path := parsed.Path
	if runtime.GOOS == "windows" && len(path) > 2 && path[0] == '/' && path[2] == ':' {
		path = path[1:]
	}
	path, err = filepath.Abs(filepath.FromSlash(path))
	if err != nil {
		return "", err
	}
	root := filepath.Join(s.dataDirectory, "tasks", taskID)
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", errors.New("artifact is outside its task directory")
	}
	return path, nil
}

// claimLLMCall takes this plugin's concurrency slot and one unit of its
// rolling budget, returning the release. Both are per plugin: one plugin
// spending its budget must not shut another out.
func (s *Service) claimLLMCall(pluginID string) (func(), error) {
	now := time.Now()
	s.llmMu.Lock()
	defer s.llmMu.Unlock()
	if s.llmActive == nil {
		s.llmActive = map[string]bool{}
		s.llmCalls = map[string][]time.Time{}
	}
	if s.llmActive[pluginID] {
		return nil, errors.New("this plugin already has an LLM call in flight")
	}
	recent := make([]time.Time, 0, len(s.llmCalls[pluginID]))
	for _, at := range s.llmCalls[pluginID] {
		if now.Sub(at) < llmCallWindow {
			recent = append(recent, at)
		}
	}
	if len(recent) >= llmCallsPerWindow {
		s.llmCalls[pluginID] = recent
		return nil, fmt.Errorf("this plugin has made %d LLM calls in the last %d minutes; try again later", llmCallsPerWindow, int(llmCallWindow.Minutes()))
	}
	s.llmCalls[pluginID] = append(recent, now)
	s.llmActive[pluginID] = true
	return func() {
		s.llmMu.Lock()
		delete(s.llmActive, pluginID)
		s.llmMu.Unlock()
	}, nil
}

// engineTasks and recordEngineTask keep the ownership list. It lives beside
// the plugin's other data so uninstalling with "remove data" forgets it too.
func (s *Service) engineTasks(pluginID string) ([]string, error) {
	path, err := s.pluginDataPath(pluginID, engineTasksFile)
	if err != nil {
		return nil, err
	}
	s.engineMu.Lock()
	defer s.engineMu.Unlock()
	return readEngineTasks(path)
}

func (s *Service) recordEngineTask(pluginID, taskID string) error {
	path, err := s.pluginDataPath(pluginID, engineTasksFile)
	if err != nil {
		return err
	}
	s.engineMu.Lock()
	defer s.engineMu.Unlock()
	tasks, err := readEngineTasks(path)
	if err != nil {
		return err
	}
	tasks = append(tasks, taskID)
	if len(tasks) > maxOwnedTasks {
		tasks = tasks[len(tasks)-maxOwnedTasks:]
	}
	body, err := json.Marshal(map[string]any{"schema": 1, "tasks": tasks})
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	return os.WriteFile(path, body, 0o600)
}

func readEngineTasks(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var value struct {
		Tasks []string `json:"tasks"`
	}
	if err := json.Unmarshal(data, &value); err != nil {
		// A corrupt list costs the plugin its history, not its next run.
		return nil, nil
	}
	return value.Tasks, nil
}

func llmPayload(request LLMRequest) (map[string]any, error) {
	if !slices.Contains(llmRoles, request.Role) {
		return nil, fmt.Errorf("LLM role must be one of: %s", strings.Join(llmRoles, ", "))
	}
	if len(request.Messages) == 0 {
		return nil, errors.New("LLM messages are required")
	}
	if len(request.Messages) > maxLLMMessages {
		return nil, fmt.Errorf("LLM messages exceed %d turns", maxLLMMessages)
	}
	total := 0
	messages := make([]map[string]any, 0, len(request.Messages))
	for index, message := range request.Messages {
		switch message.Role {
		case "system", "user", "assistant":
		default:
			return nil, fmt.Errorf("LLM message %d role must be system, user or assistant", index)
		}
		total += len(message.Content)
		if total > maxLLMPromptBytes {
			return nil, fmt.Errorf("LLM messages exceed %d bytes", maxLLMPromptBytes)
		}
		messages = append(messages, map[string]any{"role": message.Role, "content": message.Content})
	}
	if strings.TrimSpace(request.Messages[len(request.Messages)-1].Content) == "" {
		return nil, errors.New("the last LLM message is empty")
	}
	payload := map[string]any{"role": request.Role, "messages": messages}
	if request.MaxTokens != 0 {
		if request.MaxTokens < 1 || request.MaxTokens > maxLLMOutputTokens {
			return nil, fmt.Errorf("maxTokens must be between 1 and %d", maxLLMOutputTokens)
		}
		payload["max_tokens"] = request.MaxTokens
	}
	if request.Temperature != 0 {
		if request.Temperature < 0 || request.Temperature > 2 {
			return nil, errors.New("temperature must be between 0 and 2")
		}
		payload["temperature"] = request.Temperature
	}
	return payload, nil
}

func engineTaskPayload(request EngineTaskRequest, entry library.Entry) (map[string]any, error) {
	if !slices.Contains(engineTargets, request.Target) {
		return nil, fmt.Errorf("engine target must be one of: %s", strings.Join(engineTargets, ", "))
	}
	language := strings.TrimSpace(request.Language)
	if language == "" {
		language = "ja"
	}
	if !languagePattern.MatchString(language) {
		return nil, errors.New("engine language must be a language tag such as ja or zh-Hans")
	}
	correction, err := correctionPayload(request.Correction)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"schema":   1,
		"provider": "local",
		"source": map[string]any{
			"kind":        "local_file",
			"path":        entry.SourcePath,
			"title":       entry.Title,
			"fingerprint": entry.Fingerprint,
			"video_id":    entry.ID,
			"duration":    entry.Duration,
		},
		"target":        request.Target,
		"language":      language,
		"device":        "cuda",
		"gpu_tier":      "auto",
		"separate":      !request.SkipSeparation,
		"vocal_profile": "quality",
		"correction":    correction,
		// Both forced, not defaulted. A knowledge update writes the user's
		// knowledge base and a projection replaces the document they are
		// editing; neither is something a plugin run was asked to do, and a
		// plugin that could ask for them could do it without the user seeing.
		"knowledge":            "none",
		"projection":           "none",
		"cleanup_intermediate": false,
	}, nil
}

func correctionPayload(correction EngineCorrection) (map[string]any, error) {
	payload := map[string]any{}
	for _, field := range []struct {
		name    string
		value   string
		allowed []string
	}{
		{"media", correction.Media, []string{"text", "audio", "video"}},
		{"retrieval", correction.Retrieval, []string{"none", "local", "native"}},
		{"difficulty", correction.Difficulty, []string{"quality", "speed"}},
		{"fast", correction.Fast, []string{"auto", "on", "off"}},
	} {
		if field.value == "" {
			continue
		}
		if !slices.Contains(field.allowed, field.value) {
			return nil, fmt.Errorf("correction.%s must be one of: %s", field.name, strings.Join(field.allowed, ", "))
		}
		payload[field.name] = field.value
	}
	for _, note := range []struct {
		name  string
		value string
	}{{"extra_info", correction.ExtraInfo}, {"extra_style", correction.ExtraStyle}} {
		if note.value == "" {
			continue
		}
		if len([]rune(note.value)) > maxCorrectionNotes {
			return nil, fmt.Errorf("correction.%s exceeds %d characters", note.name, maxCorrectionNotes)
		}
		payload[note.name] = note.value
	}
	return payload, nil
}

func readableArtifact(path string) bool {
	return slices.Contains(textArtifactExtensions, strings.ToLower(filepath.Ext(path)))
}

// artifactFileName keeps the plugin's suggestion as a bare file name with the
// artifact's own extension; the directory is the dialog's, never the plugin's.
func artifactFileName(suggested, name, extension string) string {
	suggested = strings.TrimSpace(filepath.Base(strings.TrimSpace(suggested)))
	if suggested == "" || suggested == "." || suggested == string(filepath.Separator) {
		suggested = name
	}
	if len([]rune(suggested)) > maxTitleRunes {
		suggested = string([]rune(suggested)[:maxTitleRunes])
	}
	if !strings.EqualFold(filepath.Ext(suggested), extension) {
		suggested += extension
	}
	return suggested
}

func sortArtifacts(artifacts []EngineArtifact) {
	// Pipeline order, so a page listing them reads as the run did. Names the
	// engine adds later fall to the end in name order rather than vanishing.
	rank := map[string]int{
		"vocal_audio": 0, "vad_json": 1, "vad_energy_npz": 2, "aligned_json": 3,
		"stable_json": 4, "raw_srt": 5, "translated_srt": 6, "annotated_csv": 7,
		"final_srt": 8, "metadata_json": 9,
	}
	order := func(name string) int {
		if position, known := rank[name]; known {
			return position
		}
		return len(rank)
	}
	slices.SortFunc(artifacts, func(left, right EngineArtifact) int {
		if order(left.Name) != order(right.Name) {
			return order(left.Name) - order(right.Name)
		}
		return strings.Compare(left.Name, right.Name)
	})
}

func stringField(value map[string]any, key string) string {
	text, _ := value[key].(string)
	return text
}
