package plugins

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// systemDigestName records which embedded bytes produced the installed copy.
const systemDigestName = ".content-digest"

// System plugins are first-party tools that ship inside the Nonoka X binary.
// They deliberately reuse the ordinary plugin package format — same manifest,
// same declared permissions, same sandboxed page — so there is one plugin
// contract to reason about rather than two. Only their origin differs, and
// origin is what capabilities.go reads when it decides how much of a host
// capability a caller may reach.
//
//go:embed all:system
var systemPackages embed.FS

const systemPackageRoot = "system"

// systemOffByDefault names the built-ins that ship installed but switched off:
// tools only some users need, which they turn on in the plugin manager. Kept
// here rather than in the manifest, for the same reason System is: whether a
// built-in starts mounted is the host's decision, not the package's.
var systemOffByDefault = map[string]bool{
	"dev.nonoka.batch-transcribe": true,
}

// ensureSystemPlugins materialises the embedded packages under the system root
// so every path-based reader (manifest parsing, page loading, validation) keeps
// working unchanged. Publication is keyed by the content digest rather than the
// version string: a built-in's bytes are decided entirely by the build, and
// keying on the hand-maintained version means editing a page without bumping it
// leaves the old copy installed forever, which is silent and very easy to miss.
// Superseded version directories are pruned so upgrades do not pile up.
func (s *Service) ensureSystemPlugins() error {
	entries, err := fs.ReadDir(systemPackages, systemPackageRoot)
	if err != nil {
		// A tree with no staged packages is valid; the build simply ships no
		// built-in tools and the plugin list stays user-only.
		return nil
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if err := s.publishSystemPackage(systemPackageRoot + "/" + entry.Name()); err != nil {
			return fmt.Errorf("publish system plugin %s: %w", entry.Name(), err)
		}
	}
	return nil
}

func (s *Service) publishSystemPackage(source string) error {
	digest, err := embeddedPackageDigest(systemPackages, source)
	if err != nil {
		return err
	}
	staging, err := os.MkdirTemp(s.systemRoot, ".stage-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(staging)
	if err := copyEmbeddedPackage(systemPackages, source, staging); err != nil {
		return err
	}
	// Built-ins go through the same manifest validation as sideloaded packages;
	// a malformed one should fail the build's own tests, not ship silently.
	manifest, err := readManifest(staging)
	if err != nil {
		return err
	}
	if err := validateManifest(manifest, staging); err != nil {
		return err
	}
	pluginRoot := filepath.Join(s.systemRoot, manifest.ID)
	if filepath.Dir(pluginRoot) != s.systemRoot {
		return errors.New("invalid system plugin id")
	}
	pointerPath := filepath.Join(pluginRoot, currentName)
	digestPath := filepath.Join(pluginRoot, systemDigestName)
	destination := filepath.Join(pluginRoot, "versions", manifest.Version)
	if info, statErr := os.Stat(filepath.Join(destination, manifestName)); statErr == nil && info.Mode().IsRegular() {
		if recorded, readErr := os.ReadFile(digestPath); readErr == nil && string(recorded) == digest {
			// Same bytes already installed. Rewriting the pointer costs one
			// small file and repairs a data directory whose pointer was lost.
			return writeJSONAtomic(pointerPath, currentPointer{Current: manifest.Version})
		}
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		return err
	}
	// A half-extracted directory from an interrupted launch would otherwise
	// block the rename and leave the plugin permanently broken.
	if err := os.RemoveAll(destination); err != nil {
		return err
	}
	if err := os.Rename(staging, destination); err != nil {
		return err
	}
	if err := writeJSONAtomic(pointerPath, currentPointer{Current: manifest.Version}); err != nil {
		_ = os.RemoveAll(destination)
		return err
	}
	// Written last: a digest recorded before the content is in place would make
	// an interrupted publish look complete on the next launch.
	if err := os.WriteFile(digestPath, []byte(digest), 0o600); err != nil {
		return err
	}
	pruneSupersededVersions(filepath.Join(pluginRoot, "versions"), manifest.Version)
	return nil
}

// embeddedPackageDigest hashes every file in the package. fs.WalkDir visits in
// lexical order, so the result depends only on the contents, not the traversal.
func embeddedPackageDigest(source fs.FS, root string) (string, error) {
	hash := sha256.New()
	err := fs.WalkDir(source, root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		data, err := fs.ReadFile(source, path)
		if err != nil {
			return err
		}
		// Length-prefixed so no rename can collide with a content change.
		fmt.Fprintf(hash, "%s\n%d\n", path, len(data))
		hash.Write(data)
		return nil
	})
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func copyEmbeddedPackage(source fs.FS, root, destination string) error {
	return fs.WalkDir(source, root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == root {
			return nil
		}
		relative, err := filepath.Rel(filepath.FromSlash(root), filepath.FromSlash(path))
		if err != nil {
			return err
		}
		target, err := safeJoin(destination, relative)
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		data, err := fs.ReadFile(source, path)
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o644)
	})
}

// pruneSupersededVersions is best effort: a leftover directory wastes space but
// never changes which version runs, so a failure here must not fail startup.
func pruneSupersededVersions(versionsRoot, keep string) {
	entries, err := os.ReadDir(versionsRoot)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if entry.Name() == keep {
			continue
		}
		_ = os.RemoveAll(filepath.Join(versionsRoot, entry.Name()))
	}
}
