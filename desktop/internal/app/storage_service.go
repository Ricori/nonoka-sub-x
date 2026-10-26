package app

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
	"github.com/Ricori/nonoka-x/desktop/internal/storage"
	"github.com/wailsapp/wails/v3/pkg/application"
)

// measurementTTL keeps the settings page responsive. Walking a 15 GB runtime
// tree costs seconds on a cold filesystem cache, and the number only has to be
// good enough to show and to divide a progress bar by.
const measurementTTL = 30 * time.Second

// spaceMargin is the headroom demanded on the destination volume on top of the
// bytes being copied. A move that fills the target drive exactly is a move that
// leaves two unusable copies.
const spaceMargin = 1.05

// estimatedRuntimeBytes is what a finished local runtime occupies, measured on
// a complete Windows install: ~5.8 GB of Python runtime, ~3.7 GB of models and
// ~5.2 GB of download cache, rounded up.
//
// It is the installed footprint rather than the download size on purpose: the
// question it answers is "will this drive hold it", asked before the download
// starts, which is the only moment choosing a drive is free.
const estimatedRuntimeBytes = 16 << 30

// emptyInstallThreshold separates "nothing is installed yet" from "an install
// exists". A fresh install root is not literally empty — FineSub drops a store
// marker and a helper script into it — but it is orders of magnitude below
// anything that has downloaded a runtime or a model.
const emptyInstallThreshold = 64 << 20

// Location describes one relocatable directory for the settings page.
type Location struct {
	Target string `json:"target"`
	// Directory is what Nonoka X uses right now, relocated or not.
	Directory string `json:"directory"`
	Default   string `json:"default"`
	Custom    bool   `json:"custom"`
	// Missing marks a recorded directory that is not there any more, which is
	// what an unplugged external drive looks like. Nonoka X keeps pointing at it
	// rather than quietly reinstalling gigabytes somewhere else.
	Missing   bool   `json:"missing"`
	Bytes     int64  `json:"bytes"`
	Files     int    `json:"files"`
	FreeBytes int64  `json:"freeBytes"`
	Volume    string `json:"volume"`
	// Estimated is how much this directory is expected to grow to. Only the
	// runtime has a meaningful figure: the video cache is bounded by a limit the
	// user sets rather than by what has to be downloaded.
	Estimated int64 `json:"estimated"`
	// Empty reports that nothing has been installed here yet, which is the one
	// moment choosing a drive costs nothing — there is no data to move.
	Empty bool `json:"empty"`
	// EnoughSpace compares the volume's free space against Estimated. False is
	// what turns the install page's location notice into a warning.
	EnoughSpace bool `json:"enoughSpace"`
}

// RelocationProgress is the live state of at most one move. It is both the
// return value of the actions and the payload of the storage:progress event.
type RelocationProgress struct {
	Schema int    `json:"schema"`
	Active bool   `json:"active"`
	Target string `json:"target"`
	Source string `json:"source"`
	// Destination is where the move is going, or where the last one went.
	Destination string `json:"destination"`
	// Stage is one of idle, preparing, moving, finishing, completed, cancelled,
	// failed.
	Stage     string `json:"stage"`
	Message   string `json:"message"`
	Completed int64  `json:"completed"`
	Total     int64  `json:"total"`
	Error     string `json:"error"`
}

// StorageStatus is what the settings card renders.
type StorageStatus struct {
	Schema        int                `json:"schema"`
	DataDirectory string             `json:"dataDirectory"`
	Locations     []Location         `json:"locations"`
	Progress      RelocationProgress `json:"progress"`
}

// Destination is the resolved answer to a folder pick, so the UI can show the
// exact path and the space arithmetic before anything is moved.
type Destination struct {
	Target      string `json:"target"`
	Directory   string `json:"directory"`
	Cancelled   bool   `json:"cancelled"`
	Bytes       int64  `json:"bytes"`
	FreeBytes   int64  `json:"freeBytes"`
	SameVolume  bool   `json:"sameVolume"`
	EnoughSpace bool   `json:"enoughSpace"`
	// Resumable marks a destination that already holds part of this directory
	// from an interrupted attempt. ResumedBytes is how much of it is there, so
	// the confirmation can promise a continuation rather than a fresh copy.
	Resumable    bool   `json:"resumable"`
	ResumedBytes int64  `json:"resumedBytes"`
	Unavailable  string `json:"unavailable"`
}

type measurement struct {
	usage storage.Usage
	taken time.Time
}

// StorageService moves Nonoka X's two large directories to another drive.
//
// The move is the easy half; the hard half is that the FineSub install root is
// in use by a running sidecar whose Python interpreter lives inside it. So a
// runtime relocation stops the sidecar, moves, records, and starts it again
// against a freshly resolved configuration — the same sequence the managed
// Python installer already uses to swap interpreters.
type StorageService struct {
	dataDirectory string
	manager       *sidecar.Manager
	library       *library.Service

	mu     sync.Mutex
	app    *application.App
	window *application.WebviewWindow

	// running guards against a second relocation starting while one is in
	// flight; the two would fight over the sidecar.
	running  sync.Mutex
	stateMu  sync.RWMutex
	progress RelocationProgress
	cancel   context.CancelFunc

	measuredMu sync.Mutex
	measured   map[string]measurement
}

func NewStorageService(dataDirectory string, manager *sidecar.Manager, libraryService *library.Service) *StorageService {
	return &StorageService{
		dataDirectory: dataDirectory,
		manager:       manager,
		library:       libraryService,
		progress:      RelocationProgress{Schema: 1, Stage: "idle"},
		measured:      map[string]measurement{},
	}
}

func (s *StorageService) attach(app *application.App, window *application.WebviewWindow) {
	s.mu.Lock()
	s.app = app
	s.window = window
	s.mu.Unlock()
}

// Status reports both directories and any move in flight.
func (s *StorageService) Status() StorageStatus {
	return StorageStatus{
		Schema:        1,
		DataDirectory: s.dataDirectory,
		Locations: []Location{
			s.location(storage.RuntimeTarget),
			s.location(storage.VideoTarget),
		},
		Progress: s.Progress(),
	}
}

// Refresh re-measures both directories and reports the result. Status caches
// sizes for measurementTTL, which is right for repeated renders and wrong right
// after an install has just written several gigabytes: the install page decides
// whether to keep offering a location change from these numbers.
func (s *StorageService) Refresh() StorageStatus {
	s.forget()
	return s.Status()
}

func (s *StorageService) location(target string) Location {
	directory := storage.Directory(s.dataDirectory, target)
	fallback := storage.Default(s.dataDirectory, target)
	location := Location{
		Target:    target,
		Directory: directory,
		Default:   fallback,
		Custom:    !strings.EqualFold(filepath.Clean(directory), filepath.Clean(fallback)),
		FreeBytes: storage.FreeSpace(directory),
		Volume:    volumeLabel(directory),
	}
	if target == storage.RuntimeTarget {
		location.Estimated = estimatedRuntimeBytes
	}
	// A volume with no estimate to meet cannot fail the check.
	location.EnoughSpace = location.FreeBytes >= location.Estimated
	if info, err := os.Stat(directory); err != nil || !info.IsDir() {
		location.Missing = location.Custom
		// A directory that does not exist yet holds nothing, which is exactly
		// the state the install page offers to redirect.
		location.Empty = true
		return location
	}
	usage := s.measure(target, directory)
	location.Empty = usage.Bytes < emptyInstallThreshold
	location.Bytes = usage.Bytes
	location.Files = usage.Files
	return location
}

// measure caches directory sizes for measurementTTL. The settings page reads
// status on every render of its card, and a full walk of the runtime tree is
// far too expensive to repeat at that rate.
func (s *StorageService) measure(target, directory string) storage.Usage {
	key := target + "\x00" + directory
	s.measuredMu.Lock()
	cached, ok := s.measured[key]
	s.measuredMu.Unlock()
	if ok && time.Since(cached.taken) < measurementTTL {
		return cached.usage
	}
	usage := storage.Measure(directory)
	s.measuredMu.Lock()
	s.measured[key] = measurement{usage: usage, taken: time.Now()}
	s.measuredMu.Unlock()
	return usage
}

func (s *StorageService) forget() {
	s.measuredMu.Lock()
	clear(s.measured)
	s.measuredMu.Unlock()
}

// ChooseDirectory opens the folder picker and resolves what the pick means: the
// exact directory Nonoka X would own, whether it is on the same volume (a move
// that costs nothing) and whether the volume has room.
func (s *StorageService) ChooseDirectory(target string) (Destination, error) {
	if target != storage.RuntimeTarget && target != storage.VideoTarget {
		return Destination{}, errors.New("unknown storage target")
	}
	s.mu.Lock()
	app, window := s.app, s.window
	s.mu.Unlock()
	if app == nil || window == nil {
		return Destination{}, errors.New("application window is not ready")
	}
	picked, err := app.Dialog.OpenFile().
		SetTitle(chooseTitle(target)).
		AttachToWindow(window).
		CanChooseDirectories(true).
		CanChooseFiles(false).
		CanCreateDirectories(true).
		PromptForSingleSelection()
	if dialogDismissed(err) {
		return Destination{Target: target, Cancelled: true}, nil
	}
	if err != nil {
		return Destination{}, err
	}
	if strings.TrimSpace(picked) == "" {
		return Destination{Target: target, Cancelled: true}, nil
	}
	return s.describeDestination(target, picked)
}

func (s *StorageService) describeDestination(target, picked string) (Destination, error) {
	destination, err := storage.DestinationFor(picked, target)
	if err != nil {
		return Destination{}, err
	}
	result := Destination{Target: target, Directory: destination}
	if err := storage.ValidateDestination(s.dataDirectory, target, destination); err != nil {
		result.Unavailable = err.Error()
		return result, nil
	}
	source := storage.Directory(s.dataDirectory, target)
	usage := s.measure(target, source)
	result.Bytes = usage.Bytes
	result.FreeBytes = storage.FreeSpace(destination)
	result.SameVolume = sameVolume(source, destination)
	if carried, resumable := storage.ResumableCopy(source, destination); resumable {
		result.Resumable = true
		result.ResumedBytes = carried.Bytes
	}
	// Only what is still missing has to fit: a resumed copy has already paid
	// for the bytes sitting at the destination.
	remaining := result.Bytes - result.ResumedBytes
	result.EnoughSpace = result.SameVolume || float64(result.FreeBytes) >= float64(remaining)*spaceMargin
	if !result.EnoughSpace {
		result.Unavailable = "目标磁盘剩余空间不足"
	}
	return result, nil
}

// Relocate moves target to destination and records the new location. It returns
// as soon as the move has started; progress arrives on storage:progress.
func (s *StorageService) Relocate(target, destination string) (RelocationProgress, error) {
	if target != storage.RuntimeTarget && target != storage.VideoTarget {
		return s.Progress(), errors.New("unknown storage target")
	}
	resolved, err := filepath.Abs(strings.TrimSpace(destination))
	if err != nil {
		return s.Progress(), err
	}
	if err := storage.ValidateDestination(s.dataDirectory, target, resolved); err != nil {
		return s.Progress(), err
	}
	return s.start(target, resolved)
}

// ResetLocation moves target back under the Nonoka X data directory.
func (s *StorageService) ResetLocation(target string) (RelocationProgress, error) {
	fallback := storage.Default(s.dataDirectory, target)
	if fallback == "" {
		return s.Progress(), errors.New("unknown storage target")
	}
	if err := storage.ValidateDestination(s.dataDirectory, target, fallback); err != nil {
		return s.Progress(), err
	}
	return s.start(target, fallback)
}

// OpenDirectory shows target's current directory in the system file manager.
// It takes the target rather than a path so the page can only ever open the two
// directories it describes.
func (s *StorageService) OpenDirectory(target string) error {
	if target != storage.RuntimeTarget && target != storage.VideoTarget {
		return errors.New("unknown storage target")
	}
	directory := storage.Directory(s.dataDirectory, target)
	if info, err := os.Stat(directory); err != nil || !info.IsDir() {
		return errors.New("directory is not available")
	}
	switch runtime.GOOS {
	case "windows":
		return exec.Command("explorer.exe", directory).Start()
	case "darwin":
		return exec.Command("open", directory).Start()
	default:
		return exec.Command("xdg-open", directory).Start()
	}
}

func (s *StorageService) start(target, destination string) (RelocationProgress, error) {
	if !s.running.TryLock() {
		return s.Progress(), errors.New("已有一个目录迁移正在进行")
	}
	source := storage.Directory(s.dataDirectory, target)
	usage := s.measure(target, source)
	jobContext, cancel := context.WithCancel(context.Background())
	s.stateMu.Lock()
	s.cancel = cancel
	s.progress = RelocationProgress{
		Schema:      1,
		Active:      true,
		Target:      target,
		Source:      source,
		Destination: destination,
		Stage:       "preparing",
		Message:     "正在准备迁移",
		Total:       usage.Bytes,
	}
	started := s.progress
	s.stateMu.Unlock()
	s.emit(started)
	go func() {
		defer cancel()
		defer s.running.Unlock()
		s.run(jobContext, target, source, destination, usage.Bytes)
	}()
	return started, nil
}

func (s *StorageService) run(ctx context.Context, target, source, destination string, total int64) {
	// The install root holds the interpreter the sidecar is running from, so it
	// cannot be moved underneath it. Video files are only ever opened for the
	// duration of a read, so that half needs no shutdown.
	restart := false
	if target == storage.RuntimeTarget && s.manager != nil {
		s.publish(func(p *RelocationProgress) {
			p.Stage = "preparing"
			p.Message = "正在停止本地服务"
		})
		stopContext, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
		stopErr := s.manager.Stop(stopContext)
		cancel()
		if stopErr != nil {
			s.fail("迁移失败，未做任何改动", fmt.Errorf("停止本地服务失败：%w", stopErr))
			return
		}
		restart = true
	}
	carried, resuming := storage.ResumableCopy(source, destination)
	s.publish(func(p *RelocationProgress) {
		p.Stage = "moving"
		p.Message = "正在移动文件"
		if resuming {
			// Starting the bar at zero for a copy that is already 80% done
			// reads as "it lost everything", which is exactly what it did not.
			p.Completed = carried.Bytes
			p.Message = "正在继续上次未完成的迁移"
		}
	})
	throttle := time.Now()
	moveErr := storage.Move(ctx, source, destination, total, func(copied int64) {
		// One event per file would flood the bridge on a tree with a hundred
		// thousand of them, and the bar cannot show more than a few frames a
		// second anyway.
		if time.Since(throttle) < 200*time.Millisecond && copied < total {
			return
		}
		throttle = time.Now()
		s.publish(func(p *RelocationProgress) { p.Completed = copied })
	})
	leftovers := ""
	switch {
	case errors.Is(moveErr, context.Canceled):
		s.restoreSidecar(restart)
		s.forget()
		s.publish(func(p *RelocationProgress) {
			p.Active = false
			p.Stage = "cancelled"
			p.Message = "迁移已取消，原目录未改动；已复制的部分保留，下次会继续"
		})
		return
	case storage.SourceRemains(moveErr):
		leftovers = moveErr.Error()
	case moveErr != nil:
		s.restoreSidecar(restart)
		s.forget()
		s.fail("迁移失败，原目录未改动；已复制的部分保留，重试会从中断处继续", moveErr)
		return
	}
	s.publish(func(p *RelocationProgress) {
		p.Completed = total
		p.Stage = "finishing"
		p.Message = "正在保存新位置"
	})
	if _, err := storage.Set(s.dataDirectory, target, destination); err != nil {
		// The payload is already at the destination. Reporting the failure
		// without moving it back is deliberate: a second move is another hour
		// of copying, and the user can retry the record by relocating again.
		s.fail("文件已迁移到新位置，但位置记录保存失败", fmt.Errorf("文件在 %s，重新执行一次迁移即可写入记录：%w", destination, err))
		return
	}
	s.forget()
	applyErr := s.apply(target, destination, restart)
	s.publish(func(p *RelocationProgress) {
		p.Active = false
		p.Stage = "completed"
		p.Message = "迁移完成"
		if leftovers != "" {
			p.Message = "迁移完成，" + leftovers
		}
		if applyErr != nil {
			p.Message = "迁移完成，但需要重启 Nonoka X 才能生效"
			p.Error = applyErr.Error()
		}
	})
}

// apply points the running application at the new directory so the move needs
// no restart.
func (s *StorageService) apply(target, destination string, restart bool) error {
	if target == storage.VideoTarget {
		if s.library == nil {
			return nil
		}
		return library.SetCacheDirectory(s.library, destination)
	}
	if !restart || s.manager == nil {
		return nil
	}
	config, configErr := SidecarConfig()
	if config.Executable == "" {
		return configErr
	}
	if err := s.manager.Configure(config); err != nil {
		return err
	}
	startContext, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
	defer cancel()
	if _, err := s.manager.Start(startContext); err != nil {
		return err
	}
	return nil
}

// restoreSidecar brings the local service back after a move that did not
// happen, using the configuration that is still recorded.
func (s *StorageService) restoreSidecar(restart bool) {
	if !restart || s.manager == nil {
		return
	}
	config, _ := SidecarConfig()
	if config.Executable == "" {
		return
	}
	if err := s.manager.Configure(config); err != nil {
		log.Printf("storage relocation: restore sidecar configuration: %v", err)
		return
	}
	startContext, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
	defer cancel()
	if _, err := s.manager.Start(startContext); err != nil {
		log.Printf("storage relocation: restart sidecar: %v", err)
	}
}

// CancelRelocation stops a move in progress. The partial copy is removed and
// the source is left exactly as it was.
func (s *StorageService) CancelRelocation() RelocationProgress {
	s.stateMu.Lock()
	cancel := s.cancel
	s.stateMu.Unlock()
	if cancel != nil {
		cancel()
	}
	return s.Progress()
}

func (s *StorageService) Progress() RelocationProgress {
	s.stateMu.RLock()
	defer s.stateMu.RUnlock()
	return s.progress
}

func (s *StorageService) publish(mutate func(*RelocationProgress)) {
	s.stateMu.Lock()
	mutate(&s.progress)
	current := s.progress
	s.stateMu.Unlock()
	s.emit(current)
}

// fail ends the job. The message is passed in because what survives a failure
// differs by where it happened, and telling the user "原目录未改动" after the
// source has already been deleted would be worse than saying nothing.
func (s *StorageService) fail(message string, err error) {
	s.publish(func(p *RelocationProgress) {
		p.Active = false
		p.Stage = "failed"
		p.Message = message
		p.Error = err.Error()
	})
}

func (s *StorageService) emit(progress RelocationProgress) {
	s.mu.Lock()
	app := s.app
	s.mu.Unlock()
	if app != nil {
		app.Event.Emit("storage:progress", progress)
	}
}

func chooseTitle(target string) string {
	if target == storage.RuntimeTarget {
		return "选择存放运行环境的位置"
	}
	return "选择存放视频缓存的位置"
}

// sameVolume reports whether a move can be a rename. Only Windows answers it
// from the path alone; elsewhere the answer is "assume not", which costs a
// space check that Move's rename fast path then makes irrelevant.
func sameVolume(source, destination string) bool {
	left := filepath.VolumeName(filepath.Clean(source))
	right := filepath.VolumeName(filepath.Clean(destination))
	if left == "" || right == "" {
		return false
	}
	return strings.EqualFold(left, right)
}

func volumeLabel(directory string) string {
	if volume := filepath.VolumeName(filepath.Clean(directory)); volume != "" {
		return volume
	}
	return "/"
}

// dialogDismissed reports whether the folder picker closed because the user
// backed out. Windows returns that as an error while the other platforms return
// an empty selection, the same split library.PickRelink already handles.
func dialogDismissed(err error) bool {
	if err == nil {
		return false
	}
	text := strings.ToLower(err.Error())
	return strings.Contains(text, "cancel") || strings.Contains(text, "canceled") || strings.Contains(text, "cancelled")
}
