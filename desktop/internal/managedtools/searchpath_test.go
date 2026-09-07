package managedtools

import (
	"slices"
	"testing"
)

// Written against POSIX paths on purpose: the widening only ever runs on macOS,
// and a Windows temp directory carries a drive letter the ":" separator would
// split in half.
func TestAugmentedSearchPathAppendsOnlyMissingExistingDirectories(t *testing.T) {
	installed := []string{"/opt/homebrew/bin", "/usr/local/bin"}
	exists := func(candidate string) bool { return slices.Contains(installed, candidate) }

	widened := augmentedSearchPath("/usr/bin:/usr/local/bin", []string{
		"/opt/homebrew/bin",  // installed and missing from PATH: appended
		"/usr/local/bin",     // already on PATH: not duplicated
		"/opt/homebrew/sbin", // not installed: left out
	}, exists)

	if want := "/usr/bin:/usr/local/bin:/opt/homebrew/bin"; widened != want {
		t.Fatalf("PATH = %q, want %q", widened, want)
	}
}

// A machine with none of the supplements installed must come back byte-for-byte
// unchanged: an empty appended entry is read as the current directory.
func TestAugmentedSearchPathLeavesAnUnchangedPathAlone(t *testing.T) {
	current := "/usr/bin:/bin"
	widened := augmentedSearchPath(current, []string{"/opt/homebrew/bin"}, func(string) bool { return false })
	if widened != current {
		t.Fatalf("PATH = %q, want %q", widened, current)
	}
}
