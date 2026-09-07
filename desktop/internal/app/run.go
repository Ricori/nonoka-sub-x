package app

import (
	"context"
	"errors"
	"io/fs"
	"log"
	"path/filepath"
	"runtime"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/assstyles"
	"github.com/Ricori/nonoka-x/desktop/internal/cloud"
	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
	"github.com/Ricori/nonoka-x/desktop/internal/plugins"
	"github.com/Ricori/nonoka-x/desktop/internal/preferences"
	"github.com/Ricori/nonoka-x/desktop/internal/provider"
	"github.com/Ricori/nonoka-x/desktop/internal/selfupdate"
	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
	"github.com/Ricori/nonoka-x/desktop/internal/taskhistory"
	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

const lifecycleTimeout = 20 * time.Second

var bundledFontNames = []string{"FZZhunYuan.woff2", "JingNanBoBoHei.woff2"}

func init() {
	application.RegisterEvent[[]string]("files:dropped")
	application.RegisterEvent[[]library.Entry]("library:changed")
	application.RegisterEvent[string]("editor:request-close")
	application.RegisterEvent[bool]("home:refresh")
	application.RegisterEvent[selfupdate.Status]("update:status")
	application.RegisterEvent[map[string]string]("update:ready")
	application.RegisterEvent[RelocationProgress]("storage:progress")
}

// Run owns the desktop lifecycle. Sidecar startup failure is intentionally
// non-fatal: the shell remains available so the settings/runtime UI can explain
// what is missing. The failure is retained in the manager's safe Snapshot.
func Run(assets fs.FS) error {
	// Before anything resolves an executable: a macOS app started from Finder
	// inherits launchd's bare PATH, and every LookPath below -- the sidecar
	// interpreter, ffmpeg, git -- would otherwise miss tools the user has
	// installed.
	managedtools.EnsureSearchPath()
	selfupdate.CleanupReplacedExecutables()
	migration, err := migrateDefaultDataDirectory()
	if err != nil {
		return err
	}
	if migration != nil {
		return showDataDirectoryMigrationComplete(migration)
	}
	dataDirectory, err := DataDirectory()
	if err != nil {
		return err
	}
	libraryService, err := library.New(dataDirectory)
	if err != nil {
		return err
	}
	fonts := make(map[string][]byte, len(bundledFontNames))
	for _, name := range bundledFontNames {
		data, readErr := fs.ReadFile(assets, "fonts/"+name)
		if readErr != nil {
			log.Printf("bundled font %s: %v", name, readErr)
			continue
		}
		fonts[name] = data
	}
	library.SetBundledFonts(libraryService, fonts)
	preferencesService, err := preferences.New(dataDirectory)
	if err != nil {
		return err
	}
	assStyleService, err := assstyles.New(dataDirectory)
	if err != nil {
		return err
	}
	// Constructed after preferences so the first launch can lift any history
	// still sitting in preferences.json before that file is rewritten.
	taskHistoryService, err := taskhistory.New(dataDirectory)
	if err != nil {
		return err
	}
	pluginService, err := plugins.New(dataDirectory, libraryService)
	if err != nil {
		return err
	}
	config, configErr := SidecarConfig()
	manager := sidecar.New(config)
	pythonBootstrap := NewPythonBootstrap(dataDirectory, manager)
	if configErr != nil {
		log.Printf("sidecar configuration: %v", configErr)
	}
	// No interpreter carries the bootstrap dependencies yet. Starting nothing
	// keeps the sidecar reported as down, which is what puts the managed-Python
	// installer in front of the user instead of a sidecar that cannot provision.
	if config.Executable != "" {
		startupContext, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
		_, startErr := manager.Start(startupContext)
		cancel()
		if startErr != nil {
			log.Printf("sidecar startup: %v", startErr)
		}
	}
	providerService, err := provider.New(manager, pythonBootstrap)
	if err != nil {
		return err
	}
	// Plugins reach subtitle documents through the same provider the editor
	// uses, so a plugin write goes through the sidecar's revision check rather
	// than touching document.json behind the editor's back.
	plugins.SetDocuments(pluginService, providerService)
	// And the engine itself: LLM calls, stage runs and the artifacts a run
	// leaves behind. Same provider, granted separately, because it is a much
	// wider surface than the document one and the call site should say so.
	plugins.SetEngine(pluginService, providerService)
	cloudService, err := cloud.New(dataDirectory, manager, libraryService)
	if err != nil {
		return err
	}
	windowService := NewWindowService(preferencesService, libraryService)
	storageService := NewStorageService(dataDirectory, manager, libraryService)
	updateService, err := selfupdate.New(dataDirectory)
	if err != nil {
		return err
	}

	applicationInstance := application.New(application.Options{
		Name:        "Nonoka Sub X",
		Description: "Nonoka Sub X",
		Services: []application.Service{
			application.NewService(providerService),
			application.NewService(libraryService),
			application.NewService(cloudService),
			application.NewService(pluginService),
			application.NewService(preferencesService),
			application.NewService(assStyleService),
			application.NewService(taskHistoryService),
			application.NewService(windowService),
			application.NewService(storageService),
			application.NewService(updateService),
		},
		Assets: application.AssetOptions{
			Handler: application.AssetFileServerFS(assets),
		},
		Mac: application.MacOptions{
			ApplicationShouldTerminateAfterLastWindowClosed: true,
		},
		Windows: application.WindowsOptions{WebviewUserDataPath: filepath.Join(dataDirectory, "webview")},
	})
	homeOptions := applyWindowOptions(preferencesService, "home", applyWindowTheme(preferencesService, "home", application.WebviewWindowOptions{
		Name:            "home",
		Title:           "Nonoka Sub X",
		Width:           1180,
		Height:          760,
		MinWidth:        960,
		MinHeight:       620,
		InitialPosition: application.WindowCentered,
		URL:             "/index.html",
		EnableFileDrop:  true,
		Frameless:       runtime.GOOS == "windows",
		Windows: application.WindowsWindow{
			// Composition hosting is what turns --wails-non-client-region into real
			// WM_NCHITTEST results, so the custom titlebar drags, snap-layouts and
			// caption buttons behave like a native frame instead of a JS mousemove.
			NonClientRegionSupport:     true,
			WebView2CompositionHosting: true,
		},
	}))
	homeOptions, deferredState := deferWindowStart(homeOptions)
	home := applicationInstance.Window.NewWithOptions(homeOptions)
	windowService.attach(applicationInstance, home)
	storageService.attach(applicationInstance, home)
	plugins.Attach(pluginService, applicationInstance, home)
	// A missing or unreachable manifest must not block startup: the shell
	// simply stays on the current build.
	if err := selfupdate.Attach(updateService, applicationInstance); err != nil {
		log.Printf("auto update: %v", err)
	} else {
		selfupdate.Start(updateService, windowService.editorIsOpen)
	}
	if deferredState != application.WindowStateNormal {
		home.OnWindowEvent(events.Common.WindowRuntimeReady, func(_ *application.WindowEvent) { showDeferredWindow(home, deferredState) })
	}
	trackWindowState(preferencesService, "home", applicationInstance, home).Activate()
	library.Attach(libraryService, applicationInstance, home)
	home.OnWindowEvent(events.Common.WindowFilesDropped, func(event *application.WindowEvent) {
		applicationInstance.Event.Emit("files:dropped", event.Context().DroppedFiles())
	})
	runErr := applicationInstance.Run()
	shutdownContext, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
	stopErr := manager.Stop(shutdownContext)
	cancel()
	closeErr := libraryService.Close()
	// Last thing before the process ends: a build downloaded during this
	// session is swapped in by a helper that waits for us to exit, so the next
	// launch comes up on it.
	selfupdate.InstallOnExit(updateService)
	return errors.Join(runErr, stopErr, closeErr)
}

func showDataDirectoryMigrationComplete(migration *dataDirectoryMigration) error {
	instance := application.New(application.Options{
		Name:   "Nonoka Sub X",
		Assets: application.AlphaAssets,
	})
	instance.Window.NewWithOptions(application.WebviewWindowOptions{
		Title:  "Nonoka Sub X",
		Width:  1,
		Height: 1,
		Hidden: true,
	})
	instance.Event.OnApplicationEvent(events.Common.ApplicationStarted, func(*application.ApplicationEvent) {
		instance.Dialog.Info().
			SetTitle("Nonoka Sub X 数据迁移完成").
			SetMessage("数据已从旧目录迁移到：\n" + migration.Destination + "\n\n请重新启动 Nonoka Sub X。").
			Show()
		instance.Quit()
	})
	return instance.Run()
}
