#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::{CStr, CString};
use std::fs;
use std::io::{self, Read, Write};
use std::os::raw::{c_char, c_int, c_long, c_uchar, c_ulong};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

type Atom = c_ulong;
type Window = c_ulong;
type Status = c_int;
#[repr(C)]
struct Display {
    _private: [u8; 0],
}
#[repr(C)]
struct XClassHint {
    res_name: *mut c_char,
    res_class: *mut c_char,
}

#[repr(C)]
struct XErrorEvent {
    type_: c_int,
    display: *mut Display,
    resourceid: c_ulong,
    serial: c_ulong,
    error_code: c_uchar,
    request_code: c_uchar,
    minor_code: c_uchar,
    _padding: c_uchar,
}

type XErrorHandler = Option<unsafe extern "C" fn(*mut Display, *mut XErrorEvent) -> c_int>;

#[link(name = "X11", kind = "static")]
unsafe extern "C" {
    fn XOpenDisplay(name: *const c_char) -> *mut Display;
    fn XCloseDisplay(display: *mut Display) -> c_int;
    fn XDefaultRootWindow(display: *mut Display) -> Window;
    fn XQueryTree(
        display: *mut Display,
        window: Window,
        root: *mut Window,
        parent: *mut Window,
        children: *mut *mut Window,
        count: *mut CUint,
    ) -> Status;
    fn XFree(data: *mut std::ffi::c_void) -> c_int;
    fn XGetClassHint(display: *mut Display, window: Window, hint: *mut XClassHint) -> Status;
    fn XGetGeometry(
        display: *mut Display,
        drawable: Window,
        root: *mut Window,
        x: *mut c_int,
        y: *mut c_int,
        width: *mut CUint,
        height: *mut CUint,
        border: *mut CUint,
        depth: *mut CUint,
    ) -> Status;
    fn XInternAtom(display: *mut Display, name: *const c_char, only_if_exists: c_int) -> Atom;
    fn XGetWindowProperty(
        display: *mut Display,
        window: Window,
        property: Atom,
        offset: c_long,
        length: c_long,
        delete: c_int,
        req_type: Atom,
        actual_type: *mut Atom,
        actual_format: *mut c_int,
        nitems: *mut c_ulong,
        bytes_after: *mut c_ulong,
        data: *mut *mut c_uchar,
    ) -> c_int;
    fn XGetInputFocus(display: *mut Display, focus: *mut Window, revert: *mut c_int) -> c_int;
    fn XSync(display: *mut Display, discard: c_int) -> c_int;
    fn XSetErrorHandler(handler: XErrorHandler) -> XErrorHandler;
}
#[link(name = "xcb", kind = "static")]
unsafe extern "C" {}
#[link(name = "Xau", kind = "static")]
unsafe extern "C" {}
#[link(name = "Xdmcp", kind = "static")]
unsafe extern "C" {}
type CUint = u32;

static X_ERROR_SEEN: AtomicBool = AtomicBool::new(false);

unsafe extern "C" fn bounded_x_error_handler(
    _display: *mut Display,
    _event: *mut XErrorEvent,
) -> c_int {
    X_ERROR_SEEN.store(true, Ordering::Release);
    0
}

struct XErrorGuard {
    previous: XErrorHandler,
}

impl XErrorGuard {
    fn install() -> Self {
        X_ERROR_SEEN.store(false, Ordering::Release);
        let previous = unsafe { XSetErrorHandler(Some(bounded_x_error_handler)) };
        Self { previous }
    }
}

impl Drop for XErrorGuard {
    fn drop(&mut self) {
        unsafe { XSetErrorHandler(self.previous) };
        X_ERROR_SEEN.store(false, Ordering::Release);
    }
}

fn x11_sync(display: *mut Display) -> Result<(), String> {
    unsafe { XSync(display, 0) };
    if X_ERROR_SEEN.swap(false, Ordering::AcqRel) {
        Err("transient X11 window error".into())
    } else {
        Ok(())
    }
}

#[derive(Debug)]
struct Args {
    display: String,
    xauthority: String,
    title: String,
    class_name: String,
    executable: String,
    wine_prefix: String,
    loader: String,
    outer_width: u32,
    outer_height: u32,
    client_width: u32,
    client_height: u32,
    nonce: String,
    binary_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessSnapshot {
    pid: u32,
    executable: String,
    start: String,
    status_name: String,
    loader: String,
    wine_prefix: String,
}

#[derive(Debug)]
enum JsonValue {
    String(String),
    U32(u32),
}

struct JsonParser<'a> {
    chars: std::str::Chars<'a>,
    lookahead: Option<char>,
}

impl<'a> JsonParser<'a> {
    fn new(input: &'a str) -> Self {
        let mut chars = input.chars();
        let lookahead = chars.next();
        Self { chars, lookahead }
    }

    fn advance(&mut self) -> Option<char> {
        let current = self.lookahead;
        self.lookahead = self.chars.next();
        current
    }

    fn whitespace(&mut self) {
        while matches!(self.lookahead, Some(' ' | '\n' | '\r' | '\t')) {
            self.advance();
        }
    }

    fn expect(&mut self, expected: char) -> Result<(), String> {
        if self.advance() == Some(expected) {
            Ok(())
        } else {
            Err(format!("expected {expected}"))
        }
    }

    fn hex_digit(value: char) -> Result<u32, String> {
        value
            .to_digit(16)
            .ok_or_else(|| "invalid unicode escape".into())
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect('"')?;
        let mut output = String::new();
        loop {
            let ch = self.advance().ok_or("unterminated string")?;
            match ch {
                '"' => return Ok(output),
                '\\' => match self.advance().ok_or("unterminated escape")? {
                    '"' => output.push('"'),
                    '\\' => output.push('\\'),
                    '/' => output.push('/'),
                    'b' => output.push('\u{0008}'),
                    'f' => output.push('\u{000c}'),
                    'n' => output.push('\n'),
                    'r' => output.push('\r'),
                    't' => output.push('\t'),
                    'u' => {
                        let mut value = 0u32;
                        for _ in 0..4 {
                            value = (value << 4)
                                | Self::hex_digit(self.advance().ok_or("short unicode escape")?)?;
                        }
                        if (0xD800..=0xDFFF).contains(&value) {
                            return Err("surrogate unicode escape is unsupported".into());
                        }
                        output.push(char::from_u32(value).ok_or("invalid unicode scalar")?);
                    }
                    _ => return Err("invalid string escape".into()),
                },
                ch if (ch as u32) < 0x20 => return Err("control character in string".into()),
                ch => output.push(ch),
            }
        }
    }

    fn u32(&mut self) -> Result<u32, String> {
        let mut digits = String::new();
        while let Some(ch) = self.lookahead {
            if !ch.is_ascii_digit() {
                break;
            }
            digits.push(ch);
            self.advance();
        }
        if digits.is_empty() || (digits.len() > 1 && digits.starts_with('0')) {
            return Err("invalid unsigned integer".into());
        }
        digits
            .parse()
            .map_err(|_| "unsigned integer overflow".into())
    }

    fn value(&mut self) -> Result<JsonValue, String> {
        match self.lookahead {
            Some('"') => Ok(JsonValue::String(self.string()?)),
            Some(ch) if ch.is_ascii_digit() => Ok(JsonValue::U32(self.u32()?)),
            _ => Err("request value has invalid type".into()),
        }
    }

    fn object(&mut self) -> Result<std::collections::BTreeMap<String, JsonValue>, String> {
        self.whitespace();
        self.expect('{')?;
        let mut values = std::collections::BTreeMap::new();
        self.whitespace();
        if self.lookahead != Some('}') {
            loop {
                self.whitespace();
                if self.lookahead != Some('"') {
                    return Err("object key is not a string".into());
                }
                let key = self.string()?;
                if values.contains_key(&key) {
                    return Err(format!("duplicate request key {key}"));
                }
                self.whitespace();
                self.expect(':')?;
                self.whitespace();
                values.insert(key, self.value()?);
                self.whitespace();
                match self.advance() {
                    Some(',') => continue,
                    Some('}') => break,
                    _ => return Err("object separator is invalid".into()),
                }
            }
        } else {
            self.advance();
        }
        self.whitespace();
        if self.lookahead.is_some() {
            return Err("trailing request data".into());
        }
        Ok(values)
    }
}

fn parse_args(line: &str) -> Result<Args, String> {
    if line.len() > 16 * 1024 || line.as_bytes().contains(&0) {
        return Err("request is oversized or contains NUL".into());
    }
    let values = JsonParser::new(line).object()?;
    let expected = [
        "protocolVersion",
        "display",
        "xauthority",
        "title",
        "class",
        "executable",
        "winePrefix",
        "loader",
        "outerWidth",
        "outerHeight",
        "clientWidth",
        "clientHeight",
        "nonce",
        "binaryDigest",
    ];
    if values.len() != expected.len() || expected.iter().any(|key| !values.contains_key(*key)) {
        return Err("request keys are not canonical".into());
    }
    let string = |key: &str| match values.get(key) {
        Some(JsonValue::String(value)) => Ok(value.clone()),
        _ => Err(format!("request key {key} is not a string")),
    };
    let u32_value = |key: &str| match values.get(key) {
        Some(JsonValue::U32(value)) => Ok(*value),
        _ => Err(format!("request key {key} is not an unsigned integer")),
    };
    if u32_value("protocolVersion")? != 1 {
        return Err("unsupported protocolVersion".into());
    }
    let nonce = string("nonce")?;
    if nonce.len() != 32 || !nonce.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("nonce is not a 128-bit hexadecimal token".into());
    }
    let binary_digest = string("binaryDigest")?;
    if binary_digest.len() != 64 || !binary_digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("binaryDigest is not a SHA-256 hexadecimal token".into());
    }
    Ok(Args {
        display: string("display")?,
        xauthority: string("xauthority")?,
        title: string("title")?,
        class_name: string("class")?,
        executable: string("executable")?,
        wine_prefix: string("winePrefix")?,
        loader: string("loader")?,
        outer_width: u32_value("outerWidth")?,
        outer_height: u32_value("outerHeight")?,
        client_width: u32_value("clientWidth")?,
        client_height: u32_value("clientHeight")?,
        nonce,
        binary_digest,
    })
}

fn basename(value: &str) -> &str {
    value.rsplit(['/', '\\']).next().unwrap_or(value)
}

fn canonical_prefix(value: &str) -> String {
    let path = Path::new(value);
    fs::canonicalize(path)
        .map(|resolved| resolved.to_string_lossy().into_owned())
        .unwrap_or_else(|_| {
            let trimmed = value.trim_end_matches('/');
            if trimmed.is_empty() {
                "/".to_string()
            } else {
                trimmed.to_string()
            }
        })
}

fn proc_candidate(pid: u32, args: &Args) -> Result<ProcessSnapshot, String> {
    let raw = fs::read(format!("/proc/{pid}/cmdline")).map_err(|_| "cmdline".to_string())?;
    let items: Vec<String> = raw
        .split(|b| *b == 0)
        .filter(|x| !x.is_empty())
        .map(|x| String::from_utf8_lossy(x).into_owned())
        .collect();
    let matches = items
        .iter()
        .map(|x| basename(x))
        .filter(|x| *x == args.executable)
        .count();
    if matches != 1 {
        return Err("configured executable occurrence mismatch".into());
    }
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).map_err(|_| "stat".to_string())?;
    let fields: Vec<&str> = stat.split_whitespace().collect();
    let start = fields.get(21).ok_or("start")?.to_string();
    if start.is_empty() || !start.chars().all(|ch| ch.is_ascii_digit()) {
        return Err("start is malformed".into());
    }
    let status =
        fs::read_to_string(format!("/proc/{pid}/status")).map_err(|_| "status".to_string())?;
    let name = status
        .lines()
        .find_map(|line| line.strip_prefix("Name:").map(str::trim))
        .unwrap_or("");
    if name != args.executable {
        return Err("process name mismatch".into());
    }
    let exe = fs::read_link(format!("/proc/{pid}/exe")).map_err(|_| "exe".to_string())?;
    let observed_loader = basename(exe.to_string_lossy().as_ref()).to_string();
    if observed_loader != args.loader {
        return Err("loader mismatch".into());
    }
    let env = fs::read(format!("/proc/{pid}/environ")).map_err(|_| "environ".to_string())?;
    let wanted = env
        .split(|b| *b == 0)
        .find_map(|item| item.strip_prefix(b"WINEPREFIX="))
        .map(|x| String::from_utf8_lossy(x).into_owned())
        .unwrap_or_default();
    let wanted = canonical_prefix(&wanted);
    if wanted != canonical_prefix(&args.wine_prefix) {
        return Err("Wine prefix mismatch".into());
    }
    Ok(ProcessSnapshot {
        pid,
        executable: args.executable.clone(),
        start,
        status_name: name.to_string(),
        loader: observed_loader,
        wine_prefix: wanted,
    })
}

fn verified_processes(args: &Args) -> Result<Vec<ProcessSnapshot>, String> {
    let mut candidates = Vec::new();
    for entry in fs::read_dir("/proc").map_err(|_| "proc".to_string())? {
        let entry = entry.map_err(|_| "proc entry".to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Ok(pid) = name.parse::<u32>() {
            if let Ok(candidate) = proc_candidate(pid, args) {
                candidates.push(candidate);
            }
        }
    }
    Ok(candidates)
}
fn property_u32(display: *mut Display, window: Window, atom: Atom) -> Result<u32, String> {
    let mut actual = 0;
    let mut format = 0;
    let mut n = 0;
    let mut after = 0;
    let mut data = std::ptr::null_mut();
    let rc = unsafe {
        XGetWindowProperty(
            display,
            window,
            atom,
            0,
            1,
            0,
            0,
            &mut actual,
            &mut format,
            &mut n,
            &mut after,
            &mut data,
        )
    };
    let synced = x11_sync(display);
    if rc != 0 || synced.is_err() || data.is_null() || format != 32 || n != 1 {
        if !data.is_null() {
            unsafe { XFree(data.cast()) };
        }
        if let Err(error) = synced {
            return Err(error);
        }
        return Err("window property missing".into());
    }
    let value = unsafe { *(data as *const c_ulong) as u32 };
    unsafe { XFree(data.cast()) };
    Ok(value)
}

fn property_present(display: *mut Display, window: Window, atom: Atom) -> Result<bool, String> {
    let mut actual = 0;
    let mut format = 0;
    let mut n = 0;
    let mut after = 0;
    let mut data = std::ptr::null_mut();
    let rc = unsafe {
        XGetWindowProperty(
            display,
            window,
            atom,
            0,
            1,
            0,
            0,
            &mut actual,
            &mut format,
            &mut n,
            &mut after,
            &mut data,
        )
    };
    let synced = x11_sync(display);
    if !data.is_null() {
        unsafe { XFree(data.cast()) };
    }
    if rc != 0 {
        return Err("window state property query failed".into());
    }
    synced?;
    Ok(actual != 0 && n != 0)
}

fn property_text(
    display: *mut Display,
    window: Window,
    property: Atom,
    accepted_types: &[Atom],
    latin1_type: Atom,
) -> Result<Option<String>, String> {
    let mut actual = 0;
    let mut format = 0;
    let mut n = 0;
    let mut after = 0;
    let mut data = std::ptr::null_mut();
    let rc = unsafe {
        XGetWindowProperty(
            display,
            window,
            property,
            0,
            4096,
            0,
            0,
            &mut actual,
            &mut format,
            &mut n,
            &mut after,
            &mut data,
        )
    };
    let synced = x11_sync(display);
    if rc != 0 || synced.is_err() {
        if !data.is_null() {
            unsafe { XFree(data.cast()) };
        }
        if let Err(error) = synced {
            return Err(error);
        }
        return Err("window text property query failed".into());
    }
    synced?;
    if data.is_null() || n == 0 {
        if !data.is_null() {
            unsafe { XFree(data.cast()) };
        }
        return Ok(None);
    }
    if format != 8 || after != 0 || !accepted_types.contains(&actual) {
        unsafe { XFree(data.cast()) };
        return Err("window text property type is unsupported or oversized".into());
    }
    let bytes = unsafe { std::slice::from_raw_parts(data, n as usize) };
    let value = if actual == latin1_type {
        Ok(bytes
            .iter()
            .map(|byte| char::from(*byte))
            .collect::<String>())
    } else {
        String::from_utf8(bytes.to_vec())
    };
    unsafe { XFree(data.cast()) };
    let value = value.map_err(|_| "window text property is not valid UTF-8")?;
    Ok(Some(value.trim_end_matches('\0').to_string()))
}
fn cstr(ptr: *const c_char) -> String {
    if ptr.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(ptr) }
            .to_string_lossy()
            .into_owned()
    }
}
fn json_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            ch if ch <= '\u{1f}' => escaped.push_str(&format!("\\u{:04x}", ch as u32)),
            _ => escaped.push(ch),
        }
    }
    escaped
}
fn window_name(
    display: *mut Display,
    window: Window,
    net_wm_name: Atom,
    wm_name: Atom,
    utf8_string: Atom,
    string: Atom,
    compound_text: Atom,
) -> Result<(String, String), String> {
    let title = match property_text(
        display,
        window,
        net_wm_name,
        &[utf8_string, string, compound_text],
        string,
    )? {
        Some(value) => value,
        None => property_text(
            display,
            window,
            wm_name,
            &[utf8_string, string, compound_text],
            string,
        )?
        .unwrap_or_default(),
    };
    let mut hint = XClassHint {
        res_name: std::ptr::null_mut(),
        res_class: std::ptr::null_mut(),
    };
    let class = if unsafe { XGetClassHint(display, window, &mut hint) } != 0 {
        x11_sync(display)?;
        let name = cstr(hint.res_name);
        let class = cstr(hint.res_class);
        unsafe { XFree(hint.res_name.cast()) };
        unsafe { XFree(hint.res_class.cast()) };
        format!("{name},{class}")
    } else {
        x11_sync(display)?;
        String::new()
    };
    Ok((title, class))
}
fn geometry(display: *mut Display, window: Window) -> Result<(i32, i32, u32, u32), String> {
    let mut root = 0;
    let (mut x, mut y, mut w, mut h, mut b, mut d) = (0, 0, 0, 0, 0, 0);
    let status = unsafe {
        XGetGeometry(
            display, window, &mut root, &mut x, &mut y, &mut w, &mut h, &mut b, &mut d,
        )
    };
    x11_sync(display)?;
    if status == 0 {
        Err("geometry".into())
    } else {
        Ok((x, y, w, h))
    }
}
fn tree(display: *mut Display, window: Window) -> Result<(Window, Window, Vec<Window>), String> {
    let (mut root, mut parent, mut children, mut n) = (0, 0, std::ptr::null_mut(), 0);
    let status = unsafe {
        XQueryTree(
            display,
            window,
            &mut root,
            &mut parent,
            &mut children,
            &mut n,
        )
    };
    x11_sync(display)?;
    if status == 0 {
        return Err("tree".into());
    }
    let xs = if children.is_null() {
        Vec::new()
    } else {
        unsafe { std::slice::from_raw_parts(children, n as usize) }.to_vec()
    };
    if !children.is_null() {
        let _ = unsafe { XFree(children.cast()) };
    };
    Ok((root, parent, xs))
}

fn wm_state_focus(
    display: *mut Display,
    raw_focus: Window,
    root: Window,
    wm_state_atom: Atom,
) -> Result<Window, String> {
    let mut current = raw_focus;
    for _ in 0..64 {
        if current == 0 {
            return Err("input focus is None".into());
        }
        if property_present(display, current, wm_state_atom)? {
            return Ok(current);
        }
        let (tree_root, parent, _) = tree(display, current)?;
        if tree_root != root || current == tree_root || parent == 0 || parent == current {
            // This is xdotool's documented fallback: an unadorned raw focus
            // remains the focus result rather than being mapped to the game.
            return Ok(raw_focus);
        }
        current = parent;
    }
    Err("focus ancestry exceeded bound".into())
}

struct VerifiedWindow {
    window: Window,
    outer_root: Window,
    outer_parent: Window,
    outer_children: Vec<Window>,
    outer_geometry: (i32, i32, u32, u32),
    child: Window,
    child_geometry: (i32, i32, u32, u32),
}

fn verify_window_candidate(
    display: *mut Display,
    window: Window,
    root: Window,
    args: &Args,
) -> Result<Option<VerifiedWindow>, String> {
    let (outer_root, outer_parent, outer_children) = tree(display, window)?;
    if outer_root != root || outer_parent != root || outer_children.len() != 1 {
        return Ok(None);
    }
    let outer_geometry = geometry(display, window)?;
    if outer_geometry.2 != args.outer_width || outer_geometry.3 != args.outer_height {
        return Ok(None);
    }
    let child = outer_children[0];
    let child_geometry = geometry(display, child)?;
    if child_geometry.2 != args.client_width || child_geometry.3 != args.client_height {
        return Ok(None);
    }
    Ok(Some(VerifiedWindow {
        window,
        outer_root,
        outer_parent,
        outer_children,
        outer_geometry,
        child,
        child_geometry,
    }))
}
fn observe(args: &Args) -> Result<(String, u64, u64, u64), String> {
    let total_started = Instant::now();
    let initial_proc_started = Instant::now();
    let initial_processes = verified_processes(args)?;
    let initial_proc_ns = initial_proc_started.elapsed().as_nanos() as u64;
    if initial_processes.len() != 1 {
        return Err(format!(
            "expected one verified game process, found {}",
            initial_processes.len()
        ));
    }
    let initial_process = initial_processes[0].clone();
    let pid = initial_process.pid;
    let display_name = CString::new(args.display.clone()).map_err(|_| "display".to_string())?;
    if !args.xauthority.is_empty() {
        std::env::set_var("XAUTHORITY", &args.xauthority);
    }
    let display = unsafe { XOpenDisplay(display_name.as_ptr()) };
    if display.is_null() {
        return Err("XOpenDisplay failed".into());
    }
    let error_guard = XErrorGuard::install();
    let x11_started = Instant::now();
    let result = (|| {
        let root = unsafe { XDefaultRootWindow(display) };
        x11_sync(display)?;
        let net_pid = CString::new("_NET_WM_PID").unwrap();
        let pid_atom = unsafe { XInternAtom(display, net_pid.as_ptr(), 0) };
        let net_active = CString::new("_NET_ACTIVE_WINDOW").unwrap();
        let active_atom = unsafe { XInternAtom(display, net_active.as_ptr(), 0) };
        let wm_state = CString::new("WM_STATE").unwrap();
        let wm_state_atom = unsafe { XInternAtom(display, wm_state.as_ptr(), 0) };
        let net_name = CString::new("_NET_WM_NAME").unwrap();
        let net_name_atom = unsafe { XInternAtom(display, net_name.as_ptr(), 0) };
        let wm_name = CString::new("WM_NAME").unwrap();
        let wm_name_atom = unsafe { XInternAtom(display, wm_name.as_ptr(), 0) };
        let utf8 = CString::new("UTF8_STRING").unwrap();
        let utf8_atom = unsafe { XInternAtom(display, utf8.as_ptr(), 0) };
        let string = CString::new("STRING").unwrap();
        let string_atom = unsafe { XInternAtom(display, string.as_ptr(), 0) };
        let compound = CString::new("COMPOUND_TEXT").unwrap();
        let compound_atom = unsafe { XInternAtom(display, compound.as_ptr(), 0) };
        x11_sync(display)?;
        if pid_atom == 0
            || active_atom == 0
            || wm_state_atom == 0
            || net_name_atom == 0
            || wm_name_atom == 0
            || utf8_atom == 0
            || string_atom == 0
            || compound_atom == 0
        {
            return Err("required X11 atom is unavailable".into());
        }
        let (_, _, root_children) = tree(display, root)?;
        let mut candidates = Vec::new();
        let mut stack = root_children.clone();
        while let Some(window) = stack.pop() {
            let named = match window_name(
                display,
                window,
                net_name_atom,
                wm_name_atom,
                utf8_atom,
                string_atom,
                compound_atom,
            ) {
                Ok(named) => named,
                Err(_) => {
                    // A non-target child can disappear between enumeration and
                    // inspection.  The reference resolver ignores that child;
                    // target/tree uncertainty is checked again below.
                    continue;
                }
            };
            let class_values: Vec<&str> = named.1.split(',').collect();
            if named.0 == args.title
                && class_values.len() == 2
                && class_values.iter().all(|value| *value == args.class_name)
                && property_u32(display, window, pid_atom).ok() == Some(pid)
            {
                candidates.push(window);
            }
            if let Ok((_, _, children)) = tree(display, window) {
                stack.extend(children);
            }
        }
        let mut verified_candidates = Vec::new();
        for window in candidates {
            if let Some(verified) = verify_window_candidate(display, window, root, args)? {
                verified_candidates.push(verified);
            }
        }
        if verified_candidates.len() != 1 {
            return Err("expected one verified game window".into());
        }
        let candidate_window_count = verified_candidates.len();
        let verified = verified_candidates.into_iter().next().unwrap();
        let outer = verified.window;
        let (verified_title, verified_class) = window_name(
            display,
            outer,
            net_name_atom,
            wm_name_atom,
            utf8_atom,
            string_atom,
            compound_atom,
        )?;
        let outer_root = verified.outer_root;
        let outer_parent = verified.outer_parent;
        let outer_children = verified.outer_children;
        let (ox, oy, ow, oh) = verified.outer_geometry;
        let child = verified.child;
        let (cx, cy, cw, ch) = verified.child_geometry;
        let mut final_focus = 0;
        let mut final_revert = 0;
        let (final_title, final_class) = window_name(
            display,
            outer,
            net_name_atom,
            wm_name_atom,
            utf8_atom,
            string_atom,
            compound_atom,
        )?;
        let final_pid = property_u32(display, outer, pid_atom)?;
        let (final_root, final_parent, final_children) = tree(display, outer)?;
        let (final_ox, final_oy, final_ow, final_oh) = geometry(display, outer)?;
        let final_child_geometry = geometry(display, child)?;
        let mut sorted_children = outer_children.clone();
        let mut sorted_final_children = final_children.clone();
        sorted_children.sort_unstable();
        sorted_final_children.sort_unstable();
        if final_root != outer_root
            || final_parent != outer_parent
            || sorted_final_children != sorted_children
            || (final_ox, final_oy, final_ow, final_oh) != (ox, oy, ow, oh)
            || final_child_geometry != (cx, cy, cw, ch)
        {
            return Err("window tree or geometry changed during observation".into());
        }
        let final_focus_status =
            unsafe { XGetInputFocus(display, &mut final_focus, &mut final_revert) };
        x11_sync(display)?;
        if final_focus_status == 0 {
            return Err("final input focus query failed".into());
        }
        final_focus = wm_state_focus(display, final_focus, root, wm_state_atom)?;
        let final_active = property_u32(display, root, active_atom).unwrap_or(0);
        if final_title != verified_title
            || final_title != args.title
            || final_class != verified_class
            || final_pid != pid
        {
            return Err("window binding changed during observation".into());
        }
        let x11_elapsed_ns = x11_started.elapsed().as_nanos() as u64;
        let final_process_started = Instant::now();
        let final_processes = verified_processes(args)?;
        let final_proc_ns = final_process_started.elapsed().as_nanos() as u64;
        if final_processes.len() != 1 || final_processes[0] != initial_process {
            return Err("process binding changed during observation".into());
        }
        let children = outer_children
            .iter()
            .map(|value| value.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let payload = format!(
            "{{\"matchingProcessCount\":{},\"candidateWindowCount\":{},\"pid\":{pid},\"start\":\"{}\",\"exe\":\"{}\",\"statusName\":\"{}\",\"loader\":\"{}\",\"winePrefix\":\"{}\",\"xid\":{outer},\"title\":\"{}\",\"class\":\"{}\",\"outer\":{outer},\"child\":{child},\"outerRoot\":{outer_root},\"outerParent\":{outer_parent},\"outerGeometry\":[{ox},{oy},{ow},{oh}],\"childGeometry\":[{cx},{cy},{cw},{ch}],\"outerChildren\":[{children}],\"focus\":{final_focus},\"active\":{final_active},\"display\":\"{}\",\"nonce\":\"{}\",\"binaryDigest\":\"{}\"}}",
            final_processes.len(),
            candidate_window_count,
            json_escape(&initial_process.start),
            json_escape(&initial_process.executable),
            json_escape(&initial_process.status_name),
            json_escape(&initial_process.loader),
            json_escape(&initial_process.wine_prefix),
            json_escape(&final_title),
            json_escape(&verified_class),
            json_escape(&args.display),
            json_escape(&args.nonce),
            json_escape(&args.binary_digest),
        );
        Ok((
            payload,
            initial_proc_ns.saturating_add(final_proc_ns),
            x11_elapsed_ns,
        ))
    })();
    drop(error_guard);
    unsafe { XCloseDisplay(display) };
    result.map(|(payload, proc_ns, x11_ns)| {
        let total_ns = total_started.elapsed().as_nanos() as u64;
        (payload, proc_ns, x11_ns, total_ns)
    })
}
fn main() {
    const MAX_REQUEST: usize = 16 * 1024;
    let mut bytes = Vec::with_capacity(MAX_REQUEST + 1);
    let mut limited = io::stdin().take((MAX_REQUEST + 1) as u64);
    if limited.read_to_end(&mut bytes).is_err() || bytes.len() > MAX_REQUEST {
        let out =
            "{\"protocolVersion\":1,\"error\":\"observer request is oversized or unreadable\"}";
        let _ = writeln!(io::stdout(), "{out}");
        let _ = writeln!(io::stderr(), "{out}");
        std::process::exit(2)
    }
    let input = match String::from_utf8(bytes) {
        Ok(value) => value,
        Err(_) => {
            let out = "{\"protocolVersion\":1,\"error\":\"observer request is not UTF-8\"}";
            let _ = writeln!(io::stdout(), "{out}");
            let _ = writeln!(io::stderr(), "{out}");
            std::process::exit(2)
        }
    };
    let result = parse_args(input.trim()).and_then(|args| observe(&args));
    let (success, out) = match result {
        Ok((payload, proc_ns, x11_ns, total_ns)) => {
            let body = payload
                .strip_prefix('{')
                .and_then(|value| value.strip_suffix('}'))
                .unwrap_or("");
            (true, format!(
                "{{\"protocolVersion\":1,{},\"timingsNs\":{{\"proc\":{proc_ns},\"x11\":{x11_ns},\"total\":{total_ns}}}}}",
                body
            ))
        }
        Err(error) => (
            false,
            format!(
                "{{\"protocolVersion\":1,\"error\":\"{}\"}}",
                json_escape(&error)
            ),
        ),
    };
    let mut stdout = io::stdout();
    let _ = writeln!(stdout, "{out}");
    let _ = stdout.flush();
    if !success {
        let _ = writeln!(io::stderr(), "{out}");
        std::process::exit(1)
    }
}

#[cfg(test)]
mod tests {
    use super::parse_args;

    fn valid_request() -> String {
        r#"{"protocolVersion":1,"display":":0","xauthority":"","title":"World of Warcraft","class":"wowb.exe","executable":"WowB.exe","winePrefix":"wine-prefix","loader":"wine-preloader","outerWidth":1928,"outerHeight":1092,"clientWidth":1920,"clientHeight":1080,"nonce":"0123456789abcdef0123456789abcdef","binaryDigest":"0000000000000000000000000000000000000000000000000000000000000000"}"#.to_string()
    }

    #[test]
    fn parses_the_closed_request_shape() {
        let args = parse_args(&valid_request()).expect("valid observer request");
        assert_eq!(args.outer_width, 1928);
        assert_eq!(args.client_height, 1080);
    }

    #[test]
    fn rejects_unsupported_protocol_and_oversized_input() {
        let unsupported = valid_request().replace("\"protocolVersion\":1", "\"protocolVersion\":2");
        assert_eq!(
            parse_args(&unsupported).err().as_deref(),
            Some("unsupported protocolVersion")
        );
        assert_eq!(
            parse_args(&"x".repeat(16 * 1024 + 1)).err().as_deref(),
            Some("request is oversized or contains NUL")
        );
    }

    #[test]
    fn rejects_malformed_nonce_and_duplicate_keys() {
        let malformed = valid_request().replace("0123456789abcdef0123456789abcdef", "short");
        assert_eq!(
            parse_args(&malformed).err().as_deref(),
            Some("nonce is not a 128-bit hexadecimal token")
        );
        let duplicate = valid_request().replace(
            "\"display\":\":0\"",
            "\"display\":\":0\",\"display\":\":1\"",
        );
        assert_eq!(
            parse_args(&duplicate).err().as_deref(),
            Some("duplicate request key display")
        );
    }
}
