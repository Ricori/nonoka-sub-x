package cloud

import (
	"context"
	"log"
	"time"
)

// AccountPublisher hands the signed-in account to the local engine: the
// backend and key, or two empty strings once signed out.
//
// The backend serves a signed-in key a free model at /v1/llm, and the engine
// offers it as one more provider ("Nonoka Cloud"). The engine reads every
// credential from its own settings, so it has to be told when the account
// changes rather than asking this package for it on each call.
type AccountPublisher func(ctx context.Context, backend, key string) error

const accountPublishTimeout = 10 * time.Second

// SetAccountPublisher wires the hand-over. A package function rather than a
// method so it stays out of the renderer's bindings.
func SetAccountPublisher(s *Service, publish AccountPublisher) {
	s.publishMu.Lock()
	defer s.publishMu.Unlock()
	s.publishAccount = publish
	s.publishedValid = false
}

// publishAccountState hands the current account over unless the last
// successful hand-over already carried it. Called on login, logout and every
// session refresh: the sidecar may be down at the moment the account changes,
// and the next refresh is what makes up for it. Failure is logged, never
// returned -- signing in must not depend on the local engine being up.
func (s *Service) publishAccountState() {
	s.publishMu.Lock()
	defer s.publishMu.Unlock()
	if s.publishAccount == nil {
		return
	}
	s.mu.RLock()
	current := s.session
	s.mu.RUnlock()
	if s.publishedValid && s.published == current {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), accountPublishTimeout)
	defer cancel()
	if err := s.publishAccount(ctx, current.Backend, current.Key); err != nil {
		log.Printf("cloud: cannot hand the account to the local engine: %v", err)
		return
	}
	s.published = current
	s.publishedValid = true
}
