//! Output writer: `table`, `json`, `yaml` and `jsonpath=<RFC 9535 expr>`.
//!
//! Stable contract: `-o json` is the machine format (keys sorted, 2-space indent); `table` is
//! for humans only. An unknown `-o` value is an error, never a silent fallback to table.

use std::io::Write;
use std::str::FromStr;

use comfy_table::{ContentArrangement, Table, presets};
use serde::Serialize;
use serde_json::{Map, Value};
use serde_json_path::JsonPath;

use crate::{CoreError, Env};

/// Selected output format.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutputFormat {
    /// Human-readable aligned table.
    Table,
    /// Indented JSON.
    Json,
    /// YAML.
    Yaml,
    /// RFC 9535 JSONPath query over the JSON form, matches space-joined.
    JsonPath(String),
}

impl FromStr for OutputFormat {
    type Err = CoreError;

    fn from_str(s: &str) -> Result<Self, CoreError> {
        match s {
            "table" => Ok(Self::Table),
            "json" => Ok(Self::Json),
            "yaml" => Ok(Self::Yaml),
            other => {
                match other.strip_prefix("jsonpath=") {
                    Some(expr) => {
                        JsonPath::parse(expr).map_err(|e| {
                        CoreError::Usage(format!("invalid jsonpath expression {expr:?} (RFC 9535, e.g. '$[*].id'): {e}"))
                    })?;
                        Ok(Self::JsonPath(expr.to_owned()))
                    }
                    None => Err(CoreError::Usage(format!(
                        "unknown output format {other:?}; valid: table, json, yaml, jsonpath=<expr>"
                    ))),
                }
            }
        }
    }
}

/// Whether to colorize: off for `--no-color`, `NO_COLOR`, `TERM=dumb`, or a non-terminal stream.
pub fn color_enabled(no_color_flag: bool, env: &dyn Env, is_terminal: bool) -> bool {
    !no_color_flag
        && is_terminal
        && env.var("NO_COLOR").is_none()
        && env.var("TERM").as_deref() != Some("dumb")
}

/// Writes command results to injected stdout/stderr according to the format and quiet flag.
/// `--quiet` suppresses everything except errors.
pub struct Writer<'a> {
    format: OutputFormat,
    quiet: bool,
    color: bool,
    out: &'a mut dyn Write,
    err: &'a mut dyn Write,
}

impl<'a> Writer<'a> {
    /// Creates a writer over the given streams.
    pub fn new(
        format: OutputFormat,
        quiet: bool,
        color: bool,
        out: &'a mut dyn Write,
        err: &'a mut dyn Write,
    ) -> Self {
        Self {
            format,
            quiet,
            color,
            out,
            err,
        }
    }

    /// The active format.
    pub fn format(&self) -> &OutputFormat {
        &self.format
    }

    /// Prints a list as a table (headers uppercase, `_` shown as space) or, for the other
    /// formats, as an array of objects keyed by lowercased header with string values.
    pub fn table(&mut self, headers: &[&str], rows: &[Vec<String>]) -> Result<(), CoreError> {
        if self.quiet {
            return Ok(());
        }
        if self.format == OutputFormat::Table {
            let text = render_table(headers, rows);
            return self.emit(&text);
        }
        let items: Vec<Value> = rows
            .iter()
            .map(|row| {
                let mut obj = Map::new();
                for (header, cell) in headers.iter().zip(row) {
                    obj.insert(
                        header.to_ascii_lowercase().replace(' ', "_"),
                        Value::String(cell.clone()),
                    );
                }
                Value::Object(obj)
            })
            .collect();
        let rendered = self.render_value(&Value::Array(items))?;
        self.emit(&rendered)
    }

    /// Prints key/value pairs: two aligned columns for table, a string-valued object otherwise.
    pub fn kv(&mut self, pairs: &[(&str, String)]) -> Result<(), CoreError> {
        if self.quiet {
            return Ok(());
        }
        if self.format == OutputFormat::Table {
            let width = pairs
                .iter()
                .map(|(k, _)| k.chars().count())
                .max()
                .unwrap_or(0);
            let mut text = String::new();
            for (key, value) in pairs {
                text.push_str(&format!("{key:<width$}  {value}\n"));
            }
            return self.emit(&text);
        }
        let obj: Map<String, Value> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), Value::String(v.clone())))
            .collect();
        let rendered = self.render_value(&Value::Object(obj))?;
        self.emit(&rendered)
    }

    /// Prints any serializable value; table output of an object is YAML with serde field names.
    pub fn object<T: Serialize>(&mut self, value: &T) -> Result<(), CoreError> {
        if self.quiet {
            return Ok(());
        }
        let value = serde_json::to_value(value).map_err(|e| CoreError::Other(e.to_string()))?;
        let rendered = self.render_value(&value)?;
        self.emit(&rendered)
    }

    /// Prints one scalar: the bare text for table, a JSON/YAML string or JSONPath query otherwise.
    pub fn value(&mut self, text: &str) -> Result<(), CoreError> {
        if self.quiet {
            return Ok(());
        }
        if self.format == OutputFormat::Table {
            return self.emit(&format!("{text}\n"));
        }
        let rendered = self.render_value(&Value::String(text.to_owned()))?;
        self.emit(&rendered)
    }

    /// Writes bytes verbatim to stdout (shell completion scripts); ignores `--quiet`.
    pub fn raw(&mut self, bytes: &[u8]) -> Result<(), CoreError> {
        self.out.write_all(bytes)?;
        Ok(())
    }

    /// Prints a success line (`✓ ...`); table format only.
    pub fn success(&mut self, msg: &str) -> Result<(), CoreError> {
        if self.format == OutputFormat::Table && !self.quiet {
            let mark = if self.color {
                "\x1b[32m✓\x1b[0m"
            } else {
                "✓"
            };
            self.emit(&format!("{mark} {msg}\n"))?;
        }
        Ok(())
    }

    /// Prints an informational line; table format only.
    pub fn info(&mut self, msg: &str) -> Result<(), CoreError> {
        if self.format == OutputFormat::Table && !self.quiet {
            self.emit(&format!("{msg}\n"))?;
        }
        Ok(())
    }

    /// Prints a warning to stderr unless quiet.
    pub fn warn(&mut self, msg: &str) -> Result<(), CoreError> {
        if !self.quiet {
            let label = if self.color {
                "\x1b[33mwarning:\x1b[0m"
            } else {
                "warning:"
            };
            writeln!(self.err, "{label} {msg}")?;
        }
        Ok(())
    }

    /// Prints a pre-rendered error line to stderr; never suppressed by `--quiet`.
    pub fn error(&mut self, rendered: &str) -> Result<(), CoreError> {
        let line = match (self.color, rendered.strip_prefix("error:")) {
            (true, Some(rest)) => format!("\x1b[31merror:\x1b[0m{rest}"),
            _ => rendered.to_owned(),
        };
        writeln!(self.err, "{line}")?;
        Ok(())
    }

    fn render_value(&self, value: &Value) -> Result<String, CoreError> {
        match &self.format {
            OutputFormat::Json => serde_json::to_string_pretty(value)
                .map(|s| s + "\n")
                .map_err(|e| CoreError::Other(e.to_string())),
            OutputFormat::Yaml | OutputFormat::Table => {
                serde_norway::to_string(value).map_err(|e| CoreError::Other(e.to_string()))
            }
            OutputFormat::JsonPath(expr) => {
                let path = JsonPath::parse(expr).map_err(|e| CoreError::Usage(e.to_string()))?;
                let parts: Vec<String> = path
                    .query(value)
                    .all()
                    .into_iter()
                    .map(|v| match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .collect();
                Ok(if parts.is_empty() {
                    String::new()
                } else {
                    parts.join(" ") + "\n"
                })
            }
        }
    }

    fn emit(&mut self, text: &str) -> Result<(), CoreError> {
        self.out.write_all(text.as_bytes())?;
        Ok(())
    }
}

fn render_table(headers: &[&str], rows: &[Vec<String>]) -> String {
    let mut table = Table::new();
    table.load_style(presets::NOTHING);
    table.set_content_arrangement(ContentArrangement::Disabled);
    table.set_header(
        headers
            .iter()
            .map(|h| h.to_ascii_uppercase().replace('_', " ")),
    );
    for row in rows {
        table.add_row(row.clone());
    }
    for column in table.column_iter_mut() {
        column.set_padding((0, 2));
    }
    let mut text = String::new();
    for line in table.to_string().lines() {
        text.push_str(line.trim_end());
        text.push('\n');
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::MapEnv;

    fn run(
        format: &str,
        quiet: bool,
        f: impl FnOnce(&mut Writer<'_>) -> Result<(), CoreError>,
    ) -> (String, String) {
        let (mut out, mut err) = (Vec::new(), Vec::new());
        {
            let mut w = Writer::new(format.parse().unwrap(), quiet, false, &mut out, &mut err);
            f(&mut w).unwrap();
        }
        (
            String::from_utf8(out).unwrap(),
            String::from_utf8(err).unwrap(),
        )
    }

    fn sample() -> (Vec<&'static str>, Vec<Vec<String>>) {
        (
            vec!["ID", "HOST_NAME", "CPU"],
            vec![
                vec!["n-1".into(), "alpha-01".into(), "16".into()],
                vec!["n-2".into(), "b".into(), "".into()],
            ],
        )
    }

    #[test]
    fn table_shapes_are_locked() {
        let (h, r) = sample();
        for fmt in ["table", "json", "yaml", "jsonpath=$[*].host_name"] {
            let (out, _) = run(fmt, false, |w| w.table(&h, &r));
            insta::assert_snapshot!(format!("table_{}", fmt.split('=').next().unwrap()), out);
        }
    }

    #[test]
    fn empty_table_json_is_empty_array() {
        let (out, _) = run("json", false, |w| w.table(&["A"], &[]));
        assert_eq!(out, "[]\n");
    }

    #[test]
    fn kv_shapes_are_locked() {
        let pairs = [
            ("version", "1.0.0".to_owned()),
            ("cluster_id", "c-1".to_owned()),
        ];
        for fmt in ["table", "json", "yaml", "jsonpath=$.version"] {
            let (out, _) = run(fmt, false, |w| w.kv(&pairs));
            insta::assert_snapshot!(format!("kv_{}", fmt.split('=').next().unwrap()), out);
        }
    }

    #[test]
    fn object_shapes_are_locked() {
        #[derive(Serialize)]
        struct Node {
            id: &'static str,
            cpu_count: u32,
            tags: Vec<&'static str>,
        }
        let node = Node {
            id: "n-1",
            cpu_count: 16,
            tags: vec!["a", "b"],
        };
        for fmt in ["table", "json", "yaml", "jsonpath=$.tags[*]"] {
            let (out, _) = run(fmt, false, |w| w.object(&node));
            insta::assert_snapshot!(format!("object_{}", fmt.split('=').next().unwrap()), out);
        }
    }

    #[test]
    fn value_and_raw() {
        for (fmt, want) in [
            ("table", "t-1\n"),
            ("json", "\"t-1\"\n"),
            ("yaml", "t-1\n"),
            ("jsonpath=$", "t-1\n"),
        ] {
            let (out, _) = run(fmt, false, |w| w.value("t-1"));
            assert_eq!(out, want, "{fmt}");
        }
        let (out, _) = run("table", true, |w| w.value("x"));
        assert_eq!(out, "");
        let (out, _) = run("table", true, |w| w.raw(b"script"));
        assert_eq!(out, "script");
    }

    #[test]
    fn jsonpath_renders_scalars_and_handles_no_match() {
        let (out, _) = run("jsonpath=$.n", false, |w| {
            w.object(&serde_json::json!({"n": 3}))
        });
        assert_eq!(out, "3\n");
        let (out, _) = run("jsonpath=$.missing", false, |w| {
            w.object(&serde_json::json!({"n": 3}))
        });
        assert_eq!(out, "");
    }

    #[test]
    fn quiet_suppresses_everything_but_errors() {
        let (h, r) = sample();
        let (out, err) = run("table", true, |w| {
            w.table(&h, &r)?;
            w.kv(&[("a", "b".into())])?;
            w.object(&1)?;
            w.success("ok")?;
            w.info("hi")?;
            w.warn("careful")
        });
        assert_eq!((out.as_str(), err.as_str()), ("", ""));
        let (_, err) = run("table", true, |w| w.error("error: boom"));
        assert_eq!(err, "error: boom\n");
    }

    #[test]
    fn success_and_info_are_table_only() {
        let (out, _) = run("table", false, |w| {
            w.success("done")?;
            w.info("note")
        });
        assert_eq!(out, "✓ done\nnote\n");
        let (out, _) = run("json", false, |w| {
            w.success("done")?;
            w.info("note")
        });
        assert_eq!(out, "");
    }

    #[test]
    fn warn_and_color() {
        let (_, err) = run("json", false, |w| w.warn("w"));
        assert_eq!(err, "warning: w\n");
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let mut w = Writer::new(OutputFormat::Table, false, true, &mut out, &mut err);
        w.success("ok").unwrap();
        w.warn("w").unwrap();
        w.error("error: bad").unwrap();
        w.error("plain").unwrap();
        assert_eq!(w.format(), &OutputFormat::Table);
        assert_eq!(String::from_utf8(out).unwrap(), "\x1b[32m✓\x1b[0m ok\n");
        assert_eq!(
            String::from_utf8(err).unwrap(),
            "\x1b[33mwarning:\x1b[0m w\n\x1b[31merror:\x1b[0m bad\nplain\n"
        );
    }

    #[test]
    fn format_parsing_is_strict() {
        assert_eq!(
            "table".parse::<OutputFormat>().unwrap(),
            OutputFormat::Table
        );
        assert_eq!("json".parse::<OutputFormat>().unwrap(), OutputFormat::Json);
        assert_eq!("yaml".parse::<OutputFormat>().unwrap(), OutputFormat::Yaml);
        assert_eq!(
            "jsonpath=$.a".parse::<OutputFormat>().unwrap(),
            OutputFormat::JsonPath("$.a".into())
        );
        for bad in [
            "",
            "xml",
            "JSON",
            "jsonpath",
            "jsonpath=",
            "jsonpath={.a}",
            "jsonpath=[*",
        ] {
            assert!(
                matches!(bad.parse::<OutputFormat>(), Err(CoreError::Usage(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn color_rules() {
        let none = MapEnv::default();
        assert!(color_enabled(false, &none, true));
        assert!(!color_enabled(true, &none, true));
        assert!(!color_enabled(false, &none, false));
        assert!(!color_enabled(
            false,
            &MapEnv::new([("NO_COLOR", "1")]),
            true
        ));
        assert!(!color_enabled(
            false,
            &MapEnv::new([("TERM", "dumb")]),
            true
        ));
    }

    #[test]
    fn broken_pipe_surfaces_as_error() {
        struct Broken;
        impl Write for Broken {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("closed"))
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let (mut err, mut sink, mut broken_out, mut broken_err) =
            (Vec::new(), Vec::new(), Broken, Broken);
        let mut w = Writer::new(OutputFormat::Json, false, false, &mut broken_out, &mut err);
        assert!(matches!(w.kv(&[("a", "b".into())]), Err(CoreError::Io(_))));
        let mut w = Writer::new(OutputFormat::Json, false, false, &mut sink, &mut broken_err);
        assert!(matches!(w.warn("x"), Err(CoreError::Io(_))));
        assert!(matches!(w.error("x"), Err(CoreError::Io(_))));
    }
}
