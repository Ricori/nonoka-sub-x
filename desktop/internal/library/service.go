// Package library owns Nonoka X's local media index and managed video cache.
// Original media remains user-owned; disposable working copies live in the
// video cache directory, which the settings page can move to another drive.
package library

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/storage"
	"github.com/wailsapp/wails/v3/pkg/application"
)

const (
	maxThumbnailBytes = 5 << 20
)

var supportedExtensions = map[string]struct{}{
	".mp4": {}, ".m4v": {}, ".mov": {}, ".mkv": {}, ".webm": {},
}

type Entry struct {
	ID                 string  `json:"id"`
	SourcePath         string  `json:"sourcePath"`
	Title              string  `json:"title"`
	Size               int64   `json:"size"`
	Duration           float64 `json:"duration"`
	Width              int     `json:"width"`
	Height             int     `json:"height"`
	Fingerprint        string  `json:"fingerprint"`
	AddedAt            int64   `json:"addedAt"`
	LastAccess         int64   `json:"lastAccess"`
	ThumbnailAvailable bool    `json:"thumbnailAvailable"`
	Available          bool    `json:"available"`
	DocumentAvailable  bool    `json:"documentAvailable"`
	DocumentRemoved    bool    `json:"documentRemoved,omitempty"`
	Cached             bool    `json:"cached"`
	// Clips is the retired named-range list. Nothing reads it any more, but it
	// is kept so an older library.json survives a round trip untouched.
	Clips     []Clip     `json:"clips,omitempty"`
	VideoEdit *VideoEdit `json:"videoEdit,omitempty"`
}

type Clip struct {
	ID        string  `json:"id"`
	Name      string  `json:"name"`
	T0        float64 `json:"t0"`
	T1        float64 `json:"t1"`
	CreatedAt int64   `json:"createdAt"`
}

type ImportFailure struct {
	Path    string `json:"path"`
	Name    string `json:"name"`
	Message string `json:"message"`
}

type ImportResult struct {
	Added  []Entry         `json:"added"`
	Failed []ImportFailure `json:"failed"`
}

type Metadata struct {
	Duration float64
	Width    int
	Height   int
	HasVideo bool
	HasAudio bool
}

type prober interface {
	Probe(context.Context, string) (Metadata, error)
}

type thumbnailer interface {
	Generate(context.Context, string, string, float64) error
}

type Service struct {
	mu           sync.RWMutex
	indexPath    string
	root         string
	thumbnailDir string
	// cacheDir is the one directory that can move while the service is
	// running, so it has a lock of its own rather than sharing cacheMu, which
	// the cache operations already hold when they need to read it.
	cachePathMu      sync.RWMutex
	cacheDir         string
	cachePath        string
	entries          []Entry
	prober           prober
	thumbnailer      thumbnailer
	app              *application.App
	home             *application.WebviewWindow
	editor           *application.WebviewWindow
	exportWindow     *application.WebviewWindow
	media            *loopbackMediaServer
	bundledFonts     map[string][]byte
	cacheMu          sync.Mutex
	cacheConfig      CacheConfig
	activeMedia      string
	exportCancels    map[string]context.CancelFunc
	transcodeCancels map[string]context.CancelFunc
}

func New(dataDirectory string) (*Service, error) {
	return newService(dataDirectory, commandMediaTools{dataDirectory: dataDirectory})
}

func newService(dataDirectory string, tools commandMediaTools) (*Service, error) {
	root, err := filepath.Abs(dataDirectory)
	if err != nil {
		return nil, fmt.Errorf("resolve media library directory: %w", err)
	}
	service := &Service{
		root:             root,
		indexPath:        filepath.Join(root, "library.json"),
		thumbnailDir:     filepath.Join(root, "thumbnails"),
		cacheDir:         storage.VideoDirectory(root),
		cachePath:        filepath.Join(root, "cache.json"),
		prober:           tools,
		thumbnailer:      tools,
		entries:          []Entry{},
		cacheConfig:      defaultCacheConfig(),
		exportCancels:    map[string]context.CancelFunc{},
		transcodeCancels: map[string]context.CancelFunc{},
	}
	if err := os.MkdirAll(service.thumbnailDir, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(service.cacheDir, 0o755); err != nil {
		return nil, err
	}
	if err := service.load(); err != nil {
		return nil, err
	}
	if err := service.loadCacheConfig(); err != nil {
		return nil, err
	}
	if err := service.convergeCache(); err != nil {
		return nil, err
	}
	return service, nil
}

func newServiceWithTools(dataDirectory string, probe prober, thumbnails thumbnailer) (*Service, error) {
	service, err := newService(dataDirectory, commandMediaTools{dataDirectory: dataDirectory})
	if err != nil {
		return nil, err
	}
	service.prober = probe
	service.thumbnailer = thumbnails
	return service, nil
}

func Attach(s *Service, app *application.App, home *application.WebviewWindow) {
	s.mu.Lock()
	s.app = app
	s.home = home
	s.mu.Unlock()
}

func SetEditorWindow(s *Service, editor *application.WebviewWindow) {
	s.mu.Lock()
	s.editor = editor
	s.mu.Unlock()
}

// SetExportWindow gives editor video exports their own native dialog owner.
// Subtitle save dialogs must keep following the active editor while an encode
// is running, so this is deliberately separate from SetEditorWindow.
func SetExportWindow(s *Service, window *application.WebviewWindow) {
	s.mu.Lock()
	s.exportWindow = window
	s.mu.Unlock()
}

func (s *Service) cacheDirectory() string {
	s.cachePathMu.RLock()
	defer s.cachePathMu.RUnlock()
	return s.cacheDir
}

// SetCacheDirectory retargets the managed video cache after the settings page
// has moved it to another volume. Converging immediately means the next status
// read describes the new directory, including any working copies that arrived
// with the move.
func SetCacheDirectory(s *Service, directory string) error {
	resolved, err := filepath.Abs(directory)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(resolved, 0o755); err != nil {
		return err
	}
	s.cachePathMu.Lock()
	s.cacheDir = resolved
	s.cachePathMu.Unlock()
	return s.convergeCache()
}

func SetBundledFonts(s *Service, fonts map[string][]byte) {
	cloned := make(map[string][]byte, len(fonts))
	for name, data := range fonts {
		cloned[name] = append([]byte(nil), data...)
	}
	s.mu.Lock()
	s.bundledFonts = cloned
	s.mu.Unlock()
}

func (s *Service) copyBundledFonts(directory string) (bool, error) {
	s.mu.RLock()
	fonts := make(map[string][]byte, len(s.bundledFonts))
	for name, data := range s.bundledFonts {
		fonts[name] = data
	}
	s.mu.RUnlock()
	if len(fonts) == 0 {
		return false, nil
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return false, err
	}
	for name, data := range fonts {
		if filepath.Base(name) != name || !strings.EqualFold(filepath.Ext(name), ".woff2") {
			return false, errors.New("invalid bundled font name")
		}
		if err := os.WriteFile(filepath.Join(directory, name), data, 0o600); err != nil {
			return false, err
		}
	}
	return true, nil
}

func (s *Service) dialogWindow() (*application.App, *application.WebviewWindow) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.editor != nil {
		return s.app, s.editor
	}
	return s.app, s.home
}

func (s *Service) exportDialogWindow() (*application.App, *application.WebviewWindow) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.exportWindow != nil {
		return s.app, s.exportWindow
	}
	if s.editor != nil {
		return s.app, s.editor
	}
	return s.app, s.home
}

// dialogCancelled reports whether a native file dialog closed because the user
// dismissed it. Windows surfaces that as an error ("cancelled by user") while
// the other platforms just return an empty selection, so callers treat both as
// "nothing was picked" instead of a failure worth reporting.
func dialogCancelled(err error) bool {
	if err == nil {
		return false
	}
	text := strings.ToLower(err.Error())
	return strings.Contains(text, "cancelled by user") || strings.Contains(text, "canceled by user")
}

func (s *Service) List() []Entry {
	s.mu.RLock()
	result := append([]Entry(nil), s.entries...)
	s.mu.RUnlock()
	for index := range result {
		result[index].ThumbnailAvailable = fileExists(s.thumbnailPath(result[index].ID))
		result[index].Cached = s.cachedPath(result[index].ID) != ""
		result[index].Available = result[index].Cached || fileExists(result[index].SourcePath)
		result[index].DocumentAvailable = fileExists(filepath.Join(s.root, "documents", result[index].ID, "document.json"))
		if result[index].DocumentAvailable {
			result[index].DocumentRemoved = false
		}
	}
	sort.SliceStable(result, func(left, right int) bool { return result[left].AddedAt > result[right].AddedAt })
	return result
}

func (s *Service) Get(id string) (Entry, error) {
	return s.entryByID(id)
}

func (s *Service) PickAndImport() (ImportResult, error) {
	app, window := s.dialogWindow()
	if app == nil || window == nil {
		return ImportResult{}, errors.New("application window is not ready")
	}
	paths, err := app.Dialog.OpenFile().
		SetTitle("选择本地媒体").
		AttachToWindow(window).
		AddFilter("视频文件", "*.mp4;*.m4v;*.mov;*.mkv;*.webm").
		PromptForMultipleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if err != nil || len(paths) == 0 {
		return ImportResult{Added: []Entry{}, Failed: []ImportFailure{}}, err
	}
	return s.Import(paths), nil
}

func (s *Service) Import(paths []string) ImportResult {
	result := ImportResult{Added: []Entry{}, Failed: []ImportFailure{}}
	for _, path := range paths {
		entry, added, err := s.importOne(path)
		if err != nil {
			result.Failed = append(result.Failed, ImportFailure{
				Path: path, Name: filepath.Base(path), Message: err.Error(),
			})
			continue
		}
		if added {
			result.Added = append(result.Added, entry)
		}
	}
	if len(result.Added) > 0 {
		s.emitChanged()
	}
	return result
}

func (s *Service) ThumbnailDataURL(id string) (string, error) {
	if !validID(id) {
		return "", errors.New("invalid media id")
	}
	data, err := os.ReadFile(s.thumbnailPath(id))
	if err != nil {
		return "", err
	}
	if len(data) > maxThumbnailBytes {
		return "", errors.New("thumbnail exceeds size limit")
	}
	return "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(data), nil
}

// EnsureThumbnail gives an entry a cover it cannot draw for itself, in the same
// JPEG data URL shape ThumbnailDataURL hands back. Adoption is the caller: the
// subtitles arriving from the cloud may belong to media this machine holds only
// a placeholder for, and there is no file here to cut a frame from.
//
// An entry that already has a cover keeps it. A frame taken from the real video
// is the better one, and Relink cuts exactly that the moment a video is
// associated with a placeholder -- overwriting here would put a stale remote
// cover back on a card that had just been given its own.
func (s *Service) EnsureThumbnail(id, dataURL string) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	image, err := decodeThumbnailDataURL(dataURL)
	if err != nil {
		return err
	}
	path := s.thumbnailPath(id)
	if fileExists(path) {
		return nil
	}
	if err := os.MkdirAll(s.thumbnailDir, 0o755); err != nil {
		return err
	}
	// Written aside and renamed into place: a cover is only ever produced once,
	// so a torn one would be what every later read of this entry returns.
	temporary, err := os.CreateTemp(s.thumbnailDir, "."+id+"-*.part")
	if err != nil {
		return err
	}
	name := temporary.Name()
	_, err = temporary.Write(image)
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

func decodeThumbnailDataURL(value string) ([]byte, error) {
	encoded, found := strings.CutPrefix(value, "data:image/jpeg;base64,")
	if !found {
		return nil, errors.New("thumbnail is not a JPEG data URL")
	}
	image, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, err
	}
	if len(image) == 0 || len(image) > maxThumbnailBytes {
		return nil, errors.New("thumbnail is empty or exceeds size limit")
	}
	// ThumbnailDataURL re-labels whatever is on disk as image/jpeg without ever
	// looking at it, so anything that is not one has to be refused on the way in.
	if len(image) < 3 || image[0] != 0xFF || image[1] != 0xD8 || image[2] != 0xFF {
		return nil, errors.New("thumbnail is not a JPEG")
	}
	return image, nil
}

// AddPlaceholder records media the library has subtitles for but no file of:
// a cloud entry transcribed on another machine, adopted here so the document
// has something to hang on. The fingerprint remains the subtitle record's
// identity even when the user later associates an arbitrary playback video.
func (s *Service) AddPlaceholder(title, fingerprint string, duration float64) (Entry, error) {
	fingerprint = strings.TrimSpace(fingerprint)
	if fingerprint == "" || len(fingerprint) > 256 {
		return Entry{}, errors.New("media fingerprint is required")
	}
	s.mu.Lock()
	for _, existing := range s.entries {
		if existing.Fingerprint == fingerprint {
			s.mu.Unlock()
			return existing, nil
		}
	}
	id, err := randomID()
	if err != nil {
		s.mu.Unlock()
		return Entry{}, err
	}
	now := time.Now().UnixMilli()
	entry := Entry{
		ID: id, Title: strings.TrimSpace(title), Duration: max(duration, 0),
		Fingerprint: fingerprint, AddedAt: now, LastAccess: now,
	}
	if entry.Title == "" {
		entry.Title = id
	}
	s.entries = append([]Entry{entry}, s.entries...)
	if err := s.saveLocked(); err != nil {
		s.entries = s.entries[1:]
		s.mu.Unlock()
		return Entry{}, err
	}
	s.mu.Unlock()
	// No change event: the caller projects a document onto this entry and then
	// refreshes. Announcing the bare placeholder here publishes a snapshot
	// taken before the subtitles exist, and if it lands after that refresh the
	// card drops back to "no document" with nothing left to correct it.
	return entry, nil
}

func (s *Service) importOne(path string) (Entry, bool, error) {
	if strings.TrimSpace(path) == "" {
		return Entry{}, false, errors.New("media path is empty")
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return Entry{}, false, err
	}
	if _, supported := supportedExtensions[strings.ToLower(filepath.Ext(absolute))]; !supported {
		return Entry{}, false, fmt.Errorf("unsupported media format %s", filepath.Ext(absolute))
	}
	stat, err := os.Stat(absolute)
	if err != nil {
		return Entry{}, false, err
	}
	if !stat.Mode().IsRegular() {
		return Entry{}, false, errors.New("media source is not a regular file")
	}
	metadata, err := s.prober.Probe(context.Background(), absolute)
	if err != nil {
		return Entry{}, false, err
	}
	if !metadata.HasVideo || metadata.Duration <= 0 {
		return Entry{}, false, errors.New("media source does not contain a readable video track")
	}
	fingerprint, err := fileFingerprint(absolute, stat.Size())
	if err != nil {
		return Entry{}, false, err
	}

	s.mu.Lock()
	for index, existing := range s.entries {
		if existing.Fingerprint != fingerprint {
			continue
		}
		// A placeholder added for cloud-only subtitles carries the fingerprint
		// and nothing else. Importing the video it belongs to has to fill it
		// in; returning the match unchanged left the card reporting a missing
		// source right after the user pointed at the file.
		if !fileExists(existing.SourcePath) {
			previous := s.entries[index]
			s.entries[index].SourcePath = absolute
			s.entries[index].Size = stat.Size()
			s.entries[index].Duration = metadata.Duration
			s.entries[index].Width, s.entries[index].Height = metadata.Width, metadata.Height
			s.entries[index].LastAccess = time.Now().UnixMilli()
			if err := s.saveLocked(); err != nil {
				s.entries[index] = previous
				s.mu.Unlock()
				return Entry{}, false, err
			}
			attached := s.entries[index]
			s.mu.Unlock()
			if err := s.thumbnailer.Generate(context.Background(), absolute, s.thumbnailPath(attached.ID), metadata.Duration); err == nil {
				attached.ThumbnailAvailable = true
			}
			attached.Available = true
			return attached, false, nil
		}
		s.mu.Unlock()
		return existing, false, nil
	}
	id, err := randomID()
	if err != nil {
		s.mu.Unlock()
		return Entry{}, false, err
	}
	now := time.Now().UnixMilli()
	entry := Entry{
		ID: id, SourcePath: absolute, Title: stat.Name(), Size: stat.Size(),
		Duration: metadata.Duration, Width: metadata.Width, Height: metadata.Height,
		Fingerprint: fingerprint, AddedAt: now, LastAccess: now,
		Available: true,
	}
	s.entries = append([]Entry{entry}, s.entries...)
	if err := s.saveLocked(); err != nil {
		s.entries = s.entries[1:]
		s.mu.Unlock()
		return Entry{}, false, err
	}
	s.mu.Unlock()

	if err := s.thumbnailer.Generate(context.Background(), absolute, s.thumbnailPath(id), metadata.Duration); err == nil {
		entry.ThumbnailAvailable = true
	}
	return entry, true, nil
}

func (s *Service) load() error {
	data, err := os.ReadFile(s.indexPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, &s.entries); err != nil {
		return fmt.Errorf("read media library: %w", err)
	}
	return nil
}

func (s *Service) saveLocked() error {
	data, err := json.MarshalIndent(s.entries, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(s.indexPath), 0o755); err != nil {
		return err
	}
	temporary := s.indexPath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o644); err != nil {
		return err
	}
	if err := os.Rename(temporary, s.indexPath); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func (s *Service) emitChanged() {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app != nil {
		app.Event.Emit("library:changed", s.List())
	}
}

func (s *Service) thumbnailPath(id string) string {
	return filepath.Join(s.thumbnailDir, id+".jpg")
}

func validID(id string) bool {
	if !strings.HasPrefix(id, "loc_") || len(id) != 16 {
		return false
	}
	_, err := hex.DecodeString(strings.TrimPrefix(id, "loc_"))
	return err == nil
}

func randomID() (string, error) {
	buffer := make([]byte, 6)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return "loc_" + hex.EncodeToString(buffer), nil
}

func fileFingerprint(path string, size int64) (string, error) {
	const chunkSize int64 = 2 << 20
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	_, _ = fmt.Fprintf(hash, "%d", size)
	length := min(size, chunkSize)
	buffer := make([]byte, length)
	if _, err := io.ReadFull(file, buffer); err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
		return "", err
	}
	_, _ = hash.Write(buffer)
	if _, err := file.Seek(max(0, size-chunkSize), io.SeekStart); err != nil {
		return "", err
	}
	buffer = make([]byte, length)
	if _, err := io.ReadFull(file, buffer); err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
		return "", err
	}
	_, _ = hash.Write(buffer)
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func fileExists(path string) bool {
	stat, err := os.Stat(path)
	return err == nil && stat.Mode().IsRegular()
}
