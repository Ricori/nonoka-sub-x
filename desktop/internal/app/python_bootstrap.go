package app

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
)

const (
	bootstrapPythonVersion = "3.12"
	bootstrapUVVersion     = "0.11.32"
	bootstrapUVRelease     = "https://github.com/astral-sh/uv/releases/download/" + bootstrapUVVersion + "/"
	bootstrapPyPIIndex     = "https://pypi.tuna.tsinghua.edu.cn/simple"
	bootstrapGitHubMirror  = "https://gh-proxy.com/"
	bootstrapPythonRelease = "https://github.com/astral-sh/python-build-standalone/releases/download"
	// bootstrapSpeedSample times the route to GitHub before uv fetches an
	// interpreter. An immutable release from the same repository.
	bootstrapSpeedSample = bootstrapPythonRelease + "/20250317/cpython-3.12.9%2B20250317-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
)

// bootstrapUVAsset pins one uv build. uv is the whole installer: it downloads
// the interpreter, creates the launcher environment and installs its
// dependencies, so pinning it by size and digest pins the only binary the
// bootstrap ever executes without having built it.
type bootstrapUVAsset struct {
	Name   string
	Size   int64
	SHA256 string
	// Tar distinguishes the .tar.gz assets from the Windows .zip. The two
	// archive formats are the only thing that differs between platforms; the
	// installation steps below are identical.
	Tar bool
}

func (a bootstrapUVAsset) URL() string { return bootstrapUVRelease + a.Name }

// bootstrapUVAssets is also the list of platforms the managed launcher supports:
// a GOOS/GOARCH pair with no uv build here has no way to install one, which is
// exactly what the unsupported state reports. Windows x64 pins the same asset
// FineSub's own runtime manifest does -- a test holds the two together.
var bootstrapUVAssets = map[string]bootstrapUVAsset{
	"windows/amd64": {
		Name:   "uv-x86_64-pc-windows-msvc.zip",
		Size:   25726255,
		SHA256: "acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984",
	},
	"darwin/arm64": {
		Name:   "uv-aarch64-apple-darwin.tar.gz",
		Size:   22510462,
		SHA256: "ed336d0ba49db8ef89b2b41fffa372ce63bd032f22a56f001c265891aec32829",
		Tar:    true,
	},
	"darwin/amd64": {
		Name:   "uv-x86_64-apple-darwin.tar.gz",
		Size:   24228395,
		SHA256: "77f5ca26c0de20e992a3677a174fe1121ee25c36f9b1434a863f75bf077a05eb",
		Tar:    true,
	},
}

func bootstrapUVAssetForHost() (bootstrapUVAsset, bool) {
	asset, ok := bootstrapUVAssets[runtime.GOOS+"/"+runtime.GOARCH]
	return asset, ok
}

// gitHubMirrorPrefix is what the mirror rung puts in front of a GitHub URL. Only
// one mirror on purpose: a dead one is not a free extra attempt, it is one
// download timeout the user waits through before finding out.
func gitHubMirrorPrefix() string {
	for _, name := range []string{"NONOKA_GITHUB_PROXY", "FINESUB_GITHUB_FILE_PROXY"} {
		if custom := strings.TrimSpace(os.Getenv(name)); custom != "" {
			return strings.TrimRight(custom, "/") + "/"
		}
	}
	return bootstrapGitHubMirror
}

// bootstrapSource is one place to fetch a file from and the route to take there.
type bootstrapSource struct {
	bootstrapAttempt
	URL string
}

// gitHubDownloadSources lays a GitHub file out along the ladder.
func gitHubDownloadSources(rawURL string, attempts []bootstrapAttempt) []bootstrapSource {
	sources := make([]bootstrapSource, 0, len(attempts))
	for _, attempt := range attempts {
		source := bootstrapSource{bootstrapAttempt: attempt, URL: rawURL}
		if attempt.Mirror {
			source.URL = gitHubMirrorPrefix() + rawURL
		}
		sources = append(sources, source)
	}
	return sources
}

type bootstrapProgress struct {
	Completed      int64   `json:"completed"`
	Total          int64   `json:"total"`
	Unit           string  `json:"unit"`
	BytesPerSecond float64 `json:"bytes_per_second,omitempty"`
}

type pythonBootstrapState struct {
	Schema    int                `json:"schema"`
	Platform  string             `json:"platform"`
	Supported bool               `json:"supported"`
	State     string             `json:"state"`
	Stage     string             `json:"stage"`
	Message   string             `json:"message"`
	Progress  *bootstrapProgress `json:"progress"`
	Python    string             `json:"python,omitempty"`
	// Proxy names the local proxy this install downloads through, or "".
	Proxy string `json:"proxy"`
}

// PythonBootstrap installs only the small launcher environment needed to run
// the sidecar. The sidecar remains responsible for the much larger AI runtime.
// Keeping this bootstrap in Go avoids requiring Python in order to install
// Python on a fresh machine.
type PythonBootstrap struct {
	mu           sync.RWMutex
	state        pythonBootstrapState
	dataDir      string
	requirements string
	manager      *sidecar.Manager
	usable       func(string) bool
	uv           bootstrapUVAsset
}

func NewPythonBootstrap(dataDirectory string, manager *sidecar.Manager) *PythonBootstrap {
	return newPythonBootstrap(dataDirectory, manager, interpreterCanRunSidecar)
}

func newPythonBootstrap(dataDirectory string, manager *sidecar.Manager, usable func(string) bool) *PythonBootstrap {
	uv, supported := bootstrapUVAssetForHost()
	state := pythonBootstrapState{
		Schema:    1,
		Platform:  runtime.GOOS + "-" + runtime.GOARCH,
		Supported: supported,
		State:     "missing",
		Stage:     "waiting",
		Message:   "需要下载 Python 以启用本地服务",
	}
	python := managedtools.BootstrapPython(dataDirectory)
	if info, err := os.Stat(python); err == nil && !info.IsDir() {
		state.Python = python
		if usable(python) {
			state.State = "ready"
			state.Stage = "completed"
			state.Message = "Python 启动环境已安装"
		} else {
			state.State = "failed"
			state.Stage = "failed"
			state.Message = "Python 启动环境已损坏，可点击“修复环境”自动重建"
		}
	}
	// Only the state the platform actually blocks: a launcher that is already
	// installed -- or already broken -- has its own message, and overwriting it
	// would hide the repair offer behind a platform notice.
	if !state.Supported && state.State == "missing" {
		state.Message = unsupportedBootstrapMessage
	}
	return &PythonBootstrap{
		state:        state,
		dataDir:      dataDirectory,
		requirements: bootstrapRequirementsPath(dataDirectory),
		manager:      manager,
		usable:       usable,
		uv:           uv,
	}
}

// The platforms named here are the keys of bootstrapUVAssets, spelled the way a
// user would recognise them.
const unsupportedBootstrapMessage = "自动安装 Python 当前仅支持 Windows x64 与 macOS（Apple 芯片 / Intel）"

func bootstrapRequirementsPath(dataDirectory string) string {
	if resourceRoot, _ := ensureBundledResources(dataDirectory); resourceRoot != "" {
		candidate := filepath.Join(resourceRoot, "bootstrap-requirements.py312.txt")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	workingDirectory, _ := os.Getwd()
	for _, candidate := range []string{
		filepath.Join(workingDirectory, "..", "bootstrap-requirements.py312.txt"),
		filepath.Join(workingDirectory, "bootstrap-requirements.py312.txt"),
	} {
		if resolved, err := filepath.Abs(candidate); err == nil {
			if _, statErr := os.Stat(resolved); statErr == nil {
				return resolved
			}
		}
	}
	return filepath.Join(workingDirectory, "..", "bootstrap-requirements.py312.txt")
}

func (b *PythonBootstrap) Status() map[string]any {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.stateMapLocked()
}

func (b *PythonBootstrap) Install() (map[string]any, error) {
	b.mu.Lock()
	if !b.state.Supported {
		result := b.stateMapLocked()
		b.mu.Unlock()
		return result, errors.New(unsupportedBootstrapMessage)
	}
	if b.state.State == "running" {
		result := b.stateMapLocked()
		b.mu.Unlock()
		return result, nil
	}
	b.state.State = "running"
	b.state.Stage = "preparing"
	b.state.Message = "正在准备 Python 下载"
	b.state.Progress = nil
	b.state.Proxy = ""
	result := b.stateMapLocked()
	b.mu.Unlock()
	go b.install()
	return result, nil
}

func (b *PythonBootstrap) stateMapLocked() map[string]any {
	result := map[string]any{
		"schema": b.state.Schema, "platform": b.state.Platform,
		"supported": b.state.Supported, "state": b.state.State,
		"stage": b.state.Stage, "message": b.state.Message,
		"progress": nil, "python": b.state.Python, "proxy": b.state.Proxy,
	}
	if b.state.Progress != nil {
		result["progress"] = map[string]any{
			"completed":        b.state.Progress.Completed,
			"total":            b.state.Progress.Total,
			"unit":             b.state.Progress.Unit,
			"bytes_per_second": b.state.Progress.BytesPerSecond,
		}
	}
	return result
}

func (b *PythonBootstrap) update(stage, message string, progress *bootstrapProgress) {
	b.mu.Lock()
	b.state.Stage = stage
	b.state.Message = message
	b.state.Progress = progress
	b.mu.Unlock()
}

func (b *PythonBootstrap) fail(err error) {
	b.mu.Lock()
	b.state.State = "failed"
	b.state.Stage = "failed"
	b.state.Message = err.Error()
	b.state.Progress = nil
	b.mu.Unlock()
}

func (b *PythonBootstrap) install() {
	python := managedtools.BootstrapPython(b.dataDir)
	if info, err := os.Stat(python); err != nil || info.IsDir() || !b.usable(python) {
		if err := b.installPython(); err != nil {
			b.fail(err)
			return
		}
	}
	b.update("activating", "正在启动本地服务", nil)
	config, err := SidecarConfig()
	if err == nil {
		err = b.manager.Configure(config)
		// A sidecar already running on a system interpreter cannot build its
		// provisioner, and the managed Python only takes effect once that
		// process is swapped for one started from it.
		if errors.Is(err, sidecar.ErrAlreadyRunning) && b.manager.Executable() != config.Executable {
			stopContext, cancelStop := context.WithTimeout(context.Background(), lifecycleTimeout)
			err = b.manager.Stop(stopContext)
			cancelStop()
			if err == nil {
				err = b.manager.Configure(config)
			}
		}
	}
	if err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTimeout)
		_, err = b.manager.Start(ctx)
		cancel()
	}
	if err != nil && !errors.Is(err, sidecar.ErrAlreadyRunning) {
		b.fail(fmt.Errorf("Python 已安装，但本地服务启动失败：%w", err))
		return
	}
	b.mu.Lock()
	b.state.State = "ready"
	b.state.Stage = "completed"
	b.state.Message = "Python 已安装，本地服务已启用"
	b.state.Progress = nil
	b.state.Python = python
	b.mu.Unlock()
}

func (b *PythonBootstrap) installPython() error {
	root := filepath.Join(b.dataDir, "bootstrap")
	downloads := filepath.Join(root, "downloads")
	tools := filepath.Join(root, "tools")
	launcher := filepath.Join(root, "launcher")
	staging := launcher + ".staging"
	if err := os.MkdirAll(downloads, 0o700); err != nil {
		return fmt.Errorf("创建 Python 下载目录失败：%w", err)
	}
	b.update("preparing", "正在选择下载线路", nil)
	attempts := resolveBootstrapAttempts()
	uvArchive := filepath.Join(downloads, bootstrapUVVersion+"-"+b.uv.Name)
	if !fileMatches(uvArchive, b.uv.Size, b.uv.SHA256) {
		sources := gitHubDownloadSources(b.uv.URL(), attempts)
		if err := b.downloadWithFallback(sources, uvArchive, b.uv.Size, b.uv.SHA256); err != nil {
			return err
		}
	}
	b.update("extracting", "正在解压 Python 安装器", nil)
	if err := os.RemoveAll(tools); err != nil {
		return err
	}
	uvName := "uv"
	if runtime.GOOS == "windows" {
		uvName = "uv.exe"
	}
	uv := filepath.Join(tools, uvName)
	if err := extractNamedArchiveFile(uvArchive, b.uv.Tar, uvName, uv); err != nil {
		return fmt.Errorf("解压 Python 安装器失败：%w", err)
	}
	if err := os.RemoveAll(staging); err != nil {
		return err
	}
	environment := append(os.Environ(),
		"UV_CACHE_DIR="+filepath.Join(root, "cache"),
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(root, "python-installations"),
		"UV_NO_PROGRESS=1",
	)

	pythonAttempts := attempts
	if os.Getenv("UV_PYTHON_INSTALL_MIRROR") != "" {
		pythonAttempts = withoutMirror(attempts)
	}
	installed := climbBootstrap(pythonAttempts, "安装 Python 失败", b.announcer("installing_python", "正在下载并安装 Python "+bootstrapPythonVersion), func(attempt bootstrapAttempt, watched bool) ([]byte, error) {
		if watched && bootstrapRouteTooSlow(bootstrapSpeedSample, attempt) {
			return nil, errBootstrapTooSlow
		}
		_ = os.RemoveAll(staging)
		step := slices.Concat(environment, attempt.environment())
		if attempt.Mirror {
			step = append(step, "UV_PYTHON_INSTALL_MIRROR="+gitHubMirrorPrefix()+bootstrapPythonRelease)
		}
		return runBootstrapCommand(uv, step, "venv", staging, "--python", bootstrapPythonVersion, "--managed-python", "--no-project")
	})
	if installed != nil {
		return installed
	}
	stagingPython := managedtools.VenvPython(staging)
	if err := installLauncherRequirements(uv, environment, attempts, b.announcer("installing_dependencies", "正在安装本地服务基础依赖"), stagingPython, b.requirements); err != nil {
		return err
	}
	if output, err := runBootstrapCommand(stagingPython, environment, "-I", "-c", "import httpx, pydantic"); err != nil {
		return fmt.Errorf("验证 Python 启动环境失败：%w%s", err, commandDetail(output))
	}
	if err := os.RemoveAll(launcher); err != nil {
		return err
	}
	if err := os.Rename(staging, launcher); err != nil {
		return fmt.Errorf("启用 Python 启动环境失败：%w", err)
	}
	return nil
}

// installLauncherRequirements installs the hash-pinned launcher requirements
// into python, one rung of the ladder at a time.
func installLauncherRequirements(uv string, environment []string, attempts []bootstrapAttempt, announce func(bootstrapAttempt, bool), python, requirements string) error {
	if os.Getenv("UV_DEFAULT_INDEX") != "" || os.Getenv("UV_INDEX_URL") != "" || os.Getenv("PIP_INDEX_URL") != "" {
		attempts = withoutMirror(attempts)
	}
	return climbBootstrap(attempts, "安装 Python 基础依赖失败", announce, func(attempt bootstrapAttempt, _ bool) ([]byte, error) {
		step := slices.Concat(environment, attempt.environment())
		if attempt.Mirror {
			step = append(step, "UV_DEFAULT_INDEX="+bootstrapPyPIIndex)
		}
		return runBootstrapCommand(uv, step, "pip", "install", "--python", python, "--require-hashes", "-r", requirements)
	})
}

// announcer shows which rung a uv step is on, for the runtime page.
func (b *PythonBootstrap) announcer(stage, message string) func(bootstrapAttempt, bool) {
	return func(attempt bootstrapAttempt, retrying bool) {
		b.mu.Lock()
		b.state.Proxy = proxyLabel(attempt.Proxy)
		b.mu.Unlock()
		text := message
		if retrying {
			text = "上一条线路失败或过慢，正在改用" + attempt.label() + "重试：" + message
		}
		b.update(stage, text, nil)
	}
}

// harnessProbeScript imports what the launcher gained so it can continue a
// cloud task from its stable.json without the managed runtime: FineSub's
// `[harness]` extra, less `cryptography`. tzdata is Windows-only, as in the
// requirements' marker.
const harnessProbeScript = "import sys, httpx, opencc; sys.platform != 'win32' or __import__('tzdata')"

// TopUpLauncher installs requirements an existing launcher predates. The
// install path above only runs on a launcher that is missing or broken, so a
// launcher from before the harness packages would otherwise never get them,
// and every cloud task's local leg would fail on it until the user rebuilt the
// environment by hand. Best-effort and quiet: it needs the uv the original
// install left behind, and any failure leaves the launcher as it was -- the
// runtime page still names the missing packages.
func (b *PythonBootstrap) TopUpLauncher() {
	b.mu.RLock()
	ready := b.state.State == "ready"
	b.mu.RUnlock()
	python := managedtools.BootstrapPython(b.dataDir)
	if !ready || runBootstrapProbe(python, harnessProbeScript) == nil {
		return
	}
	uvName := "uv"
	if runtime.GOOS == "windows" {
		uvName = "uv.exe"
	}
	root := filepath.Join(b.dataDir, "bootstrap")
	uv := filepath.Join(root, "tools", uvName)
	if info, err := os.Stat(uv); err != nil || info.IsDir() {
		return
	}
	environment := append(os.Environ(), "UV_CACHE_DIR="+filepath.Join(root, "cache"), "UV_NO_PROGRESS=1")
	if err := installLauncherRequirements(uv, environment, resolveBootstrapAttempts(), nil, python, b.requirements); err != nil {
		log.Printf("python bootstrap: top-up failed: %v", err)
	}
}

func runBootstrapProbe(python, script string) error {
	probeContext, cancel := context.WithTimeout(context.Background(), interpreterProbeTimeout)
	defer cancel()
	command := exec.CommandContext(probeContext, python, "-I", "-c", script)
	configureBootstrapProcess(command)
	return command.Run()
}

func (b *PythonBootstrap) downloadWithFallback(sources []bootstrapSource, destination string, size int64, digest string) error {
	var failures []string

	official := b.uv.URL()
	rungs := bootstrapRungs(sources)
	setback := ""
	for index := 0; index < len(rungs); index++ {
		source := rungs[index].item
		sourceLabel := "官方源"
		if source.URL != official {
			sourceLabel = "加速镜像"
			if parsed, err := url.Parse(source.URL); err == nil && parsed.Host != "" {
				sourceLabel = parsed.Host
			}
		}
		if source.Proxy != "" {
			sourceLabel += "（通过" + proxyLabel(source.Proxy) + "）"
		}

		progressMsg := fmt.Sprintf("正在从%s下载 Python 安装器", sourceLabel)
		if setback != "" {
			progressMsg = fmt.Sprintf("上一来源%s，正在改从%s下载 Python 安装器", setback, sourceLabel)
		}
		b.mu.Lock()
		b.state.Proxy = proxyLabel(source.Proxy)
		b.mu.Unlock()
		b.update("downloading", progressMsg, &bootstrapProgress{Total: size, Unit: "bytes"})

		err := b.downloadSingle(source, destination, size, digest, progressMsg, rungs[index].watched)
		if err == nil {
			return nil
		}

		if errors.Is(err, errBootstrapTooSlow) {
			rungs = append(rungs, bootstrapRung[bootstrapSource]{item: source})
			setback = "速度过慢"
			continue
		}
		setback = "下载失败"
		failures = append(failures, fmt.Sprintf("[%s]: %v", sourceLabel, err))
		time.Sleep(500 * time.Millisecond)
	}

	return fmt.Errorf("%s。\n"+
		"也可以手动下载该文件并放入指定目录后重试：\n"+
		"• 官方下载链接：%s\n"+
		"• 镜像加速链接：%s\n"+
		"• 目标放置路径：%s\n"+
		"• 详细错误日志：%s",
		bootstrapUnreachableMessage, official, gitHubMirrorPrefix()+official, destination, strings.Join(failures, "；"))
}

func (b *PythonBootstrap) downloadSingle(source bootstrapSource, destination string, size int64, digest, message string, watched bool) error {
	partial := destination + ".part"
	_ = os.Remove(partial)

	// An unwatched rung is the one left to finish, however slowly.
	limit := 30 * time.Minute
	if watched {
		limit = 5 * time.Minute
	}
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, source.URL, nil)
	if err != nil {
		return err
	}
	request.Header.Set("User-Agent", "nonoka-sub-x-bootstrap")

	client := &http.Client{Timeout: 30 * time.Minute, Transport: source.transport()}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("网络请求失败：%w", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP 响应码 %d", response.StatusCode)
	}

	output, err := os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}

	// The timer catches a body that stops arriving, which no read would report.
	var (
		watch *speedWatch
		stall *time.Timer
	)
	if watched {
		watch = &speedWatch{now: time.Now}
		stall = time.AfterFunc(bootstrapSlowWindow, cancel)
		defer stall.Stop()
	}

	started := time.Now()
	buffer := make([]byte, 128<<10)
	var completed int64
	for {
		count, readErr := response.Body.Read(buffer)
		if count > 0 {
			if _, err = output.Write(buffer[:count]); err != nil {
				output.Close()
				_ = os.Remove(partial)
				return err
			}
			completed += int64(count)
			seconds := time.Since(started).Seconds()
			b.update("downloading", message, &bootstrapProgress{
				Completed: completed, Total: size, Unit: "bytes", BytesPerSecond: float64(completed) / max(seconds, 0.001),
			})
			if watch != nil {
				stall.Reset(bootstrapSlowWindow)
				if err := watch.add(count); err != nil {
					output.Close()
					_ = os.Remove(partial)
					return err
				}
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			output.Close()
			_ = os.Remove(partial)
			if watched && errors.Is(readErr, context.Canceled) {
				return errBootstrapTooSlow
			}
			return readErr
		}
	}
	if err := output.Close(); err != nil {
		_ = os.Remove(partial)
		return err
	}

	b.update("verifying", "正在校验 Python 安装器", nil)
	if !fileMatches(partial, size, digest) {
		_ = os.Remove(partial)
		return errors.New("Python 安装器大小或 SHA-256 校验失败")
	}

	if err := os.Remove(destination); err != nil && !errors.Is(err, os.ErrNotExist) {
		_ = os.Remove(partial)
		return err
	}
	if err := os.Rename(partial, destination); err != nil {
		_ = os.Remove(partial)
		return err
	}
	return nil
}

func fileMatches(path string, size int64, expectedDigest string) bool {
	file, err := os.Open(path)
	if err != nil {
		return false
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info.Size() != size {
		return false
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return false
	}
	return hex.EncodeToString(hash.Sum(nil)) == expectedDigest
}

// extractNamedArchiveFile pulls one member out of the uv download by base name.
// The member's own path never reaches the destination, so a crafted archive
// cannot write outside the tools directory.
func extractNamedArchiveFile(archive string, compressedTar bool, name, destination string) error {
	if compressedTar {
		return extractNamedTarGzFile(archive, name, destination)
	}
	return extractNamedZipFile(archive, name, destination)
}

func extractNamedTarGzFile(archive, name, destination string) error {
	file, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer file.Close()
	decompressed, err := gzip.NewReader(file)
	if err != nil {
		return err
	}
	defer decompressed.Close()
	reader := tar.NewReader(decompressed)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		if header.Typeflag != tar.TypeReg || path.Base(header.Name) != name {
			continue
		}
		return writeExtractedFile(reader, destination)
	}
	return fmt.Errorf("%s 不在下载的压缩包中", name)
}

func extractNamedZipFile(archive, name, destination string) error {
	reader, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer reader.Close()
	for _, entry := range reader.File {
		if filepath.Base(filepath.FromSlash(entry.Name)) != name || entry.FileInfo().IsDir() {
			continue
		}
		input, err := entry.Open()
		if err != nil {
			return err
		}
		return errors.Join(writeExtractedFile(input, destination), input.Close())
	}
	return fmt.Errorf("%s 不在下载的压缩包中", name)
}

// 0o700 is what makes the extracted uv runnable on macOS: the archive member's
// own mode is deliberately not carried over.
func writeExtractedFile(input io.Reader, destination string) error {
	if err := os.MkdirAll(filepath.Dir(destination), 0o700); err != nil {
		return err
	}
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o700)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	return errors.Join(copyErr, output.Close())
}

func runBootstrapCommand(executable string, environment []string, arguments ...string) ([]byte, error) {
	command := exec.Command(executable, arguments...)
	command.Env = environment
	configureBootstrapProcess(command)
	return command.CombinedOutput()
}

func commandDetail(output []byte) string {
	detail := strings.TrimSpace(string(output))
	if len(detail) > 2000 {
		detail = detail[len(detail)-2000:]
	}
	if detail == "" {
		return ""
	}
	return "：" + detail
}
