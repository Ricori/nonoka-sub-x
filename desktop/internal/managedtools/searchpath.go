package managedtools

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// guiSearchPathSupplements are the directories a macOS application launched from
// Finder or the Dock does not inherit. launchd hands a GUI process the bare
// `/usr/bin:/bin:/usr/sbin:/sbin`, so Homebrew's prefixes and uv's default
// install location are invisible to exec.LookPath -- and a machine with a
// perfectly good python3.12 or ffmpeg on it then looks to Nonoka X like a
// machine with neither. Nothing equivalent is needed on Windows, where the
// registry PATH reaches every process however it was started.
var guiSearchPathSupplements = []string{
	"/opt/homebrew/bin",
	"/opt/homebrew/sbin",
	"/usr/local/bin",
	"/usr/local/sbin",
}

// EnsureSearchPath widens this process's PATH with those directories. It runs
// before the sidecar interpreter is resolved, so exec.LookPath and every child
// process inherit the same search.
func EnsureSearchPath() {
	if runtime.GOOS != "darwin" {
		return
	}
	supplements := guiSearchPathSupplements
	if home := strings.TrimSpace(os.Getenv("HOME")); home != "" {
		supplements = append(append([]string{}, supplements...), filepath.Join(home, ".local", "bin"))
	}
	current := os.Getenv("PATH")
	if widened := augmentedSearchPath(current, supplements, isDirectory); widened != current {
		_ = os.Setenv("PATH", widened)
	}
}

func isDirectory(candidate string) bool {
	info, err := os.Stat(candidate)
	return err == nil && info.IsDir()
}

// augmentedSearchPath appends rather than prepends: whatever the user put on
// PATH themselves stays ahead of anything guessed here. Directories that are
// already listed, or that do not exist on this machine, are left out.
func augmentedSearchPath(current string, supplements []string, exists func(string) bool) string {
	entries := strings.Split(current, ":")
	present := make(map[string]bool, len(entries))
	for _, entry := range entries {
		present[entry] = true
	}
	for _, supplement := range supplements {
		if present[supplement] {
			continue
		}
		if !exists(supplement) {
			continue
		}
		present[supplement] = true
		entries = append(entries, supplement)
	}
	return strings.Join(entries, ":")
}
