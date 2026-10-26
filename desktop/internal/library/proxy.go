package library

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

// Editing proxies. A 4K60 AV1 or HEVC source is long-GOP and expensive per
// frame, so every seek decodes from a distant keyframe at full resolution and
// a scrub turns into a queue of those. Like the proxy workflow of desktop NLEs,
// heavy sources get a small H.264 copy with a keyframe every few dozen frames,
// built once in the background. The editor plays it and draws its filmstrip
// from it; tasks and exports never see it, they keep reading the original or
// its cache copy.
const (
	// Sources whose short side is at least this get a proxy: 1440p and up.
	proxyMinShortSide = 1440
	proxyShortSide    = 720
	proxyGOP          = 30
)

type PreviewSource struct {
	URL string `json:"url"`
	// Proxy reports that URL plays the editing proxy rather than the original.
	Proxy bool `json:"proxy"`
	// Heavy reports a source that warrants a proxy, built or not.
	Heavy bool `json:"heavy"`
}

func needsProxy(entry Entry) bool {
	return !entry.AudioOnly && min(entry.Width, entry.Height) >= proxyMinShortSide
}

func (s *Service) proxyDirectory() string { return filepath.Join(s.cacheDirectory(), "proxy") }

func (s *Service) proxyPath(id string) string { return filepath.Join(s.proxyDirectory(), id+".mp4") }

// readyProxy returns the proxy's path once it has been fully built.
func (s *Service) readyProxy(id string) string {
	if !validID(id) {
		return ""
	}
	path := s.proxyPath(id)
	if !fileExists(path) {
		return ""
	}
	return path
}

// PreviewURL is the address the editor's player loads: the proxy once it is
// built, unless original asks for the source itself.
func (s *Service) PreviewURL(id string, original bool) (PreviewSource, error) {
	url, err := s.MediaURL(id)
	if err != nil {
		return PreviewSource{}, err
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return PreviewSource{}, err
	}
	result := PreviewSource{URL: url, Heavy: needsProxy(entry)}
	if !original && s.readyProxy(id) != "" {
		s.mu.RLock()
		media := s.media
		s.mu.RUnlock()
		if media != nil {
			result.URL, result.Proxy = media.ProxyURL(id), true
		}
	}
	return result, nil
}

// BuildProxy starts building the editing proxy in the background and returns
// at once. It does nothing for a source that does not need one, has one, or is
// already being built. Progress arrives as media:progress with stage "proxy",
// the outcome as media:proxy.
func (s *Service) BuildProxy(id string) error {
	entry, err := s.entryByID(id)
	if err != nil {
		return err
	}
	if !needsProxy(entry) || s.readyProxy(id) != "" {
		return nil
	}
	source, err := s.transcodeSource(id)
	if err != nil {
		return err
	}
	s.mu.Lock()
	if _, running := s.proxyCancels[id]; running {
		s.mu.Unlock()
		return nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.proxyCancels[id] = cancel
	s.mu.Unlock()
	go func() {
		defer func() {
			s.mu.Lock()
			delete(s.proxyCancels, id)
			s.mu.Unlock()
			cancel()
		}()
		buildErr := s.buildProxy(ctx, id, source, entry)
		if errors.Is(ctx.Err(), context.Canceled) {
			return
		}
		event := ProxyEvent{ID: id, OK: buildErr == nil}
		if buildErr != nil {
			event.Error = buildErr.Error()
		}
		s.emitProxy(event)
	}()
	return nil
}

func (s *Service) cancelProxy(id string) {
	s.mu.RLock()
	cancel := s.proxyCancels[id]
	s.mu.RUnlock()
	if cancel != nil {
		cancel()
	}
}

type ProxyEvent struct {
	ID    string `json:"id"`
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

func (s *Service) emitProxy(event ProxyEvent) {
	s.mu.RLock()
	app := s.app
	s.mu.RUnlock()
	if app != nil {
		app.Event.Emit("media:proxy", event)
	}
}

func (s *Service) buildProxy(ctx context.Context, id, source string, entry Entry) error {
	// One build at a time: hardware encoders have few sessions and the
	// software fallback already takes every core.
	select {
	case s.proxySlot <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { <-s.proxySlot }()

	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return errors.New("生成预览代理需要 FFmpeg，请先在设置中安装媒体工具")
	}
	if err := os.MkdirAll(s.proxyDirectory(), 0o755); err != nil {
		return err
	}
	destination := s.proxyPath(id)
	part := destination + ".part"
	width, height := proxySize(entry.Width, entry.Height)
	var failures []string
	for _, pipeline := range s.orderedProxyPipelines() {
		_ = os.Remove(part)
		runErr := s.runProxyPipeline(ctx, ffmpeg, id, pipeline.args(source, part, width, height), entry.Duration)
		if runErr == nil {
			s.rememberProxyPipeline(pipeline.name)
			// Cancelled as it finished: whoever cancelled has removed the proxy
			// and must not find it reinstalled behind them.
			if ctx.Err() != nil {
				_ = os.Remove(part)
				return ctx.Err()
			}
			if err := os.Rename(part, destination); err != nil {
				_ = os.Remove(part)
				return err
			}
			s.touchEntry(id)
			return s.convergeCache()
		}
		_ = os.Remove(part)
		if errors.Is(ctx.Err(), context.Canceled) {
			return ctx.Err()
		}
		failures = append(failures, pipeline.name+": "+runErr.Error())
	}
	return fmt.Errorf("生成预览代理失败：%s", strings.Join(failures, "; "))
}

func (s *Service) runProxyPipeline(ctx context.Context, ffmpeg, id string, args []string, duration float64) error {
	command := exec.CommandContext(ctx, ffmpeg, args...)
	configureBackgroundCommand(command)
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
		done, ok := parseTranscodeProgress(scanner.Text(), duration)
		if !ok || (done < duration && time.Since(lastProgress) < 500*time.Millisecond) {
			continue
		}
		lastProgress = time.Now()
		s.emitMediaProgress(MediaProgress{ID: id, Stage: "proxy", Done: done, Total: duration})
	}
	if err := command.Wait(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if lines := strings.Split(message, "\n"); len(lines) > 3 {
			message = strings.Join(lines[len(lines)-3:], "\n")
		}
		if message == "" {
			message = err.Error()
		}
		return errors.New(message)
	}
	return nil
}

// proxySize scales the short side to proxyShortSide, keeping both sides even
// as the H.264 encoders require.
func proxySize(width, height int) (int, int) {
	even := func(value float64) int { return max(2, int(value/2+.5)*2) }
	if width >= height {
		return even(float64(width) * proxyShortSide / float64(height)), proxyShortSide
	}
	return proxyShortSide, even(float64(height) * proxyShortSide / float64(width))
}

// proxyPipeline is one way to decode, scale and encode. The hardware ones keep
// frames on the GPU from decoder to encoder: decoding 4K on the GPU but
// scaling on the CPU spends as long copying frames back as the software
// decoder spends decoding them.
type proxyPipeline struct {
	name   string
	input  []string
	filter func(width, height int) string
	encode []string
}

var proxyPipelines = []proxyPipeline{
	{
		name:   "nvidia",
		input:  []string{"-hwaccel", "cuda", "-hwaccel_output_format", "cuda"},
		filter: func(w, h int) string { return fmt.Sprintf("scale_cuda=%d:%d:format=yuv420p", w, h) },
		encode: []string{"-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "28", "-maxrate", "6M", "-bufsize", "12M"},
	},
	{
		name:   "intel",
		input:  []string{"-hwaccel", "qsv", "-hwaccel_output_format", "qsv"},
		filter: func(w, h int) string { return fmt.Sprintf("scale_qsv=w=%d:h=%d:format=nv12", w, h) },
		encode: []string{"-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "28", "-maxrate", "6M", "-bufsize", "12M"},
	},
	{
		name:   "amd",
		input:  []string{"-hwaccel", "d3d11va"},
		filter: func(w, h int) string { return fmt.Sprintf("scale=%d:%d:flags=fast_bilinear,format=yuv420p", w, h) },
		encode: []string{"-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "26", "-qp_p", "28"},
	},
	{
		name:   "software",
		filter: func(w, h int) string { return fmt.Sprintf("scale=%d:%d:flags=fast_bilinear,format=yuv420p", w, h) },
		encode: []string{"-c:v", "libx264", "-preset", "veryfast", "-tune", "fastdecode", "-crf", "26"},
	},
}

func (p proxyPipeline) args(input, output string, width, height int) []string {
	args := append([]string{"-v", "error", "-y"}, p.input...)
	args = append(args, "-i", input,
		"-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
		"-vf", p.filter(width, height))
	args = append(args, p.encode...)
	return append(args,
		"-g", fmt.Sprint(proxyGOP), "-bf", "0",
		"-c:a", "aac", "-b:a", "128k", "-ac", "2",
		"-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", output)
}

// orderedProxyPipelines tries the pipeline that last worked first, so a
// machine without an NVIDIA card does not fail the CUDA attempt every time.
func (s *Service) orderedProxyPipelines() []proxyPipeline {
	s.mu.RLock()
	preferred := s.proxyPipeline
	s.mu.RUnlock()
	ordered := make([]proxyPipeline, 0, len(proxyPipelines))
	for _, pipeline := range proxyPipelines {
		if pipeline.name == preferred {
			ordered = append(ordered, pipeline)
		}
	}
	for _, pipeline := range proxyPipelines {
		if pipeline.name != preferred {
			ordered = append(ordered, pipeline)
		}
	}
	return ordered
}

func (s *Service) rememberProxyPipeline(name string) {
	s.mu.Lock()
	s.proxyPipeline = name
	s.mu.Unlock()
}
