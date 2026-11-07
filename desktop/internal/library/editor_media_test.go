package library

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestEditorExportAudioArgs(t *testing.T) {
	mp4, mkv := exportContainers["mp4"], exportContainers["mkv"]
	aac192 := []string{"-c:a", "aac", "-b:a", "192k"}
	copyArgs := []string{"-c:a", "copy", "-copypriorss", "0"}
	tests := []struct {
		name      string
		option    string
		container exportContainer
		joined    bool
		want      []string
	}{
		{name: "copies source audio", option: "copy", container: mp4, want: copyArgs},
		{name: "copies source audio into mkv", option: "copy", container: mkv, want: copyArgs},
		{name: "encodes 320k AAC", option: "320k", container: mp4, want: []string{"-c:a", "aac", "-b:a", "320k"}},
		{name: "keeps existing 256k option", option: "256k", container: mkv, want: []string{"-c:a", "aac", "-b:a", "256k"}},
		{name: "defaults invalid input", option: "lossless", container: mp4, want: aac192},
		{name: "encodes flac into mkv", option: "flac", container: mkv, want: []string{"-c:a", "flac"}},
		{name: "keeps flac out of mp4", option: "flac", container: mp4, want: aac192},
		{name: "joined mp4 copy falls back to AAC", option: "copy", container: mp4, joined: true, want: aac192},
		{name: "joined mkv copy stays lossless", option: "copy", container: mkv, joined: true, want: []string{"-c:a", "flac"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := editorExportAudioArgs(test.option, test.container, test.joined); !reflect.DeepEqual(got, test.want) {
				t.Fatalf("editorExportAudioArgs(%q) = %#v; want %#v", test.option, got, test.want)
			}
		})
	}
}

func TestExportContainerFor(t *testing.T) {
	if got := exportContainerFor("MKV"); got.muxer != "matroska" || got.ext != ".mkv" {
		t.Fatalf("mkv container = %#v", got)
	}
	if got := exportContainerFor("avi"); got.name != "mp4" {
		t.Fatalf("unknown container = %#v; want mp4", got)
	}
}

func TestExportFileName(t *testing.T) {
	mkv := exportContainers["mkv"]
	for input, want := range map[string]string{
		"clip - 字幕.mp4": "clip - 字幕.mkv",
		"clip - 字幕.MKV": "clip - 字幕.mkv",
		"clip - 字幕":     "clip - 字幕.mkv",
		"ep.01":         "ep.01.mkv",
	} {
		if got := exportFileName(input, mkv); got != want {
			t.Fatalf("exportFileName(%q) = %q; want %q", input, got, want)
		}
	}
}

func TestBuildEditorExportArgsSinglePieceMKV(t *testing.T) {
	args := strings.Join(buildEditorExportArgs(editorExportJob{
		video: "cache.mp4", audio: "source.mkv", pieces: []Range{{T0: 10, T1: 25}},
		filter: "subtitles=subtitle.ass", crf: 21, preset: "medium",
		audioArgs: editorExportAudioArgs("copy", exportContainers["mkv"], false), container: exportContainers["mkv"], output: "out.mkv.part.mkv",
	}), " ")
	for _, part := range []string{
		"-ss 10.000 -t 15.000 -i cache.mp4 -ss 10.000 -t 15.000 -i source.mkv",
		"-vf subtitles=subtitle.ass -map 0:V:0 -map 1:a?",
		"-sn -dn -c:v libx264",
		"-c:a copy -copypriorss 0 -f matroska -progress pipe:1 -nostats out.mkv.part.mkv",
	} {
		if !strings.Contains(args, part) {
			t.Fatalf("args missing %q:\n%s", part, args)
		}
	}
}

func TestBuildEditorExportArgsSameSourceMP4(t *testing.T) {
	args := strings.Join(buildEditorExportArgs(editorExportJob{
		video: "source.mp4", audio: "source.mp4", pieces: []Range{{T0: 0, T1: 5}},
		filter: "subtitles=subtitle.ass", crf: 21, preset: "medium",
		audioArgs: []string{"-c:a", "aac", "-b:a", "192k"}, container: exportContainers["mp4"], output: "out.part.mp4",
	}), " ")
	if strings.Count(args, " -i ") != 1 || !strings.Contains(args, "-map 0:V:0 -map 0:a:0?") || !strings.Contains(args, "-f mp4") {
		t.Fatalf("args = %s", args)
	}
}

func TestBuildEditorExportArgsJoinedPieces(t *testing.T) {
	args := strings.Join(buildEditorExportArgs(editorExportJob{
		video: "cache.mp4", audio: "source.mkv", pieces: []Range{{T0: 10, T1: 12}, {T0: 15, T1: 20}},
		hasAudio: true, filter: "subtitles=subtitle.ass", crf: 21, preset: "medium",
		audioArgs: []string{"-c:a", "flac"}, container: exportContainers["mkv"], output: "out.part.mkv",
	}), " ")
	if !strings.Contains(args, "-ss 10.000 -t 10.000 -i source.mkv -/filter_complex graph.txt -map [vo] -map [ac] -sn") ||
		strings.Contains(args, "-vf") {
		t.Fatalf("args = %s", args)
	}
}

func TestExportAudioSourcePrefersOriginal(t *testing.T) {
	directory := t.TempDir()
	original := filepath.Join(directory, "source.mkv")
	cache := filepath.Join(directory, "cache.mp4")
	for _, path := range []string{original, cache} {
		if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if got := exportAudioSource(Entry{SourcePath: original}, cache); got != original {
		t.Fatalf("audio source = %q; want original", got)
	}
	if got := exportAudioSource(Entry{SourcePath: original}, original); got != original {
		t.Fatalf("audio source = %q; want original", got)
	}
	if got := exportAudioSource(Entry{SourcePath: filepath.Join(directory, "gone.mkv")}, cache); got != cache {
		t.Fatalf("audio source = %q; want cache when the original is gone", got)
	}
}

func TestParseExportProgress(t *testing.T) {
	done, ok := parseExportProgress("out_time_us=12500000", 20)
	if !ok || done != 12.5 {
		t.Fatalf("progress = %v, %v; want 12.5, true", done, ok)
	}
	done, ok = parseExportProgress("out_time_us=25000000", 20)
	if !ok || done != 20 {
		t.Fatalf("clamped progress = %v, %v; want 20, true", done, ok)
	}
}

func TestParseExportProgressRejectsOtherOutput(t *testing.T) {
	for _, line := range []string{"frame=12", "out_time_us=invalid", "out_time_us=-1"} {
		if _, ok := parseExportProgress(line, 20); ok {
			t.Fatalf("accepted invalid progress line %q", line)
		}
	}
}

func TestCancelExport(t *testing.T) {
	service := &Service{
		exportCancels: make(map[string]context.CancelFunc),
	}
	cancelled := false
	id := "loc_0123456789ab"
	service.exportCancels[id] = func() { cancelled = true }

	if err := service.CancelExport("invalid"); err == nil {
		t.Fatal("expected error for invalid id")
	}
	if err := service.CancelExport(id); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !cancelled {
		t.Fatal("expected cancel func to be called")
	}
}
