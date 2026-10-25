package cloud

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
)

// A relay is one subtitle task that crosses the machine boundary twice: the
// cloud recognises the media up to stable.json, this desktop downloads that
// file, and the local provider continues from it with the user's own models.
// The renderer sees one task -- provider "cloud", one id, one event stream --
// and everything that makes it two lives here.
//
// The relay is the durable record, not the cloud job: a job is deleted the
// moment its stable.json is safely on disk, and the cloud keeps no log once it
// is. So the record, the merged event stream and the downloaded stable.json
// all live under the data directory, and a restart resumes the relay from
// whichever leg it was in.

var validRelayID = regexp.MustCompile(`^rly_[0-9a-f]{24}$`)
var validJobID = regexp.MustCompile(`^job_[0-9a-f]{24}$`)

const (
	relayPhaseCloud = "cloud" // the cloud job is queued or running
	relayPhaseFetch = "fetch" // the job finished; stable.json is being collected
	relayPhaseLocal = "local" // the local provider continues from it

	// How many collection attempts before the relay gives up and asks the user
	// to retry. A download that keeps failing is not going to be fixed by the
	// next poll, and a relay stuck "running" forever reads as a hang.
	relayFetchAttempts = 5
	// How many polls the local leg waits for a sidecar that is not running --
	// it is restarted on its own after a crash, and starts later than this
	// service on launch -- before failing the relay instead of waiting forever.
	relayLocalWaits = 60
	relayEventPage  = 500
)

// The stages a continuation reports for work it skipped: the cloud already ran
// them, and replaying "已有结果，跳过" for each would send the progress bar back
// to the start of the run.
var relayReplayedStages = map[string]bool{"vocal": true, "aligned": true, "stable": true}

// The stages the local leg may name in the relay's snapshot. Anything before
// these is the replay above, and the snapshot keeps saying "handoff" instead.
var relayLocalStages = map[string]bool{"raw-srt": true, "translated-srt": true, "final-srt": true}

type relayError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type relayRecord struct {
	Schema      int     `json:"schema"`
	ID          string  `json:"id"`
	LocalID     string  `json:"localId"`
	Title       string  `json:"title"`
	Fingerprint string  `json:"fingerprint"`
	Duration    float64 `json:"duration"`
	// What the user asked for overall; the cloud leg always stops at stable.
	Target string `json:"target"`
	// The dialog's request as it arrived. Both legs are built from it, the
	// cloud one again on a resubmission, the local one when the handoff comes.
	Options map[string]any `json:"options"`

	Phase          string `json:"phase"`
	CloudJobID     string `json:"cloudJobId"`
	CloudCursor    int    `json:"cloudCursor"`
	CloudCollected bool   `json:"cloudCollected"`
	FetchAttempts  int    `json:"fetchAttempts"`
	LocalTaskID    string `json:"localTaskId,omitempty"`
	LocalCursor    int    `json:"localCursor"`
	LocalWaits     int    `json:"localWaits"`

	State     string         `json:"state"`
	Stage     string         `json:"stage"`
	Progress  any            `json:"progress"`
	Error     *relayError    `json:"error"`
	Engine    map[string]any `json:"engine,omitempty"`
	Cursor    int            `json:"cursor"`
	CreatedAt string         `json:"createdAt"`
	StartedAt string         `json:"startedAt"`
	UpdatedAt string         `json:"updatedAt"`
}

func (r *relayRecord) clone() *relayRecord {
	copied := *r
	return &copied
}

func (r *relayRecord) terminal() bool {
	switch r.State {
	case "completed", "failed", "cancelled", "interrupted":
		return true
	}
	return false
}

func (r *relayRecord) fail(code, message string) {
	r.State = "failed"
	r.Error = &relayError{Code: code, Message: message}
}

// relayEvent is what the renderer reads: the provider event shape, renumbered
// into the relay's own cursor space.
type relayEvent struct {
	Cursor    int            `json:"cursor"`
	TaskID    string         `json:"task_id"`
	Type      string         `json:"type"`
	Timestamp string         `json:"timestamp"`
	Payload   map[string]any `json:"payload"`
}

// relayLine is one line of events.jsonl. The leg and the upstream cursor ride
// beside the event so a restart can recover each leg's read position from the
// file itself: the record is saved after the events it describes, and a crash
// in between must not replay them.
type relayLine struct {
	Leg      string     `json:"leg"`
	Source   string     `json:"source,omitempty"`
	Upstream int        `json:"upstream,omitempty"`
	Event    relayEvent `json:"event"`
}

type relayDriver struct {
	cancel context.CancelFunc
	done   chan struct{}
}

type upstreamEvent struct {
	Cursor    int            `json:"cursor"`
	Type      string         `json:"type"`
	Timestamp string         `json:"timestamp"`
	Payload   map[string]any `json:"payload"`
}

type upstreamSnapshot struct {
	State    string         `json:"state"`
	Stage    string         `json:"stage"`
	Progress any            `json:"progress"`
	Error    *relayError    `json:"error"`
	Engine   map[string]any `json:"engine"`
}

func newRelayID() (string, error) {
	buffer := make([]byte, 12)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return "rly_" + hex.EncodeToString(buffer), nil
}

func relayNow() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func (s *Service) relayDirectory(id string) string {
	return filepath.Join(s.relayRoot, id)
}

func (s *Service) relayStablePath(id string) string {
	return filepath.Join(s.relayDirectory(id), "stable.json")
}

// loadRelays reads every relay on disk and recovers each leg's read position
// from its event file. A damaged record is skipped with a log line, the way
// the other state files are: one bad task must not keep the app from starting.
func (s *Service) loadRelays() {
	entries, err := os.ReadDir(s.relayRoot)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			log.Printf("cloud: cannot list relays: %v", err)
		}
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() || !validRelayID.MatchString(entry.Name()) {
			continue
		}
		var record relayRecord
		if !readState(filepath.Join(s.relayDirectory(entry.Name()), "relay.json"), &record) || record.ID != entry.Name() {
			continue
		}
		s.recoverRelayCursors(&record)
		s.relays[record.ID] = &record
	}
}

func (s *Service) recoverRelayCursors(record *relayRecord) {
	lines, err := s.readRelayLines(record.ID)
	if err != nil {
		return
	}
	for _, line := range lines {
		record.Cursor = max(record.Cursor, line.Event.Cursor)
		switch {
		case line.Leg == relayPhaseCloud && line.Source == record.CloudJobID:
			record.CloudCursor = max(record.CloudCursor, line.Upstream)
		case line.Leg == relayPhaseLocal && line.Source != "" && line.Source == record.LocalTaskID:
			record.LocalCursor = max(record.LocalCursor, line.Upstream)
		}
	}
}

func (s *Service) readRelayLines(id string) ([]relayLine, error) {
	file, err := os.Open(filepath.Join(s.relayDirectory(id), "events.jsonl"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	defer file.Close()
	var lines []relayLine
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64<<10), maxResponseBytes)
	for scanner.Scan() {
		var line relayLine
		// A torn last line is what a crash mid-append leaves; everything
		// before it is still the log.
		if json.Unmarshal(scanner.Bytes(), &line) == nil {
			lines = append(lines, line)
		}
	}
	return lines, scanner.Err()
}

// saveRelayLocked writes the record atomically. Callers hold relayMu.
func (s *Service) saveRelayLocked(record *relayRecord) error {
	directory := s.relayDirectory(record.ID)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	path := filepath.Join(directory, "relay.json")
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

// appendRelayLinesLocked numbers the events into the relay's cursor space and
// appends them. Callers hold relayMu and save the record afterwards.
func (s *Service) appendRelayLinesLocked(record *relayRecord, lines []relayLine) error {
	if len(lines) == 0 {
		return nil
	}
	directory := s.relayDirectory(record.ID)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	file, err := os.OpenFile(filepath.Join(directory, "events.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	writer := bufio.NewWriter(file)
	for _, line := range lines {
		record.Cursor++
		line.Event.Cursor = record.Cursor
		line.Event.TaskID = record.ID
		if line.Event.Timestamp == "" {
			line.Event.Timestamp = relayNow()
		}
		if line.Event.Payload == nil {
			line.Event.Payload = map[string]any{}
		}
		encoded, err := json.Marshal(line)
		if err != nil {
			return err
		}
		if _, err := writer.Write(append(encoded, '\n')); err != nil {
			return err
		}
	}
	return writer.Flush()
}

func relayNote(eventType, message string) relayLine {
	return relayLine{Leg: "relay", Event: relayEvent{Type: eventType, Payload: map[string]any{"message": message}}}
}

// updateRelay applies change to a copy of the record, appends the events it
// returns, and saves both. Nothing is changed in memory unless the save
// succeeded, so a full disk cannot leave the snapshot ahead of the file.
func (s *Service) updateRelay(id string, change func(*relayRecord) ([]relayLine, error)) (*relayRecord, error) {
	s.relayMu.Lock()
	defer s.relayMu.Unlock()
	current, exists := s.relays[id]
	if !exists {
		return nil, errors.New("cloud task does not exist")
	}
	next := current.clone()
	lines, err := change(next)
	if err != nil {
		return nil, err
	}
	if err := s.appendRelayLinesLocked(next, lines); err != nil {
		return nil, err
	}
	next.UpdatedAt = relayNow()
	if err := s.saveRelayLocked(next); err != nil {
		return nil, err
	}
	s.relays[id] = next
	return next.clone(), nil
}

func (s *Service) relay(id string) (*relayRecord, error) {
	if !validRelayID.MatchString(id) {
		return nil, errors.New("invalid cloud task id")
	}
	s.relayMu.RLock()
	defer s.relayMu.RUnlock()
	record, exists := s.relays[id]
	if !exists {
		return nil, errors.New("cloud task does not exist")
	}
	return record.clone(), nil
}

func (r *relayRecord) snapshot() map[string]any {
	var failure any
	if r.Error != nil {
		failure = map[string]any{"code": r.Error.Code, "message": r.Error.Message}
	}
	engine := map[string]any{"version": "", "commit": ""}
	for key, value := range r.Engine {
		engine[key] = value
	}
	capabilities := map[string]any{"target": r.Target, "video_multimodal": false}
	return map[string]any{
		"schema":                 1,
		"task_id":                r.ID,
		"provider":               "cloud",
		"state":                  r.State,
		"stage":                  r.Stage,
		"progress":               r.Progress,
		"engine":                 engine,
		"requested_capabilities": capabilities,
		"effective_capabilities": capabilities,
		"error":                  failure,
		"last_cursor":            r.Cursor,
		"created_at":             r.CreatedAt,
		"started_at":             r.StartedAt,
		"updated_at":             r.UpdatedAt,
		// Which leg is running, for a log panel that wants to say so. The
		// renderer's contract does not need it.
		"relay": map[string]any{"phase": r.Phase, "cloud_job_id": r.CloudJobID, "local_task_id": r.LocalTaskID},
	}
}

// legacyCloudSnapshot settles a history row from before the relay. Those ids
// named records the backend no longer keeps, so polling them would fail for
// as long as the row stayed active.
func legacyCloudSnapshot(id string) map[string]any {
	now := relayNow()
	return map[string]any{
		"schema": 1, "task_id": id, "provider": "cloud", "state": "failed", "stage": "failed",
		"progress": nil, "engine": map[string]any{"version": "", "commit": ""},
		"requested_capabilities": map[string]any{}, "effective_capabilities": map[string]any{},
		"error":       map[string]any{"code": "legacy_cloud_task", "message": "这是旧版云端任务，新版本已无法查询；如需字幕请重新提交"},
		"last_cursor": 0, "created_at": now, "updated_at": now,
	}
}

func isLegacyCloudTask(id string) bool {
	return validLibraryID.MatchString(id) || validJobID.MatchString(id)
}

// --- driving ---------------------------------------------------------------

func (s *Service) startRelayDriver(id string) {
	s.relayMu.Lock()
	defer s.relayMu.Unlock()
	if s.lifetime.Err() != nil || s.drivers[id] != nil {
		return
	}
	record, exists := s.relays[id]
	if !exists || record.terminal() {
		return
	}
	ctx, cancel := context.WithCancel(s.lifetime)
	driver := &relayDriver{cancel: cancel, done: make(chan struct{})}
	s.drivers[id] = driver
	s.relayWG.Add(1)
	go func() {
		defer s.relayWG.Done()
		defer close(driver.done)
		defer func() {
			s.relayMu.Lock()
			if s.drivers[id] == driver {
				delete(s.drivers, id)
			}
			s.relayMu.Unlock()
		}()
		s.driveRelay(ctx, id)
	}()
}

// stopRelayDriver stops the relay's driver and waits for it, so the caller is
// the only writer for as long as it acts on the relay.
func (s *Service) stopRelayDriver(id string) {
	s.relayMu.Lock()
	driver := s.drivers[id]
	delete(s.drivers, id)
	s.relayMu.Unlock()
	if driver != nil {
		driver.cancel()
		<-driver.done
	}
}

func (s *Service) driveRelay(ctx context.Context, id string) {
	for ctx.Err() == nil {
		record, err := s.relay(id)
		if err != nil || record.terminal() {
			return
		}
		switch record.Phase {
		case relayPhaseCloud:
			s.stepRelayCloud(ctx, record)
		case relayPhaseFetch:
			s.stepRelayFetch(ctx, record)
		case relayPhaseLocal:
			s.stepRelayLocal(ctx, record)
		default:
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(s.relayPoll):
		}
	}
}

// stepRelayCloud copies the job's new events and follows its state. Transport
// failures change nothing: the next poll asks again. Only an answer the
// backend gave moves the relay.
func (s *Service) stepRelayCloud(ctx context.Context, record *relayRecord) {
	jobID := record.CloudJobID
	// Status before events: a job that ends between the two reads then still
	// has its last events read, where the other order would stop the relay
	// with them unread.
	var snapshot upstreamSnapshot
	statusErr := s.authenticatedDo(ctx, http.MethodGet, "/v1/tasks/"+jobID, nil, &snapshot)
	var page struct {
		Events []upstreamEvent `json:"events"`
	}
	eventsErr := s.authenticatedDo(ctx, http.MethodGet, fmt.Sprintf("/v1/tasks/%s/events?after=%d", jobID, record.CloudCursor), nil, &page)
	if ctx.Err() != nil {
		return
	}
	var status *httpStatusError
	missing := errors.As(statusErr, &status) && status.Status == http.StatusNotFound
	_, _ = s.updateRelay(record.ID, func(next *relayRecord) ([]relayLine, error) {
		if next.CloudJobID != jobID || next.Phase != relayPhaseCloud {
			return nil, errors.New("relay moved on")
		}
		var lines []relayLine
		if eventsErr == nil {
			for _, event := range page.Events {
				if event.Cursor <= next.CloudCursor {
					continue
				}
				next.CloudCursor = event.Cursor
				// The relay announces the handoff itself; the job's own
				// "completed" is not the end of this task.
				if event.Type == "completed" {
					continue
				}
				lines = append(lines, relayLine{Leg: relayPhaseCloud, Source: jobID, Upstream: event.Cursor, Event: relayEvent{Type: event.Type, Timestamp: event.Timestamp, Payload: event.Payload}})
			}
		}
		if missing {
			next.fail("cloud_job_missing", "云端作业已不存在（可能已超过保留期限）；重试会重新上传音频，不会再次扣次")
			return append(lines, relayNote("failed", next.Error.Message)), nil
		}
		if statusErr != nil {
			return lines, nil
		}
		if len(snapshot.Engine) > 0 {
			next.Engine = snapshot.Engine
		}
		next.Progress = snapshot.Progress
		switch snapshot.State {
		case "queued":
			next.State = "queued"
			next.Stage = "queued"
		case "running":
			next.State = "running"
			if snapshot.Stage != "" {
				next.Stage = snapshot.Stage
			}
		case "completed":
			next.State = "running"
			next.Phase = relayPhaseFetch
			next.Stage = "handoff"
			next.Progress = nil
			next.FetchAttempts = 0
			lines = append(lines, relayLine{Leg: "relay", Event: relayEvent{Type: "handoff", Payload: map[string]any{"stage": "handoff", "message": "云端识别完成，正在取回识别结果"}}})
		case "failed", "cancelled", "interrupted":
			next.State = snapshot.State
			next.Stage = snapshot.Stage
			next.Error = snapshot.Error
		}
		return lines, nil
	})
}

// stepRelayFetch collects the job's stable.json, checks it against the
// digest the backend published, and only then tells the cloud to forget the
// job. The order is the whole safety of the handoff: deleting first would
// leave a run with no copy anywhere if the download then failed.
func (s *Service) stepRelayFetch(ctx context.Context, record *relayRecord) {
	err := s.collectStable(ctx, record)
	if ctx.Err() != nil {
		return
	}
	var status *httpStatusError
	_, _ = s.updateRelay(record.ID, func(next *relayRecord) ([]relayLine, error) {
		if next.Phase != relayPhaseFetch || next.CloudJobID != record.CloudJobID {
			return nil, errors.New("relay moved on")
		}
		if err == nil {
			next.Phase = relayPhaseLocal
			next.LocalTaskID = ""
			next.LocalCursor = 0
			next.LocalWaits = 0
			return []relayLine{relayNote("log", "识别结果已校验并保存在本机")}, nil
		}
		if errors.As(err, &status) && status.Status == http.StatusNotFound {
			next.fail("cloud_job_missing", "云端作业已不存在（可能已超过保留期限）；重试会重新上传音频，不会再次扣次")
			return []relayLine{relayNote("failed", next.Error.Message)}, nil
		}
		next.FetchAttempts++
		if next.FetchAttempts < relayFetchAttempts {
			return []relayLine{relayNote("warning", "取回识别结果失败，稍后重试："+err.Error())}, nil
		}
		next.fail("fetch_failed", "取回云端识别结果失败："+err.Error())
		return []relayLine{relayNote("failed", next.Error.Message)}, nil
	})
	if err == nil {
		s.collectCloudJob(ctx, record.ID)
	}
}

func (s *Service) collectStable(ctx context.Context, record *relayRecord) error {
	var manifest remoteArtifactManifest
	if err := s.authenticatedDo(ctx, http.MethodGet, "/v1/tasks/"+record.CloudJobID+"/artifacts", nil, &manifest); err != nil {
		return err
	}
	item, ok := manifest.Artifacts["stable_json"]
	if !ok {
		return errors.New("cloud job published no stable.json")
	}
	content, err := s.authenticatedText(ctx, item.URI)
	if err != nil {
		return err
	}
	verified, _, ok := verifyArtifactContent(content, item.SHA256)
	if !ok {
		return errors.New("stable.json does not match the digest the cloud published")
	}
	path := s.relayStablePath(record.ID)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".stable-*.part")
	if err != nil {
		return err
	}
	name := temporary.Name()
	_, err = temporary.Write(verified)
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, path)
	}
	if err != nil {
		_ = os.Remove(name)
	}
	return err
}

// collectCloudJob deletes the job once its result is on disk, which also
// redeems the ticket that would otherwise let the same media re-run for free.
// Best-effort and repeated: the local leg retries it on every poll until the
// backend answers, and the backend's own sweep removes the job regardless.
func (s *Service) collectCloudJob(ctx context.Context, id string) {
	record, err := s.relay(id)
	if err != nil || record.CloudCollected || record.CloudJobID == "" {
		return
	}
	err = s.authenticatedDo(ctx, http.MethodDelete, "/v1/tasks/"+record.CloudJobID, nil, nil)
	var status *httpStatusError
	if err != nil && !(errors.As(err, &status) && status.Status == http.StatusNotFound) {
		return
	}
	_, _ = s.updateRelay(id, func(next *relayRecord) ([]relayLine, error) {
		if next.CloudJobID != record.CloudJobID {
			return nil, errors.New("relay moved on")
		}
		next.CloudCollected = true
		return nil, nil
	})
}

func (s *Service) stepRelayLocal(ctx context.Context, record *relayRecord) {
	if !record.CloudCollected {
		s.collectCloudJob(ctx, record.ID)
	}
	if record.LocalTaskID == "" {
		s.startRelayLocal(ctx, record)
		return
	}
	localID := record.LocalTaskID
	// Status first, for the same reason as the cloud leg.
	var snapshot upstreamSnapshot
	statusErr := s.provider.DoJSON(ctx, http.MethodGet, "/v1/tasks/"+localID, nil, &snapshot)
	var page struct {
		Events []upstreamEvent `json:"events"`
	}
	eventsErr := s.provider.DoJSON(ctx, http.MethodGet, "/v1/tasks/"+localID+"/events?after="+strconv.Itoa(record.LocalCursor), nil, &page)
	if ctx.Err() != nil {
		return
	}
	_, _ = s.updateRelay(record.ID, func(next *relayRecord) ([]relayLine, error) {
		if next.LocalTaskID != localID || next.Phase != relayPhaseLocal {
			return nil, errors.New("relay moved on")
		}
		var lines []relayLine
		if eventsErr == nil {
			for _, event := range page.Events {
				if event.Cursor <= next.LocalCursor {
					continue
				}
				next.LocalCursor = event.Cursor
				if event.Type == "started" {
					continue
				}
				if event.Type == "stage" {
					stage, _ := event.Payload["stage"].(string)
					if reused, _ := event.Payload["reused"].(bool); reused && relayReplayedStages[stage] {
						continue
					}
				}
				lines = append(lines, relayLine{Leg: relayPhaseLocal, Source: localID, Upstream: event.Cursor, Event: relayEvent{Type: event.Type, Timestamp: event.Timestamp, Payload: event.Payload}})
			}
		}
		var gone *sidecar.HTTPError
		if errors.As(statusErr, &gone) && gone.StatusCode == http.StatusNotFound {
			// The local task was removed under the relay. Retrying starts a
			// new one from the stable.json that is still here.
			next.LocalTaskID = ""
			next.LocalCursor = 0
			next.fail("local_task_missing", "本机续跑任务已不存在；重试会从已取回的识别结果重新开始")
			return append(lines, relayNote("failed", next.Error.Message)), nil
		}
		if statusErr != nil {
			return lines, nil
		}
		if len(snapshot.Engine) > 0 {
			next.Engine = snapshot.Engine
		}
		if relayLocalStages[snapshot.Stage] {
			next.Stage = snapshot.Stage
		}
		switch snapshot.State {
		case "queued", "running":
			next.State = "running"
			next.Progress = snapshot.Progress
		case "completed":
			next.State = "completed"
			next.Stage = "completed"
			next.Progress = snapshot.Progress
		case "failed", "cancelled", "interrupted":
			next.State = snapshot.State
			next.Error = snapshot.Error
		}
		return lines, nil
	})
}

// startRelayLocal hands the downloaded stable.json to the local provider. A
// refusal -- no model configured, the launcher missing the harness -- fails
// the relay with the provider's own words; a sidecar that is not up yet is
// waited for, within a bound.
func (s *Service) startRelayLocal(ctx context.Context, record *relayRecord) {
	request, err := s.continuationRequest(record)
	var snapshot map[string]any
	if err == nil {
		err = s.provider.DoJSON(ctx, http.MethodPost, "/v1/tasks", request, &snapshot)
	}
	if ctx.Err() != nil {
		return
	}
	taskID, _ := snapshot["task_id"].(string)
	var refused *sidecar.HTTPError
	_, _ = s.updateRelay(record.ID, func(next *relayRecord) ([]relayLine, error) {
		if next.Phase != relayPhaseLocal || next.LocalTaskID != "" {
			return nil, errors.New("relay moved on")
		}
		switch {
		case err == nil && validTaskID.MatchString(taskID):
			next.LocalTaskID = taskID
			next.LocalCursor = 0
			next.State = "running"
			message := "转到本机继续：生成字幕"
			if next.Target == "final-srt" {
				message = "转到本机继续：纠错与翻译"
			}
			return []relayLine{{Leg: "relay", Event: relayEvent{Type: "handoff", Payload: map[string]any{"stage": "handoff", "message": message, "local_task_id": taskID}}}}, nil
		case err == nil:
			next.fail("local_start_failed", "本地服务返回了无效的任务 id")
		case errors.As(err, &refused):
			code := refused.Code
			if code == "" {
				code = "local_start_failed"
			}
			next.fail(code, refused.Message)
		case errors.Is(err, sidecar.ErrNotRunning) && next.LocalWaits < relayLocalWaits:
			next.LocalWaits++
			return nil, nil
		default:
			next.fail("local_start_failed", "无法在本机继续："+err.Error())
		}
		return []relayLine{relayNote("failed", next.Error.Message)}, nil
	})
}

// continuationRequest is the local leg's TaskRequest: the dialog's settings for
// everything past stable.json, and the media resolved afresh -- the file may
// have been relinked while the cloud worked.
func (s *Service) continuationRequest(record *relayRecord) (map[string]any, error) {
	if s.media == nil {
		return nil, errors.New("media library is unavailable")
	}
	path, title, fingerprint, duration, err := s.media.ResolveMedia(record.LocalID)
	if err != nil {
		return nil, fmt.Errorf("本机媒体不可用：%w", err)
	}
	options := record.Options
	source := map[string]any{
		"kind": "cloud_stable", "path": path, "stable_json": s.relayStablePath(record.ID),
		"title": title, "fingerprint": fingerprint, "video_id": record.LocalID, "duration": duration,
	}
	request := map[string]any{
		"schema": 1, "provider": "local", "source": source, "target": record.Target,
		"language": stringOption(options, "language", "ja"), "projection": "document",
		"cleanup_intermediate": false, "knowledge": "none", "correction": map[string]any{},
	}
	if record.Target == "final-srt" {
		if correction, ok := options["correction"].(map[string]any); ok {
			request["correction"] = correction
		}
		knowledge := stringOption(options, "knowledge", "none")
		if knowledge == "collect" {
			knowledge = "read"
		}
		request["knowledge"] = knowledge
		for _, key := range []string{"knowledge_subjects", "llm_model"} {
			if value, exists := options[key]; exists && value != nil {
				request[key] = value
			}
		}
	}
	return request, nil
}

// --- the renderer's surface ------------------------------------------------

func (s *Service) relayList(limit int) []map[string]any {
	s.relayMu.RLock()
	records := make([]*relayRecord, 0, len(s.relays))
	for _, record := range s.relays {
		records = append(records, record.clone())
	}
	s.relayMu.RUnlock()
	sort.Slice(records, func(left, right int) bool { return records[left].UpdatedAt > records[right].UpdatedAt })
	if len(records) > limit {
		records = records[:limit]
	}
	items := make([]map[string]any, 0, len(records))
	for _, record := range records {
		items = append(items, map[string]any{"snapshot": record.snapshot(), "media_id": record.LocalID, "title": record.Title})
	}
	return items
}

func (s *Service) relayEvents(id string, after int) (map[string]any, error) {
	record, err := s.relay(id)
	if err != nil {
		return nil, err
	}
	s.relayMu.RLock()
	lines, err := s.readRelayLines(id)
	s.relayMu.RUnlock()
	if err != nil {
		return nil, err
	}
	events := []relayEvent{}
	next := after
	for _, line := range lines {
		if line.Event.Cursor <= after {
			continue
		}
		if len(events) == relayEventPage {
			break
		}
		events = append(events, line.Event)
		next = line.Event.Cursor
	}
	return map[string]any{"schema": 1, "task_id": id, "events": events, "next_cursor": next, "state": record.State}, nil
}

func (s *Service) cancelRelay(id string) (map[string]any, error) {
	s.relayActionMu.Lock()
	defer s.relayActionMu.Unlock()
	record, err := s.relay(id)
	if err != nil {
		return nil, err
	}
	if record.terminal() {
		return record.snapshot(), nil
	}
	s.stopRelayDriver(id)
	switch {
	case record.Phase == relayPhaseCloud:
		if err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/tasks/"+record.CloudJobID+"/cancel", map[string]any{}, nil); err != nil {
			var status *httpStatusError
			if !(errors.As(err, &status) && status.Status == http.StatusNotFound) {
				s.startRelayDriver(id)
				return nil, err
			}
		}
	case record.Phase == relayPhaseLocal && record.LocalTaskID != "":
		if err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/tasks/"+record.LocalTaskID+"/cancel", map[string]any{}, nil); err != nil {
			s.startRelayDriver(id)
			return nil, err
		}
	}
	updated, err := s.updateRelay(id, func(next *relayRecord) ([]relayLine, error) {
		next.State = "cancelled"
		next.Progress = nil
		next.Error = nil
		return []relayLine{relayNote("cancelled", "任务已取消")}, nil
	})
	if err != nil {
		return nil, err
	}
	return updated.snapshot(), nil
}

// restartRelay picks the relay up in the leg it stopped in. The cloud leg
// re-runs its job, or submits a new one when the job is gone; the backend's
// ticket makes that resubmission free. The local leg retries the local task,
// which resumes from the engine's own checkpoints.
func (s *Service) restartRelay(id, action string) (map[string]any, error) {
	s.relayActionMu.Lock()
	defer s.relayActionMu.Unlock()
	record, err := s.relay(id)
	if err != nil {
		return nil, err
	}
	if !record.terminal() {
		return record.snapshot(), nil
	}
	if record.State == "completed" {
		return nil, errors.New("任务已完成，无需继续")
	}
	s.stopRelayDriver(id)
	var lines []relayLine
	change := func(next *relayRecord) {}
	switch record.Phase {
	case relayPhaseCloud:
		jobAction := "retry"
		if action == "resume" && record.State == "interrupted" {
			jobAction = "resume"
		}
		err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/tasks/"+record.CloudJobID+"/"+jobAction, map[string]any{}, nil)
		var status *httpStatusError
		if errors.As(err, &status) && status.Status == http.StatusNotFound {
			jobID, submitErr := s.submitCloudJob(record.LocalID, record.Options)
			if submitErr != nil {
				return nil, submitErr
			}
			change = func(next *relayRecord) {
				next.CloudJobID = jobID
				next.CloudCursor = 0
				next.CloudCollected = false
			}
			lines = append(lines, relayNote("started", "云端作业已过期，已重新上传音频并提交识别"))
		} else if err != nil {
			return nil, err
		} else {
			lines = append(lines, relayNote("started", "重新开始云端识别"))
		}
	case relayPhaseFetch:
		change = func(next *relayRecord) { next.FetchAttempts = 0 }
		lines = append(lines, relayNote("started", "重新取回云端识别结果"))
	case relayPhaseLocal:
		if record.LocalTaskID != "" {
			localAction := "retry"
			if record.State == "interrupted" {
				localAction = "resume"
			}
			var snapshot upstreamSnapshot
			if err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/tasks/"+record.LocalTaskID+"/"+localAction, map[string]any{}, &snapshot); err != nil {
				return nil, err
			}
		} else {
			change = func(next *relayRecord) { next.LocalWaits = 0 }
		}
		lines = append(lines, relayNote("started", "在本机继续处理"))
	}
	updated, err := s.updateRelay(id, func(next *relayRecord) ([]relayLine, error) {
		change(next)
		next.State = "running"
		if next.Phase == relayPhaseCloud {
			next.State = "queued"
			next.Stage = "queued"
		}
		next.Error = nil
		next.Progress = nil
		next.StartedAt = relayNow()
		return lines, nil
	})
	if err != nil {
		return nil, err
	}
	s.startRelayDriver(id)
	return updated.snapshot(), nil
}

// relayArtifacts reports the local leg's manifest under the relay's id. The
// local provider has already projected the document by the time it reports
// completion, so there is nothing to download here.
func (s *Service) relayArtifacts(id string) (map[string]any, error) {
	record, err := s.relay(id)
	if err != nil {
		return nil, err
	}
	if record.State != "completed" || record.LocalTaskID == "" {
		return nil, errors.New("cloud task has not finished")
	}
	var manifest map[string]any
	if err := s.provider.DoJSON(context.Background(), http.MethodGet, "/v1/tasks/"+record.LocalTaskID+"/artifacts", nil, &manifest); err != nil {
		return nil, err
	}
	manifest["task_id"] = id
	return manifest, nil
}

// relayLocalTask resolves a finished relay to the local task that produced its
// subtitles, which is what the library sync reads.
func (s *Service) relayLocalTask(id string) (string, error) {
	record, err := s.relay(id)
	if err != nil {
		return "", err
	}
	if record.State != "completed" || !validTaskID.MatchString(record.LocalTaskID) {
		return "", errors.New("cloud task has not finished")
	}
	return record.LocalTaskID, nil
}

// Close stops every relay driver. Their state is on disk; the next launch
// resumes each from the leg it was in.
func (s *Service) Close() {
	s.stopRelays()
	s.relayWG.Wait()
}
