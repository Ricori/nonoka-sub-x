package library

import (
	"context"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNeedsProxyFromShortSide(t *testing.T) {
	for _, test := range []struct {
		entry Entry
		want  bool
	}{
		{Entry{Width: 3840, Height: 2160}, true},
		{Entry{Width: 2560, Height: 1440}, true},
		{Entry{Width: 2160, Height: 3840}, true},
		{Entry{Width: 1920, Height: 1080}, false},
		{Entry{Width: 3840, Height: 2160, AudioOnly: true}, false},
	} {
		if got := needsProxy(test.entry); got != test.want {
			t.Fatalf("needsProxy(%dx%d audio=%v) = %v", test.entry.Width, test.entry.Height, test.entry.AudioOnly, got)
		}
	}
}

func TestProxySizeKeepsAspectAndEvenSides(t *testing.T) {
	for _, test := range []struct{ width, height, wantW, wantH int }{
		{3840, 2160, 1280, 720},
		{2160, 3840, 720, 1280},
		{4096, 2160, 1366, 720},
		{3440, 1440, 1720, 720},
	} {
		if w, h := proxySize(test.width, test.height); w != test.wantW || h != test.wantH {
			t.Fatalf("proxySize(%d, %d) = %dx%d", test.width, test.height, w, h)
		}
	}
}

func TestProxyPipelineArgs(t *testing.T) {
	args := strings.Join(proxyPipelines[0].args("in.mkv", "out.part", 1280, 720), " ")
	for _, part := range []string{
		"-hwaccel cuda -hwaccel_output_format cuda -i in.mkv",
		"-vf scale_cuda=1280:720:format=yuv420p -c:v h264_nvenc",
		"-g 30 -bf 0",
		"-f mp4 -progress pipe:1 -nostats out.part",
	} {
		if !strings.Contains(args, part) {
			t.Fatalf("args missing %q: %s", part, args)
		}
	}
}

func TestOrderedProxyPipelinesPrefersLastSuccess(t *testing.T) {
	service := &Service{}
	if first := service.orderedProxyPipelines()[0].name; first != "nvidia" {
		t.Fatalf("first = %s", first)
	}
	service.rememberProxyPipeline("software")
	ordered := service.orderedProxyPipelines()
	if ordered[0].name != "software" || len(ordered) != len(proxyPipelines) {
		t.Fatalf("ordered = %v", ordered)
	}
}

func TestProxiesCountTowardsCacheAndGoWithMedia(t *testing.T) {
	root := t.TempDir()
	id := "loc_0123456789ab"
	cacheDir := filepath.Join(root, "videos")
	service := &Service{root: root, cacheDir: cacheDir, cacheConfig: defaultCacheConfig(), proxyCancels: map[string]context.CancelFunc{}}
	if err := os.MkdirAll(service.proxyDirectory(), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(service.proxyPath(id), []byte("proxy"), 0o600); err != nil {
		t.Fatal(err)
	}
	// An unfinished build is neither a proxy nor counted.
	if err := os.WriteFile(service.proxyPath(id)+".part", []byte("partial"), 0o600); err != nil {
		t.Fatal(err)
	}
	if service.readyProxy(id) == "" {
		t.Fatal("finished proxy not found")
	}
	if status := service.CacheStatus(); status.Files != 1 || status.Bytes != int64(len("proxy")) {
		t.Fatalf("status = %#v", status)
	}
	if err := service.removeCachedMedia(id); err != nil {
		t.Fatal(err)
	}
	if service.readyProxy(id) != "" {
		t.Fatal("proxy survived removing the media")
	}
}

// TestBuildProxySample runs the real pipelines on a sample file:
// NONOKA_PROXY_SAMPLE=path/to/4k.mp4 go test -run TestBuildProxySample -v
func TestBuildProxySample(t *testing.T) {
	sample := os.Getenv("NONOKA_PROXY_SAMPLE")
	if sample == "" {
		t.Skip("NONOKA_PROXY_SAMPLE not set")
	}
	root := t.TempDir()
	service, err := newService(root, commandMediaTools{dataDirectory: root})
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := service.prober.Probe(context.Background(), sample)
	if err != nil {
		t.Fatal(err)
	}
	id := "loc_0123456789ab"
	entry := Entry{ID: id, SourcePath: sample, Width: metadata.Width, Height: metadata.Height, Duration: metadata.Duration}
	service.entries = []Entry{entry}
	started := time.Now()
	if err := service.buildProxy(context.Background(), id, sample, entry); err != nil {
		t.Fatal(err)
	}
	t.Logf("pipeline %s built a %.0fs proxy in %s", service.proxyPipeline, metadata.Duration, time.Since(started).Round(time.Millisecond))
	proxy, err := service.prober.Probe(context.Background(), service.readyProxy(id))
	if err != nil {
		t.Fatal(err)
	}
	if want, _ := proxySize(metadata.Width, metadata.Height); proxy.Width != want {
		t.Fatalf("proxy is %dx%d", proxy.Width, proxy.Height)
	}
}

// The player's range requests must not queue behind cache work: CacheMedia
// holds cacheMu for the whole copy of a large source.
func TestMediaServerDoesNotWaitOnCacheLock(t *testing.T) {
	root := t.TempDir()
	id := "loc_0123456789ab"
	source := filepath.Join(root, "source.mp4")
	if err := os.WriteFile(source, []byte("video bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	service := &Service{root: root, cacheDir: filepath.Join(root, "videos"), indexPath: filepath.Join(root, "library.json"),
		entries: []Entry{{ID: id, SourcePath: source}}}
	url, err := service.MediaURL(id)
	if err != nil {
		t.Fatal(err)
	}
	defer service.Close()
	service.cacheMu.Lock()
	defer service.cacheMu.Unlock()
	done := make(chan error, 1)
	go func() {
		response, err := http.Get(url)
		if err == nil {
			response.Body.Close()
			if response.StatusCode != http.StatusOK {
				err = errors.New(response.Status)
			}
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("media request waited on cacheMu")
	}
}
