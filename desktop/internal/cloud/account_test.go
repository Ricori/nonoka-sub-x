package cloud

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAccountIsHandedToTheEngineWhenItChanges(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/session" {
			http.NotFound(writer, request)
			return
		}
		_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":3,"running":0,"videos":[]}`))
	}))
	defer server.Close()

	service, err := New(t.TempDir(), fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	type handed struct{ backend, key string }
	var calls []handed
	failing := false
	SetAccountPublisher(service, func(_ context.Context, backend, key string) error {
		calls = append(calls, handed{backend, key})
		if failing {
			return errors.New("sidecar down")
		}
		return nil
	})

	if _, err := service.Login(server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 1 || calls[0] != (handed{server.URL, "login-key"}) {
		t.Fatalf("after login = %#v", calls)
	}
	// Nothing changed, so a refresh hands nothing over again.
	if _, err := service.RefreshSession(); err != nil || len(calls) != 1 {
		t.Fatalf("after refresh = %#v, %v", calls, err)
	}

	// A sign-out the engine missed is made up for by the next refresh.
	failing = true
	if err := service.Logout(); err != nil {
		t.Fatalf("logout must not depend on the engine: %v", err)
	}
	failing = false
	if _, err := service.RefreshSession(); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 3 || calls[1] != (handed{}) || calls[2] != (handed{}) {
		t.Fatalf("after logout = %#v", calls)
	}
	if _, err := service.RefreshSession(); err != nil || len(calls) != 3 {
		t.Fatalf("after settled refresh = %#v, %v", calls, err)
	}
}
