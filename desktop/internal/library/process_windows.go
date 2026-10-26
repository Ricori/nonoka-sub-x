//go:build windows

package library

import (
	"os/exec"
	"syscall"
)

// belowNormalPriorityClass is the BELOW_NORMAL_PRIORITY_CLASS process creation flag.
const belowNormalPriorityClass = 0x00004000

func configureMediaCommand(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
}

// configureBackgroundCommand is for work nobody is waiting on (filmstrips,
// proxies): it yields the CPU to the editor's own playback and decoding.
func configureBackgroundCommand(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: belowNormalPriorityClass}
}
