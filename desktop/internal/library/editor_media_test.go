package library

import (
	"context"
	"reflect"
	"testing"
)

func TestEditorExportAudioArgs(t *testing.T) {
	tests := []struct {
		name   string
		option string
		want   []string
	}{
		{name: "copies source audio", option: "copy", want: []string{"-c:a", "copy"}},
		{name: "encodes 320k AAC", option: "320k", want: []string{"-c:a", "aac", "-b:a", "320k"}},
		{name: "keeps existing 256k option", option: "256k", want: []string{"-c:a", "aac", "-b:a", "256k"}},
		{name: "defaults invalid input", option: "lossless", want: []string{"-c:a", "aac", "-b:a", "192k"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := editorExportAudioArgs(test.option); !reflect.DeepEqual(got, test.want) {
				t.Fatalf("editorExportAudioArgs(%q) = %#v; want %#v", test.option, got, test.want)
			}
		})
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
