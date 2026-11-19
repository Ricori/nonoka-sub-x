package app

import (
	"bytes"
	"cmp"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

const bundledResourcePrefix = "bundled/nonoka_x"

// How many extracted version directories survive a launch, the running one
// included. One spare keeps a self-update rollback (and a second build run
// against the same data directory) from paying a full re-extraction, while
// still bounding what is otherwise an unbounded pile of ~170 MB copies.
const retainedResourceVersions = 2

// The build task stages the sidecar and pinned FineSub snapshot here before
// compiling. The tracked placeholder also keeps source-only Go tests valid.
//
//go:embed all:bundled/nonoka_x
var bundledResources embed.FS

// stagedResource is one entry of STAGED.json, written by build/tools/stage.
// Only what an integrity check needs is decoded; the hash stays unread because
// re-reading ~170 MB on every launch costs far more than it catches.
type stagedResource struct {
	Path string `json:"path"`
	Size int64  `json:"size"`
}

func ensureBundledResources(dataDirectory string) (string, error) {
	return extractBundledResources(bundledResources, bundledResourcePrefix, dataDirectory)
}

func extractBundledResources(source fs.FS, prefix, dataDirectory string) (string, error) {
	manifestPath := prefix + "/STAGED.json"
	manifest, err := fs.ReadFile(source, manifestPath)
	if errors.Is(err, fs.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read embedded resource manifest: %w", err)
	}
	staged, err := parseStagedManifest(manifest)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(manifest)
	version := hex.EncodeToString(digest[:8])
	base := filepath.Join(dataDirectory, "app-resources")
	root := filepath.Join(base, version, "nonoka_x")
	installedManifest := filepath.Join(root, "STAGED.json")
	// A matching manifest is not proof the tree beneath it survived: antivirus
	// quarantine, a cleanup tool, or an interrupted profile sync can take the
	// Python sources and leave the small JSON behind. Without this check the
	// damage is permanent — every later launch trusts the manifest, and the
	// runtime page repeats "sidecar script is unavailable" forever.
	if existing, readErr := os.ReadFile(installedManifest); readErr == nil && bytes.Equal(existing, manifest) {
		if stagedResourcesPresent(root, staged) {
			pruneResourceVersions(base, version)
			return root, nil
		}
	}
	// Staging plus a rename keeps the live root either whole or absent. Writing
	// into it directly leaves a half-tree behind whenever extraction is
	// interrupted, and the manifest that vouches for it is written last.
	staging := root + ".staging"
	if err := os.RemoveAll(staging); err != nil {
		return "", fmt.Errorf("replace embedded resources: %w", err)
	}
	if err := os.MkdirAll(staging, 0o700); err != nil {
		return "", fmt.Errorf("create embedded resource directory: %w", err)
	}
	err = fs.WalkDir(source, prefix, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() || path == manifestPath || entry.Name() == ".gitkeep" {
			return nil
		}
		relative := strings.TrimPrefix(path, prefix+"/")
		if relative == path || relative == "" || strings.Contains(relative, "..") {
			return fmt.Errorf("invalid embedded resource path %q", path)
		}
		contents, err := fs.ReadFile(source, path)
		if err != nil {
			return err
		}
		destination := filepath.Join(staging, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(destination), 0o700); err != nil {
			return err
		}
		return os.WriteFile(destination, contents, 0o600)
	})
	if err != nil {
		return "", fmt.Errorf("extract embedded resources: %w", err)
	}
	if err := os.WriteFile(filepath.Join(staging, "STAGED.json"), manifest, 0o600); err != nil {
		return "", fmt.Errorf("commit embedded resources: %w", err)
	}
	// Rename onto an existing directory fails on Windows, so the damaged tree
	// goes first. A failure between the two leaves nothing to trust and the
	// next launch extracts again, which is the recoverable side to fail on.
	if err := removeAllWithRetry(root); err != nil {
		return "", fmt.Errorf("replace embedded resources: %w", err)
	}
	if err := renamePathWithRetry(staging, root); err != nil {
		return "", fmt.Errorf("commit embedded resources: %w", err)
	}
	pruneResourceVersions(base, version)
	return root, nil
}

func parseStagedManifest(manifest []byte) ([]stagedResource, error) {
	var decoded struct {
		Files []stagedResource `json:"files"`
	}
	if err := json.Unmarshal(manifest, &decoded); err != nil {
		return nil, fmt.Errorf("parse embedded resource manifest: %w", err)
	}
	return decoded.Files, nil
}

// stagedResourcesPresent reports whether every staged file is still on disk at
// its recorded size. Size is the cheap half of the manifest: it catches
// truncation and removal, the two ways an extracted tree is observed to rot,
// for one stat per file.
func stagedResourcesPresent(root string, staged []stagedResource) bool {
	for _, entry := range staged {
		if entry.Path == "" || strings.Contains(entry.Path, "..") {
			return false
		}
		info, err := os.Stat(filepath.Join(root, filepath.FromSlash(entry.Path)))
		if err != nil || info.IsDir() || info.Size() != entry.Size {
			return false
		}
	}
	return true
}

// pruneResourceVersions keeps the running version and the most recent spares,
// deleting the rest. Best effort: a directory another Nonoka X process still runs
// from refuses to go on Windows, and skipping it is preferable to failing a
// launch over disk hygiene.
func pruneResourceVersions(base, keep string) {
	entries, err := os.ReadDir(base)
	if err != nil {
		return
	}
	type candidate struct {
		name    string
		modTime int64
	}
	stale := make([]candidate, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() || entry.Name() == keep {
			continue
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			continue
		}
		stale = append(stale, candidate{name: entry.Name(), modTime: info.ModTime().UnixNano()})
	}
	slices.SortFunc(stale, func(a, b candidate) int { return cmp.Compare(b.modTime, a.modTime) })
	for index, entry := range stale {
		if index < retainedResourceVersions-1 {
			continue
		}
		_ = os.RemoveAll(filepath.Join(base, entry.name))
	}
}
