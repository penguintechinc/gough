//go:build noxdp

// Package output handles formatted output for the gough CLI.
// Supports table (default), json, yaml, and jsonpath output formats.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/fatih/color"
	"github.com/olekukonko/tablewriter"
	"gopkg.in/yaml.v3"
	"k8s.io/client-go/util/jsonpath"
)

// Format enumerates supported output formats.
type Format string

const (
	FormatTable    Format = "table"
	FormatJSON     Format = "json"
	FormatYAML     Format = "yaml"
	FormatJSONPath Format = "jsonpath"
)

// Writer manages output formatting.
type Writer struct {
	format  Format
	expr    string // jsonpath expression when format == FormatJSONPath
	quiet   bool
	noColor bool
	out     io.Writer
	errOut  io.Writer
}

// New creates a Writer with the given format string, which may be
// "table", "json", "yaml", or "jsonpath=<expr>".
func New(formatStr string, quiet, noColor bool) *Writer {
	w := &Writer{
		quiet:   quiet,
		noColor: noColor,
		out:     os.Stdout,
		errOut:  os.Stderr,
	}
	if noColor {
		color.NoColor = true
	}

	switch {
	case strings.HasPrefix(formatStr, "jsonpath="):
		w.format = FormatJSONPath
		w.expr = strings.TrimPrefix(formatStr, "jsonpath=")
	case formatStr == "json":
		w.format = FormatJSON
	case formatStr == "yaml":
		w.format = FormatYAML
	default:
		w.format = FormatTable
	}
	return w
}

// PrintTable renders a table with headers and rows to stdout.
func (w *Writer) PrintTable(headers []string, rows [][]string) {
	if w.quiet {
		return
	}
	switch w.format {
	case FormatJSON:
		records := make([]map[string]string, 0, len(rows))
		for _, row := range rows {
			rec := make(map[string]string, len(headers))
			for i, h := range headers {
				if i < len(row) {
					rec[strings.ToLower(strings.ReplaceAll(h, " ", "_"))] = row[i]
				}
			}
			records = append(records, rec)
		}
		w.printJSON(records)
	case FormatYAML:
		records := make([]map[string]string, 0, len(rows))
		for _, row := range rows {
			rec := make(map[string]string, len(headers))
			for i, h := range headers {
				if i < len(row) {
					rec[strings.ToLower(strings.ReplaceAll(h, " ", "_"))] = row[i]
				}
			}
			records = append(records, rec)
		}
		w.printYAML(records)
	case FormatJSONPath:
		records := make([]map[string]string, 0, len(rows))
		for _, row := range rows {
			rec := make(map[string]string, len(headers))
			for i, h := range headers {
				if i < len(row) {
					rec[strings.ToLower(strings.ReplaceAll(h, " ", "_"))] = row[i]
				}
			}
			records = append(records, rec)
		}
		w.printJSONPath(records)
	default:
		t := tablewriter.NewWriter(w.out)
		t.SetHeader(headers)
		t.SetBorder(false)
		t.SetHeaderLine(true)
		t.SetCenterSeparator(" ")
		t.SetColumnSeparator("  ")
		t.SetRowSeparator("-")
		t.SetHeaderAlignment(tablewriter.ALIGN_LEFT)
		t.SetAlignment(tablewriter.ALIGN_LEFT)
		t.SetAutoWrapText(false)
		for _, row := range rows {
			t.Append(row)
		}
		t.Render()
	}
}

// PrintObject renders a single object.
func (w *Writer) PrintObject(obj interface{}) {
	if w.quiet {
		return
	}
	switch w.format {
	case FormatJSON:
		w.printJSON(obj)
	case FormatYAML:
		w.printYAML(obj)
	case FormatJSONPath:
		w.printJSONPath(obj)
	default:
		// For table format, fall back to YAML for complex objects.
		w.printYAML(obj)
	}
}

// PrintKV renders key-value pairs in a two-column table or structured format.
func (w *Writer) PrintKV(pairs [][2]string) {
	if w.quiet {
		return
	}
	switch w.format {
	case FormatJSON, FormatJSONPath:
		m := make(map[string]string, len(pairs))
		for _, p := range pairs {
			m[p[0]] = p[1]
		}
		if w.format == FormatJSON {
			w.printJSON(m)
		} else {
			w.printJSONPath(m)
		}
	case FormatYAML:
		m := make(map[string]string, len(pairs))
		for _, p := range pairs {
			m[p[0]] = p[1]
		}
		w.printYAML(m)
	default:
		t := tablewriter.NewWriter(w.out)
		t.SetBorder(false)
		t.SetColumnSeparator("  ")
		t.SetCenterSeparator(" ")
		t.SetAutoWrapText(false)
		t.SetAlignment(tablewriter.ALIGN_LEFT)
		for _, p := range pairs {
			t.Append([]string{p[0], p[1]})
		}
		t.Render()
	}
}

// Successf prints a success message (suppressed in --quiet mode).
func (w *Writer) Successf(format string, args ...interface{}) {
	if w.quiet || w.format != FormatTable {
		return
	}
	fmt.Fprintf(w.out, color.GreenString("✓ ")+format+"\n", args...)
}

// Infof prints an informational message (suppressed in --quiet mode).
func (w *Writer) Infof(format string, args ...interface{}) {
	if w.quiet || w.format != FormatTable {
		return
	}
	fmt.Fprintf(w.out, format+"\n", args...)
}

// Warnf prints a warning to stderr.
func (w *Writer) Warnf(format string, args ...interface{}) {
	if w.quiet {
		return
	}
	fmt.Fprintf(w.errOut, color.YellowString("warning: ")+format+"\n", args...)
}

// Errorf prints an error to stderr and is never suppressed by --quiet.
func (w *Writer) Errorf(format string, args ...interface{}) {
	fmt.Fprintf(w.errOut, color.RedString("error: ")+format+"\n", args...)
}

// DeferredNote prints the deferred-note string from a 202 API response.
func (w *Writer) DeferredNote(note string) {
	if w.quiet {
		return
	}
	if w.format == FormatTable {
		fmt.Fprintf(w.out, color.YellowString("⏳ deferred: ")+"%s\n", note)
		return
	}
	w.PrintObject(map[string]string{"status": "deferred", "note": note})
}

func (w *Writer) printJSON(v interface{}) {
	enc := json.NewEncoder(w.out)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func (w *Writer) printYAML(v interface{}) {
	b, _ := yaml.Marshal(v)
	_, _ = w.out.Write(b)
}

func (w *Writer) printJSONPath(v interface{}) {
	// Marshal to JSON then apply the JSONPath expression.
	raw, err := json.Marshal(v)
	if err != nil {
		w.Errorf("jsonpath marshal: %v", err)
		return
	}
	var data interface{}
	if err := json.Unmarshal(raw, &data); err != nil {
		w.Errorf("jsonpath unmarshal: %v", err)
		return
	}
	jp := jsonpath.New("gough")
	expr := w.expr
	// Wrap bare expression in {}.
	if !strings.HasPrefix(expr, "{") {
		expr = "{" + expr + "}"
	}
	if err := jp.Parse(expr); err != nil {
		w.Errorf("jsonpath parse %q: %v", expr, err)
		return
	}
	if err := jp.Execute(w.out, data); err != nil {
		w.Errorf("jsonpath execute: %v", err)
		return
	}
	fmt.Fprintln(w.out)
}

// MaskToken returns a masked representation of a token suitable for logging.
// Never prints the full token; always masks as tok_****<last4> (or tok_**** if short).
func MaskToken(token string) string {
	if len(token) < 4 {
		return "tok_****"
	}
	return "tok_****" + token[len(token)-4:]
}
