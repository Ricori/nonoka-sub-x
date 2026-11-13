// Package storage records where Nonoka X keeps the two directories that grow
// large enough to fill a system drive: the big-file root `runtime` (the prebuilt
// Python, model and tool layers and the download cache) and the managed video
// cache.
//
// Everything else Nonoka X writes — the library index, preferences, documents,
// task history — is small and stays in the application data directory, which
// is also where the record of these two choices lives. That keeps the record
// findable without needing a record of its own.
package storage

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
)

// Relocatable targets. The values are stable: they cross the Wails bridge and
// are part of storage.json's shape.
const (
	RuntimeTarget = "runtime"
	VideoTarget   = "video"
)

const (
	fileName    = "storage.json"
	runtimeName = "runtime"
	videoName   = "videos"
	// containerName is the folder a relocation creates inside the directory the
	// user picked. Picking a drive root is the common case, and scattering
	// "runtime" and "videos" over E:\ is not what anyone means by it.
	containerName = "Nonoka X"
)

// Locations is storage.json. An empty directory means "still the default",
// which keeps a fresh install free of absolute paths that would outlive the
// machine they were written on.
type Locations struct {
	Schema     int    `json:"schema"`
	RuntimeDir string `json:"runtimeDir"`
	VideoDir   string `json:"videoDir"`
}

// writes serialises the read-modify-write in Set. Readers need no lock: the
// record is replaced atomically, so a concurrent Load sees one version or the
// other and never a half-written one.
var writes sync.Mutex

func recordPath(dataDirectory string) string {
	return filepath.Join(dataDirectory, fileName)
}

// Load reports the recorded locations. A missing or unreadable record is not an
// error: it is what every installation starts with, and a corrupt one is worth
// less than the defaults it would otherwise hide.
func Load(dataDirectory string) Locations {
	locations := Locations{Schema: 1}
	data, err := os.ReadFile(recordPath(dataDirectory))
	if err != nil {
		return locations
	}
	if json.Unmarshal(data, &locations) != nil {
		return Locations{Schema: 1}
	}
	locations.Schema = 1
	locations.RuntimeDir = cleanRecorded(locations.RuntimeDir)
	locations.VideoDir = cleanRecorded(locations.VideoDir)
	return locations
}

// cleanRecorded drops anything that cannot be a location. A relative path in
// the record would resolve against whatever directory the process happens to be
// in, so it is treated as absent rather than honoured.
func cleanRecorded(value string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" || !filepath.IsAbs(trimmed) {
		return ""
	}
	return filepath.Clean(trimmed)
}

// Default is where a target lives when nothing has been relocated.
func Default(dataDirectory, target string) string {
	switch target {
	case RuntimeTarget:
		return filepath.Join(dataDirectory, runtimeName)
	case VideoTarget:
		return filepath.Join(dataDirectory, videoName)
	}
	return ""
}

// Directory resolves a target against the record.
//
// A recorded directory that has since gone missing — an external disk left
// unplugged — is still returned. Silently falling back to the default would
// hide an installed 15 GB runtime and invite the user to download it again; the
// settings page reports the missing path instead.
func Directory(dataDirectory, target string) string {
	return resolve(dataDirectory, target, Load(dataDirectory))
}

func resolve(dataDirectory, target string, locations Locations) string {
	recorded := ""
	switch target {
	case RuntimeTarget:
		recorded = locations.RuntimeDir
	case VideoTarget:
		recorded = locations.VideoDir
	default:
		return ""
	}
	if recorded != "" {
		return recorded
	}
	return Default(dataDirectory, target)
}

// RuntimeDirectory is the big-file root: layers\<layer>\<version>\ with each
// layer's current.json, and cache\downloads\. It moves as one directory, and
// nothing in it records an absolute path, so a move needs no repair afterwards
// (plan 4.2 item 17).
func RuntimeDirectory(dataDirectory string) string {
	return Directory(dataDirectory, RuntimeTarget)
}

// VideoDirectory is the managed working-copy cache for imported media.
func VideoDirectory(dataDirectory string) string {
	return Directory(dataDirectory, VideoTarget)
}

// Set records directory for target, or restores the default when directory is
// empty. It only writes the record: moving what is already on disk is the
// caller's job, and doing that afterwards would strand the data.
func Set(dataDirectory, target, directory string) (Locations, error) {
	if target != RuntimeTarget && target != VideoTarget {
		return Locations{}, errors.New("unknown storage target")
	}
	value := strings.TrimSpace(directory)
	if value != "" {
		absolute, err := filepath.Abs(value)
		if err != nil {
			return Locations{}, err
		}
		value = absolute
		// The default is recorded as "no choice at all", so a data directory
		// its owner later moves keeps taking its children along with it.
		if sameLocation(value, Default(dataDirectory, target)) {
			value = ""
		}
	}
	writes.Lock()
	defer writes.Unlock()
	locations := Load(dataDirectory)
	switch target {
	case RuntimeTarget:
		locations.RuntimeDir = value
	case VideoTarget:
		locations.VideoDir = value
	}
	if err := save(dataDirectory, locations); err != nil {
		return Locations{}, err
	}
	return locations, nil
}

func save(dataDirectory string, locations Locations) error {
	locations.Schema = 1
	data, err := json.MarshalIndent(locations, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dataDirectory, 0o755); err != nil {
		return err
	}
	temporary := recordPath(dataDirectory) + ".tmp"
	if err := os.WriteFile(temporary, append(data, '\n'), 0o600); err != nil {
		return err
	}
	if err := os.Rename(temporary, recordPath(dataDirectory)); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

// DestinationFor turns the directory the user picked in the folder dialog into
// the directory Nonoka X will own. The payload always lands one level down inside
// a "Nonoka X" folder, which is what makes picking a drive root safe: the
// relocation can create and remove its own directory without ever touching a
// folder the user keeps other things in.
func DestinationFor(picked, target string) (string, error) {
	name := ""
	switch target {
	case RuntimeTarget:
		name = runtimeName
	case VideoTarget:
		name = videoName
	default:
		return "", errors.New("unknown storage target")
	}
	parent, err := filepath.Abs(strings.TrimSpace(picked))
	if err != nil {
		return "", err
	}
	// Picking the container a previous relocation made should not nest a second
	// one inside it.
	if !strings.EqualFold(filepath.Base(parent), containerName) {
		parent = filepath.Join(parent, containerName)
	}
	return filepath.Join(parent, name), nil
}

// ValidateDestination rejects the moves that would corrupt an installation
// rather than relocate it.
func ValidateDestination(dataDirectory, target, destination string) error {
	if !filepath.IsAbs(destination) {
		return errors.New("目标目录必须是绝对路径")
	}
	current := Directory(dataDirectory, target)
	if sameLocation(destination, current) {
		return errors.New("目标目录就是当前目录")
	}
	// Copying a directory into itself is the one mistake that destroys the
	// source while it is still being read.
	if within(destination, current) {
		return errors.New("目标目录不能位于当前目录内部")
	}
	other := RuntimeTarget
	if target == RuntimeTarget {
		other = VideoTarget
	}
	otherDirectory := Directory(dataDirectory, other)
	if sameLocation(destination, otherDirectory) || within(destination, otherDirectory) || within(otherDirectory, destination) {
		return errors.New("目标目录与另一个 Nonoka X 目录重叠")
	}
	if sameLocation(destination, dataDirectory) {
		return errors.New("目标目录不能是 Nonoka X 数据目录本身")
	}
	info, err := os.Stat(destination)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if !info.IsDir() {
		return errors.New("目标位置已存在同名文件")
	}
	entries, err := os.ReadDir(destination)
	if err != nil {
		return err
	}
	if len(entries) == 0 {
		return nil
	}
	// Nonoka X's own unfinished copy is the one non-empty directory worth
	// accepting: refusing it would throw away everything a cancelled or
	// interrupted relocation already carried across.
	if _, resumable := ResumableCopy(current, destination); resumable {
		return nil
	}
	return errors.New("目标目录已存在且不为空，请换一个位置")
}

// sameLocation compares two paths the way the filesystem would: Windows paths
// are case-insensitive, and a trailing separator never changes which directory
// is meant.
func sameLocation(left, right string) bool {
	if left == "" || right == "" {
		return false
	}
	left = filepath.Clean(left)
	right = filepath.Clean(right)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(left, right)
	}
	return left == right
}

// within reports whether child lies inside parent.
func within(child, parent string) bool {
	if child == "" || parent == "" {
		return false
	}
	relative, err := filepath.Rel(filepath.Clean(parent), filepath.Clean(child))
	if err != nil {
		return false
	}
	if relative == "." {
		return true
	}
	if runtime.GOOS == "windows" {
		relative = strings.ToLower(relative)
	}
	return !strings.HasPrefix(relative, "..") && !filepath.IsAbs(relative)
}
