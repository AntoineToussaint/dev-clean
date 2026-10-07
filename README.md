# dev-cleaner

> One command to sweep the build dirs and caches a dev machine accumulates: stale `target/`, `node_modules`, `.venv`, Go build cache, Docker leftovers, Homebrew downloads, Xcode DerivedData, cargo/npm/uv/pip/gradle caches, and more.

A single-file Python script with a friendly TUI. Useful when your laptop has 9 GB free and `go build` starts failing with *no space left on device*.

```
$ dev-cleaner --dry-run

╭──────────────────────────────────────────────────────────────────────────────╮
│  🧹  dev-cleaner                                                             │
│                                                                              │
│  sweep stale build dirs & dev caches so your disk stops filling up           │
│  disk: 68.5 GB free of 926.3 GB (93% used)                                   │
╰──────────────────────────────────────────────────────────────────────────────╯

🧹  reclaimable (project dirs idle >1d)
╭─────┬───────────┬────────┬───────────────────────────────────────────────────╮
│   # │      size │    age │ what                                              │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │           │        │ 📦 node  14.7 GB                                  │
│   1 │    1.0 GB │   6.9d │ ~/dev/saas-starter/frontend  node_modules/        │
│   2 │  970.3 MB │   7.0d │ ~/dev/saas-starter/frontend  .next/               │
│   3 │  599.8 MB │      — │ npx cache  ~/.npm/_npx  (throwaway npx installs)  │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │           │        │ 🍺 brew  6.4 GB                                   │
│   4 │    6.4 GB │      — │ brew cleanup --prune=all  (old versions, …)       │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │           │        │ 🐹 go  5.1 GB                                     │
│   5 │    5.1 GB │      — │ go build cache  ~/Library/Caches/go-build         │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │           │        │ 🦀 rust  1.9 GB                                   │
│   6 │    1.3 GB │      — │ cargo registry sources  ~/.cargo/registry/src     │
│   7 │  671.5 MB │      — │ sccache  ~/Library/Caches/Mozilla.sccache         │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │           │        │ 🐳 docker  629.4 MB                               │
│   8 │  629.4 MB │      — │ docker system prune  (stopped containers, …)      │
├─────┼───────────┼────────┼───────────────────────────────────────────────────┤
│     │  31.8 GB+ │        │ 56 item(s) — reclaimable                          │
╰─────┴───────────┴────────┴───────────────────────────────────────────────────╯
another 47.6 GB in 14 dependency cache(s) / unused image(s) with --aggressive:
uv cache 21.8 GB, go module cache 17.4 GB, npm cache 2.1 GB, +11 more

dry-run — no changes made.
```

> Formerly `rust-clean`, which only handled Rust `target/` dirs. The installer removes the old binary and `dev-cleaner` keeps reading the old config until you run `--init`.

## Install

**One-liner** (macOS + Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/AntoineToussaint/dev-clean/main/install.sh | bash
```

**Or clone:**

```bash
git clone https://github.com/AntoineToussaint/dev-clean.git
cd dev-cleaner
./install.sh
```

The installer:
- checks for [`uv`](https://docs.astral.sh/uv/) (offers to install it via the official installer if missing)
- copies `dev-cleaner` into `~/.local/bin`
- installs shell completions for your `$SHELL` (zsh / bash / fish) — opt out with `--no-completions`
- verifies `~/.local/bin` is on your `PATH` (prints the right shell-rc hint if not)
- offers to run `dev-cleaner --init` (the interactive setup wizard) if no config exists yet

**Requirements:** `uv`. Everything else is optional: each cleaner only runs if its tool or cache dir exists.

| Install flag | Effect |
|---|---|
| `--dev` | Symlink to the local checkout instead of copying. Edits propagate. (Requires clone.) |
| `--yes` | Non-interactive: auto-install `uv` if missing, auto-edit shell rc files. Pipeable: `curl ... \| bash -s -- --yes`. |
| `--no-completions` | Skip installing shell completions. |
| `--no-rc-edit` | Don't touch shell rc files; just print the lines to add. |

## Usage

```bash
dev-cleaner --dry-run          # show what would be cleaned (no changes)
dev-cleaner                    # scan, list, prompt, clean
dev-cleaner --yes              # skip the confirmation prompt
dev-cleaner --aggressive       # also wipe dependency download caches + all unused docker images
dev-cleaner --only rust,docker # restrict to some groups
dev-cleaner --skip brew        # run everything except some groups
dev-cleaner --days 7           # only sweep projects idle for 7+ days
dev-cleaner --hours 2          # anything not touched in the last 2 hours
dev-cleaner --root ~/code      # scan a specific directory for projects
dev-cleaner --list             # what each group cleans
```

## What gets cleaned

Two tiers. **Default** removes things that are pure derived output: rebuilding them costs CPU, not network. **`--aggressive`** adds dependency *download* caches (re-fetched on the next build) and every Docker image not used by a container. The default run always tells you how much more `--aggressive` would free, so nothing is hidden.

| group | default | `--aggressive` adds |
|---|---|---|
| 🦀 rust | stale `target/`; `~/.cargo/registry/src`, `~/.cargo/git/checkouts`; sccache | `~/.cargo/registry/cache` (`.crate` files), `~/.cargo/git/db` |
| 📦 node | stale `node_modules/`, `.next/`, `.nuxt/`, `.turbo/`, `.parcel-cache/`, `.svelte-kit/`, `.angular/`; npx cache; `pnpm store prune` | npm / yarn / bun caches, whole pnpm store |
| 🐍 python | stale `.venv/`, `venv/`, `.tox/`, `.nox/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`; `uv cache prune` | `uv cache clean`, pip & poetry caches, pre-commit envs |
| ☕ jvm | stale `build/`, `target/`, `.gradle/`; gradle daemon logs | `~/.gradle/caches`, wrapper dists, `~/.m2/repository`, ivy, coursier |
| 🕊️ swift | stale `.build/` | SwiftPM cache |
| 💧 elixir | stale `_build/` | `deps/` |
| ⚡ zig | stale `zig-cache/`, `zig-out/`; `~/.cache/zig` | |
| 🐫 ocaml | stale `_build/` | |
| λ haskell | stale `.stack-work/`, `dist-newstyle/` | |
| 🎯 dart | stale `build/`, `.dart_tool/` | `~/.pub-cache` |
| 🔧 cmake | stale `build/` (only if it holds `CMakeCache.txt`), `cmake-build-*` | |
| 🟣 dotnet | stale `bin/`, `obj/` | `~/.nuget/packages` |
| 🏗️ terraform | | `.terraform/` dirs, plugin cache |
| 🐘 php | | `vendor/`, composer cache |
| 🐹 go | `go clean -cache` | `go clean -modcache` |
| 🐳 docker | `docker system prune` (stopped containers, dangling images, unused networks, build cache) | `docker image prune -a` |
| 🍺 brew | `brew cleanup -s --prune=all` | `brew autoremove` |
| 🍎 xcode | DerivedData, Xcode cache, CoreSimulator caches, `simctl delete unavailable` | `iOS/watchOS/tvOS/visionOS DeviceSupport` |
| 🧰 tools | ccache, kubectl discovery cache | playwright / puppeteer / cypress browsers, electron, node-gyp, deno, helm, bazel, JetBrains caches |

**Never touched:** Docker volumes (opt-in via config, anonymous volumes only), Xcode Archives, rustup toolchains, HuggingFace / Ollama models, your Trash, `~/Library/Caches` in general. Each path is checked against the scan roots, `~` and `/` before deletion.

### What "stale" means for project dirs

A project's build dirs are stale once **no source file in the project** has been modified for `--days` (default 1). Build-dir mtimes are ignored on purpose: cargo rewrites `target/.rustc_info.json` on every invocation, node tooling touches `node_modules/.cache`, so they'd look fresh forever.

Nested projects inherit activity from their outermost parent. If you edited anything in a monorepo today, none of its packages' `node_modules` get swept — important for pnpm/yarn workspaces where nested `node_modules` are symlink farms into the root.

Global caches have no meaningful age and are listed with `—`.

## Configuration

`dev-cleaner` reads `~/.config/dev-cleaner/config.toml` (or `$XDG_CONFIG_HOME/dev-cleaner/config.toml`). CLI flags always win. If only a legacy `~/.config/rust-clean/config.toml` exists, it is used until you run `--init`.

### Interactive setup (recommended)

```bash
dev-cleaner --init
```

Scans your home directory for projects, groups them by top-level dir, and walks you through picking scan roots, dir names to ignore, the idle threshold, whether to default to aggressive mode, groups to never run, and whether Docker volume pruning is allowed. For scripted environments, `dev-cleaner --init --yes` writes static defaults.

### Config file format

```toml
# Directories to scan for projects (supports ~ expansion).
roots = ["~/development", "~/.pilot"]

# Extra directory names to skip while scanning.
ignore = ["archive", "vendored"]

# Project build dirs are stale after this many idle days.
days = 1.0

# Also clean dependency download caches (same as --aggressive).
aggressive = false

# Groups to never run. See `dev-cleaner --list`.
skip = ["brew"]

size_jobs = 16   # parallel sizing workers
clean_jobs = 4   # parallel delete workers (disk bound; keep modest)

[docker]
# Prune unused anonymous volumes too. Off by default: volumes hold data.
volumes = false
```

Check what `dev-cleaner` resolved (file + flags merged):

```bash
dev-cleaner --show-config
```

## Shell completions

The installer auto-installs completions for your `$SHELL`. To install manually:

```bash
# zsh
dev-cleaner --completion zsh > ~/.zsh/completions/_dev-cleaner
# then in ~/.zshrc: fpath=(~/.zsh/completions $fpath); autoload -Uz compinit && compinit

# bash
dev-cleaner --completion bash > ~/.local/share/dev-cleaner/completion.bash
# then in ~/.bashrc (or ~/.bash_profile on macOS): source ~/.local/share/dev-cleaner/completion.bash

# fish (auto-loaded from this path)
dev-cleaner --completion fish > ~/.config/fish/completions/dev-cleaner.fish
```

## All flags

| Flag | Default | Notes |
|---|---|---|
| `--root PATH` | `~` | Repeatable. Overrides config. |
| `--days N` | `1.0` | Project idle age in days. |
| `--hours N` | | Project idle age in hours (mutually exclusive with `--days`). |
| `-n`, `--dry-run` | off | List candidates, don't clean. |
| `-y`, `--yes` | off | Skip the y/N prompt. |
| `-a`, `--aggressive` | off | Also clean dependency download caches and all unused docker images. |
| `--only GROUPS` | all | Comma-separated groups to run. |
| `--skip GROUPS` | | Comma-separated groups to skip (merged with config). |
| `--list` | | Show the groups table and exit. |
| `--size-jobs N` | `16` | Parallel sizing workers (`du -sk`). |
| `--clean-jobs N` | `4` | Parallel delete workers. |
| `--config PATH` | `~/.config/dev-cleaner/config.toml` | Alternate config file. |
| `--init` | | Interactive wizard (or `--init --yes` for static defaults). |
| `--show-config` | | Print resolved config and exit. |
| `--completion {bash,zsh,fish}` | | Print a shell completion script and exit. |

## How it works

1. Walks each root with `os.walk`, skipping irrelevant dirs and never descending into build dirs or venvs (their `site-packages` are full of fake `pyproject.toml`s).
2. For every dir holding a project marker (`Cargo.toml`, `package.json`, `go.mod`, `*.csproj`, …) records its sibling build dirs per the rules table.
3. Measures each top-level project's last source edit in parallel; nested projects inherit it.
4. Probes the global caches: asks `go env`, `docker system df`, `brew cleanup -n`, `uv cache dir`, `pnpm store path`, and checks a list of known cache paths.
5. Sizes everything with `du -sk` in parallel, dedupes, and shows the grouped table plus a note about what `--aggressive` would add and which active projects were skipped.
6. On confirm: filesystem deletes run in parallel (read-only trees like Go's modcache are force-chmodded first); external commands (`docker system prune`, `brew cleanup`, `go clean -cache`) run one at a time. The summary reports actual freed disk, not just the estimate.

One broken tool never sinks the run: a hung Docker daemon or a failing `brew` just produces a warning line.

## Development

```bash
./install.sh --dev                              # symlink ~/.local/bin/dev-cleaner to the checkout
uv run --with pytest --with rich pytest -q      # tests
```

## Comparison

| | dev-cleaner | [kondo](https://github.com/tbillington/kondo) | [cargo-sweep](https://crates.io/crates/cargo-sweep) | [npkill](https://npkill.js.org) | `docker system prune` |
|---|---|---|---|---|---|
| project build dirs (multi-language) | ✓ | ✓ | rust only | node only | |
| global caches (go, cargo, npm, uv, gradle, …) | ✓ | | | | |
| docker / brew / xcode | ✓ | | | | docker only |
| age-based, source-mtime aware | ✓ | manual | fingerprints | | |
| safe / aggressive tiers | ✓ | | | | `-a` |
| shows disk free before/after | ✓ | | | | |
| config file + interactive setup + completions | ✓ | | | | |

## License

[MIT](./LICENSE)
