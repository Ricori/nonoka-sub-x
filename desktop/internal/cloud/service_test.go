package cloud

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/library"
)

type fakeProvider struct {
	manifest artifactManifest
}

type fakeMedia struct {
	path      string
	thumbnail string
	// Covers handed to the library by an adoption, keyed by local media id. A
	// map so the value receiver the other methods use can still record.
	covers map[string]string
}

type fakeCatalog struct {
	fakeMedia
	entries []library.Entry
}

func (f fakeCatalog) List() []library.Entry {
	return append([]library.Entry(nil), f.entries...)
}

func (f fakeMedia) ResolveMedia(id string) (string, string, string, float64, error) {
	if id != "loc_0123456789ab" {
		return "", "", "", 0, os.ErrNotExist
	}
	return f.path, "demo.mp4", "fingerprint", 60, nil
}

func (f fakeMedia) AddPlaceholder(title, fingerprint string, duration float64) (library.Entry, error) {
	return library.Entry{ID: "loc_cccccccccccc", Title: title, Fingerprint: fingerprint, Duration: duration}, nil
}

func (f fakeMedia) ThumbnailDataURL(id string) (string, error) {
	if id != "loc_0123456789ab" || f.thumbnail == "" {
		return "", os.ErrNotExist
	}
	return "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString([]byte(f.thumbnail)), nil
}

func (f fakeMedia) EnsureThumbnail(id, dataURL string) error {
	if f.covers == nil {
		return os.ErrInvalid
	}
	// Same rule as the real library: an entry that already has a cover keeps it.
	if _, exists := f.covers[id]; !exists {
		f.covers[id] = dataURL
	}
	return nil
}

type projectingProvider struct {
	projected map[string]any
}

func (p *projectingProvider) DoJSON(_ context.Context, method, endpoint string, body any, result any) error {
	if method != http.MethodPost || endpoint != "/v1/documents/project" {
		return os.ErrInvalid
	}
	p.projected = body.(map[string]any)
	encoded, _ := json.Marshal(map[string]any{"schema": 1, "video_id": "loc_0123456789ab"})
	return json.Unmarshal(encoded, result)
}

func (f fakeProvider) DoJSON(_ context.Context, method, endpoint string, _ any, result any) error {
	if method != http.MethodGet || endpoint != "/v1/tasks/0123456789abcdef0123456789abcdef/artifacts" {
		panic("unexpected provider call")
	}
	encoded, _ := json.Marshal(f.manifest)
	return json.Unmarshal(encoded, result)
}

func TestBackendValidation(t *testing.T) {
	for _, value := range []string{"", "http://example.com", "https://user@example.com", "https://example.com?q=1"} {
		if _, err := validBackend(value); err == nil {
			t.Fatalf("expected %q to fail", value)
		}
	}
	if got, err := validBackend("https://example.com/api/"); err != nil || got != "https://example.com/api" {
		t.Fatalf("backend = %q, %v", got, err)
	}
}

func TestArtifactVerificationRepairsOnlyDigestVerifiedNewlines(t *testing.T) {
	stored := []byte("1\r\n00:00:00,000 --> 00:00:01,000\r\nsubtitle\r\n")
	digest := sha256.Sum256(stored)
	expected := hex.EncodeToString(digest[:])
	downloaded := bytes.ReplaceAll(stored, []byte("\r\n"), []byte("\n"))

	verified, actual, ok := verifyArtifactContent(downloaded, expected)
	if !ok || actual != expected || !bytes.Equal(verified, stored) {
		t.Fatalf("CRLF artifact was not restored: ok=%v actual=%q content=%q", ok, actual, verified)
	}
	if _, _, ok := verifyArtifactContent([]byte("tampered\n"), expected); ok {
		t.Fatal("non-newline artifact corruption passed verification")
	}
}

func TestLoginLibraryAndArtifactSync(t *testing.T) {
	root := t.TempDir()
	taskID := "0123456789abcdef0123456789abcdef"
	taskRoot := filepath.Join(root, "tasks", taskID, "workspace")
	if err := os.MkdirAll(taskRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	artifactPath := filepath.Join(taskRoot, "final.srt")
	if err := os.WriteFile(artifactPath, []byte("1\n00:00:00,000 --> 00:00:01,000\n字幕\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer login-key" {
			http.Error(writer, `{"detail":"key 无效"}`, http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":3,"running":0}`))
		case "/v1/library":
			_, _ = writer.Write([]byte(`{"videos":[]}`))
		case "/v1/tasks":
			_, _ = writer.Write([]byte(`{"schema":1,"tasks":[{"snapshot":{"task_id":"vid_0123456789abcdef01234567","provider":"cloud","state":"completed"},"media_id":"","title":"demo"}]}`))
		case "/v1/library/sync":
			var body syncRequest
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil || body.Artifacts["final_srt"] == "" {
				http.Error(writer, `{"detail":"missing artifact"}`, http.StatusBadRequest)
				return
			}
			_, _ = writer.Write([]byte(`{"id":"vid_1","title":"demo","fingerprint":"fp","duration":1,"status":"completed","source":"local_sync","createdAt":"now","updatedAt":"now"}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	provider := fakeProvider{manifest: artifactManifest{
		EngineCommit: "commit",
		Artifacts: map[string]struct {
			URI string `json:"uri"`
		}{
			"final_srt": {URI: localFileURI(artifactPath)},
		},
	}}
	service, err := New(root, provider)
	if err != nil {
		t.Fatal(err)
	}
	if session, err := service.Login(server.URL, "login-key"); err != nil || !session.Authenticated {
		t.Fatalf("login = %#v, %v", session, err)
	}
	if session, err := service.RefreshSession(); err != nil || session.Remaining == nil || *session.Remaining != 3 {
		t.Fatalf("refresh session = %#v, %v", session, err)
	}
	if library, err := service.Library(); err != nil || len(library) != 0 {
		t.Fatalf("library = %#v, %v", library, err)
	}
	if tasks, err := service.ListTasks(); err != nil || len(tasks["tasks"].([]any)) != 1 {
		t.Fatalf("tasks = %#v, %v", tasks, err)
	}
	entry, err := service.SyncLocalTask(taskID, "loc_1", "fp", "demo", 1)
	if err != nil || entry.ID != "vid_1" {
		t.Fatalf("sync = %#v, %v", entry, err)
	}
	if err := service.Logout(); err != nil || service.Session().Authenticated {
		t.Fatalf("logout = %v, %#v", err, service.Session())
	}
}

func TestAdminKeyManagement(t *testing.T) {
	root := t.TempDir()
	identifier := "custom-secret"
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer admin-token" {
			http.Error(writer, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"admin":true,"name":"管理员","remaining":null,"running":0}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/library":
			_, _ = writer.Write([]byte(`{"videos":[]}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/admin/keys":
			_, _ = writer.Write([]byte(`{"keys":[{"id":"` + identifier + `","name":"剪辑组","key":"demo-secret","keyPreview":"demo-secret","remaining":3,"submitted":2,"running":1}]}`))
		case request.Method == http.MethodPost && request.URL.Path == "/v1/admin/keys":
			var body map[string]any
			_ = json.NewDecoder(request.Body).Decode(&body)
			if body["name"] != "字幕组" || body["key"] != "custom-secret" || body["remaining"] != float64(5) {
				http.Error(writer, `{"detail":"invalid body"}`, http.StatusBadRequest)
				return
			}
			_, _ = writer.Write([]byte(`{"id":"` + identifier + `","name":"字幕组","keyPreview":"custom-secret","remaining":5,"key":"custom-secret","generated":false}`))
		case request.Method == http.MethodPut && request.URL.Path == "/v1/admin/keys/"+identifier:
			_, _ = writer.Write([]byte(`{"id":"` + identifier + `","name":"终审组","key":"custom-secret","keyPreview":"custom-secret","remaining":8}`))
		case request.Method == http.MethodDelete && request.URL.Path == "/v1/admin/keys/"+identifier:
			_, _ = writer.Write([]byte(`{"removed":"` + identifier + `"}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	service, err := New(root, fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	session, err := service.Login(server.URL, "admin-token")
	if err != nil || !session.Admin || session.Name != "管理员" || session.Remaining != nil {
		t.Fatalf("admin login = %#v, %v", session, err)
	}
	keys, err := service.AdminKeys()
	if err != nil || len(keys) != 1 || keys[0].Name != "剪辑组" || keys[0].Key != "demo-secret" || keys[0].Running != 1 {
		t.Fatalf("admin keys = %#v, %v", keys, err)
	}
	issued, err := service.CreateAdminKey("字幕组", "custom-secret", 5)
	if err != nil || issued.ID != "custom-secret" || issued.Key != "custom-secret" || issued.Name != "字幕组" {
		t.Fatalf("issued key = %#v, %v", issued, err)
	}
	updated, err := service.UpdateAdminKey(identifier, "终审组", 8)
	if err != nil || updated.Remaining != 8 {
		t.Fatalf("updated key = %#v, %v", updated, err)
	}
	if err := service.DeleteAdminKey(identifier); err != nil {
		t.Fatalf("delete key: %v", err)
	}
}

func TestDeleteLibraryEntry(t *testing.T) {
	root := t.TempDir()
	identifier := "vid_0123456789abcdef01234567"
	taskID := "0123456789abcdef0123456789abcdef"
	taskRoot := filepath.Join(root, "tasks", taskID, "workspace")
	if err := os.MkdirAll(taskRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	artifactPath := filepath.Join(taskRoot, "final.srt")
	if err := os.WriteFile(artifactPath, []byte("deleted cloud subtitles must stay deleted"), 0o600); err != nil {
		t.Fatal(err)
	}
	provider := fakeProvider{manifest: artifactManifest{
		TaskID: taskID,
		Artifacts: map[string]struct {
			URI string `json:"uri"`
		}{"final_srt": {URI: localFileURI(artifactPath)}},
	}}
	deleted := false
	syncCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case request.Method == http.MethodGet && request.URL.Path == "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":3,"running":0}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/library":
			if deleted {
				_, _ = writer.Write([]byte(`{"videos":[]}`))
			} else {
				_ = json.NewEncoder(writer).Encode(map[string]any{"videos": []Entry{{ID: identifier, Title: "demo", Fingerprint: "fingerprint", Status: "completed", Source: "local_sync"}}})
			}
		case request.Method == http.MethodDelete && request.URL.Path == "/v1/library/"+identifier:
			deleted = true
			_, _ = writer.Write([]byte(`{"removed":"` + identifier + `"}`))
		case request.Method == http.MethodPost && request.URL.Path == "/v1/library/sync":
			syncCalls++
			_, _ = writer.Write([]byte(`{"id":"vid_111111111111111111111111","fingerprint":"fingerprint","status":"completed","source":"local_sync"}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	service, err := New(root, provider)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Login(server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	if err := service.DeleteLibraryEntry(identifier); err != nil {
		t.Fatalf("delete library entry: %v", err)
	}
	entry, err := service.SyncLocalTask(taskID, "loc_0123456789ab", "fingerprint", "demo", 1)
	if err != nil || entry.Status != "suppressed" || syncCalls != 0 {
		t.Fatalf("deleted subtitles were re-synced: entry=%#v calls=%d err=%v", entry, syncCalls, err)
	}
	reloaded, err := New(root, provider)
	if err != nil {
		t.Fatal(err)
	}
	entry, err = reloaded.SyncLocalTask(taskID, "loc_0123456789ab", "fingerprint", "demo", 1)
	if err != nil || entry.Status != "suppressed" || syncCalls != 0 {
		t.Fatalf("sync suppression was not persisted: entry=%#v calls=%d err=%v", entry, syncCalls, err)
	}
	future := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(artifactPath, future, future); err != nil {
		t.Fatal(err)
	}
	entry, err = reloaded.SyncLocalTask(taskID, "loc_0123456789ab", "fingerprint", "demo", 1)
	if err != nil || entry.Status != "completed" || syncCalls != 1 {
		t.Fatalf("new local result did not clear suppression: entry=%#v calls=%d err=%v", entry, syncCalls, err)
	}
	if err := service.DeleteLibraryEntry("invalid"); err == nil {
		t.Fatal("expected invalid library entry id to fail")
	}
}

func TestLoginMergesExistingLocalDocumentsAndSkipsRemoteFingerprints(t *testing.T) {
	root := t.TempDir()
	taskID := "0123456789abcdef0123456789abcdef"
	localID := "loc_0123456789ab"
	artifactRoot := filepath.Join(root, "tasks", taskID, "workspace")
	if err := os.MkdirAll(artifactRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	artifactPath := filepath.Join(artifactRoot, "final.srt")
	if err := os.WriteFile(artifactPath, []byte("1\n00:00:00,000 --> 00:00:01,000\n字幕\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	manifest := artifactManifest{
		TaskID: taskID, EngineCommit: "commit",
		Artifacts: map[string]struct {
			URI string `json:"uri"`
		}{"final_srt": {URI: localFileURI(artifactPath)}},
	}
	documentRoot := filepath.Join(root, "documents", localID)
	if err := os.MkdirAll(documentRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	encodedManifest, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(documentRoot, "artifacts.json"), encodedManifest, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(documentRoot, "document.json"), []byte(`{"schema":1}`), 0o600); err != nil {
		t.Fatal(err)
	}

	remote := []Entry{}
	syncCalls := 0
	cover := "ÿØÿcover"
	var syncedCover []byte
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer login-key" {
			http.Error(writer, `{"detail":"key 无效"}`, http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":3,"running":0}`))
		case "/v1/library":
			_ = json.NewEncoder(writer).Encode(map[string]any{"videos": remote})
		case "/v1/library/sync":
			syncCalls++
			var body syncRequest
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil || body.Fingerprint != "fingerprint" || body.Artifacts["final_srt"] == "" {
				http.Error(writer, `{"detail":"invalid sync"}`, http.StatusBadRequest)
				return
			}
			syncedCover = body.Thumbnail
			entry := Entry{ID: "vid_1", Title: body.Title, Fingerprint: body.Fingerprint, Duration: body.Duration, Status: "completed", Source: "local_sync", CreatedAt: "now", UpdatedAt: "now"}
			remote = append(remote, entry)
			_ = json.NewEncoder(writer).Encode(entry)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	catalog := fakeCatalog{
		fakeMedia: fakeMedia{path: artifactPath, thumbnail: cover},
		entries: []library.Entry{{
			ID: localID, Title: "demo.mp4", Fingerprint: "fingerprint", Duration: 60,
			DocumentAvailable: true, Available: true,
		}},
	}
	service, err := New(root, fakeProvider{}, catalog)
	if err != nil {
		t.Fatal(err)
	}
	session, err := service.Login(server.URL, "login-key")
	if err != nil || session.Synced != 1 || session.SyncFailed != 0 || session.CloudVideos != 1 || syncCalls != 1 {
		t.Fatalf("first merge = %#v, calls=%d, err=%v", session, syncCalls, err)
	}
	// The cover rides the sync. Nothing else uploads one for a locally
	// transcribed entry -- a local run never goes through /v1/uploads/init --
	// so without it the subtitles reach every other machine with no frame to
	// show and no video there to draw one from.
	if string(syncedCover) != cover {
		t.Fatalf("synced cover = %q", syncedCover)
	}
	session, err = service.RefreshSession()
	if err != nil || session.Synced != 0 || session.SyncSkipped != 1 || session.CloudVideos != 1 || syncCalls != 1 {
		t.Fatalf("second merge = %#v, calls=%d, err=%v", session, syncCalls, err)
	}
}

func TestRealModalLoginMergesAnIsolatedLocalLibrary(t *testing.T) {
	keyPath := os.Getenv("NONOKA_MODAL_TEST_KEY_FILE")
	if keyPath == "" {
		t.Skip("set NONOKA_MODAL_TEST_KEY_FILE to run the deployed Modal integration")
	}
	keyResponse, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatal(err)
	}
	var issued struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(keyResponse, &issued); err != nil || issued.Key == "" {
		t.Fatalf("invalid generated key file: %v", err)
	}

	root := t.TempDir()
	taskID := "0123456789abcdef0123456789abcdef"
	localID := "loc_0123456789ab"
	fingerprint := "modal-sync-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	artifactRoot := filepath.Join(root, "tasks", taskID, "workspace")
	if err := os.MkdirAll(artifactRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	artifactPath := filepath.Join(artifactRoot, "final.srt")
	if err := os.WriteFile(artifactPath, []byte("1\n00:00:00,000 --> 00:00:01,000\n真实同步验证\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	manifest := artifactManifest{
		TaskID: taskID, EngineCommit: "modal-integration",
		Artifacts: map[string]struct {
			URI string `json:"uri"`
		}{"final_srt": {URI: localFileURI(artifactPath)}},
	}
	documentRoot := filepath.Join(root, "documents", localID)
	if err := os.MkdirAll(documentRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	encodedManifest, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(documentRoot, "artifacts.json"), encodedManifest, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(documentRoot, "document.json"), []byte(`{"schema":1}`), 0o600); err != nil {
		t.Fatal(err)
	}
	encodedLibrary, _ := json.Marshal([]library.Entry{{
		ID: localID, SourcePath: artifactPath, Title: "Modal 同步验证", Fingerprint: fingerprint,
		Duration: 1, AddedAt: time.Now().UnixMilli(), Available: true,
	}})
	if err := os.WriteFile(filepath.Join(root, "library.json"), encodedLibrary, 0o600); err != nil {
		t.Fatal(err)
	}
	catalog, err := library.New(root)
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(root, fakeProvider{}, catalog)
	if err != nil {
		t.Fatal(err)
	}
	endpoint := os.Getenv("NONOKA_MODAL_ENDPOINT")
	if endpoint == "" {
		endpoint = "https://ricori--nonoka-x-cloud-api.modal.run"
	}
	session, err := service.Login(endpoint, issued.Key)
	if err != nil || session.Synced != 1 || session.SyncFailed != 0 {
		t.Fatalf("real login merge = %#v, %v", session, err)
	}
	remote, err := service.Library()
	if err != nil || len(remote) != 1 || remote[0].Fingerprint != fingerprint {
		t.Fatalf("real merged library = %#v, %v", remote, err)
	}
	refreshed, err := service.RefreshSession()
	if err != nil || refreshed.SyncSkipped != 1 || refreshed.Synced != 0 {
		t.Fatalf("real idempotent refresh = %#v, %v", refreshed, err)
	}
}

func TestArtifactPathCannotEscapeTaskRoot(t *testing.T) {
	service, err := New(t.TempDir(), fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.safeArtifactPath("file:///etc/passwd"); err == nil {
		t.Fatal("expected escaped artifact to fail")
	}
}

func TestArtifactPathRebasesMovedDataDirectory(t *testing.T) {
	root := t.TempDir()
	taskID := "0123456789abcdef0123456789abcdef"
	workspace := filepath.Join(root, "tasks", taskID, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	artifact := filepath.Join(workspace, "final.srt")
	if err := os.WriteFile(artifact, []byte("1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	service, err := New(root, fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	// The manifest still names the data directory the run wrote it under.
	previous := filepath.Join(filepath.Dir(root), "Finoka", "tasks", taskID, "workspace", "final.srt")
	resolved, err := service.safeArtifactPath(localFileURI(previous))
	if err != nil {
		t.Fatalf("stale artifact path = %v", err)
	}
	if resolved != artifact {
		t.Fatalf("rebased artifact = %q, want %q", resolved, artifact)
	}
	missing := filepath.Join(filepath.Dir(root), "Finoka", "tasks", taskID, "workspace", "absent.srt")
	if _, err := service.safeArtifactPath(localFileURI(missing)); err == nil {
		t.Fatal("expected a stale path with no current file to fail")
	}
	if err := os.WriteFile(filepath.Join(root, "secret.srt"), []byte("1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(filepath.Dir(root), "Finoka", "tasks", taskID, "..", "..", "secret.srt")
	if _, err := service.safeArtifactPath(localFileURI(outside)); err == nil {
		t.Fatal("expected a traversing stale path to fail")
	}
}

func TestWindowsFileURIPathDropsSlashBeforeDriveLetter(t *testing.T) {
	tests := map[string]string{
		"/C:/tasks/final.srt": "C:/tasks/final.srt",
		"/d:/tasks/final.srt": "d:/tasks/final.srt",
		"/tasks/final.srt":    "/tasks/final.srt",
		"/1:/tasks/final.srt": "/1:/tasks/final.srt",
	}
	for input, expected := range tests {
		if actual := windowsPathFromFileURI(input); actual != expected {
			t.Errorf("windowsPathFromFileURI(%q) = %q, want %q", input, actual, expected)
		}
	}
}

func TestStartTaskKeepsUploadUnlessBackendRefusedTask(t *testing.T) {
	const uploadID = "upl_0123456789abcdef0123456789abcdef"
	tests := []struct {
		name    string
		status  int
		aborted bool
	}{
		{name: "refused", status: http.StatusBadRequest, aborted: true},
		{name: "quota", status: http.StatusConflict, aborted: true},
		{name: "dispatch failed after reservation", status: http.StatusBadGateway, aborted: false},
		{name: "gateway timeout", status: http.StatusGatewayTimeout, aborted: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			root := t.TempDir()
			source := filepath.Join(root, "video.mp4")
			if err := os.WriteFile(source, []byte("video"), 0o600); err != nil {
				t.Fatal(err)
			}
			aborted := false
			var server *httptest.Server
			server = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				writer.Header().Set("Content-Type", "application/json")
				switch {
				case request.Method == http.MethodPut:
					writer.WriteHeader(http.StatusNoContent)
				case request.URL.Path == "/v1/session":
					_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":2,"running":0}`))
				case request.URL.Path == "/v1/uploads/init":
					_, _ = writer.Write([]byte(`{"objectId":"` + uploadID + `","uploadUrl":"` + server.URL + `/upload","contentType":"audio/mp4","thumbUploadUrl":"` + server.URL + `/upload-thumb"}`))
				case request.Method == http.MethodPost && request.URL.Path == "/v1/tasks":
					http.Error(writer, `{"detail":"fixture"}`, test.status)
				case request.Method == http.MethodDelete && request.URL.Path == "/v1/uploads/"+uploadID:
					aborted = true
					_, _ = writer.Write([]byte(`{"aborted":true}`))
				default:
					http.NotFound(writer, request)
				}
			}))
			defer server.Close()

			service, err := New(root, &projectingProvider{}, fakeMedia{path: source})
			if err != nil {
				t.Fatal(err)
			}
			service.extractor = func(_ context.Context, _, output string) error {
				return os.WriteFile(output, []byte("audio-fixture"), 0o600)
			}
			if _, err := service.Login(server.URL, "login-key"); err != nil {
				t.Fatal(err)
			}
			if _, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "raw-srt"}); err == nil {
				t.Fatal("StartTask succeeded against a failing backend")
			}
			if aborted != test.aborted {
				t.Fatalf("upload aborted = %v, want %v", aborted, test.aborted)
			}
		})
	}
}

func TestCloudTaskUploadsAudioPollsCancelsAndProjectsArtifacts(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "video.mp4")
	if err := os.WriteFile(source, []byte("video"), 0o600); err != nil {
		t.Fatal(err)
	}
	const taskID = "vid_0123456789abcdef01234567"
	const uploadID = "upl_0123456789abcdef0123456789abcdef"
	uploaded := false
	var cover []byte
	declaredThumbnail := false
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		if request.Method == http.MethodPut && request.URL.Path == "/upload" {
			payload, _ := io.ReadAll(request.Body)
			uploaded = string(payload) == "audio-fixture"
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.Method == http.MethodPut && request.URL.Path == "/upload-thumb" {
			cover, _ = io.ReadAll(request.Body)
			if request.Header.Get("Content-Type") != "image/jpeg" {
				http.Error(writer, `{"detail":"cover must be jpeg"}`, http.StatusBadRequest)
				return
			}
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.Header.Get("Authorization") != "Bearer login-key" {
			http.Error(writer, `{"detail":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		switch {
		case request.URL.Path == "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":2,"running":0}`))
		case request.URL.Path == "/v1/capabilities":
			_, _ = writer.Write([]byte(`{"provider":"cloud","adapter_schema":1,"artifact_schema":1}`))
		case request.URL.Path == "/v1/uploads/init":
			_, _ = writer.Write([]byte(`{"objectId":"` + uploadID + `","uploadUrl":"` + server.URL + `/upload","contentType":"audio/mp4","thumbUploadUrl":"` + server.URL + `/upload-thumb"}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/library/"+taskID+"/thumbnail":
			writer.Header().Set("Content-Type", "image/jpeg")
			_, _ = writer.Write(cover)
		case request.Method == http.MethodPost && request.URL.Path == "/v1/tasks":
			var body map[string]any
			_ = json.NewDecoder(request.Body).Decode(&body)
			source := body["source"].(map[string]any)
			correction := body["correction"].(map[string]any)
			if source["kind"] != "uploaded_audio" || source["object_id"] != uploadID || source["fingerprint"] != "fingerprint" {
				http.Error(writer, `{"detail":"invalid source"}`, http.StatusBadRequest)
				return
			}
			declaredThumbnail = source["thumbnail"] == true
			if correction["media"] != "text" || correction["retrieval"] != "none" {
				http.Error(writer, `{"detail":"cloud correction must be text-only without retrieval"}`, http.StatusBadRequest)
				return
			}
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","provider":"cloud","state":"queued"}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/tasks/"+taskID:
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","provider":"cloud","state":"completed"}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/tasks/"+taskID+"/events":
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","events":[],"next_cursor":0,"state":"completed"}`))
		case request.Method == http.MethodPost && request.URL.Path == "/v1/tasks/"+taskID+"/cancel":
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","provider":"cloud","state":"cancelled"}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/tasks/"+taskID+"/artifacts":
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","engine_commit":"commit","artifacts":{"stable_json":{"uri":"/v1/tasks/` + taskID + `/artifacts/stable_json","sha256":"97bfe45dfecd80477bc6cafdd457b5f23045d8715fbdc98f6da175f6f2ec6b4f","bytes":58}}}`))
		case request.Method == http.MethodGet && request.URL.Path == "/v1/tasks/"+taskID+"/artifacts/stable_json":
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{"segments":[{"id":1,"start":0,"end":1,"text":"fixture"}]}`))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	projector := &projectingProvider{}
	service, err := New(root, projector, fakeMedia{path: source, thumbnail: "cover-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	service.extractor = func(_ context.Context, _, output string) error {
		return os.WriteFile(output, []byte("audio-fixture"), 0o600)
	}
	if _, err := service.Login(server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	if capabilities, err := service.Capabilities(); err != nil || capabilities["provider"] != "cloud" {
		t.Fatalf("capabilities = %#v, %v", capabilities, err)
	}
	snapshot, err := service.StartTask("loc_0123456789ab", map[string]any{"target": "raw-srt"})
	if err != nil || snapshot["task_id"] != taskID || !uploaded {
		t.Fatalf("start = %#v, uploaded=%v, err=%v", snapshot, uploaded, err)
	}
	// The cover goes up beside the audio, and the task declares it: a library
	// that trusted the flag without the upload would render a broken image on
	// every cloud-only card.
	if string(cover) != "cover-fixture" || !declaredThumbnail {
		t.Fatalf("cover = %q, declared = %v", cover, declaredThumbnail)
	}
	thumbnail, err := service.ThumbnailDataURL(taskID)
	if err != nil || thumbnail != "data:image/jpeg;base64,"+base64.StdEncoding.EncodeToString([]byte("cover-fixture")) {
		t.Fatalf("thumbnail = %q, %v", thumbnail, err)
	}
	if data, err := os.ReadFile(filepath.Join(root, "cloud-thumbnails", taskID+".jpg")); err != nil || string(data) != "cover-fixture" {
		t.Fatalf("cached cover = %q, %v", data, err)
	}
	if status, err := service.TaskStatus(taskID); err != nil || status["state"] != "completed" {
		t.Fatalf("status = %#v, %v", status, err)
	}
	if _, err := service.TaskEvents(taskID, 0); err != nil {
		t.Fatal(err)
	}
	if cancelled, err := service.CancelTask(taskID); err != nil || cancelled["state"] != "cancelled" {
		t.Fatalf("cancel = %#v, %v", cancelled, err)
	}
	manifest, err := service.TaskArtifacts(taskID)
	if err != nil || manifest["engine_commit"] != "commit" {
		t.Fatalf("artifacts = %#v, %v", manifest, err)
	}
	if projector.projected == nil || projector.projected["video_id"] != "loc_0123456789ab" {
		t.Fatalf("projection = %#v", projector.projected)
	}
	if projector.projected["relaxed_srt"] != true {
		t.Fatalf("cloud projection must bypass SRT validation: %#v", projector.projected)
	}
}

// Media transcribed on another machine reaches this one as a fingerprint match
// and nothing more: the card merges, reports the cloud copy, and still offers
// to transcribe the video again because no local document exists. Adoption is
// what turns that match into an editable document.
func TestAdoptLibraryEntryProjectsSubtitlesTranscribedElsewhere(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "video.mp4")
	if err := os.WriteFile(source, []byte("video"), 0o600); err != nil {
		t.Fatal(err)
	}
	const taskID = "vid_0123456789abcdef01234567"
	const foreignID = "vid_ffffffffffffffffffffffff"
	const subtitles = `{"segments":[{"id":1,"start":0,"end":1,"text":"fixture"}]}`
	cover := "\xFF\xD8\xFFcover"
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case request.URL.Path == "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":2,"running":0}`))
		case request.URL.Path == "/v1/library":
			_, _ = writer.Write([]byte(`{"videos":[` +
				`{"id":"` + taskID + `","title":"demo","fingerprint":"fingerprint","duration":60,"status":"completed","source":"cloud_audio","thumbnailAvailable":true,"artifactNames":["stable_json"]},` +
				`{"id":"` + foreignID + `","title":"other","fingerprint":"other","duration":60,"status":"completed","source":"local_sync","artifactNames":["stable_json"]}]}`))
		case request.URL.Path == "/v1/library/"+taskID+"/thumbnail":
			writer.Header().Set("Content-Type", "image/jpeg")
			_, _ = writer.Write([]byte(cover))
		case request.URL.Path == "/v1/tasks/"+taskID+"/artifacts":
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","engine_commit":"commit","artifacts":{"stable_json":{"uri":"/v1/tasks/` + taskID + `/artifacts/stable_json","sha256":"97bfe45dfecd80477bc6cafdd457b5f23045d8715fbdc98f6da175f6f2ec6b4f","bytes":58}}}`))
		case request.URL.Path == "/v1/tasks/"+taskID+"/artifacts/stable_json":
			_, _ = writer.Write([]byte(subtitles))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	projector := &projectingProvider{}
	media := fakeMedia{path: source, covers: map[string]string{}}
	service, err := New(root, projector, media)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Login(server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	// No StartTask ran here, so there is no task link to resolve the media by.
	if err := service.AdoptLibraryEntry(taskID, "loc_0123456789ab"); err != nil {
		t.Fatal(err)
	}
	// The cover follows the subtitles down. A cloud-only card draws it from the
	// bucket and a local card from the library, so leaving it behind would blank
	// the very card the adoption just filled in.
	expectedCover := "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString([]byte(cover))
	if media.covers["loc_0123456789ab"] != expectedCover {
		t.Fatalf("adopted cover = %q", media.covers["loc_0123456789ab"])
	}
	if projector.projected == nil || projector.projected["video_id"] != "loc_0123456789ab" {
		t.Fatalf("projection = %#v", projector.projected)
	}
	if projector.projected["relaxed_srt"] != true {
		t.Fatalf("cloud adoption must bypass SRT validation: %#v", projector.projected)
	}
	if artifacts, ok := projector.projected["artifacts"].(map[string]string); !ok || artifacts["stable_json"] != subtitles {
		t.Fatalf("projected artifacts = %#v", projector.projected["artifacts"])
	}

	// Nothing local at all: the subtitles are still the deliverable, so the
	// library records a placeholder for the fingerprint and the document hangs
	// on that. The projection carries no source path, which is what leaves the
	// editor without a player until the video is relinked.
	projector.projected = nil
	localID, err := service.AdoptCloudEntry(taskID)
	if err != nil || localID != "loc_cccccccccccc" {
		t.Fatalf("adopt cloud-only = %q, %v", localID, err)
	}
	if projector.projected["video_id"] != localID || projector.projected["source_path"] != "" {
		t.Fatalf("cloud-only projection = %#v", projector.projected)
	}
	if projector.projected["fingerprint"] != "fingerprint" || projector.projected["title"] != "demo" {
		t.Fatalf("cloud-only projection metadata = %#v", projector.projected)
	}
	// The placeholder has no video at all, so this cover is the only one it can
	// ever have until a real file is associated with it.
	if media.covers[localID] != expectedCover {
		t.Fatalf("placeholder cover = %q", media.covers[localID])
	}

	// A cloud entry for different media must never be projected onto this one:
	// the local document would silently fill with another video's subtitles.
	projector.projected = nil
	if err := service.AdoptLibraryEntry(foreignID, "loc_0123456789ab"); err == nil || projector.projected != nil {
		t.Fatalf("adopting a foreign fingerprint = %v, projection = %#v", err, projector.projected)
	}
}

// The upload must not be the thing that degrades the audio. Everything below
// the desktop assumes the object it receives carries the source signal intact:
// the separator picks its own input rate from the declared duration, and the
// delivery to VAD/Whisper is resampled once, in the cloud. A lossy or resampled
// upload puts a second, invisible conversion in front of all of that.
func TestCloudAudioIsUploadedLosslessAndUnresampled(t *testing.T) {
	cases := map[string][]string{
		"copy":     cloudAudioCopyArgs("in.mkv", "out.m4a"),
		"lossless": cloudAudioLosslessArgs("in.mkv", "out.m4a"),
	}
	for name, args := range cases {
		joined := strings.Join(args, " ")
		for _, forbidden := range []string{"-b:a", "-ar", "-ac", "-sample_fmt", "-q:a"} {
			for _, arg := range args {
				if arg == forbidden {
					t.Fatalf("%s: %s resamples, downmixes or requantizes: %q", name, forbidden, joined)
				}
			}
		}
		// `-f mp4` over the `.m4a` extension's `ipod` muxer, which admits only
		// AAC and ALAC and so would reject a copy of Opus, MP3, FLAC or AC-3 --
		// exactly the tracks most worth copying.
		if !strings.Contains(joined, "-f mp4") {
			t.Fatalf("%s: expected the generic MP4 muxer, got %q", name, joined)
		}
		if !strings.Contains(joined, "-movflags +faststart") {
			t.Fatalf("%s: expected +faststart, got %q", name, joined)
		}
		if args[len(args)-1] != "out.m4a" {
			t.Fatalf("%s: output must be last, got %q", name, joined)
		}
	}
	if !strings.Contains(strings.Join(cases["copy"], " "), "-c:a copy") {
		t.Fatalf("copy branch must stream-copy: %q", cases["copy"])
	}
	// ALAC, not FLAC: the object key, the extension and the declared content
	// type are `.m4a` / `audio/mp4` on both sides and in every object already
	// in the bucket, and ALAC is the lossless codec that keeps all three true.
	if !strings.Contains(strings.Join(cases["lossless"], " "), "-c:a alac") {
		t.Fatalf("fallback branch must be lossless: %q", cases["lossless"])
	}
}

// Copying is preferred because it is better on every axis at once -- no second
// generation, a fifth of the bytes, no CPU on the user's machine. It is skipped
// only where it would upload raw PCM, which is the one case where re-encoding
// losslessly is both smaller and still lossless.
func TestUncompressedSourcesAreDetectedForLosslessReencode(t *testing.T) {
	for codec, uncompressed := range map[string]bool{
		"pcm_s16le":   true,
		"pcm_f32le":   true,
		"adpcm_ms":    true,
		"wavpack":     true,
		"PCM_S24LE":   true,
		"pcm_s16le\n": true,
		// Lossy, and losslessly compressed, are both cheaper to copy: copying
		// adds no generation, and re-encoding either of them to ALAC would only
		// grow the upload for the same signal.
		"aac":  false,
		"opus": false,
		"mp3":  false,
		"ac3":  false,
		"flac": false,
		"alac": false,
		// An unreadable probe must not route a compressed source through a
		// pointless decode/encode round trip.
		"": false,
	} {
		if got := isUncompressedCodec(codec); got != uncompressed {
			t.Fatalf("codec %q: uncompressed=%v, want %v", codec, got, uncompressed)
		}
	}
}

// A re-transcribe of the same media reuses the cloud library id, so the entry's
// status describes the new attempt while the previous run's subtitles are still
// on the backend's volume and still named by the manifest. Gating adoption on
// the status made those unreachable for as long as the new attempt had not
// succeeded -- and permanently, if the user gave up on it.
func TestAdoptLibraryEntryServesSubtitlesWhileANewAttemptIsUnfinished(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "video.mp4")
	if err := os.WriteFile(source, []byte("video"), 0o600); err != nil {
		t.Fatal(err)
	}
	const taskID = "vid_0123456789abcdef01234567"
	const emptyID = "vid_ffffffffffffffffffffffff"
	const subtitles = `{"segments":[{"id":1,"start":0,"end":1,"text":"fixture"}]}`
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		switch {
		case request.URL.Path == "/v1/session":
			_, _ = writer.Write([]byte(`{"authenticated":true,"remaining":2,"running":0}`))
		case request.URL.Path == "/v1/library":
			_, _ = writer.Write([]byte(`{"videos":[` +
				`{"id":"` + taskID + `","title":"demo","fingerprint":"fingerprint","duration":60,"status":"failed","source":"cloud_audio","artifactNames":["stable_json"]},` +
				`{"id":"` + emptyID + `","title":"never ran","fingerprint":"fingerprint","duration":60,"status":"failed","source":"cloud_audio"}]}`))
		case request.URL.Path == "/v1/tasks/"+taskID+"/artifacts":
			_, _ = writer.Write([]byte(`{"schema":1,"task_id":"` + taskID + `","engine_commit":"commit","artifacts":{"stable_json":{"uri":"/v1/tasks/` + taskID + `/artifacts/stable_json","sha256":"97bfe45dfecd80477bc6cafdd457b5f23045d8715fbdc98f6da175f6f2ec6b4f","bytes":58}}}`))
		case request.URL.Path == "/v1/tasks/"+taskID+"/artifacts/stable_json":
			_, _ = writer.Write([]byte(subtitles))
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	projector := &projectingProvider{}
	service, err := New(root, projector, fakeMedia{path: source})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.Login(server.URL, "login-key"); err != nil {
		t.Fatal(err)
	}
	if err := service.AdoptLibraryEntry(taskID, "loc_0123456789ab"); err != nil {
		t.Fatalf("adopting an entry whose newest attempt failed: %v", err)
	}
	if artifacts, ok := projector.projected["artifacts"].(map[string]string); !ok || artifacts["stable_json"] != subtitles {
		t.Fatalf("projected artifacts = %#v", projector.projected["artifacts"])
	}

	// An entry that never published a set is still refused: the widened gate
	// asks what is retrievable, not merely whether an attempt has ended.
	projector.projected = nil
	if err := service.AdoptLibraryEntry(emptyID, "loc_0123456789ab"); err == nil || projector.projected != nil {
		t.Fatalf("adopting an entry with no artifacts = %v, projection = %#v", err, projector.projected)
	}
}

// A refresh used to make three requests for one screen: the session, the
// library to render it, and the library again inside the sync check. The
// backend returns the library beside the session now, and the two extra walks
// are meant to disappear -- while a backend that predates the field keeps
// working off the separate request.
func TestSessionCarriesTheLibraryAndDropsTheExtraRequests(t *testing.T) {
	for _, testCase := range []struct {
		name           string
		sessionBody    string
		wantLibraryHit int
		wantTitles     []string
	}{
		{
			name:           "backend sends the library",
			sessionBody:    `{"authenticated":true,"running":0,"videos":[{"id":"vid_a","title":"从 session 来"}]}`,
			wantLibraryHit: 0,
			wantTitles:     []string{"从 session 来"},
		},
		{
			// A nil slice, not an empty one: the field is absent entirely.
			name:           "backend predates the field",
			sessionBody:    `{"authenticated":true,"running":0}`,
			wantLibraryHit: 1,
			wantTitles:     []string{"从 library 来"},
		},
		{
			// An empty library is not an absent one, and must not cost a request.
			name:           "backend sends an empty library",
			sessionBody:    `{"authenticated":true,"running":0,"videos":[]}`,
			wantLibraryHit: 0,
			wantTitles:     []string{},
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			libraryHits := 0
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				writer.Header().Set("Content-Type", "application/json")
				switch request.URL.Path {
				case "/v1/session":
					_, _ = writer.Write([]byte(testCase.sessionBody))
				case "/v1/library":
					libraryHits++
					_, _ = writer.Write([]byte(`{"videos":[{"id":"vid_a","title":"从 library 来"}]}`))
				default:
					http.NotFound(writer, request)
				}
			}))
			defer server.Close()

			// A catalog that lists, so the sync check actually runs -- it is
			// the second of the two places that fetched the library, and the
			// one that has to stop.
			service, err := New(t.TempDir(), fakeProvider{}, fakeCatalog{})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := service.Login(server.URL, "login-key"); err != nil {
				t.Fatal(err)
			}
			libraryHits = 0

			session, err := service.RefreshSession()
			if err != nil {
				t.Fatal(err)
			}
			if libraryHits != testCase.wantLibraryHit {
				t.Fatalf("/v1/library requests = %d, want %d", libraryHits, testCase.wantLibraryHit)
			}
			titles := []string{}
			for _, entry := range session.Videos {
				titles = append(titles, entry.Title)
			}
			if len(titles) != len(testCase.wantTitles) {
				t.Fatalf("session videos = %#v, want %#v", titles, testCase.wantTitles)
			}
			for index, title := range titles {
				if title != testCase.wantTitles[index] {
					t.Fatalf("session videos = %#v, want %#v", titles, testCase.wantTitles)
				}
			}
			if session.CloudVideos != len(testCase.wantTitles) {
				t.Fatalf("cloudVideos = %d, want %d", session.CloudVideos, len(testCase.wantTitles))
			}
		})
	}
}

// A zero-byte cloud-sync-suppressions.json took the whole desktop down with
// "unexpected end of JSON input" before main() drew a window. The writer cannot
// produce that file -- it marshals at least `{}`, writes a temporary and
// renames -- so the damage arrives from outside the app, and none of the three
// files is worth refusing to start over: the session is a key the user can
// enter again, and the links and suppressions rebuild from use.
func TestDamagedStateFilesDoNotBlockStartup(t *testing.T) {
	for _, testCase := range []struct {
		name string
		file string
		body string
	}{
		{"empty suppressions", "cloud-sync-suppressions.json", ""},
		{"truncated suppressions", "cloud-sync-suppressions.json", `{"fingerprint":`},
		{"empty links", "cloud-tasks.json", ""},
		{"links of the wrong shape", "cloud-tasks.json", `["not","a","map"]`},
		{"empty session", "cloud-session.json", ""},
		{"session with no key", "cloud-session.json", `{"backend":"https://example.test"}`},
		{"session with a rejected backend", "cloud-session.json", `{"backend":"ftp://example.test","key":"k"}`},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.WriteFile(filepath.Join(root, testCase.file), []byte(testCase.body), 0o600); err != nil {
				t.Fatal(err)
			}
			service, err := New(root, fakeProvider{})
			if err != nil {
				t.Fatalf("New() = %v, want a service that starts", err)
			}
			if service.Session().Authenticated {
				t.Fatal("damaged state must not authenticate a session")
			}
			// Usable, not merely constructed: the maps have to be the empty
			// ones the rest of the service writes into, never nil.
			if service.links == nil || service.syncSuppressions == nil {
				t.Fatalf("links=%#v suppressions=%#v, want empty maps", service.links, service.syncSuppressions)
			}
		})
	}
}

// The other direction: a file that is intact still has to be read.
func TestIntactStateIsStillLoaded(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "cloud-session.json"), []byte(`{"backend":"https://example.test","key":"login-key"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "cloud-sync-suppressions.json"), []byte(`{"fingerprint":1234}`), 0o600); err != nil {
		t.Fatal(err)
	}
	service, err := New(root, fakeProvider{})
	if err != nil {
		t.Fatal(err)
	}
	if session := service.Session(); !session.Authenticated || session.Backend != "https://example.test" {
		t.Fatalf("session = %#v, want the stored one", session)
	}
	if service.syncSuppressions["fingerprint"] != 1234 {
		t.Fatalf("suppressions = %#v, want the stored one", service.syncSuppressions)
	}
}
