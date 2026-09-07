// Package managedtools resolves project-managed command-line dependencies.
package managedtools

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/Ricori/nonoka-x/desktop/internal/storage"
)

// PythonEnvironment overrides the interpreter Nonoka X runs Python code with.
const PythonEnvironment = "NONOKA_PYTHON"

// Find prefers an existing system tool and falls back to the verified copy in
// Nonoka X's application-data runtime. The active version pointer is written
// atomically by the Python provisioner after download and checksum validation.
func Find(dataDirectory, name string) (string, error) {
	if executable, err := exec.LookPath(name); err == nil {
		return executable, nil
	}
	if executable := findManaged(dataDirectory, name); executable != "" {
		return executable, nil
	}
	return "", errors.New(name + " is not installed")
}

func findManaged(dataDirectory, name string) string {
	filename := name
	if runtime.GOOS == "windows" {
		filename += ".exe"
	}
	resourceIDs := []string{name}
	if name == "ffprobe" {
		// The Windows FFmpeg archive contains both tools in one resource.
		resourceIDs = append(resourceIDs, "ffmpeg")
	}
	for _, resourceID := range resourceIDs {
		root := filepath.Join(storage.RuntimeDirectory(dataDirectory), "runtime", resourceID)
		version := activeVersion(filepath.Join(root, "current.json"))
		if version == "" {
			continue
		}
		active := filepath.Join(root, version)
		var result string
		_ = filepath.WalkDir(active, func(path string, entry os.DirEntry, err error) error {
			if err != nil || result != "" {
				return filepath.SkipAll
			}
			if entry.IsDir() || !strings.EqualFold(entry.Name(), filename) {
				return nil
			}
			info, infoErr := entry.Info()
			if infoErr != nil || !info.Mode().IsRegular() {
				return nil
			}
			if runtime.GOOS != "windows" && info.Mode().Perm()&0o111 == 0 {
				return nil
			}
			result = path
			return filepath.SkipAll
		})
		if result != "" {
			return result
		}
	}
	return ""
}

func activeVersion(pointer string) string {
	data, err := os.ReadFile(pointer)
	if err != nil {
		return ""
	}
	var value struct {
		Current string `json:"current"`
	}
	if json.Unmarshal(data, &value) != nil || value.Current == "" || filepath.Base(value.Current) != value.Current || strings.ContainsAny(value.Current, `/\\`) {
		return ""
	}
	return value.Current
}

// Everything below resolves yt-dlp, which ships as a Python wheel.

// BootstrapPython locates the interpreter Nonoka X installs for itself.
func BootstrapPython(dataDirectory string) string {
	return VenvPython(filepath.Join(dataDirectory, "bootstrap", "launcher"))
}

// VenvPython names the interpreter inside a virtual environment rooted at
// directory. The bootstrap builds its launcher in a staging directory before
// moving it into place, so the layout has to be nameable for both.
func VenvPython(directory string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(directory, "Scripts", "python.exe")
	}
	return filepath.Join(directory, "bin", "python3")
}

// YTDLP describes how to invoke yt-dlp: the executable to run, arguments that
// must precede yt-dlp's own, and extra environment variables the command needs.
// A native binary needs neither of the latter two.
type YTDLP struct {
	Executable  string
	Prefix      []string
	Environment []string
}

// FindYTDLP resolves how to run yt-dlp. Nonoka X installs it as a Python wheel
// rather than a standalone binary, so Find alone cannot see it: the package is
// imported through the managed interpreter with PYTHONPATH pointing at the
// unpacked wheel, the same way nonoka_x.provision runs it for the FineSub
// pipeline. A real binary still wins, so swapping the resource back to a
// standalone build needs no change here.
func FindYTDLP(dataDirectory string) (YTDLP, error) {
	if native, err := Find(dataDirectory, "yt-dlp"); err == nil {
		return YTDLP{Executable: native}, nil
	}
	notInstalled := errors.New("Nonoka X yt-dlp is not installed; install it from 运行环境 first")
	root := filepath.Join(storage.RuntimeDirectory(dataDirectory), "runtime", "yt-dlp")
	version := activeVersion(filepath.Join(root, "current.json"))
	if version == "" {
		return YTDLP{}, notInstalled
	}
	// The runtime manifest declares yt_dlp/__init__.py as the resource's
	// required file, so provisioner and host agree on what "installed" means.
	packageRoot := filepath.Join(root, version)
	if info, err := os.Stat(filepath.Join(packageRoot, "yt_dlp", "__init__.py")); err != nil || !info.Mode().IsRegular() {
		return YTDLP{}, notInstalled
	}
	python := findPython(dataDirectory)
	if python == "" {
		return YTDLP{}, errors.New("no Python interpreter is available to run yt-dlp; finish the Python runtime setup in 运行环境 first")
	}
	// -s keeps user site-packages out of the run; PYTHONPATH makes the unpacked
	// wheel importable without installing it into the interpreter.
	return YTDLP{
		Executable:  python,
		Prefix:      []string{"-s", "-m", "yt_dlp"},
		Environment: []string{pythonPath(packageRoot)},
	}, nil
}

// potProviderDirectory holds both halves of the PO token provider. Upstream
// versions the yt-dlp plugin and the generator together, so Nonoka X ships them as
// one bundle: the two resolvers below point at the same directory and differ
// only in the file each needs to find.
const potProviderDirectory = "pot-provider"

// FindPOTPlugin resolves the unpacked bgutil PO token plugin. It is a yt-dlp
// plugin package rather than a tool: yt-dlp discovers it by importing
// `yt_dlp_plugins` off sys.path, so the caller adds this directory to
// PYTHONPATH instead of executing anything in it.
func FindPOTPlugin(dataDirectory string) (string, error) {
	notInstalled := errors.New("the PO Token provider is not installed; install it from 运行环境 first")
	root := filepath.Join(storage.RuntimeDirectory(dataDirectory), "runtime", potProviderDirectory)
	version := activeVersion(filepath.Join(root, "current.json"))
	if version == "" {
		return "", notInstalled
	}
	packageRoot := filepath.Join(root, version)
	// The same file the runtime manifest declares as the resource's required
	// file, so provisioner and host agree on what "installed" means.
	marker := filepath.Join(packageRoot, "yt_dlp_plugins", "extractor", "getpot_bgutil_http.py")
	if info, err := os.Stat(marker); err != nil || !info.Mode().IsRegular() {
		return "", notInstalled
	}
	return packageRoot, nil
}

// FindPOTServer resolves the PO token generation bundle. The provider's own
// backend cannot be expressed as a single pinned archive upstream -- it needs an
// npm install with a native module -- so Nonoka X installs a prebuilt bundle and
// points yt-dlp's plugin at this directory with `server_home`. Script mode is
// deliberate: it spawns the generator per call, so a desktop install carries no
// long-running daemon and no listening port.
func FindPOTServer(dataDirectory string) (string, error) {
	notInstalled := errors.New("the PO Token provider is not installed; install it from 运行环境 first")
	root := filepath.Join(storage.RuntimeDirectory(dataDirectory), "runtime", potProviderDirectory)
	version := activeVersion(filepath.Join(root, "current.json"))
	if version == "" {
		return "", notInstalled
	}
	home := filepath.Join(root, version)
	// The generator the plugin actually invokes; the manifest declares the same
	// file, so provisioner and host agree on what "installed" means.
	if info, err := os.Stat(filepath.Join(home, "build", "generate_once.js")); err != nil || !info.Mode().IsRegular() {
		return "", notInstalled
	}
	return home, nil
}

// AddPythonPath puts directory at the front of the environment's PYTHONPATH,
// creating the entry when the command carries none. yt-dlp's own wheel already
// arrives this way, so a plugin package has to join it rather than replace it.
func AddPythonPath(environment []string, directory string) []string {
	const key = "PYTHONPATH="
	for index, entry := range environment {
		if strings.HasPrefix(entry, key) {
			existing := strings.TrimPrefix(entry, key)
			environment[index] = key + directory + string(os.PathListSeparator) + existing
			return environment
		}
	}
	return append(environment, pythonPath(directory))
}

// pythonPath prepends the unpacked wheel to any PYTHONPATH already inherited
// from the environment rather than replacing it.
func pythonPath(packageRoot string) string {
	if inherited := os.Getenv("PYTHONPATH"); inherited != "" {
		return "PYTHONPATH=" + packageRoot + string(os.PathListSeparator) + inherited
	}
	return "PYTHONPATH=" + packageRoot
}

func findPython(dataDirectory string) string {
	candidates := []string{os.Getenv(PythonEnvironment), BootstrapPython(dataDirectory), "python3.12", "python3", "python"}
	for _, candidate := range candidates {
		if candidate == "" {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return candidate
		}
		// NONOKA_PYTHON may name an interpreter on PATH instead of giving a
		// path, which is how SidecarConfig already accepts it.
		if executable, err := exec.LookPath(candidate); err == nil {
			return executable
		}
	}
	return ""
}
