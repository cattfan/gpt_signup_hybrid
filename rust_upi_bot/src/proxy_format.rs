//! Proxy line parsing + `{SID}` placeholder materialization.
//!
//! Port 1:1 từ `web/proxy_format.py` (Python upi). Mọi consumer feed proxy URL
//! cho `reqwest::Proxy` PHẢI gọi [`materialize_proxy`] trước — pool/store lưu
//! raw line/template để hỗ trợ rotate sticky session.
//!
//! Format hỗ trợ:
//!   - `host:port`                       → `http://host:port` (no-auth)
//!   - `host:port:user:pass`             → `http://user:pass@host:port`
//!   - `host:port:user`                  → `http://user@host:port` (pass rỗng)
//!   - `scheme://user:pass@host:port`    → giữ nguyên (URL form, backward-compat)
//!
//! Placeholder `{SID}` / `{sid}` (case-insensitive) ở user và/hoặc pass được
//! thay bằng **cùng 1 SID** ngẫu nhiên mỗi lần materialize → 1 base line =
//! vô hạn sticky session khác IP.
//!
//! Đây là single source of truth cho `mask_proxy` (gộp impl cũ ở
//! `upi/runner.rs::mask_proxy`).

use anyhow::{anyhow, Result};
use once_cell::sync::Lazy;
use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
use rand::Rng;
use regex::Regex;

/// Match cả `{SID}` lẫn `{sid}` (case-insensitive). User import thường viết
/// chữ thường: `user-{sid}:pass`.
static SID_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\{sid\}").unwrap());

/// Strip credential khỏi proxy URL nhúng trong text bất kỳ (exception/detail)
/// trước khi log. Phủ cả URL materialized random SID mà masker theo-string
/// không biết trước để replace.
static PROXY_CRED_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"//[^/@\s]+@").unwrap());

/// `quote(safe="")` của Python — encode mọi non-alphanumeric trừ `_-.~`.
const QUOTE_NONE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'_')
    .remove(b'-')
    .remove(b'.')
    .remove(b'~');

const SID_ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789";

/// Random sticky-session id `[a-z0-9]{length}`.
pub fn gen_sid(length: usize) -> String {
    let mut rng = rand::thread_rng();
    (0..length)
        .map(|_| {
            let idx = rng.gen_range(0..SID_ALPHABET.len());
            SID_ALPHABET[idx] as char
        })
        .collect()
}

/// True nếu line chứa placeholder `{SID}` / `{sid}`.
pub fn has_template(line: &str) -> bool {
    !line.is_empty() && SID_RE.is_match(line)
}

/// Thay `//user:pass@` → `//***@` trong text bất kỳ (chống leak creds).
pub fn sanitize_proxy_text(text: &str) -> String {
    PROXY_CRED_RE.replace_all(text, "//***@").into_owned()
}

/// Line/template → concrete proxy URL `http://user:pass@host:port`.
///
/// - Thay **mọi** `{SID}`/`{sid}` bằng **cùng 1 SID** (gen 1 lần/call).
/// - Credential URL-encoded → password chứa `@`/`:`/`/` không phá `urlparse`.
/// - URL form (có `://`) → passthrough (chỉ thay SID, không re-parse/re-encode).
/// - Credential-at form `user:pass@host:port` (có `@`, không `://`) → prepend
///   `http://` passthrough (format phổ biến của BrightData/CliProxy/Oxylabs).
///
/// Trả `Err` nếu line rỗng hoặc không đủ `host:port`.
pub fn materialize_proxy(line: &str, sid_len: usize) -> Result<String> {
    let line = line.trim();
    if line.is_empty() {
        return Err(anyhow!("empty proxy line"));
    }

    // SID thay TRƯỚC khi parse/quote → 1 SID phủ cả user+pass; SID là [a-z0-9]
    // không chứa ':' nên split phía dưới an toàn.
    let line: String = if has_template(line) {
        let sid = gen_sid(sid_len);
        SID_RE.replace_all(line, sid.as_str()).into_owned()
    } else {
        line.to_string()
    };

    // URL form: caller tự chuẩn → giữ nguyên (không re-encode để khỏi double-quote).
    if line.contains("://") {
        return Ok(line);
    }

    // ── Credential-at form: `[user[:pass]@]host:port` ──────────────────────
    // Nhiều proxy provider cung cấp format `user:pass@host:port` (KHÔNG có
    // scheme `://`). Nếu có `@` → tách bằng `@` cuối cùng; phần SAU phải
    // match `host:port` (port toàn digit) → prepend `http://` + passthrough.
    // Nếu phần SAU @ KHÔNG match host:port → fallback colon-split path cũ
    // (backward-compat cho edge case user@domain trong colon-form).
    if let Some(at_pos) = line.rfind('@') {
        let after_at = &line[at_pos + 1..];
        // Check after_at = "host:port" — port phải toàn digits, > 0.
        if let Some(colon) = after_at.rfind(':') {
            let host = &after_at[..colon];
            let port = &after_at[colon + 1..];
            if !host.is_empty()
                && !port.is_empty()
                && port.chars().all(|c| c.is_ascii_digit())
            {
                // Confirmed: `user[:pass]@host:port` form → prepend http://
                return Ok(format!("http://{}", line));
            }
        }
        // after_at không match host:port → fall through vào colon-split path.
    }

    // maxsplit=3 → pass giữ được dấu ':'
    let parts: Vec<&str> = line.splitn(4, ':').collect();
    match parts.len() {
        2 => {
            let host = parts[0];
            let port = parts[1];
            if host.is_empty() || port.is_empty() {
                return Err(anyhow!("invalid proxy format: empty host or port: {:?}", line));
            }
            Ok(format!("http://{}:{}", host, port))
        }
        3 => {
            let (host, port, user) = (parts[0], parts[1], parts[2]);
            if host.is_empty() || port.is_empty() {
                return Err(anyhow!("invalid proxy format: empty host or port: {:?}", line));
            }
            let user_q = utf8_percent_encode(user, QUOTE_NONE).to_string();
            Ok(format!("http://{}@{}:{}", user_q, host, port))
        }
        4 => {
            let (host, port, user, pwd) = (parts[0], parts[1], parts[2], parts[3]);
            if host.is_empty() || port.is_empty() {
                return Err(anyhow!("invalid proxy format: empty host or port: {:?}", line));
            }
            let user_q = utf8_percent_encode(user, QUOTE_NONE).to_string();
            let pwd_q = utf8_percent_encode(pwd, QUOTE_NONE).to_string();
            Ok(format!("http://{}:{}@{}:{}", user_q, pwd_q, host, port))
        }
        _ => Err(anyhow!(
            "invalid proxy format (need host:port[:user[:pass]]): {:?}",
            line
        )),
    }
}

/// Mask credential cho log/UI. Empty → `"direct"`.
///
/// Xử cả 2 shape (store lưu raw line colon-form, log dùng URL materialized):
///   - URL form  `scheme://user:pass@host:port` → `scheme://***@host:port`
///   - colon raw `host:port:user:pass`           → `***@host:port`
///   - no-auth (`host:port` hoặc `scheme://host:port`) → trả nguyên (không creds).
pub fn mask_proxy(url: &str) -> String {
    if url.is_empty() {
        return "direct".into();
    }
    // URL form (có scheme)
    if let Some(scheme_end) = url.find("://") {
        let scheme = &url[..scheme_end];
        let rest = &url[scheme_end + 3..];
        if !rest.contains('@') {
            return url.to_string(); // no-auth URL
        }
        let host_part = rest.rsplit('@').next().unwrap_or("");
        return format!("{}://***@{}", scheme, host_part);
    }
    // "user:pass@host:port" no-scheme — hiếm
    if url.contains('@') {
        let host_part = url.rsplit('@').next().unwrap_or("");
        return format!("***@{}", host_part);
    }
    // colon-form raw line: host:port[:user[:pass]]
    let parts: Vec<&str> = url.splitn(4, ':').collect();
    if parts.len() >= 3 {
        return format!("***@{}:{}", parts[0], parts[1]);
    }
    url.to_string() // host:port / host-only — không creds
}

/// Validate proxy line (gọi khi user gõ `/proxy_set`). Trả `Ok(masked_line)` để
/// log/echo, `Err` với mô tả nếu format sai. Materialize 1 lần với SID dummy
/// để confirm syntax đầy đủ — nếu line có `{SID}` template thì sample materialize.
pub fn validate_and_mask(line: &str) -> Result<String> {
    let _ = materialize_proxy(line, 8)?;
    Ok(mask_proxy(line))
}

/// Chuẩn hóa `socks5://` → `socks5h://` (remote DNS). Idempotent: `socks5h://`
/// KHÔNG match prefix `socks5://` nên gọi nhiều lần an toàn. Mirror logic
/// `LoginClient::new` để probe/login/runner dùng CHUNG 1 dạng URL → tránh
/// phân kỳ giữa stack probe (reqwest) và stack login (wreq).
pub fn normalize_socks(url: String) -> String {
    if let Some(rest) = url.strip_prefix("socks5://") {
        format!("socks5h://{}", rest)
    } else {
        url
    }
}

/// Materialize line → concrete URL ĐÃ normalize cho HTTP client. Đây là single
/// source mọi consumer (login, runner pool, probe) PHẢI dùng để materialize
/// proxy trước khi feed reqwest/wreq → đảm bảo cùng 1 URL shape.
pub fn materialize_for_client(line: &str, sid_len: usize) -> Result<String> {
    Ok(normalize_socks(materialize_proxy(line, sid_len)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_port() {
        assert_eq!(
            materialize_proxy("1.2.3.4:8080", 8).unwrap(),
            "http://1.2.3.4:8080"
        );
    }

    #[test]
    fn host_port_user_pass() {
        let url = materialize_proxy("h:80:u:p", 8).unwrap();
        assert_eq!(url, "http://u:p@h:80");
    }

    #[test]
    fn url_form_passthrough() {
        let s = "socks5://u:p@h:1080";
        assert_eq!(materialize_proxy(s, 8).unwrap(), s);
    }

    #[test]
    fn sid_template_replaced_once() {
        let url = materialize_proxy("h:80:user-{sid}:pass-{SID}", 8).unwrap();
        // user và pass có cùng SID
        let prefix = "http://user-";
        let after = &url[prefix.len()..];
        let (sid_a, rest) = after.split_once(':').unwrap();
        let (sid_b, _) = rest.split_once('@').unwrap();
        assert!(sid_b.starts_with("pass-"));
        assert_eq!(sid_a, &sid_b["pass-".len()..]);
        assert_eq!(sid_a.len(), 8);
    }

    #[test]
    fn mask_url_form() {
        assert_eq!(
            mask_proxy("http://u:p@1.2.3.4:8080"),
            "http://***@1.2.3.4:8080"
        );
    }

    #[test]
    fn mask_colon_form() {
        assert_eq!(mask_proxy("h:80:u:p"), "***@h:80");
    }

    #[test]
    fn mask_no_creds() {
        assert_eq!(mask_proxy("1.2.3.4:8080"), "1.2.3.4:8080");
        assert_eq!(mask_proxy("http://1.2.3.4:8080"), "http://1.2.3.4:8080");
    }

    #[test]
    fn empty_rejected() {
        assert!(materialize_proxy("", 8).is_err());
        assert!(materialize_proxy("   ", 8).is_err());
    }

    #[test]
    fn pass_with_special_chars_encoded() {
        let url = materialize_proxy("h:80:u:p@ss/word", 8).unwrap();
        // '@' phải được encode → %40, '/' → %2F
        assert!(url.contains("p%40ss%2Fword"));
    }

    #[test]
    fn mask_empty_is_direct() {
        assert_eq!(mask_proxy(""), "direct");
    }

    // ── Credential-at form tests ─────────────────────────────────────────
    #[test]
    fn credential_at_form_user_pass() {
        // Format phổ biến BrightData/CliProxy: user:pass@host:port
        assert_eq!(
            materialize_proxy("9g5r1200918-region-IN-sid-TsM3NwMX-t-120:kpra1kp6@sg.cliproxy.io:3010", 8).unwrap(),
            "http://9g5r1200918-region-IN-sid-TsM3NwMX-t-120:kpra1kp6@sg.cliproxy.io:3010"
        );
    }

    #[test]
    fn credential_at_form_user_only() {
        // user@host:port (no password)
        assert_eq!(
            materialize_proxy("myuser@proxy.example.com:8080", 8).unwrap(),
            "http://myuser@proxy.example.com:8080"
        );
    }

    #[test]
    fn credential_at_form_with_sid() {
        // user-{sid}:pass@host:port
        let url = materialize_proxy("user-{sid}:pass@sg.cliproxy.io:3010", 8).unwrap();
        assert!(url.starts_with("http://user-"));
        assert!(url.ends_with("@sg.cliproxy.io:3010"));
    }

    #[test]
    fn colon_form_user_with_at_in_pass() {
        // host:port:user:p@ss — colon form nhưng pass chứa @ →
        // rfind('@') → after_at = "ss" → không có ':' → fallback colon path
        // → encode p@ss → %40
        let url = materialize_proxy("h:80:u:p@ss", 8).unwrap();
        assert!(url.contains("p%40ss"));
        assert!(url.starts_with("http://u:"));
    }
}
