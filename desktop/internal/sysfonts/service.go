// Package sysfonts lists the font families installed on this machine so the
// style panel can offer them by name, and hands their files to the subtitle
// preview: libass runs as WASM there and cannot see system fonts on its own.
// It reads the `name` table straight out of the font files instead of asking
// the OS, so the same code works on every platform and the names it returns
// are the ones libass matches on.
package sysfonts

import (
	"encoding/binary"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"unicode/utf16"
)

// RoutePath is where the asset server answers font requests. The preview asks
// for `RoutePath?family=<name>` (repeatable; the first installed one wins).
const RoutePath = "/__sysfonts"

type Service struct {
	mu    sync.Mutex
	files map[string]string // lowercased family / full name -> font file
}

func New() *Service { return &Service{} }

// List returns the installed family names, deduplicated case-insensitively and
// sorted. Unreadable or malformed files are skipped rather than reported: one
// broken font must not empty the whole list. Each call rescans, so a font
// installed while the app is open shows up the next time an editor opens.
func (s *Service) List() []string {
	families, files := scan()
	s.mu.Lock()
	s.files = files
	s.mu.Unlock()
	return families
}

// lookup resolves a name to its font file, scanning once on first use.
func (s *Service) lookup(name string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.files == nil {
		_, s.files = scan()
	}
	return s.files[strings.ToLower(strings.TrimSpace(name))]
}

// Middleware serves font files on RoutePath and passes everything else on.
// Only files found by the scan can be served: the query names a family, never
// a path. It is a package function rather than a method so the Wails binding
// generator does not try to expose it to the frontend.
func Middleware(s *Service) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.URL.Path != RoutePath {
				next.ServeHTTP(writer, request)
				return
			}
			if request.Method != http.MethodGet && request.Method != http.MethodHead {
				http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			for _, family := range request.URL.Query()["family"] {
				if path := s.lookup(family); path != "" {
					writer.Header().Set("Content-Type", "application/octet-stream")
					writer.Header().Set("Cache-Control", "no-store")
					http.ServeFile(writer, request, path)
					return
				}
			}
			http.NotFound(writer, request)
		})
	}
}

func scan() ([]string, map[string]string) {
	display := make(map[string]string)
	files := make(map[string]string)
	// Full names are only lookup aliases ("Arial Bold"), so they never shadow
	// a real family name that some other file carries.
	fullNames := make(map[string]string)
	for _, dir := range fontDirectories() {
		_ = filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() || !isFontFile(path) {
				return nil
			}
			for _, face := range facesInFile(path) {
				key := strings.ToLower(face.family)
				if _, ok := display[key]; !ok {
					display[key] = face.family
				}
				for _, alias := range face.families {
					if _, ok := files[alias]; !ok {
						files[alias] = path
					}
				}
				for _, alias := range face.fullNames {
					if _, ok := fullNames[alias]; !ok {
						fullNames[alias] = path
					}
				}
			}
			return nil
		})
	}
	for alias, path := range fullNames {
		if _, ok := files[alias]; !ok {
			files[alias] = path
		}
	}
	families := make([]string, 0, len(display))
	for _, family := range display {
		families = append(families, family)
	}
	sort.Slice(families, func(i, j int) bool {
		return strings.ToLower(families[i]) < strings.ToLower(families[j])
	})
	return families, files
}

func fontDirectories() []string {
	home, _ := os.UserHomeDir()
	switch runtime.GOOS {
	case "windows":
		windir := os.Getenv("WINDIR")
		if windir == "" {
			windir = `C:\Windows`
		}
		dirs := []string{filepath.Join(windir, "Fonts")}
		// Fonts installed "for this user only" live here, not under WINDIR.
		if local := os.Getenv("LOCALAPPDATA"); local != "" {
			dirs = append(dirs, filepath.Join(local, "Microsoft", "Windows", "Fonts"))
		}
		return dirs
	case "darwin":
		return []string{"/System/Library/Fonts", "/Library/Fonts", filepath.Join(home, "Library", "Fonts")}
	default:
		return []string{"/usr/share/fonts", "/usr/local/share/fonts",
			filepath.Join(home, ".local", "share", "fonts"), filepath.Join(home, ".fonts")}
	}
}

func isFontFile(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".ttf", ".otf", ".ttc", ".otc":
		return true
	}
	return false
}

// face is one font inside a file: the name shown in the picker, plus every
// lowercased name libass may be asked for it by.
type face struct {
	family    string
	families  []string
	fullNames []string
}

func facesInFile(path string) []face {
	file, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer file.Close()
	return readFaces(file)
}

// Collections hold far fewer faces than this; anything larger is not a font.
const (
	maxCollectionFonts = 256
	maxTables          = 1024
)

// readFaces returns every face in an sfnt file or collection (.ttc/.otc).
func readFaces(r io.ReaderAt) []face {
	head := make([]byte, 12)
	if _, err := r.ReadAt(head, 0); err != nil {
		return nil
	}
	offsets := []uint32{0}
	if string(head[:4]) == "ttcf" {
		count := binary.BigEndian.Uint32(head[8:12])
		if count == 0 || count > maxCollectionFonts {
			return nil
		}
		table := make([]byte, 4*count)
		if _, err := r.ReadAt(table, 12); err != nil {
			return nil
		}
		offsets = offsets[:0]
		for i := range count {
			offsets = append(offsets, binary.BigEndian.Uint32(table[4*i:]))
		}
	}
	var faces []face
	for _, offset := range offsets {
		if f, ok := faceAt(r, int64(offset)); ok {
			faces = append(faces, f)
		}
	}
	return faces
}

func faceAt(r io.ReaderAt, offset int64) (face, bool) {
	header := make([]byte, 12)
	if _, err := r.ReadAt(header, offset); err != nil {
		return face{}, false
	}
	numTables := int(binary.BigEndian.Uint16(header[4:6]))
	if numTables == 0 || numTables > maxTables {
		return face{}, false
	}
	records := make([]byte, 16*numTables)
	if _, err := r.ReadAt(records, offset+12); err != nil {
		return face{}, false
	}
	for i := range numTables {
		record := records[16*i:]
		if string(record[:4]) == "name" {
			start := binary.BigEndian.Uint32(record[8:12])
			length := binary.BigEndian.Uint32(record[12:16])
			return faceFromNameTable(r, int64(start), int64(length))
		}
	}
	return face{}, false
}

// Name IDs: 1 is the family name GDI, Aegisub and libass all match against;
// libass also accepts the typographic family (16) and the full name (4).
const (
	nameIDFamily            = 1
	nameIDFullName          = 4
	nameIDTypographicFamily = 16
)

// languageRank orders the Windows-platform language IDs: the UI is Chinese,
// so a font's Chinese name wins over its English one when it has both.
func languageRank(language uint16) int {
	switch language {
	case 0x0804: // zh-CN
		return 0
	case 0x1004, 0x0404, 0x0C04, 0x1404: // zh-SG, zh-TW, zh-HK, zh-MO
		return 1
	case 0x0411: // ja-JP
		return 2
	case 0x0409: // en-US
		return 3
	}
	return 4
}

func faceFromNameTable(r io.ReaderAt, start, length int64) (face, bool) {
	if length < 6 || length > 1<<20 {
		return face{}, false
	}
	table := make([]byte, length)
	if _, err := r.ReadAt(table, start); err != nil {
		return face{}, false
	}
	count := int(binary.BigEndian.Uint16(table[2:4]))
	storage := int(binary.BigEndian.Uint16(table[4:6]))
	var result face
	seen := make(map[string]bool)
	bestRank := 1 << 30
	for i := range count {
		at := 6 + 12*i
		if at+12 > len(table) {
			break
		}
		record := table[at : at+12]
		platform := binary.BigEndian.Uint16(record[0:2])
		language := binary.BigEndian.Uint16(record[4:6])
		nameID := binary.BigEndian.Uint16(record[6:8])
		if nameID != nameIDFamily && nameID != nameIDFullName && nameID != nameIDTypographicFamily {
			continue
		}
		size := int(binary.BigEndian.Uint16(record[8:10]))
		from := storage + int(binary.BigEndian.Uint16(record[10:12]))
		if size == 0 || from+size > len(table) {
			continue
		}
		raw := table[from : from+size]
		var name string
		var rank int
		switch platform {
		case 3: // Windows: UTF-16BE
			name, rank = decodeUTF16BE(raw), languageRank(language)
		case 1: // Macintosh: only trust English, which is plain ASCII in Mac Roman
			if language != 0 || !isASCII(raw) {
				continue
			}
			name, rank = string(raw), 5
		default:
			continue
		}
		name = strings.TrimSpace(name)
		if name == "" || strings.HasPrefix(name, "@") {
			continue
		}
		key := strings.ToLower(name)
		if !seen[key] {
			seen[key] = true
			if nameID == nameIDFullName {
				result.fullNames = append(result.fullNames, key)
			} else {
				result.families = append(result.families, key)
			}
		}
		if nameID == nameIDFamily && rank < bestRank {
			result.family, bestRank = name, rank
		}
	}
	return result, result.family != ""
}

func isASCII(raw []byte) bool {
	for _, b := range raw {
		if b >= 0x80 {
			return false
		}
	}
	return true
}

func decodeUTF16BE(raw []byte) string {
	units := make([]uint16, len(raw)/2)
	for i := range units {
		units[i] = binary.BigEndian.Uint16(raw[2*i:])
	}
	return strings.TrimRight(string(utf16.Decode(units)), "\x00")
}
