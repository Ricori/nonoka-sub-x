// Package plugins owns installation and discovery of Nonoka X tool plugins.
//
// Plugin UI is deliberately data-driven: the renderer asks this service for
// validated manifests and isolated HTML, while native capabilities remain
// behind explicit host APIs. Installing a plugin never loads code into the Go
// process.
package plugins

import (
	"archive/zip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/wailsapp/wails/v3/pkg/application"
)

const (
	manifestName           = "nonoka-plugin.json"
	registryName           = "plugin-registry.json"
	currentName            = "current.json"
	maxPackageFiles        = 10_000
	maxPackageBytes  int64 = 2 << 30
	maxManifestBytes int64 = 1 << 20
	maxPageBytes     int64 = 4 << 20
)

var (
	pluginIDPattern = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$`)
	versionPattern  = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$`)
)

type ToolContribution struct {
	ID    string `json:"id"`
	Title string `json:"title"`
	Icon  string `json:"icon,omitempty"`
	Page  string `json:"page"`
	Order int    `json:"order,omitempty"`
}

type Contributions struct {
	Tools []ToolContribution `json:"tools"`
}

type Manifest struct {
	ID          string        `json:"id"`
	Name        string        `json:"name"`
	Version     string        `json:"version"`
	APIVersion  int           `json:"apiVersion"`
	Description string        `json:"description,omitempty"`
	Publisher   string        `json:"publisher,omitempty"`
	License     string        `json:"license,omitempty"`
	Permissions []string      `json:"permissions,omitempty"`
	Contributes Contributions `json:"contributes"`
}

type InstalledPlugin struct {
	Manifest
	Enabled bool `json:"enabled"`
	// System marks a first-party plugin that ships inside the Nonoka X binary.
	// The flag is derived from where the plugin is installed, never from the
	// manifest, so a sideloaded package cannot promote itself.
	System bool `json:"system"`
}

type Change struct {
	Kind     string `json:"kind"`
	PluginID string `json:"pluginId"`
}

type registry struct {
	Schema  int             `json:"schema"`
	Enabled map[string]bool `json:"enabled"`
}

type currentPointer struct {
	Current string `json:"current"`
}

type Service struct {
	mu         sync.RWMutex
	mediaJobMu sync.Mutex
	// downloadActive tracks a running yt-dlp download so a page that was
	// unmounted mid-download can find out it is still going. The iframe is
	// destroyed whenever the user navigates away, taking every bit of page state
	// with it, so the Go side has to be the one that remembers.
	downloadActive atomic.Bool
	// downloadCancel interrupts the running yt-dlp. Guarded by its own mutex
	// rather than `mu`, because cancelling must not queue behind whatever else
	// holds the service lock -- the point of the button is that it acts at once.
	downloadCancelMu  sync.Mutex
	downloadCancel    context.CancelFunc
	downloadCancelled atomic.Bool
	// downloadLogLines retains the current run's output. The page renders the log
	// into the iframe's DOM, which navigation destroys, and the log events only
	// carry what arrives next -- so without this a page that comes back mid-run
	// shows a log that starts in the middle of nowhere.
	downloadLogMu    sync.Mutex
	downloadLogLines []string
	dataDirectory    string
	pluginsRoot      string
	systemRoot       string
	dataRoot         string
	registryPath     string
	registry         registry
	app              *application.App
	home             *application.WebviewWindow
	media            mediaLibrary
	documents        documentStore
	engine           engineHost
	// The engine capabilities' own state, on separate locks. An LLM call takes
	// as long as the model does, and a task's ownership list is a file read:
	// neither belongs behind `mu`, which every page paint takes.
	llmMu     sync.Mutex
	llmActive map[string]bool
	llmCalls  map[string][]time.Time
	engineMu  sync.Mutex
}

// mediaLibrary names exactly the library calls plugin capabilities are built
// from. Anything the host does not list here stays out of reach of a plugin,
// however the library grows.
type mediaLibrary interface {
	List() []library.Entry
	Get(string) (library.Entry, error)
	Import([]string) library.ImportResult
	SaveSubtitle(defaultName, content string) (string, error)
	ExportVideoRange(id, defaultName, ass string, t0, t1 float64, crf int, preset string, scaleH int, abr string) (library.ExportResult, error)
}

func init() {
	application.RegisterEvent[Change]("plugins:changed")
	application.RegisterEvent[DownloadLog]("plugins:download-log")
}

func New(dataDirectory string, libraries ...mediaLibrary) (*Service, error) {
	root, err := filepath.Abs(dataDirectory)
	if err != nil {
		return nil, fmt.Errorf("resolve plugin data directory: %w", err)
	}
	service := &Service{
		dataDirectory: root,
		pluginsRoot:   filepath.Join(root, "plugins"),
		systemRoot:    filepath.Join(root, "system-plugins"),
		dataRoot:      filepath.Join(root, "plugin-data"),
		registryPath:  filepath.Join(root, registryName),
		registry:      registry{Schema: 1, Enabled: map[string]bool{}},
	}
	if len(libraries) > 0 {
		service.media = libraries[0]
	}
	if err := os.MkdirAll(service.pluginsRoot, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(service.systemRoot, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(service.dataRoot, 0o755); err != nil {
		return nil, err
	}
	if err := service.loadRegistry(); err != nil {
		return nil, err
	}
	// Built-in tools are published before the first List so they are mounted on
	// a cold data directory without the user installing anything.
	if err := service.ensureSystemPlugins(); err != nil {
		return nil, err
	}
	return service, nil
}

func Attach(service *Service, app *application.App, home *application.WebviewWindow) {
	service.mu.Lock()
	service.app = app
	service.home = home
	service.mu.Unlock()
}

func (s *Service) List() []InstalledPlugin {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.listLocked()
}

func (s *Service) PickAndInstall() (InstalledPlugin, error) {
	s.mu.RLock()
	app, home := s.app, s.home
	s.mu.RUnlock()
	if app == nil || home == nil {
		return InstalledPlugin{}, errors.New("application window is not ready")
	}
	path, err := app.Dialog.OpenFile().
		SetTitle("安装 Nonoka Sub X 插件").
		AttachToWindow(home).
		AddFilter("Nonoka Sub X 插件", "*.nonoka-plugin").
		PromptForSingleSelection()
	if err != nil && !dialogCancelled(err) {
		return InstalledPlugin{}, err
	}
	if path == "" {
		return InstalledPlugin{}, nil
	}
	return s.Install(path)
}

// Install accepts a .nonoka-plugin ZIP or an unpacked plugin directory. The
// directory form is intended for developer workflows and automated tests.
func (s *Service) Install(path string) (InstalledPlugin, error) {
	absolute, err := filepath.Abs(strings.TrimSpace(path))
	if err != nil {
		return InstalledPlugin{}, err
	}
	info, err := os.Stat(absolute)
	if err != nil {
		return InstalledPlugin{}, err
	}
	staging, err := os.MkdirTemp(s.pluginsRoot, ".install-*")
	if err != nil {
		return InstalledPlugin{}, err
	}
	defer os.RemoveAll(staging)
	if info.IsDir() {
		err = copyPackage(absolute, staging)
	} else {
		err = extractPackage(absolute, staging)
	}
	if err != nil {
		return InstalledPlugin{}, err
	}
	manifest, err := readManifest(staging)
	if err != nil {
		return InstalledPlugin{}, err
	}
	if err := validateManifest(manifest, staging); err != nil {
		return InstalledPlugin{}, err
	}

	s.mu.Lock()
	if _, system := s.pluginHomeLocked(manifest.ID); system {
		s.mu.Unlock()
		return InstalledPlugin{}, errors.New("this id belongs to a built-in system plugin")
	}
	destination := filepath.Join(s.pluginsRoot, manifest.ID, "versions", manifest.Version)
	if _, statErr := os.Stat(destination); statErr == nil {
		s.mu.Unlock()
		return InstalledPlugin{}, errors.New("this plugin version is already installed")
	} else if !errors.Is(statErr, os.ErrNotExist) {
		s.mu.Unlock()
		return InstalledPlugin{}, statErr
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	if err := os.Rename(staging, destination); err != nil {
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	pluginRoot := filepath.Join(s.pluginsRoot, manifest.ID)
	previousPointer, previousPointerErr := os.ReadFile(filepath.Join(pluginRoot, currentName))
	previousEnabled, hadPreviousEnabled := s.registry.Enabled[manifest.ID]
	if err := writeJSONAtomic(filepath.Join(pluginRoot, currentName), currentPointer{Current: manifest.Version}); err != nil {
		_ = os.RemoveAll(destination)
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	s.registry.Enabled[manifest.ID] = true
	if err := s.saveRegistryLocked(); err != nil {
		_ = os.RemoveAll(destination)
		if previousPointerErr == nil {
			_ = os.WriteFile(filepath.Join(pluginRoot, currentName), previousPointer, 0o600)
		} else {
			_ = os.Remove(filepath.Join(pluginRoot, currentName))
		}
		if hadPreviousEnabled {
			s.registry.Enabled[manifest.ID] = previousEnabled
		} else {
			delete(s.registry.Enabled, manifest.ID)
		}
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	result := InstalledPlugin{Manifest: manifest, Enabled: true}
	s.mu.Unlock()
	s.emit(Change{Kind: "installed", PluginID: manifest.ID})
	return result, nil
}

func (s *Service) SetEnabled(id string, enabled bool) (InstalledPlugin, error) {
	if !pluginIDPattern.MatchString(id) {
		return InstalledPlugin{}, errors.New("invalid plugin id")
	}
	s.mu.Lock()
	manifest, _, system, err := s.installedManifestLocked(id)
	if err != nil {
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	previous, hadPrevious := s.registry.Enabled[id]
	s.registry.Enabled[id] = enabled
	if err := s.saveRegistryLocked(); err != nil {
		if hadPrevious {
			s.registry.Enabled[id] = previous
		} else {
			delete(s.registry.Enabled, id)
		}
		s.mu.Unlock()
		return InstalledPlugin{}, err
	}
	result := InstalledPlugin{Manifest: manifest, Enabled: enabled, System: system}
	s.mu.Unlock()
	kind := "disabled"
	if enabled {
		kind = "enabled"
	}
	s.emit(Change{Kind: kind, PluginID: id})
	return result, nil
}

func (s *Service) Uninstall(id string, removeData bool) error {
	if !pluginIDPattern.MatchString(id) {
		return errors.New("invalid plugin id")
	}
	s.mu.Lock()
	pluginRoot := filepath.Join(s.pluginsRoot, id)
	if filepath.Dir(pluginRoot) != s.pluginsRoot {
		s.mu.Unlock()
		return errors.New("invalid plugin directory")
	}
	_, _, system, err := s.installedManifestLocked(id)
	if err != nil {
		s.mu.Unlock()
		return err
	}
	if system {
		s.mu.Unlock()
		return errors.New("system plugins are built into Nonoka Sub X and can only be disabled")
	}
	if err := os.RemoveAll(pluginRoot); err != nil {
		s.mu.Unlock()
		return err
	}
	if removeData {
		data := filepath.Join(s.dataRoot, id)
		if filepath.Dir(data) != s.dataRoot {
			s.mu.Unlock()
			return errors.New("invalid plugin data directory")
		}
		if err := os.RemoveAll(data); err != nil {
			s.mu.Unlock()
			return err
		}
	}
	delete(s.registry.Enabled, id)
	if err := s.saveRegistryLocked(); err != nil {
		s.mu.Unlock()
		return err
	}
	s.mu.Unlock()
	s.emit(Change{Kind: "uninstalled", PluginID: id})
	return nil
}

func (s *Service) PageHTML(pluginID, toolID string) (string, error) {
	if !pluginIDPattern.MatchString(pluginID) || !pluginIDPattern.MatchString(toolID) {
		return "", errors.New("invalid plugin page")
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	manifest, versionRoot, system, err := s.installedManifestLocked(pluginID)
	if err != nil {
		return "", err
	}
	if !s.enabledLocked(pluginID, system) {
		return "", errors.New("plugin is disabled")
	}
	var page string
	for _, tool := range manifest.Contributes.Tools {
		if tool.ID == toolID {
			page = tool.Page
			break
		}
	}
	if page == "" {
		return "", errors.New("plugin tool does not exist")
	}
	path, err := safeJoin(versionRoot, page)
	if err != nil {
		return "", err
	}
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxPageBytes+1))
	if err != nil {
		return "", err
	}
	if int64(len(data)) > maxPageBytes {
		return "", errors.New("plugin page exceeds 4 MB")
	}
	return injectPagePolicy(string(data)), nil
}

func (s *Service) listLocked() []InstalledPlugin {
	result := make([]InstalledPlugin, 0, 8)
	seen := map[string]bool{}
	// System first: pluginHomeLocked resolves that tree first too, so visiting
	// it first keeps a shadowing user directory from producing a second card.
	for _, root := range []string{s.systemRoot, s.pluginsRoot} {
		entries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if !entry.IsDir() || !pluginIDPattern.MatchString(entry.Name()) || seen[entry.Name()] {
				continue
			}
			manifest, _, system, manifestErr := s.installedManifestLocked(entry.Name())
			if manifestErr != nil {
				continue
			}
			seen[manifest.ID] = true
			result = append(result, InstalledPlugin{
				Manifest: manifest,
				Enabled:  s.enabledLocked(manifest.ID, system),
				System:   system,
			})
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].System != result[j].System {
			return result[i].System
		}
		return strings.ToLower(result[i].Name) < strings.ToLower(result[j].Name)
	})
	return result
}

// pluginHomeLocked resolves which tree owns an id. System plugins are looked up
// first so a user package can never shadow a built-in one, and the answer is
// what every trust decision downstream is based on.
func (s *Service) pluginHomeLocked(id string) (root string, system bool) {
	candidate := filepath.Join(s.systemRoot, id)
	if info, err := os.Stat(filepath.Join(candidate, currentName)); err == nil && info.Mode().IsRegular() {
		return candidate, true
	}
	return filepath.Join(s.pluginsRoot, id), false
}

// enabledLocked reports whether a plugin's entry points are mounted. System
// plugins ship switched on, so an id the registry has never recorded counts as
// enabled; user plugins stay off until Install writes their entry.
func (s *Service) enabledLocked(id string, system bool) bool {
	if enabled, recorded := s.registry.Enabled[id]; recorded {
		return enabled
	}
	return system
}

func (s *Service) installedManifestLocked(id string) (Manifest, string, bool, error) {
	pluginRoot, system := s.pluginHomeLocked(id)
	var pointer currentPointer
	data, err := os.ReadFile(filepath.Join(pluginRoot, currentName))
	if err != nil || json.Unmarshal(data, &pointer) != nil || !versionPattern.MatchString(pointer.Current) {
		return Manifest{}, "", system, errors.New("plugin installation is incomplete")
	}
	versionRoot := filepath.Join(pluginRoot, "versions", pointer.Current)
	manifest, err := readManifest(versionRoot)
	if err != nil {
		return Manifest{}, "", system, err
	}
	if manifest.ID != id || manifest.Version != pointer.Current {
		return Manifest{}, "", system, errors.New("plugin manifest does not match its installation")
	}
	if err := validateManifest(manifest, versionRoot); err != nil {
		return Manifest{}, "", system, err
	}
	return manifest, versionRoot, system, nil
}

func validateManifest(manifest Manifest, root string) error {
	if !pluginIDPattern.MatchString(manifest.ID) {
		return errors.New("plugin id must contain lowercase letters, digits, dots, dashes, or underscores")
	}
	if strings.TrimSpace(manifest.Name) == "" || len([]rune(manifest.Name)) > 80 {
		return errors.New("plugin name is required and must not exceed 80 characters")
	}
	if !versionPattern.MatchString(manifest.Version) {
		return errors.New("plugin version must use semantic versioning")
	}
	if manifest.APIVersion != 1 {
		return fmt.Errorf("unsupported plugin API version %d", manifest.APIVersion)
	}
	if len(manifest.Contributes.Tools) == 0 {
		return errors.New("plugin must contribute at least one tool")
	}
	knownPermissions := map[string]bool{
		"media.list":           true,
		"media.import":         true,
		"media.export-video":   true,
		"document.read":        true,
		"document.write":       true,
		"subtitle.export":      true,
		"tools.yt-dlp":         true,
		"tools.cookies":        true,
		"ffmpeg.extract-audio": true,
		"llm.complete":         true,
		"engine.run":           true,
		"engine.artifacts":     true,
	}
	seenPermissions := map[string]bool{}
	for _, permission := range manifest.Permissions {
		if !knownPermissions[permission] {
			return fmt.Errorf("unsupported plugin permission %q", permission)
		}
		if seenPermissions[permission] {
			return fmt.Errorf("duplicate plugin permission %q", permission)
		}
		seenPermissions[permission] = true
	}
	seen := map[string]bool{}
	for _, tool := range manifest.Contributes.Tools {
		if !pluginIDPattern.MatchString(tool.ID) || seen[tool.ID] {
			return errors.New("plugin tool ids must be unique and use lowercase identifier characters")
		}
		seen[tool.ID] = true
		if strings.TrimSpace(tool.Title) == "" || len([]rune(tool.Title)) > 60 {
			return errors.New("plugin tool title is required and must not exceed 60 characters")
		}
		path, err := safeJoin(root, tool.Page)
		if err != nil || !strings.EqualFold(filepath.Ext(path), ".html") {
			return errors.New("plugin tool page must be a relative HTML file")
		}
		info, statErr := os.Stat(path)
		if statErr != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("plugin tool page %q does not exist", tool.Page)
		}
	}
	return nil
}

func readManifest(root string) (Manifest, error) {
	file, err := os.Open(filepath.Join(root, manifestName))
	if err != nil {
		return Manifest{}, fmt.Errorf("read %s: %w", manifestName, err)
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxManifestBytes+1))
	if err != nil {
		return Manifest{}, fmt.Errorf("read %s: %w", manifestName, err)
	}
	if int64(len(data)) > maxManifestBytes {
		return Manifest{}, fmt.Errorf("%s exceeds 1 MB", manifestName)
	}
	var manifest Manifest
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return Manifest{}, fmt.Errorf("parse %s: %w", manifestName, err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return Manifest{}, fmt.Errorf("parse %s: trailing data is not allowed", manifestName)
	}
	return manifest, nil
}

func extractPackage(path, destination string) error {
	if !strings.EqualFold(filepath.Ext(path), ".nonoka-plugin") && !strings.EqualFold(filepath.Ext(path), ".zip") {
		return errors.New("plugin package must have a .nonoka-plugin extension")
	}
	archive, err := zip.OpenReader(path)
	if err != nil {
		return fmt.Errorf("open plugin package: %w", err)
	}
	defer archive.Close()
	if len(archive.File) > maxPackageFiles {
		return errors.New("plugin package contains too many files")
	}
	var total int64
	for _, item := range archive.File {
		if item.FileInfo().Mode()&os.ModeSymlink != 0 {
			return errors.New("plugin packages may not contain symbolic links")
		}
		if item.UncompressedSize64 > uint64(maxPackageBytes) || total > maxPackageBytes-int64(item.UncompressedSize64) {
			return errors.New("plugin package exceeds the 2 GB extraction limit")
		}
		total += int64(item.UncompressedSize64)
		target, joinErr := safeJoin(destination, filepath.FromSlash(item.Name))
		if joinErr != nil {
			return errors.New("plugin package contains an unsafe path")
		}
		if item.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		source, err := item.Open()
		if err != nil {
			return err
		}
		mode := fs.FileMode(0o644)
		if item.Mode()&0o111 != 0 {
			mode = 0o755
		}
		output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode)
		if err == nil {
			_, err = io.Copy(output, source)
		}
		closeErr := outputClose(output)
		sourceErr := source.Close()
		if err == nil {
			err = errors.Join(closeErr, sourceErr)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func copyPackage(source, destination string) error {
	var total int64
	files := 0
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == source {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return errors.New("plugin directories may not contain symbolic links")
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target, err := safeJoin(destination, relative)
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			return err
		}
		files++
		total += info.Size()
		if files > maxPackageFiles || total > maxPackageBytes {
			return errors.New("plugin directory exceeds the installation limit")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		input, err := os.Open(path)
		if err != nil {
			return err
		}
		output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, info.Mode().Perm())
		if err == nil {
			_, err = io.Copy(output, input)
		}
		closeErr := outputClose(output)
		inputErr := input.Close()
		return errors.Join(err, closeErr, inputErr)
	})
}

func safeJoin(root, relative string) (string, error) {
	if strings.TrimSpace(relative) == "" || filepath.IsAbs(relative) {
		return "", errors.New("path must be relative")
	}
	clean := filepath.Clean(relative)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", errors.New("path escapes the plugin directory")
	}
	target := filepath.Join(root, clean)
	rel, err := filepath.Rel(root, target)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("path escapes the plugin directory")
	}
	return target, nil
}

func injectPagePolicy(html string) string {
	policy := `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; connect-src 'none'">`
	bridge := `<script>window.nonoka={apiVersion:1,post:function(method,params,id){parent.postMessage({source:'nonoka-plugin',apiVersion:1,id:id,method:method,params:params||{}},'*')}};parent.postMessage({source:'nonoka-plugin',apiVersion:1,method:'ui.ready',params:{}},'*');</script>`
	lower := strings.ToLower(html)
	if index := strings.Index(lower, "<head>"); index >= 0 {
		position := index + len("<head>")
		return html[:position] + policy + bridge + html[position:]
	}
	return "<!doctype html><html><head>" + policy + bridge + "</head><body>" + html + "</body></html>"
}

func (s *Service) loadRegistry() error {
	data, err := os.ReadFile(s.registryPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var value registry
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	value.Schema = 1
	if value.Enabled == nil {
		value.Enabled = map[string]bool{}
	}
	s.registry = value
	return nil
}

func (s *Service) saveRegistryLocked() error {
	s.registry.Schema = 1
	return writeJSONAtomic(s.registryPath, s.registry)
}

func writeJSONAtomic(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".plugin-*.tmp")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o600); err == nil {
		_, err = temporary.Write(append(data, '\n'))
	}
	closeErr := temporary.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, path)
	}
	return err
}

func outputClose(file *os.File) error {
	if file == nil {
		return nil
	}
	return file.Close()
}

func dialogCancelled(err error) bool {
	return errors.Is(err, context.Canceled) || strings.Contains(strings.ToLower(err.Error()), "cancel")
}

func (s *Service) emit(change Change) {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app != nil {
		app.Event.Emit("plugins:changed", change)
	}
}
