package sysfonts

import (
	"bytes"
	"encoding/binary"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"unicode/utf16"
)

func readFamilies(r *bytes.Reader) []string {
	var names []string
	for _, f := range readFaces(r) {
		names = append(names, f.family)
	}
	return names
}

type nameRecord struct {
	platform, language, nameID uint16
	value                      string
}

// buildFont assembles a minimal sfnt holding only a `name` table.
func buildFont(records []nameRecord) []byte {
	var storage bytes.Buffer
	var table bytes.Buffer
	be := func(w *bytes.Buffer, v ...uint16) {
		for _, x := range v {
			_ = binary.Write(w, binary.BigEndian, x)
		}
	}
	be(&table, 0, uint16(len(records)), uint16(6+12*len(records)))
	for _, record := range records {
		var raw []byte
		if record.platform == 3 {
			for _, unit := range utf16.Encode([]rune(record.value)) {
				raw = binary.BigEndian.AppendUint16(raw, unit)
			}
		} else {
			raw = []byte(record.value)
		}
		encoding := uint16(1)
		if record.platform == 1 {
			encoding = 0
		}
		be(&table, record.platform, encoding, record.language, record.nameID,
			uint16(len(raw)), uint16(storage.Len()))
		storage.Write(raw)
	}
	table.Write(storage.Bytes())

	var font bytes.Buffer
	_ = binary.Write(&font, binary.BigEndian, uint32(0x00010000))
	be(&font, 1, 0, 0, 0)
	font.WriteString("name")
	_ = binary.Write(&font, binary.BigEndian, []uint32{0, 12 + 16, uint32(table.Len())})
	font.Write(table.Bytes())
	return font.Bytes()
}

func TestPrefersChineseFamilyName(t *testing.T) {
	font := buildFont([]nameRecord{
		{1, 0, 1, "Microsoft YaHei"},
		{3, 0x0409, 1, "Microsoft YaHei"},
		{3, 0x0409, 4, "Microsoft YaHei Regular"},
		{3, 0x0804, 1, "微软雅黑"},
	})
	faces := readFaces(bytes.NewReader(font))
	if len(faces) != 1 || faces[0].family != "微软雅黑" {
		t.Fatalf("faces = %+v, want one named 微软雅黑", faces)
	}
	// Either language must resolve, since a style sheet may spell it either way.
	if !slices.Equal(faces[0].families, []string{"microsoft yahei", "微软雅黑"}) {
		t.Fatalf("family aliases = %q", faces[0].families)
	}
	if !slices.Equal(faces[0].fullNames, []string{"microsoft yahei regular"}) {
		t.Fatalf("full-name aliases = %q", faces[0].fullNames)
	}
}

func TestMiddlewareServesOnlyScannedFonts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "yahei.ttf")
	if err := os.WriteFile(path, []byte("font bytes"), 0o644); err != nil {
		t.Fatal(err)
	}
	service := &Service{files: map[string]string{"微软雅黑": path}}
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) })
	handler := Middleware(service)(next)
	get := func(target string) *httptest.ResponseRecorder {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, target, nil))
		return recorder
	}
	if got := get(RoutePath + "?family=nope&family=%E5%BE%AE%E8%BD%AF%E9%9B%85%E9%BB%91"); got.Code != http.StatusOK || got.Body.String() != "font bytes" {
		t.Fatalf("font request = %d %q", got.Code, got.Body.String())
	}
	if got := get(RoutePath + "?family=" + path); got.Code != http.StatusNotFound {
		t.Fatalf("a path must not be servable, got %d", got.Code)
	}
	if got := get("/index.html"); got.Code != http.StatusTeapot {
		t.Fatalf("other routes must pass through, got %d", got.Code)
	}
}

func TestFallsBackToEnglishThenMac(t *testing.T) {
	english := buildFont([]nameRecord{{3, 0x0407, 1, "Schrift"}, {3, 0x0409, 1, "Font"}})
	if got := readFamilies(bytes.NewReader(english)); len(got) != 1 || got[0] != "Font" {
		t.Fatalf("families = %q, want [Font]", got)
	}
	mac := buildFont([]nameRecord{{1, 0, 1, "Old Mac"}})
	if got := readFamilies(bytes.NewReader(mac)); len(got) != 1 || got[0] != "Old Mac" {
		t.Fatalf("families = %q, want [Old Mac]", got)
	}
}

func TestReadsEveryFaceOfACollection(t *testing.T) {
	first := buildFont([]nameRecord{{3, 0x0409, 1, "One"}})
	second := buildFont([]nameRecord{{3, 0x0409, 1, "Two"}})
	header := 12 + 4*2
	// Table offsets inside each face are relative to the file, so rebase them.
	rebase := func(face []byte, at int) []byte {
		out := append([]byte(nil), face...)
		binary.BigEndian.PutUint32(out[12+8:], uint32(at+12+16))
		return out
	}
	var ttc bytes.Buffer
	ttc.WriteString("ttcf")
	_ = binary.Write(&ttc, binary.BigEndian, []uint32{0x00010000, 2, uint32(header), uint32(header + len(first))})
	ttc.Write(rebase(first, header))
	ttc.Write(rebase(second, header+len(first)))
	got := readFamilies(bytes.NewReader(ttc.Bytes()))
	if len(got) != 2 || got[0] != "One" || got[1] != "Two" {
		t.Fatalf("families = %q, want [One Two]", got)
	}
}

func TestGarbageIsIgnored(t *testing.T) {
	for _, data := range [][]byte{nil, []byte("ttcf\x00\x01\x00\x00\xff\xff\xff\xff"), bytes.Repeat([]byte{0xff}, 64)} {
		if got := readFamilies(bytes.NewReader(data)); len(got) != 0 {
			t.Fatalf("readFamilies(%q) = %q, want none", data, got)
		}
	}
}
