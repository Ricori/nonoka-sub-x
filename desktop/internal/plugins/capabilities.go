package plugins

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

type MediaSummary struct {
	ID                string  `json:"id"`
	Title             string  `json:"title"`
	Duration          float64 `json:"duration"`
	Width             int     `json:"width"`
	Height            int     `json:"height"`
	Available         bool    `json:"available"`
	DocumentAvailable bool    `json:"documentAvailable"`
}

type ExportedArtifact struct {
	Path   string `json:"path"`
	Format string `json:"format"`
	Size   int64  `json:"size"`
}

// DownloadLog is one line of yt-dlp output on its way to the plugin page. The
// page shows the log because a download that takes minutes is otherwise a bar
// with no explanation of what it is waiting on.
type DownloadLog struct {
	Line string `json:"line"`
}

type DownloadedMedia struct {
	Platform   string       `json:"platform"`
	OutputPath string       `json:"outputPath"`
	Media      MediaSummary `json:"media"`
}

func (s *Service) MediaList(pluginID string) ([]MediaSummary, error) {
	if err := s.requirePermission(pluginID, "media.list"); err != nil {
		return nil, err
	}
	s.mu.RLock()
	media := s.media
	s.mu.RUnlock()
	if media == nil {
		return nil, errors.New("media library is unavailable")
	}
	entries := media.List()
	result := make([]MediaSummary, 0, len(entries))
	for _, entry := range entries {
		result = append(result, summarizeMedia(entry))
	}
	return result, nil
}

// ExportAudio is a structured FFmpeg capability. Plugins select a Nonoka X media
// ID and an output format; the host owns input resolution, arguments, the save
// dialog, temporary files, and final publication.
func (s *Service) ExportAudio(pluginID, mediaID, format string) (ExportedArtifact, error) {
	if err := s.requirePermission(pluginID, "ffmpeg.extract-audio"); err != nil {
		return ExportedArtifact{}, err
	}
	if !s.mediaJobMu.TryLock() {
		return ExportedArtifact{}, errors.New("another plugin media export is already running")
	}
	defer s.mediaJobMu.Unlock()
	s.mu.RLock()
	media, app, home := s.media, s.app, s.home
	s.mu.RUnlock()
	if media == nil || app == nil || home == nil {
		return ExportedArtifact{}, errors.New("application media services are unavailable")
	}
	entry, err := media.Get(mediaID)
	if err != nil {
		return ExportedArtifact{}, err
	}
	input, err := filepath.Abs(entry.SourcePath)
	if err != nil {
		return ExportedArtifact{}, err
	}
	if info, statErr := os.Stat(input); statErr != nil || !info.Mode().IsRegular() {
		return ExportedArtifact{}, errors.New("local media is unavailable")
	}

	format = strings.ToLower(strings.TrimSpace(format))
	extension, label, args, err := audioFormat(format)
	if err != nil {
		return ExportedArtifact{}, err
	}
	base := strings.TrimSuffix(filepath.Base(entry.Title), filepath.Ext(entry.Title))
	if strings.TrimSpace(base) == "" || base == "." {
		base = "audio"
	}
	output, err := app.Dialog.SaveFile().
		SetFilename(base+extension).
		AddFilter(label, "*"+extension).
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
	if samePath(input, output) {
		return ExportedArtifact{}, errors.New("audio export may not overwrite the source media")
	}

	ffmpeg, err := managedtools.Find(s.dataDirectory, "ffmpeg")
	if err != nil {
		return ExportedArtifact{}, errors.New("Nonoka X FFmpeg is not installed")
	}
	partial := strings.TrimSuffix(output, extension) + ".part" + extension
	_ = os.Remove(partial)
	commandArgs := []string{"-v", "error", "-y", "-i", input, "-vn"}
	commandArgs = append(commandArgs, args...)
	commandArgs = append(commandArgs, partial)
	command := exec.Command(ffmpeg, commandArgs...)
	configurePluginCommand(command)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		_ = os.Remove(partial)
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		if len(detail) > 4_000 {
			detail = detail[len(detail)-4_000:]
		}
		return ExportedArtifact{}, fmt.Errorf("FFmpeg audio export failed: %s", detail)
	}
	if err := os.Rename(partial, output); err != nil {
		_ = os.Remove(partial)
		return ExportedArtifact{}, err
	}
	info, err := os.Stat(output)
	if err != nil {
		return ExportedArtifact{}, err
	}
	return ExportedArtifact{Path: output, Format: format, Size: info.Size()}, nil
}

// RunYTDLP executes plugin-supplied yt-dlp arguments from a closed safe set for
// public YouTube and Twitch URLs. The host still owns tool resolution, the
// output path, FFmpeg wiring and the final media-library import.
func (s *Service) RunYTDLP(pluginID, rawURL string, pluginArgs []string) (DownloadedMedia, error) {
	system, err := s.requireTrustedPermission(pluginID, "tools.yt-dlp")
	if err != nil {
		return DownloadedMedia{}, err
	}
	if err := s.requirePermission(pluginID, "media.import"); err != nil {
		return DownloadedMedia{}, err
	}
	platform, normalizedURL, err := validateVideoURL(rawURL)
	if err != nil {
		return DownloadedMedia{}, err
	}
	if err := validateYTDLPArguments(pluginArgs, system); err != nil {
		return DownloadedMedia{}, err
	}
	pluginArgs, wantAria2c := takeAria2cRequest(pluginArgs)
	if !s.mediaJobMu.TryLock() {
		return DownloadedMedia{}, errors.New("another plugin media job is already running")
	}
	defer s.mediaJobMu.Unlock()
	// Mirrors the lock's lifetime exactly, so a page that reloads mid-download
	// can ask whether one is running instead of guessing from its own state --
	// which it no longer has, the iframe having been torn down.
	s.downloadActive.Store(true)
	defer func() {
		s.downloadActive.Store(false)
		s.emitDownloadEnd()
	}()

	s.mu.RLock()
	media, app, home := s.media, s.app, s.home
	s.mu.RUnlock()
	if media == nil || app == nil || home == nil {
		return DownloadedMedia{}, errors.New("application media services are unavailable")
	}
	output, err := app.Dialog.SaveFile().
		SetFilename(platform+"-video.mp4").
		AddFilter("MP4 视频", "*.mp4").
		AttachToWindow(home).
		PromptForSingleSelection()
	if err != nil && !dialogCancelled(err) {
		return DownloadedMedia{}, err
	}
	if output == "" {
		return DownloadedMedia{}, nil
	}
	if !strings.EqualFold(filepath.Ext(output), ".mp4") {
		output += ".mp4"
	}
	output, err = filepath.Abs(output)
	if err != nil {
		return DownloadedMedia{}, err
	}
	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return DownloadedMedia{}, err
	}

	ytDLP, err := managedtools.FindYTDLP(s.dataDirectory)
	if err != nil {
		return DownloadedMedia{}, err
	}
	ffmpeg, err := managedtools.Find(s.dataDirectory, "ffmpeg")
	if err != nil {
		return DownloadedMedia{}, errors.New("Nonoka X FFmpeg is not installed; install it from 运行环境 first")
	}
	args := append([]string(nil), ytDLP.Prefix...)
	args = append(args, pluginArgs...)
	args = append(args,
		"--no-playlist",
		"--newline",
		"--force-overwrites",
		"--ffmpeg-location", filepath.Dir(ffmpeg),
		"--merge-output-format", "mp4",
		"--remux-video", "mp4",
	)

	// The three pieces below only work together, which is why none of them is
	// conditional on its own merits. YouTube gates in this order: a signed-in
	// session makes formats visible at all, a GVS PO token makes their URLs
	// durable, and solving the "n" challenge needs a JavaScript runtime plus the
	// EJS solver. Miss any one and the failure surfaces at an earlier gate, which
	// is what makes each piece look useless when tried by itself.
	environment := append([]string(nil), ytDLP.Environment...)
	cookies := s.cookiesFor(pluginID)
	if cookies != "" {
		args = append(args, "--cookies", cookies)
	}
	potReady := false
	if potPlugin, pluginErr := managedtools.FindPOTPlugin(s.dataDirectory); pluginErr == nil {
		if potServer, serverErr := managedtools.FindPOTServer(s.dataDirectory); serverErr == nil {
			// yt-dlp finds the provider by importing `yt_dlp_plugins`; without this
			// it reports "PO Token Providers: none" and no token is ever requested.
			environment = managedtools.AddPythonPath(environment, potPlugin)
			// Script mode: the generator is spawned per call, so nothing stays
			// resident and no port is opened on the user's machine.
			args = append(args, "--extractor-args", "youtubepot-bgutilscript:server_home="+potServer)
			potReady = true
		}
	}
	if runtimeArgs := jsRuntimeArgs(s.dataDirectory); len(runtimeArgs) > 0 {
		args = append(args, runtimeArgs...)
		args = append(args, "--remote-components", "ejs:github")
	}
	if cookies != "" && potReady {
		// Only worth forcing once the session and the token are both in hand:
		// without them mweb returns no formats at all, while the default
		// android_vr fallback at least lists some.
		args = append(args, "--extractor-args", "youtube:player_client=mweb")
	}
	proxy := downloadProxy()
	if proxy != "" {
		args = append(args, "--proxy", proxy)
	}
	// Multi-connection fetching, as the reference downloader does it: aria2c
	// pulls a long VOD far faster than yt-dlp's own HTTP downloader. It is opt-in
	// because it is a second network client with its own failure modes -- a
	// proxy it cannot use times every connection out -- so the page asks for it
	// and the host resolves the binary. When it cannot be honoured the download
	// still runs on the built-in downloader, just more slowly. Only built-in
	// plugins may ask, since validateYTDLPArguments refuses the request from
	// sideloaded pages.
	var aria2cNotice string
	if wantAria2c {
		aria2c, lookupErr := managedtools.Find(s.dataDirectory, "aria2c")
		switch {
		case lookupErr != nil:
			aria2cNotice = "aria2c is not installed; using yt-dlp's built-in downloader. Install it from 运行环境 to enable acceleration."
		case !aria2cSupportsProxy(proxy):
			aria2cNotice = "aria2c cannot use a SOCKS proxy; using yt-dlp's built-in downloader."
		default:
			args = append(args,
				"--downloader", "dash,m3u8,http:"+aria2c,
				"--downloader-args", "aria2c:-x 16 -s 16 -k 1M",
			)
		}
	}
	args = append(args, "-o", output, normalizedURL)
	// A stalled fragment server can wedge yt-dlp indefinitely, and it holds the
	// single media-job lock while it does, so every download is bounded.
	downloadContext, cancelDownload := context.WithTimeout(context.Background(), ytDLPDownloadTimeout)
	defer cancelDownload()
	// Published so CancelDownload can reach this run. Cleared on the way out so a
	// later cancel cannot fire at a process that has already gone.
	s.downloadCancelled.Store(false)
	s.resetDownloadLog()
	if aria2cNotice != "" {
		// Otherwise the only sign of the fallback is a slower bar.
		s.emitDownloadLog("WARNING: " + aria2cNotice)
	}
	s.downloadCancelMu.Lock()
	s.downloadCancel = cancelDownload
	s.downloadCancelMu.Unlock()
	defer func() {
		s.downloadCancelMu.Lock()
		s.downloadCancel = nil
		s.downloadCancelMu.Unlock()
	}()
	command := exec.CommandContext(downloadContext, ytDLP.Executable, args...)
	if len(environment) > 0 {
		command.Env = append(os.Environ(), environment...)
	}
	configurePluginCommand(command)
	if err := s.streamDownload(command, pluginID); err != nil {
		if s.downloadCancelled.Load() {
			// Shaped like cancelling the save dialog: nothing went wrong, there is
			// just nothing to import. The half-written file is ours -- we chose the
			// path and passed --no-part -- so it does not outlive the run.
			_ = os.Remove(output)
			return DownloadedMedia{}, nil
		}
		if errors.Is(downloadContext.Err(), context.DeadlineExceeded) {
			return DownloadedMedia{}, fmt.Errorf("yt-dlp download exceeded the %s limit and was stopped", ytDLPDownloadTimeout)
		}
		return DownloadedMedia{}, err
	}
	info, err := os.Stat(output)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 {
		return DownloadedMedia{}, errors.New("yt-dlp completed without producing the selected MP4 file")
	}
	imported := media.Import([]string{output})
	if len(imported.Failed) > 0 {
		return DownloadedMedia{}, fmt.Errorf("video was downloaded to %s but media import failed: %s", output, imported.Failed[0].Message)
	}
	var entrySummary MediaSummary
	for _, entry := range media.List() {
		absolute, absoluteErr := filepath.Abs(entry.SourcePath)
		if absoluteErr == nil && samePath(absolute, output) {
			entrySummary = summarizeMedia(entry)
			break
		}
	}
	if entrySummary.ID == "" {
		return DownloadedMedia{}, fmt.Errorf("video was downloaded to %s but could not be found in the media library", output)
	}
	return DownloadedMedia{Platform: platform, OutputPath: output, Media: entrySummary}, nil
}

// jsRuntimeArgs points yt-dlp at a JavaScript engine so the EJS solver can run.
// yt-dlp enables only deno by default, so an installed node has to be named
// explicitly; the path is passed too, since the engine need not be on PATH.
func jsRuntimeArgs(dataDirectory string) []string {
	for _, runtime := range []string{"deno", "node"} {
		if executable, err := managedtools.Find(dataDirectory, runtime); err == nil {
			return []string{"--js-runtimes", runtime + ":" + executable}
		}
	}
	return nil
}

// ytDLPDownloadTimeout bounds one download end to end.
const ytDLPDownloadTimeout = 20 * time.Minute

// drainGrace bounds how long output draining may outlive the process itself.
const drainGrace = 2 * time.Second

// downloadProgressInterval throttles what reaches the renderer. yt-dlp rewrites
// its progress line constantly and an HLS video has thousands of fragments, so
// forwarding every line would flood the event bus to redraw the same bar.
const downloadProgressInterval = 400 * time.Millisecond

var downloadProgressPattern = regexp.MustCompile(`^\[download\]\s+([0-9.]+)%`)

// streamDownload runs yt-dlp while reading its output live rather than
// buffering everything until exit. A slow download and a hung one look
// identical otherwise, so percentages are forwarded as they arrive while the
// bounded tail is still kept for the failure message.
func (s *Service) streamDownload(command *exec.Cmd, jobID string) error {
	reader, writer, err := os.Pipe()
	if err != nil {
		return err
	}
	command.Stdout = writer
	command.Stderr = writer
	if err := command.Start(); err != nil {
		_ = writer.Close()
		_ = reader.Close()
		return err
	}
	// The child holds its own descriptor; dropping ours is what lets the
	// scanner below observe EOF once yt-dlp exits.
	_ = writer.Close()
	outputLog := newTailBuffer(8_000)
	drained := make(chan struct{})
	go func() {
		defer close(drained)
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
		var lastProgress time.Time
		for scanner.Scan() {
			line := scanner.Text()
			_, _ = outputLog.Write([]byte(line + "\n"))
			match := downloadProgressPattern.FindStringSubmatch(line)
			if match == nil {
				// Ordinary output -- extraction notices, warnings, errors. Low volume,
				// and the part a user can actually act on, so it always goes through.
				s.emitDownloadLog(line)
				continue
			}
			if time.Since(lastProgress) < downloadProgressInterval {
				continue
			}
			percent, parseErr := strconv.ParseFloat(match[1], 64)
			if parseErr != nil {
				continue
			}
			lastProgress = time.Now()
			s.emitDownloadProgress(jobID, percent)
			// The same line carries speed and ETA, which the bar cannot show.
			s.emitDownloadLog(line)
		}
	}()
	waitErr := command.Wait()
	// yt-dlp's own children — aria2c above all — inherit this pipe, so a killed
	// or timed-out download can exit while a grandchild still holds the write
	// end. Waiting on EOF alone would then hang here holding the media job lock,
	// which is precisely what the timeout exists to prevent. Give the drain a
	// moment to end on its own, then force it by dropping the read end.
	select {
	case <-drained:
	case <-time.After(drainGrace):
	}
	_ = reader.Close()
	<-drained
	if waitErr != nil {
		detail := strings.TrimSpace(outputLog.String())
		if detail == "" {
			detail = waitErr.Error()
		}
		return errors.New(explainDownloadFailure(detail))
	}
	s.emitDownloadProgress(jobID, 100)
	return nil
}

// forbiddenDownloadHelp explains the one failure users actually hit. YouTube
// strips the formats of every client that would hand out durable media URLs
// unless the request carries a proof-of-origin token, which yt-dlp cannot mint
// on its own -- it has no native PO token component, only the EJS pieces that
// solve signatures. What is left is the android_vr fallback, whose URLs serve a
// few percent of a sustained transfer and are then revoked. Short videos finish
// inside that window; anything longer does not.
const forbiddenDownloadHelp = "YouTube refused the download with HTTP 403. " +
	"It hands out media URLs that stop working partway through a transfer unless the request " +
	"carries a proof-of-origin (PO) token, which yt-dlp cannot generate by itself, so in practice " +
	"only short videos finish. This is not a network, FFmpeg, or aria2c problem, and retrying rarely helps."

// explainDownloadFailure turns a failed run into something a user can act on.
// The raw tail is thousands of characters of aria2c retry notices and signed
// googlevideo URLs, which buries the one line that carries the reason.
func explainDownloadFailure(output string) string {
	trimmed := strings.TrimSpace(output)
	// aria2c reports the same rejection as its own exit code, so both spellings
	// have to count as the 403 or the message depends on which downloader ran.
	if strings.Contains(trimmed, "HTTP Error 403") || strings.Contains(trimmed, "status=403") {
		return forbiddenDownloadHelp
	}
	if strings.Contains(trimmed, "aria2c exited") && strings.Contains(trimmed, "Network problem has occurred") {
		return unreachableDownloadHelp
	}
	return "yt-dlp download failed: " + downloadFailureDetail(trimmed)
}

// unreachableDownloadHelp explains aria2c timing out against googlevideo. By the
// time aria2c runs, yt-dlp has already fetched the video page, so YouTube itself
// was reachable; the transfer was not, which almost always means the proxy that
// carried the first request did not carry the second.
const unreachableDownloadHelp = "aria2c could not connect to the video server (the connection timed out). " +
	"The video information was fetched, so YouTube is reachable, but the media transfer did not go through your proxy. " +
	"Nonoka X forwards the Windows system proxy; if your proxy client does not set one (or only sets a PAC script), " +
	"set HTTPS_PROXY to its HTTP port before starting Nonoka X, turn on the client's TUN mode, " +
	"or switch the downloader back to the built-in one."

// downloadFailureDetail keeps the actionable part of the log: the ERROR lines
// when yt-dlp emitted any, otherwise the tail, always bounded so a wall of
// progress redraws cannot reach the plugin page.
func downloadFailureDetail(output string) string {
	var lines, failures []string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		lines = append(lines, line)
		if strings.Contains(line, "ERROR:") {
			failures = append(failures, line)
		}
	}
	if len(failures) > 0 {
		lines = failures
	}
	if len(lines) > 3 {
		lines = lines[len(lines)-3:]
	}
	detail := strings.Join(lines, "; ")
	// Trimmed by runes: the log carries Chinese from Nonoka X-managed tools, and
	// slicing bytes would hand the UI a broken character.
	if runes := []rune(detail); len(runes) > 400 {
		detail = string(runes[len(runes)-400:])
	}
	return detail
}

// CancelDownload interrupts the running download. Killing the process is what
// actually stops it: yt-dlp has no gentler protocol, and the context it runs
// under is already the timeout's, so cancelling that covers both paths.
func (s *Service) CancelDownload(pluginID string) error {
	if _, err := s.requireTrustedPermission(pluginID, "tools.yt-dlp"); err != nil {
		return err
	}
	s.downloadCancelMu.Lock()
	cancel := s.downloadCancel
	s.downloadCancelMu.Unlock()
	if cancel == nil {
		return errors.New("no download is running")
	}
	// Set before cancelling, so the run cannot finish and read a stale false.
	s.downloadCancelled.Store(true)
	cancel()
	return nil
}

// downloadLogRetained bounds what is kept. It matches the page's own limit, so a
// reattached page ends up with exactly the log it would have had.
const downloadLogRetained = 200

func (s *Service) resetDownloadLog() {
	s.downloadLogMu.Lock()
	s.downloadLogLines = nil
	s.downloadLogMu.Unlock()
}

func (s *Service) recordDownloadLog(line string) {
	s.downloadLogMu.Lock()
	defer s.downloadLogMu.Unlock()
	s.downloadLogLines = append(s.downloadLogLines, line)
	if len(s.downloadLogLines) > downloadLogRetained {
		// Re-sliced into a fresh array so the dropped head can be collected
		// instead of being pinned by the slice's backing store for the whole run.
		s.downloadLogLines = append([]string(nil), s.downloadLogLines[len(s.downloadLogLines)-downloadLogRetained:]...)
	}
}

// DownloadLogLines returns the running -- or most recently finished -- run's
// output, so a page can render the history it missed. It survives past the end
// of a run deliberately: coming back after a download finished should still show
// what happened, and the buffer is cleared when the next one starts.
func (s *Service) DownloadLogLines(pluginID string) ([]string, error) {
	if _, err := s.requireTrustedPermission(pluginID, "tools.yt-dlp"); err != nil {
		return nil, err
	}
	s.downloadLogMu.Lock()
	defer s.downloadLogMu.Unlock()
	return append([]string{}, s.downloadLogLines...), nil
}

// ClearDownloadLog empties the retained log. It clears the host's buffer, not
// just the page's view: the page re-fetches on every mount, so wiping only the
// DOM would bring the whole log back the next time the user navigated away and
// returned.
func (s *Service) ClearDownloadLog(pluginID string) error {
	if _, err := s.requireTrustedPermission(pluginID, "tools.yt-dlp"); err != nil {
		return err
	}
	s.resetDownloadLog()
	return nil
}

// emitDownloadLog forwards one line. Long lines are trimmed: aria2c reports a
// failure with the whole signed media URL, well over a thousand characters,
// which would push everything readable out of the page's log view.
func (s *Service) emitDownloadLog(line string) {
	line = strings.TrimRight(line, "\r")
	if strings.TrimSpace(line) == "" {
		return
	}
	if runes := []rune(line); len(runes) > 500 {
		line = string(runes[:500]) + "…"
	}
	s.recordDownloadLog(line)
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app != nil {
		app.Event.Emit("plugins:download-log", DownloadLog{Line: line})
	}
}

// emitDownloadEnd tells any listening page the download is over, whatever the
// outcome. The rpc.result that carries the actual result goes to the frame that
// asked, and that frame is gone if the user navigated away; without this a
// reattached page would sit on a progress bar that never finishes.
func (s *Service) emitDownloadEnd() {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app == nil {
		return
	}
	app.Event.Emit("media:progress", library.MediaProgress{Stage: "download-end", Done: 100, Total: 100})
}

// emitDownloadProgress reuses the media:progress channel the editor export
// already publishes on, so the plugin page host keeps a single progress relay
// instead of growing one per capability. The stage tells them apart.
func (s *Service) emitDownloadProgress(jobID string, percent float64) {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app == nil {
		return
	}
	app.Event.Emit("media:progress", library.MediaProgress{
		ID:    jobID,
		Stage: "download",
		Done:  math.Min(math.Max(percent, 0), 100),
		Total: 100,
	})
}

func (s *Service) requirePermission(pluginID, permission string) error {
	_, err := s.requireTrustedPermission(pluginID, permission)
	return err
}

// requireTrustedPermission checks a capability grant and additionally reports
// whether the caller is a built-in system plugin. Trust comes from the install
// location alone (see pluginHomeLocked), so a sideloaded package cannot reach
// the wider limits by declaring anything in its manifest.
func (s *Service) requireTrustedPermission(pluginID, permission string) (bool, error) {
	if !pluginIDPattern.MatchString(pluginID) {
		return false, errors.New("invalid plugin id")
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	manifest, _, system, err := s.installedManifestLocked(pluginID)
	if err != nil {
		return false, err
	}
	if !s.enabledLocked(pluginID, system) {
		return false, errors.New("plugin is disabled")
	}
	for _, granted := range manifest.Permissions {
		if granted == permission {
			return system, nil
		}
	}
	return system, fmt.Errorf("plugin does not have %s permission", permission)
}

func audioFormat(format string) (extension, label string, args []string, err error) {
	switch format {
	case "wav":
		return ".wav", "WAV 音频", []string{"-c:a", "pcm_s16le"}, nil
	case "flac":
		return ".flac", "FLAC 音频", []string{"-c:a", "flac"}, nil
	case "mp3":
		return ".mp3", "MP3 音频", []string{"-c:a", "libmp3lame", "-q:a", "2"}, nil
	case "m4a":
		return ".m4a", "M4A 音频", []string{"-c:a", "aac", "-b:a", "192k"}, nil
	default:
		return "", "", nil, errors.New("audio format must be wav, flac, mp3, or m4a")
	}
}

func validateVideoURL(raw string) (platform, normalized string, err error) {
	if len(raw) > 2_048 {
		return "", "", errors.New("video URL is too long")
	}
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil {
		return "", "", errors.New("enter a valid HTTPS YouTube or Twitch URL")
	}
	host := strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
	switch {
	case host == "youtu.be" || host == "youtube.com" || strings.HasSuffix(host, ".youtube.com"):
		platform = "youtube"
	case host == "twitch.tv" || strings.HasSuffix(host, ".twitch.tv"):
		platform = "twitch"
	default:
		return "", "", errors.New("only YouTube and Twitch URLs are supported")
	}
	return platform, parsed.String(), nil
}

// validateYTDLPArguments keeps plugin-supplied arguments inside a closed set.
// The set widens for built-in plugins rather than opening up per flag: they
// ship with Nonoka X and are reviewed with it, so the taller formats and the
// 16-way fragment concurrency that pair with aria2c are reachable there while
// sideloaded pages stay on the conservative set.
func validateYTDLPArguments(arguments []string, system bool) error {
	if len(arguments) > 32 {
		return errors.New("yt-dlp command contains too many arguments")
	}
	formatValues := map[string]bool{
		"bv*+ba/b":                             true,
		"bv*[height<=1080]+ba/b[height<=1080]": true,
		"bv*[height<=720]+ba/b[height<=720]":   true,
	}
	fragmentValues := map[string]bool{"1": true, "2": true, "4": true, "8": true}
	fragmentLimits := "1, 2, 4, or 8"
	if system {
		formatValues["bv*[height<=1440]+ba/b[height<=1440]"] = true
		formatValues["bv*[height<=2160]+ba/b[height<=2160]"] = true
		fragmentValues["16"] = true
		fragmentLimits = "1, 2, 4, 8, or 16"
	}
	for index := 0; index < len(arguments); index++ {
		argument := arguments[index]
		if argument == "" || len(argument) > 512 {
			return errors.New("yt-dlp command contains an invalid argument")
		}
		switch argument {
		case "--no-playlist", "--newline", "--embed-metadata":
			continue
		case "--no-part":
			// Writing straight to the final name suits the multi-connection
			// downloader, which fills the file out of order anyway.
			if !system {
				return fmt.Errorf("yt-dlp argument %q is only allowed for built-in plugins", argument)
			}
			continue
		case "--downloader":
			// Only ever the request `--downloader aria2c`: the host swaps it for the
			// resolved binary, so a page can never name an executable of its own.
			if !system {
				return fmt.Errorf("yt-dlp argument %q is only allowed for built-in plugins", argument)
			}
			if index+1 >= len(arguments) || arguments[index+1] != "aria2c" {
				return errors.New("yt-dlp --downloader only accepts aria2c")
			}
			index++
			continue
		case "-f", "--format", "--merge-output-format", "--remux-video", "-N", "--concurrent-fragments":
			if index+1 >= len(arguments) {
				return fmt.Errorf("yt-dlp argument %s requires a value", argument)
			}
			value := arguments[index+1]
			index++
			switch argument {
			case "-f", "--format":
				if !formatValues[value] {
					return errors.New("yt-dlp format selector is not allowed")
				}
			case "--merge-output-format", "--remux-video":
				if value != "mp4" {
					return errors.New("yt-dlp demo output format must be mp4")
				}
			case "-N", "--concurrent-fragments":
				if !fragmentValues[value] {
					return errors.New("yt-dlp concurrent fragments must be " + fragmentLimits)
				}
			}
		default:
			return fmt.Errorf("yt-dlp argument %q is not allowed for UI plugins", argument)
		}
	}
	return nil
}

// takeAria2cRequest removes the validated `--downloader aria2c` pair, reporting
// whether it was there. The pair is a request to the host, not an argument
// yt-dlp should see as written.
func takeAria2cRequest(arguments []string) ([]string, bool) {
	kept := make([]string, 0, len(arguments))
	requested := false
	for index := 0; index < len(arguments); index++ {
		if arguments[index] == "--downloader" && index+1 < len(arguments) && arguments[index+1] == "aria2c" {
			requested = true
			index++
			continue
		}
		kept = append(kept, arguments[index])
	}
	return kept, requested
}

func summarizeMedia(entry library.Entry) MediaSummary {
	return MediaSummary{
		ID:                entry.ID,
		Title:             entry.Title,
		Duration:          entry.Duration,
		Width:             entry.Width,
		Height:            entry.Height,
		Available:         entry.Available,
		DocumentAvailable: entry.DocumentAvailable,
	}
}

func samePath(left, right string) bool {
	left = filepath.Clean(left)
	right = filepath.Clean(right)
	if filepath.Separator == '\\' {
		return strings.EqualFold(left, right)
	}
	return left == right
}

type tailBuffer struct {
	data  []byte
	limit int
}

func newTailBuffer(limit int) tailBuffer {
	return tailBuffer{data: make([]byte, 0, limit), limit: limit}
}

func (b *tailBuffer) Write(value []byte) (int, error) {
	written := len(value)
	if b.limit <= 0 {
		return written, nil
	}
	b.data = append(b.data, value...)
	if len(b.data) > b.limit {
		copy(b.data, b.data[len(b.data)-b.limit:])
		b.data = b.data[:b.limit]
	}
	return written, nil
}

func (b *tailBuffer) String() string { return string(b.data) }
