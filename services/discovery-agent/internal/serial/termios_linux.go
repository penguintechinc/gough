//go:build noxdp && linux

package serial

import (
	"os"

	"golang.org/x/sys/unix"
)

const (
	ioctlGetTermios = unix.TCGETS
	ioctlSetTermios = unix.TCSETS
	syscallNoctty   = unix.O_NOCTTY
)

var baudMap = map[int]uint32{
	9600:   unix.B9600,
	19200:  unix.B19200,
	38400:  unix.B38400,
	57600:  unix.B57600,
	115200: unix.B115200,
	230400: unix.B230400,
}

// setSerial configures the tty to the given baud rate with 8N1 framing.
func setSerial(f *os.File, baud int) error {
	t, err := unix.IoctlGetTermios(int(f.Fd()), ioctlGetTermios)
	if err != nil {
		return err
	}
	baudConst, ok := baudMap[baud]
	if !ok {
		baudConst = unix.B115200
	}
	t.Cflag &^= unix.CSIZE | unix.PARENB | unix.CSTOPB
	t.Cflag |= unix.CS8 | unix.CREAD | unix.CLOCAL | baudConst
	t.Iflag &^= unix.IXON | unix.IXOFF | unix.IXANY | unix.ICRNL
	t.Oflag &^= unix.OPOST
	t.Lflag &^= unix.ICANON | unix.ECHO | unix.ECHOE | unix.ISIG
	return unix.IoctlSetTermios(int(f.Fd()), ioctlSetTermios, t)
}
