//go:build noxdp

package output

import (
	"bytes"
	"io"
	"strings"
	"testing"
)

func newTestWriter(format string) (*Writer, *bytes.Buffer) {
	buf := &bytes.Buffer{}
	w := New(format, false, true)
	w.out = buf
	w.errOut = io.Discard
	return w, buf
}

func TestMaskToken(t *testing.T) {
	tests := []struct {
		token string
		want  string
	}{
		{"abcd1234", "tok_****1234"},
		{"abc", "tok_****"},
		{"", "tok_****"},
		{"tok_s3cr3tKEY", "tok_****tKEY"},
	}
	for _, tt := range tests {
		got := MaskToken(tt.token)
		if got != tt.want {
			t.Errorf("MaskToken(%q) = %q; want %q", tt.token, got, tt.want)
		}
	}
}

func TestPrintTable_Default(t *testing.T) {
	w, buf := newTestWriter("table")
	w.PrintTable([]string{"A", "B"}, [][]string{{"v1", "v2"}, {"x1", "x2"}})
	out := buf.String()
	if !strings.Contains(out, "v1") || !strings.Contains(out, "A") {
		t.Errorf("table output missing expected content: %q", out)
	}
}

func TestPrintTable_JSON(t *testing.T) {
	w, buf := newTestWriter("json")
	w.PrintTable([]string{"Name", "Version"}, [][]string{{"nginx", "1.0"}})
	out := buf.String()
	if !strings.Contains(out, `"name"`) || !strings.Contains(out, "nginx") {
		t.Errorf("json output unexpected: %q", out)
	}
}

func TestPrintTable_YAML(t *testing.T) {
	w, buf := newTestWriter("yaml")
	w.PrintTable([]string{"Name"}, [][]string{{"value"}})
	out := buf.String()
	if !strings.Contains(out, "name") || !strings.Contains(out, "value") {
		t.Errorf("yaml output unexpected: %q", out)
	}
}

func TestPrintTable_JSONPath(t *testing.T) {
	w, buf := newTestWriter("jsonpath={[0].name}")
	w.PrintTable([]string{"Name"}, [][]string{{"myvalue"}})
	out := buf.String()
	if !strings.Contains(out, "myvalue") {
		t.Errorf("jsonpath output unexpected: %q", out)
	}
}

func TestPrintKV(t *testing.T) {
	w, buf := newTestWriter("table")
	w.PrintKV([][2]string{{"key1", "val1"}, {"key2", "val2"}})
	out := buf.String()
	if !strings.Contains(out, "key1") || !strings.Contains(out, "val1") {
		t.Errorf("PrintKV output missing expected content: %q", out)
	}
}

func TestPrintKV_JSON(t *testing.T) {
	w, buf := newTestWriter("json")
	w.PrintKV([][2]string{{"foo", "bar"}})
	out := buf.String()
	if !strings.Contains(out, `"foo"`) || !strings.Contains(out, "bar") {
		t.Errorf("PrintKV JSON output unexpected: %q", out)
	}
}

func TestQuietSuppressesOutput(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.PrintTable([]string{"A"}, [][]string{{"v"}})
	w.Infof("should not appear")
	w.Successf("should not appear")
	if buf.Len() > 0 {
		t.Errorf("quiet mode produced output: %q", buf.String())
	}
}

func TestDeferredNote(t *testing.T) {
	w, buf := newTestWriter("table")
	w.DeferredNote("operation in progress")
	out := buf.String()
	if !strings.Contains(out, "operation in progress") {
		t.Errorf("DeferredNote output missing note: %q", out)
	}
}

// TestDeferredNote_Quiet tests that DeferredNote suppresses output in quiet mode.
func TestDeferredNote_Quiet(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.DeferredNote("should not appear")
	if buf.Len() > 0 {
		t.Errorf("DeferredNote in quiet mode produced output: %q", buf.String())
	}
}

func TestFormatParsing(t *testing.T) {
	tests := []struct {
		input  string
		format Format
		expr   string
	}{
		{"table", FormatTable, ""},
		{"json", FormatJSON, ""},
		{"yaml", FormatYAML, ""},
		{"jsonpath=.foo", FormatJSONPath, ".foo"},
		{"jsonpath={.bar}", FormatJSONPath, "{.bar}"},
		{"unknown", FormatTable, ""},
	}
	for _, tt := range tests {
		w := New(tt.input, false, true)
		if w.format != tt.format {
			t.Errorf("New(%q).format = %v; want %v", tt.input, w.format, tt.format)
		}
		if w.expr != tt.expr {
			t.Errorf("New(%q).expr = %q; want %q", tt.input, w.expr, tt.expr)
		}
	}
}

func TestPrintObject_JSON(t *testing.T) {
	w, buf := newTestWriter("json")
	w.PrintObject(map[string]string{"hello": "world"})
	out := buf.String()
	if !strings.Contains(out, `"hello"`) {
		t.Errorf("PrintObject JSON unexpected: %q", out)
	}
}

func TestPrintObject_YAML(t *testing.T) {
	w, buf := newTestWriter("yaml")
	w.PrintObject(map[string]string{"key": "value"})
	out := buf.String()
	if !strings.Contains(out, "key") || !strings.Contains(out, "value") {
		t.Errorf("PrintObject YAML unexpected: %q", out)
	}
}

func TestPrintObject_Table(t *testing.T) {
	w, buf := newTestWriter("table")
	w.PrintObject(map[string]string{"status": "ok"})
	out := buf.String()
	if !strings.Contains(out, "status") || !strings.Contains(out, "ok") {
		t.Errorf("PrintObject table unexpected: %q", out)
	}
}

func TestPrintObject_JSONPath(t *testing.T) {
	w, buf := newTestWriter("jsonpath={.key}")
	w.PrintObject(map[string]string{"key": "output"})
	out := buf.String()
	if !strings.Contains(out, "output") {
		t.Errorf("PrintObject jsonpath unexpected: %q", out)
	}
}

func TestWarnf(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", false, true)
	w.errOut = buf
	w.Warnf("warning message")
	out := buf.String()
	if !strings.Contains(out, "warning message") {
		t.Errorf("Warnf output missing message: %q", out)
	}
}

func TestErrorf(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", false, true)
	w.errOut = buf
	w.Errorf("error message")
	out := buf.String()
	if !strings.Contains(out, "error message") {
		t.Errorf("Errorf output missing message: %q", out)
	}
}

func TestSuccessf_QuietMode(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.Successf("success")
	if buf.Len() > 0 {
		t.Errorf("Successf in quiet mode produced output: %q", buf.String())
	}
}

func TestSuccessf_TableFormat(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", false, true)
	w.out = buf
	w.Successf("operation completed")
	out := buf.String()
	if !strings.Contains(out, "operation completed") {
		t.Errorf("Successf table format output missing message: %q", out)
	}
}

func TestSuccessf_NonTableFormat(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("json", false, true)
	w.out = buf
	w.Successf("success")
	// Successf should not output in non-table format
	if buf.Len() > 0 {
		t.Errorf("Successf in non-table format produced output: %q", buf.String())
	}
}

func TestInfof_QuietMode(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.Infof("info")
	if buf.Len() > 0 {
		t.Errorf("Infof in quiet mode produced output: %q", buf.String())
	}
}

func TestInfof_TableFormat(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", false, true)
	w.out = buf
	w.Infof("informational message")
	out := buf.String()
	if !strings.Contains(out, "informational message") {
		t.Errorf("Infof table format output missing message: %q", out)
	}
}

func TestInfof_NonTableFormat(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("yaml", false, true)
	w.out = buf
	w.Infof("info")
	// Infof should not output in non-table format
	if buf.Len() > 0 {
		t.Errorf("Infof in non-table format produced output: %q", buf.String())
	}
}

func TestPrintJSONPath_ValidExpression(t *testing.T) {
	w, buf := newTestWriter("jsonpath={.key}")
	w.PrintObject(map[string]string{"key": "value"})
	out := buf.String()
	if !strings.Contains(out, "value") {
		t.Errorf("PrintObject with valid jsonpath missing value: %q", out)
	}
}

func TestDeferredNote_NonTableFormat(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("json", false, true)
	w.out = buf
	w.DeferredNote("note in json")
	out := buf.String()
	// DeferredNote should still output in non-table format
	if !strings.Contains(out, "note in json") {
		t.Errorf("DeferredNote in non-table format missing note: %q", out)
	}
}

// TestWarnf_Quiet tests that Warnf suppresses output in quiet mode.
func TestWarnf_Quiet(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.errOut = buf
	w.Warnf("warning message")
	if buf.Len() > 0 {
		t.Errorf("Warnf in quiet mode produced output: %q", buf.String())
	}
}

// TestPrintKV_JSONPath tests PrintKV with jsonpath format expression.
func TestPrintKV_JSONPath(t *testing.T) {
	w, buf := newTestWriter("jsonpath={.key}")
	w.PrintKV([][2]string{{"key", "value"}})
	out := buf.String()
	if !strings.Contains(out, "value") {
		t.Errorf("PrintKV jsonpath output missing expected value: %q", out)
	}
}

// TestPrintKV_YAML tests PrintKV with YAML format.
func TestPrintKV_YAML(t *testing.T) {
	w, buf := newTestWriter("yaml")
	w.PrintKV([][2]string{{"key1", "val1"}, {"key2", "val2"}})
	out := buf.String()
	if !strings.Contains(out, "key1") || !strings.Contains(out, "val1") {
		t.Errorf("PrintKV YAML output unexpected: %q", out)
	}
}

// TestPrintJSONPath_InvalidExpr tests printJSONPath with an invalid jsonpath expression.
func TestPrintJSONPath_InvalidExpr(t *testing.T) {
	errBuf := &bytes.Buffer{}
	w, _ := newTestWriter("jsonpath={invalid[")
	w.errOut = errBuf
	w.PrintObject(map[string]string{"k": "v"})
	out := errBuf.String()
	// Should have an error about jsonpath parse
	if !strings.Contains(out, "jsonpath") {
		t.Errorf("invalid jsonpath expr should produce error, got: %q", out)
	}
}

// TestPrintJSONPath_ExecuteError tests printJSONPath with a valid parse but invalid execution.
func TestPrintJSONPath_ExecuteError(t *testing.T) {
	errBuf := &bytes.Buffer{}
	w, _ := newTestWriter("jsonpath={.items[*]}")
	w.errOut = errBuf
	// PrintObject with a non-array object should trigger jsonpath execute error
	w.PrintObject(map[string]string{"key": "value"})
	out := errBuf.String()
	// Should have an error about jsonpath execute
	if !strings.Contains(out, "jsonpath") {
		t.Errorf("jsonpath execute error expected, got: %q", out)
	}
}

// TestPrintJSONPath_BareExpression tests printJSONPath with a bare expression (no braces).
func TestPrintJSONPath_BareExpression(t *testing.T) {
	w, buf := newTestWriter("jsonpath=.foo")
	w.PrintObject(map[string]string{"foo": "bar"})
	out := buf.String()
	if !strings.Contains(out, "bar") {
		t.Errorf("bare jsonpath expression should work, got: %q", out)
	}
}

// TestPrintObject_QuietMode tests PrintObject suppresses output in quiet mode.
func TestPrintObject_QuietMode(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.PrintObject(map[string]string{"key": "value"})
	if buf.Len() > 0 {
		t.Errorf("PrintObject in quiet mode produced output: %q", buf.String())
	}
}

// TestPrintKV_QuietMode tests PrintKV suppresses output in quiet mode.
func TestPrintKV_QuietMode(t *testing.T) {
	buf := &bytes.Buffer{}
	w := New("table", true, true)
	w.out = buf
	w.PrintKV([][2]string{{"key", "value"}})
	if buf.Len() > 0 {
		t.Errorf("PrintKV in quiet mode produced output: %q", buf.String())
	}
}
