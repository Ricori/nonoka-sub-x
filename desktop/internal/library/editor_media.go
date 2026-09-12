package library

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

const (
	spectrogramWidth  = 4096
	spectrogramHeight = 256
)

type ExportResult struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
}

type MediaProgress struct {
	ID    string  `json:"id"`
	Stage string  `json:"stage"`
	Done  float64 `json:"done"`
	Total float64 `json:"total"`
}

type SpectrogramTileResult struct {
	URL      string  `json:"url"`
	Start    float64 `json:"start"`
	Duration float64 `json:"duration"`
	Width    int     `json:"width"`
	Height   int     `json:"height"`
}

var editorExportPresets = map[string]bool{
	"ultrafast": true, "superfast": true, "veryfast": true, "faster": true, "fast": true,
	"medium": true, "slow": true, "slower": true, "veryslow": true,
}

func (s *Service) ExportVideoRange(id, defaultName, ass string, t0, t1 float64, crf int, preset string, scaleH int, abr string) (ExportResult, error) {
	if !validID(id) || len(ass) > 64<<20 || !finite(t0) || !finite(t1) || t1 <= t0 {
		return ExportResult{}, errors.New("invalid video export request")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return ExportResult{}, err
	}
	source := s.cachedPath(id)
	if source == "" {
		source = entry.SourcePath
	}
	if !fileExists(source) {
		return ExportResult{}, errors.New("local media is unavailable")
	}
	t0 = math.Max(0, t0)
	t1 = math.Min(entry.Duration, t1)
	if t1 <= t0 {
		return ExportResult{}, errors.New("video export range is empty")
	}
	if crf < 1 || crf > 51 {
		crf = 21
	}
	if !editorExportPresets[preset] {
		preset = "medium"
	}
	if scaleH != 0 && (scaleH < 240 || scaleH > 4320) {
		scaleH = 0
	}
	audioArgs := editorExportAudioArgs(abr)
	ctx, cancel := context.WithCancel(context.Background())
	s.mu.Lock()
	if s.exportCancels == nil {
		s.exportCancels = make(map[string]context.CancelFunc)
	}
	if _, running := s.exportCancels[id]; running {
		s.mu.Unlock()
		cancel()
		return ExportResult{}, errors.New("video export is already running")
	}
	s.exportCancels[id] = cancel
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.exportCancels, id)
		s.mu.Unlock()
		cancel()
	}()

	app, window := s.exportDialogWindow()
	if app == nil || window == nil {
		return ExportResult{}, errors.New("application window is not ready")
	}
	name := strings.TrimSpace(filepath.Base(defaultName))
	if name == "" || name == "." {
		name = strings.TrimSuffix(entry.Title, filepath.Ext(entry.Title)) + " - 字幕.mp4"
	}
	if !strings.EqualFold(filepath.Ext(name), ".mp4") {
		name += ".mp4"
	}
	output, err := app.Dialog.SaveFile().SetFilename(name).AddFilter("MP4 视频", "*.mp4").AttachToWindow(window).PromptForSingleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if errors.Is(ctx.Err(), context.Canceled) {
		return ExportResult{}, errors.New("已取消")
	}
	if err != nil || output == "" {
		return ExportResult{}, err
	}
	if !strings.EqualFold(filepath.Ext(output), ".mp4") {
		output += ".mp4"
	}
	if samePath(source, output) {
		return ExportResult{}, errors.New("export destination must not overwrite source video")
	}

	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return ExportResult{}, errors.New("ffmpeg is required to export embedded subtitles")
	}
	work, err := os.MkdirTemp(filepath.Join(s.root, "temporary"), "editor-export-")
	if err != nil {
		if mkErr := os.MkdirAll(filepath.Join(s.root, "temporary"), 0o700); mkErr != nil {
			return ExportResult{}, mkErr
		}
		work, err = os.MkdirTemp(filepath.Join(s.root, "temporary"), "editor-export-")
	}
	if err != nil {
		return ExportResult{}, err
	}
	defer os.RemoveAll(work)
	if err := os.WriteFile(filepath.Join(work, "subtitle.ass"), []byte(ass), 0o600); err != nil {
		return ExportResult{}, err
	}
	fontsCopied, err := s.copyBundledFonts(filepath.Join(work, "fonts"))
	if err != nil {
		return ExportResult{}, err
	}
	filter := "subtitles=subtitle.ass"
	if fontsCopied {
		filter += ":fontsdir=fonts"
	}
	if scaleH > 0 {
		filter += fmt.Sprintf(",scale=-2:%d", scaleH)
	}
	partial := output + ".part.mp4"
	args := []string{
		"-v", "error", "-y", "-ss", formatFloat(t0), "-t", formatFloat(t1 - t0), "-i", source,
		"-vf", filter, "-c:v", "libx264", "-preset", preset, "-crf", strconv.Itoa(crf),
	}
	args = append(args, audioArgs...)
	args = append(args, "-progress", "pipe:1", "-nostats", partial)
	command := exec.CommandContext(ctx, ffmpeg, args...)
	command.Dir = work
	configureMediaCommand(command)
	stdout, err := command.StdoutPipe()
	if err != nil {
		return ExportResult{}, err
	}
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return ExportResult{}, errors.New("已取消")
		}
		return ExportResult{}, err
	}
	duration := t1 - t0
	jobID := "exp_" + id
	lastProgress := time.Time{}
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		done, ok := parseExportProgress(scanner.Text(), duration)
		if !ok || (done < duration && time.Since(lastProgress) < 150*time.Millisecond) {
			continue
		}
		lastProgress = time.Now()
		s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "export", Done: done, Total: duration})
	}
	scanErr := scanner.Err()
	runErr := command.Wait()
	if runErr != nil {
		_ = os.Remove(partial)
		if errors.Is(ctx.Err(), context.Canceled) {
			return ExportResult{}, errors.New("已取消")
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = runErr.Error()
		}
		return ExportResult{}, fmt.Errorf("export video: %s", message)
	}
	if scanErr != nil {
		_ = os.Remove(partial)
		if errors.Is(ctx.Err(), context.Canceled) {
			return ExportResult{}, errors.New("已取消")
		}
		return ExportResult{}, fmt.Errorf("read export progress: %w", scanErr)
	}
	s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "export", Done: duration, Total: duration})
	if err := os.Rename(partial, output); err != nil {
		_ = os.Remove(partial)
		return ExportResult{}, err
	}
	stat, err := os.Stat(output)
	if err != nil {
		return ExportResult{}, err
	}
	return ExportResult{Path: output, Size: stat.Size()}, nil
}

func editorExportAudioArgs(option string) []string {
	if option == "copy" {
		return []string{"-c:a", "copy"}
	}
	if option != "128k" && option != "192k" && option != "256k" && option != "320k" {
		option = "192k"
	}
	return []string{"-c:a", "aac", "-b:a", option}
}

func (s *Service) CancelExport(id string) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	s.mu.Lock()
	cancel, ok := s.exportCancels[id]
	s.mu.Unlock()
	if ok && cancel != nil {
		cancel()
	}
	return nil
}

func parseExportProgress(line string, duration float64) (float64, bool) {
	const prefix = "out_time_us="
	if duration <= 0 || !strings.HasPrefix(line, prefix) {
		return 0, false
	}
	microseconds, err := strconv.ParseFloat(strings.TrimSpace(strings.TrimPrefix(line, prefix)), 64)
	if err != nil || microseconds < 0 {
		return 0, false
	}
	return min(microseconds/1e6, duration), true
}

func (s *Service) emitMediaProgress(progress MediaProgress) {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app != nil {
		app.Event.Emit("media:progress", progress)
	}
}

func (s *Service) SpectrogramTile(id string, start, duration float64) (SpectrogramTileResult, error) {
	if !validID(id) || !finite(start) || !finite(duration) || start < 0 || duration < .25 || duration > 6000 {
		return SpectrogramTileResult{}, errors.New("invalid spectrogram range")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return SpectrogramTileResult{}, err
	}
	source := s.cachedPath(id)
	if source == "" {
		source = entry.SourcePath
	}
	if !fileExists(source) {
		return SpectrogramTileResult{}, errors.New("spectrogram requires local media")
	}
	if start >= entry.Duration {
		return SpectrogramTileResult{}, errors.New("spectrogram range is outside media")
	}
	duration = math.Min(duration, entry.Duration-start)
	start = math.Round(start*1000) / 1000
	duration = math.Round(duration*1000) / 1000

	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return SpectrogramTileResult{}, errors.New("ffmpeg is required to generate spectrograms")
	}
	directory := filepath.Join(s.root, "temporary", "spectrogram", id)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return SpectrogramTileResult{}, err
	}
	output := filepath.Join(directory, fmt.Sprintf("v1-%d-%d.png", int64(start*1000), int64(duration*1000)))
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	if !fileExists(output) {
		filter := fmt.Sprintf(
			"showspectrumpic=s=%dx%d:legend=disabled:mode=combined:color=green:scale=log:fscale=log:gain=2:drange=78,"+
				"lutrgb=r='33+0.870588*val':g='19+0.92549*val':b='63+0.752941*val'",
			spectrogramWidth, spectrogramHeight,
		)
		command := exec.Command(ffmpeg,
			"-v", "error", "-ss", formatFloat(start), "-t", formatFloat(duration), "-i", source,
			"-lavfi", filter, "-frames:v", "1", "-y", output,
		)
		configureMediaCommand(command)
		if result, runErr := command.CombinedOutput(); runErr != nil {
			_ = os.Remove(output)
			return SpectrogramTileResult{}, fmt.Errorf("generate spectrogram: %s", strings.TrimSpace(string(result)))
		}
	}
	data, err := os.ReadFile(output)
	if err != nil {
		return SpectrogramTileResult{}, err
	}
	return SpectrogramTileResult{
		URL:   "data:image/png;base64," + base64.StdEncoding.EncodeToString(data),
		Start: start, Duration: duration, Width: spectrogramWidth, Height: spectrogramHeight,
	}, nil
}

func finite(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0)
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', 3, 64)
}
