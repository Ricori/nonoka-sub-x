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
	"strconv"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

// erasedDirName is the folder in the video cache that holds the copies of
// videos with their on-screen text erased. Cache eviction and "clear cache"
// only look at files directly in the cache directory, so these stay: each is
// the entry's source and hours of inpainting to make again.
const erasedDirName = "erased"

// EraseResult is the video EraseScreenText wrote.
type EraseResult struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
	// Applied reports that the library entry already plays the new file. It is
	// false while the editor has the video open: the player still holds the
	// old file, so the editor lets go of it and calls UseErasedVideo.
	Applied bool `json:"applied"`
}

// eraseProgressID is the media:progress id of a video's erase job.
func eraseProgressID(id string) string { return "erase_" + id }

// EraseScreenText removes the original on-screen text under every cover box in
// platesASS from the whole video with FlowOCR's LaMa inpainter, and writes the
// result, with the original's audio, to the video cache (<cache>/erased/).
// The original file is left untouched.
func (s *Service) EraseScreenText(id, platesASS, device, preset string) (EraseResult, error) {
	if !validID(id) || len(platesASS) > 64<<20 {
		return EraseResult{}, errors.New("invalid erase request")
	}
	if strings.TrimSpace(platesASS) == "" {
		return EraseResult{}, errors.New("文档里没有可抹除的原字框，请先做文字识别")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return EraseResult{}, err
	}
	if entry.AudioOnly {
		return EraseResult{}, errors.New("纯音频素材没有画面可抹除")
	}
	original := entry.SourcePath
	if entry.ErasedFrom != "" {
		original = entry.ErasedFrom
	}
	if !fileExists(original) {
		return EraseResult{}, errors.New("找不到原视频文件，请先重新关联")
	}
	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return EraseResult{}, errors.New("抹除原字需要 FFmpeg，请先在设置中安装媒体工具")
	}

	ctx, finish, ok := s.beginErase(id)
	if !ok {
		return EraseResult{}, errors.New("这个视频正在抹除原字")
	}
	defer finish()

	erasedDir := s.erasedDirectory()
	if err := os.MkdirAll(erasedDir, 0o755); err != nil {
		return EraseResult{}, fmt.Errorf("无法创建缓存目录：%w", err)
	}
	removeErasedParts(erasedDir, id)
	destination := erasedOutputPath(erasedDir, id, time.Now())
	partial := destination + ".part.mp4"
	// Fail on an unwritable folder now, not after hours of inpainting.
	placeholder, err := os.OpenFile(partial, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		return EraseResult{}, fmt.Errorf("无法在缓存目录写入新视频：%w", err)
	}
	_ = placeholder.Close()
	defer os.Remove(partial)
	required := estimateEraseBytes(entry.Duration)
	if free, err := diskFreeBytes(erasedDir); err == nil && free < required {
		return EraseResult{}, fmt.Errorf("缓存目录所在磁盘剩余空间不足（需要约 %.1f GB，当前剩余 %.1f GB）",
			float64(required)/float64(1<<30), float64(free)/float64(1<<30))
	}

	temporary := filepath.Join(s.root, "temporary")
	if err := os.MkdirAll(temporary, 0o700); err != nil {
		return EraseResult{}, err
	}
	work, err := os.MkdirTemp(temporary, "screen-text-erase-")
	if err != nil {
		return EraseResult{}, err
	}
	defer os.RemoveAll(work)

	jobID := eraseProgressID(id)
	erase := EraseOptions{Enabled: true, PlatesASS: platesASS, Device: device, Preset: preset}
	cleaned, err := s.runEraseIntermediate(ctx, work, entry, original, 0, entry.Duration, erase, "erase", jobID, destination)
	if err != nil {
		return EraseResult{}, err
	}

	s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "erase", Phase: "mux", Total: entry.Duration, Output: destination})
	if err := s.runEraseMux(ctx, ffmpeg, jobID, cleaned, original, partial, destination, entry.Duration); err != nil {
		return EraseResult{}, err
	}
	if err := os.Rename(partial, destination); err != nil {
		return EraseResult{}, err
	}
	stat, err := os.Stat(destination)
	if err != nil {
		return EraseResult{}, err
	}
	result := EraseResult{Path: destination, Size: stat.Size()}
	s.mu.RLock()
	open := s.activeMedia == id
	s.mu.RUnlock()
	if !open {
		if _, err := s.applyErased(id, destination, original); err != nil {
			return result, err
		}
		result.Applied = true
	}
	return result, nil
}

// CancelEraseScreenText stops a running EraseScreenText, LaMa task included.
func (s *Service) CancelEraseScreenText(id string) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	s.mu.RLock()
	cancel := s.eraseCancels[id]
	s.mu.RUnlock()
	if cancel != nil {
		cancel()
	}
	return nil
}

// UseErasedVideo makes the entry play the erased copy EraseScreenText wrote,
// remembering the original. The caller must have closed the player first.
func (s *Service) UseErasedVideo(id, path string) (Entry, error) {
	entry, err := s.entryByID(id)
	if err != nil {
		return Entry{}, err
	}
	original := entry.SourcePath
	if entry.ErasedFrom != "" {
		original = entry.ErasedFrom
	}
	if samePath(path, original) {
		return Entry{}, errors.New("抹字视频不能是原视频本身")
	}
	return s.applyErased(id, path, original)
}

// RestoreOriginalVideo switches the entry back from the erased copy to the
// original video and deletes the erased copies from the cache.
func (s *Service) RestoreOriginalVideo(id string) (Entry, error) {
	entry, err := s.entryByID(id)
	if err != nil {
		return Entry{}, err
	}
	if entry.ErasedFrom == "" {
		return entry, nil
	}
	if !fileExists(entry.ErasedFrom) {
		return Entry{}, errors.New("原视频文件找不到了：" + entry.ErasedFrom)
	}
	if err := s.removeCachedMedia(id); err != nil {
		return Entry{}, fmt.Errorf("旧视频还在使用中：%w", err)
	}
	restored, err := s.relink(id, entry.ErasedFrom, "")
	if err != nil {
		return Entry{}, err
	}
	s.removeErasedFiles(id, "")
	return restored, nil
}

// applyErased drops the working copy of the old picture (the player and every
// task would keep reading it ahead of the source) and relinks to the new one.
func (s *Service) applyErased(id, path, original string) (Entry, error) {
	if err := s.removeCachedMedia(id); err != nil {
		return Entry{}, fmt.Errorf("旧视频还在使用中：%w", err)
	}
	entry, err := s.relink(id, path, original)
	if err != nil {
		return Entry{}, err
	}
	// An earlier erase of the same video is no longer anyone's source.
	s.removeErasedFiles(id, path)
	return entry, nil
}

func (s *Service) erasedDirectory() string { return filepath.Join(s.cacheDirectory(), erasedDirName) }

// erasedFiles are the erased copies of one video in the cache.
func (s *Service) erasedFiles(id string) []string {
	dir := s.erasedDirectory()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var files []string
	for _, entry := range entries {
		if !entry.IsDir() && erasedFileID(entry.Name()) == id {
			files = append(files, filepath.Join(dir, entry.Name()))
		}
	}
	return files
}

// removeErasedFiles deletes a video's erased copies except keep, best effort:
// a copy still held open stays until the next erase or restore.
func (s *Service) removeErasedFiles(id, keep string) {
	for _, path := range s.erasedFiles(id) {
		if keep == "" || !samePath(path, keep) {
			_ = os.Remove(path)
		}
	}
}

// rebaseErasedSources follows the erased copies when the cache directory moved
// from oldDir (the move takes the whole tree): entries playing one point at
// its new place.
func (s *Service) rebaseErasedSources(oldDir string) {
	if oldDir == "" {
		return
	}
	oldErased := filepath.Join(oldDir, erasedDirName)
	newErased := s.erasedDirectory()
	if samePath(oldErased, newErased) {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	changed := false
	for index := range s.entries {
		entry := s.entries[index]
		if entry.ErasedFrom == "" || !samePath(filepath.Dir(entry.SourcePath), oldErased) {
			continue
		}
		moved := filepath.Join(newErased, filepath.Base(entry.SourcePath))
		if fileExists(moved) {
			s.entries[index].SourcePath = moved
			changed = true
		}
	}
	if changed {
		_ = s.saveLocked()
	}
}

// removeErasedParts drops what an interrupted erase of the video left behind.
func removeErasedParts(dir, id string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		name := entry.Name()
		if !entry.IsDir() && strings.HasSuffix(name, ".part.mp4") && erasedFileID(strings.TrimSuffix(name, ".part.mp4")) == id {
			_ = os.Remove(filepath.Join(dir, name))
		}
	}
}

func (s *Service) beginErase(id string) (context.Context, func(), bool) {
	s.mu.Lock()
	if s.eraseCancels == nil {
		s.eraseCancels = make(map[string]context.CancelFunc)
	}
	if _, running := s.eraseCancels[id]; running {
		s.mu.Unlock()
		return nil, nil, false
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.eraseCancels[id] = cancel
	s.mu.Unlock()
	return ctx, func() {
		s.mu.Lock()
		delete(s.eraseCancels, id)
		s.mu.Unlock()
		cancel()
	}, true
}

// erasedOutputPath is "<dir>/<id>-<unix ms>.mp4". Every erase gets a new name:
// the previous copy may be the file the player has open, which Windows will
// not let anything replace.
func erasedOutputPath(dir, id string, now time.Time) string {
	stamp := now.UnixMilli()
	candidate := filepath.Join(dir, fmt.Sprintf("%s-%d.mp4", id, stamp))
	for fileExists(candidate) || fileExists(candidate+".part.mp4") {
		stamp++
		candidate = filepath.Join(dir, fmt.Sprintf("%s-%d.mp4", id, stamp))
	}
	return candidate
}

// erasedFileID is the media id of an erased copy's file name ("" for others).
func erasedFileID(name string) string {
	if !strings.EqualFold(filepath.Ext(name), ".mp4") {
		return ""
	}
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	cut := strings.LastIndex(stem, "-")
	if cut <= 0 {
		return ""
	}
	if _, err := strconv.ParseInt(stem[cut+1:], 10, 64); err != nil {
		return ""
	}
	return stem[:cut]
}

// runEraseMux puts the original's audio back under the inpainted picture. The
// audio is copied when MP4 takes its codec and re-encoded to AAC otherwise.
func (s *Service) runEraseMux(ctx context.Context, ffmpeg, jobID, video, audio, output, destination string, duration float64) error {
	var last error
	for _, reencode := range []bool{false, true} {
		command := exec.CommandContext(ctx, ffmpeg, buildEraseMuxArgs(video, audio, output, reencode)...)
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
			done, ok := parseTranscodeProgress(scanner.Text(), duration)
			if !ok || time.Since(lastProgress) < 150*time.Millisecond {
				continue
			}
			lastProgress = time.Now()
			s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "erase", Phase: "mux", Done: done, Total: duration, Output: destination})
		}
		runErr := command.Wait()
		if errors.Is(ctx.Err(), context.Canceled) {
			return errors.New("已取消")
		}
		if runErr == nil {
			s.emitMediaProgress(MediaProgress{ID: jobID, Stage: "erase", Phase: "mux", Done: duration, Total: duration, Output: destination})
			return nil
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = runErr.Error()
		}
		last = fmt.Errorf("合成音轨失败：%s", message)
	}
	return last
}

func buildEraseMuxArgs(video, audio, output string, reencodeAudio bool) []string {
	args := []string{
		"-v", "error", "-y", "-i", video, "-i", audio,
		"-map", "0:v:0", "-map", "1:a?", "-sn", "-dn", "-c:v", "copy",
	}
	if reencodeAudio {
		args = append(args, "-c:a", "aac", "-b:a", "256k")
	} else {
		args = append(args, "-c:a", "copy")
	}
	return append(args, "-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", output)
}
