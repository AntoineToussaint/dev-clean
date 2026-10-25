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
dev-cleaner --audit            # biggest dirs on disk, and how much of each would be freed
dev-cleaner --mem              # memory hogs by program, plus PIDs, parents and commands
dev-cleaner --mem --mem-tree   # combine an app/agent with its child processes
dev-cleaner --mem --mem-watch 5 --mem-sort growth # watch which processes keep growing
dev-cleaner --docker-audit     # Docker disk, container memory and individual buildx caches
```

### Where did my disk go?

`--audit` measures your home dir (and the temp dirs) with `du`, shows every dir over 1 GB down to where the bulk sits, and puts next to each one what a default run and an `--aggressive` run would free there. Anything marked **not covered** is space dev-cleaner can't see: either a project tree outside your `roots`, or a disposable dir of some tool you can declare under `[[extra]]`. It changes nothing, and on a full disk it takes minutes.

### Which processes hold my RAM?

`--mem` shows host RAM, compression on macOS, available memory on Linux, swap use, programs ranked by memory, and individual processes with PID, parent PID, CPU, age and command. Programs are grouped by the reported executable, using its full path when available so different installations stay separate. Some processes report only a name; use the command and PID details to distinguish those. Small processes remain visible; there is no 256 MB cutoff.

```bash
dev-cleaner --mem --mem-top 20                         # more rows in each table
dev-cleaner --mem --mem-filter claude                  # match PID, executable or command
dev-cleaner --mem --mem-filter codefly --mem-tree      # matching families, including their descendants
dev-cleaner --mem --mem-filter postgres --mem-full     # full executable paths and command arguments
dev-cleaner --mem --mem-sort cpu                       # find CPU-heavy processes
dev-cleaner --mem --mem-watch 5 --mem-sort growth       # memory changes between snapshots; Ctrl-C to stop
```

Process families follow same-owner parent links, stopping at init, service managers or a missing parent. With `--mem-tree`, a filter selects whole matching families so a small agent's large database child is included. The largest individual processes still appear in a second table. Commands are shortened to fit the terminal unless you pass `--mem-full`.

Watch mode compares surviving processes with the same PID, start time, executable, owner and memory metric. New processes are marked `new`; exited processes are excluded from growth. Growth sorting becomes useful after the second snapshot. The refresh interval is a delay between scans; the report shows the actual elapsed time. macOS uses `top`'s physical footprint where available and labels RSS fallbacks; Linux uses RSS, which excludes swapped-out pages. Shared pages can appear in multiple processes, so summed memory is an estimate, not the amount stopping them will release.

CPU is the average reported by `ps`. “Old / low CPU” counts processes older than `--days` / `--hours` with CPU at most 1%; it does not prove they have been idle for that duration. Swap use alone also does not establish current memory pressure. Orphan candidates are reported across the full snapshot, along with memory held by their same-owner descendants, and a command to preview cleanup.

### What is Docker holding?

`--docker-audit` reads the local daemon's image/container/volume/build-cache totals, running containers' memory and CPU, and allocated host VM disk size. It also lists each buildx builder once, with driver, node status, last activity when reported, and reclaimable cache for running local container builders. Stopped builders stay stopped; remote or unverified endpoints are skipped. A named Docker context is resolved before deciding whether it is local. Docker cleanup uses the same local-endpoint checks.

Container-builder caches can survive daemon-level cache pruning. Builder estimates are shown separately from daemon and VM disk totals because these views can overlap. Some cache queries can take a while; unavailable measurements display `?`.

The RAM visible to the daemon is capacity, not the VM's host memory consumption. Compare container memory with OrbStack/Docker/Colima processes in `--mem`; the difference also includes guest caches, services and differences in accounting. A large difference alone does not explain slow container starts.

The audit changes nothing. Cache pruning means later rebuilds; removing a builder also removes its configuration. Restarting the runtime interrupts its workloads, and deleting volumes can remove persistent data. Preview the existing cleanup actions with `dev-cleaner --only docker --dry-run`. Space freed inside a VM may take time to return to the host filesystem.

### Faster scans, same commands

Use `dev-cleaner --mem` for memory diagnostics: it does not discover projects or size build directories. Plain `dev-cleaner` performs a disk-cleanup scan. Its default root is `~`, so it discovers projects across every checkout and worktree under directories such as `~/.lazybox`; a large worktree forest can take minutes. For a focused disk scan, pass `--root ~/development` or set narrower `roots` in the config.

Global cleaner checks run up to four at a time while project discovery runs. Docker audits read daemon disk usage, container memory and builder inventories concurrently, and measure up to four builder caches at once. The normal Docker scan also measures builder caches concurrently. Results retain their usual order and one failed probe does not hide other groups. Directory sizing still uses `--size-jobs` (default 16).

Project discovery shows a directory count and current path throughout the walk, even where no build directories are found. Activity checks stop as soon as a recent source establishes that a project must be kept. Normal cleanup skips sizing active projects' build directories; `--audit` measures them when you want the full disk breakdown. Stale projects still receive the full activity check before qualifying for cleanup.

This overlaps waits for independent tools without adding setup or flags. A single slow daemon request can still take its timeout. In a controlled benchmark of six simulated 100 ms builder reads, the parallel version took 0.21 seconds versus 0.62 seconds sequentially; actual speedups depend on the tools and workload.

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
| 🐹 go | `go clean -cache`; gopls, goimports, golangci-lint, staticcheck caches | `go clean -modcache` |
| 🐳 docker | `docker system prune` (stopped containers, dangling images, unused networks); `docker builder prune -a`; `docker buildx prune` for every local `docker-container` builder (their cache lives in the builder's own container, so `system prune` never sees it); unused volumes if enabled in config | `docker image prune -a` |
| 🍺 brew | `brew cleanup -s --prune=all` | `brew autoremove` |
| 🍎 xcode | DerivedData, Xcode cache, CoreSimulator caches, `simctl delete unavailable` | `iOS/watchOS/tvOS/visionOS DeviceSupport` |
| 🧰 tools | ccache, kubectl discovery cache | playwright / puppeteer / cypress browsers, electron, node-gyp, deno, trivy, helm, bazel, JetBrains caches |
| 🗑️ tmp | entries of `/tmp` and `$TMPDIR` idle past the threshold; idle Claude Code session scratch (`/tmp/claude-<uid>/…`) | |
| 📁 extra | idle children of the dirs you list under `[[extra]]` | |
| 🧟 procs | stops idle orphaned processes (parent gone) that run from a temp dir or a `[procs] orphan_dirs` dir | |

**Orphaned processes.** Test binaries and tool plugins can outlive the run that started them and keep holding RAM. A process qualifies for cleanup only when it is yours, has parent PID 1, is older than the configured threshold, has `ps` CPU at most 1%, and its executable lives in a temp dir (`go test` binaries, throwaway builds) or a dir you listed under `[procs] orphan_dirs`. PID 1 alone does not prove a process is abandoned; only list plugin directories whose processes should end with their parent run. Cleanup rechecks the process's executable, owner and start time to avoid signalling a recycled PID. It sends SIGTERM first so a supervisor can tear down its children, then SIGKILL after 8 seconds if it remains. Children are not independently targeted. Its row shows RAM, which is never added to the disk total. Use `--mem` to investigate and `--only procs --dry-run` to preview candidates.

**Temp dirs.** macOS only clears `/tmp` on reboot and `$TMPDIR` after 3 days, and build tools and agents can fill both with gigabytes of throwaway Go caches and checkouts in a day. An entry goes only when it belongs to you, nothing in its top two levels changed within the threshold, and it holds no sockets or pipes (a live program's IPC). `com.apple.*`, `tmux-*`, `ssh-*` and the like are always kept. Each entry is checked again right before it is removed, so one that came back into use since the scan is left alone.

**Docker on macOS.** The images live inside a VM disk image (OrbStack, Docker Desktop, Colima). dev-cleaner reports how much that image holds even when the daemon is down, which is usually because the disk filled up. In that case, free some space, start the daemon and re-run.

**Never touched:** anything across a mount point (`~/OrbStack` is the Docker VM's live filesystem), Docker volumes unless enabled in config, Xcode Archives, rustup toolchains, HuggingFace / Ollama models, your Trash, `~/Library/Caches` in general. Each path is checked against the scan roots, `~` and `/` before deletion.

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
# Prune unused volumes too. Off by default: volumes hold data.
# true = anonymous volumes only, "all" = named ones as well.
volumes = false

[procs]
# Orphaned processes are stopped when their executable lives in a temp dir.
# Add dirs whose executables only ever run under a parent (a plugin dir).
orphan_dirs = ["~/.mytool/plugins"]

# Disposable dirs of your own tools: children idle longer than `days` are
# removed (`days` defaults to the global one). Repeat the block per dir.
[[extra]]
path = "~/Library/Caches/mytool"
days = 3
```

`roots = ["~"]` is a fine default: the walk skips `~/Library`, package-manager homes and mount points, and finding projects is fast. Narrow roots miss every project that lands next to them later (new clones, worktrees).

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
| `--audit` | | Show the biggest dirs and how much of each would be freed; change nothing. |
| `--docker-audit` | | Read-only Docker disk, container memory and per-builder cache inventory. |
| `--mem` | | Read-only host memory, programs, individual PIDs and orphan candidates. |
| `--mem-top N` | `10` | Rows per memory table; requires `--mem`. |
| `--mem-filter TEXT` | | Case-insensitive match on PID, executable or command. With `--mem-tree`, selects whole matching families. |
| `--mem-tree` | off | Group memory by process family; requires `--mem`. |
| `--mem-full` | off | Wrap full paths and commands instead of shortening them; requires `--mem`. |
| `--mem-sort {memory,cpu,growth}` | `memory` | Sort both memory tables. Growth requires `--mem-watch`. |
| `--mem-watch SECONDS` | | Refresh memory, showing deltas since the previous snapshot; requires `--mem`. Ctrl-C stops. |
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
4. Probes the global caches: asks `go env`, `docker system df`, `docker buildx ls`/`du`, `brew cleanup -n`, `uv cache dir`, `pnpm store path`, checks a list of known cache paths, and lists idle entries in the temp dirs and `[[extra]]` dirs.
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
