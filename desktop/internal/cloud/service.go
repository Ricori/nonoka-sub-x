// Package cloud owns Nonoka X account login, the remote library and safe local-artifact sync.
package cloud

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
	"github.com/Ricori/nonoka-x/desktop/internal/telemetry"
)

const (
	maxResponseBytes  = 16 << 20
	maxArtifactBytes  = 32 << 20
	maxThumbnailBytes = 5 << 20
	// Subtitle artifacts can run to tens of megabytes, so their download gets no
	// client-wide deadline: a slow link must not cut a healthy transfer at 30s.
	// The header timeout still catches a backend that never answers.
	artifactHeaderTimeout   = 30 * time.Second
	artifactDownloadTimeout = 10 * time.Minute
)

var validTaskID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var validLibraryID = regexp.MustCompile(`^vid_[0-9a-f]{24}$`)
var validUploadID = regexp.MustCompile(`^upl_[0-9a-f]{32}$`)
var validLocalMediaID = regexp.MustCompile(`^loc_[0-9a-f]{12}$`)
var validPlaintextKey = regexp.MustCompile(`^[A-Za-z0-9._~-]{1,256}$`)

type providerCaller interface {
	DoJSON(context.Context, string, string, any, any) error
}

type mediaResolver interface {
	ResolveMedia(string) (path, title, fingerprint string, duration float64, err error)
	ThumbnailDataURL(string) (string, error)
	EnsureThumbnail(id, dataURL string) error
	AddPlaceholder(title, fingerprint string, duration float64) (library.Entry, error)
}

// mediaTarget is the local media a projection writes its document for. Only
// localID is required: a cloud-only adoption has no file yet, and the empty
// path simply means the editor opens without a player until the video is
// relinked.
type mediaTarget struct {
	localID     string
	path        string
	title       string
	fingerprint string
	duration    float64
}

type Session struct {
	Authenticated bool   `json:"authenticated"`
	Backend       string `json:"backend,omitempty"`
	Admin         bool   `json:"admin,omitempty"`
	Name          string `json:"name,omitempty"`
	Remaining     *int   `json:"remaining,omitempty"`
	Running       int    `json:"running"`
	Synced        int    `json:"synced,omitempty"`
	SyncSkipped   int    `json:"syncSkipped,omitempty"`
	SyncFailed    int    `json:"syncFailed,omitempty"`
	CloudVideos   int    `json:"cloudVideos,omitempty"`
	SyncError     string `json:"syncError,omitempty"`
	// The library, as /v1/session now returns it beside the session fields.
	// Deliberately without omitempty: a nil slice means the backend did not
	// send one and the caller has to ask /v1/library, while an empty slice
	// means it did and the library is empty. omitempty would collapse those
	// two into the same wire shape and put the extra request back.
	Videos []Entry `json:"videos"`
}

type Entry struct {
	ID          string  `json:"id"`
	Title       string  `json:"title"`
	Fingerprint string  `json:"fingerprint"`
	Duration    float64 `json:"duration"`
	Status      string  `json:"status"`
	Source      string  `json:"source"`
	// Reported by whichever desktop put the cover there, not probed by the
	// backend: a cloud task PUTs it to a presigned slot the backend never sees
	// the body of, and a local run sends it with the subtitles it syncs. False
	// wherever neither could produce one -- a source with no video stream, or a
	// machine without ffmpeg -- and the library then asks for no frame at all.
	ThumbnailAvailable bool `json:"thumbnailAvailable,omitempty"`
	// Which subtitle artifacts the entry holds, written by whichever run last
	// published a set. Status describes the newest attempt; this describes what
	// is actually retrievable, and the two come apart whenever a re-transcribe
	// of the same media is queued, running, or has failed -- the backend reuses
	// the library id for those, and the previous run's files stay in place until
	// a completing run replaces them.
	ArtifactNames []string `json:"artifactNames,omitempty"`
	EngineCommit  string   `json:"engineCommit,omitempty"`
	CreatedAt     string   `json:"createdAt"`
	UpdatedAt     string   `json:"updatedAt"`
	OwnerID       string   `json:"ownerId,omitempty"`
	OwnerName     string   `json:"ownerName,omitempty"`
}

// HasSubtitles reports whether the entry has a subtitle set to hand over.
//
// The one question every adoption path actually asks. A cloud library entry
// carries artifact names only once some run published them, and a later
// attempt on the same media does not take them away, so this stays true across
// a re-transcribe that is queued, running, or failed.
func (e Entry) HasSubtitles() bool {
	return len(e.ArtifactNames) > 0
}

type AdminKey struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	Key        string `json:"key"`
	KeyPreview string `json:"keyPreview"`
	Remaining  int    `json:"remaining"`
	Submitted  int    `json:"submitted,omitempty"`
	Running    int    `json:"running,omitempty"`
	CreatedAt  string `json:"createdAt,omitempty"`
}

type IssuedAdminKey struct {
	AdminKey
	Key       string `json:"key"`
	Generated bool   `json:"generated"`
}

// remoteSeenState is the fingerprint set of the last library listing this
// desktop received, and when. It is what tells "never reached the cloud" apart
// from "deleted there by another device": both look the same in a listing, but
// only the first may be uploaded again. Account is a digest of the backend and
// key, so signing in with another key starts from an empty set.
type remoteSeenState struct {
	Account      string   `json:"account"`
	ListedAt     int64    `json:"listedAt"`
	Fingerprints []string `json:"fingerprints"`
}

func accountTag(session storedSession) string {
	sum := sha256.Sum256([]byte(session.Backend + "\x00" + session.Key))
	return hex.EncodeToString(sum[:8])
}

type syncRequest struct {
	LocalID      string            `json:"localId"`
	Fingerprint  string            `json:"fingerprint"`
	Title        string            `json:"title"`
	Duration     float64           `json:"duration"`
	TaskID       string            `json:"taskId"`
	EngineCommit string            `json:"engineCommit"`
	Artifacts    map[string]string `json:"artifacts"`
	// The cover the local library already drew for this media, carried up with
	// the subtitles as the JPEG bytes encoding/json base64-encodes for it.
	// Nothing else uploads one on this path -- a local run never goes through
	// /v1/uploads/init, which is the only other place a cover reaches the
	// bucket -- so leaving it out is what made every synced entry coverless on
	// every other machine. Omitted when there is none, which leaves the
	// backend's flag false rather than promising a frame that is not there.
	Thumbnail []byte `json:"thumbnail,omitempty"`
}

type storedSession struct {
	Backend string `json:"backend"`
	Key     string `json:"key"`
}

type artifactManifest struct {
	TaskID       string `json:"task_id"`
	EngineCommit string `json:"engine_commit"`
	Artifacts    map[string]struct {
		URI string `json:"uri"`
	} `json:"artifacts"`
}

type remoteArtifactManifest struct {
	Schema       int    `json:"schema"`
	TaskID       string `json:"task_id"`
	EngineCommit string `json:"engine_commit"`
	Artifacts    map[string]struct {
		URI    string `json:"uri"`
		SHA256 string `json:"sha256"`
		Bytes  int64  `json:"bytes"`
	} `json:"artifacts"`
}

type Service struct {
	telemetry            *telemetry.Service
	mu                   sync.RWMutex
	syncMu               sync.Mutex
	configPath           string
	syncSuppressionsPath string
	remoteSeenPath       string
	syncDisabledPath     string
	root                 string
	thumbRoot            string
	tasksRoot            string
	provider             providerCaller
	media                mediaResolver
	client               *http.Client
	upload               *http.Client
	download             *http.Client
	extractor            func(context.Context, string, string) error
	session              storedSession
	syncSuppressions     map[string]int64
	remoteSeen           remoteSeenState
	// Fingerprints the user switched cloud sync off for. Unlike a suppression
	// this does not lapse when the media is transcribed again: it holds until
	// the user switches it back on.
	syncDisabled map[string]bool

	// Relays (relay.go). relayMu guards the records and the driver table;
	// relayActionMu serialises the renderer's cancel and restart, which stop
	// a relay's driver and act on the relay as its only writer.
	relayRoot     string
	relayPoll     time.Duration
	relayMu       sync.RWMutex
	relayActionMu sync.Mutex
	relays        map[string]*relayRecord
	drivers       map[string]*relayDriver
	relayWG       sync.WaitGroup
	lifetime      context.Context
	stopRelays    context.CancelFunc
}

func New(dataDirectory string, provider providerCaller, resolvers ...mediaResolver) (*Service, error) {
	if provider == nil {
		return nil, errors.New("local provider is required")
	}
	root, err := filepath.Abs(dataDirectory)
	if err != nil {
		return nil, err
	}
	service := &Service{
		configPath:           filepath.Join(root, "cloud-session.json"),
		syncSuppressionsPath: filepath.Join(root, "cloud-sync-suppressions.json"),
		remoteSeenPath:       filepath.Join(root, "cloud-sync-remote.json"),
		syncDisabledPath:     filepath.Join(root, "cloud-sync-disabled.json"),
		syncDisabled:         map[string]bool{},
		root:                 root,
		thumbRoot:            filepath.Join(root, "cloud-thumbnails"),
		tasksRoot:            filepath.Join(root, "tasks"),
		provider:             provider,
		client:               &http.Client{Timeout: 30 * time.Second},
		upload:               &http.Client{Timeout: 3 * time.Hour},
		download:             newDownloadClient(),
		extractor: func(ctx context.Context, source, output string) error {
			return extractAudioWithRoot(ctx, root, source, output)
		},
		syncSuppressions: map[string]int64{},
		relayRoot:        filepath.Join(root, "cloud-relays"),
		relayPoll:        2 * time.Second,
		relays:           map[string]*relayRecord{},
		drivers:          map[string]*relayDriver{},
	}
	service.lifetime, service.stopRelays = context.WithCancel(context.Background())
	if len(resolvers) > 0 {
		service.media = resolvers[0]
	}
	service.load()
	service.loadRelays()
	// Every relay that was mid-flight when the app last closed picks up in
	// the leg it was in. A driver waits out a sidecar or a session that is
	// not there yet, so starting them this early is safe.
	for id := range service.relays {
		service.startRelayDriver(id)
	}
	return service, nil
}

// Capabilities are the relay's, not the backend's. The backend declares what
// it runs -- recognition, and nothing past stable.json -- and the relay hands
// the rest to the local provider, so correction, translation, the knowledge
// base and video correction are all things a cloud task does. Whether this
// machine can actually run them is the local provider's runtime report.
func (s *Service) Capabilities() (map[string]any, error) {
	var result map[string]any
	if err := s.authenticatedDo(context.Background(), http.MethodGet, "/v1/capabilities", nil, &result); err != nil {
		return nil, err
	}
	features, _ := result["features"].(map[string]any)
	if features == nil {
		features = map[string]any{}
	}
	for _, name := range []string{"translation", "video_multimodal", "knowledge", "resume"} {
		features[name] = true
	}
	result["features"] = features
	return result, nil
}

func SetTelemetry(s *Service, t *telemetry.Service) {
	s.telemetry = t
	// Answered from the relay record, not the backend: the cloud job is
	// deleted once its result is collected, and the stage a relay failed in
	// may be one that ran on this machine.
	t.SetCloudStatus(func(_ context.Context, id string) (map[string]any, error) {
		record, err := s.relay(id)
		if err != nil {
			return nil, err
		}
		return record.snapshot(), nil
	})
}

// StartTask starts a relay: the cloud recognises the media up to stable.json,
// and this desktop continues from it with the user's own models once the file
// is collected (relay.go). Returns the relay's snapshot, which is what the
// renderer tracks; the cloud job behind it is an implementation detail that is
// deleted as soon as its result is on disk.
func (s *Service) StartTask(localID string, options map[string]any) (result map[string]any, err error) {
	submitted := false
	defer func() {
		if s.telemetry == nil {
			return
		}
		if err == nil {
			if id, ok := result["task_id"].(string); ok {
				s.telemetry.TrackCloud(id)
			}
			return
		}
		var status *httpStatusError
		if !submitted || (errors.As(err, &status) && status.Status >= 400 && status.Status < 500) {
			s.telemetry.StartFailure("cloud", startFailureCode(err))
		}
	}()
	if !validLocalMediaID.MatchString(localID) {
		return nil, errors.New("invalid media id")
	}
	if options == nil {
		options = map[string]any{}
	}
	// A source-text axis is already the recognition result, so a relay would
	// have nothing to send the cloud. The backend refuses it too; saying so here
	// spares the upload.
	if axisOption(options) != nil {
		return nil, errors.New("导入的日文轴不需要云端识别，请选择本机运行")
	}
	if s.media == nil {
		return nil, errors.New("media library is unavailable")
	}
	_, title, fingerprint, duration, err := s.media.ResolveMedia(localID)
	if err != nil {
		return nil, err
	}
	target := "final-srt"
	if value, _ := options["target"].(string); value == "raw-srt" {
		target = value
	}
	id, err := newRelayID()
	if err != nil {
		return nil, err
	}
	jobID, err := s.submitCloudJobTracked(localID, options, &submitted)
	if err != nil {
		return nil, err
	}
	now := relayNow()
	record := &relayRecord{
		Schema: 1, ID: id, LocalID: localID, Title: title, Fingerprint: fingerprint, Duration: duration,
		Target: target, Options: options, Phase: relayPhaseCloud, CloudJobID: jobID,
		State: "queued", Stage: "queued", CreatedAt: now, StartedAt: now, UpdatedAt: now,
	}
	s.relayMu.Lock()
	err = s.appendRelayLinesLocked(record, []relayLine{relayNote("started", "已提交云端识别，等待 GPU 调度")})
	if err == nil {
		err = s.saveRelayLocked(record)
	}
	if err == nil {
		s.relays[id] = record
	}
	s.relayMu.Unlock()
	if err != nil {
		// The job is paid for and running; only this desktop lost track of
		// it. The backend sweeps it, and the ticket makes a resubmission free.
		return nil, fmt.Errorf("云端识别已提交，但本机记录任务失败：%w", err)
	}
	s.startRelayDriver(id)
	return record.clone().snapshot(), nil
}

func (s *Service) submitCloudJob(localID string, options map[string]any) (string, error) {
	submitted := false
	return s.submitCloudJobTracked(localID, options, &submitted)
}

// submitCloudJobTracked extracts the audio, uploads it and submits the
// recognition job. `submitted` turns true once the job request has been sent,
// which is where a failure stops being proof that nothing was created.
func (s *Service) submitCloudJobTracked(localID string, options map[string]any, submitted *bool) (string, error) {
	if s.media == nil {
		return "", errors.New("media library is unavailable")
	}
	path, title, fingerprint, duration, err := s.media.ResolveMedia(localID)
	if err != nil {
		return "", err
	}
	if duration <= 0 || duration > 2*time.Hour.Seconds() {
		return "", errors.New("cloud audio duration must be between 0 and 2 hours")
	}
	uploadRoot := filepath.Join(s.root, "cloud-uploads")
	if err := os.MkdirAll(uploadRoot, 0o700); err != nil {
		return "", err
	}
	work, err := os.MkdirTemp(uploadRoot, "task-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(work)
	audio := filepath.Join(work, "source.m4a")
	if err := s.extractor(context.Background(), path, audio); err != nil {
		return "", err
	}
	stat, err := os.Stat(audio)
	if err != nil {
		return "", err
	}
	var initialized struct {
		ObjectID    string `json:"objectId"`
		UploadURL   string `json:"uploadUrl"`
		ContentType string `json:"contentType"`
	}
	if err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/uploads/init", map[string]any{"filename": filepath.Base(path) + ".m4a", "bytes": stat.Size()}, &initialized); err != nil {
		return "", err
	}
	if !validUploadID.MatchString(initialized.ObjectID) {
		return "", errors.New("cloud returned an invalid upload object id")
	}
	if err := s.putAudio(context.Background(), initialized.UploadURL, initialized.ContentType, audio, stat.Size()); err != nil {
		s.abortUpload(initialized.ObjectID)
		return "", err
	}
	// Recognition only: the cloud stops at stable.json, and correction,
	// translation and the knowledge base belong to the local leg. No cover
	// goes up beside the audio any more -- a job is not a library entry, and
	// the sync after the relay completes carries the cover with the subtitles.
	request := map[string]any{
		"schema":               1,
		"provider":             "cloud",
		"source":               map[string]any{"kind": "uploaded_audio", "object_id": initialized.ObjectID, "title": title, "fingerprint": fingerprint, "duration": duration},
		"target":               "stable",
		"language":             stringOption(options, "language", "ja"),
		"device":               "cuda",
		"gpu_tier":             stringOption(options, "gpu_tier", "auto"),
		"vocal_profile":        "cost",
		"separate":             boolOption(options, "separate", true),
		"cleanup_intermediate": false,
	}
	// Speaker diarization: the level the user chose and an optional cap, only
	// when the run asks for something. The backend computes the acoustic
	// timeline whichever level was asked and records the request; the judge
	// levels run in the local leg, from the timeline it hands back.
	if diarization := diarizationOption(options); diarization != nil {
		request["diarization"] = diarization
	}
	var snapshot map[string]any
	*submitted = true
	if err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/tasks", request, &snapshot); err != nil {
		// Only a refusal proves no job holds the upload. A timeout, a dropped
		// connection or a 5xx can arrive after the backend already reserved
		// and dispatched the job -- deleting the audio then failed it with a
		// 404 on its first download. An upload no job ever claimed is
		// reclaimed by the backend's upload sweep instead.
		var status *httpStatusError
		if errors.As(err, &status) && status.Status >= 400 && status.Status < 500 {
			s.abortUpload(initialized.ObjectID)
			return "", err
		}
		return "", fmt.Errorf("%w (the cloud job may still have been created; it is swept if nothing collects it, and resubmitting the same media does not charge again)", err)
	}
	jobID, _ := snapshot["task_id"].(string)
	if !validJobID.MatchString(jobID) {
		return "", errors.New("cloud returned an invalid job id")
	}
	return jobID, nil
}

func (s *Service) abortUpload(objectID string) {
	if !validUploadID.MatchString(objectID) {
		return
	}
	_ = s.authenticatedDo(context.Background(), http.MethodDelete, "/v1/uploads/"+objectID, nil, nil)
}

func (s *Service) TaskStatus(taskID string) (map[string]any, error) {
	if isLegacyCloudTask(taskID) {
		return legacyCloudSnapshot(taskID), nil
	}
	record, err := s.relay(taskID)
	if err != nil {
		return nil, err
	}
	return record.snapshot(), nil
}

// ListTasks lists this desktop's relays. The backend's own listing only knows
// jobs still waiting to be collected, with no media attached; the relay record
// is what names the media and outlives the job.
func (s *Service) ListTasks() (map[string]any, error) {
	return map[string]any{"schema": 1, "tasks": s.relayList(100)}, nil
}

func (s *Service) TaskEvents(taskID string, afterCursor int) (map[string]any, error) {
	if afterCursor < 0 {
		return nil, errors.New("event cursor must not be negative")
	}
	if isLegacyCloudTask(taskID) {
		return map[string]any{"schema": 1, "task_id": taskID, "events": []any{}, "next_cursor": afterCursor, "state": "failed"}, nil
	}
	return s.relayEvents(taskID, afterCursor)
}

func (s *Service) CancelTask(taskID string) (map[string]any, error) {
	if isLegacyCloudTask(taskID) {
		return legacyCloudSnapshot(taskID), nil
	}
	return s.cancelRelay(taskID)
}

func (s *Service) RetryTask(taskID string) (map[string]any, error) {
	return s.restartWithTelemetry(taskID, "retry")
}

func (s *Service) ResumeTask(taskID string) (map[string]any, error) {
	return s.restartWithTelemetry(taskID, "resume")
}

func (s *Service) restartWithTelemetry(taskID, action string) (map[string]any, error) {
	if isLegacyCloudTask(taskID) {
		return nil, errors.New("旧版云端任务无法继续，请重新提交")
	}
	if s.telemetry != nil {
		s.telemetry.CollectCloud(taskID)
	}
	result, err := s.restartRelay(taskID, action)
	if s.telemetry != nil {
		if err == nil {
			s.telemetry.TrackCloud(taskID)
		} else {
			var status *httpStatusError
			if errors.As(err, &status) && status.Status >= 400 && status.Status < 500 {
				s.telemetry.StartFailure("cloud", startFailureCode(err))
			}
		}
	}
	return result, err
}

// Classify locally; neither backend response text nor transport errors leave the device.
func startFailureCode(err error) string {
	var status *httpStatusError
	if errors.As(err, &status) {
		switch status.Status {
		case 402:
			return "quota_exceeded"
		case 403:
			// Older Modal deployments expose this fixed quota message without a code.
			if status.Detail == "转写任务余量不足" {
				return "quota_exceeded"
			}
			return "auth_failed"
		case 401:
			return "auth_failed"
		case 429:
			return "rate_limited"
		case 408, 504:
			return "timeout"
		case 409:
			return "invalid_state"
		case 400, 404, 422:
			return "invalid_request"
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	var network net.Error
	if errors.As(err, &network) {
		if network.Timeout() {
			return "timeout"
		}
		return "network_error"
	}
	return "unknown"
}

// TaskArtifacts reports a finished relay's subtitles. The local leg projected
// them into the document already; this is the manifest of what it wrote.
func (s *Service) TaskArtifacts(taskID string) (map[string]any, error) {
	return s.relayArtifacts(taskID)
}

func (s *Service) resolveTarget(localID string) (mediaTarget, error) {
	if s.media == nil {
		return mediaTarget{}, errors.New("media library is unavailable")
	}
	path, title, fingerprint, duration, err := s.media.ResolveMedia(localID)
	if err != nil {
		return mediaTarget{}, err
	}
	return mediaTarget{localID: localID, path: path, title: title, fingerprint: fingerprint, duration: duration}, nil
}

// AdoptLibraryEntry projects a finished cloud entry onto local media that this
// installation never ran the task for -- the subtitles were produced on another
// machine, or before a reinstall, and the video has only just been imported or
// relinked here. Without it the card correctly reports the cloud copy while
// still offering to transcribe the media all over again.
func (s *Service) AdoptLibraryEntry(videoID, localID string) error {
	if !validLibraryID.MatchString(videoID) {
		return errors.New("invalid cloud library entry id")
	}
	if !validLocalMediaID.MatchString(localID) {
		return errors.New("invalid media id")
	}
	if s.media == nil {
		return errors.New("media library is unavailable")
	}
	target, err := s.resolveTarget(localID)
	if err != nil {
		return err
	}
	entry, err := s.finishedLibraryEntry(videoID)
	if err != nil {
		return err
	}
	// The identity check is the whole safety of this call: projecting one
	// video's subtitles onto another would silently overwrite the local
	// document with text that belongs to something else.
	if entry.Fingerprint == "" || entry.Fingerprint != target.fingerprint {
		return errors.New("cloud entry belongs to different media")
	}
	return s.adopt(entry, target)
}

// AdoptCloudEntry makes a finished cloud entry editable on a machine that does
// not have the video at all. The subtitles are the deliverable; the media is
// only needed to play them back, so the library records a placeholder for the
// fingerprint and the document hangs on that. Associating a video later fills
// the same entry in while retaining the subtitle record's identity. Returns the
// local media id to open.
func (s *Service) AdoptCloudEntry(videoID string) (string, error) {
	if !validLibraryID.MatchString(videoID) {
		return "", errors.New("invalid cloud library entry id")
	}
	if s.media == nil {
		return "", errors.New("media library is unavailable")
	}
	entry, err := s.finishedLibraryEntry(videoID)
	if err != nil {
		return "", err
	}
	if entry.Fingerprint == "" {
		return "", errors.New("cloud entry has no media fingerprint")
	}
	placeholder, err := s.media.AddPlaceholder(entry.Title, entry.Fingerprint, entry.Duration)
	if err != nil {
		return "", err
	}
	target := mediaTarget{localID: placeholder.ID, path: placeholder.SourcePath, title: placeholder.Title, fingerprint: placeholder.Fingerprint, duration: placeholder.Duration}
	if err := s.adopt(entry, target); err != nil {
		return "", err
	}
	return placeholder.ID, nil
}

func (s *Service) adopt(entry Entry, target mediaTarget) error {
	if _, err := s.projectRemoteArtifacts(entry.ID, target); err != nil {
		return err
	}
	s.adoptThumbnail(entry, target.localID)
	return nil
}

// adoptThumbnail hands the cloud's cover to the local library so an adopted
// card keeps the frame it was already showing while the entry was cloud-only.
// Without it the cover is lost at exactly the moment the subtitles arrive: a
// cloud-only card draws from the bucket, a local card draws from the library's
// thumbnail directory, and adoption moves the entry from the first to the
// second. Media adopted onto a placeholder has no file to cut a frame from
// either, so nothing would ever put one back.
//
// Best-effort, like the upload that put the cover there: an entry whose desktop
// sent none, a backend that no longer has it, or local media that already has
// its own frame all leave the card exactly as it was. The bytes usually come
// from the on-disk cache the cloud-only card already filled.
func (s *Service) adoptThumbnail(entry Entry, localID string) {
	if !entry.ThumbnailAvailable || s.media == nil {
		return
	}
	dataURL, err := s.ThumbnailDataURL(entry.ID)
	if err != nil {
		return
	}
	_ = s.media.EnsureThumbnail(localID, dataURL)
}

func (s *Service) finishedLibraryEntry(videoID string) (Entry, error) {
	remote, err := s.Library()
	if err != nil {
		return Entry{}, err
	}
	for _, entry := range remote {
		if entry.ID != videoID {
			continue
		}
		// Whether subtitles can be fetched, not whether the newest attempt
		// succeeded. Gating on the status left an entry's finished subtitles
		// unreachable for as long as a re-transcribe of the same media was in
		// flight or had failed -- the files were still there, and the manifest
		// still named them.
		if !entry.HasSubtitles() {
			return Entry{}, errors.New("cloud entry has no finished subtitles")
		}
		return entry, nil
	}
	return Entry{}, errors.New("cloud library has no such entry")
}

// projectRemoteArtifacts downloads one cloud library entry's subtitles, keeps
// a local copy under the task root and hands the set to the local provider as a
// document for the target. Retrieval deliberately does not reject old cloud
// artifacts for digest or SRT-format differences.
func (s *Service) projectRemoteArtifacts(taskID string, target mediaTarget) (map[string]any, error) {
	var remote remoteArtifactManifest
	if err := s.authenticatedDo(context.Background(), http.MethodGet, "/v1/library/"+taskID+"/artifacts", nil, &remote); err != nil {
		return nil, err
	}
	directory := filepath.Join(s.tasksRoot, "cloud", taskID)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, err
	}
	contents := map[string]string{}
	local := map[string]any{}
	total := int64(0)
	for _, name := range []string{"stable_json", "raw_srt", "annotated_csv", "final_srt"} {
		item, ok := remote.Artifacts[name]
		if !ok {
			continue
		}
		content, err := s.authenticatedText(context.Background(), item.URI)
		if err != nil {
			return nil, err
		}
		digest := sha256.Sum256(content)
		actualSHA := hex.EncodeToString(digest[:])
		total += int64(len(content))
		if total > maxArtifactBytes {
			return nil, errors.New("cloud artifacts exceed the 32 MB limit")
		}
		contents[name] = string(content)
		suffixes := map[string]string{"stable_json": ".stable.json", "raw_srt": ".raw.srt", "annotated_csv": ".annotated.csv", "final_srt": ".srt"}
		artifactPath := filepath.Join(directory, name+suffixes[name])
		temporary, err := os.CreateTemp(directory, "."+name+"-*.part")
		if err != nil {
			return nil, err
		}
		temporaryName := temporary.Name()
		if err := temporary.Chmod(0o600); err == nil {
			_, err = temporary.Write(content)
		}
		closeErr := temporary.Close()
		if err == nil {
			err = closeErr
		}
		if err == nil {
			err = os.Rename(temporaryName, artifactPath)
		}
		if err != nil {
			_ = os.Remove(temporaryName)
			return nil, err
		}
		local[name] = map[string]any{"uri": localFileURI(artifactPath), "sha256": actualSHA, "bytes": len(content)}
	}
	if len(contents) == 0 {
		return nil, errors.New("cloud task returned no subtitle artifacts")
	}
	projection := map[string]any{"video_id": target.localID, "task_id": taskID, "engine_commit": remote.EngineCommit, "title": target.title, "fingerprint": target.fingerprint, "duration": target.duration, "source_path": target.path, "artifacts": contents, "relaxed_srt": true}
	var document map[string]any
	if err := s.provider.DoJSON(context.Background(), http.MethodPost, "/v1/documents/project", projection, &document); err != nil {
		return nil, err
	}
	return map[string]any{"schema": 1, "task_id": taskID, "engine_commit": remote.EngineCommit, "artifacts": local}, nil
}

// verifyArtifactContent preserves the manifest's byte-level integrity while
// repairing the one transformation a text HTTP response is allowed to have
// introduced: universal-newline conversion. A candidate is accepted only when
// its exact bytes reproduce the signed digest.
func verifyArtifactContent(content []byte, expectedSHA string) ([]byte, string, bool) {
	if len(expectedSHA) != 64 {
		return nil, "", false
	}
	verify := func(candidate []byte) (string, bool) {
		digest := sha256.Sum256(candidate)
		actual := hex.EncodeToString(digest[:])
		return actual, strings.EqualFold(actual, expectedSHA)
	}
	if actual, ok := verify(content); ok {
		return content, actual, true
	}
	if bytes.Contains(content, []byte("\r\n")) {
		normalized := bytes.ReplaceAll(content, []byte("\r\n"), []byte("\n"))
		if actual, ok := verify(normalized); ok {
			return normalized, actual, true
		}
	}
	if bytes.Contains(content, []byte("\n")) {
		expanded := make([]byte, 0, len(content)+bytes.Count(content, []byte("\n")))
		for index, value := range content {
			if value == '\n' && (index == 0 || content[index-1] != '\r') {
				expanded = append(expanded, '\r')
			}
			expanded = append(expanded, value)
		}
		if actual, ok := verify(expanded); ok {
			return expanded, actual, true
		}
	}
	return nil, "", false
}

func localFileURI(path string) string {
	value := filepath.ToSlash(path)
	if filepath.VolumeName(path) != "" && !strings.HasPrefix(value, "/") {
		value = "/" + value
	}
	return (&url.URL{Scheme: "file", Path: value}).String()
}

func (s *Service) taskGet(taskID, suffix string) (map[string]any, error) {
	if !validLibraryID.MatchString(taskID) {
		return nil, errors.New("invalid cloud task id")
	}
	endpoint := "/v1/tasks/" + taskID
	if suffix != "" {
		endpoint += "/" + suffix
	}
	var result map[string]any
	err := s.authenticatedDo(context.Background(), http.MethodGet, endpoint, nil, &result)
	return result, err
}

func (s *Service) taskAction(taskID, action string) (map[string]any, error) {
	if !validLibraryID.MatchString(taskID) {
		return nil, errors.New("invalid cloud task id")
	}
	var result map[string]any
	err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/tasks/"+taskID+"/"+action, map[string]any{}, &result)
	return result, err
}

func (s *Service) Session() Session {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return Session{Authenticated: s.session.Key != "", Backend: s.session.Backend}
}

func (s *Service) RefreshSession() (Session, error) {
	s.mu.RLock()
	stored := s.session
	s.mu.RUnlock()
	if stored.Backend == "" || stored.Key == "" {
		return Session{Authenticated: false}, nil
	}
	var result Session
	if err := s.do(context.Background(), stored, http.MethodGet, "/v1/session", nil, &result); err != nil {
		return Session{}, err
	}
	result.Authenticated = true
	result.Backend = stored.Backend
	s.mergeLocalLibrary(&result)
	return result, nil
}

func (s *Service) Login(backend, key string) (Session, error) {
	resolved, err := validBackend(backend)
	if err != nil {
		return Session{}, err
	}
	key = strings.TrimSpace(key)
	if key == "" || len(key) > 256 {
		return Session{}, errors.New("cloud key must contain 1 to 256 characters")
	}
	var result Session
	if err := s.do(context.Background(), storedSession{Backend: resolved, Key: key}, http.MethodGet, "/v1/session", nil, &result); err != nil {
		return Session{}, err
	}
	result.Authenticated = true
	result.Backend = resolved
	s.mu.Lock()
	previous := s.session
	s.session = storedSession{Backend: resolved, Key: key}
	if err := s.saveLocked(); err != nil {
		s.session = previous
		s.mu.Unlock()
		return Session{}, err
	}
	s.mu.Unlock()
	s.mergeLocalLibrary(&result)
	return result, nil
}

func (s *Service) Logout() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.session = storedSession{}
	if err := os.Remove(s.configPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func (s *Service) Library() ([]Entry, error) {
	var envelope struct {
		Videos []Entry `json:"videos"`
	}
	if err := s.authenticatedDo(context.Background(), http.MethodGet, "/v1/library", nil, &envelope); err != nil {
		return nil, err
	}
	if envelope.Videos == nil {
		envelope.Videos = []Entry{}
	}
	return envelope.Videos, nil
}

func (s *Service) DeleteLibraryEntry(identifier string) error {
	if !validLibraryID.MatchString(identifier) {
		return errors.New("invalid cloud library entry id")
	}
	s.syncMu.Lock()
	defer s.syncMu.Unlock()
	entries, err := s.Library()
	if err != nil {
		return err
	}
	fingerprint := ""
	for _, entry := range entries {
		if entry.ID == identifier {
			fingerprint = entry.Fingerprint
			break
		}
	}
	deletedAt := time.Now().UnixMilli()
	previousSuppressedAt := int64(0)
	suppressionExisted := false
	if fingerprint != "" {
		s.mu.Lock()
		previousSuppressedAt, suppressionExisted = s.syncSuppressions[fingerprint]
		s.syncSuppressions[fingerprint] = deletedAt
		if err := s.saveSyncSuppressionsLocked(); err != nil {
			if suppressionExisted {
				s.syncSuppressions[fingerprint] = previousSuppressedAt
			} else {
				delete(s.syncSuppressions, fingerprint)
			}
			s.mu.Unlock()
			return err
		}
		s.mu.Unlock()
	}
	if err := s.authenticatedDo(context.Background(), http.MethodDelete, "/v1/library/"+identifier, nil, nil); err != nil {
		if fingerprint != "" {
			s.mu.Lock()
			if suppressionExisted {
				s.syncSuppressions[fingerprint] = previousSuppressedAt
			} else {
				delete(s.syncSuppressions, fingerprint)
			}
			_ = s.saveSyncSuppressionsLocked()
			s.mu.Unlock()
		}
		return err
	}
	_ = os.Remove(filepath.Join(s.thumbRoot, identifier+".jpg"))
	return nil
}

func (s *Service) AdminKeys() ([]AdminKey, error) {
	var envelope struct {
		Keys []AdminKey `json:"keys"`
	}
	if err := s.authenticatedDo(context.Background(), http.MethodGet, "/v1/admin/keys", nil, &envelope); err != nil {
		return nil, err
	}
	if envelope.Keys == nil {
		envelope.Keys = []AdminKey{}
	}
	return envelope.Keys, nil
}

func (s *Service) CreateAdminKey(name, key string, remaining int) (IssuedAdminKey, error) {
	name = strings.TrimSpace(name)
	key = strings.TrimSpace(key)
	if name == "" || utf8.RuneCountInString(name) > 100 {
		return IssuedAdminKey{}, errors.New("key name must contain 1 to 100 characters")
	}
	if !validPlaintextKey.MatchString(key) {
		return IssuedAdminKey{}, errors.New("custom key must contain 1 to 256 URL-safe characters")
	}
	if remaining < 0 || remaining > 1_000_000 {
		return IssuedAdminKey{}, errors.New("remaining must be between 0 and 1000000")
	}
	var result IssuedAdminKey
	err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/admin/keys", map[string]any{
		"name": name, "key": key, "remaining": remaining,
	}, &result)
	return result, err
}

func (s *Service) UpdateAdminKey(identifier, name string, remaining int) (AdminKey, error) {
	name = strings.TrimSpace(name)
	if !validPlaintextKey.MatchString(identifier) {
		return AdminKey{}, errors.New("invalid key id")
	}
	if name == "" || utf8.RuneCountInString(name) > 100 {
		return AdminKey{}, errors.New("key name must contain 1 to 100 characters")
	}
	if remaining < 0 || remaining > 1_000_000 {
		return AdminKey{}, errors.New("remaining must be between 0 and 1000000")
	}
	var result AdminKey
	err := s.authenticatedDo(context.Background(), http.MethodPut, "/v1/admin/keys/"+identifier, map[string]any{
		"name": name, "remaining": remaining,
	}, &result)
	return result, err
}

func (s *Service) DeleteAdminKey(identifier string) error {
	if !validPlaintextKey.MatchString(identifier) {
		return errors.New("invalid key id")
	}
	return s.authenticatedDo(context.Background(), http.MethodDelete, "/v1/admin/keys/"+identifier, nil, nil)
}

// SyncLocalTask reads only artifacts reported by the Local Provider and only
// when their resolved paths remain under Nonoka X's task root.
func (s *Service) SyncLocalTask(taskID, localID, fingerprint, title string, duration float64) (Entry, error) {
	// A finished relay syncs what its local leg produced: the cloud library
	// is only ever filled from this desktop, whichever machine recognised.
	if validRelayID.MatchString(taskID) {
		localTask, err := s.relayLocalTask(taskID)
		if err != nil {
			return Entry{}, err
		}
		taskID = localTask
	}
	if !validTaskID.MatchString(taskID) {
		return Entry{}, errors.New("invalid local task id")
	}
	if strings.TrimSpace(fingerprint) == "" || len(fingerprint) > 256 {
		return Entry{}, errors.New("media fingerprint is required")
	}
	s.syncMu.Lock()
	defer s.syncMu.Unlock()
	var manifest artifactManifest
	endpoint := "/v1/tasks/" + taskID + "/artifacts"
	if err := s.provider.DoJSON(context.Background(), http.MethodGet, endpoint, nil, &manifest); err != nil {
		return Entry{}, err
	}
	manifest.TaskID = taskID
	return s.syncArtifactManifest(manifest, localID, fingerprint, title, duration)
}

func (s *Service) syncArtifactManifest(manifest artifactManifest, localID, fingerprint, title string, duration float64) (Entry, error) {
	if !validTaskID.MatchString(manifest.TaskID) {
		return Entry{}, errors.New("invalid local task id in artifact manifest")
	}
	// Paths and ages first, contents only once the upload is decided: every
	// library refresh walks the suppressed entries, and reading their
	// subtitles just to discard them made each background poll cost a full
	// read of every deleted entry's artifacts.
	paths := map[string]string{}
	newestArtifact := int64(0)
	for _, name := range []string{"stable_json", "raw_srt", "annotated_csv", "final_srt"} {
		item, ok := manifest.Artifacts[name]
		if !ok {
			continue
		}
		path, err := s.safeArtifactPath(item.URI)
		if err != nil {
			return Entry{}, fmt.Errorf("%s: %w", name, err)
		}
		info, err := os.Stat(path)
		if err != nil {
			return Entry{}, err
		}
		if info.ModTime().UnixMilli() > newestArtifact {
			newestArtifact = info.ModTime().UnixMilli()
		}
		paths[name] = path
	}
	if len(paths) == 0 {
		return Entry{}, errors.New("local task has no syncable artifacts")
	}
	s.mu.Lock()
	suppressedAt := s.syncSuppressions[fingerprint]
	if s.syncDisabled[fingerprint] || (suppressedAt > 0 && newestArtifact <= suppressedAt) {
		s.mu.Unlock()
		return Entry{Fingerprint: fingerprint, Status: "suppressed", Source: "local_sync"}, nil
	}
	s.mu.Unlock()
	contents := map[string]string{}
	total := int64(0)
	for name, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return Entry{}, err
		}
		total += int64(len(data))
		if total > maxArtifactBytes {
			return Entry{}, errors.New("local task artifacts exceed the sync limit")
		}
		contents[name] = string(data)
	}
	s.mu.Lock()
	// Re-read under the lock it is cleared under: the files were read without it.
	suppressedAt = s.syncSuppressions[fingerprint]
	if suppressedAt > 0 {
		delete(s.syncSuppressions, fingerprint)
		if err := s.saveSyncSuppressionsLocked(); err != nil {
			s.syncSuppressions[fingerprint] = suppressedAt
			s.mu.Unlock()
			return Entry{}, err
		}
	}
	s.mu.Unlock()
	request := syncRequest{
		LocalID: localID, Fingerprint: fingerprint, Title: strings.TrimSpace(title), Duration: duration,
		TaskID: manifest.TaskID, EngineCommit: manifest.EngineCommit, Artifacts: contents,
		Thumbnail: s.localCover(localID),
	}
	var result Entry
	if err := s.authenticatedDo(context.Background(), http.MethodPost, "/v1/library/sync", request, &result); err != nil {
		return Entry{}, err
	}
	return result, nil
}

// mergeLocalLibrary uploads completed local subtitle artifacts that are not
// already represented by the authenticated key's remote fingerprint set. It
// never uploads source video and does not spend cloud task quota.
func (s *Service) mergeLocalLibrary(session *Session) {
	catalog, ok := s.media.(interface{ List() []library.Entry })
	if !ok {
		return
	}
	s.syncMu.Lock()
	defer s.syncMu.Unlock()
	// /v1/session carries the library now, and this is the second of the two
	// places that used to fetch it again immediately after. A backend that does
	// not send one is still asked, so an older deployment keeps working.
	remote := session.Videos
	if remote == nil {
		fetched, err := s.Library()
		if err != nil {
			session.SyncError = err.Error()
			return
		}
		remote = fetched
		session.Videos = fetched
	}
	listedAt := time.Now().UnixMilli()
	remoteFingerprints := make(map[string]struct{}, len(remote))
	for _, entry := range remote {
		if entry.Fingerprint != "" {
			remoteFingerprints[entry.Fingerprint] = struct{}{}
		}
	}
	s.mu.RLock()
	account := accountTag(s.session)
	previous := s.remoteSeen
	s.mu.RUnlock()
	seenBefore := map[string]struct{}{}
	if previous.Account == account {
		for _, fingerprint := range previous.Fingerprints {
			seenBefore[fingerprint] = struct{}{}
		}
	}
	for _, entry := range catalog.List() {
		if !entry.DocumentAvailable || entry.Fingerprint == "" || !validLocalMediaID.MatchString(entry.ID) {
			continue
		}
		if _, exists := remoteFingerprints[entry.Fingerprint]; exists || s.syncDisabledFor(entry.Fingerprint) {
			session.SyncSkipped++
			continue
		}
		if _, seen := seenBefore[entry.Fingerprint]; seen {
			// On the previous listing and gone from this one: another device
			// deleted it. Uploading it again would undo that deletion on every
			// machine, so it is held back like a deletion made here -- as of
			// that listing, so subtitles produced here since still go up.
			if err := s.suppressSync(entry.Fingerprint, previous.ListedAt); err != nil {
				session.SyncFailed++
				continue
			}
		}
		data, readErr := os.ReadFile(filepath.Join(s.root, "documents", entry.ID, "artifacts.json"))
		if readErr != nil {
			session.SyncFailed++
			continue
		}
		var manifest artifactManifest
		if json.Unmarshal(data, &manifest) != nil || !validTaskID.MatchString(manifest.TaskID) {
			session.SyncFailed++
			continue
		}
		synced, syncErr := s.syncArtifactManifest(manifest, entry.ID, entry.Fingerprint, entry.Title, entry.Duration)
		if syncErr != nil {
			session.SyncFailed++
			continue
		}
		if synced.Status == "suppressed" {
			session.SyncSkipped++
			continue
		}
		remoteFingerprints[entry.Fingerprint] = struct{}{}
		session.Synced++
	}
	session.CloudVideos = len(remote) + session.Synced
	if session.Synced > 0 {
		// Only now is the list the caller was handed actually out of date, and
		// only on the first refresh after a local run -- so the re-read is paid
		// exactly when something was added, rather than on every poll.
		if refreshed, err := s.Library(); err == nil {
			session.Videos = refreshed
			session.CloudVideos = len(refreshed)
		}
	}
	s.rememberRemote(account, listedAt, remoteFingerprints)
}

// suppressSync holds a fingerprint's local subtitles back from the cloud for
// everything produced up to at. An existing later suppression wins.
func (s *Service) suppressSync(fingerprint string, at int64) error {
	if at <= 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	previous, existed := s.syncSuppressions[fingerprint]
	if previous >= at {
		return nil
	}
	s.syncSuppressions[fingerprint] = at
	if err := s.saveSyncSuppressionsLocked(); err != nil {
		if existed {
			s.syncSuppressions[fingerprint] = previous
		} else {
			delete(s.syncSuppressions, fingerprint)
		}
		return err
	}
	return nil
}

func (s *Service) rememberRemote(account string, listedAt int64, fingerprints map[string]struct{}) {
	listed := make([]string, 0, len(fingerprints))
	for fingerprint := range fingerprints {
		listed = append(listed, fingerprint)
	}
	sort.Strings(listed)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.remoteSeen = remoteSeenState{Account: account, ListedAt: listedAt, Fingerprints: listed}
	if err := s.saveRemoteSeenLocked(); err != nil {
		log.Printf("cloud: cannot save the remote library snapshot: %v", err)
	}
}

func (s *Service) safeArtifactPath(uri string) (string, error) {
	parsed, err := url.Parse(uri)
	if err != nil || parsed.Scheme != "file" || parsed.Host != "" {
		return "", errors.New("artifact URI must be a local file URI")
	}
	path, err := url.PathUnescape(parsed.Path)
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		path = windowsPathFromFileURI(path)
	}
	resolved, err := filepath.Abs(filepath.FromSlash(path))
	if err != nil {
		return "", err
	}
	if withinTaskRoot(s.tasksRoot, resolved) {
		return resolved, nil
	}
	if rebased, ok := s.rebaseArtifactPath(resolved); ok {
		return rebased, nil
	}
	return "", errors.New("artifact path escapes the local task directory")
}

func withinTaskRoot(tasksRoot, resolved string) bool {
	relative, err := filepath.Rel(tasksRoot, resolved)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return false
	}
	return true
}

// rebaseArtifactPath re-anchors a manifest path recorded under an earlier data
// directory. Manifests store absolute file URIs, so moving the data root -- the
// Finoka to Nonoka X rename moved every existing one -- leaves each stored path
// naming a tree that no longer exists even though the artifacts themselves
// travelled with the root and kept their position under it. Only the tail below
// the recorded path's own task directory is reused: it is rejoined onto the
// current root, and accepted only once it still resolves inside that root and
// names a file that is really there. A path that never belonged to a task tree
// has no such tail and stays rejected.
func (s *Service) rebaseArtifactPath(resolved string) (string, bool) {
	segments := strings.Split(filepath.ToSlash(resolved), "/")
	for index, segment := range segments {
		if segment != "tasks" || index+1 >= len(segments) {
			continue
		}
		candidate := filepath.Join(append([]string{s.tasksRoot}, segments[index+1:]...)...)
		if !withinTaskRoot(s.tasksRoot, candidate) {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return candidate, true
		}
	}
	return "", false
}

func windowsPathFromFileURI(path string) string {
	// RFC 8089 file URIs encode a Windows drive path as /C:/path. Windows
	// filepath functions expect C:/path; retaining the URI's leading slash
	// makes filepath.Abs resolve it against the current drive instead.
	if len(path) >= 3 && path[0] == '/' && path[2] == ':' && ((path[1] >= 'A' && path[1] <= 'Z') || (path[1] >= 'a' && path[1] <= 'z')) {
		return path[1:]
	}
	return path
}

func validUploadTarget(uploadURL string) error {
	parsed, err := url.Parse(uploadURL)
	if err != nil || parsed.Host == "" || parsed.User != nil {
		return errors.New("cloud returned an invalid upload URL")
	}
	loopback := parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "localhost" || parsed.Hostname() == "::1"
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && loopback) {
		return errors.New("upload URL must use HTTPS")
	}
	return nil
}

func (s *Service) authenticatedDo(ctx context.Context, method, path string, body, result any) error {
	s.mu.RLock()
	session := s.session
	s.mu.RUnlock()
	if session.Backend == "" || session.Key == "" {
		return errors.New("cloud login is required")
	}
	return s.do(ctx, session, method, path, body, result)
}

// httpStatusError is a response the backend (or its gateway) actually sent, as
// opposed to a transport failure, so callers can tell a refusal from an
// outcome they cannot know.
type httpStatusError struct {
	Status int
	Detail string
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("cloud HTTP %d: %s", e.Status, e.Detail)
}

func (s *Service) do(ctx context.Context, session storedSession, method, path string, body, result any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, session.Backend+path, reader)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+session.Key)
	request.Header.Set("Accept", "application/json")
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := s.client.Do(request)
	if err != nil {
		return fmt.Errorf("call cloud backend: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return err
	}
	if len(payload) > maxResponseBytes {
		return errors.New("cloud response exceeds size limit")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var failure struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(payload, &failure)
		if failure.Detail == "" {
			failure.Detail = strings.TrimSpace(string(payload))
		}
		return &httpStatusError{Status: response.StatusCode, Detail: failure.Detail}
	}
	if result != nil {
		if err := json.Unmarshal(payload, result); err != nil {
			return err
		}
	}
	return nil
}

func (s *Service) putAudio(ctx context.Context, uploadURL, contentType, path string, size int64) error {
	if err := validUploadTarget(uploadURL); err != nil {
		return err
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, uploadURL, file)
	if err != nil {
		return err
	}
	request.ContentLength = size
	if contentType == "" {
		contentType = "audio/mp4"
	}
	request.Header.Set("Content-Type", contentType)
	response, err := s.upload.Do(request)
	if err != nil {
		return fmt.Errorf("upload cloud audio: %w", err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("upload cloud audio: HTTP %d", response.StatusCode)
	}
	return nil
}

// localCover is the frame the local library drew for this media, as the JPEG
// bytes the library sync sends. Best-effort by design: a source with no video
// stream, a missing ffmpeg, or a placeholder that has no file to draw from all
// leave the entry without a cover rather than failing the work it rides along
// with.
func (s *Service) localCover(localID string) []byte {
	if s.media == nil {
		return nil
	}
	dataURL, err := s.media.ThumbnailDataURL(localID)
	if err != nil {
		return nil
	}
	image, err := decodeJPEGDataURL(dataURL)
	if err != nil {
		return nil
	}
	return image
}

// ThumbnailDataURL returns the cover of a cloud library entry, mirroring the
// local library accessor of the same name so the renderer treats both alike.
// The bytes are cached on disk: a cover never changes for a given upload, and
// the cloud library is re-read on every refresh.
func (s *Service) ThumbnailDataURL(videoID string) (string, error) {
	if !validLibraryID.MatchString(videoID) {
		return "", errors.New("invalid cloud library entry id")
	}
	cached := filepath.Join(s.thumbRoot, videoID+".jpg")
	if data, err := os.ReadFile(cached); err == nil && len(data) > 0 && len(data) <= maxThumbnailBytes {
		return jpegDataURL(data), nil
	}
	data, err := s.authenticatedBytes(context.Background(), "/v1/library/"+videoID+"/thumbnail")
	if err != nil {
		return "", err
	}
	if len(data) == 0 {
		return "", errors.New("cloud returned an empty thumbnail")
	}
	if err := os.MkdirAll(s.thumbRoot, 0o700); err == nil {
		_ = os.WriteFile(cached, data, 0o600)
	}
	return jpegDataURL(data), nil
}

func (s *Service) authenticatedBytes(ctx context.Context, path string) ([]byte, error) {
	s.mu.RLock()
	session := s.session
	s.mu.RUnlock()
	if session.Backend == "" || session.Key == "" {
		return nil, errors.New("cloud login is required")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, session.Backend+path, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+session.Key)
	response, err := s.client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("call cloud backend: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxThumbnailBytes+1))
	if err != nil {
		return nil, err
	}
	if len(payload) > maxThumbnailBytes {
		return nil, errors.New("cloud thumbnail exceeds size limit")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("cloud HTTP %d", response.StatusCode)
	}
	return payload, nil
}

func jpegDataURL(data []byte) string {
	return "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(data)
}

func decodeJPEGDataURL(value string) ([]byte, error) {
	encoded, found := strings.CutPrefix(value, "data:image/jpeg;base64,")
	if !found {
		return nil, errors.New("local thumbnail is not a JPEG data URL")
	}
	image, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, err
	}
	if len(image) == 0 || len(image) > maxThumbnailBytes {
		return nil, errors.New("local thumbnail is empty or too large")
	}
	return image, nil
}

func newDownloadClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = artifactHeaderTimeout
	return &http.Client{Transport: transport}
}

func (s *Service) authenticatedText(ctx context.Context, endpoint string) ([]byte, error) {
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.IsAbs() || parsed.Host != "" || !(strings.HasPrefix(parsed.Path, "/v1/tasks/") || strings.HasPrefix(parsed.Path, "/v1/library/")) {
		return nil, errors.New("cloud returned an invalid artifact URI")
	}
	s.mu.RLock()
	session := s.session
	s.mu.RUnlock()
	if session.Backend == "" || session.Key == "" {
		return nil, errors.New("cloud login is required")
	}
	ctx, cancel := context.WithTimeout(ctx, artifactDownloadTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, session.Backend+parsed.RequestURI(), nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+session.Key)
	response, err := s.download.Do(request)
	if err != nil {
		return nil, fmt.Errorf("download cloud artifact: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, maxArtifactBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read cloud artifact: %w", err)
	}
	if len(payload) > maxArtifactBytes {
		return nil, errors.New("cloud artifact exceeds the 32 MB limit")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("download cloud artifact: HTTP %d", response.StatusCode)
	}
	return payload, nil
}

func extractAudio(ctx context.Context, source, output string) error {
	return extractAudioWithRoot(ctx, "", source, output)
}

// The upload must not add loss of its own, and the cheapest way to add no loss
// is to not re-encode at all.
//
// The command this replaced re-encoded everything to AAC 160k. On a stereo
// stream that is 80 kbps per channel, and the audio track of a downloaded video
// has already been through a lossy encoder once, so it was a second generation:
// measured 1.2 dB of SI-SDR in the 16 kHz band the ASR chain actually reads.
// Re-encoding losslessly instead removes that 1.2 dB -- and so does copying the
// track, for a fifth of the bytes and none of the CPU. Copying is therefore not
// a compromise on "lossless upload"; it is the strongest form of it.
//
// Two branches, in the order they are tried:
//
//	copy   Everything already compressed. `-f mp4` is load-bearing: the `.m4a`
//	       extension otherwise selects the `ipod` muxer, which admits only AAC
//	       and ALAC, while the generic MP4 muxer takes Opus, MP3, FLAC, AC-3 and
//	       E-AC-3 as well -- all verified to survive the cloud's decode.
//	alac   Uncompressed sources, where copying would upload raw PCM at twice the
//	       size for the same signal, and anything the muxer refuses (WMA, and
//	       whatever else turns up). Lossless either way.
//
// Deciding by attempt rather than by a codec allowlist: the muxer already knows
// which codecs it takes, and a list here would be one more thing to keep in
// sync with whatever ffmpeg the user happens to have installed. The failed
// attempt costs nothing -- the muxer rejects the codec while opening the
// output, before any audio is read.
//
// Neither branch passes -ar, -ac or -sample_fmt: nothing here may resample,
// downmix or requantize. The cloud's prepare phase decodes to the separator's
// tier rate itself, and that resample is then the only one in the chain.
//
// Separate functions so the flags are testable. What regressed here before was
// a bitrate flag sitting in the middle of a long exec line that nothing read.
func cloudAudioCopyArgs(source, output string) []string {
	return cloudAudioArgs(source, output, "copy")
}

func cloudAudioLosslessArgs(source, output string) []string {
	return cloudAudioArgs(source, output, "alac")
}

func cloudAudioArgs(source, output, codec string) []string {
	return []string{
		"-v", "error", "-y",
		"-i", source,
		"-vn", "-sn", "-dn",
		"-c:a", codec,
		"-f", "mp4",
		"-movflags", "+faststart",
		output,
	}
}

// isUncompressedCodec reports whether a stream in this codec is stored as raw
// or near-raw samples, so that copying it would upload roughly twice the bytes
// ALAC needs for the same signal. Everything else -- lossy or losslessly
// compressed -- is cheaper and no worse to copy verbatim.
func isUncompressedCodec(codec string) bool {
	codec = strings.TrimSpace(strings.ToLower(codec))
	return strings.HasPrefix(codec, "pcm_") ||
		strings.HasPrefix(codec, "adpcm_") ||
		codec == "wavpack"
}

// sourceAudioIsUncompressed reports whether copying the source track would
// upload raw PCM. Best effort: an unreadable probe answers false, which only
// means the copy attempt happens and, for PCM, succeeds at twice the size.
func sourceAudioIsUncompressed(ctx context.Context, dataDirectory, source string) bool {
	ffprobe, err := managedtools.Find(dataDirectory, "ffprobe")
	if err != nil {
		return false
	}
	command := exec.CommandContext(ctx, ffprobe,
		"-v", "error",
		"-select_streams", "a:0",
		"-show_entries", "stream=codec_name",
		"-of", "default=noprint_wrappers=1:nokey=1",
		source,
	)
	configureCloudCommand(command)
	output, err := command.Output()
	if err != nil {
		return false
	}
	return isUncompressedCodec(string(output))
}

func extractAudioWithRoot(ctx context.Context, dataDirectory, source, output string) error {
	ffmpeg, err := managedtools.Find(dataDirectory, "ffmpeg")
	if err != nil {
		return errors.New("ffmpeg is required to prepare cloud audio")
	}
	bounded, cancel := context.WithTimeout(ctx, 3*time.Hour)
	defer cancel()

	run := func(args []string) ([]byte, error) {
		command := exec.CommandContext(bounded, ffmpeg, args...)
		configureCloudCommand(command)
		return command.CombinedOutput()
	}

	if !sourceAudioIsUncompressed(bounded, dataDirectory, source) {
		if _, err := run(cloudAudioCopyArgs(source, output)); err == nil {
			return nil
		}
		// The muxer refused the codec. Fall through and re-encode losslessly;
		// the copy attempt wrote nothing worth keeping.
		_ = os.Remove(output)
	}
	result, err := run(cloudAudioLosslessArgs(source, output))
	if err != nil {
		return fmt.Errorf("extract cloud audio: %s", strings.TrimSpace(string(result)))
	}
	return nil
}

func stringOption(options map[string]any, key, fallback string) string {
	if value, ok := options[key].(string); ok && strings.TrimSpace(value) != "" {
		return strings.TrimSpace(value)
	}
	return fallback
}

// boolOption reads a switch the renderer sends as a JSON boolean. Anything
// else falls back, so a malformed value never turns separation off.
func boolOption(options map[string]any, key string, fallback bool) bool {
	if value, ok := options[key].(bool); ok {
		return value
	}
	return fallback
}

func numberOption(options map[string]any, key string, fallback int) int {
	switch value := options[key].(type) {
	case int:
		if value >= 4 && value <= 24 {
			return value
		}
	case float64:
		if value >= 4 && value <= 24 {
			return int(value)
		}
	}
	return fallback
}

// cloudBackend is where the cloud lives; the renderer's DEFAULT_CLOUD_BACKEND
// says the same.
const cloudBackend = "https://cloud.nonoka.online"

// retiredBackend reports a session saved against the Modal app the backend
// moved away from.
func retiredBackend(backend string) bool {
	parsed, err := url.Parse(backend)
	return err == nil && strings.HasSuffix(strings.ToLower(parsed.Hostname()), ".modal.run")
}

func validBackend(value string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", errors.New("cloud backend must be an absolute URL without credentials, query, or fragment")
	}
	host := parsed.Hostname()
	loopback := host == "127.0.0.1" || host == "localhost" || host == "::1"
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && loopback) {
		return "", errors.New("cloud backend must use HTTPS; HTTP is allowed only for loopback development")
	}
	parsed.Path = strings.TrimRight(parsed.Path, "/")
	return strings.TrimRight(parsed.String(), "/"), nil
}

// readState decodes one persisted file. A file that is missing, unreadable or
// damaged all mean the same thing here -- no state -- and `value` is left as
// the caller initialised it.
//
// Nothing these files hold is irreplaceable: the session is a key the user
// can enter again, and the suppressions rebuild from use. What
// refusing to start over one of them costs is the whole desktop, which is how
// a zero-byte `cloud-sync-suppressions.json` turned into `unexpected end of
// JSON input` before main() had drawn a window. The writer cannot produce that
// file -- it marshals at least `{}`, writes a temporary and renames -- so the
// damage arrives from outside the app, and the app has to survive it.
// `taskhistory` already treats its own file this way.
func readState(path string, value any) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			log.Printf("cloud: ignoring unreadable %s: %v", filepath.Base(path), err)
		}
		return false
	}
	if err := json.Unmarshal(data, value); err != nil {
		log.Printf("cloud: ignoring damaged %s: %v", filepath.Base(path), err)
		return false
	}
	return true
}

func (s *Service) load() {
	var stored storedSession
	if readState(s.configPath, &stored) {
		backend, backendErr := validBackend(stored.Backend)
		if backendErr == nil && strings.TrimSpace(stored.Key) != "" {
			stored.Backend = backend
			s.session = stored
			if retiredBackend(backend) {
				// The key outlives the move to the Cloudflare backend, so a
				// signed-in user follows it instead of signing in again.
				s.session.Backend = cloudBackend
				if err := s.saveLocked(); err != nil {
					log.Printf("cloud: cannot save the migrated session: %v", err)
				}
			}
		} else if stored != (storedSession{}) {
			// A stored session that no longer passes the backend rules is a
			// logged-out start, not a dead app: the user signs in again.
			log.Printf("cloud: ignoring invalid stored session in %s", filepath.Base(s.configPath))
		}
	}
	suppressions := map[string]int64{}
	if readState(s.syncSuppressionsPath, &suppressions) && suppressions != nil {
		s.syncSuppressions = suppressions
	}
	var seen remoteSeenState
	if readState(s.remoteSeenPath, &seen) {
		s.remoteSeen = seen
	}
	var disabled []string
	if readState(s.syncDisabledPath, &disabled) {
		for _, fingerprint := range disabled {
			s.syncDisabled[fingerprint] = true
		}
	}
}

func (s *Service) saveLocked() error {
	data, err := json.Marshal(s.session)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(s.configPath), 0o700); err != nil {
		return err
	}
	temporary := s.configPath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, s.configPath); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func (s *Service) saveSyncSuppressionsLocked() error {
	data, err := json.Marshal(s.syncSuppressions)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(s.syncSuppressionsPath), 0o700); err != nil {
		return err
	}
	temporary := s.syncSuppressionsPath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, s.syncSuppressionsPath); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

// SyncDisabled lists the fingerprints cloud sync is switched off for.
func (s *Service) SyncDisabled() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	listed := make([]string, 0, len(s.syncDisabled))
	for fingerprint := range s.syncDisabled {
		listed = append(listed, fingerprint)
	}
	sort.Strings(listed)
	return listed
}

func (s *Service) syncDisabledFor(fingerprint string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.syncDisabled[fingerprint]
}

// SetSyncEnabled switches cloud sync for one media on or off. Off keeps this
// desktop from uploading its subtitles; the renderer also stops pulling the
// cloud copy down. Nothing already in the cloud is deleted.
func (s *Service) SetSyncEnabled(fingerprint string, enabled bool) error {
	fingerprint = strings.TrimSpace(fingerprint)
	if fingerprint == "" || len(fingerprint) > 256 {
		return errors.New("media fingerprint is required")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.syncDisabled[fingerprint] == !enabled {
		return nil
	}
	if enabled {
		delete(s.syncDisabled, fingerprint)
	} else {
		s.syncDisabled[fingerprint] = true
	}
	if err := s.saveSyncDisabledLocked(); err != nil {
		if enabled {
			s.syncDisabled[fingerprint] = true
		} else {
			delete(s.syncDisabled, fingerprint)
		}
		return err
	}
	return nil
}

func (s *Service) saveSyncDisabledLocked() error {
	listed := make([]string, 0, len(s.syncDisabled))
	for fingerprint := range s.syncDisabled {
		listed = append(listed, fingerprint)
	}
	sort.Strings(listed)
	data, err := json.Marshal(listed)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(s.syncDisabledPath), 0o700); err != nil {
		return err
	}
	temporary := s.syncDisabledPath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, s.syncDisabledPath); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func (s *Service) saveRemoteSeenLocked() error {
	data, err := json.Marshal(s.remoteSeen)
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(s.remoteSeenPath), 0o700); err != nil {
		return err
	}
	temporary := s.remoteSeenPath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, s.remoteSeenPath); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

// diarizationOption is the dialog's speaker diarization as both legs send it:
// the level unchanged and an optional cap, or nil when it asks for none.
func diarizationOption(options map[string]any) map[string]any {
	diarization, ok := options["diarization"].(map[string]any)
	if !ok {
		return nil
	}
	level, ok := diarization["level"].(string)
	if !ok || level == "" || level == "off" {
		return nil
	}
	cleaned := map[string]any{"level": level}
	if limit, exists := diarization["max_speakers"]; exists && limit != nil {
		cleaned["max_speakers"] = limit
	}
	return cleaned
}

// axisOption returns the imported axis a cloud task must run over, or nil.
//
// Only the source-text shape is forwarded: an empty axis is applied locally
// when the artifacts are projected, and the finished shapes never start a task.
// Passing another kind through would have the backend reject the whole request,
// so it is dropped here and the run proceeds as an ordinary transcription.
func axisOption(options map[string]any) map[string]any {
	axis, ok := options["axis"].(map[string]any)
	if !ok || axis == nil {
		return nil
	}
	if kind, _ := axis["kind"].(string); kind != "ja" {
		return nil
	}
	rows, ok := axis["rows"].([]any)
	if !ok || len(rows) == 0 {
		return nil
	}
	return map[string]any{"kind": "ja", "filename": stringOption(axis, "filename", ""), "rows": rows}
}
