package app

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Ricori/nonoka-x/desktop/internal/managedtools"
	"github.com/Ricori/nonoka-x/desktop/internal/sidecar"
)

func TestPythonBootstrapDetectsUsableInstalledLauncher(t *testing.T) {
	data := t.TempDir()
	python := managedtools.BootstrapPython(data)
	if err := os.MkdirAll(filepath.Dir(python), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(python, []byte("fixture"), 0o700); err != nil {
		t.Fatal(err)
	}
	bootstrap := newPythonBootstrap(data, sidecar.New(sidecar.Config{}), func(candidate string) bool {
		return candidate == python
	})
	status := bootstrap.Status()
	if status["state"] != "ready" || status["python"] != python {
		t.Fatalf("status = %#v", status)
	}
	_, wantSupported := bootstrapUVAssetForHost()
	if status["supported"] != wantSupported {
		t.Fatalf("supported = %#v, want %v", status["supported"], wantSupported)
	}
}

func TestPythonBootstrapOffersRepairForBrokenInstalledLauncher(t *testing.T) {
	data := t.TempDir()
	python := managedtools.BootstrapPython(data)
	if err := os.MkdirAll(filepath.Dir(python), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(python, []byte("broken uv trampoline"), 0o700); err != nil {
		t.Fatal(err)
	}

	bootstrap := newPythonBootstrap(data, sidecar.New(sidecar.Config{}), func(string) bool { return false })
	status := bootstrap.Status()
	if status["state"] != "failed" || status["stage"] != "failed" {
		t.Fatalf("status = %#v", status)
	}
	if message, _ := status["message"].(string); !strings.Contains(message, "修复环境") {
		t.Fatalf("message = %q", message)
	}
}

func TestFileMatchesRequiresSizeAndSHA256(t *testing.T) {
	path := filepath.Join(t.TempDir(), "asset.zip")
	contents := []byte("pinned bootstrap asset")
	if err := os.WriteFile(path, contents, 0o600); err != nil {
		t.Fatal(err)
	}
	digest := fmt.Sprintf("%x", sha256.Sum256(contents))
	if !fileMatches(path, int64(len(contents)), digest) {
		t.Fatal("matching file was rejected")
	}
	if fileMatches(path, int64(len(contents)+1), digest) || fileMatches(path, int64(len(contents)), "00") {
		t.Fatal("invalid size or digest was accepted")
	}
}

// The three platforms the launcher supports, and the two archive formats their
// uv builds come in. A typo in an asset name only surfaces at download time on
// the platform it belongs to, which is never the machine the test runs on.
func TestBootstrapUVAssetsCoverEverySupportedPlatform(t *testing.T) {
	for _, platform := range []string{"windows/amd64", "darwin/arm64", "darwin/amd64"} {
		asset, ok := bootstrapUVAssets[platform]
		if !ok {
			t.Fatalf("%s has no uv asset", platform)
		}
		if asset.Tar != strings.HasSuffix(asset.Name, ".tar.gz") {
			t.Errorf("%s: Tar = %v for asset %q", platform, asset.Tar, asset.Name)
		}
		if !asset.Tar && !strings.HasSuffix(asset.Name, ".zip") {
			t.Errorf("%s: asset %q is neither a tarball nor a zip", platform, asset.Name)
		}
		if asset.Size <= 0 || len(asset.SHA256) != 64 {
			t.Errorf("%s: asset is not pinned: %#v", platform, asset)
		}
		if want := bootstrapUVRelease + asset.Name; asset.URL() != want {
			t.Errorf("%s: URL = %q, want %q", platform, asset.URL(), want)
		}
	}
}

func TestBootstrapUVPinMatchesFineSubManifest(t *testing.T) {
	manifestPath := filepath.Join("..", "..", "..", "third_party", "finesub", "src", "finesub_bootstrap", "runtime-manifest.json")
	contents, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Resources []struct {
			ID    string `json:"id"`
			Asset struct {
				URL    string `json:"url"`
				Size   int64  `json:"size"`
				SHA256 string `json:"sha256"`
			} `json:"asset"`
		} `json:"resources"`
	}
	if err := json.Unmarshal(contents, &manifest); err != nil {
		t.Fatal(err)
	}
	windows := bootstrapUVAssets["windows/amd64"]
	for _, resource := range manifest.Resources {
		if resource.ID == "uv" {
			if resource.Asset.URL != windows.URL() || resource.Asset.Size != windows.Size || resource.Asset.SHA256 != windows.SHA256 {
				t.Fatalf("bootstrap uv pin drifted from FineSub manifest: %#v", resource.Asset)
			}
			return
		}
	}
	t.Fatal("FineSub runtime manifest has no uv resource")
}

// The macOS uv download is a tarball whose executable sits one directory deep,
// beside a uvx of the same shape. Taking the member by base name is what keeps
// the two apart, and the mode written here is what makes the result runnable --
// the archive's own mode is not carried over.
func TestExtractNamedTarGzFileTakesTheNamedMember(t *testing.T) {
	archive := filepath.Join(t.TempDir(), "uv-aarch64-apple-darwin.tar.gz")
	members := []struct {
		name     string
		contents string
	}{
		{"uv-aarch64-apple-darwin/uvx", "wrong member"},
		{"uv-aarch64-apple-darwin/uv", "uv binary"},
	}
	buffer := &bytes.Buffer{}
	compressor := gzip.NewWriter(buffer)
	writer := tar.NewWriter(compressor)
	for _, member := range members {
		if err := writer.WriteHeader(&tar.Header{
			Name:     member.name,
			Typeflag: tar.TypeReg,
			Mode:     0o644,
			Size:     int64(len(member.contents)),
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write([]byte(member.contents)); err != nil {
			t.Fatal(err)
		}
	}
	if err := errors.Join(writer.Close(), compressor.Close()); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(archive, buffer.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}

	destination := filepath.Join(t.TempDir(), "tools", "uv")
	if err := extractNamedArchiveFile(archive, true, "uv", destination); err != nil {
		t.Fatal(err)
	}
	extracted, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	if string(extracted) != "uv binary" {
		t.Fatalf("extracted %q", extracted)
	}
	info, err := os.Stat(destination)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("extracted uv is not executable: %v", info.Mode())
	}
}

func TestExtractNamedTarGzFileReportsAMissingMember(t *testing.T) {
	archive := filepath.Join(t.TempDir(), "empty.tar.gz")
	buffer := &bytes.Buffer{}
	compressor := gzip.NewWriter(buffer)
	writer := tar.NewWriter(compressor)
	if err := errors.Join(writer.Close(), compressor.Close()); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(archive, buffer.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := extractNamedArchiveFile(archive, true, "uv", filepath.Join(t.TempDir(), "uv")); err == nil {
		t.Fatal("a tarball without the member was accepted")
	}
}

func TestGitHubDownloadSourcesFollowTheLadder(t *testing.T) {
	t.Setenv("NONOKA_GITHUB_PROXY", "")
	t.Setenv("FINESUB_GITHUB_FILE_PROXY", "")
	raw := "https://github.com/example/repo/releases/download/v1.0/file.zip"
	proxy := "http://127.0.0.1:7890"

	sources := gitHubDownloadSources(raw, []bootstrapAttempt{{Proxy: proxy}, {}, {Mirror: true}})
	want := []bootstrapSource{
		{bootstrapAttempt: bootstrapAttempt{Proxy: proxy}, URL: raw},
		{URL: raw},
		{bootstrapAttempt: bootstrapAttempt{Mirror: true}, URL: "https://gh-proxy.com/" + raw},
	}
	if !slices.Equal(sources, want) {
		t.Fatalf("sources = %#v", sources)
	}

	// A proxy the user names takes the mirror's place on the ladder.
	t.Setenv("NONOKA_GITHUB_PROXY", "https://custom-proxy.example.com")
	custom := gitHubDownloadSources(raw, []bootstrapAttempt{{}, {Mirror: true}})
	if custom[0].URL != raw || custom[1].URL != "https://custom-proxy.example.com/"+raw {
		t.Fatalf("custom proxy not in the mirror rung: %#v", custom)
	}
}

func TestDownloadAbandonsACrawlingSourceForTheNext(t *testing.T) {
	shrinkBootstrapWindows(t, 100*time.Millisecond)
	payload := []byte("uv archive payload")
	digest := sha256.Sum256(payload)
	fast := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = writer.Write(payload)
	}))
	defer fast.Close()

	data := t.TempDir()
	bootstrap := NewPythonBootstrap(data, sidecar.New(sidecar.Config{}))
	destination := filepath.Join(data, "uv.zip")
	sources := []bootstrapSource{{URL: dripServer(t).URL}, {URL: fast.URL}}

	if err := bootstrap.downloadWithFallback(sources, destination, int64(len(payload)), hex.EncodeToString(digest[:])); err != nil {
		t.Fatalf("the second source should have been used: %v", err)
	}
	written, err := os.ReadFile(destination)
	if err != nil || !bytes.Equal(written, payload) {
		t.Fatalf("written=%q err=%v", written, err)
	}
}

func TestDownloadAbandonsASourceThatStopsSending(t *testing.T) {
	shrinkBootstrapWindows(t, 100*time.Millisecond)
	silent := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = writer.Write([]byte("x"))
		writer.(http.Flusher).Flush()
		<-request.Context().Done()
	}))
	defer silent.Close()

	data := t.TempDir()
	bootstrap := NewPythonBootstrap(data, sidecar.New(sidecar.Config{}))
	err := bootstrap.downloadSingle(bootstrapSource{URL: silent.URL}, filepath.Join(data, "uv.zip"), 100, "digest", "message", true)
	if !errors.Is(err, errBootstrapTooSlow) {
		t.Fatalf("a stalled body should be too slow, got %v", err)
	}
}

func TestDownloadWithFallbackFailsWithManualInstructions(t *testing.T) {
	data := t.TempDir()
	bootstrap := NewPythonBootstrap(data, sidecar.New(sidecar.Config{}))
	destination := filepath.Join(data, "uv.zip")

	// Invalid candidate URLs that cannot be connected to
	sources := []bootstrapSource{
		{URL: "http://127.0.0.1:54321/nonexistent1.zip"},
		{URL: "http://127.0.0.1:54321/nonexistent2.zip"},
	}

	err := bootstrap.downloadWithFallback(sources, destination, 100, "dummy-digest")
	if err == nil {
		t.Fatal("expected error, got nil")
	}

	errMsg := err.Error()
	if !strings.Contains(errMsg, bootstrapUnreachableMessage) {
		t.Errorf("expected the advice to turn a proxy on, got: %s", errMsg)
	}
	if !strings.Contains(errMsg, "手动下载") {
		t.Errorf("expected manual download guidance in error message, got: %s", errMsg)
	}
	if !strings.Contains(errMsg, destination) {
		t.Errorf("expected destination path in error message, got: %s", errMsg)
	}
	if !strings.Contains(errMsg, bootstrap.uv.URL()) {
		t.Errorf("expected official download link in error message, got: %s", errMsg)
	}
}
