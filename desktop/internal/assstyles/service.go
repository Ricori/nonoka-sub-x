// Package assstyles owns the machine-local *default* ASS style sheet -- the one
// a newly opened document is seeded from. The sheet a video actually renders
// with travels inside its own document (the `styles` field); this file only
// answers "what should a new video start out looking like". It is stored as a
// plain .ass fragment so the same file can be opened in Aegisub or hand-edited.
package assstyles

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"unicode/utf8"
)

// A style sheet is a Format line plus a handful of Style lines; anything past
// this is not one, and refusing it keeps a stray paste out of the data dir.
const maxSheetBytes = 1 << 20

type Service struct {
	mu   sync.RWMutex
	path string
	text string
}

func New(dataDirectory string) (*Service, error) {
	root, err := filepath.Abs(dataDirectory)
	if err != nil {
		return nil, err
	}
	service := &Service{path: filepath.Join(root, "styles.ass")}
	if err := service.load(); err != nil {
		return nil, err
	}
	return service, nil
}

// Get returns the stored sheet, or "" when nothing has been saved yet. The
// empty case is deliberately not filled in here: the built-in styles live in
// the frontend, and duplicating them in Go would give them two sources of truth.
func (s *Service) Get() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.text
}

func (s *Service) Save(text string) (string, error) {
	if len(text) > maxSheetBytes {
		return s.Get(), errors.New("ass style sheet is too large")
	}
	if !utf8.ValidString(text) {
		return s.Get(), errors.New("ass style sheet must be valid UTF-8")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := writeAtomic(s.path, text); err != nil {
		return s.text, err
	}
	s.text = text
	return s.text, nil
}

func (s *Service) load() error {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if len(data) > maxSheetBytes || !utf8.Valid(data) {
		// Unreadable on disk is reported as "nothing saved" rather than as a
		// startup failure: a corrupt sheet must not keep the editor shut.
		return nil
	}
	s.text = string(data)
	return nil
}

func writeAtomic(path string, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".styles-*.tmp")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o600); err == nil {
		_, err = temporary.WriteString(text)
	}
	closeErr := temporary.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(name, path)
	}
	return err
}
