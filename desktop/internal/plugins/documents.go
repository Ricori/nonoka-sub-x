package plugins

// Subtitle documents are the widest plugin surface in API v1: reading one
// exposes every line a user has written, and writing one can replace the work
// of a whole session. Read and write are therefore separate permissions, the
// host resolves media ids against its own library instead of trusting a plugin
// path, and writes carry the document revision so the sidecar's optimistic
// lock rejects a save that would silently overwrite an edit made in the editor.

import (
	"errors"
	"fmt"
	"math"
	"path/filepath"
	"strings"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

const (
	// A document that big is already far past what the editor can open; the
	// cap exists so a runaway plugin cannot write an unbounded file.
	maxDocumentSegments = 200_000
	maxSubtitleBytes    = 16 << 20
	maxTitleRunes       = 200
	maxStyleSheetBytes  = 1 << 20
)

// documentStore is the sidecar-backed subtitle document contract. Only the two
// calls plugins may reach are named here, so the plugin service cannot grow
// access to tasks, settings or the runtime through the same dependency.
type documentStore interface {
	Document(videoID string) (map[string]any, error)
	SaveDocument(videoID string, document map[string]any) (map[string]any, error)
}

// SetDocuments attaches the local provider. It is a separate call because the
// provider is constructed after the sidecar starts, while the plugin service
// has to exist before that to answer the first window paint.
func SetDocuments(s *Service, documents documentStore) {
	s.mu.Lock()
	s.documents = documents
	s.mu.Unlock()
}

// Document returns the stored EditDocument for a media entry. The plugin names
// the media by the id media.list handed it, never by path.
func (s *Service) Document(pluginID, mediaID string) (map[string]any, error) {
	if err := s.requirePermission(pluginID, "document.read"); err != nil {
		return nil, err
	}
	documents, _, err := s.documentTarget(mediaID)
	if err != nil {
		return nil, err
	}
	return documents.Document(mediaID)
}

// SaveDocument writes the editable part of a document back. The payload is
// rebuilt from the fields the sidecar accepts, so a plugin can neither invent
// document fields nor drop the revision that guards a concurrent editor save.
func (s *Service) SaveDocument(pluginID, mediaID string, document map[string]any) (map[string]any, error) {
	if err := s.requirePermission(pluginID, "document.write"); err != nil {
		return nil, err
	}
	documents, _, err := s.documentTarget(mediaID)
	if err != nil {
		return nil, err
	}
	payload, err := documentPayload(document)
	if err != nil {
		return nil, err
	}
	return documents.SaveDocument(mediaID, payload)
}

// SaveSubtitleFile publishes plugin-built subtitle text through the host save
// dialog. The plugin picks the content and a suggested name; the user picks
// where it lands, and the host owns the extension.
func (s *Service) SaveSubtitleFile(pluginID, fileName, content string) (string, error) {
	if err := s.requirePermission(pluginID, "subtitle.export"); err != nil {
		return "", err
	}
	if strings.TrimSpace(content) == "" {
		return "", errors.New("subtitle content is empty")
	}
	if len(content) > maxSubtitleBytes {
		return "", errors.New("subtitle content exceeds 16 MB")
	}
	s.mu.RLock()
	media := s.media
	s.mu.RUnlock()
	if media == nil {
		return "", errors.New("media library is unavailable")
	}
	return media.SaveSubtitle(subtitleFileName(fileName), content)
}

// ExportVideo burns plugin-supplied ASS into a library video. The plugin owns
// the subtitle text and the range; the host owns FFmpeg, the encoding
// settings, the save dialog and atomic publication, exactly as the editor's
// own export does.
func (s *Service) ExportVideo(pluginID, mediaID, fileName, ass string, t0, t1 float64, scaleHeight int) (ExportedArtifact, error) {
	if err := s.requirePermission(pluginID, "media.export-video"); err != nil {
		return ExportedArtifact{}, err
	}
	if strings.TrimSpace(ass) == "" {
		return ExportedArtifact{}, errors.New("subtitle track is empty")
	}
	if len(ass) > maxSubtitleBytes {
		return ExportedArtifact{}, errors.New("subtitle track exceeds 16 MB")
	}
	if scaleHeight != 0 && (scaleHeight < 240 || scaleHeight > 4320) {
		return ExportedArtifact{}, errors.New("export height must be 0 or between 240 and 4320")
	}
	if !finiteValue(t0) || !finiteValue(t1) {
		return ExportedArtifact{}, errors.New("export range must be finite")
	}
	if !s.mediaJobMu.TryLock() {
		return ExportedArtifact{}, errors.New("another plugin media export is already running")
	}
	defer s.mediaJobMu.Unlock()
	s.mu.RLock()
	media := s.media
	s.mu.RUnlock()
	if media == nil {
		return ExportedArtifact{}, errors.New("media library is unavailable")
	}
	entry, err := media.Get(mediaID)
	if err != nil {
		return ExportedArtifact{}, err
	}
	if !entry.Available {
		return ExportedArtifact{}, errors.New("local media is unavailable")
	}
	t0 = math.Max(0, t0)
	if t1 <= 0 || t1 > entry.Duration {
		t1 = entry.Duration
	}
	if t1 <= t0 {
		return ExportedArtifact{}, errors.New("video export range is empty")
	}
	result, err := media.ExportVideoRange(mediaID, videoFileName(fileName, entry.Title), ass, t0, t1, 21, "medium", scaleHeight, "192k")
	if err != nil {
		return ExportedArtifact{}, err
	}
	if result.Path == "" {
		return ExportedArtifact{}, nil
	}
	return ExportedArtifact{Path: result.Path, Format: "mp4", Size: result.Size}, nil
}

func (s *Service) documentTarget(mediaID string) (documentStore, library.Entry, error) {
	s.mu.RLock()
	documents, media := s.documents, s.media
	s.mu.RUnlock()
	if media == nil {
		return nil, library.Entry{}, errors.New("media library is unavailable")
	}
	if documents == nil {
		return nil, library.Entry{}, errors.New("subtitle documents are unavailable until the local provider is running")
	}
	entry, err := media.Get(mediaID)
	if err != nil {
		return nil, library.Entry{}, err
	}
	return documents, entry, nil
}

// documentPayload copies the writable fields of an incoming document after
// checking their shape. Segments are carried through as they arrived so a
// plugin round-trip preserves fields it does not understand (word timings,
// confidence flags), while a malformed segment is rejected before it reaches a
// document the user cannot easily repair.
func documentPayload(document map[string]any) (map[string]any, error) {
	if document == nil {
		return nil, errors.New("document is required")
	}
	revision, ok := document["rev"].(float64)
	if !ok || revision != math.Trunc(revision) || revision < 0 {
		return nil, errors.New("document rev is required; read the document first and save the revision you edited")
	}
	subtitles, count, err := validateSegments(document["subtitles"], 0)
	if err != nil {
		return nil, fmt.Errorf("document subtitles: %w", err)
	}
	payload := map[string]any{"rev": revision, "subtitles": subtitles}
	if raw, present := document["tracks"]; present && raw != nil {
		items, listed := raw.([]any)
		if !listed {
			return nil, errors.New("document tracks must be a list")
		}
		tracks := make([]any, 0, len(items))
		for _, item := range items {
			track, valid := item.(map[string]any)
			if !valid {
				return nil, errors.New("document track must be an object")
			}
			if _, valid := track["id"].(string); !valid {
				return nil, errors.New("document track requires a string id")
			}
			segments, total, segmentErr := validateSegments(track["segs"], count)
			if segmentErr != nil {
				return nil, fmt.Errorf("document track %v: %w", track["id"], segmentErr)
			}
			count = total
			track["segs"] = segments
			tracks = append(tracks, track)
		}
		payload["tracks"] = tracks
	}
	if raw, present := document["track_meta"]; present && raw != nil {
		meta, valid := raw.(map[string]any)
		if !valid {
			return nil, errors.New("document track_meta must be an object")
		}
		payload["track_meta"] = meta
	}
	if raw, present := document["effects"]; present && raw != nil {
		items, listed := raw.([]any)
		if !listed {
			return nil, errors.New("document effects must be a list")
		}
		if len(items) > 10_000 {
			return nil, errors.New("document effects exceeds 10000 bindings")
		}
		for _, item := range items {
			if _, valid := item.(map[string]any); !valid {
				return nil, errors.New("document effect must be an object")
			}
		}
		payload["effects"] = items
	}
	if raw, present := document["title"]; present && raw != nil {
		title, valid := raw.(string)
		if !valid || len([]rune(title)) > maxTitleRunes {
			return nil, fmt.Errorf("document title must be text of at most %d characters", maxTitleRunes)
		}
		payload["title"] = title
	}
	if raw, present := document["styles"]; present && raw != nil {
		styles, valid := raw.(string)
		if !valid {
			return nil, errors.New("document styles must be text")
		}
		if len(styles) > maxStyleSheetBytes {
			return nil, fmt.Errorf("document styles must be at most %d bytes", maxStyleSheetBytes)
		}
		payload["styles"] = styles
	}
	return payload, nil
}

func validateSegments(raw any, carried int) ([]any, int, error) {
	if raw == nil {
		return []any{}, carried, nil
	}
	items, listed := raw.([]any)
	if !listed {
		return nil, carried, errors.New("segments must be a list")
	}
	if carried+len(items) > maxDocumentSegments {
		return nil, carried, fmt.Errorf("document exceeds %d subtitle segments", maxDocumentSegments)
	}
	for index, item := range items {
		segment, valid := item.(map[string]any)
		if !valid {
			return nil, carried, fmt.Errorf("segment %d must be an object", index)
		}
		start, startOK := segment["t0"].(float64)
		end, endOK := segment["t1"].(float64)
		if !startOK || !endOK || !finiteValue(start) || !finiteValue(end) || start < 0 || end < start {
			return nil, carried, fmt.Errorf("segment %d needs finite t0 and t1 with t1 >= t0", index)
		}
		for _, lane := range []string{"ja", "zh"} {
			if value, present := segment[lane]; present && value != nil {
				if _, valid := value.(string); !valid {
					return nil, carried, fmt.Errorf("segment %d field %s must be text", index, lane)
				}
			}
		}
	}
	return items, carried + len(items), nil
}

// subtitleFileName keeps the plugin's suggestion but strips it back to a bare
// file name: the save dialog decides the directory, never the plugin.
func subtitleFileName(name string) string {
	name = strings.TrimSpace(filepath.Base(strings.TrimSpace(name)))
	if name == "" || name == "." || name == string(filepath.Separator) {
		return "subtitle.ass"
	}
	if len([]rune(name)) > maxTitleRunes {
		name = string([]rune(name)[:maxTitleRunes])
	}
	extension := strings.ToLower(filepath.Ext(name))
	if extension != ".ass" && extension != ".srt" {
		name += ".ass"
	}
	return name
}

func videoFileName(name, title string) string {
	name = strings.TrimSpace(filepath.Base(strings.TrimSpace(name)))
	if name == "" || name == "." || name == string(filepath.Separator) {
		return strings.TrimSuffix(title, filepath.Ext(title)) + " - 字幕.mp4"
	}
	if len([]rune(name)) > maxTitleRunes {
		name = string([]rune(name)[:maxTitleRunes])
	}
	if !strings.EqualFold(filepath.Ext(name), ".mp4") {
		name += ".mp4"
	}
	return name
}

func finiteValue(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0)
}
