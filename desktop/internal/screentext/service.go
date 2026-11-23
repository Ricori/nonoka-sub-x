// Package screentext is the editor's on-screen text add-on (文字识别): it starts the sidecar's
// `screen-text` task for a library video, follows it, and tells the windows when the video's
// document has the new tracks.
//
// The task itself is the sidecar's (`nonoka_x.screen_text`): it reads the text in the frames,
// translates it, and appends the result to the document as extra tracks (ids `ocr…`) when it
// ends -- the default track and the user's tracks are never touched. Here there is only the
// bookkeeping: which task belongs to which video, a poll that turns the task's snapshot into
// `screentext:progress` events, and `document:changed` once the document moved, so an open editor
// merges the tracks in instead of finding its next save refused.
package screentext

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/wailsapp/wails/v3/pkg/application"
)

// Target is the sidecar task target of an on-screen text run.
const Target = "screen-text"

// pollInterval is how often a running task's snapshot is read.
var pollInterval = time.Second

// Events the windows listen to.
const (
	ProgressEvent        = "screentext:progress"
	DocumentChangedEvent = "document:changed"
)

type engine interface {
	StartTask(request map[string]any) (map[string]any, error)
	ListTasks() (map[string]any, error)
	TaskStatus(taskID string) (map[string]any, error)
	TaskEvents(taskID string, afterCursor int) (map[string]any, error)
	CancelTask(taskID string) (map[string]any, error)
}

type media interface {
	Get(id string) (library.Entry, error)
}

type emitter func(name string, data any)

// Options are what the editor's tab chooses for one run.
type Options struct {
	// Start and End limit the run to part of the video (seconds; End 0 = to the end).
	Start float64 `json:"start"`
	End   float64 `json:"end"`
	// Device is "cuda", "cpu" or "cuda:N".
	Device string `json:"device"`
	// SkipMain leaves out the video's own subtitle band (the speech track covers it).
	SkipMain bool `json:"skipMain"`
	// Translate is "zh" or "none".
	Translate string `json:"translate"`
	// Cover is "plate" (an opaque plate over the original text) or "none".
	Cover string `json:"cover"`
	// Game names a game-script bundle to match against ("" for none).
	Game string `json:"game"`
	// Trial reads a minute only and appends nothing (a preview).
	Trial bool `json:"trial"`
	// OnEdited says what a re-run does with on-screen tracks the user changed: "replace" or "keep".
	OnEdited string `json:"onEdited"`
	// ROI is a part of the frame to read ([x0, y0, x1, y1] in 0..1); empty for the whole frame.
	ROI []float64 `json:"roi,omitempty"`
}

// Service follows the on-screen text tasks, one per video at most.
type Service struct {
	mu     sync.Mutex
	engine engine
	media  media
	emit   emitter
	// tasks: video id -> the task id of its latest run.
	tasks map[string]string
	// watching: task ids a poll is following.
	watching map[string]bool
}

// New is the service, with nothing attached yet.
func New(engine engine, media media) *Service {
	return &Service{engine: engine, media: media, tasks: map[string]string{}, watching: map[string]bool{}}
}

// Attach gives the service the app to emit events on, and picks up tasks a previous run left going.
func Attach(s *Service, app *application.App) {
	s.mu.Lock()
	s.emit = func(name string, data any) { app.Event.Emit(name, data) }
	s.mu.Unlock()
	go s.resume()
}

func (s *Service) send(name string, data any) {
	s.mu.Lock()
	emit := s.emit
	s.mu.Unlock()
	if emit != nil {
		emit(name, data)
	}
}

func validChoice(value string, choices ...string) bool {
	for _, choice := range choices {
		if value == choice {
			return true
		}
	}
	return false
}

func payload(entry library.Entry, options Options) (map[string]any, error) {
	device := strings.TrimSpace(options.Device)
	if device == "" {
		device = "cuda"
	}
	translate := options.Translate
	if translate == "" {
		translate = "zh"
	}
	cover := options.Cover
	if cover == "" {
		cover = "plate"
	}
	onEdited := options.OnEdited
	if onEdited == "" {
		onEdited = "replace"
	}
	if !validChoice(translate, "zh", "none") || !validChoice(cover, "plate", "none") || !validChoice(onEdited, "replace", "keep") {
		return nil, errors.New("invalid screen-text options")
	}
	if options.Start < 0 || (options.End != 0 && options.End <= options.Start) {
		return nil, errors.New("invalid screen-text range")
	}
	screenText := map[string]any{
		"skip_main": options.SkipMain,
		"translate": translate,
		"cover":     cover,
		"trial":     options.Trial,
		"on_edited": onEdited,
		"range":     nil,
		"match":     nil,
		"roi":       nil,
	}
	if options.Start > 0 || options.End > 0 {
		screenText["range"] = map[string]any{"start": options.Start, "end": options.End}
	}
	if options.Game != "" {
		screenText["match"] = map[string]any{"game": options.Game}
	}
	if len(options.ROI) == 4 {
		screenText["roi"] = options.ROI
	} else if len(options.ROI) != 0 {
		return nil, errors.New("invalid screen-text roi")
	}
	return map[string]any{
		"schema":   1,
		"provider": "local",
		"target":   Target,
		"device":   device,
		"source": map[string]any{
			"kind":        "local_file",
			"path":        entry.SourcePath,
			"title":       entry.Title,
			"fingerprint": entry.Fingerprint,
			"video_id":    entry.ID,
			"duration":    entry.Duration,
		},
		"screen_text": screenText,
	}, nil
}

// Start runs the add-on on a library video and follows the task.
func (s *Service) Start(mediaID string, options Options) (map[string]any, error) {
	if s.engine == nil || s.media == nil {
		return nil, errors.New("local engine is unavailable")
	}
	entry, err := s.media.Get(mediaID)
	if err != nil {
		return nil, err
	}
	if !entry.Available {
		return nil, errors.New("找不到这个视频的本地文件")
	}
	request, err := payload(entry, options)
	if err != nil {
		return nil, err
	}
	snapshot, err := s.engine.StartTask(request)
	if err != nil {
		return nil, err
	}
	taskID, _ := snapshot["task_id"].(string)
	if taskID == "" {
		return nil, errors.New("the engine did not report a task id")
	}
	s.mu.Lock()
	s.tasks[mediaID] = taskID
	s.mu.Unlock()
	s.follow(mediaID, taskID, options.Trial)
	return snapshot, nil
}

// Status is the snapshot of the video's latest on-screen text task, or nil when it never had one.
func (s *Service) Status(mediaID string) (map[string]any, error) {
	s.mu.Lock()
	taskID := s.tasks[mediaID]
	s.mu.Unlock()
	if taskID == "" {
		found, err := s.latest(mediaID)
		if err != nil || found == "" {
			return nil, err
		}
		taskID = found
	}
	return s.engine.TaskStatus(taskID)
}

// Cancel stops the video's running on-screen text task.
func (s *Service) Cancel(mediaID string) (map[string]any, error) {
	s.mu.Lock()
	taskID := s.tasks[mediaID]
	s.mu.Unlock()
	if taskID == "" {
		return nil, fmt.Errorf("没有正在进行的文字识别任务")
	}
	return s.engine.CancelTask(taskID)
}

// latest finds the video's newest on-screen text task in the sidecar's list (after a restart).
func (s *Service) latest(mediaID string) (string, error) {
	listing, err := s.engine.ListTasks()
	if err != nil {
		return "", err
	}
	records, _ := listing["tasks"].([]any)
	newest, newestAt := "", ""
	for _, raw := range records {
		record, _ := raw.(map[string]any)
		snapshot, _ := record["snapshot"].(map[string]any)
		if record == nil || snapshot == nil || record["media_id"] != mediaID || snapshot["kind"] != Target {
			continue
		}
		created, _ := snapshot["created_at"].(string)
		if newest == "" || created > newestAt {
			newest, _ = snapshot["task_id"].(string)
			newestAt = created
		}
	}
	if newest != "" {
		s.mu.Lock()
		if _, ok := s.tasks[mediaID]; !ok {
			s.tasks[mediaID] = newest
		}
		s.mu.Unlock()
	}
	return newest, nil
}

// resume follows the on-screen text tasks still running when the app starts.
func (s *Service) resume() {
	if s.engine == nil {
		return
	}
	listing, err := s.engine.ListTasks()
	if err != nil {
		return
	}
	records, _ := listing["tasks"].([]any)
	for _, raw := range records {
		record, _ := raw.(map[string]any)
		snapshot, _ := record["snapshot"].(map[string]any)
		if snapshot == nil || snapshot["kind"] != Target {
			continue
		}
		state, _ := snapshot["state"].(string)
		mediaID, _ := record["media_id"].(string)
		taskID, _ := snapshot["task_id"].(string)
		if (state == "queued" || state == "running") && mediaID != "" && taskID != "" {
			s.mu.Lock()
			s.tasks[mediaID] = taskID
			s.mu.Unlock()
			s.follow(mediaID, taskID, false)
		}
	}
}

func terminal(state string) bool {
	return state == "completed" || state == "failed" || state == "cancelled" || state == "interrupted"
}

// follow polls one task until it ends, emitting its snapshot each time it changes.
func (s *Service) follow(mediaID, taskID string, trial bool) {
	s.mu.Lock()
	if s.watching[taskID] {
		s.mu.Unlock()
		return
	}
	s.watching[taskID] = true
	s.mu.Unlock()
	go func() {
		defer func() {
			s.mu.Lock()
			delete(s.watching, taskID)
			s.mu.Unlock()
		}()
		last := ""
		failures := 0
		for {
			snapshot, err := s.engine.TaskStatus(taskID)
			if err != nil {
				failures++
				if failures > 30 {
					return
				}
				time.Sleep(pollInterval)
				continue
			}
			failures = 0
			stamp := fmt.Sprint(snapshot["state"], snapshot["stage"], snapshot["progress"], snapshot["updated_at"])
			if stamp != last {
				last = stamp
				s.send(ProgressEvent, map[string]any{"mediaId": mediaID, "taskId": taskID, "snapshot": snapshot})
			}
			state, _ := snapshot["state"].(string)
			if terminal(state) {
				if state == "completed" && !trial {
					s.send(DocumentChangedEvent, map[string]any{
						"videoId": mediaID, "rev": s.completedRev(taskID), "reason": Target, "taskId": taskID,
					})
					s.send("home:refresh", true)
				}
				return
			}
			time.Sleep(pollInterval)
		}
	}()
}

// completedRev is the document revision the task's completion reported, or -1 when it said none.
func (s *Service) completedRev(taskID string) int {
	page, err := s.engine.TaskEvents(taskID, 0)
	if err != nil {
		return -1
	}
	events, _ := page["events"].([]any)
	for index := len(events) - 1; index >= 0; index-- {
		event, _ := events[index].(map[string]any)
		if event == nil || event["type"] != "completed" {
			continue
		}
		payload, _ := event["payload"].(map[string]any)
		if rev, ok := payload["document_rev"].(float64); ok {
			return int(rev)
		}
	}
	return -1
}
