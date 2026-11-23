//go:build windows

package library

import (
	"syscall"
	"unsafe"
)

var (
	kernel32DLL         = syscall.NewLazyDLL("kernel32.dll")
	getDiskFreeSpaceExW = kernel32DLL.NewProc("GetDiskFreeSpaceExW")
)

func diskFreeBytes(dir string) (uint64, error) {
	ptr, err := syscall.UTF16PtrFromString(dir)
	if err != nil {
		return 0, err
	}
	var freeBytesAvailable uint64
	r1, _, callErr := getDiskFreeSpaceExW.Call(
		uintptr(unsafe.Pointer(ptr)),
		uintptr(unsafe.Pointer(&freeBytesAvailable)),
		0,
		0,
	)
	if r1 == 0 {
		return 0, callErr
	}
	return freeBytesAvailable, nil
}
