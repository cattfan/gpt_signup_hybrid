//! Telegram bot client — minimal long-poll wrapper trên `reqwest`.
//!
//! Hỗ trợ:
//!   - getUpdates long-poll
//!   - sendMessage (bao gồm reply_to + parse_mode)
//!   - sendDocument (multipart upload PNG QR)
//!   - getFile + download (lấy session.json user upload).

use anyhow::{anyhow, Result};
use reqwest::multipart::{Form, Part};
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::Path;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct TelegramClient {
    base_url: String,
    file_base_url: String,
    http: Client,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Update {
    pub update_id: i64,
    pub message: Option<Message>,
    pub callback_query: Option<CallbackQuery>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CallbackQuery {
    pub id: String,
    pub from: User,
    pub message: Option<Message>,
    pub data: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Message {
    pub message_id: i64,
    pub chat: Chat,
    pub from: Option<User>,
    pub text: Option<String>,
    pub document: Option<Document>,
    pub caption: Option<String>,
    pub date: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Chat {
    pub id: i64,
    #[serde(rename = "type")]
    pub chat_type: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct User {
    pub id: i64,
    pub username: Option<String>,
    pub first_name: Option<String>,
    pub is_bot: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Document {
    pub file_id: String,
    pub file_name: Option<String>,
    pub file_size: Option<u64>,
    pub mime_type: Option<String>,
}

impl TelegramClient {
    pub fn new(token: &str) -> Result<Self> {
        let http = Client::builder()
            .timeout(Duration::from_secs(120))
            .build()?;
        Ok(Self {
            base_url: format!("https://api.telegram.org/bot{}", token),
            file_base_url: format!("https://api.telegram.org/file/bot{}", token),
            http,
        })
    }

    pub async fn get_updates(&self, offset: i64, timeout: u64) -> Result<Vec<Update>> {
        let url = format!("{}/getUpdates", self.base_url);
        let resp = self
            .http
            .get(&url)
            .query(&[
                ("offset", offset.to_string()),
                ("timeout", timeout.to_string()),
                ("allowed_updates", r#"["message","callback_query"]"#.into()),
            ])
            .send()
            .await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "getUpdates fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        let updates: Vec<Update> = serde_json::from_value(
            v.get("result").cloned().unwrap_or(Value::Array(vec![])),
        )?;
        Ok(updates)
    }

    /// Đăng ký bot commands cho menu Telegram (`/`).
    /// `commands` — slice (command_name_no_slash, description).
    pub async fn set_my_commands(&self, commands: &[(&str, &str)]) -> Result<()> {
        let url = format!("{}/setMyCommands", self.base_url);
        let cmds: Vec<Value> = commands
            .iter()
            .map(|(c, d)| json!({"command": *c, "description": *d}))
            .collect();
        let body = json!({ "commands": cmds });
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "setMyCommands fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(())
    }

    /// Trả lời callback query (đóng spinner trên client). `text` optional —
    /// nếu set sẽ hiện toast nhỏ.
    pub async fn answer_callback_query(
        &self,
        callback_id: &str,
        text: Option<&str>,
    ) -> Result<()> {
        let url = format!("{}/answerCallbackQuery", self.base_url);
        let mut body = json!({ "callback_query_id": callback_id });
        if let Some(t) = text {
            body["text"] = json!(t);
        }
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            tracing::debug!(
                "answerCallbackQuery warn: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            );
        }
        Ok(())
    }

    pub async fn send_message(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
    ) -> Result<i64> {
        self.send_message_inner(chat_id, text, reply_to, None).await
    }

    /// Gửi message kèm inline keyboard. `keyboard` là JSON array of array of
    /// button objects (chuẩn Telegram InlineKeyboardMarkup).
    pub async fn send_message_kb(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
        keyboard: Value,
    ) -> Result<i64> {
        self.send_message_inner(chat_id, text, reply_to, Some(keyboard))
            .await
    }

    async fn send_message_inner(
        &self,
        chat_id: i64,
        text: &str,
        reply_to: Option<i64>,
        reply_markup: Option<Value>,
    ) -> Result<i64> {
        let url = format!("{}/sendMessage", self.base_url);
        let mut body = json!({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        if let Some(r) = reply_to {
            body["reply_to_message_id"] = json!(r);
        }
        if let Some(kb) = reply_markup {
            body["reply_markup"] = json!({ "inline_keyboard": kb });
        }
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendMessage fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(v["result"]["message_id"].as_i64().unwrap_or(0))
    }

    pub async fn edit_message_text(
        &self,
        chat_id: i64,
        message_id: i64,
        text: &str,
    ) -> Result<()> {
        let url = format!("{}/editMessageText", self.base_url);
        let body = json!({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": true,
        });
        let resp = self.http.post(&url).json(&body).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            tracing::debug!(
                "editMessageText warn: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            );
        }
        Ok(())
    }

    pub async fn send_photo(
        &self,
        chat_id: i64,
        photo_path: &Path,
        caption: Option<&str>,
        reply_to: Option<i64>,
    ) -> Result<()> {
        let url = format!("{}/sendPhoto", self.base_url);
        let bytes = std::fs::read(photo_path)?;
        let part = Part::bytes(bytes)
            .file_name(
                photo_path
                    .file_name()
                    .map(|s| s.to_string_lossy().to_string())
                    .unwrap_or_else(|| "qr.png".into()),
            )
            .mime_str("image/png")?;
        let mut form = Form::new()
            .text("chat_id", chat_id.to_string())
            .part("photo", part);
        if let Some(c) = caption {
            form = form.text("caption", c.to_string());
        }
        if let Some(r) = reply_to {
            form = form.text("reply_to_message_id", r.to_string());
        }
        let resp = self.http.post(&url).multipart(form).send().await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "sendPhoto fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        Ok(())
    }

    pub async fn get_file_path(&self, file_id: &str) -> Result<String> {
        let url = format!("{}/getFile", self.base_url);
        let resp = self
            .http
            .get(&url)
            .query(&[("file_id", file_id)])
            .send()
            .await?;
        let v: Value = resp.json().await?;
        if v.get("ok").and_then(|b| b.as_bool()) != Some(true) {
            return Err(anyhow!(
                "getFile fail: {}",
                v.get("description").and_then(|s| s.as_str()).unwrap_or("?")
            ));
        }
        let path = v["result"]["file_path"]
            .as_str()
            .ok_or_else(|| anyhow!("getFile missing file_path"))?
            .to_string();
        Ok(path)
    }

    pub async fn download_file(&self, file_path: &str) -> Result<bytes::Bytes> {
        let url = format!("{}/{}", self.file_base_url, file_path);
        let resp = self.http.get(&url).send().await?;
        Ok(resp.bytes().await?)
    }
}
