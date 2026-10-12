package library

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"mime"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// audioContentTypes is consulted before the platform table: Windows answers
// mime.TypeByExtension from the registry, which often has nothing for FLAC or
// Opus, and the fallback below would then label an audio file as MP4 video.
var audioContentTypes = map[string]string{
	".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
	".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
}

type loopbackMediaServer struct {
	listener net.Listener
	server   *http.Server
	path     string
	baseURL  string
	source   func(string) string
}

func newLoopbackMediaServer(source func(string) string) (*loopbackMediaServer, error) {
	tokenBytes := make([]byte, 24)
	if _, err := rand.Read(tokenBytes); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	mediaPath := "/media/" + base64.RawURLEncoding.EncodeToString(tokenBytes)
	media := &loopbackMediaServer{listener: listener, path: mediaPath, baseURL: "http://" + listener.Addr().String() + mediaPath, source: source}
	media.server = &http.Server{Handler: media.handler(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: time.Minute}
	go func() { _ = media.server.Serve(listener) }()
	return media, nil
}

func (s *loopbackMediaServer) URL(id string) string { return s.baseURL + "?id=" + id }

func (s *loopbackMediaServer) Close() error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return s.server.Shutdown(ctx)
}

func (s *loopbackMediaServer) handler() http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != s.path || (request.Method != http.MethodGet && request.Method != http.MethodHead) {
			http.NotFound(writer, request)
			return
		}
		id := request.URL.Query().Get("id")
		if !validID(id) {
			http.NotFound(writer, request)
			return
		}
		path := s.source(id)
		file, err := os.Open(path)
		if err != nil {
			http.NotFound(writer, request)
			return
		}
		defer file.Close()
		stat, err := file.Stat()
		if err != nil || !stat.Mode().IsRegular() {
			http.NotFound(writer, request)
			return
		}
		extension := strings.ToLower(filepath.Ext(path))
		contentType := audioContentTypes[extension]
		if contentType == "" {
			contentType = mime.TypeByExtension(extension)
		}
		if contentType == "" {
			contentType = "video/mp4"
		}
		writer.Header().Set("Content-Type", contentType)
		writer.Header().Set("Accept-Ranges", "bytes")
		writer.Header().Set("Cache-Control", "no-store")
		writer.Header().Set("Cross-Origin-Resource-Policy", "cross-origin")
		writer.Header().Set("X-Content-Type-Options", "nosniff")
		http.ServeContent(writer, request, stat.Name(), stat.ModTime(), file)
	})
}
