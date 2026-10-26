package library

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
)

func (s *Service) ResolveMedia(id string) (path, title, fingerprint string, duration float64, err error) {
	if !validID(id) {
		return "", "", "", 0, errors.New("invalid media id")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return "", "", "", 0, err
	}
	path = s.cachedPath(id)
	if path == "" {
		path = entry.SourcePath
	}
	if !fileExists(path) {
		return "", "", "", 0, errors.New("local media is missing; relink it first")
	}
	s.touchEntry(id)
	return path, entry.Title, entry.Fingerprint, entry.Duration, nil
}

func (s *Service) Rename(id, title string) (Entry, error) {
	if !validID(id) {
		return Entry{}, errors.New("invalid media id")
	}
	title = strings.TrimSpace(title)
	if title == "" || len([]rune(title)) > 500 {
		return Entry{}, errors.New("title must contain 1 to 500 characters")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for index := range s.entries {
		if s.entries[index].ID != id {
			continue
		}
		previous := s.entries[index].Title
		s.entries[index].Title = title
		if err := s.saveLocked(); err != nil {
			s.entries[index].Title = previous
			return Entry{}, err
		}
		entry := s.entries[index]
		go s.emitChanged()
		return entry, nil
	}
	return Entry{}, errors.New("media entry not found")
}

func (s *Service) Remove(id string, deleteDocument bool) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	s.mu.Lock()
	index := -1
	for current := range s.entries {
		if s.entries[current].ID == id {
			index = current
			break
		}
	}
	if index < 0 {
		s.mu.Unlock()
		return errors.New("media entry not found")
	}
	previous := append([]Entry(nil), s.entries...)
	s.entries = append(s.entries[:index], s.entries[index+1:]...)
	if err := s.saveLocked(); err != nil {
		s.entries = previous
		s.mu.Unlock()
		return err
	}
	s.mu.Unlock()
	_ = os.Remove(s.thumbnailPath(id))
	if err := s.removeCachedMedia(id); err != nil {
		return err
	}
	if deleteDocument {
		directory := filepath.Join(s.root, "documents", id)
		if filepath.Dir(directory) != filepath.Join(s.root, "documents") {
			return errors.New("invalid document directory")
		}
		if err := os.RemoveAll(directory); err != nil {
			return err
		}
	}
	s.emitChanged()
	return nil
}

// DeleteDocument removes the editable subtitles and their revision history
// while keeping the media entry, thumbnail and source video in the library.
func (s *Service) DeleteDocument(id string) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	directory := filepath.Join(s.root, "documents", id)
	if filepath.Dir(directory) != filepath.Join(s.root, "documents") {
		return errors.New("invalid document directory")
	}
	s.mu.Lock()
	index := -1
	for current := range s.entries {
		if s.entries[current].ID == id {
			index = current
			break
		}
	}
	if index < 0 {
		s.mu.Unlock()
		return errors.New("media entry not found")
	}
	previous := s.entries[index].DocumentRemoved
	s.entries[index].DocumentRemoved = true
	if err := s.saveLocked(); err != nil {
		s.entries[index].DocumentRemoved = previous
		s.mu.Unlock()
		return err
	}
	s.mu.Unlock()
	if err := os.RemoveAll(directory); err != nil {
		s.mu.Lock()
		for current := range s.entries {
			if s.entries[current].ID == id {
				s.entries[current].DocumentRemoved = previous
				break
			}
		}
		_ = s.saveLocked()
		s.mu.Unlock()
		return err
	}
	s.emitChanged()
	return nil
}

func (s *Service) PickRelink(id string) (Entry, error) {
	if !validID(id) {
		return Entry{}, errors.New("invalid media id")
	}
	app, window := s.dialogWindow()
	if app == nil || window == nil {
		return Entry{}, errors.New("application window is not ready")
	}
	path, err := app.Dialog.OpenFile().
		SetTitle("重新定位本地媒体").
		AttachToWindow(window).
		AddFilter("视频或音频文件", mediaDialogPattern).
		PromptForSingleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if err != nil || path == "" {
		return Entry{}, err
	}
	return s.Relink(id, path)
}

func (s *Service) Relink(id, path string) (Entry, error) {
	if !validID(id) {
		return Entry{}, errors.New("invalid media id")
	}
	absolute, err := filepath.Abs(strings.TrimSpace(path))
	if err != nil {
		return Entry{}, err
	}
	if _, supported := supportedExtensions[strings.ToLower(filepath.Ext(absolute))]; !supported {
		return Entry{}, errors.New("unsupported media format")
	}
	stat, err := os.Stat(absolute)
	if err != nil || !stat.Mode().IsRegular() {
		return Entry{}, errors.New("selected media is not a readable file")
	}
	metadata, err := s.prober.Probe(context.Background(), absolute)
	if err != nil || !usableMetadata(metadata) {
		return Entry{}, errors.New("selected file has no readable video or audio track")
	}
	fingerprint, err := fileFingerprint(absolute, stat.Size())
	if err != nil {
		return Entry{}, err
	}
	s.mu.Lock()
	for index := range s.entries {
		if s.entries[index].ID != id {
			continue
		}
		previous := s.entries[index]
		s.entries[index].SourcePath = absolute
		s.entries[index].Size = stat.Size()
		s.entries[index].Duration = metadata.Duration
		s.entries[index].Width = metadata.Width
		s.entries[index].Height = metadata.Height
		s.entries[index].AudioOnly = !metadata.HasVideo
		// Once subtitles exist, the fingerprint identifies that subtitle record
		// locally and in the cloud. An arbitrarily associated video is only its
		// playback source; replacing the identity here makes the original cloud
		// entry look unmatched and creates another subtitle-only card on the next
		// adoption. Entries without subtitles can still take on the new video's
		// identity as before.
		if !fileExists(filepath.Join(s.root, "documents", id, "document.json")) {
			s.entries[index].Fingerprint = fingerprint
		}
		s.entries[index].LastAccess = time.Now().UnixMilli()
		s.entries[index].Available = true
		if err := s.saveLocked(); err != nil {
			s.entries[index] = previous
			s.mu.Unlock()
			return Entry{}, err
		}
		entry := s.entries[index]
		s.mu.Unlock()
		// A different file makes the old proxy and filmstrips show the wrong
		// picture; the same file moved elsewhere keeps them.
		if fingerprint != previous.Fingerprint {
			s.dropPreviewDerivatives(id)
		}
		_ = s.thumbnailer.Generate(context.Background(), absolute, s.thumbnailPath(id), thumbnailDuration(metadata))
		s.emitChanged()
		return entry, nil
	}
	s.mu.Unlock()
	return Entry{}, errors.New("media entry not found")
}

func (s *Service) MediaURL(id string) (string, error) {
	if !validID(id) {
		return "", errors.New("invalid media id")
	}
	entry, err := s.entryByID(id)
	if err != nil {
		return "", err
	}
	path := s.cachedPath(id)
	if path == "" {
		path = entry.SourcePath
	}
	if path == "" || !fileExists(path) {
		return "", errors.New("local media is missing; relink it first")
	}
	s.touchEntry(id)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.media == nil {
		server, err := newLoopbackMediaServer(func(mediaID string, proxy bool) string {
			if proxy {
				return s.readyProxy(mediaID)
			}
			cached := s.cachedPath(mediaID)
			if cached != "" {
				return cached
			}
			entry, lookupErr := s.entryByID(mediaID)
			if lookupErr != nil {
				return ""
			}
			return entry.SourcePath
		})
		if err != nil {
			return "", err
		}
		s.media = server
	}
	return s.media.URL(id), nil
}

func (s *Service) SaveSubtitle(defaultName, content string) (string, error) {
	if len(content) > 64<<20 {
		return "", errors.New("subtitle content exceeds 64 MB")
	}
	app, window := s.dialogWindow()
	if app == nil || window == nil {
		return "", errors.New("application window is not ready")
	}
	extension := strings.ToLower(filepath.Ext(defaultName))
	if extension != ".srt" && extension != ".ass" {
		extension = ".srt"
	}
	name := strings.TrimSpace(filepath.Base(defaultName))
	if name == "" || name == "." {
		name = "subtitle" + extension
	}
	path, err := app.Dialog.SaveFile().SetFilename(name).
		AddFilter(strings.ToUpper(strings.TrimPrefix(extension, "."))+" 字幕", "*"+extension).
		AttachToWindow(window).PromptForSingleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if err != nil || path == "" {
		return "", err
	}
	if !strings.EqualFold(filepath.Ext(path), extension) {
		path += extension
	}
	return path, os.WriteFile(path, []byte(content), 0o644)
}

func (s *Service) ExportVideo(id, ass string) (string, error) {
	if !validID(id) || len(ass) > 64<<20 {
		return "", errors.New("invalid video export request")
	}
	s.mu.RLock()
	app := s.app
	var entry *Entry
	for index := range s.entries {
		if s.entries[index].ID == id {
			copy := s.entries[index]
			entry = &copy
			break
		}
	}
	window := s.editor
	if window == nil {
		window = s.home
	}
	s.mu.RUnlock()
	if app == nil || window == nil || entry == nil || !fileExists(entry.SourcePath) {
		return "", errors.New("local media is unavailable")
	}
	if entry.AudioOnly {
		return "", errAudioOnlyExport
	}
	name :=strings.TrimSuffix(entry.Title, filepath.Ext(entry.Title)) + " - 字幕.mp4"
	output, err := app.Dialog.SaveFile().SetFilename(name).AddFilter("MP4 视频", "*.mp4").AttachToWindow(window).PromptForSingleSelection()
	if dialogCancelled(err) {
		err = nil
	}
	if err != nil || output == "" {
		return "", err
	}
	if !strings.EqualFold(filepath.Ext(output), ".mp4") {
		output += ".mp4"
	}
	sourceAbsolute, sourceErr := filepath.Abs(entry.SourcePath)
	outputAbsolute, outputErr := filepath.Abs(output)
	if sourceErr != nil || outputErr != nil {
		return "", errors.New("resolve video export paths")
	}
	if (runtime.GOOS == "windows" && strings.EqualFold(filepath.Clean(sourceAbsolute), filepath.Clean(outputAbsolute))) || (runtime.GOOS != "windows" && filepath.Clean(sourceAbsolute) == filepath.Clean(outputAbsolute)) {
		return "", errors.New("export destination must not overwrite the source video")
	}
	ffmpeg, err := managedtools.Find(s.root, "ffmpeg")
	if err != nil {
		return "", errors.New("ffmpeg is required to export embedded subtitles")
	}
	temporaryRoot := filepath.Join(s.root, "temporary")
	if err := os.MkdirAll(temporaryRoot, 0o700); err != nil {
		return "", err
	}
	work, err := os.MkdirTemp(temporaryRoot, "export-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(work)
	if err := os.WriteFile(filepath.Join(work, "subtitle.ass"), []byte(ass), 0o600); err != nil {
		return "", err
	}
	fontsCopied, err := s.copyBundledFonts(filepath.Join(work, "fonts"))
	if err != nil {
		return "", err
	}
	filter := "subtitles=subtitle.ass"
	if fontsCopied {
		filter += ":fontsdir=fonts"
	}
	partial := output + ".part.mp4"
	command := exec.Command(ffmpeg, "-v", "error", "-y", "-i", entry.SourcePath, "-vf", filter, "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "192k", partial)
	command.Dir = work
	configureMediaCommand(command)
	if result, runErr := command.CombinedOutput(); runErr != nil {
		_ = os.Remove(partial)
		return "", fmt.Errorf("export video: %s", strings.TrimSpace(string(result)))
	}
	if err := os.Rename(partial, output); err != nil {
		_ = os.Remove(partial)
		return "", err
	}
	return output, nil
}

func (s *Service) RevealInFolder(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	if _, err := os.Stat(absolute); err != nil {
		return err
	}
	switch runtime.GOOS {
	case "windows":
		return exec.Command("explorer.exe", "/select,", absolute).Start()
	case "darwin":
		return exec.Command("open", "-R", absolute).Start()
	default:
		return fmt.Errorf("reveal is unsupported on %s", runtime.GOOS)
	}
}

func (s *Service) Close() error {
	s.mu.Lock()
	server := s.media
	s.media = nil
	s.mu.Unlock()
	if server != nil {
		return server.Close()
	}
	return nil
}
