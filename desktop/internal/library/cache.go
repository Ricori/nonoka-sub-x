package library

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	defaultCacheLimitGB = 20.0
	bytesPerGB          = int64(1024 * 1024 * 1024)
)

type CacheConfig struct {
	Schema  int     `json:"schema"`
	LimitGB float64 `json:"limitGB"`
}

type CacheStatus struct {
	Directory  string  `json:"directory"`
	LimitGB    float64 `json:"limitGB"`
	LimitBytes int64   `json:"limitBytes"`
	Bytes      int64   `json:"bytes"`
	Files      int     `json:"files"`
}

type cacheCandidate struct {
	path       string
	size       int64
	lastAccess int64
	active     bool
}

func defaultCacheConfig() CacheConfig {
	return CacheConfig{Schema: 1, LimitGB: defaultCacheLimitGB}
}

func (s *Service) CacheStatus() CacheStatus {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	return s.cacheStatusLocked()
}

func (s *Service) SetCacheLimitGB(limit float64) (CacheStatus, error) {
	if math.IsNaN(limit) || math.IsInf(limit, 0) || limit < 1 || limit > 1024 {
		return s.CacheStatus(), errors.New("cache limit must be between 1 and 1024 GB")
	}
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	previous := s.cacheConfig
	s.cacheConfig = CacheConfig{Schema: 1, LimitGB: limit}
	if err := s.writeCacheConfigLocked(); err != nil {
		s.cacheConfig = previous
		return s.cacheStatusLocked(), err
	}
	if err := s.convergeCacheLocked(); err != nil {
		return s.cacheStatusLocked(), err
	}
	return s.cacheStatusLocked(), nil
}

// CacheMedia creates a disposable working copy for task execution. Importing a
// video remains fast and does not duplicate it until the media is actually used.
func (s *Service) CacheMedia(id string) (string, error) {
	entry, err := s.entryByID(id)
	if err != nil {
		return "", err
	}
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	if cached := s.cachedPathLocked(id); cached != "" {
		s.touchEntry(id)
		return cached, nil
	}
	if !fileExists(entry.SourcePath) {
		return "", errors.New("local media is missing; relink it first")
	}
	extension := strings.ToLower(filepath.Ext(entry.SourcePath))
	if _, ok := supportedExtensions[extension]; !ok {
		return "", errors.New("unsupported media format")
	}
	destination := filepath.Join(s.cacheDirectory(), id+extension)
	if samePath(entry.SourcePath, destination) {
		return destination, nil
	}
	part := destination + ".part"
	_ = os.Remove(part)
	input, err := os.Open(entry.SourcePath)
	if err != nil {
		return "", err
	}
	output, err := os.OpenFile(part, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		_ = input.Close()
		return "", err
	}
	_, copyErr := io.Copy(output, input)
	inputErr := input.Close()
	closeErr := output.Close()
	if copyErr != nil || inputErr != nil || closeErr != nil {
		_ = os.Remove(part)
		return "", errors.Join(copyErr, inputErr, closeErr)
	}
	if err := os.Rename(part, destination); err != nil {
		_ = os.Remove(part)
		return "", err
	}
	s.touchEntry(id)
	if err := s.convergeCacheLocked(); err != nil {
		return destination, err
	}
	if !fileExists(destination) {
		return entry.SourcePath, nil
	}
	return destination, nil
}

func (s *Service) ClearVideoCache() (CacheStatus, error) {
	s.cacheMu.Lock()
	s.mu.RLock()
	active := s.activeMedia
	s.mu.RUnlock()
	entries, err := os.ReadDir(s.cacheDirectory())
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return s.cacheStatusLocked(), err
	}
	var removeErr error
	for _, entry := range entries {
		if entry.IsDir() || (active != "" && cacheFileID(entry.Name()) == active) {
			continue
		}
		if isCacheFile(entry.Name()) || strings.HasSuffix(strings.ToLower(entry.Name()), ".part") {
			removeErr = errors.Join(removeErr, os.Remove(filepath.Join(s.cacheDirectory(), entry.Name())))
		}
	}
	for _, path := range s.proxyFiles() {
		id := proxyFileID(filepath.Base(path))
		if active != "" && id == active {
			continue
		}
		s.cancelProxy(id)
		removeErr = errors.Join(removeErr, os.Remove(path))
	}
	status := s.cacheStatusLocked()
	s.cacheMu.Unlock()
	s.emitChanged()
	return status, removeErr
}

func (s *Service) SetActiveMedia(id string) error {
	if id != "" && !validID(id) {
		return errors.New("invalid media id")
	}
	s.mu.Lock()
	s.activeMedia = id
	s.mu.Unlock()
	return nil
}

func (s *Service) removeCachedMedia(id string) error {
	if !validID(id) {
		return errors.New("invalid media id")
	}
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	entries, err := os.ReadDir(s.cacheDirectory())
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	var result error
	for _, entry := range entries {
		if !entry.IsDir() && cacheFileID(entry.Name()) == id {
			result = errors.Join(result, os.Remove(filepath.Join(s.cacheDirectory(), entry.Name())))
		}
	}
	return errors.Join(result, s.dropPreviewDerivatives(id))
}

// dropPreviewDerivatives removes what the editor's preview derived from the
// source: its proxy (cancelling a build in progress) and its filmstrips.
func (s *Service) dropPreviewDerivatives(id string) error {
	s.cancelProxy(id)
	var result error
	if err := os.Remove(s.proxyPath(id)); err != nil && !errors.Is(err, os.ErrNotExist) {
		result = err
	}
	if err := os.RemoveAll(filepath.Join(s.root, "temporary", "filmstrip", id)); err != nil {
		result = errors.Join(result, err)
	}
	return result
}

// cachedPath reads the cache directory without cacheMu. Every cache file is
// installed by rename, so a reader sees either the old file or the new one,
// and a lookup must never wait on a cache operation: the media server calls
// this for each range request the player makes, and CacheMedia can hold the
// lock for minutes while it copies a large source.
func (s *Service) cachedPath(id string) string {
	return s.cachedPathLocked(id)
}

func (s *Service) cachedPathLocked(id string) string {
	if !validID(id) {
		return ""
	}
	entries, err := os.ReadDir(s.cacheDirectory())
	if err != nil {
		return ""
	}
	for _, entry := range entries {
		if !entry.IsDir() && cacheFileID(entry.Name()) == id && isCacheFile(entry.Name()) {
			return filepath.Join(s.cacheDirectory(), entry.Name())
		}
	}
	return ""
}

func (s *Service) entryByID(id string) (Entry, error) {
	if !validID(id) {
		return Entry{}, errors.New("invalid media id")
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, entry := range s.entries {
		if entry.ID == id {
			return entry, nil
		}
	}
	return Entry{}, errors.New("media entry not found")
}

func (s *Service) touchEntry(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for index := range s.entries {
		if s.entries[index].ID == id {
			s.entries[index].LastAccess = time.Now().UnixMilli()
			_ = s.saveLocked()
			return
		}
	}
}

func (s *Service) cacheStatusLocked() CacheStatus {
	status := CacheStatus{
		Directory:  s.cacheDirectory(),
		LimitGB:    s.cacheConfig.LimitGB,
		LimitBytes: cacheLimitBytes(s.cacheConfig.LimitGB),
	}
	entries, _ := os.ReadDir(s.cacheDirectory())
	for _, entry := range entries {
		if entry.IsDir() || !isCacheFile(entry.Name()) {
			continue
		}
		if info, err := entry.Info(); err == nil {
			status.Bytes += info.Size()
			status.Files++
		}
	}
	for _, path := range s.proxyFiles() {
		if info, err := os.Stat(path); err == nil {
			status.Bytes += info.Size()
			status.Files++
		}
	}
	return status
}

func (s *Service) loadCacheConfig() error {
	data, err := os.ReadFile(s.cachePath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var config CacheConfig
	if err := json.Unmarshal(data, &config); err != nil {
		return fmt.Errorf("read cache settings: %w", err)
	}
	if config.LimitGB < 1 || config.LimitGB > 1024 || math.IsNaN(config.LimitGB) || math.IsInf(config.LimitGB, 0) {
		config.LimitGB = defaultCacheLimitGB
	}
	config.Schema = 1
	s.cacheConfig = config
	return nil
}

func (s *Service) writeCacheConfigLocked() error {
	data, err := json.MarshalIndent(s.cacheConfig, "", "  ")
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(s.root, ".cache-*.tmp")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o600); err == nil {
		_, err = temporary.Write(append(data, '\n'))
	}
	closeErr := temporary.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, s.cachePath)
	}
	return err
}

func (s *Service) convergeCache() error {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	return s.convergeCacheLocked()
}

func (s *Service) convergeCacheLocked() error {
	entries, err := os.ReadDir(s.cacheDirectory())
	if err != nil {
		return err
	}
	s.mu.RLock()
	lastAccess := make(map[string]int64, len(s.entries))
	for _, entry := range s.entries {
		lastAccess[entry.ID] = entry.LastAccess
	}
	active := s.activeMedia
	s.mu.RUnlock()
	var candidates []cacheCandidate
	var total int64
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		path := filepath.Join(s.cacheDirectory(), entry.Name())
		if strings.HasSuffix(strings.ToLower(entry.Name()), ".part") {
			_ = os.Remove(path)
			continue
		}
		if !isCacheFile(entry.Name()) {
			continue
		}
		info, statErr := entry.Info()
		if statErr != nil {
			continue
		}
		id := cacheFileID(entry.Name())
		access := lastAccess[id]
		if access == 0 {
			access = info.ModTime().UnixMilli()
		}
		candidate := cacheCandidate{path: path, size: info.Size(), lastAccess: access, active: id == active}
		candidates = append(candidates, candidate)
		total += candidate.size
	}
	// Proxies share the budget and the media's last access: an idle video's
	// proxy goes as readily as its cache copy.
	for _, path := range s.proxyFiles() {
		info, statErr := os.Stat(path)
		if statErr != nil {
			continue
		}
		id := proxyFileID(filepath.Base(path))
		access := lastAccess[id]
		if access == 0 {
			access = info.ModTime().UnixMilli()
		}
		candidates = append(candidates, cacheCandidate{path: path, size: info.Size(), lastAccess: access, active: id == active})
		total += info.Size()
	}
	limit := cacheLimitBytes(s.cacheConfig.LimitGB)
	sort.SliceStable(candidates, func(i, j int) bool { return candidates[i].lastAccess < candidates[j].lastAccess })
	for _, candidate := range candidates {
		if total <= limit {
			break
		}
		if candidate.active {
			continue
		}
		if err := os.Remove(candidate.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		total -= candidate.size
	}
	return nil
}

func cacheLimitBytes(limitGB float64) int64 {
	return int64(limitGB * float64(bytesPerGB))
}

func cacheFileID(name string) string {
	base := strings.TrimSuffix(name, ".part")
	return strings.TrimSuffix(base, filepath.Ext(base))
}

func isCacheFile(name string) bool {
	if strings.HasSuffix(strings.ToLower(name), ".part") {
		return false
	}
	_, ok := supportedExtensions[strings.ToLower(filepath.Ext(name))]
	return ok
}

func samePath(left, right string) bool {
	left, leftErr := filepath.Abs(left)
	right, rightErr := filepath.Abs(right)
	if leftErr != nil || rightErr != nil {
		return false
	}
	return filepath.Clean(left) == filepath.Clean(right)
}

// proxyFiles lists finished proxies; a build's .part file is not one yet.
func (s *Service) proxyFiles() []string {
	entries, err := os.ReadDir(s.proxyDirectory())
	if err != nil {
		return nil
	}
	var paths []string
	for _, entry := range entries {
		if !entry.IsDir() && strings.EqualFold(filepath.Ext(entry.Name()), ".mp4") && validID(proxyFileID(entry.Name())) {
			paths = append(paths, filepath.Join(s.proxyDirectory(), entry.Name()))
		}
	}
	return paths
}

func proxyFileID(name string) string { return strings.TrimSuffix(name, filepath.Ext(name)) }
