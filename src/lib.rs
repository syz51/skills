pub mod cancellation;
pub mod config;
pub mod daily;
pub mod export;
pub mod message;

pub type Error = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Debug)]
pub struct CommitMessage {
    pub subject: String,
    pub body: String,
}
