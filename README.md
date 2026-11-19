# dev-cleaner

> One command to sweep the build dirs and caches a dev machine accumulates: stale `target/`, `node_modules`, `.venv`, Go build cache, Docker leftovers, Homebrew downloads, Xcode DerivedData, cargo/npm/uv/pip/gradle caches, and more.

A single-file Python script with a compact terminal report. Useful when your laptop has 9 GB free and `go build` starts failing with *no space left on device*.

```
$ dev-cleaner --dry-run --sizes

dev-cleaner
  disk    68.5 GB free of 926.3 GB  ━━━━━━━━━━━━━━━━━━━━━━━━  93% used
  memory  9.1 GB compressed of 48.0 GB · 2.3 GB swapped
  cpu     load 6.2 on 15 cores

Will clean  56 targets · project builds idle over 1d · frees 31.8 GB+
  node    41  14.7 GB  node_modules/, .next/, npx cache, pnpm store prune
  brew     1   6.4 GB  brew cleanup --prune=all
  go       2   5.1 GB  go build cache, golangci-lint cache
  rust     7   1.9 GB  target/, cargo registry sources, sccache
  docker   5  629.4 MB+  docker system prune, docker builder prune -a

Left alone  add the flag to include it
  --hours N     6 build dir(s) in 6 project(s) edited within 1d holding 3.2 GB
  --aggressive  47.6 GB in 14 dependency cache(s) / unused image(s): uv cache 21.8 GB,
                go module cache 17.4 GB, npm cache 2.1 GB, +11 more

  --verbose lists every path · --audit shows where the disk went

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
dev-cleaner --dry-run --sizes  # also measure byte counts (slower)
dev-cleaner                    # scan disk, memory and CPU, then review and pick what to clean
dev-cleaner --yes              # clean the plan without asking (cron, scripts, agents)
dev-cleaner --json --yes       # the same, with the plan and each result as JSON on stdout
dev-cleaner --plain            # no review screen: print the plan, ask yes/no
dev-cleaner --aggressive       # also wipe dependency download caches + all unused docker images
dev-cleaner --only rust,docker # restrict to some groups
dev-cleaner --skip brew        # run everything except some groups
dev-cleaner --days 7           # only sweep projects idle for 7+ days
dev-cleaner --hours 2          # anything not touched in the last 2 hours
dev-cleaner --root ~/code      # scan a specific directory for projects
dev-cleaner --list             # what each group cleans
dev-cleaner --audit            # biggest dirs on disk, and how much of each would be freed
dev-cleaner --mem              # memory hogs by program, plus PIDs, parents and commands
dev-cleaner --mem --tree       # combine an app/agent with its child processes
dev-cleaner --mem --mem-watch 5 --mem-sort growth # watch which processes keep growing
dev-cleaner --cpu              # what is burning CPU right now, and what has been for hours
dev-cleaner --docker-audit     # Docker disk, container memory and individual buildx caches
```

### Review screen

On a terminal, a run ends in a review screen instead of a yes/no question:

```
Will clean  project builds idle over 1d
   ▾ [-] node     6/8  11.5 GB+ node_modules, pnpm store prune  +1 aggressive, off
     [x]         1.5d    3.4 GB ~/dev/app0  node_modules
 ❯   [ ]         2.5d    3.0 GB ~/dev/app1  node_modules
     [ ]                11.2 GB npm cache  ~/.npm  aggressive
   ▸ [x] go         1    6.5 GB go build cache
   ▸ [-] docker   1/2         — docker system prune  +1 aggressive, off

Selected 9 of 12 targets  frees 18.1 GB+ and 2.8 GB RAM
```

| Key | Does |
|---|---|
| `↑` `↓` (`j` `k`), PgUp/PgDn | Move. |
| `space` | Tick or untick the group or target under the cursor. |
| `→` `←` | Open or close a group to see its targets. |
| `a` | Add or drop the aggressive tier (dependency caches, unused images). |
| `n`, `d` | Untick everything; return to the plan. |
| `s` | Measure sizes, when the run did not use `--sizes`. |
| `enter`, then `y` | Clean the ticked targets. |
| `q` | Leave without cleaning. |

### Unattended runs

`--yes` cleans the plan without asking and `--dry-run` only prints it; neither opens the review screen. Without a terminal on stdin and without `--yes`, nothing is cleaned and the exit code is 2.

`--json` writes one JSON object to stdout and sends the human-readable output to stderr. Alone it is a preview (`"dry_run": true`); with `--yes` it cleans and adds a `status` (`cleaned`, `incomplete`, `failed`) to every target and a `result` with the counts and `freed_bytes`.

Exit codes: 0 when everything selected was cleaned or nothing needed cleaning, 1 when a cleanup was incomplete or failed, 2 for a usage error or a run that had no way to confirm.

### Where did my disk go?

`--audit` measures your home dir (and the temp dirs) with `du`, shows every dir over 1 GB down to where the bulk sits, and puts next to each one what a default run and an `--aggressive` run would free there. Anything marked **not covered** is space dev-cleaner can't see: either a project tree outside your `roots`, or a disposable dir of some tool you can declare under `[[extra]]`. It changes nothing. The dirs two levels down are each measured by their own `du`, `--size-jobs` at a time, with a running count of dirs done and bytes found; on a full disk it still takes minutes. A dir whose measurement fails is named under the table instead of silently missing from it.

### Which processes hold my RAM?

`--mem` shows host RAM, compression on macOS, available memory on Linux, swap use, programs ranked by memory, and individual processes with PID, parent PID, CPU, age and command. Programs are grouped by the reported executable, using its full path when available so different installations stay separate. Some processes report only a name; use the command and PID details to distinguish those. Small processes remain visible; there is no 256 MB cutoff.

```bash
dev-cleaner --mem --top 20                             # more rows in each table
dev-cleaner --mem --filter claude                      # match PID, executable or command
dev-cleaner --mem --filter codefly --tree              # matching families, including their descendants
dev-cleaner --mem --filter postgres --full             # full executable paths and command arguments
dev-cleaner --mem --mem-sort cpu                       # find CPU-heavy processes
dev-cleaner --mem --mem-watch 5 --mem-sort growth       # memory changes between snapshots; Ctrl-C to stop
```

Process families follow same-owner parent links, stopping at init, service managers or a missing parent. With `--tree`, a filter selects whole matching families so a small agent's large database child is included. The largest individual processes still appear in a second table. Commands are shortened to fit the terminal unless you pass `--full`. The older `--mem-top`, `--mem-filter`, `--mem-tree` and `--mem-full` spellings still work.

Watch mode compares surviving processes with the same PID, start time, executable, owner and memory metric. New processes are marked `new`; exited processes are excluded from growth. Growth sorting becomes useful after the second snapshot. The refresh interval is a delay between scans; the report shows the actual elapsed time. macOS uses `top`'s physical footprint where available and labels RSS fallbacks; Linux uses RSS, which excludes swapped-out pages. Shared pages can appear in multiple processes, so summed memory is an estimate, not the amount stopping them will release.

CPU is the average reported by `ps`. “Old / low CPU” counts processes older than `--days` / `--hours` with CPU at most 1%; it does not prove they have been idle for that duration. Swap use alone also does not establish current memory pressure. Orphan candidates are reported across the full snapshot, along with memory held by their same-owner descendants, and a command to preview cleanup.

**Silent terminal sessions.** Agents and shells left open in tmux panes or terminal tabs keep their memory, and their language servers', for as long as the terminal exists. `--mem` lists the sessions whose terminal has carried no input or output for a day (or `--days` / `--hours`, or `idle_session_days` when set), in which no process started since and none is using CPU, with the memory each holds including helpers that detached from the terminal. The terminal's own device timestamps are the clock, the one `w` reports as idle time, so a program that prints or is typed at is never silent.

A plain `dev-cleaner` run lists them in its `memory` row and, once you confirm, hangs each one up the way closing its terminal would: SIGHUP to the programs on the terminal, SIGTERM to their detached helpers, SIGKILL after 8 seconds for whatever ignores both. Every session is re-checked right before: one that printed, ran or started anything since the scan is left alone, as is the terminal dev-cleaner itself runs in and sessions under 64 MB (a bare shell). Preview with `dev-cleaner --only procs --dry-run`. A session holds whatever it has not saved: raise the age with `idle_session_days = 3` under `[procs]`, or set it to `false` to keep every session.

**Spinning processes.** The same run has a `cpu` row for your own detached programs that have averaged 10% of a core for 6 hours and are still at it: a dev server stuck in a loop, a forgotten test binary. Only executables under your home or a temp dir qualify, never apps, system daemons, package-manager installs, VMs, or anything attached to a terminal. Each is re-checked before it gets SIGTERM (SIGKILL after 8 seconds); one that calmed down is left alone. A long computation you started detached on purpose fits the same description, so read the row before confirming, or set `spinning = false` under `[procs]`.

### What is burning my CPU?

`ps` reports a lifetime or decaying average, which hides a process that just went wild and flatters one that has been spinning for a day. `--cpu` takes two snapshots (2 seconds apart; `--cpu-sample N` for longer) and shows:

- the load average against the core count: a load several times the cores means work is queueing;
- **CPU by program**, or by whole process family with `--tree`, ranked by what each used during the sample (`100` = one full core), with its total CPU time and memory;
- the **busiest processes right now**, with their lifetime average next to the sampled rate;
- **sustained** burners: processes at least an hour old that have averaged 10% of a core or more since they started, ranked by CPU time. A server or agent you expected to be idle that has averaged a quarter of a core for 19 hours is spinning.

```bash
dev-cleaner --cpu --tree                 # which app, agent session or dev stack as a whole
dev-cleaner --cpu --filter python --full # only matching processes, full command lines
dev-cleaner --cpu --cpu-sample 10 --top 20
```

Processes that start and exit inside the sample (compiler and test-binary swarms) cannot be attributed; `--tree` still shows their surviving parents, and a load far above the busy cores in the header is their signature. When Spotlight's indexer, `fseventsd` or a container VM ranks, the report says what drives it and where to look. The report itself is read-only; a plain `dev-cleaner` run is what offers to stop the spinning ones.

### What is Docker holding?

`--docker-audit` reads the local daemon's image/container/volume/build-cache totals, running containers' memory and CPU, and allocated host VM disk size. It also lists each buildx builder once, with driver, node status, last activity when reported, and reclaimable cache for running local container builders. Stopped builders stay stopped; remote or unverified endpoints are skipped. A named Docker context is resolved before deciding whether it is local. Docker cleanup uses the same local-endpoint checks.

Container-builder caches can survive daemon-level cache pruning. Builder estimates are shown separately from daemon and VM disk totals because these views can overlap. Some cache queries can take a while; unavailable measurements display `?`.

The RAM visible to the daemon is capacity, not the VM's host memory consumption. Compare container memory and CPU with OrbStack/Docker/Colima processes in `--mem` and `--cpu`; the difference also includes guest caches, services and differences in accounting. A large difference alone does not explain slow container starts.

The audit changes nothing. Cache pruning means later rebuilds; removing a builder also removes its configuration. Restarting the runtime interrupts its workloads, and deleting volumes can remove persistent data. Preview the existing cleanup actions with `dev-cleaner --only docker --dry-run`. Space freed inside a VM may take time to return to the host filesystem.

### Faster scans, same commands

Use `dev-cleaner --mem` for memory diagnostics: it does not discover projects or size build directories. Plain `dev-cleaner` uses a 10-second discovery budget: project search gets the first half, and source-activity checks use the remainder while global cache probes run concurrently. It searches breadth first so nearby checkouts are considered before deep worktree forests. Incomplete activity checks always keep the project. A partial scan is explicitly reported; it is not an exhaustive inventory. Use `--root ~/development` to focus the search, `--thorough` for the full crawl, or `--caches-only` to skip project discovery entirely. `--audit` implies a thorough scan. The budget is checked between filesystem operations; a blocked filesystem call can overrun it. Deletion and opt-in size measurements have separate runtimes.

Normal cleanup skips recursive directory sizing and Docker disk/cache size queries. It shows targets with unmeasured sizes and reports actual freed disk after cleanup. Add `--sizes` for directory byte counts and Docker cache estimates; `--audit` also enables measurements. This avoids walking millions of cache files merely to print a number before deleting them.

Global cleaner checks run alongside project discovery, with a shared deadline in normal runs. Completed tools disappear from the status row immediately. Normal runs schedule Homebrew cleanup directly instead of performing the same expensive walk in a dry run first. Docker audits read daemon disk usage, container memory and builder inventories concurrently, and measure up to four builder caches at once. With `--sizes`, the normal Docker scan also measures builder caches concurrently. Results retain their usual order and one failed probe does not hide other groups. Opt-in directory sizing uses `--size-jobs` (default 16); each directory/batch size read has a 30-second limit and unavailable sizes stay unknown. A timed-out `du` is never restarted as a Python walk.

Project discovery shows a directory count and current path throughout the walk, even where no build directories are found. Activity checks stop as soon as a recent source establishes that a project must be kept. Normal cleanup skips sizing active projects' build directories; `--audit` measures them when you want the full disk breakdown. Stale projects still receive the full activity check before qualifying for cleanup.

Normal tool probes use only the remaining scan budget. Thorough scans use each tool’s full probe timeout. In a controlled benchmark of six simulated 100 ms builder reads, the parallel version took 0.21 seconds versus 0.62 seconds sequentially; actual speedups depend on the tools and workload.

### Cleanup that keeps moving

Cleanup starts global caches first, then independent project build directories, limited by `--clean-jobs` (default 4). Tool actions and global cache deletions within one group remain serial, including Docker prunes. Independent project directories can run concurrently even when their language has a package-manager action. Each running action has its own elapsed timer, and the display shows the observed increase in free disk space.

The scan display reserves three fixed rows for projects, tools and sizes. Cleanup reserves fixed worker rows; completed jobs do not remove rows or shift the others. Long paths are clipped to one line in the live display. The preview shows one row per cleaner, and successful cleanup results collapse into counts per cleaner. Failures and incomplete work remain visible. Use `--verbose` for every candidate path and individual cleanup result. Partial temp cleanup is counted as incomplete even when some entries were removed.

External cleanup commands have a 60-second limit. On timeout, dev-cleaner stops the client and its plugins, marks cleanup incomplete, and skips later actions in that group (a sweep that leaves a few entries behind does not hold up the other sweeps). Other groups continue. A Docker daemon may keep working after its client stops; timeout does not prove that pruning was cancelled or finished. Ctrl-C cancels running command clients too; filesystem deletions already in progress finish before exit.

Use `--command-timeout 300` (or `command_timeout = 300` in config) when you deliberately want to wait longer. This limits external cleanup commands, not scanning or filesystem deletion. To leave Docker alone for a run, use `--skip docker`.

Caches written by active builds can report “directory not empty.” Cleanup leaves busy entries in place, continues through other entries in that cache, and reports partial cleanup instead of retrying against active writers. Later cleanup in the same group is skipped; finish active builds before trying again. Incomplete/skipped items are counted separately from successful cleanup, and the command exits nonzero when anything is incomplete or failed.

## What gets cleaned

**Sharing instead of deleting.** `dev-cleaner --dedupe --root PATH` (or `paths` under `[dedupe]`) finds byte-identical files across checkouts, such as the same `node_modules` installed in thirty worktrees, and replaces each duplicate with a copy-on-write clone of one original (APFS, macOS only). Every file keeps its own path, mode and times and can still be edited independently; only the disk blocks are shared. Files under 16 KB (`min_kb`), hard links, symlinks, VCS internals and anything written in the last 10 minutes are left alone. `--dry-run` reports the duplicate bytes found; the real saving is measured from free disk afterwards, because copies that already share blocks free nothing. `du` keeps counting every copy at full size.

**Running processes.** A project whose sources look idle is still kept when a running process has its working directory, executable or a script argument inside it, so a dev server or agent left running in an old checkout keeps its `node_modules`.

Two tiers. **Default** removes things that are pure derived output: rebuilding them costs CPU, not network. **`--aggressive`** adds dependency *download* caches (re-fetched on the next build) and every Docker image not used by a container. The default run always tells you how much more `--aggressive` would free, so nothing is hidden.

The tier follows the disk unless you pin it. With `aggressive = "auto"` (the default), a run on a disk with less than 15% free includes the `--aggressive` tier, and under 5% free it also cuts the idle age to 6 hours unless you passed `--days` or `--hours`. The run says so above the plan. `--gentle`, or `aggressive = false` in the config, keeps the safe tier whatever the disk looks like.

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
| 🧰 tools | ccache, kubectl discovery cache | playwright / puppeteer / cypress browsers, electron, node-gyp, deno, trivy, grype, helm, bazel, JetBrains caches |
| 🗑️ tmp | entries of `/tmp` and `$TMPDIR` idle past the threshold; idle Claude Code session scratch (`/tmp/claude-<uid>/…`) | |
| 📁 extra | idle children of the dirs you list under `[[extra]]` | |
| 🧟 procs | stops idle orphaned processes (parent gone) that run from a temp dir or a `[procs] orphan_dirs` dir; closes terminal sessions silent for a day; stops your detached programs that have burned CPU for 6+ hours | |

**Orphaned processes.** Test binaries and tool plugins can outlive the run that started them and keep holding RAM. A process qualifies for cleanup only when it is yours, has parent PID 1, is older than the configured threshold, has `ps` CPU at most 1%, and its executable lives in a temp dir (`go test` binaries, throwaway builds) or a dir you listed under `[procs] orphan_dirs`. PID 1 alone does not prove a process is abandoned; only list plugin directories whose processes should end with their parent run. Cleanup rechecks the process's executable, owner and start time to avoid signalling a recycled PID. It sends SIGTERM first so a supervisor can tear down its children, then SIGKILL after 8 seconds if it remains. Children are not independently targeted. Its row shows RAM, which is never added to the disk total. Use `--mem` to investigate and `--only procs --dry-run` to preview candidates.

**Temp dirs.** macOS only clears `/tmp` on reboot and `$TMPDIR` after 3 days, and build tools and agents can fill both with gigabytes of throwaway Go caches and checkouts in a day. Claude session scratchpads and task-output folders are checked entry by entry: touching one scratch directory no longer protects all the old entries in that session. An entry goes only when it belongs to you, nothing in its top two levels changed within the threshold, and that check finds no sockets or pipes (a live program's IPC). `com.apple.*`, `tmux-*`, `ssh-*` and the like are always kept. Each entry is checked again right before it is removed, so one that came back into use since the scan is left alone. Set `[tmp] days` for a separate temp age threshold, or `claude_scratch = false` to keep the older whole-session behavior.

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

# Dependency download caches and unused docker images: "auto" includes them
# once under 15% of the disk is free; true always (--aggressive); false never.
aggressive = "auto"

# Groups to never run. See `dev-cleaner --list`.
skip = ["brew"]

size_jobs = 16   # parallel sizing workers
clean_jobs = 4   # parallel cleanup workers (keep modest)
command_timeout = 60  # seconds per external cleanup command

[tmp]
# Optional separate age threshold for /tmp, $TMPDIR and Claude scratch.
# Omit days to use --days / --hours or the global days setting.
days = 1
claude_scratch = true  # check individual entries inside active sessions

[docker]
# Prune unused volumes too. Off by default: volumes hold data.
# true = anonymous volumes only, "all" = named ones as well.
volumes = false

[procs]
# Orphaned processes are stopped when their executable lives in a temp dir.
# Add dirs whose executables only ever run under a parent (a plugin dir).
orphan_dirs = ["~/.mytool/plugins"]
# Close terminal sessions (tmux panes, tabs) silent for this many days; false keeps them.
idle_session_days = 1
# Stop your detached programs that have burned CPU for 6+ hours and still do.
spinning = true

# Disposable dirs of your own tools: children idle longer than `days` are
# removed (`days` defaults to the global one). Repeat the block per dir.
[[extra]]
path = "~/Library/Caches/mytool"
days = 3
include = ["*"]
exclude = ["keep-*"]

# Versioned output (releases, snapshots, per-version caches): keep the newest
# few, remove every older one whatever its age.
[[extra]]
path = "~/Library/Caches/mytool/releases"
days = 0
keep = 3
```

Each `[[extra]]` rule removes only matching **direct children** of `path`; the folder itself stays. `include` defaults to `["*"]`, `exclude` defaults to `[]`, and exclusions win. Patterns match names using `*`, `?` and `[abc]`; paths such as `logs/*.log` are rejected. Use a separate rule with `path = "~/.codefly/logs"` for that folder's contents. An empty `include = []` disables a rule. `days = 0` ignores age while keeping ownership, symlink, mount-point and IPC checks. Explicit per-rule ages and `[tmp] days` take precedence over the global / CLI age.

`keep = N` always spares the N most recently modified matching children, so a rule can mean "the latest versions stay": with `days = 0` everything older than the newest N goes; with an age, an entry must be both outside the newest N and idle past it.

An entry is also kept, whatever its age, while one of your running processes has its working directory inside it. An idle database or dev server writes nothing for days, so mtimes alone would hand its data dir to the sweep; this applies to temp dirs as well.

For example, to clean selected old Codefly log files while keeping a current log and all other Codefly data:

```toml
[[extra]]
path = "~/.codefly/logs"
days = 3
include = ["*.log", "*.log.*"]
exclude = ["current.log"]
```

This rule does not select `.codefly/agents`, `modules`, `data`, `object-storage`, or keys. Preview your rules with `dev-cleaner --only extra --dry-run --sizes`; `--show-config` lists each rule's path, age and patterns. The existing project and cache cleaners retain their own rules.

`roots = ["~"]` searches broadly within the normal time budget. Narrow roots prioritize a particular workspace; `--thorough` searches all configured roots without a budget. The walk skips `~/Library`, package-manager homes and mount points.

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
| `--thorough` | off | Remove the normal 10-second discovery budget. Implied by `--audit`. |
| `--caches-only` | off | Skip project discovery; keep global caches and configured temp cleanup. |
| `-v`, `--verbose` | off | Show every candidate path and individual cleanup result. |
| `--days N` | `1.0` | Project idle age in days. |
| `--hours N` | | Project idle age in hours (mutually exclusive with `--days`). |
| `-n`, `--dry-run` | off | List candidates, don't clean. |
| `-y`, `--yes` | off | Clean without asking. No review screen. |
| `--plain` | off | Print the plan and ask yes/no instead of opening the review screen. |
| `--json` | off | Plan and results as one JSON object on stdout; never asks. Cleans only with `--yes`. |
| `-a`, `--aggressive` | auto | Also clean dependency download caches and all unused docker images. On by itself once under 15% of the disk is free. |
| `--dedupe` | off | Share identical files across `--root` dirs via copy-on-write clones. Removes nothing. |
| `--gentle` | off | Keep dependency caches and the configured idle age even when the disk is tight. |
| `--only GROUPS` | all | Comma-separated groups to run. |
| `--skip GROUPS` | | Comma-separated groups to skip (merged with config). |
| `--audit` | | Show the biggest dirs and how much of each would be freed; change nothing. |
| `--docker-audit` | | Read-only Docker disk, container memory and per-builder cache inventory. |
| `--mem` | | Read-only host memory, programs, individual PIDs, silent terminal sessions and orphan candidates. |
| `--cpu` | | Read-only sampled CPU by program, busiest processes and sustained burners. |
| `--cpu-sample SECONDS` | `2` | Sampling window; requires `--cpu`. |
| `--top N` | `10` | Rows per table; with `--mem` or `--cpu`. |
| `--filter TEXT` | | Case-insensitive match on PID, executable or command. With `--tree`, selects whole matching families. |
| `--tree` | off | Group by process family; with `--mem` or `--cpu`. |
| `--full` | off | Wrap full paths and commands instead of shortening them; with `--mem` or `--cpu`. |
| `--mem-sort {memory,cpu,growth}` | `memory` | Sort both memory tables. Growth requires `--mem-watch`. |
| `--mem-watch SECONDS` | | Refresh memory, showing deltas since the previous snapshot; requires `--mem`. Ctrl-C stops. |
| `--list` | | Show the groups table and exit. |
| `--size-jobs N` | `16` | Parallel sizing workers (`du -sk`). |
| `--sizes` | off | Measure directory sizes and Docker disk/cache estimates; slower. Implied by `--audit`. |
| `--clean-jobs N` | `4` | Parallel cleanup workers. |
| `--command-timeout SECONDS` | `60` | Time limit per external cleanup command; incomplete groups are not retried. |
| `--config PATH` | `~/.config/dev-cleaner/config.toml` | Alternate config file. |
| `--init` | | Interactive wizard (or `--init --yes` for static defaults). |
| `--show-config` | | Print resolved config and exit. |
| `--completion {bash,zsh,fish}` | | Print a shell completion script and exit. |

## How it works

1. Searches each root breadth first with `os.scandir`, skipping irrelevant dirs and never descending into matched build dirs or venvs. Normal discovery is time-budgeted; `--thorough` removes the limit. Empty build directories are ignored.
2. For every dir holding a project marker (`Cargo.toml`, `package.json`, `go.mod`, `*.csproj`, …) records its sibling build dirs per the rules table.
3. Measures each top-level project's last source edit in parallel; nested projects inherit it.
4. Probes global caches in parallel: asks `go env`, `docker buildx ls`, `uv cache dir`, `pnpm store path`, checks known cache paths, and lists idle temp and `[[extra]]` entries. Docker disk/cache size queries and Homebrew preview walks are opt-in with `--sizes` or `--audit`.
5. Dedupes and shows a compact table per cleaner (`--verbose` lists every path) plus notes about `--aggressive` and active projects. Directory sizes are measured with parallel `du -sk` only when `--sizes` or `--audit` is requested; normal cleanup does not wait for size walks.
6. On confirm: independent cleanup groups and filesystem deletes run concurrently; actions sharing a tool group stay serial. External commands have a configurable 60-second time limit. Busy caches and timed-out commands are reported as incomplete, and later actions in that group are skipped. The summary reports actual freed disk and counts successful, incomplete/skipped, and failed items separately.

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
