use anyhow::{ensure, Context, Result};
use std::{env, path::PathBuf, time::Duration};

#[path = "../collector.rs"]
mod collector;

fn main() -> Result<()> {
    // DNS may outlive cancellation in the system resolver. Bound its blocking
    // executor and avoid waiting indefinitely for those threads at shutdown.
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(4)
        .build()?;
    let result = runtime.block_on(run());
    runtime.shutdown_timeout(Duration::from_millis(500));
    result
}

async fn run() -> Result<()> {
    let mut config = PathBuf::from("examples/collector.toml");
    let mut once = false;
    let mut status = false;
    let mut export = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" => config = args.next().context("--config requires a path")?.into(),
            "--once" => once = true,
            "--status" => status = true,
            "--export" => export = Some(args.next().context("--export requires stdout or a path")?),
            "--help" | "-h" => {
                println!(
                    "cauce-collector [--config PATH] [--once | --status | --export stdout|PATH]"
                );
                return Ok(());
            }
            other => anyhow::bail!("unknown argument {other}"),
        }
    }
    ensure!(
        usize::from(once) + usize::from(status) + usize::from(export.is_some()) <= 1,
        "--once, --status and --export are mutually exclusive"
    );
    let config = collector::Config::load(&config)?;
    let mut state = collector::State::open(&config)?;
    if status {
        println!("{}", serde_json::to_string(&state.status(&config)?)?);
        return Ok(());
    }
    if let Some(destination) = export {
        state.export(&destination, &config)?;
        return Ok(());
    }

    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        result = collect(&mut state, &config, once) => result,
        _ = terminate.recv() => Ok(()),
        result = tokio::signal::ctrl_c() => { result?; Ok(()) },
    }
}

async fn collect(
    state: &mut collector::State,
    config: &collector::CollectorConfig,
    once: bool,
) -> Result<()> {
    let mut retry = config.backoff_min_ms;
    let mut next_send = tokio::time::Instant::now();
    loop {
        state.scan_one(config)?;
        if config.delivery == collector::Delivery::ProposalHttp
            && tokio::time::Instant::now() >= next_send
        {
            match state.deliver_one(config).await {
                Ok(true) => {
                    retry = config.backoff_min_ms;
                    next_send = tokio::time::Instant::now();
                }
                Ok(false) => {}
                Err(error) if once => return Err(error),
                Err(error) => {
                    eprintln!("cauce-collector: proposal transport attempt failed: {error:#}");
                    let jitter = rand_jitter() % (retry / 4 + 1);
                    let delay = retry.saturating_add(jitter).min(config.backoff_max_ms);
                    next_send = tokio::time::Instant::now() + Duration::from_millis(delay);
                    retry = retry.saturating_mul(2).min(config.backoff_max_ms);
                }
            }
        }
        if once {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(config.poll_ms)).await;
    }
}

fn rand_jitter() -> u64 {
    let value = uuid::Uuid::new_v4();
    u64::from_be_bytes(value.as_bytes()[..8].try_into().expect("UUID bytes"))
}
