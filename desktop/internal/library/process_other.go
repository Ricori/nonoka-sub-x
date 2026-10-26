//go:build !windows

package library

import "os/exec"

func configureMediaCommand(_ *exec.Cmd) {}

func configureBackgroundCommand(_ *exec.Cmd) {}
