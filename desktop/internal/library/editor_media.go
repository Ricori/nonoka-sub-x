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
	// Encoder is the label of the video encoder that produced the file.
	Encoder string `json:"encoder"`
}

type MediaProgress struct {
	ID    string  `json:"id"`
	Stage string  `json:"stage"`
	// Phase distinguishes sub-steps of an export: "erase" during LaMa text
	// removal and empty/"encode" during final ffmpeg encoding.
	Phase string  `json:"phase,omitempty"`
	Done  float64 `json:"done"`
	Total float64 `json:"total"`
	// Encoder labels the video encoder of an export; it changes when a GPU
	// encoder failed and the export started over on the next one.
	Encoder string `json:"encoder,omitempty"`
	// Output is where a running export will land, once it has been chosen.
	Output string `json:"output,omitempty"`
	// TaskID is the sidecar task of the LaMa erase phase, for reading its log.
	TaskID string `json:"taskId,omitempty"`
}

// EraseOptions asks ExportVideoPiecesWithErase to remove original on-screen
// text with FlowOCR's LaMa inpainter into a temporary intermediate video before
// running the normal final encode.
type EraseOptions struct {
	Enabled   bool   `json:"enabled"`
	PlatesASS string `json:"platesAss,omitempty"`
	Device    string `json:"device,omitempty"`
	Preset    string `json:"preset,omitempty"`
}

var inpaintPollInterval = 500 * time.Millisecond

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

// exportContainer is a container the editor can export burned-in video to.
type exportContainer struct {
	name  string
	ext   string
	label string
	muxer string
	// allAudio keeps every source audio track instead of only the first.
	// Matroska takes any codec, so a copy cannot fail on a second track.
	allAudio bool
}

var exportContainers = map[string]exportContainer{
	"mp4": {name: "mp4", ext: ".mp4", label: "MP4 视频", muxer: "mp4"},
	"mkv": {name: "mkv", ext: ".mkv", label: "MKV 视频", muxer: "matroska", allAudio: true},
}

func exportContainerFor(name string) exportContainer {
	if container, ok := exportContainers[strings.ToLower(name)]; ok {
		return container
	}
	return exportContainers["mp4"]
}

// exportFileName swaps a known container extension for the chosen one, so the
// editor's "x - 字幕.mp4" default becomes "x - 字幕.mkv".
func exportFileName(name string, container exportContainer) string {
	ext := filepath.Ext(name)
	for _, known := range exportContainers {
		if strings.EqualFold(ext, known.ext) {
			return strings.TrimSuffix(name, ext) + container.ext
		}
	}
	return name + container.ext
}

func (s *Service) ExportVideoRange(id, defaultName, ass string, t0, t1 float64, crf int, preset string, scaleH int, abr string) (ExportResult, error) {
	if !finite(t0) || !finite(t1) || t1 <= t0 {
		return ExportResult{}, errors.New("invalid video export request")
	}
	return s.ExportVideoPieces(id, defaultName, ass, []Range{{T0: t0, T1: t1}}, crf, preset, scaleH, abr, "mp4", false)
}

// ExportVideoPieces burns ass onto the given source pieces joined in order.
// The ASS must already be in output time (see subtitles/build.ts piecesAss).
// A single piece keeps the plain -ss/-t path; several go through trim+concat,
// which always re-encodes the audio. container is "mp4" or "mkv". gpu tries
// the hardware H.264 encoders first and falls back to libx264 when none works.
func (s *Service) ExportVideoPieces(id, defaultName, ass string, pieces []Range, crf int, preset string, scaleH int, abr, container string, gpu bool) (ExportResult, error) {
	return s.ExportVideoPiecesWithErase(id, defaultName, ass, pieces, crf, preset, scaleH, abr, container, gpu, EraseOptions{})
}

// ExportVideoPiecesWithErase extends ExportVideoPieces with an optional LaMa
// erase pass that removes original on-screen text before burning the subtitles.
func (s *Service) ExportVideoPiecesWithErase(id, defaultName, ass string, pieces []Range, crf int, preset string, scaleH int, abr, container string, gpu bool, erase EraseOptions) (ExportResult, error) {
	if !validID(id) || len(ass) > 64<<20 || len(pieces) == 0 {
		return ExportResult{}, errors.New("invalid video export request")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return ExportResult{}, err
	}
	if entry.AudioOnly {
		return ExportResult{}, errAudioOnlyExport
	}
	source := s.cachedPath(id)
	if source == "" {
		source = entry.SourcePath
	}
	if !fileExists(source) {
		return ExportResult{}, errors.New("local media is unavailable")
	}
	audioSource := exportAudioSource(entry, source)
	pieces = joinPieces(normalizePieces(pieces, entry.Duration))
	if len(pieces) == 0 {
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
	format := exportContainerFor(container)
	audioArgs := editorExportAudioArgs(abr, format, len(pieces) > 1)
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
		name = strings.TrimSuffix(entry.Title, filepath.Ext(entry.Title)) + " - 字幕"
	}
	name = exportFileName(name, format)
	output, err := app.Dialog.SaveFile().SetFilename(name).AddFilter(format.label, "*"+format.ext).AttachToWindow(window).PromptForSingleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if errors.Is(ctx.Err(), context.Canceled) {
		return ExportResult{}, errors.New("已取消")
	}
	if err != nil || output == "" {
		return ExportResult{}, err
	}
	if !strings.EqualFold(filepath.Ext(output), format.ext) {
		output += format.ext
	}
	if samePath(source, output) || samePath(audioSource, output) {
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
	partial := output + ".part" + format.ext
	jobID := "exp_" + id
	videoInput := source
	videoPreTrimmed := false
	videoSpanStart := 0.0
	if erase.Enabled && strings.TrimSpace(erase.PlatesASS) != "" {
		spanStart := pieces[0].T0
		spanEnd := pieces[len(pieces)-1].T1
		cleanedPath, eraseErr := s.runEraseIntermediate(ctx, work, entry, source, spanStart, spanEnd, erase, "export", jobID, output)
		if eraseErr != nil {
			return ExportResult{}, eraseErr
		}
		videoInput = cleanedPath
		videoPreTrimmed = true
		videoSpanStart = spanStart
	}
	job := editorExportJob{
		video: videoInput, audio: audioSource, pieces: pieces, filter: filter,
		videoPreTrimmed: videoPreTrimmed, videoSpanStart: videoSpanStart,
		crf: crf, preset: preset, audioArgs: audioArgs, container: format, output: partial,
	}
	if len(pieces) > 1 {
		job.hasAudio = true
		if s.prober != nil {
			if metadata, probeErr := s.prober.Probe(ctx, audioSource); probeErr == nil {
				job.hasAudio = metadata.HasAudio
			}
		}
		// Written to a file: a few hundred pieces overflow the Windows command line.
		videoOffset := pieces[0].T0
		if job.videoPreTrimmed {
			videoOffset = math.Max(pieces[0].T0, job.videoSpanStart)
		}
		graph := piecesFilterGraph(pieces, videoOffset, pieces[0].T0, job.audioInput(), job.hasAudio, filter)
		if err := os.WriteFile(filepath.Join(work, "graph.txt"), []byte(graph), 0o600); err != nil {
			return ExportResult{}, err
		}
	}
	duration := piecesDuration(pieces)
	var used exportEncoder
	var failures []string
	for _, encoder := range s.exportEncoders(gpu) {
		job.encoder = encoder
		_ = os.Remove(partial)
		s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "export", Total: duration, Encoder: encoder.label, Output: output})
		runErr := s.runEditorExport(ctx, ffmpeg, work, buildEditorExportArgs(job), duration, jobID, encoder.label)
		if runErr == nil {
			used = encoder
			break
		}
		_ = os.Remove(partial)
		if errors.Is(ctx.Err(), context.Canceled) {
			return ExportResult{}, errors.New("已取消")
		}
		message := runErr.Error()
		if format.name == "mp4" && abr == "copy" && strings.Contains(message, "not currently supported in container") {
			return ExportResult{}, errors.New("源音轨的编码放不进 MP4，请改用 MKV 容器，或改为重新编码音频")
		}
		failures = append(failures, encoder.label+": "+message)
	}
	if used.args == nil {
		// libx264 is last and its own reason is the one worth showing; the GPU
		// attempts before it most likely failed for want of a card.
		return ExportResult{}, fmt.Errorf("export video: %s", strings.TrimPrefix(failures[len(failures)-1], softwareExportEncoder.label+": "))
	}
	s.rememberExportEncoder(used)
	s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "export", Done: duration, Total: duration, Encoder: used.label})
	if err := os.Rename(partial, output); err != nil {
		_ = os.Remove(partial)
		return ExportResult{}, err
	}
	stat, err := os.Stat(output)
	if err != nil {
		return ExportResult{}, err
	}
	return ExportResult{Path: output, Size: stat.Size(), Encoder: used.label}, nil
}

// runEditorExport runs one ffmpeg attempt, relaying its progress.
func (s *Service) runEditorExport(ctx context.Context, ffmpeg, work string, args []string, duration float64, jobID, encoder string) error {
	command := exec.CommandContext(ctx, ffmpeg, args...)
	command.Dir = work
	configureMediaCommand(command)
	stdout, err := command.StdoutPipe()
	if err != nil {
		return err
	}
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		return err
	}
	lastProgress := time.Time{}
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		done, ok := parseExportProgress(scanner.Text(), duration)
		if !ok || (done < duration && time.Since(lastProgress) < 150*time.Millisecond) {
			continue
		}
		lastProgress = time.Now()
		s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "export", Done: done, Total: duration, Encoder: encoder})
	}
	scanErr := scanner.Err()
	if runErr := command.Wait(); runErr != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = runErr.Error()
		}
		return errors.New(message)
	}
	if scanErr != nil {
		return fmt.Errorf("read export progress: %w", scanErr)
	}
	return nil
}

// exportAudioSource picks the file the audio is taken from. When the video
// comes from the H.264 working copy, its audio is an AAC re-encode; the
// original still has the real track (PCM, FLAC, TrueHD...), which is what
// "copy" and the lossless option are for, and a better start for AAC too.
func exportAudioSource(entry Entry, video string) string {
	if entry.SourcePath != "" && !samePath(video, entry.SourcePath) && fileExists(entry.SourcePath) {
		return entry.SourcePath
	}
	return video
}

// editorExportJob is everything the export ffmpeg command line depends on.
type editorExportJob struct {
	video, audio    string
	pieces          []Range
	videoPreTrimmed bool
	videoSpanStart  float64
	// hasAudio is consulted only for joined pieces, whose filter graph must
	// know; a single piece maps audio optionally instead.
	hasAudio bool
	filter   string
	crf      int
	preset   string
	// encoder defaults to libx264 when unset.
	encoder   exportEncoder
	audioArgs []string
	container exportContainer
	output    string
}

func (job editorExportJob) audioInput() int {
	if job.audio != "" && job.audio != job.video {
		return 1
	}
	return 0
}

// buildEditorExportArgs maps streams explicitly: left to itself ffmpeg would
// also carry a soft subtitle track from an MKV source into an MKV output,
// doubling the subtitles that were just burned in.
func buildEditorExportArgs(job editorExportJob) []string {
	t0, t1 := job.pieces[0].T0, job.pieces[len(job.pieces)-1].T1
	span := t1 - t0
	audioSeek := []string{"-ss", formatFloat(t0), "-t", formatFloat(span)}
	args := []string{"-v", "error", "-y"}
	if job.videoPreTrimmed {
		if delta := t0 - job.videoSpanStart; delta > 0.0005 {
			args = append(args, "-ss", formatFloat(delta))
		}
		args = append(args, "-t", formatFloat(span), "-i", job.video)
	} else {
		args = append(args, audioSeek...)
		args = append(args, "-i", job.video)
	}
	audio := job.audioInput()
	if audio == 1 {
		args = append(args, audioSeek...)
		args = append(args, "-i", job.audio)
	}
	if len(job.pieces) == 1 {
		audioMap := fmt.Sprintf("%d:a:0?", audio)
		if job.container.allAudio {
			audioMap = fmt.Sprintf("%d:a?", audio)
		}
		// V, not v: a cover picture is a video stream too.
		args = append(args, "-vf", job.filter, "-map", "0:V:0", "-map", audioMap)
	} else {
		// "-/opt file" reads the option's value from a file (FFmpeg 7+); the old
		// -filter_complex_script spelling was removed in FFmpeg 8.
		args = append(args, "-/filter_complex", "graph.txt", "-map", "[vo]")
		if job.hasAudio {
			args = append(args, "-map", "[ac]")
		}
	}
	encoder := job.encoder
	if encoder.args == nil {
		encoder = softwareExportEncoder
	}
	args = append(args, "-sn", "-dn")
	args = append(args, encoder.args(job.crf, job.preset)...)
	args = append(args, job.audioArgs...)
	return append(args, "-f", job.container.muxer, "-progress", "pipe:1", "-nostats", job.output)
}

// editorExportAudioArgs turns the audio option into encoder flags. Joined
// pieces build a new audio stream, so there is nothing to copy: MKV falls back
// to lossless FLAC there, MP4 to AAC. FLAC is offered for MKV only, where
// players expect it.
func editorExportAudioArgs(option string, container exportContainer, joined bool) []string {
	if option == "copy" && joined {
		option = ""
		if container.name == "mkv" {
			option = "flac"
		}
	}
	switch {
	case option == "copy":
		// Matroska seeks to the video keyframe's cue for every stream, so a
		// copied track would start that far before the cut; drop what precedes it.
		return []string{"-c:a", "copy", "-copypriorss", "0"}
	case option == "flac" && container.name == "mkv":
		return []string{"-c:a", "flac"}
	case option == "128k" || option == "192k" || option == "256k" || option == "320k":
		return []string{"-c:a", "aac", "-b:a", option}
	}
	return []string{"-c:a", "aac", "-b:a", "192k"}
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
	data, err := s.renderTile(output, func(part string) error {
		filter := fmt.Sprintf(
			"showspectrumpic=s=%dx%d:legend=disabled:mode=combined:color=green:scale=log:fscale=log:gain=2:drange=78,"+
				"lutrgb=r='33+0.870588*val':g='19+0.92549*val':b='63+0.752941*val'",
			spectrogramWidth, spectrogramHeight,
		)
		command := exec.Command(ffmpeg,
			"-v", "error", "-ss", formatFloat(start), "-t", formatFloat(duration), "-i", source,
			"-lavfi", filter, "-frames:v", "1", "-y", part,
		)
		configureBackgroundCommand(command)
		if result, runErr := command.CombinedOutput(); runErr != nil {
			return fmt.Errorf("generate spectrogram: %s", strings.TrimSpace(string(result)))
		}
		return nil
	})
	if err != nil {
		return SpectrogramTileResult{}, err
	}
	return SpectrogramTileResult{
		URL:   "data:image/png;base64," + base64.StdEncoding.EncodeToString(data),
		Start: start, Duration: duration, Width: spectrogramWidth, Height: spectrogramHeight,
	}, nil
}

func estimateEraseBytes(spanSeconds float64) uint64 {
	const minRequired uint64 = 256 << 20
	if spanSeconds <= 0 {
		return minRequired
	}
	// 1080p intermediate at cq 14 / crf 12 is roughly 2-4 GB per 20 min (~3.5 MB/s).
	estimated := uint64(spanSeconds * 3.5 * float64(1<<20))
	if estimated < minRequired {
		return minRequired
	}
	return estimated
}

func (s *Service) runEraseIntermediate(ctx context.Context, work string, entry Entry, source string, spanStart, spanEnd float64, erase EraseOptions, stage, jobID, output string) (string, error) {
	s.mu.RLock()
	engine := s.inpaintEngine
	s.mu.RUnlock()
	if engine == nil {
		return "", errors.New("文字识别抹除服务不可用，请确认本地引擎正在运行")
	}
	spanDur := math.Max(0.1, spanEnd-spanStart)
	required := estimateEraseBytes(spanDur)
	if free, err := diskFreeBytes(work); err == nil && free < required {
		return "", fmt.Errorf("临时目录磁盘剩余空间不足（需要约 %.1f GB，当前剩余 %.1f GB）",
			float64(required)/float64(1<<30), float64(free)/float64(1<<30))
	}
	platesPath := filepath.Join(work, "plates.ass")
	if err := os.WriteFile(platesPath, []byte(erase.PlatesASS), 0o600); err != nil {
		return "", err
	}
	cleanedPath := filepath.Join(work, "cleaned.mp4")
	device := strings.TrimSpace(erase.Device)
	if device == "" {
		device = "auto"
	}
	preset := strings.TrimSpace(erase.Preset)
	if preset == "" {
		preset = "default"
	}
	s.emitMediaProgress(MediaProgress{ID: jobID, Stage: stage, Phase: "erase", Done: 0, Total: spanDur, Output: output})
	snapshot, err := engine.StartTask(map[string]any{
		"schema":   1,
		"provider": "local",
		"target":   "screen-text-inpaint",
		"device":   device,
		"source": map[string]any{
			"kind":        "local_file",
			"path":        source,
			"title":       entry.Title,
			"fingerprint": entry.Fingerprint,
			"video_id":    entry.ID,
			"duration":    entry.Duration,
		},
		"inpaint": map[string]any{
			"plates_path": platesPath,
			"out_path":    cleanedPath,
			"start":       spanStart,
			"end":         spanEnd,
			"preset":      preset,
		},
	})
	if err != nil {
		return "", fmt.Errorf("启动文字识别抹除失败：%w", err)
	}
	taskID, _ := snapshot["task_id"].(string)
	if taskID == "" {
		return "", errors.New("抹除任务没有返回 task_id")
	}
	s.emitMediaProgress(MediaProgress{ID: jobID, Stage: stage, Phase: "erase", Total: spanDur, Output: output, TaskID: taskID})

	ticker := time.NewTicker(inpaintPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			_, _ = engine.CancelTask(taskID)
			return "", errors.New("已取消")
		case <-ticker.C:
			status, statusErr := engine.TaskStatus(taskID)
			if statusErr != nil {
				continue
			}
			if progress, ok := status["progress"].(map[string]any); ok {
				done, _ := progress["completed"].(float64)
				total, _ := progress["total"].(float64)
				if total <= 0 {
					total = spanDur
				}
				s.emitMediaProgress(MediaProgress{ID: jobID, Stage: stage, Phase: "erase", Done: done, Total: total, Output: output, TaskID: taskID})
			}
			state, _ := status["state"].(string)
			switch state {
			case "completed":
				if !fileExists(cleanedPath) {
					return "", errors.New("文字识别抹除未生成中间视频")
				}
				s.emitMediaProgress(MediaProgress{ID: jobID, Stage: stage, Phase: "erase", Done: spanDur, Total: spanDur, Output: output, TaskID: taskID})
				return cleanedPath, nil
			case "cancelled", "interrupted":
				return "", errors.New("已取消")
			case "failed":
				msg := "文字识别抹除失败"
				if errObj, ok := status["error"].(map[string]any); ok {
					if text, _ := errObj["message"].(string); text != "" {
						msg = text
					}
				}
				return "", errors.New(msg)
			}
		}
	}
}

func finite(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0)
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', 3, 64)
}
