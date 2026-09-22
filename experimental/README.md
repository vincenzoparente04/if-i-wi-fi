# Experimental

Code that exists but was never verified end to end. It is kept because it
is useful groundwork, and kept *here* so it is not mistaken for the parts we
trust.

| Path | What it is | Status |
| --- | --- | --- |
| [`host-rust/`](host-rust) | Minimal Rust workspace: `CsiFrame` codec (ADR-018), streaming Hampel filter, UDP receiver with a ring buffer, `ifi-cli capture` | ported from RuView, ~1k LOC, **never compiled in this repo** |
| [`tools/diag_paths.py`](tools/diag_paths.py) | per-path diagnostics for the radar3d mesh | used during development |

The Rust workspace is the intended foundation for running the CSI host on
the UNO Q without the Python scientific stack:

```bash
cd experimental/host-rust
cargo test --workspace
cargo run --release -p ifi-cli -- capture --bind 0.0.0.0:5555
```

[NOTES.md](NOTES.md) has the rules we set for experiments: anything that
graduates out of here needs a short note in `docs/decisions/` with the
evidence.
