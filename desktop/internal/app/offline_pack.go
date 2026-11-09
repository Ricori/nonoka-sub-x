package app

import (
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// An offline package is a directory named offline-pack beside the executable.
// Files are kept by what they are (files/<sha256>/, python/<tag>/, launcher/),
// not by which release they belong to, so a package is never out of date as a
// whole: whatever it lacks is downloaded as usual, and new files can be merged
// in. NONOKA_OFFLINE_PACK names one elsewhere, and is how the sidecar and its
// children are told about the one found here.
const (
	offlinePackEnvironment = "NONOKA_OFFLINE_PACK"
	offlinePackDirectory   = "offline-pack"
)

func isOfflinePack(directory string) bool {
	info, err := os.Stat(filepath.Join(directory, "pack.json"))
	return err == nil && info.Mode().IsRegular()
}

// offlinePack is the absolute path of the package in use, or "".
func offlinePack() string {
	if configured := strings.TrimSpace(os.Getenv(offlinePackEnvironment)); configured != "" {
		if resolved, err := filepath.Abs(configured); err == nil && isOfflinePack(resolved) {
			return resolved
		}
		return ""
	}
	executable, err := os.Executable()
	if err != nil {
		return ""
	}
	candidate := filepath.Join(filepath.Dir(executable), offlinePackDirectory)
	if isOfflinePack(candidate) {
		return candidate
	}
	return ""
}

// announceOfflinePack tells every child process where the package is.
func announceOfflinePack() {
	if pack := offlinePack(); pack != "" {
		_ = os.Setenv(offlinePackEnvironment, pack)
	}
}

// offlineRung puts the local rung first when the package has this directory.
func offlineRung(attempts []bootstrapAttempt, directory string) []bootstrapAttempt {
	if directory == "" {
		return attempts
	}
	if info, err := os.Stat(directory); err != nil || !info.IsDir() {
		return attempts
	}
	return append([]bootstrapAttempt{{Local: true}}, attempts...)
}

// offlineSubdirectory names a directory of the package, or "" with no package.
func offlineSubdirectory(name string) string {
	pack := offlinePack()
	if pack == "" {
		return ""
	}
	return filepath.Join(pack, name)
}

// fileURI spells a local directory the way uv's mirror variable takes it.
func fileURI(path string) string {
	slashed := filepath.ToSlash(path)
	if !strings.HasPrefix(slashed, "/") {
		slashed = "/" + slashed
	}
	return (&url.URL{Scheme: "file", Path: slashed}).String()
}

// seedFromOfflinePack copies the file with this digest to destination. The
// caller checks the result like any download, so wrong bytes get no further.
func seedFromOfflinePack(digest, destination string) bool {
	directory := offlineSubdirectory(filepath.Join("files", strings.ToLower(digest)))
	if directory == "" {
		return false
	}
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 1 || !entries[0].Type().IsRegular() {
		return false
	}
	return copyFileInto(filepath.Join(directory, entries[0].Name()), destination) == nil
}

func copyFileInto(source, destination string) error {
	reader, err := os.Open(source)
	if err != nil {
		return err
	}
	defer reader.Close()
	partial := destination + ".part"
	writer, err := os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err = io.Copy(writer, reader); err != nil {
		writer.Close()
		_ = os.Remove(partial)
		return err
	}
	if err = writer.Close(); err != nil {
		_ = os.Remove(partial)
		return err
	}
	if err = os.Rename(partial, destination); err != nil {
		_ = os.Remove(partial)
	}
	return err
}
