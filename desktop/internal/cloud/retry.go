package cloud

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// defaultFetchBackoff is how long a subtitle read waits before each retry.
// The cross-border routes users in mainland China reach the backend over drop
// or stall for seconds at a time at peak hours, and one bad moment used to fail
// a whole retrieval that is a chain of three to six sequential requests. The
// waits are long enough to step over a short outage and short enough that a
// retrieval the user is watching does not look hung.
var defaultFetchBackoff = []time.Duration{2 * time.Second, 5 * time.Second, 15 * time.Second}

// transientError marks a failure that says nothing about the request itself:
// the connection broke while a response was being read.
type transientError struct{ err error }

func (e *transientError) Error() string { return e.err.Error() }
func (e *transientError) Unwrap() error { return e.err }

// downloadStatusError is an artifact download the backend answered with a
// non-2xx status. Kept apart from httpStatusError on purpose: the relay reads
// an httpStatusError 404 as "the job is gone", which a missing artifact file
// is not.
type downloadStatusError struct{ Status int }

func (e *downloadStatusError) Error() string {
	return fmt.Sprintf("download cloud artifact: HTTP %d", e.Status)
}

// retryableStatus is a response a later identical request may well not get:
// the gateway or the Worker failing, or the backend asking to slow down.
// Every 4xx but these is an answer about the request and stays an answer.
func retryableStatus(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests || status >= 500
}

// isTransient reports whether an idempotent read that failed with err is worth
// sending again. Only failures of the network or the gateway qualify; a
// refusal, a digest mismatch or a validation error would fail the same way.
func isTransient(err error) bool {
	if err == nil {
		return false
	}
	var status *httpStatusError
	if errors.As(err, &status) {
		return retryableStatus(status.Status)
	}
	var download *downloadStatusError
	if errors.As(err, &download) {
		return retryableStatus(download.Status)
	}
	var marked *transientError
	if errors.As(err, &marked) {
		return true
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true
	}
	var network net.Error
	return errors.As(err, &network)
}

// retryRead runs an idempotent read, sending it again after each transient
// failure with the service's backoff, and returns the last error once the
// waits are spent. A cancelled ctx stops the waiting and returns at once.
func (s *Service) retryRead(ctx context.Context, read func() error) error {
	backoff := s.fetchBackoff
	if backoff == nil {
		backoff = defaultFetchBackoff
	}
	for attempt := 0; ; attempt++ {
		err := read()
		if err == nil || attempt >= len(backoff) || !isTransient(err) || ctx.Err() != nil {
			return err
		}
		timer := time.NewTimer(backoff[attempt])
		select {
		case <-ctx.Done():
			timer.Stop()
			return err
		case <-timer.C:
		}
	}
}

// fetchJSON is authenticatedDo for a GET, retried over transient failures.
func (s *Service) fetchJSON(ctx context.Context, path string, result any) error {
	return s.retryRead(ctx, func() error {
		return s.authenticatedDo(ctx, http.MethodGet, path, nil, result)
	})
}

// fetchText is authenticatedText retried over transient failures.
func (s *Service) fetchText(ctx context.Context, endpoint string) ([]byte, error) {
	var content []byte
	err := s.retryRead(ctx, func() error {
		var err error
		content, err = s.authenticatedText(ctx, endpoint)
		return err
	})
	return content, err
}
