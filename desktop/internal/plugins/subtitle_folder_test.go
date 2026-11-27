package plugins

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSubtitleFolderRequiresExportPermission(t *testing.T) {
	service, _, _ := engineService(t, `"media.list"`)
	if _, err := service.SubtitleFolder("dev.nonoka.hello"); err == nil {
		t.Fatal("reading the output folder needs subtitle.export")
	}
	if _, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "a.srt", "1\n"); err == nil {
		t.Fatal("writing into the output folder needs subtitle.export")
	}
}

func TestSaveSubtitleToFolderNeedsAChosenFolder(t *testing.T) {
	service, _, _ := engineService(t, `"subtitle.export"`)
	folder, err := service.SubtitleFolder("dev.nonoka.hello")
	if err != nil || folder != "" {
		t.Fatalf("no folder should be remembered yet: %q, %v", folder, err)
	}
	if _, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "a.srt", "1\n"); err == nil {
		t.Fatal("saving before a folder was chosen should fail")
	}
}

func TestSaveSubtitleToFolderWritesNewFilesOnly(t *testing.T) {
	service, _, _ := engineService(t, `"subtitle.export"`)
	output := t.TempDir()
	if _, err := service.rememberSubtitleFolder("dev.nonoka.hello", output); err != nil {
		t.Fatal(err)
	}
	folder, err := service.SubtitleFolder("dev.nonoka.hello")
	if err != nil || folder != output {
		t.Fatalf("remembered folder = %q, %v; want %q", folder, err, output)
	}

	first, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "Ep 1.srt", "first")
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "Ep 1.srt", "second")
	if err != nil {
		t.Fatal(err)
	}
	if first != filepath.Join(output, "Ep 1.srt") || second != filepath.Join(output, "Ep 1 (2).srt") {
		t.Fatalf("unexpected paths: %q, %q", first, second)
	}
	if content, _ := os.ReadFile(first); string(content) != "first" {
		t.Fatalf("the first file was overwritten: %q", content)
	}

	// The plugin names a file, never a directory.
	escaped, err := service.SaveSubtitleToFolder("dev.nonoka.hello", `..\..\evil.srt`, "x")
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Dir(escaped) != output {
		t.Fatalf("a path in the name escaped the folder: %q", escaped)
	}
	if _, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "empty.srt", "  \n"); err == nil {
		t.Fatal("empty subtitle content should be refused")
	}

	if err := os.RemoveAll(output); err != nil {
		t.Fatal(err)
	}
	if folder, _ := service.SubtitleFolder("dev.nonoka.hello"); folder != "" {
		t.Fatalf("a removed folder should read as not chosen, got %q", folder)
	}
	if _, err := service.SaveSubtitleToFolder("dev.nonoka.hello", "late.srt", "x"); err == nil {
		t.Fatal("saving into a removed folder should fail")
	}
}

func TestSafeSubtitleFileName(t *testing.T) {
	cases := map[string]string{
		`【歌枠】a/b:c?.srt`: "b_c_.srt",
		`a<b>"c"|d*.SRT`: "a_b__c__d_.srt",
		"trailing. .srt": "trailing.srt",
		"CON.srt":        "_CON.srt",
		"com3.srt":       "_com3.srt",
		"console.srt":    "console.srt",
		"":               "subtitle.ass",
		"???.srt":        "___.srt",
		"notes.txt":      "notes.txt.ass",
	}
	for input, want := range cases {
		if got := safeSubtitleFileName(input); got != want {
			t.Errorf("safeSubtitleFileName(%q) = %q, want %q", input, got, want)
		}
	}
}
