package plugins

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
)

const (
	stateFileName = "state.json"
	maxStateBytes = 10 << 20
)

// StateGet reads one value from the calling plugin's private persistent state.
// A nil result distinguishes a missing key from a stored empty string.
func (s *Service) StateGet(pluginID, key string) (*string, error) {
	if err := s.requirePermission(pluginID, "state.persist"); err != nil {
		return nil, err
	}
	if err := validateStateKey(key); err != nil {
		return nil, err
	}

	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	state, err := s.readPluginState(pluginID)
	if err != nil {
		return nil, err
	}
	value, found := state[key]
	if !found {
		return nil, nil
	}
	return &value, nil
}

// StateSet stores one value, or deletes the key when value is nil.
func (s *Service) StateSet(pluginID, key string, value *string) error {
	if err := s.requirePermission(pluginID, "state.persist"); err != nil {
		return err
	}
	if err := validateStateKey(key); err != nil {
		return err
	}

	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	state, err := s.readPluginState(pluginID)
	if err != nil {
		return err
	}
	if value == nil {
		delete(state, key)
	} else {
		state[key] = *value
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	if len(data)+1 > maxStateBytes {
		return errors.New("plugin state exceeds 10 MiB")
	}
	path, err := s.pluginDataPath(pluginID, stateFileName)
	if err != nil {
		return err
	}
	return writeJSONAtomic(path, state)
}

// StateKeys lists the calling plugin's keys in stable order.
func (s *Service) StateKeys(pluginID string) ([]string, error) {
	if err := s.requirePermission(pluginID, "state.persist"); err != nil {
		return nil, err
	}

	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	state, err := s.readPluginState(pluginID)
	if err != nil {
		return nil, err
	}
	keys := make([]string, 0, len(state))
	for key := range state {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys, nil
}

func validateStateKey(key string) error {
	if !pluginIDPattern.MatchString(key) {
		return errors.New("state key must contain lowercase letters, digits, dots, dashes, or underscores")
	}
	return nil
}

func (s *Service) readPluginState(pluginID string) (map[string]string, error) {
	path, err := s.pluginDataPath(pluginID, stateFileName)
	if err != nil {
		return nil, err
	}
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]string{}, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxStateBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxStateBytes {
		return nil, errors.New("plugin state exceeds 10 MiB")
	}
	state := map[string]string{}
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	if err := decoder.Decode(&state); err != nil {
		return nil, fmt.Errorf("parse plugin state: %w", err)
	}
	if state == nil {
		return nil, errors.New("parse plugin state: expected a JSON object")
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return nil, errors.New("parse plugin state: trailing data is not allowed")
	}
	for key := range state {
		if err := validateStateKey(key); err != nil {
			return nil, fmt.Errorf("parse plugin state: invalid key %q", key)
		}
	}
	return state, nil
}
