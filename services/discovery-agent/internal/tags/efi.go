//go:build noxdp

package tags

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// readEFIVarFromFS reads an EFI variable by prefix from the efivars directory.
// EFI variable files are named <VarName>-<GUID>; we match by prefix.
func readEFIVarFromFS(efiVarsPath, varPrefix string) ([]byte, error) {
	entries, err := os.ReadDir(efiVarsPath)
	if err != nil {
		return nil, fmt.Errorf("readdir %s: %w", efiVarsPath, err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), varPrefix+"-") {
			data, err := os.ReadFile(filepath.Join(efiVarsPath, e.Name()))
			if err != nil {
				return nil, err
			}
			return data, nil
		}
	}
	return nil, fmt.Errorf("EFI var %s not found", varPrefix)
}
