package app

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func makeOfflinePack(t *testing.T, directories ...string) string {
	t.Helper()
	pack := t.TempDir()
	if err := os.WriteFile(filepath.Join(pack, "pack.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, directory := range directories {
		if err := os.MkdirAll(filepath.Join(pack, directory), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv(offlinePackEnvironment, pack)
	return pack
}

func TestOfflinePackNeedsItsManifest(t *testing.T) {
	t.Setenv(offlinePackEnvironment, t.TempDir())
	if got := offlinePack(); got != "" {
		t.Fatalf("a directory without pack.json is not a package: %q", got)
	}
	pack := makeOfflinePack(t)
	if got := offlinePack(); got != pack {
		t.Fatalf("offlinePack = %q, want %q", got, pack)
	}
}

func TestOfflineRungComesFirstOnlyWhenThePackageHasTheDirectory(t *testing.T) {
	makeOfflinePack(t, "python")
	attempts := bootstrapAttempts("")
	if got := offlineRung(attempts, offlineSubdirectory("python")); !got[0].Local || len(got) != len(attempts)+1 {
		t.Fatalf("local rung missing: %#v", got)
	}
	if got := offlineRung(attempts, offlineSubdirectory("launcher")); !slices.Equal(got, attempts) {
		t.Fatalf("no such directory should add no rung: %#v", got)
	}
	t.Setenv(offlinePackEnvironment, "")
	if got := offlineRung(attempts, ""); !slices.Equal(got, attempts) {
		t.Fatalf("no package should add no rung: %#v", got)
	}
}

func TestSeedFindsAFileByItsDigest(t *testing.T) {
	const digest = "abc123"
	pack := makeOfflinePack(t, filepath.Join("files", digest))
	if err := os.WriteFile(filepath.Join(pack, "files", digest, "uv.zip"), []byte("payload"), 0o600); err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(t.TempDir(), "uv.zip")
	if !seedFromOfflinePack("ABC123", destination) {
		t.Fatal("the file should have been found")
	}
	if data, err := os.ReadFile(destination); err != nil || string(data) != "payload" {
		t.Fatalf("copied %q, %v", data, err)
	}
	if seedFromOfflinePack("other", destination+".2") {
		t.Fatal("a digest the package lacks should not be found")
	}
}

func TestFileURIEncodesThePath(t *testing.T) {
	cases := map[string]string{
		`D:/离线 包/python`:   "file:///D:/%E7%A6%BB%E7%BA%BF%20%E5%8C%85/python",
		`/opt/pack/python`: "file:///opt/pack/python",
	}
	for path, want := range cases {
		if got := fileURI(path); got != want {
			t.Errorf("fileURI(%q) = %q, want %q", path, got, want)
		}
	}
}
