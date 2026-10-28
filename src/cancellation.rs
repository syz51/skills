//! Process-wide cancellation is installed only by the application entrypoint.
use crate::Result;
use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicBool, Ordering},
};

static TOKEN: OnceLock<Arc<AtomicBool>> = OnceLock::new();

pub fn install() -> Result<()> {
    let token = TOKEN.get_or_init(|| Arc::new(AtomicBool::new(false)));
    signal_hook::flag::register(signal_hook::consts::SIGINT, token.clone())?;
    signal_hook::flag::register(signal_hook::consts::SIGTERM, token.clone())?;
    Ok(())
}

pub fn token() -> Option<Arc<AtomicBool>> {
    TOKEN.get().cloned()
}
pub fn requested() -> bool {
    TOKEN.get().is_some_and(|t| t.load(Ordering::Relaxed))
}
