package plugins

// An output folder for batch subtitle exports. `subtitle.save` asks the user
// where every single file goes, which is the right default and the wrong one
// for a plugin that turns twenty videos into twenty files. Here the user picks
// a folder once, through the host's own folder dialog, and the plugin may then
// write subtitle files into that folder and nowhere else: it names a file,
// never a directory, and an existing file is never replaced.
//
// The choice is the host's record, kept next to the plugin's other host-owned
// files rather than in its state, so a plugin cannot point it somewhere the
// user did not pick.

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const subtitleFolderFile = "subtitle-folder.json"

// Enough for "a (2).srt" through "a (999).srt"; a folder that already holds a
// thousand copies of one name is a loop in the plugin, not a user's choice.
const maxSubtitleNameAttempts = 1000

type subtitleFolderRecord struct {
	Path string `json:"path"`
}

// ChooseSubtitleFolder opens the folder picker and remembers the pick as this
// plugin's output folder. A cancelled dialog keeps the previous choice and
// returns an empty string.
func (s *Service) ChooseSubtitleFolder(pluginID string) (string, error) {
	if err := s.requirePermission(pluginID, "subtitle.export"); err != nil {
		return "", err
	}
	s.mu.RLock()
	app, home := s.app, s.home
	s.mu.RUnlock()
	if app == nil || home == nil {
		return "", errors.New("application window is not ready")
	}
	picked, err := app.Dialog.OpenFile().
		SetTitle("选择字幕保存位置").
		AttachToWindow(home).
		CanChooseDirectories(true).
		CanChooseFiles(false).
		CanCreateDirectories(true).
		PromptForSingleSelection()
	if err != nil && !dialogCancelled(err) {
		return "", err
	}
	if strings.TrimSpace(picked) == "" {
		return "", nil
	}
	return s.rememberSubtitleFolder(pluginID, picked)
}

// SubtitleFolder is the remembered output folder, or an empty string when none
// was chosen or the folder has since gone away.
func (s *Service) SubtitleFolder(pluginID string) (string, error) {
	if err := s.requirePermission(pluginID, "subtitle.export"); err != nil {
		return "", err
	}
	folder, err := s.subtitleFolder(pluginID)
	if err != nil || folder == "" {
		return "", err
	}
	if info, statErr := os.Stat(folder); statErr != nil || !info.IsDir() {
		return "", nil
	}
	return folder, nil
}

// SaveSubtitleToFolder writes plugin-built subtitle text into the remembered
// folder. The name is reduced to a safe bare file name, and a name already
// taken becomes "name (2).srt" rather than overwriting what is there.
func (s *Service) SaveSubtitleToFolder(pluginID, fileName, content string) (string, error) {
	if err := s.requirePermission(pluginID, "subtitle.export"); err != nil {
		return "", err
	}
	if strings.TrimSpace(content) == "" {
		return "", errors.New("subtitle content is empty")
	}
	if len(content) > maxSubtitleBytes {
		return "", errors.New("subtitle content exceeds 16 MB")
	}
	folder, err := s.subtitleFolder(pluginID)
	if err != nil {
		return "", err
	}
	if folder == "" {
		return "", errors.New("no output folder has been chosen")
	}
	if info, statErr := os.Stat(folder); statErr != nil || !info.IsDir() {
		return "", errors.New("the output folder no longer exists; choose it again")
	}
	return writeNewFile(folder, safeSubtitleFileName(fileName), []byte(content))
}

func (s *Service) rememberSubtitleFolder(pluginID, picked string) (string, error) {
	folder, err := filepath.Abs(picked)
	if err != nil {
		return "", err
	}
	if info, statErr := os.Stat(folder); statErr != nil || !info.IsDir() {
		return "", errors.New("the chosen folder is not a directory")
	}
	path, err := s.pluginDataPath(pluginID, subtitleFolderFile)
	if err != nil {
		return "", err
	}
	if err := writeJSONAtomic(path, subtitleFolderRecord{Path: folder}); err != nil {
		return "", err
	}
	return folder, nil
}

func (s *Service) subtitleFolder(pluginID string) (string, error) {
	path, err := s.pluginDataPath(pluginID, subtitleFolderFile)
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	var record subtitleFolderRecord
	if err := json.Unmarshal(data, &record); err != nil {
		// A damaged record means "not chosen", not a plugin that can never
		// export again: the next pick rewrites it.
		return "", nil
	}
	if !filepath.IsAbs(record.Path) {
		return "", nil
	}
	return filepath.Clean(record.Path), nil
}

// safeSubtitleFileName is subtitleFileName plus what a media title can carry
// that Windows refuses in a file name. The save dialog used to be the one to
// object; with no dialog in between, the host has to.
func safeSubtitleFileName(name string) string {
	name = subtitleFileName(name)
	extension := filepath.Ext(name)
	stem := strings.TrimSuffix(name, extension)
	stem = strings.Map(func(r rune) rune {
		if r < 0x20 || strings.ContainsRune(`<>:"/\|?*`, r) {
			return '_'
		}
		return r
	}, stem)
	stem = strings.TrimRight(strings.TrimSpace(stem), ". ")
	if stem == "" {
		stem = "subtitle"
	}
	if reservedWindowsName(stem) {
		stem = "_" + stem
	}
	return stem + strings.ToLower(extension)
}

func reservedWindowsName(stem string) bool {
	base := strings.ToUpper(stem)
	if index := strings.IndexByte(base, '.'); index >= 0 {
		base = base[:index]
	}
	switch base {
	case "CON", "PRN", "AUX", "NUL":
		return true
	}
	if len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) {
		return base[3] >= '1' && base[3] <= '9'
	}
	return false
}

// writeNewFile creates name in folder, or the first free "name (n)" beside it.
// O_EXCL makes "is it free" and "take it" one step, so two saves of the same
// title cannot both land on one name.
func writeNewFile(folder, name string, content []byte) (string, error) {
	extension := filepath.Ext(name)
	stem := strings.TrimSuffix(name, extension)
	for attempt := 1; attempt <= maxSubtitleNameAttempts; attempt++ {
		candidate := name
		if attempt > 1 {
			candidate = fmt.Sprintf("%s (%d)%s", stem, attempt, extension)
		}
		path := filepath.Join(folder, candidate)
		file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		if err != nil {
			return "", err
		}
		_, writeErr := file.Write(content)
		closeErr := file.Close()
		if writeErr != nil || closeErr != nil {
			_ = os.Remove(path)
			return "", errors.Join(writeErr, closeErr)
		}
		return path, nil
	}
	return "", fmt.Errorf("too many files named %s in the output folder", name)
}
