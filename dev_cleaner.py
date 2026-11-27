#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13.7"]
# ///
"""dev-cleaner: find and remove the build artifacts and caches a dev machine accumulates.

Covers per-project build output (Rust target/, node_modules, .venv, Gradle build/, ...)
that hasn't been touched in N days, global caches (Go build cache, Docker,
Homebrew, Xcode DerivedData, cargo/npm/uv/pip/gradle caches, ...) and idle temp files.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, get_ident, local
from typing import Callable

from rich.box import SIMPLE_HEAD
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table
from rich.text import Text

console = Console()
MAX_WIDTH = 110                    # wider terminals only stretch tables into unreadable rows
TABLE = dict(box=SIMPLE_HEAD, header_style="dim", title_style="bold", border_style="dim")

HOME = Path.home()
IS_MAC = sys.platform == "darwin"
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
MAC_CACHES = HOME / "Library" / "Caches"

SAFE = "safe"
AGGRESSIVE = "aggressive"
PROBE_JOBS = 4                      # cap concurrent tool probes separately from disk sizing
SCAN_SECONDS = 10                  # normal discovery budget; --thorough opts out
_scan_thread = local()


def scan_expired() -> bool:
    deadline = getattr(_scan_thread, "deadline", None)
    return deadline is not None and time.monotonic() >= deadline

# ---------------------------------------------------------------------------
# Scanning rules
# ---------------------------------------------------------------------------

# Never descend into these *directly under the home dir* while looking for
# projects. Dot-dirs are not blanket-skipped: some (e.g. ~/.pilot) legitimately
# contain projects. Deeper down the same names are ordinary project content: a
# repo's .claude/worktrees or .cache/ can hold hundreds of checkouts, and go/ is
# a common package dir.
SKIP_DIRS = {
    # macOS
    ".Trash", "Library", "Pictures", "Music", "Movies",
    # Linux
    "snap", ".var",
    # Global caches / package managers (handled by the global cleaners)
    ".cargo", ".rustup", ".cache", ".npm", ".yarn", ".pnpm", ".bun", ".gem",
    ".gradle", ".m2", ".ivy2", ".bundle", ".cocoapods", ".nuget", ".pub-cache",
    ".composer", "go",
    # Editors / IDEs / agents
    ".vscode", ".cursor", ".idea", ".zed", ".vim", ".emacs.d", ".claude", ".codex",
    # Language version managers
    ".pyenv", ".rbenv", ".nvm", ".gvm", ".sdkman", ".asdf", ".volta", ".mise",
    # Cloud / infra tooling
    ".docker", ".kube", ".aws", ".gcloud", ".azure", ".orbstack", ".terraform.d",
    # XDG config / privacy
    ".config", ".local", ".ssh", ".gnupg",
}

# Dirs that are never a project container, whatever their parent is. These are
# pruned from every walk (both project discovery and activity measurement).
# Notably venvs: site-packages is full of pyproject.toml/package.json files.
ALWAYS_PRUNE = {
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", ".tox", ".nox", "site-packages", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".stack-work", "dist-newstyle",
    ".dart_tool", ".zig-cache", "zig-cache", ".terraform", ".gradle", "_build",
    ".build", ".next", ".nuxt", ".turbo", ".parcel-cache", ".svelte-kit", ".angular",
    ".direnv",
}


@dataclass(frozen=True)
class BuildDir:
    name: str
    tier: str = SAFE
    require: str | None = None  # file that must exist inside, e.g. CMakeCache.txt
    note: str = ""


@dataclass(frozen=True)
class Rule:
    group: str
    markers: tuple[str, ...]
    dirs: tuple[BuildDir, ...]
    suffixes: tuple[str, ...] = ()  # marker by filename suffix (e.g. ".csproj")

    def matches(self, filenames: set[str]) -> bool:
        if any(m in filenames for m in self.markers):
            return True
        if self.suffixes:
            return any(f.endswith(self.suffixes) for f in filenames)
        return False


RULES: tuple[Rule, ...] = (
    Rule("rust", ("Cargo.toml",), (BuildDir("target"),)),
    Rule("node", ("package.json",), (
        BuildDir("node_modules", note="re-installed by npm/pnpm/yarn/bun"),
        BuildDir(".next"), BuildDir(".nuxt"), BuildDir(".turbo"),
        BuildDir(".parcel-cache"), BuildDir(".svelte-kit"), BuildDir(".angular"),
    )),
    Rule("python", ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile"), (
        BuildDir(".venv", note="re-created by uv/poetry/pip"), BuildDir("venv"),
        BuildDir(".tox"), BuildDir(".nox"), BuildDir(".mypy_cache"),
        BuildDir(".pytest_cache"), BuildDir(".ruff_cache"),
    )),
    Rule("jvm", ("build.gradle", "build.gradle.kts", "pom.xml", "settings.gradle",
                 "settings.gradle.kts"), (
        BuildDir("build"), BuildDir("target"), BuildDir(".gradle"),
    )),
    Rule("swift", ("Package.swift",), (BuildDir(".build"),)),
    Rule("elixir", ("mix.exs",), (
        BuildDir("_build"), BuildDir("deps", AGGRESSIVE, note="re-fetched by mix"),
    )),
    Rule("zig", ("build.zig",), (
        BuildDir("zig-cache"), BuildDir(".zig-cache"), BuildDir("zig-out"),
    )),
    Rule("ocaml", ("dune-project",), (BuildDir("_build"),)),
    Rule("haskell", ("stack.yaml", "cabal.project"), (
        BuildDir(".stack-work"), BuildDir("dist-newstyle"),
    )),
    Rule("dart", ("pubspec.yaml",), (BuildDir("build"), BuildDir(".dart_tool"))),
    Rule("cmake", ("CMakeLists.txt",), (
        BuildDir("build", require="CMakeCache.txt"),
        BuildDir("cmake-build-debug"), BuildDir("cmake-build-release"),
    )),
    Rule("dotnet", (), (BuildDir("bin"), BuildDir("obj")), suffixes=(".csproj", ".fsproj", ".sln")),
    Rule("terraform", (), (
        BuildDir(".terraform", AGGRESSIVE, note="providers re-downloaded by terraform init"),
    ), suffixes=(".tf",)),
    Rule("php", ("composer.json",), (
        BuildDir("vendor", AGGRESSIVE, require="autoload.php", note="re-installed by composer"),
    )),
)

ALL_BUILD_DIR_NAMES = {bd.name for r in RULES for bd in r.dirs}
PROJECT_GROUPS = tuple(dict.fromkeys(r.group for r in RULES))
GLOBAL_GROUPS = ("go", "docker", "brew", "xcode", "tools", "tmp", "extra", "procs")
ALL_GROUPS = tuple(dict.fromkeys(PROJECT_GROUPS + GLOBAL_GROUPS))

# Global cache dirs that belong to no project. (label, group, tier, note, candidate paths)
KNOWN_CACHES: tuple[tuple[str, str, str, str, tuple[Path, ...]], ...] = (
    ("sccache", "rust", SAFE, "compiler cache; rebuilt on demand",
     (MAC_CACHES / "Mozilla.sccache", XDG_CACHE / "sccache")),
    ("ccache", "tools", SAFE, "compiler cache; rebuilt on demand",
     (HOME / ".ccache", XDG_CACHE / "ccache", MAC_CACHES / "ccache")),
    ("zig global cache", "zig", SAFE, "rebuilt on demand", (XDG_CACHE / "zig",)),
    ("goimports cache", "go", SAFE, "rebuilt on demand", (MAC_CACHES / "goimports", XDG_CACHE / "goimports")),
    ("golangci-lint cache", "go", SAFE, "rebuilt on demand",
     (MAC_CACHES / "golangci-lint", XDG_CACHE / "golangci-lint")),
    ("gopls cache", "go", SAFE, "rebuilt on demand", (MAC_CACHES / "gopls", XDG_CACHE / "gopls")),
    ("staticcheck cache", "go", SAFE, "rebuilt on demand", (MAC_CACHES / "staticcheck", XDG_CACHE / "staticcheck")),
    ("npx cache", "node", SAFE, "throwaway npx installs", (HOME / ".npm" / "_npx",)),
    ("TypeScript ATA cache", "node", SAFE, "auto-acquired types", (XDG_CACHE / "typescript",)),
    ("kubectl discovery cache", "tools", SAFE, "", (HOME / ".kube" / "cache",)),
    ("Gradle daemon logs", "jvm", SAFE, "", (HOME / ".gradle" / "daemon",)),
    ("npm cache", "node", AGGRESSIVE, "tarballs; re-downloaded", (HOME / ".npm" / "_cacache",)),
    ("yarn cache", "node", AGGRESSIVE, "re-downloaded",
     (MAC_CACHES / "Yarn", XDG_CACHE / "yarn", HOME / ".yarn" / "berry" / "cache")),
    ("bun install cache", "node", AGGRESSIVE, "re-downloaded", (HOME / ".bun" / "install" / "cache",)),
    ("pip cache", "python", AGGRESSIVE, "wheels; re-downloaded", (MAC_CACHES / "pip", XDG_CACHE / "pip")),
    ("poetry cache", "python", AGGRESSIVE, "re-downloaded", (MAC_CACHES / "pypoetry", XDG_CACHE / "pypoetry")),
    ("pre-commit envs", "python", AGGRESSIVE, "re-created on next run", (XDG_CACHE / "pre-commit",)),
    ("Gradle caches", "jvm", AGGRESSIVE, "dependency jars; re-downloaded", (HOME / ".gradle" / "caches",)),
    ("Gradle wrapper dists", "jvm", AGGRESSIVE, "re-downloaded", (HOME / ".gradle" / "wrapper" / "dists",)),
    ("Maven repository", "jvm", AGGRESSIVE, "re-downloaded", (HOME / ".m2" / "repository",)),
    ("Ivy cache", "jvm", AGGRESSIVE, "re-downloaded", (HOME / ".ivy2" / "cache",)),
    ("Coursier cache", "jvm", AGGRESSIVE, "re-downloaded",
     (MAC_CACHES / "Coursier", XDG_CACHE / "coursier")),
    ("SwiftPM cache", "swift", AGGRESSIVE, "re-fetched", (MAC_CACHES / "org.swift.swiftpm",)),
    ("Playwright browsers", "tools", AGGRESSIVE, "re-downloaded by `playwright install`",
     (MAC_CACHES / "ms-playwright", XDG_CACHE / "ms-playwright")),
    ("Puppeteer browsers", "tools", AGGRESSIVE, "re-downloaded", (XDG_CACHE / "puppeteer",)),
    ("Cypress binaries", "tools", AGGRESSIVE, "re-downloaded", (MAC_CACHES / "Cypress", XDG_CACHE / "Cypress")),
    ("Electron downloads", "tools", AGGRESSIVE, "re-downloaded", (MAC_CACHES / "electron", XDG_CACHE / "electron")),
    ("node-gyp headers", "tools", AGGRESSIVE, "re-downloaded",
     (MAC_CACHES / "node-gyp", XDG_CACHE / "node-gyp", HOME / ".node-gyp")),
    ("Deno cache", "tools", AGGRESSIVE, "re-downloaded", (MAC_CACHES / "deno", XDG_CACHE / "deno")),
    ("Composer cache", "php", AGGRESSIVE, "re-downloaded", (HOME / ".composer" / "cache", XDG_CACHE / "composer")),
    ("NuGet packages", "dotnet", AGGRESSIVE, "re-downloaded", (HOME / ".nuget" / "packages",)),
    ("Dart pub cache", "dart", AGGRESSIVE, "re-downloaded", (HOME / ".pub-cache",)),
    ("Terraform plugin cache", "terraform", AGGRESSIVE, "re-downloaded",
     (HOME / ".terraform.d" / "plugin-cache",)),
    ("Trivy cache", "tools", AGGRESSIVE, "vulnerability DB; re-downloaded",
     (MAC_CACHES / "trivy", XDG_CACHE / "trivy")),
    ("Grype cache", "tools", AGGRESSIVE, "vulnerability DB; re-downloaded",
     (MAC_CACHES / "grype", XDG_CACHE / "grype")),
    ("Helm cache", "tools", AGGRESSIVE, "re-downloaded", (MAC_CACHES / "helm", XDG_CACHE / "helm")),
    ("Bazel output bases", "tools", AGGRESSIVE, "run `bazel shutdown` first", (XDG_CACHE / "bazel",)),
    ("JetBrains caches", "tools", AGGRESSIVE, "IDE re-indexes projects",
     (MAC_CACHES / "JetBrains", XDG_CACHE / "JetBrains")),
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "roots": ["~"],
    "ignore": [],
    "days": 1.0,
    "aggressive": "auto",          # "auto" follows free disk space; true / false pin it
    "skip": [],
    "size_jobs": 16,
    "clean_jobs": 4,
    "command_timeout": 60,
    "scan_seconds": SCAN_SECONDS,
    "dedupe": {"paths": [], "min_kb": 16},
    "docker": {"volumes": False},
    "tmp": {"claude_scratch": True},
    "extra": [],
    "procs": {"orphan_dirs": []},
}


TIGHT_FREE = 0.15                  # under this share of the disk free: dependency caches go too
CRITICAL_FREE = 0.05               # under this: the idle age is cut as well
CRITICAL_HOURS = 6.0


def disk_pressure(path: Path | None = None) -> tuple[str, int, int]:
    """("ok" | "tight" | "critical", free bytes, total bytes) for the disk holding the home dir."""
    try:
        u = shutil.disk_usage(path or HOME)
    except OSError:
        return "ok", 0, 0
    share = u.free / u.total if u.total else 1.0
    return ("critical" if share < CRITICAL_FREE else "tight" if share < TIGHT_FREE else "ok"), u.free, u.total


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "dev-cleaner" / "config.toml"


def legacy_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "rust-clean" / "config.toml"


def render_config(roots: list[str], days: float, ignore: list[str] | None = None,
                  aggressive: bool | str = "auto", skip: list[str] | None = None,
                  docker_volumes: bool | str = False) -> str:
    """Serialize a config as TOML with comments."""
    ignore = ignore or []
    skip = skip or []
    return f"""\
# dev-cleaner config — https://github.com/AntoineToussaint/dev-clean
# CLI flags override anything set here.

# Directories to scan for projects (supports ~ expansion).
roots = {roots!r}

# Extra directory names to skip while scanning (added to the built-in list).
ignore = {ignore!r}

# A project's build dirs are stale once nothing in the project was edited for
# this many days.
days = {days}

# Dependency download caches (cargo registry, go modcache, npm/pip, gradle,
# .terraform, ...) and all unused docker images. "auto" includes them once less
# than 15% of the disk is free, and under 5% also cuts the idle age to 6 hours.
# true always includes them (--aggressive); false never does (--gentle).
aggressive = {'"auto"' if aggressive == "auto" else str(bool(aggressive)).lower()}

# Cleaner groups to never run. See `dev-cleaner --list`.
skip = {skip!r}

# Parallel workers when sizing dirs (I/O bound).
size_jobs = 16

# Parallel cleanup workers (disk bound; keep modest).
clean_jobs = 4

# Maximum seconds per external cleanup command; slow commands are left incomplete.
command_timeout = 60

[tmp]
# Omit days to inherit the global / CLI idle threshold. Use 0 to ignore age.
# days = 1
# Clean old scratchpad entries independently inside active Claude sessions.
claude_scratch = true

[docker]
# Prune unused volumes too. Off by default: volumes hold data.
# true = anonymous volumes only, "all" = named ones as well.
volumes = {json.dumps(docker_volumes)}

[procs]
# Orphaned processes (parent gone, idle past `days`) are stopped when their
# executable lives in a temp dir. List more dirs whose executables are only
# ever meant to run under a parent, e.g. a tool's plugin dir.
orphan_dirs = []
# Close terminal sessions (tmux panes, tabs: agents and shells left open) whose
# terminal saw no input or output for this many days and where nothing ran or
# started since.
# A session holds its unsaved state: raise the age, or set false to keep them all.
idle_session_days = 1
# Stop your own detached programs (dev servers, test binaries, scripts under your
# home or temp dirs) that have averaged 10% of a core for 6 hours and still are.
spinning = true

# Disposable dirs of your own: children idle longer than `days` are removed
# (`days` is optional and defaults to the global one above).
# [[extra]]
# path = "~/Library/Caches/mytool"
# days = 3
# include = ["*"]  # direct child names; shell-style wildcards
# exclude = ["keep-*"]  # exclusions always win
# keep = 2  # the newest 2 matching children always stay (with days = 0: keep only the latest 2)
"""


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        console.print(f"[yellow]warning:[/] could not read {path}: {e}")
        return {}


def write_default_config(path: Path, force: bool = False) -> bool:
    if path.exists() and not force:
        if not Confirm.ask(f"[yellow]{path} already exists. overwrite?[/]", default=False):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(DEFAULTS["roots"], DEFAULTS["days"]))
    return True


# ---------------------------------------------------------------------------
# Shell completions
# ---------------------------------------------------------------------------

_GROUPS_WS = " ".join(ALL_GROUPS)

COMPLETION_BASH = rf"""
_dev_cleaner() {{
    local cur prev opts
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    opts="--root --thorough --caches-only --below-free-gib --verbose -v --days --hours --dry-run --yes --plain --json --aggressive --gentle --only --skip --audit --docker-audit --mem --cpu --cpu-sample --top --filter --tree --full --mem-sort --mem-watch --list --dedupe --sizes --size-jobs --clean-jobs --command-timeout --config --init --show-config --completion -n -y -a -h --help"
    case "$prev" in
        --root|--config) COMPREPLY=( $(compgen -f -- "$cur") ); return ;;
        --completion) COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") ); return ;;
        --mem-sort) COMPREPLY=( $(compgen -W "memory cpu growth" -- "$cur") ); return ;;
        --only|--skip) COMPREPLY=( $(compgen -W "{_GROUPS_WS}" -- "$cur") ); return ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    fi
}}
complete -F _dev_cleaner dev-cleaner
""".lstrip()

COMPLETION_ZSH = rf"""
#compdef dev-cleaner
_dev_cleaner() {{
    _arguments -s \
        '*--root[Where to search]:path:_files -/' \
        '--days[Min idle age in days]:days:' \
        '--hours[Min idle age in hours]:hours:' \
        '(-n --dry-run)'{{-n,--dry-run}}'[List candidates, do not clean]' \
        '(-y --yes)'{{-y,--yes}}'[Skip confirmation prompt]' \
        '(-a --aggressive)'{{-a,--aggressive}}'[Also clean dependency caches / unused images]' \
        '--gentle[Keep dependency caches even when the disk is tight]' \
        '--plain[Print the plan and ask yes/no instead of the review screen]' \
        '--json[Print the plan and results as JSON; never asks]' \
        '--only[Only run these groups]:groups:_values -s , group {_GROUPS_WS}' \
        '--skip[Skip these groups]:groups:_values -s , group {_GROUPS_WS}' \
        '--audit[Show the biggest dirs and how much of each would be freed]' \
        '--docker-audit[Read-only Docker disk, container memory and builder cache inventory]' \
        '--mem[Show which programs and silent terminal sessions hold the memory]' \
        '--cpu[Sample which programs burn CPU now and which have for hours]' \
        '--cpu-sample[CPU sampling window]:seconds:' \
        '--top[Rows per --mem / --cpu table]:N:' \
        '--filter[Match PID, executable or command]:text:' \
        '--tree[Group by process family]' \
        '--full[Show full paths and commands]' \
        '--mem-sort[Sort memory tables]:sort:(memory cpu growth)' \
        '--mem-watch[Refresh memory and show growth]:seconds:' \
        '--list[List cleaner groups and exit]' \
        '--thorough[Search all projects without a scan budget]' \
        '--below-free-gib[Only scan when home disk free space is below threshold]:GiB:' \
        '--caches-only[Skip project discovery]' \
        '(-v --verbose)'{{-v,--verbose}}'[Show every path and result]' \
        '--sizes[Measure directory and Docker cache sizes (slower)]' \
        '--size-jobs[Parallel sizing workers]:N:' \
        '--clean-jobs[Parallel cleanup workers]:N:' \
        '--command-timeout[Cleanup command time limit]:seconds:' \
        '--config[Config path]:file:_files' \
        '--init[Interactive setup / write a starter config]' \
        '--show-config[Print resolved config and exit]' \
        '--completion[Print completion script]:shell:(bash zsh fish)' \
        '(-h --help)'{{-h,--help}}'[Show help]'
}}
_dev_cleaner "$@"
""".lstrip()

COMPLETION_FISH = rf"""
complete -c dev-cleaner -l root -d "Where to search" -r -F
complete -c dev-cleaner -l days -d "Min idle age in days" -x
complete -c dev-cleaner -l hours -d "Min idle age in hours" -x
complete -c dev-cleaner -s n -l dry-run -d "List candidates, do not clean"
complete -c dev-cleaner -s y -l yes -d "Skip confirmation prompt"
complete -c dev-cleaner -s a -l aggressive -d "Also clean dependency caches / unused images"
complete -c dev-cleaner -l gentle -d "Keep dependency caches even when the disk is tight"
complete -c dev-cleaner -l plain -d "Print the plan and ask yes/no instead of the review screen"
complete -c dev-cleaner -l json -d "Print the plan and results as JSON; never asks"
complete -c dev-cleaner -l only -d "Only run these groups" -x -a "{_GROUPS_WS}"
complete -c dev-cleaner -l skip -d "Skip these groups" -x -a "{_GROUPS_WS}"
complete -c dev-cleaner -l audit -d "Show the biggest dirs and how much of each would be freed"
complete -c dev-cleaner -l docker-audit -d "Read-only Docker disk, container memory and builder cache inventory"
complete -c dev-cleaner -l mem -d "Show which programs and silent terminal sessions hold the memory"
complete -c dev-cleaner -l cpu -d "Sample which programs burn CPU now and which have for hours"
complete -c dev-cleaner -l cpu-sample -d "CPU sampling window in seconds" -x
complete -c dev-cleaner -l top -d "Rows per --mem / --cpu table" -x
complete -c dev-cleaner -l filter -d "Match PID, executable or command" -x
complete -c dev-cleaner -l tree -d "Group by process family"
complete -c dev-cleaner -l full -d "Show full paths and commands"
complete -c dev-cleaner -l mem-sort -d "Sort memory tables" -x -a "memory cpu growth"
complete -c dev-cleaner -l mem-watch -d "Refresh memory and show growth" -x
complete -c dev-cleaner -l list -d "List cleaner groups and exit"
complete -c dev-cleaner -l thorough -d "Search all projects without a scan budget"
complete -c dev-cleaner -l below-free-gib -d "Only scan below this much free disk space in GiB" -x
complete -c dev-cleaner -l caches-only -d "Skip project discovery"
complete -c dev-cleaner -s v -l verbose -d "Show every path and result"
complete -c dev-cleaner -l sizes -d "Measure directory and Docker cache sizes (slower)"
complete -c dev-cleaner -l dedupe -d "Share identical files across checkouts (APFS clones)"
complete -c dev-cleaner -l size-jobs -d "Parallel sizing workers" -x
complete -c dev-cleaner -l clean-jobs -d "Parallel cleanup workers" -x
complete -c dev-cleaner -l command-timeout -d "Cleanup command time limit in seconds" -x
complete -c dev-cleaner -l config -d "Config path" -r -F
complete -c dev-cleaner -l init -d "Interactive setup / write a starter config"
complete -c dev-cleaner -l show-config -d "Print resolved config and exit"
complete -c dev-cleaner -l completion -d "Print completion script" -x -a "bash zsh fish"
complete -c dev-cleaner -s h -l help -d "Show help"
""".lstrip()

COMPLETIONS = {"bash": COMPLETION_BASH, "zsh": COMPLETION_ZSH, "fish": COMPLETION_FISH}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} PB"


def tilde(p: Path | str) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def age_color(days: float) -> str:
    if days < 7:
        return "yellow"
    if days < 30:
        return "orange3"
    return "red"


def size_color(n: int | None) -> str:
    if n is None:
        return "dim"
    gb = n / (1024**3)
    if gb < 1:
        return "green"
    if gb < 5:
        return "yellow"
    return "red bold"


_SIZE_RE = re.compile(r"([\d.]+)\s*([kKMGTP]?i?B)\b")


def parse_size(s: str, base: int = 1000) -> int | None:
    """Parse '1.2GB', '660.4MB (100%)', '0B' → bytes. Docker uses SI (1000)."""
    m = _SIZE_RE.search(s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower().rstrip("b").rstrip("i")
    exp = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}[unit]
    return int(num * (base ** exp))


def run(cmd: list[str], timeout: float = 60, **kw) -> subprocess.CompletedProcess | None:
    """Run a command, capturing output. None if missing/timeout/crashed."""
    deadline = getattr(_scan_thread, "deadline", None)
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def dir_size(path: Path) -> int | None:
    """Disk usage in bytes. Uses `du -sk` (fast, counts blocks) when available."""
    if shutil.which("du"):
        r = run(["du", "-sk", str(path)], timeout=30)
        if r is not None and r.stdout.strip():
            try:
                return int(r.stdout.split()[0]) * 1024
            except ValueError:
                pass
        return None  # Do not restart a timed-out du as an unbounded Python walk.
    total = 0
    deadline = time.monotonic() + 30
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        if time.monotonic() >= deadline:
            return None
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
    return total


def same_fs(path: Path | str, dev: int) -> bool:
    """False across a mount point. ~/OrbStack, for one, is the docker VM's live filesystem."""
    try:
        return os.lstat(path).st_dev == dev
    except OSError:
        return False


def paths_size(paths: list[Path], workers: int) -> dict[Path, int]:
    """Disk usage of many paths at once: a few batched `du` calls instead of one each."""
    if not shutil.which("du"):
        return {p: n for p in paths if (n := dir_size(p)) is not None}

    def batch(chunk: list[Path]) -> dict[Path, int]:
        out: dict[Path, int] = {}
        r = run(["du", "-sk", *map(str, chunk)], timeout=30)
        for line in (r.stdout.splitlines() if r else ()):
            kb, _, name = line.partition("\t")
            if kb.isdigit():
                out[Path(name)] = int(kb) * 1024
        return out

    sizes: dict[Path, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(batch, [paths[i:i + 200] for i in range(0, len(paths), 200)]):
            sizes.update(part)
    return sizes


def file_disk_size(path: Path) -> int:
    """Blocks actually allocated: VM disk images are sparse, st_size lies."""
    try:
        return os.stat(path).st_blocks * 512
    except OSError:
        return 0


def _force_writable(path: Path) -> None:
    try:
        os.chmod(path, os.lstat(path).st_mode | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass
    # POSIX deletion needs write/search permission on directories, not files.
    # A scratch tree can hold millions of files: do not chmod every cache file.
    for dirpath, dirnames, _filenames in os.walk(path, followlinks=False):
        for name in dirnames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                if not stat.S_ISLNK(st.st_mode):
                    os.chmod(p, st.st_mode | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(st.st_mode) else 0))
            except OSError:
                pass


def force_rmtree(path: Path) -> None:
    """rmtree that copes with read-only trees (go modcache, nix-style stores)."""
    if path.is_symlink():
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        _force_writable(path)
        shutil.rmtree(path)


class CleanupDeferred(RuntimeError):
    """Partial or unverified cleanup: do not retry a busy backend during this run."""


class CleanupPartial(CleanupDeferred):
    """Some entries of one sweep stayed behind. Other sweeps are independent and still run."""


_cleanup_thread = local()


def clear_dir(path: Path) -> str | None:
    """Remove a directory's contents but keep the directory itself."""
    busy = 0
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                force_rmtree(child)
            else:
                child.unlink()
        except FileNotFoundError:  # another cleaner already removed it
            continue
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EBUSY):
                raise
            busy += 1
    if busy:
        raise CleanupDeferred(f"partially cleared; {busy} busy cache entries left in place (files changed during cleanup)")
    return None


def assert_deletable(path: Path, roots: list[Path]) -> None:
    """Refuse to delete anything that looks like a mistake."""
    rp = path.resolve()
    if rp == Path("/") or rp == HOME or len(rp.parts) < 3:
        raise RuntimeError(f"refusing to delete {rp}")
    if rp in {r.resolve() for r in roots}:
        raise RuntimeError(f"refusing to delete scan root {rp}")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


@dataclass
class Item:
    group: str
    label: str                       # "target/", "go build cache", ...
    path: Path | None = None         # what is shown; what is deleted when action is None
    size: int | None = None          # bytes; None = unknown (sized later if path set)
    tier: str = SAFE
    mtime: float | None = None       # project last-activity; None for global caches
    note: str = ""
    mode: str = "rmtree"             # "rmtree" | "clear" | "action"
    action: Callable[[], str | None] | None = None  # returns a one-line result
    shadowed: bool = False           # safe item made redundant by an aggressive one
    project: Path | None = None
    ram: bool = False                # size is memory, not disk: shown, never summed
    cores: float | None = None       # CPU the item gives back, in cores; set on process items that burn it

    @property
    def is_project(self) -> bool:
        return self.project is not None

    def key(self) -> str:
        return f"{self.group}:{self.path}:{self.label}"


@dataclass
class Context:
    roots: list[Path]
    cutoff: float
    extra_skip: set[str]
    groups: set[str]
    aggressive: bool
    docker_volumes: bool | str       # False | True (anonymous only) | "all"
    size_jobs: int
    sweeps: list[Sweep] = field(default_factory=list)  # user-declared, from [[extra]]
    orphan_dirs: list[Path] = field(default_factory=list)  # from [procs], on top of the temp dirs
    idle_session_days: float | None = 1.0  # from [procs]; None leaves terminal sessions alone
    stop_spinning: bool = True       # from [procs]: stop detached processes that have burned CPU for hours
    warnings: list[str] = field(default_factory=list)
    measure_active: bool = False     # deep --audit includes sizes of build dirs excluded by activity
    command_timeout: float = 60
    measure_sizes: bool = False      # recursive size reads are opt-in, never needed to clean
    tmp_cutoff: float | None = None
    tmp_claude_scratch: bool = True
    scan_seconds: float | None = SCAN_SECONDS
    caches_only: bool = False


# ---------------------------------------------------------------------------
# Project scanner
# ---------------------------------------------------------------------------


def find_projects(root: Path, extra_skip: set[str] | None = None, *,
                  visit: Callable[[Path], None] | None = None,
                  deadline: float | None = None):
    """Yield (group, project_dir, build_dir, BuildDir) for every build dir under root."""
    root = root.expanduser().resolve()
    skip = ALWAYS_PRUNE | (extra_skip or set())
    try:
        home = HOME.resolve()
    except OSError:
        home = HOME
    try:
        dev = os.stat(root).st_dev
    except OSError:
        return
    # Breadth first: nearby checkouts get a turn before a deep worktree forest.
    pending = deque([root])
    while pending:
        if deadline is not None and time.monotonic() >= deadline:
            return
        here = pending.popleft()
        try:
            with os.scandir(here) as entries:
                dirs, filenames = {}, set()
                for entry in entries:
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    if entry.is_dir(follow_symlinks=False):
                        dirs[entry.name] = entry.path
                    else:
                        filenames.add(entry.name)
        except OSError:
            continue
        dirnames = dirs.keys()
        if visit is not None:
            visit(here)
        matched: set[str] = set()
        if not ALL_BUILD_DIR_NAMES.isdisjoint(dirnames):
            fset = set(filenames)
            for rule in RULES:
                if not rule.matches(fset):
                    continue
                for bd in rule.dirs:
                    if bd.name not in dirnames or bd.name in matched:
                        continue
                    p = here / bd.name
                    if p.is_symlink() or not p.is_dir() or not same_fs(p, dev):
                        continue
                    if bd.require and not (p / bd.require).exists():
                        continue
                    matched.add(bd.name)
                    try:
                        with os.scandir(p) as contents:
                            if next(contents, None) is None:
                                continue  # empty build fixtures reclaim nothing
                    except OSError:
                        continue
                    yield rule.group, here, p, bd
        skip_here = skip | SKIP_DIRS if here == home else skip
        pending.extend(Path(dirs[d]) for d in dirnames
                       if d not in skip_here and d not in matched and same_fs(dirs[d], dev))


def project_activity(project: Path, cutoff: float | None = None, *, deadline: float | None = None) -> float:
    """Latest mtime across a project's sources, ignoring build dirs and VCS.

    Proxy for "when was this last worked on". Build-dir mtimes are useless:
    cargo rewrites target/.rustc_info.json on every invocation, node tooling
    touches node_modules/.cache, etc.
    With a cutoff, stop as soon as one source proves the project is active.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return float("inf")
    prune = ALL_BUILD_DIR_NAMES | ALWAYS_PRUNE
    latest = 0.0
    try:
        project_stat = project.stat()
        latest = project_stat.st_mtime
    except OSError:
        return float("inf")
    if cutoff is not None and latest >= cutoff:
        return latest
    dev = project_stat.st_dev
    unreadable = False
    def onerror(error: OSError) -> None:
        nonlocal unreadable
        unreadable = True
    for dirpath, dirnames, filenames in os.walk(project, followlinks=False, onerror=onerror):
        if deadline is not None and time.monotonic() >= deadline:
            return float("inf")  # incomplete check must NEVER qualify a project for deletion
        dirnames[:] = [d for d in dirnames if d not in prune and same_fs(Path(dirpath) / d, dev)]
        for f in filenames:
            if deadline is not None and time.monotonic() >= deadline:
                return float("inf")
            try:
                m = os.lstat(os.path.join(dirpath, f)).st_mtime
                if m > latest:
                    latest = m
                if cutoff is not None and latest >= cutoff:
                    return latest
            except OSError:
                unreadable = True
    return float("inf") if unreadable else latest


def scan_projects(ctx: Context, progress: Progress) -> list[Item]:
    started = time.monotonic()
    deadline = started + ctx.scan_seconds if ctx.scan_seconds is not None else None
    discovery_deadline = started + ctx.scan_seconds / 2 if ctx.scan_seconds is not None else None
    found: list[tuple[str, Path, Path, BuildDir]] = []
    seen: set[Path] = set()
    for root in ctx.roots:
        if discovery_deadline is not None and time.monotonic() >= discovery_deadline:
            break
        if not root.exists():
            console.print(f"[dim]skip[/] [yellow]{root}[/] (does not exist)")
            continue
        task = progress.add_task(f"scanning {tilde(root)}", detail="", total=None)
        visited = 0
        last_update = 0.0
        def visit(here: Path) -> None:
            nonlocal visited, last_update
            visited += 1
            now = time.monotonic()
            if now - last_update >= 0.1:
                progress.update(task, description=f"scanning {tilde(root)} ({visited:,} dirs)", detail=tilde(here))
                last_update = now
        for group, project, build, bd in find_projects(root, ctx.extra_skip, visit=visit, deadline=discovery_deadline):
            if build in seen:
                continue
            seen.add(build)
            if group in ctx.groups:
                found.append((group, project, build, bd))
        progress.remove_task(task)

    if discovery_deadline is not None and time.monotonic() >= discovery_deadline:
        ctx.warnings.append(f"projects: search stopped at its {ctx.scan_seconds:g}s budget, deeper projects were not seen. "
                            "--thorough searches everything, --root PATH narrows it")

    if not found:
        return []

    # A nested project (monorepo package, workspace member) inherits activity
    # from its outermost enclosing project: if you're working anywhere in the
    # repo, nothing in it gets swept.
    projects = sorted({p for _, p, _, _ in found}, key=lambda p: len(p.parts))
    tops: set[Path] = set()
    top_of: dict[Path, Path] = {}
    for p in projects:
        parent = next((t for t in p.parents if t in tops), None)
        if parent is None:
            tops.add(p)
            parent = p
        top_of[p] = parent

    task = progress.add_task(f"measuring activity in {len(tops)} project(s)",
                             detail="", total=len(tops))
    activity: dict[Path, float] = {}
    with ThreadPoolExecutor(max_workers=ctx.size_jobs) as pool:
        futs = {pool.submit(project_activity, t, ctx.cutoff, deadline=deadline): t for t in tops}
        for fut in as_completed(futs):
            t = futs[fut]
            activity[t] = fut.result()
            progress.update(task, detail=tilde(t))
            progress.advance(task)
    progress.remove_task(task)
    unchecked = sum(m == float("inf") for m in activity.values())
    if unchecked:
        ctx.warnings.append(f"projects: kept {unchecked} project(s) with incomplete activity checks (time budget or unreadable files).")

    # Source mtimes cannot see a dev server or agent still running out of an old checkout.
    busy = live_cwds()
    in_use = {t for t in activity if t.resolve() in busy}
    if in_use:
        ctx.warnings.append(f"projects: kept {len(in_use)} idle-looking project(s) that a running process is working in")
    now = time.time()
    items = []
    for group, project, build, bd in found:
        items.append(Item(
            group=group, label=bd.name + "/", path=build, tier=bd.tier,
            mtime=now if top_of[project] in in_use else activity[top_of[project]],
            note=bd.note, mode="rmtree", project=project,
        ))
    return items


# ---------------------------------------------------------------------------
# Global cleaners
# ---------------------------------------------------------------------------


def _dir_item(group: str, label: str, path: Path, tier: str, note: str = "",
              mode: str = "clear") -> Item | None:
    if not path.is_dir() or path.is_symlink():
        return None
    try:
        with os.scandir(path) as entries:
            if next(entries, None) is None:
                return None
    except OSError:
        return None
    return Item(group=group, label=label, path=path, tier=tier, note=note, mode=mode)


def _cmd_action(cmd: list[str], timeout: float = 60, cwd: Path | None = None,
                summary: Callable[[str], str] | None = None) -> Callable[[], str | None]:
    def act() -> str | None:
        # Put plugins/children (notably docker-buildx) in the same client group.
        # Killing only the docker executable can leave its plugin running.
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              cwd=cwd, start_new_session=True) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    cancelled = getattr(_cleanup_thread, "cancelled", None)
                    if cancelled is not None and cancelled.is_set():
                        raise CleanupDeferred("cancelled; client stopped, daemon work may still be running")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CleanupDeferred(
                            f"timed out after {timeout:g}s; client stopped, cleanup may be partial")
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except (CleanupDeferred, KeyboardInterrupt):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            if process.returncode != 0:
                tail = (stderr or stdout).strip().splitlines()
                if "directory not empty" in (stderr or stdout).lower():
                    raise CleanupDeferred("cache changed during cleanup; some files remain. "
                                          "Finish active builds before trying again")
                raise RuntimeError(tail[-1] if tail else f"exit {process.returncode}")
            out = stdout.strip()
            return summary(out) if summary else None
    return act


def scan_rust_global(ctx: Context) -> list[Item]:
    cargo_home = Path(os.environ.get("CARGO_HOME") or HOME / ".cargo")
    items = [
        _dir_item("rust", "cargo registry sources", cargo_home / "registry" / "src", SAFE,
                  "extracted from cached .crate files"),
        _dir_item("rust", "cargo git checkouts", cargo_home / "git" / "checkouts", SAFE,
                  "re-extracted from git/db"),
        _dir_item("rust", "cargo registry downloads", cargo_home / "registry" / "cache", AGGRESSIVE,
                  ".crate files; re-downloaded"),
        _dir_item("rust", "cargo git db", cargo_home / "git" / "db", AGGRESSIVE,
                  "git deps; re-cloned"),
    ]
    return [i for i in items if i]


def scan_go(ctx: Context) -> list[Item]:
    go = shutil.which("go")
    gocache = gomod = None
    if go:
        r = run([go, "env", "GOCACHE", "GOMODCACHE"], timeout=30)
        if r and r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if len(lines) == 2:
                gocache, gomod = (Path(l) for l in lines)
    if gocache is None:
        gocache = (MAC_CACHES if IS_MAC else XDG_CACHE) / "go-build"
    if gomod is None:
        gomod = Path(os.environ.get("GOPATH") or HOME / "go") / "pkg" / "mod"

    items = []
    it = _dir_item("go", "go build cache", gocache, SAFE, "rebuilt on demand")
    if it:
        if go:
            it.mode, it.action = "action", _cmd_action([go, "clean", "-cache"], timeout=ctx.command_timeout)
        items.append(it)
    it = _dir_item("go", "go module cache", gomod, AGGRESSIVE, "re-downloaded")
    if it:
        if go:
            it.mode, it.action = "action", _cmd_action([go, "clean", "-modcache"], timeout=ctx.command_timeout)
        items.append(it)
    return items


def scan_node_global(ctx: Context) -> list[Item]:
    items = []
    pnpm = shutil.which("pnpm")
    if pnpm:
        r = run([pnpm, "store", "path"], timeout=30)
        store = Path(r.stdout.strip()) if r and r.returncode == 0 and r.stdout.strip() else None
        if store and store.is_dir():
            items.append(Item(
                group="node", label="pnpm store prune", path=store, size=None, tier=SAFE,
                note="unreferenced packages only", mode="action", shadowed=True,
                action=_cmd_action([pnpm, "store", "prune"], timeout=ctx.command_timeout, summary=lambda o: o.splitlines()[-1] if o else None),
            ))
            it = _dir_item("node", "pnpm store", store, AGGRESSIVE, "re-downloaded; live projects keep hardlinks")
            if it:
                items.append(it)
    return items


def scan_python_global(ctx: Context) -> list[Item]:
    items = []
    uv = shutil.which("uv")
    uv_dir = None
    if uv:
        r = run([uv, "cache", "dir"], timeout=30)
        if r and r.returncode == 0 and r.stdout.strip():
            uv_dir = Path(r.stdout.strip())
    if uv_dir is None:
        uv_dir = XDG_CACHE / "uv"
    if uv_dir.is_dir():
        if uv:
            items.append(Item(
                group="python", label="uv cache prune", path=uv_dir, tier=SAFE,
                note="unreachable entries only", mode="action", shadowed=True,
                action=_cmd_action([uv, "cache", "prune"], timeout=ctx.command_timeout, summary=lambda o: o.splitlines()[-1] if o else None),
            ))
        it = _dir_item("python", "uv cache", uv_dir, AGGRESSIVE, "wheels + sources; re-downloaded")
        if it:
            if uv:
                it.mode, it.action = "action", _cmd_action([uv, "cache", "clean"], timeout=ctx.command_timeout)
            items.append(it)
    return items


def _docker_df(docker: str) -> dict[str, dict]:
    r = run([docker, "system", "df", "--format", "{{json .}}"], timeout=60)
    out: dict[str, dict] = {}
    if r is None or r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if "Type" in d:
            out[d["Type"]] = d
    return out


def docker_vm_disks() -> list[tuple[str, Path]]:
    """Host-side disk images of local docker VMs: where the space really sits on macOS."""
    lib = HOME / "Library"
    cands = [("OrbStack", p) for p in (lib / "Group Containers").glob("*.dev.orbstack/data/data.img*")]
    cands += [
        ("OrbStack", HOME / ".orbstack" / "data" / "data.img"),
        ("Docker Desktop", lib / "Containers" / "com.docker.docker" / "Data" / "vms" / "0" / "data" / "Docker.raw"),
        ("Colima", HOME / ".colima" / "_lima" / "_disks" / "colima" / "datadisk"),
        ("Colima", HOME / ".colima" / "_lima" / "colima" / "diffdisk"),
        ("Rancher Desktop", lib / "Application Support" / "rancher-desktop" / "lima" / "0" / "diffdisk"),
    ]
    return [(name, p) for name, p in cands if p.is_file()]


def docker_endpoint(docker: str, context: str | None = None) -> str:
    """Resolve named contexts too: a friendly context name may point at a remote host."""
    if context is None:
        context = os.environ.get("DOCKER_CONTEXT") or None
        if context is None and os.environ.get("DOCKER_HOST"):
            return os.environ["DOCKER_HOST"]
    r = run([docker, "context", "inspect", *([context] if context else []),
             "--format", "{{.Endpoints.docker.Host}}"], timeout=10)
    return r.stdout.strip() if r and r.returncode == 0 else ""


def _local_docker_endpoint(endpoint: str) -> bool:
    return endpoint.startswith(("unix://", "npipe://"))


def _buildx_builders(docker: str) -> list[dict]:
    r = run([docker, "buildx", "ls", "--format", "{{json .}}"], timeout=30)
    builders: dict[str, dict] = {}
    if r is None or r.returncode != 0:
        return []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("Name"), str) and d["Name"]:
            builders[d["Name"]] = d  # some buildx versions emit each builder more than once
    return list(builders.values())


def _builder_is_local(docker: str, builder: dict) -> bool:
    endpoints = [n.get("Endpoint") or "" for n in builder.get("Nodes") or [] if isinstance(n, dict)]
    if not endpoints:
        return False
    for endpoint in endpoints:
        if not isinstance(endpoint, str) or not endpoint:
            return False
        resolved = endpoint if "://" in endpoint else docker_endpoint(docker, endpoint)
        if not _local_docker_endpoint(resolved):
            return False
    return True


def _container_builders(docker: str) -> list[str]:
    """Local container builders: daemon-level pruning does not cover their caches."""
    return list(dict.fromkeys(d["Name"] for d in _buildx_builders(docker)
                              if d.get("Driver") == "docker-container" and _builder_is_local(docker, d)))


def _buildx_reclaimable(docker: str, builder: str) -> int | None:
    r = run([docker, "buildx", "du", "--builder", builder], timeout=120)
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"^Reclaimable:\s*(\S+)", r.stdout, re.M)
    return parse_size(m.group(1)) if m else None


def _builder_cache_sizes(docker: str, names: list[str]) -> dict[str, int | None]:
    with ThreadPoolExecutor(max_workers=PROBE_JOBS) as pool:
        return dict(zip(names, pool.map(lambda name: _buildx_reclaimable(docker, name), names)))


def _builder_inventory(docker: str) -> tuple[list[dict], dict[str, bool], dict[str, int | None]]:
    builders = _buildx_builders(docker)
    local = {b["Name"]: _builder_is_local(docker, b) for b in builders if b.get("Driver") == "docker-container"}
    running = [b["Name"] for b in builders if local.get(b["Name"]) and
               any(n.get("Status") == "running" for n in b.get("Nodes") or [] if isinstance(n, dict))]
    return builders, local, _builder_cache_sizes(docker, running)


def scan_docker(ctx: Context) -> list[Item]:
    docker = shutil.which("docker")
    if not docker:
        return []
    if not _local_docker_endpoint(docker_endpoint(docker)):
        ctx.warnings.append("docker: endpoint is remote or could not be verified as local — skipped")
        return []
    held = ", ".join(f"{human_size(sz)} in the {name} VM disk" for name, sz in
                     ((name, file_disk_size(p)) for name, p in docker_vm_disks()) if sz >= 10 * 1024**3)
    info = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=15)
    if info is None or info.returncode != 0:
        ctx.warnings.append("docker: daemon not reachable — skipped"
                            + (f" ({held}; start it and re-run)" if held else ""))
        return []
    if held:
        ctx.warnings.append(f"docker: {held} — cleanup inside the VM may not immediately shrink the host file")

    df = _docker_df(docker) if ctx.measure_sizes else {}
    if ctx.measure_sizes and not df:
        ctx.warnings.append("docker: `docker system df` failed — some sizes unknown")

    def reclaimable(t: str) -> int | None:
        return parse_size(df[t]["Reclaimable"]) if t in df else None

    dangling = None
    r = run([docker, "images", "-f", "dangling=true", "--format", "{{.Size}}"], timeout=30) if ctx.measure_sizes else None
    if r and r.returncode == 0:
        dangling = sum(parse_size(l) or 0 for l in r.stdout.splitlines() if l.strip())

    def summarize(out: str) -> str | None:
        for line in reversed(out.splitlines()):
            if "reclaimed" in line.lower():
                return line.strip()
        return None

    def prune(label: str, size: int | None, tier: str, note: str, *args: str) -> None:
        # Unknown is not zero: a failed `system df` must not hide the row.
        if size is None or size > 0:
            items.append(Item(group="docker", label=label, size=size, tier=tier, note=note, mode="action",
                              action=_cmd_action([docker, *args], timeout=ctx.command_timeout, summary=summarize)))

    items: list[Item] = []
    containers = reclaimable("Containers")
    prune("docker system prune", None if None in (containers, dangling) else containers + dangling, SAFE,
          "stopped containers, dangling images, unused networks", "system", "prune", "-f")

    cache = reclaimable("Build Cache")
    if cache is None and ctx.measure_sizes:
        ctxname = run([docker, "context", "show"], timeout=15)
        if ctxname and ctxname.returncode == 0 and ctxname.stdout.strip():
            cache = _buildx_reclaimable(docker, ctxname.stdout.strip())
    prune("docker builder prune -a", cache, SAFE, "build cache; rebuilt on demand",
          "builder", "prune", "-a", "-f")
    builders = _container_builders(docker)
    caches = _builder_cache_sizes(docker, builders) if ctx.measure_sizes else dict.fromkeys(builders)
    for name, size in caches.items():
        prune(f"docker buildx prune ({name})", size, SAFE,
              "build cache of a container builder; rebuilt on demand",
              "buildx", "prune", "--builder", name, "-a", "-f")

    img = reclaimable("Images")
    prune("docker image prune -a", max(0, img - (dangling or 0)) if img is not None else None, AGGRESSIVE,
          "every image not used by a container; re-pulled", "image", "prune", "-a", "-f")

    vol = reclaimable("Local Volumes")
    r = run([docker, "volume", "ls", "-qf", "dangling=true"], timeout=30)
    unused = len(r.stdout.split()) if r and r.returncode == 0 else 0
    if ctx.docker_volumes == "all":
        prune("docker volume prune -a", vol if unused else 0, SAFE,
              f"{unused} unused volume(s), named ones included (enabled in config)", "volume", "prune", "-a", "-f")
    elif ctx.docker_volumes:
        prune("docker volume prune", None if unused else 0, SAFE,
              "unused anonymous volumes (enabled in config)", "volume", "prune", "-f")
    elif unused:
        ctx.warnings.append(
            f"docker: {unused} unused volume(s)" + (f" holding {human_size(vol)}" if vol else "")
            + " left alone, volumes hold data — opt in with docker.volumes = true (anonymous) or \"all\"")
    return items


def run_docker_audit() -> bool:
    """Read-only Docker disk/cache and container-memory inventory for a local daemon."""
    docker = shutil.which("docker")
    if not docker:
        console.print("[yellow]docker audit: docker is not installed[/]")
        return False
    endpoint = docker_endpoint(docker)
    if not _local_docker_endpoint(endpoint):
        console.print(Text("docker audit: endpoint is remote or could not be verified as local — skipped", style="yellow"))
        return False
    console.print(Text(f"Docker endpoint: {endpoint}", style="dim"))
    for name, disk in docker_vm_disks():
        console.print(Text(f"{name} VM disk: {human_size(file_disk_size(disk))} allocated on the host — {tilde(disk)}"))
    info = run([docker, "info", "--format", "{{.MemTotal}}"], timeout=15)
    if not info or info.returncode != 0:
        console.print("[yellow]docker audit: daemon not reachable; start the runtime and try again[/]")
        return False
    if info.stdout.strip().isdigit():
        console.print(Text(f"RAM visible to the Docker daemon: {human_size(int(info.stdout.strip()))} "
                           "(capacity, not host memory consumption)"))

    # The daemon's disk accounting and each builder may be slow independently.
    # Keep rendering on this thread and overlap only read-only probes.
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(_docker_df, docker): "disk usage",
                   pool.submit(run, [docker, "stats", "--no-stream", "--format", "{{json .}}"], timeout=20): "container memory",
                   pool.submit(_builder_inventory, docker): "builder caches"}
        results = {}
        with console.status("Reading Docker disk, memory and builder caches in parallel…") as status:
            for future in as_completed(pending):
                name = pending[future]
                try:
                    results[name] = future.result()
                except Exception:
                    results[name] = None
                remaining = [label for label in pending.values() if label not in results]
                status.update("Waiting for " + ", ".join(remaining) + "…")
    df = results["disk usage"] or {}
    stats = results["container memory"]
    builders, local, caches = results["builder caches"] or ([], {}, {})
    table = Table(title="Docker disk usage (daemon)", **TABLE, title_justify="left", expand=True)
    for label in ("resource", "total", "active", "size", "reclaimable"):
        table.add_column(label)
    for kind, values in df.items():
        table.add_row(*[Text(str(v)) for v in (kind, values.get("TotalCount", "?"), values.get("Active", "?"),
                                              values.get("Size", "?"), values.get("Reclaimable", "?"))])
    console.print(table)
    if not df:
        console.print("[yellow]Daemon disk usage unavailable; builder caches may still be measurable.[/]")

    containers = []
    if stats and stats.returncode == 0:
        for line in stats.stdout.splitlines():
            try:
                d = json.loads(line)
                mem = parse_size(str(d.get("MemUsage", "")).split("/", 1)[0], base=1024)
                if mem is not None:
                    containers.append((d, mem))
            except (ValueError, AttributeError):
                continue
        table = Table(title="running containers", **TABLE, title_justify="left", expand=True)
        table.add_column("memory", justify="right")
        table.add_column("CPU %", justify="right")
        table.add_column("container")
        for d, mem in sorted(containers, key=lambda row: -row[1]):
            table.add_row(human_size(mem), Text(str(d.get("CPUPerc", "?"))), Text(str(d.get("Name", d.get("ID", "?")))))
        console.print(table)
        console.print(Text(f"Reported container memory: {human_size(sum(mem for _, mem in containers))} "
                           f"across {len(containers)} container(s). Compare with VM processes in --mem --tree.", style="dim"))
    else:
        console.print("[yellow]Container memory stats unavailable.[/]")
    console.print(Text("Container stats and host VM footprint use different accounting; their difference is not all VM overhead. "
                       "Guest caches and services also use memory.", style="dim"))

    table = Table(title="buildx builders and container-builder caches", **TABLE,
                  title_justify="left", expand=True)
    for label in ("builder", "driver / status", "last activity", "reclaimable", "endpoint / notes"):
        table.add_column(label, overflow="fold")
    measured = []
    for builder in builders:
        name = builder["Name"]
        nodes = [n for n in builder.get("Nodes") or [] if isinstance(n, dict)]
        statuses = ", ".join(sorted({str(n.get("Status", "unknown")) for n in nodes})) or "unknown"
        endpoints = ", ".join(str(n.get("Endpoint", "?")) for n in nodes)
        driver = builder.get("Driver", "unknown")
        cache = None
        if driver != "docker-container":
            note = "daemon-managed cache; inspect its context" if driver == "docker" else "not a container builder"
        elif not local.get(name):
            note = "remote or unverified; not queried"
        elif not any(n.get("Status") == "running" for n in nodes):
            note = "not running; cache not queried"
        else:
            cache = caches.get(name)
            note = "cache rebuilt on demand" if cache is not None else "cache query unavailable"
        if cache is not None:
            measured.append(cache)
        table.add_row(*[Text(str(value)) for value in (
            name, f"{driver} / {statuses}", builder.get("LastActivity") or "unknown", human_size(cache),
            f"{endpoints}; {note}")])
    console.print(table)
    if not builders:
        console.print("[yellow]No buildx builders reported (buildx may be unavailable).[/]")
    console.print(Text(f"Measured container-builder reclaimable cache: {human_size(sum(measured) if measured else None)} "
                       f"from {len(measured)} builder(s); unmeasured caches excluded. "
                       "These estimates are not added to the daemon or VM disk totals.", style="dim"))
    console.print(Text("Builder status and last activity do not prove a builder is unused. Cache pruning causes rebuilds; "
                       "removing a builder also removes its configuration. Restarting the VM interrupts all its workloads. "
                       "Deleting volumes can remove persistent data.", style="dim"))
    console.print("[dim]Preview cleanup: dev-cleaner --only docker --dry-run[/]")
    return True


def scan_brew(ctx: Context) -> list[Item]:
    brew = shutil.which("brew")
    if not brew:
        return []
    if not ctx.measure_sizes:
        # Homebrew's dry-run performs the expensive walk we would repeat at cleanup.
        items = [Item("brew", "brew cleanup --prune=all", mode="action",
                      note="old versions, downloads, logs",
                      action=_cmd_action([brew, "cleanup", "-s", "--prune=all"], timeout=ctx.command_timeout))]
        items.append(Item("brew", "brew autoremove", tier=AGGRESSIVE, mode="action",
                          note="formulae no longer needed by anything",
                          action=_cmd_action([brew, "autoremove"], timeout=ctx.command_timeout)))
        return items
    items = []
    r = run([brew, "cleanup", "-n", "-s", "--prune=all"], timeout=180)
    if r is None:
        ctx.warnings.append("brew: `brew cleanup -n` timed out — skipped")
        return []
    size = None
    m = re.search(r"would free approximately\s+([\d.]+\s*[kKMGT]?i?B)", r.stdout)
    if m:
        size = parse_size(m.group(1), base=1024)
    n_lines = sum(1 for l in r.stdout.splitlines() if l.startswith("Would remove"))
    if size or n_lines:
        items.append(Item(
            group="brew", label="brew cleanup --prune=all", size=size or 0, tier=SAFE,
            note="old versions, downloads, logs", mode="action",
            action=_cmd_action([brew, "cleanup", "-s", "--prune=all"], timeout=ctx.command_timeout,
                               summary=lambda o: f"removed {sum(1 for l in o.splitlines() if l.startswith('Removing'))} item(s)"),
        ))
    r = run([brew, "autoremove", "-n"], timeout=120)
    if r and r.returncode == 0 and "Would autoremove" in r.stdout:
        formulae = [l.strip() for l in r.stdout.splitlines()[1:] if l.strip()]
        items.append(Item(
            group="brew", label="brew autoremove", size=None, tier=AGGRESSIVE,
            note=f"{len(formulae)} formula(e) no longer needed by anything: "
                 + ", ".join(formulae[:6]) + (" …" if len(formulae) > 6 else ""),
            mode="action", action=_cmd_action([brew, "autoremove"], timeout=ctx.command_timeout),
        ))
    return items


def scan_xcode(ctx: Context) -> list[Item]:
    if not IS_MAC:
        return []
    dev = HOME / "Library" / "Developer"
    if not dev.is_dir():
        return []
    items = [
        _dir_item("xcode", "Xcode DerivedData", dev / "Xcode" / "DerivedData", SAFE, "rebuilt on next build"),
        _dir_item("xcode", "Xcode cache", MAC_CACHES / "com.apple.dt.Xcode", SAFE),
        _dir_item("xcode", "CoreSimulator caches", dev / "CoreSimulator" / "Caches", SAFE),
    ]
    for os_name in ("iOS", "watchOS", "tvOS", "visionOS"):
        items.append(_dir_item(
            "xcode", f"{os_name} DeviceSupport", dev / "Xcode" / f"{os_name} DeviceSupport", AGGRESSIVE,
            "debug symbols; re-copied when a device connects"))
    items = [i for i in items if i]
    xcrun = shutil.which("xcrun")
    if xcrun and (dev / "CoreSimulator" / "Devices").is_dir():
        items.append(Item(
            group="xcode", label="simctl delete unavailable", size=None, tier=SAFE,
            note="simulators whose runtime is gone", mode="action",
            action=_cmd_action([xcrun, "simctl", "delete", "unavailable"], timeout=ctx.command_timeout),
        ))
    return items


def scan_known_caches(ctx: Context) -> list[Item]:
    items = []
    for label, group, tier, note, paths in KNOWN_CACHES:
        if group not in ctx.groups:
            continue
        for p in paths:
            it = _dir_item(group, label, p, tier, note)
            if it:
                items.append(it)
    return items


# ---------------------------------------------------------------------------
# Temp dirs and user-declared sweeps
# ---------------------------------------------------------------------------

# Temp entries that belong to running programs however idle they look.
TMP_KEEP_PREFIXES = ("com.apple.", ".com.apple.", "tmux-", "ssh-", "systemd-private-", "snap-private-",
                     ".X", ".ICE-", ".font-", ".XIM-", ".Test-")
TMP_KEEP_NAMES = {"TemporaryItems"}


@dataclass(frozen=True)
class Sweep:
    """Dirs whose idle children are disposable: temp dirs, caches the user declared."""
    group: str
    label: str
    path: Path                       # what is shown
    containers: tuple[Path, ...]     # dirs whose direct children are the candidates
    cutoff: float | None = None      # None = the run's idle threshold
    note: str = ""
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    keep: int = 0                    # newest N matching children per container always stay

    def matches(self, name: str) -> bool:
        return (any(fnmatch.fnmatchcase(name, p) for p in self.include)
                and not any(fnmatch.fnmatchcase(name, p) for p in self.exclude))


def sweep_days(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("cleanup days must be a finite nonnegative number")
    return float(value)


def extra_sweeps(entries: list[dict], now: float) -> list[Sweep]:
    """Parse explicit cleanup roots; patterns select direct children, never paths."""
    if not isinstance(entries, list):
        raise ValueError("extra must be a list of [[extra]] tables")
    sweeps = []
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"].strip():
            raise ValueError("each [[extra]] entry needs a nonempty path string")
        patterns = {}
        for key, default in (("include", ["*"]), ("exclude", [])):
            values = e.get(key, default)
            if (not isinstance(values, list) or any(
                    not isinstance(v, str) or not v or v in {".", ".."} or "/" in v or "\\" in v
                    for v in values)):
                raise ValueError(f"[[extra]] {key} must be a list of direct child name patterns (no paths)")
            patterns[key] = tuple(values)
        keep = e.get("keep", 0)
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
            raise ValueError("[[extra]] keep must be a nonnegative whole number of newest entries to keep")
        d = Path(e["path"]).expanduser().resolve()
        sweeps.append(Sweep("extra", "idle entries" if not keep else f"all but the newest {keep}", d, (d,),
                            cutoff=now - sweep_days(e["days"]) * 86400 if "days" in e else None,
                            keep=keep, **patterns))
    return sweeps


def idle_since(path: Path, depth: int = 2, budget: int = 2000) -> float | None:
    """Newest mtime in `path` down to `depth` levels; None when it must be left alone.

    Left alone: other users' files, symlinks, sockets/fifos/devices and any dir
    holding one (a live program's IPC). A dir's own mtime only moves when a
    direct child comes or goes, hence the shallow look inside.
    """
    if scan_expired():
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if st.st_uid != os.getuid() or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return None
    latest = st.st_mtime
    level = [str(path)] if stat.S_ISDIR(st.st_mode) else []
    for _ in range(depth):
        nxt: list[str] = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if scan_expired():
                            return None
                        budget -= 1
                        if budget < 0:
                            return latest
                        mode = e.stat(follow_symlinks=False)
                        if not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode)
                                or stat.S_ISLNK(mode.st_mode)):
                            return None
                        latest = max(latest, mode.st_mtime)
                        if stat.S_ISDIR(mode.st_mode):
                            nxt.append(e.path)
            except OSError:
                continue
        level = nxt
    return latest


def live_cwds() -> frozenset[Path]:
    """Every dir that is, or contains, the working directory of one of our running processes.

    mtimes cannot tell an idle database or dev server from an abandoned one: a
    postgres with no traffic leaves its data dir untouched for days. A process
    still standing in a dir is the proof that it is in use. Empty when the
    lookup is unavailable (the mtime checks still apply).
    """
    cwds: set[str] = set()
    proc = Path("/proc")
    if not IS_MAC and proc.is_dir():
        for entry in os.scandir(proc):
            if entry.name.isdigit():
                try:
                    cwds.add(os.readlink(os.path.join(entry.path, "cwd")))
                except OSError:
                    continue
    elif lsof := shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None):
        r = run([lsof, "-a", "-d", "cwd", "-Fn"], timeout=15)  # exits 1 when some processes are unreadable
        if r:
            cwds.update(line[1:] for line in r.stdout.splitlines() if line.startswith("n/"))
    # A server started elsewhere still runs out of the dir holding its executable or script.
    r = run(["ps", "-A", "-ww", "-o", "command="], timeout=15)
    if r and r.returncode == 0:
        cwds.update(token for line in r.stdout.splitlines() for token in line.split() if token.startswith("/"))
    busy: set[Path] = set()
    for cwd in cwds:
        p = Path(cwd)
        busy.add(p)
        busy.update(p.parents)
    return frozenset(busy)


def temp_roots() -> list[Path]:
    seen: set[Path] = set()
    bases = []
    for base in (Path("/tmp"), Path(tempfile.gettempdir())):
        try:
            base = base.resolve()
        except OSError:
            continue
        if base.is_dir() and base not in seen:
            seen.add(base)
            bases.append(base)
    return bases


def _owned_child_dirs(parent: Path) -> list[Path]:
    """Enumerate only real, same-filesystem directories owned by this user."""
    if scan_expired():
        return []
    try:
        dev = parent.lstat().st_dev
        with os.scandir(parent) as entries:
            return [Path(e.path) for e in entries
                    if (s := e.stat(follow_symlinks=False)).st_uid == os.getuid()
                    and s.st_dev == dev and stat.S_ISDIR(s.st_mode)]
    except OSError:
        return []


def tmp_sweeps(ctx: Context | None = None) -> list[Sweep]:
    cutoff = ctx.tmp_cutoff if ctx else None
    bases = temp_roots()
    out = [Sweep("tmp", "idle temp files", base, (base,), cutoff=cutoff) for base in bases]
    # Claude Code: <tmp>/claude-<uid>/<project>/<session>/{scratchpad,tasks}.
    for base in bases:
        claude = base / f"claude-{os.getuid()}"
        if claude not in _owned_child_dirs(base):
            continue
        projects = [p for p in _owned_child_dirs(claude) if p.name.startswith("-")]
        containers = [claude, *projects]
        if ctx is None or ctx.tmp_claude_scratch:
            sessions = [s for p in projects for s in _owned_child_dirs(p)]
            payloads = [d for s in sessions for d in _owned_child_dirs(s)
                        if d.name in {"scratchpad", "tasks"}]
            containers.extend([*sessions, *payloads])
        out.append(Sweep("tmp", "idle Claude Code scratch", claude, tuple(containers), cutoff=cutoff,
                         note="individual scratchpad entries and task output"))
    return out


def _sweep_action(entries: list[Path], cutoff: float) -> Callable[[], str | None]:
    parents = {p: p.parent.resolve() for p in entries}

    busy: frozenset[Path] = frozenset()

    def remove(p: Path) -> bool | None:
        if p.parent.resolve() != parents[p]:  # a container was replaced by a symlink
            return None
        if p in busy:  # a process moved in since the scan
            return None
        m = idle_since(p)
        if m is None or m >= cutoff:  # back in use (or gone) since the scan
            return None
        try:
            if stat.S_ISDIR(os.lstat(p).st_mode):
                force_rmtree(p)
            else:
                p.unlink()
            return True
        except OSError:
            return False

    def act() -> str | None:
        nonlocal busy
        busy = live_cwds()
        with ThreadPoolExecutor(max_workers=DEFAULTS["clean_jobs"]) as pool:
            results = list(pool.map(remove, entries))
        removed, failed, skipped = results.count(True), results.count(False), results.count(None)
        if failed and not removed:
            raise RuntimeError(f"could not remove any of {failed} entries")
        if failed:
            raise CleanupPartial(f"removed {removed}, {failed} could not be removed"
                                 + (f", {skipped} back in use" if skipped else ""))
        return (f"removed {removed}" + (f", {failed} failed" if failed else "")
                + (f", {skipped} back in use" if skipped else ""))
    return act


def scan_sweeps(sweeps: list[Sweep], ctx: Context) -> list[Item]:
    # A dir that is itself swept is never a candidate of its parent.
    reserved = {c for s in [*tmp_sweeps(ctx), *ctx.sweeps, *sweeps] for c in s.containers}
    # Reserve ancestors too: a broad rule must not remove a narrower rule's parent.
    reserved.update(p for c in tuple(reserved) for p in c.parents)
    items = []
    selected: set[Path] = set()
    busy = live_cwds() if sweeps else frozenset()
    in_use = 0
    for sw in sweeps:
        if scan_expired():
            break
        cutoff = sw.cutoff if sw.cutoff is not None else ctx.cutoff
        stale: list[Path] = []
        for c in sw.containers:
            try:
                if c.is_symlink():
                    continue
                names, dev = os.listdir(c), os.stat(c).st_dev
            except OSError:
                continue
            aged: list[tuple[float, Path]] = []
            complete = True
            for name in names:
                if scan_expired():
                    complete = False
                    break
                p = c / name
                if (p in reserved or p in selected or not sw.matches(name)
                        or name in TMP_KEEP_NAMES or name.startswith(TMP_KEEP_PREFIXES)):
                    continue
                if not same_fs(p, dev):
                    continue
                m = idle_since(p)
                if m is not None:
                    aged.append((m, p))
            if sw.keep:
                if not complete:
                    continue  # "newest" is unknowable from a partial listing: remove nothing
                aged = sorted(aged, key=lambda mp: mp[0], reverse=True)[sw.keep:]
            for m, p in aged:
                if m < cutoff:
                    if p in busy:
                        in_use += 1
                        continue
                    stale.append(p)
                    selected.add(p)
        if not stale:
            continue
        sizes = paths_size(stale, ctx.size_jobs) if ctx.measure_sizes else {}
        top = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
        note = f"{len(stale)} entries" + (
            "; biggest: " + ", ".join(f"{p.name[:28]} {human_size(n)}" for p, n in top) if top else "")
        if sw.include != ("*",) or sw.exclude:
            note += f"; include={list(sw.include)!r}, exclude={list(sw.exclude)!r}"
        if sw.keep:
            note += f"; newest {sw.keep} kept"
        items.append(Item(
            group=sw.group, label=sw.label, path=sw.path,
            size=sum(sizes.values()) if len(sizes) == len(stale) else None, tier=SAFE,
            note=f"{sw.note}; {note}" if sw.note else note, mode="action",
            action=_sweep_action(stale, cutoff),
        ))
    if in_use:
        ctx.warnings.append(f"{sweeps[0].group}: kept {in_use} idle-looking entr{'y' if in_use == 1 else 'ies'} "
                            "that a running process is working in")
    return items


def scan_tmp(ctx: Context) -> list[Item]:
    return scan_sweeps(tmp_sweeps(ctx), ctx)


def scan_extra(ctx: Context) -> list[Item]:
    return scan_sweeps(ctx.sweeps, ctx)


# ---------------------------------------------------------------------------
# Processes: orphans that hold memory long after their parent died
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    age: float                       # seconds since start
    cpu: float                       # ps CPU average; not an instantaneous measurement
    mem: int                         # bytes: footprint incl. compressed/swapped where known, else RSS
    exe: str
    started: str = ""                # ps lstart: distinguish recycled PIDs between snapshots
    footprint: bool = False          # macOS top supplied MEM; otherwise mem is RSS
    tty: str = ""                    # controlling terminal ("ttys012", "pts/3"); empty when detached
    cpu_time: float = 0.0            # cumulative CPU seconds: two snapshots give the true current rate

    @property
    def name(self) -> str:
        return os.path.basename(self.exe)

    @property
    def identity(self) -> tuple[int, int, str, str]:
        return self.pid, self.uid, self.exe, self.started


def parse_etime(s: str) -> float:
    """ps etime, [[dd-]hh:]mm:ss → seconds."""
    days, _, rest = s.rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_cputime(s: str) -> float:
    """ps time, [dd-][hh:]mm:ss[.cc] → seconds (macOS lets the minutes run past 59)."""
    days, _, rest = s.rpartition("-")
    total = 0.0
    for part in rest.split(":"):
        total = total * 60 + float(part)
    return int(days or 0) * 86400 + total


def _footprints() -> dict[int, int]:
    """pid → memory footprint. RSS collapses once a machine swaps, which is exactly when this matters."""
    if not IS_MAC:
        return {}
    r = run(["top", "-l", "1", "-n", "100000", "-stats", "pid,mem"], timeout=60,
            env={**os.environ, "LC_ALL": "C"})
    out: dict[int, int] = {}
    for line in (r.stdout.splitlines() if r and r.returncode == 0 else ()):
        m = re.fullmatch(r"\s*(\d+)\s+(\d+(?:\.\d+)?)([BKMGTP])[+-]?\s*", line)
        if m:
            out[int(m.group(1))] = int(float(m.group(2)) * 1024 ** "BKMGTP".index(m.group(3)))
    return out


def list_procs(footprints: bool = True) -> list[Proc]:
    r = run(["ps", "-A", "-ww", "-o", "pid=,ppid=,uid=,etime=,%cpu=,rss=,tty=,time=,lstart=,comm="], timeout=30,
            env={**os.environ, "LC_ALL": "C"})
    if r is None or r.returncode != 0:
        return []
    foot = _footprints() if footprints else {}
    procs = []
    for line in r.stdout.splitlines():
        f = line.split(None, 13)
        if len(f) < 14:
            continue
        try:
            pid, exe = int(f[0]), f[13].strip()
            cpu_time = parse_cputime(f[7])
            if not IS_MAC:  # Linux `comm` is a bare, truncated name, and ps rounds CPU time to seconds
                try:
                    exe = os.readlink(f"/proc/{pid}/exe")
                except OSError:
                    pass
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
                    cpu_time = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
                except (OSError, ValueError, IndexError):
                    pass
            procs.append(Proc(pid, int(f[1]), int(f[2]), parse_etime(f[3]), float(f[4].replace(",", ".")),
                              foot.get(pid, int(f[5]) * 1024), exe, " ".join(f[8:13]), pid in foot,
                              tty="" if f[6].strip("?-") == "" else f[6], cpu_time=cpu_time))
        except ValueError:
            continue
    return procs


def orphan_roots(ctx: Context) -> tuple[str, ...]:
    """Dirs whose executables never run on their own: test binaries, tool plugins."""
    dirs = {Path("/tmp"), Path(tempfile.gettempdir()), *(d.expanduser() for d in ctx.orphan_dirs)}
    out = set()
    for d in dirs:
        for form in (d, d.resolve()):  # ps reports /var/folders/…, the fs says /private/var/folders/…
            out.add(str(form).rstrip("/") + "/")
    return tuple(sorted(out))


def find_orphans(procs: list[Proc], roots: tuple[str, ...], min_age: float) -> list[Proc]:
    """Mine, re-parented to init, idle, older than the threshold, running from one of `roots`."""
    me = os.getuid()
    return [p for p in procs
            if p.ppid == 1 and p.uid == me and p.age >= min_age and p.cpu <= 1.0 and p.exe.startswith(roots)]


def _name_counts(procs: list[Proc], n: int = 5) -> str:
    counts: dict[str, int] = defaultdict(int)
    for p in procs:
        counts[p.name] += 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{name} ×{c}" for name, c in top[:n]) + (f", +{len(top) - n} more" if len(top) > n else "")


def _signal_and_wait(targets: list[tuple[Proc, int]], grace: float = 8) -> int:
    """Send each process its polite signal, then SIGKILL what outlives the grace period. Returns the forced count."""
    def alive(p: Proc) -> bool:
        try:
            os.kill(p.pid, 0)
            return True
        except OSError:
            return False

    def send(p: Proc, sig: int) -> None:
        try:
            os.kill(p.pid, sig)
        except OSError:
            pass

    for p, sig in targets:
        send(p, sig)
    deadline = time.time() + grace
    while time.time() < deadline and any(alive(p) for p, _ in targets):
        time.sleep(0.25)
    stuck = [p for p, _ in targets if alive(p)]
    if stuck:
        # A process may exit during the grace period and its PID be reused.
        identities = {p.identity for p in list_procs()}
        stuck = [p for p in stuck if p.identity in identities]
    for p in stuck:
        send(p, signal.SIGKILL)
    return len(stuck)


def _stop_action(targets: list[Proc], roots: tuple[str, ...], min_age: float) -> Callable[[], str | None]:
    def act() -> str | None:
        # Pids get recycled and orphans get adopted: only signal what still qualifies.
        want = {p.identity for p in targets}
        live = [p for p in find_orphans(list_procs(), roots, min_age)
                if p.identity in want]
        forced = _signal_and_wait([(p, signal.SIGTERM) for p in live])  # lets a supervisor tear its children down
        return f"stopped {len(live)}" + (f" ({forced} forced)" if forced else "")
    return act


SESSION_MIN_MEM = 64 * 1024**2       # a bare idle shell holds nothing worth closing a terminal for


@dataclass(frozen=True)
class Session:
    """Everything of ours hanging off one terminal: a tmux pane, a terminal tab, an ssh login."""
    tty: str
    idle: float                      # seconds since the terminal last carried input or output
    procs: tuple[Proc, ...]          # on the tty, plus same-owner descendants that detached from it

    @property
    def mem(self) -> int:
        return sum(p.mem for p in self.procs)

    @property
    def main(self) -> Proc:
        return max(self.procs, key=lambda p: p.mem)


def _own_ttys(procs: list[Proc]) -> set[str]:
    """Terminals this run sits in (ours and every ancestor's): never candidates."""
    by_pid = {p.pid: p for p in procs}
    out, pid, seen = set(), os.getpid(), set()
    while pid in by_pid and pid not in seen:
        seen.add(pid)
        out.add(by_pid[pid].tty)
        pid = by_pid[pid].ppid
    try:
        out.add(os.ttyname(sys.stdin.fileno()).removeprefix("/dev/"))
    except (OSError, ValueError, AttributeError):
        pass
    out.discard("")
    return out


def tty_idle(tty: str, now: float) -> float | None:
    """Seconds since the terminal last carried output (mtime) or input (atime): the clock `w` reports."""
    try:
        st = os.stat(f"/dev/{tty}")
    except OSError:
        return None
    return max(0.0, now - max(st.st_mtime, st.st_atime))


def terminal_sessions(procs: list[Proc], now: float | None = None) -> list[Session]:
    """Group our processes by controlling terminal, with how long each terminal has been silent.

    An agent or shell left open in a tmux pane for days shows up here even
    though each of its processes looks like any other long-running program.
    """
    now = time.time() if now is None else now
    me = os.getuid()
    mine = [p for p in procs if p.uid == me]
    children: dict[int, list[Proc]] = defaultdict(list)
    for p in mine:
        children[p.ppid].append(p)
    members: dict[str, dict[int, Proc]] = defaultdict(dict)
    for p in mine:
        if not p.tty:
            continue
        members[p.tty][p.pid] = p
        pending = list(children[p.pid])
        while pending:  # helpers that dropped the terminal (language servers, MCP servers) die with it
            q = pending.pop()
            if q.tty or q.pid in members[p.tty]:
                continue
            members[p.tty][q.pid] = q
            pending.extend(children[q.pid])
    own = _own_ttys(procs)
    out = []
    for tty, found in members.items():
        if tty in own:
            continue
        idle = tty_idle(tty, now)
        if idle is not None:
            out.append(Session(tty, idle, tuple(found.values())))
    return out


def idle_sessions(sessions: list[Session], min_idle: float) -> list[Session]:
    """Silent past the threshold, with nothing started or computing in it since."""
    return [s for s in sessions
            if s.idle >= min_idle and s.mem >= SESSION_MIN_MEM
            and all(p.age >= min_idle and p.cpu <= 1.0 for p in s.procs)]


def _close_sessions_action(targets: list[Session], min_idle: float) -> Callable[[], str | None]:
    def act() -> str | None:
        # Re-qualify from a fresh snapshot: a session that printed, ran or spawned anything since stays.
        want = {s.tty: {p.identity for p in s.procs if p.tty} for s in targets}
        live = [s for s in idle_sessions(terminal_sessions(list_procs()), min_idle)
                if s.tty in want and want[s.tty] == {p.identity for p in s.procs if p.tty}]
        # SIGHUP is what closing the terminal sends; detached helpers get SIGTERM.
        signals = [(p, signal.SIGHUP if p.tty else signal.SIGTERM) for s in live for p in s.procs]
        forced = _signal_and_wait(signals)
        kept = len(targets) - len(live)
        return (f"closed {len(live)} session(s), {len(signals)} processes" + (f" ({forced} forced)" if forced else "")
                + (f", {kept} back in use" if kept else ""))
    return act


def scan_procs(ctx: Context) -> list[Item]:
    roots, min_age = orphan_roots(ctx), time.time() - ctx.cutoff
    procs = list_procs()
    items = []
    orphans = find_orphans(procs, roots, min_age)
    if orphans:
        items.append(Item(
            group="procs", label="orphaned processes", size=sum(p.mem for p in orphans), tier=SAFE, ram=True,
            note=f"{len(orphans)} idle, parent gone: {_name_counts(orphans)}",
            mode="action", action=_stop_action(orphans, roots, min_age),
        ))
    sessions = terminal_sessions(procs)
    if ctx.idle_session_days is not None:
        min_idle = ctx.idle_session_days * 86400
        idle = idle_sessions(sessions, min_idle)
        if idle:
            items.append(Item(
                group="procs", label="idle terminal sessions", size=sum(s.mem for s in idle), tier=SAFE, ram=True,
                note=f"{len(idle)} silent ≥{ctx.idle_session_days:g}d, {sum(len(s.procs) for s in idle)} processes: "
                     + _name_counts([s.main for s in idle]),
                mode="action", action=_close_sessions_action(idle, min_idle),
            ))
    if ctx.stop_spinning:
        spinning = find_spinning(procs)
        if spinning:
            cores = sum(p.cpu_time / p.age for p in spinning)
            items.append(Item(
                group="procs", label="spinning processes", size=sum(p.mem for p in spinning), tier=SAFE, ram=True,
                cores=cores,
                note=f"{len(spinning)} detached, busy ≥{_duration(SPIN_AGE)} "
                     f"({sum(p.cpu_time for p in spinning) / 3600:.1f} CPU-hours so far): {_name_counts(spinning)}",
                mode="action", action=_stop_spinning_action(spinning),
            ))
    return items


SPIN_AGE = 6 * 3600                  # running this long ...
SPIN_AVG = 10.0                      # ... at this % of a core on average since it started ...
SPIN_NOW = 5.0                       # ... and still at it


def find_spinning(procs: list[Proc]) -> list[Proc]:
    """Our own detached programs that have burned CPU for hours and still do.

    Only executables under the home dir or a temp dir qualify: dev servers,
    venv interpreters, test binaries. Apps, system daemons, package-manager
    installs and VMs are someone else's to judge, and anything attached to a
    terminal is a job somebody is watching.
    """
    me = os.getuid()
    by_pid = {p.pid: p for p in procs}
    own, pid = set(), os.getpid()
    while pid in by_pid and pid not in own:
        own.add(pid)
        pid = by_pid[pid].ppid
    roots = tuple(sorted({str(d).rstrip("/") + "/" for base in (HOME, Path("/tmp"), Path(tempfile.gettempdir()))
                          for d in (base, base.resolve())}))
    return [p for p in procs
            if p.uid == me and not p.tty and p.pid not in own and p.age >= SPIN_AGE
            and p.cpu_time / p.age * 100 >= SPIN_AVG and p.cpu >= SPIN_NOW and p.exe.startswith(roots)]


def _stop_spinning_action(targets: list[Proc]) -> Callable[[], str | None]:
    def act() -> str | None:
        want = {p.identity for p in targets}
        live = [p for p in find_spinning(list_procs()) if p.identity in want]  # it calmed down or exited: leave it
        forced = _signal_and_wait([(p, signal.SIGTERM) for p in live])
        kept = len(targets) - len(live)
        return (f"stopped {len(live)}" + (f" ({forced} forced)" if forced else "")
                + (f", {kept} no longer spinning" if kept else ""))
    return act


def system_memory() -> dict[str, int]:
    """Best-effort host counters; unknown values stay unknown, never zero-filled."""
    out: dict[str, int] = {}
    if not IS_MAC:
        try:
            fields = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                fields[key] = int(value.split()[0]) * 1024
            for key, label in (("MemTotal", "RAM"), ("MemAvailable", "available")):
                if key in fields:
                    out[label] = fields[key]
            if "SwapTotal" in fields and "SwapFree" in fields:
                out.update(swap_total=fields["SwapTotal"], swap_used=fields["SwapTotal"] - fields["SwapFree"])
        except (OSError, ValueError, IndexError):
            pass
        return out
    total = run(["sysctl", "-n", "hw.memsize"], timeout=5)
    if total and total.returncode == 0 and total.stdout.strip().isdigit():
        out["RAM"] = int(total.stdout.strip())
    vm = run(["vm_stat"], timeout=5)
    if vm and vm.returncode == 0:
        page = re.search(r"page size of (\d+) bytes", vm.stdout)
        compressed = re.search(r"^Pages occupied by compressor:\s+(\d+)", vm.stdout, re.M)
        if page and compressed:
            out["compressed"] = int(page[1]) * int(compressed[1])
    swap = run(["sysctl", "-n", "vm.swapusage"], timeout=5)
    if swap and swap.returncode == 0:
        for key, label in (("total", "swap_total"), ("used", "swap_used")):
            m = re.search(rf"{key} = ([\d.]+[KMGT])", swap.stdout)
            if m:
                value = parse_size(m[1] + "B", base=1024)
                if value is not None:
                    out[label] = value
    return out


def process_commands() -> dict[int, str]:
    """Arguments help distinguish node/python servers and agent sessions."""
    r = run(["ps", "-A", "-ww", "-o", "pid=,args="], timeout=10)
    out = {}
    for line in (r.stdout.splitlines() if r and r.returncode == 0 else ()):
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            out[int(fields[0])] = fields[1]
    return out


def group_procs(procs: list[Proc], by_tree: bool = False) -> list[tuple[Proc, list[Proc]]]:
    """Group by full executable or by same-owner ancestry below init/service managers."""
    by_pid = {p.pid: p for p in procs}
    groups: dict[str | int, list[Proc]] = defaultdict(list)
    representatives = {}
    for p in procs:
        root = p
        if by_tree:
            chain: list[int] = []
            seen: set[int] = set()
            while root.pid not in seen:
                seen.add(root.pid)
                chain.append(root.pid)
                parent = by_pid.get(root.ppid)
                if (root.ppid <= 1 or parent is None or parent.uid != root.uid
                        or parent.name in {"systemd", "launchd", "init"}):
                    break
                root = parent
            else:  # a process-table race or malformed snapshot must not loop forever
                root = by_pid[min(chain[chain.index(root.pid):])]
        key = root.pid if by_tree else p.exe
        groups[key].append(p)
        representatives[key] = root
    return [(representatives[key], members) for key, members in groups.items()]


def memory_deltas(procs: list[Proc], previous: list[Proc]) -> dict[int, int]:
    """Compare the same process and metric; PID reuse or RSS fallback is not growth."""
    old = {(p.identity, p.footprint): p.mem for p in previous}
    return {p.pid: p.mem - old[key] for p in procs
            if (key := (p.identity, p.footprint)) in old}


def _memory_change(delta: int | None, new: bool = False) -> Text:
    if delta is None:
        return Text("new" if new else "—", style="dim")
    if not delta:
        return Text("—", style="dim")
    return Text(("+" if delta > 0 else "−") + human_size(abs(delta)), style="yellow" if delta > 0 else "green")


def render_memory(procs: list[Proc], ctx: Context, *, previous: list[Proc] | None = None,
                  commands: dict[int, str] | None = None, top: int = 10, pattern: str = "",
                  by_tree: bool = False, sort: str = "memory", elapsed: float | None = None,
                  full: bool = False) -> Group:
    commands = commands or {}
    needle = pattern.casefold()
    # Family filtering keeps descendants: the server may hold more RAM than its agent.
    groups = group_procs(procs, by_tree)
    if needle:
        def matches(p: Proc) -> bool:
            return needle in f"{p.pid} {p.exe} {commands.get(p.pid, '')}".casefold()
        if by_tree:
            groups = [(root, members) for root, members in groups if any(matches(p) for p in members)]
        else:
            groups = [(root, [p for p in members if matches(p)]) for root, members in groups]
        groups = [(root, members) for root, members in groups if members]
    selected = [p for _, members in groups for p in members]
    deltas = memory_deltas(procs, previous) if previous is not None else {}
    old_ids = {p.identity for p in previous} if previous is not None else set()

    def score(members: list[Proc]) -> float:
        if sort == "cpu":
            return sum(p.cpu for p in members)
        if sort == "growth":
            return sum(deltas.get(p.pid, 0) for p in members)
        return sum(p.mem for p in members)

    groups.sort(key=lambda row: (-score(row[1]), -sum(p.mem for p in row[1]), row[0].pid))
    selected.sort(key=lambda p: (-score([p]), -p.mem, p.pid))
    orphans = find_orphans(procs, orphan_roots(ctx), max(0, time.time() - ctx.cutoff))
    orphan_ids = {p.pid for p in orphans}
    tree_members = {p.pid: members for p, members in group_procs(procs, True)}
    parts: list = []
    status = system_memory()
    counters = [f"{key}: {human_size(status[key])}" for key in ("RAM", "available", "compressed") if key in status]
    if "swap_used" in status:
        counters.append(f"swap: {human_size(status['swap_used'])} / {human_size(status.get('swap_total'))}")
    parts.append(Text("  ".join(counters) or "Host memory counters unavailable", style="bold"))
    if status.get("swap_used", 0):
        parts.append(Text("Swap use alone does not prove current pressure; watch growth and system responsiveness.", style="dim"))
    sources = sum(p.footprint for p in procs)
    metric = ("macOS footprint" if sources == len(procs) else
              f"macOS footprint ({sources} PIDs), RSS fallback ({len(procs) - sources} PIDs)" if sources else "RSS")
    parts.append(Text(f"{time.strftime('%H:%M:%S')}  •  {len(selected)} / {len(procs)} processes  •  memory: {metric}", style="dim"))
    parts.append(Text("CPU is the ps average. Process memory can share pages; totals are estimates, not reclaimable RAM.", style="dim"))
    if previous is not None:
        parts.append(Text(f"Δ over {elapsed:.1f}s; surviving PIDs only. New processes show ‘new’; exits are excluded."
                          if elapsed is not None else "Δ compares surviving PIDs only; exits are excluded.", style="dim"))

    table = Table(title="memory by " + ("process family" if by_tree else "program (reported executable)"),
                  **TABLE, title_justify="left", expand=True)
    for label in ("memory", "Δ", "CPU %", "procs", "old/low"):
        table.add_column(label, justify="right")
    table.add_column("root PID" if by_tree else "top PID", justify="right")
    table.add_column("program", overflow="fold" if full else "ellipsis", no_wrap=not full, ratio=2)
    min_age = max(0, time.time() - ctx.cutoff)
    for root, members in groups[:top]:
        mem = sum(p.mem for p in members)
        matched = [deltas[p.pid] for p in members if p.pid in deltas]
        change = sum(matched) if matched else None
        old = sum(p.age >= min_age and p.cpu <= 1 for p in members)
        shown_pid = root.pid if by_tree else max(members, key=lambda p: p.mem).pid
        table.add_row(Text(human_size(mem), style=size_color(mem)),
                      _memory_change(change, previous is not None and all(p.identity not in old_ids for p in members)),
                      f"{sum(p.cpu for p in members):.1f}", str(len(members)), str(old), str(shown_pid),
                      Text(tilde(root.exe) if full or root.exe == root.name else f"{root.name} ({tilde(root.exe)})"))
    if not selected:
        parts.append(Text(f"No processes match {pattern!r}." if pattern else "No processes found.", style="yellow"))
    parts.append(table)
    parts.append(Text(f"Showing {min(top, len(groups))} / {len(groups)} groups. Old / low CPU means age ≥ "
                      f"{min_age / 86400:.2g}d and ps CPU ≤1%; it does not establish inactivity or a leak.", style="dim"))
    detail = Table(title="individual processes (same sort and filter)", **TABLE,
                   title_justify="left", expand=True)
    for label in ("PID", "PPID", "memory", "Δ", "CPU %", "age"):
        detail.add_column(label, justify="right")
    detail.add_column("owner", overflow="fold" if full else "ellipsis", no_wrap=not full)
    detail.add_column("command", overflow="fold" if full else "ellipsis", no_wrap=not full, ratio=2)
    for p in selected[:top]:
        hints = "mine" if p.uid == os.getuid() else f"uid {p.uid}"
        if p.pid in orphan_ids:
            hints += "; orphan candidate"
        command = commands.get(p.pid, p.exe)
        if not full and command.startswith(p.exe):
            command = p.name + command[len(p.exe):]
        detail.add_row(str(p.pid), str(p.ppid), Text(human_size(p.mem), style=size_color(p.mem)),
                       _memory_change(deltas.get(p.pid), previous is not None and p.identity not in old_ids),
                       f"{p.cpu:.1f}", f"{p.age / 86400:.1f}d", Text(hints),
                       Text(tilde(command)))
    parts.append(detail)
    if orphans:
        # Descendants may hold most of the memory (e.g. a postgres agent's server).
        related = {q.pid: q for p in orphans for q in tree_members.get(p.pid, [p])}
        parts.append(Text(f"Across all processes: {len(orphans)} configured orphan candidate(s): {human_size(sum(p.mem for p in orphans))}; "
                          f"with same-owner descendants: {len(related)} processes, "
                          f"{human_size(sum(p.mem for p in related.values()))}. Preview: dev-cleaner --only procs --dry-run",
                          style="yellow"))
        parts.append(Text("Cleanup targets the qualifying parents; children depend on their shutdown behavior. "
                          "PPID 1 alone does not mean a process is abandoned.", style="dim"))
    parts.extend(render_sessions(procs, ctx, commands, top=top, full=full))
    parts.append(Text("Use --tree for app/session totals, --filter TEXT to narrow down, "
                      "--mem-watch 5 --mem-sort growth to track growth, and --cpu for what is burning CPU. "
                      "Read-only; Ctrl-C stops watching.", style="dim"))
    return Group(*parts)


def _duration(seconds: float) -> str:
    return f"{seconds / 86400:.1f}d" if seconds >= 86400 else f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{seconds / 60:.0f}m"


def render_sessions(procs: list[Proc], ctx: Context, commands: dict[int, str], *, top: int = 10,
                    full: bool = False) -> list:
    """Terminals that went quiet while their programs kept the memory."""
    configured = ctx.idle_session_days is not None
    min_idle = ctx.idle_session_days * 86400 if configured else 86400.0
    idle = sorted(idle_sessions(terminal_sessions(procs), min_idle), key=lambda s: (-s.mem, s.tty))
    if not idle:
        return []
    table = Table(title=f"terminal sessions silent ≥{_duration(min_idle)} (no input, output, new process or CPU)",
                  **TABLE, title_justify="left", expand=True)
    for label in ("memory", "silent", "procs", "oldest"):
        table.add_column(label, justify="right")
    table.add_column("tty")
    table.add_column("largest process", overflow="fold" if full else "ellipsis", no_wrap=not full, ratio=2)
    for s in idle[:top]:
        command = commands.get(s.main.pid, s.main.exe)
        if not full and command.startswith(s.main.exe):
            command = s.main.name + command[len(s.main.exe):]
        table.add_row(Text(human_size(s.mem), style=size_color(s.mem)), _duration(s.idle), str(len(s.procs)),
                      _duration(max(p.age for p in s.procs)), s.tty, Text(f"{s.main.pid} {tilde(command)}"))
    total = (f"{len(idle)} silent session(s) hold {human_size(sum(s.mem for s in idle))} across "
             f"{sum(len(s.procs) for s in idle)} processes: {_name_counts([s.main for s in idle], 3)}. ")
    action = ("Running dev-cleaner offers to close them." if configured else
              "Closing them is turned off ([procs] idle_session_days = false).")
    return [table, Text(total + action, style="yellow")]


# Daemons whose CPU is a symptom of something else on the machine.
CPU_HINTS = {
    "mds": "Spotlight indexing", "mds_stores": "Spotlight indexing", "mdworker_shared": "Spotlight indexing",
    "fseventsd": "file-change notifications",
    "kernel_task": "kernel work",
    "tracker-miner-fs-3": "desktop file indexing", "baloo_file": "desktop file indexing",
}
CPU_HINT_ADVICE = {
    "Spotlight indexing": "Spotlight is indexing files as they change: exclude checkout, worktree and cache dirs under "
                          "System Settings › Spotlight › Search Privacy (or `sudo mdutil -i off <volume>`).",
    "file-change notifications": "fseventsd fans every file change out to watchers; heavy use follows mass writes and "
                                 "deletes (builds, installs, caches) and file watchers on large trees.",
    "kernel work": "kernel_task CPU on macOS is usually memory compression or thermal throttling: check --mem.",
    "desktop file indexing": "The desktop indexer is crawling changing files: exclude build and cache dirs from it.",
}
VM_NAMES = ("OrbStack Helper", "com.docker.virtualization", "com.docker.hyperkit", "qemu-system", "limactl", "vfkit")
SUSTAINED_AGE = 3600                 # a burner is "sustained" once it has run for an hour ...
SUSTAINED_AVG = 10.0                 # ... averaging at least this % of a core since it started


def cpu_rates(before: list[Proc], after: list[Proc], elapsed: float) -> dict[int, float]:
    """pid → % of one core used between two snapshots. Processes that came and went in between are invisible."""
    if elapsed <= 0:
        return {}
    old = {p.identity: p.cpu_time for p in before}
    rates = {}
    for p in after:
        if p.identity in old:
            rates[p.pid] = max(0.0, p.cpu_time - old[p.identity]) / elapsed * 100
        elif p.age <= elapsed + 1:  # started during the sample
            rates[p.pid] = p.cpu_time / elapsed * 100
    return rates


def render_cpu(before: list[Proc], procs: list[Proc], elapsed: float, *, commands: dict[int, str] | None = None,
               top: int = 10, pattern: str = "", by_tree: bool = False, full: bool = False) -> Group:
    commands = commands or {}
    rates = cpu_rates(before, procs, elapsed)
    needle = pattern.casefold()
    groups = group_procs(procs, by_tree)
    if needle:
        def matches(p: Proc) -> bool:
            return needle in f"{p.pid} {p.exe} {commands.get(p.pid, '')}".casefold()
        if by_tree:
            groups = [(root, members) for root, members in groups if any(matches(p) for p in members)]
        else:
            groups = [(root, [p for p in members if matches(p)]) for root, members in groups]
        groups = [(root, members) for root, members in groups if members]
    selected = [p for _, members in groups for p in members]

    def now(members: list[Proc]) -> float:
        return sum(rates.get(p.pid, 0.0) for p in members)

    def average(p: Proc) -> float:
        return p.cpu_time / p.age * 100 if p.age > 0 else 0.0

    def command(p: Proc) -> str:
        text = commands.get(p.pid, p.exe)
        if not full and text.startswith(p.exe):
            text = p.name + text[len(p.exe):]
        return tilde(text)

    def minutes(seconds: float) -> str:
        return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{seconds / 60:.1f}m"

    groups.sort(key=lambda row: (-now(row[1]), -sum(p.cpu_time for p in row[1]), row[0].pid))
    parts: list = []
    cores = os.cpu_count() or 1
    busy = sum(rates.values()) / 100
    try:
        load = os.getloadavg()
        ratio = load[0] / cores
        parts.append(Text.assemble(
            ("load: ", "bold"), (f"{load[0]:.1f}", "red bold" if ratio > 2 else "yellow bold" if ratio > 1 else "green bold"),
            (f" {load[1]:.1f} {load[2]:.1f} (1/5/15 min) on {cores} cores", "bold"),
            (f"  •  {ratio:.1f}× the cores wanted at once" if ratio > 1 else "", "bold")))
    except OSError:
        parts.append(Text(f"{cores} cores", style="bold"))
    parts.append(Text(f"{time.strftime('%H:%M:%S')}  •  {len(selected)} / {len(procs)} processes  •  "
                      f"sampled {elapsed:.1f}s: {busy:.1f} of {cores} cores busy in processes that lived through it",
                      style="dim"))
    parts.append(Text("now % is measured over the sample (100 = one full core); avg % is CPU time over lifetime. "
                      "Short-lived processes (compilers, test binaries) start and exit between samples and are not "
                      "attributed: a load far above the busy cores means many of them, or processes waiting on disk.",
                      style="dim"))

    table = Table(title="CPU by " + ("process family" if by_tree else "program (reported executable)"),
                  **TABLE, title_justify="left", expand=True)
    for label in ("now %", "CPU time", "memory", "procs"):
        table.add_column(label, justify="right")
    table.add_column("root PID" if by_tree else "top PID", justify="right")
    table.add_column("program", overflow="fold" if full else "ellipsis", no_wrap=not full, ratio=2)
    for root, members in groups[:top]:
        shown = root if by_tree else max(members, key=lambda p: rates.get(p.pid, 0.0))
        rate = now(members)
        table.add_row(Text(f"{rate:.1f}", style="red bold" if rate >= 100 else "yellow" if rate >= 25 else ""),
                      minutes(sum(p.cpu_time for p in members)), human_size(sum(p.mem for p in members)),
                      str(len(members)), str(shown.pid),
                      Text(tilde(root.exe) if full or root.exe == root.name else f"{root.name} ({tilde(root.exe)})"))
    if not selected:
        parts.append(Text(f"No processes match {pattern!r}." if pattern else "No processes found.", style="yellow"))
    parts.append(table)

    def process_table(title: str, rows: list[Proc]) -> Table:
        t = Table(title=title, **TABLE, title_justify="left", expand=True)
        for label in ("PID", "PPID", "now %", "avg %", "CPU time", "age", "memory"):
            t.add_column(label, justify="right")
        t.add_column("command", overflow="fold" if full else "ellipsis", no_wrap=not full, ratio=2)
        for p in rows:
            rate = rates.get(p.pid)
            t.add_row(str(p.pid), str(p.ppid), "—" if rate is None else f"{rate:.1f}", f"{average(p):.1f}",
                      minutes(p.cpu_time), _duration(p.age), human_size(p.mem),
                      Text(command(p) + ("" if p.uid == os.getuid() else f"  (uid {p.uid})")))
        return t

    current = sorted(selected, key=lambda p: (-rates.get(p.pid, 0.0), -p.cpu_time, p.pid))
    parts.append(process_table("busiest processes right now", current[:top]))
    sustained = sorted((p for p in selected if p.age >= SUSTAINED_AGE and average(p) >= SUSTAINED_AVG),
                       key=lambda p: (-p.cpu_time, p.pid))
    if sustained:
        parts.append(process_table(
            f"⏳  sustained: running ≥{_duration(SUSTAINED_AGE)} at ≥{SUSTAINED_AVG:g}% of a core on average "
            f"({len(sustained)} processes, {sum(average(p) for p in sustained) / 100:.1f} cores)", sustained[:top]))
        parts.append(Text("A server or agent that has averaged a steady share of a core for hours is either working "
                          "or spinning. Running dev-cleaner offers to stop your own detached ones once they "
                          f"have kept it up for {_duration(SPIN_AGE)}.", style="dim"))
    shown = {p.name for p in current[:top]} | {p.name for p in sustained[:top]}
    for hint in dict.fromkeys(CPU_HINTS[name] for name in sorted(shown) if name in CPU_HINTS):
        parts.append(Text(CPU_HINT_ADVICE[hint], style="yellow"))
    if any(name.startswith(VM_NAMES) for name in shown):
        parts.append(Text("A VM helper's CPU and memory belong to the containers inside it: "
                          "dev-cleaner --docker-audit lists them.", style="yellow"))
    parts.append(Text("Use --tree for whole apps/sessions, --filter TEXT to narrow down, --cpu-sample N for a longer "
                      "sample, and --mem for memory. Read-only.", style="dim"))
    return Group(*parts)


def run_cpu(*, sample: float = 2.0, top: int = 10, pattern: str = "", by_tree: bool = False,
            full: bool = False) -> bool:
    """Two snapshots: `ps` alone only offers a decaying or lifetime average."""
    before = list_procs(footprints=False)
    started = time.monotonic()
    if not before:
        console.print("[yellow]cpu: could not list processes[/]")
        return False
    with console.status(f"[dim]sampling CPU for {sample:g}s…[/]"):
        time.sleep(sample)
    elapsed = time.monotonic() - started
    procs = list_procs()  # memory footprints are read after the sample so `top` does not sit inside it
    if not procs:
        console.print("[yellow]cpu: could not list processes[/]")
        return False
    console.print(render_cpu(before, procs, elapsed, commands=process_commands(), top=top, pattern=pattern,
                             by_tree=by_tree, full=full))
    return True


def run_mem(ctx: Context, *, top: int = 10, pattern: str = "", by_tree: bool = False,
            sort: str = "memory", watch: float | None = None, full: bool = False) -> bool:
    """Read-only snapshots, optionally refreshed with per-process memory deltas."""
    previous = None
    sampled_at = None
    while True:
        procs = list_procs()
        now = time.monotonic()
        if not procs:
            console.print("[yellow]mem: could not list processes[/]")
            return False
        if watch and console.is_terminal:
            console.clear()
        console.print(render_memory(procs, ctx, previous=previous, commands=process_commands(), top=top,
                                    pattern=pattern, by_tree=by_tree, sort=sort,
                                    elapsed=now - sampled_at if sampled_at is not None else None, full=full))
        if watch is None:
            return True
        previous, sampled_at = procs, now
        time.sleep(watch)


GLOBAL_SCANNERS: dict[str, Callable[[Context], list[Item]]] = {
    "rust": scan_rust_global,
    "go": scan_go,
    "node": scan_node_global,
    "python": scan_python_global,
    "docker": scan_docker,
    "brew": scan_brew,
    "xcode": scan_xcode,
    "tmp": scan_tmp,
    "extra": scan_extra,
    "procs": scan_procs,
}

GROUP_HELP: dict[str, tuple[str, str]] = {
    # group: (safe, aggressive)
    "rust": ("stale target/, cargo registry sources + git checkouts, sccache", "cargo .crate downloads, git db"),
    "node": ("stale node_modules/.next/.turbo/..., npx cache, pnpm store prune", "npm/yarn/bun caches, whole pnpm store"),
    "python": ("stale .venv/.tox/.mypy_cache/..., uv cache prune", "uv/pip/poetry caches, pre-commit envs"),
    "jvm": ("stale build/, target/, .gradle/, gradle daemon logs", "~/.gradle/caches, ~/.m2, ivy, coursier"),
    "swift": ("stale .build/", "SwiftPM cache"),
    "elixir": ("stale _build/", "deps/"),
    "zig": ("stale zig-cache/zig-out, global zig cache", ""),
    "ocaml": ("stale _build/", ""),
    "haskell": ("stale .stack-work/, dist-newstyle/", ""),
    "dart": ("stale build/, .dart_tool/", "~/.pub-cache"),
    "cmake": ("stale build/ (with CMakeCache.txt), cmake-build-*", ""),
    "dotnet": ("stale bin/, obj/", "~/.nuget/packages"),
    "terraform": ("", ".terraform/ dirs, plugin cache"),
    "php": ("", "vendor/, composer cache"),
    "go": ("go build cache, gopls / goimports / golangci-lint / staticcheck caches", "go module cache"),
    "docker": ("system prune (stopped containers, dangling images, networks), build cache of every builder incl. buildx containers; unused volumes if enabled in config",
               "all unused images (image prune -a)"),
    "brew": ("brew cleanup --prune=all", "brew autoremove"),
    "xcode": ("DerivedData, simulator caches, unavailable simulators", "*OS DeviceSupport"),
    "tools": ("ccache, kubectl cache", "playwright/puppeteer/cypress browsers, electron, deno, trivy, grype, helm, bazel, JetBrains caches"),
    "tmp": ("entries of /tmp and $TMPDIR, and Claude Code session scratch, idle past the threshold", ""),
    "extra": ("idle children of the dirs listed under [[extra]] in the config", ""),
    "procs": ("stops idle orphaned processes (parent gone) running from a temp dir or a [procs] orphan_dirs dir; "
              "closes terminal sessions silent for a day; stops your detached programs that have burned CPU for hours", ""),
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _status_table() -> Table:
    """One physical terminal line per row, even with very long paths/errors."""
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=11, no_wrap=True, overflow="ellipsis")
    table.add_column(width=10, no_wrap=True, overflow="ellipsis")
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    return table


def _elapsed(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{seconds:02d}"


class ScanProgress(Progress):
    """Keep discovery, tool checks and sizing anchored to three reserved rows."""

    def get_renderables(self):
        buckets = {name: [] for name in ("Projects", "Tools", "Sizes")}
        for task in self.tasks:
            category = ("Tools" if task.description.startswith("checking ") else
                        "Sizes" if task.description.startswith("sizing ") else "Projects")
            buckets[category].append(task)
        table = _status_table()
        for category, tasks in buckets.items():
            if not tasks:
                table.add_row(Text(category, style="bold"), Text("—", style="dim"), Text("", style="dim"))
                continue
            elapsed = max(task.elapsed or 0 for task in tasks)
            if category == "Tools":
                label = ", ".join(task.description.removeprefix("checking ") for task in tasks)
            else:
                task = tasks[-1]
                count = f" {int(task.completed)}/{int(task.total)}" if task.total is not None else ""
                label = f"{task.description}{count}  {task.fields.get('detail', '')}"
            table.add_row(Text(category, style="bold"), Text(_elapsed(elapsed), style="dim"), Text(label))
        yield table


def make_progress() -> Progress:
    return ScanProgress(console=console, transient=True, refresh_per_second=4, disable=not console.is_terminal)


def size_items(items: list[Item], workers: int, progress: Progress, cutoff: float | None = None) -> None:
    # Shadowed prune-style items (pnpm/uv prune) stay "?" — the whole dir size would overstate them.
    todo = [i for i in items if i.size is None and i.path is not None and not i.shadowed
            and i.group not in {"tmp", "extra"}  # sweeps measure selected entries, not their whole parent
            and not (cutoff is not None and i.is_project and i.mtime is not None and i.mtime >= cutoff)]
    if not todo:
        return
    task = progress.add_task(f"sizing {len(todo)} dir(s)", detail="", total=len(todo))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(dir_size, i.path): i for i in todo}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                it.size = fut.result()
            except Exception:
                it.size = None
            progress.update(task, detail=tilde(it.path))
            progress.advance(task)
    progress.remove_task(task)


def scan_all(ctx: Context) -> list[Item]:
    items: list[Item] = []
    deadline = time.monotonic() + ctx.scan_seconds if ctx.scan_seconds is not None else None

    def probe(name: str, fn: Callable[[Context], list[Item]]) -> tuple[list[Item], list[str]]:
        # Keep warning buffers private; merge results in the familiar group order.
        local_ctx = replace(ctx, warnings=[])
        _scan_thread.deadline = deadline
        try:
            found = fn(local_ctx)
        except Exception as e:  # one broken tool must not sink the run
            found = []
            local_ctx.warnings.append(f"{name}: scan failed ({e}) — skipped")
        finally:
            if scan_expired():
                local_ctx.warnings.append(f"{name}: scan time budget reached; some candidates may be missing. Use --thorough to finish.")
            _scan_thread.deadline = None
        return found, local_ctx.warnings

    with make_progress() as progress:
        probes = [(name, fn) for name, fn in GLOBAL_SCANNERS.items() if name in ctx.groups]
        probes.append(("known caches", scan_known_caches))
        with ThreadPoolExecutor(max_workers=PROBE_JOBS + 1) as pool:
            project_future = None
            if not ctx.caches_only and ctx.groups & set(PROJECT_GROUPS):
                # Reap tool results while discovery runs, so finished tools disappear immediately.
                project_future = pool.submit(scan_projects, ctx, progress)
            pending = {pool.submit(probe, name, fn): (index, progress.add_task(
                f"checking {name}", detail="", total=None)) for index, (name, fn) in enumerate(probes)}
            if project_future is not None:
                pending[project_future] = (None, None)
            results = {}
            for future in as_completed(pending):
                index, task = pending[future]
                if future is project_future:
                    items += future.result()
                else:
                    results[index] = future.result()
                    progress.remove_task(task)
            for index in range(len(probes)):
                found, warnings = results[index]
                items += found
                ctx.warnings.extend(warnings)
        if ctx.measure_sizes:
            size_items(items, ctx.size_jobs, progress, cutoff=None if ctx.measure_active else ctx.cutoff)

    # Dedupe by path+label (a `build/` can match jvm and dart in the same dir).
    seen: set[str] = set()
    out = []
    for it in items:
        if it.key() in seen:
            continue
        seen.add(it.key())
        out.append(it)
    return out


TINY = 1024**2


def partition(items: list[Item], ctx: Context):
    """→ (to_clean, active_projects, aggressive_extra)."""
    to_clean, active, extra = [], [], []
    for it in items:
        if it.is_project and it.size is not None and it.size < TINY:
            continue  # empty build dirs (test fixtures, fresh clones): rows of noise, nothing to free
        if it.is_project and it.mtime is not None and it.mtime >= ctx.cutoff:
            active.append(it)
            continue
        if it.tier == AGGRESSIVE:
            (to_clean if ctx.aggressive else extra).append(it)
        elif it.shadowed and ctx.aggressive:
            continue
        else:
            to_clean.append(it)

    def sort_key(i: Item):
        return (-(i.size if i.size is not None else -1),)

    to_clean.sort(key=sort_key)
    extra.sort(key=sort_key)
    return to_clean, active, extra


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bar(fraction: float, color: str, width: int = 24) -> Text:
    filled = min(width, max(0, round(fraction * width)))
    return Text("━" * filled, style=color).append("━" * (width - filled), style="grey30")


def _section(title: str, detail: str = "") -> Text:
    return Text.assemble((title, "bold"), ("  " + detail if detail else "", "dim"))


def _grid(rows: list[tuple], *, styles: tuple[str, ...] = (), right: tuple[int, ...] = (),
          wrap_last: bool = True) -> Padding:
    """Aligned, borderless rows indented under a section heading. The last column takes the slack."""
    width = max(len(r) for r in rows)
    grid = Table.grid(padding=(0, 2), expand=True)
    for n in range(width):
        last = n == width - 1
        # Leading columns keep their full width: only the last one gives way on a narrow terminal.
        fixed = None if last else max((Text(c).cell_len if isinstance(c, str) else c.cell_len)
                                      for c in (r[n] if n < len(r) else "" for r in rows))
        grid.add_column(style=styles[n] if n < len(styles) else "", justify="right" if n in right else "left",
                        min_width=fixed, ratio=1 if last else None, no_wrap=not (last and wrap_last),
                        overflow="fold" if last and wrap_last else "ellipsis")
    for row in rows:
        grid.add_row(*row, *[""] * (width - len(row)))
    return Padding(grid, (0, 0, 0, 2))


def health_rows(disk_was: int | None = None) -> list[tuple[str, Text]]:
    """Disk, memory and CPU at a glance. Missing counters are left out, never guessed."""
    rows: list[tuple[str, Text]] = []
    try:
        u = shutil.disk_usage(HOME)
    except OSError:
        u = None
    if u is not None and u.total:
        pct = u.used / u.total
        color = "red bold" if u.free < 20 * 1024**3 or pct > 0.9 else "yellow" if pct > 0.8 else "green"
        head = Text.assemble((f"{human_size(u.free)} free", color), (f" of {human_size(u.total)}  ", "dim"))
        tail = Text(f"  {pct * 100:.0f}% used" + (f"  was {human_size(disk_was)} free" if disk_was is not None else ""),
                    style="dim")
        # The bar is the part that gives way on a narrow terminal, never the numbers.
        room = console.width - 10 - head.cell_len - tail.cell_len
        line = Text.assemble(head, _bar(pct, color.split()[0], width=max(6, min(24, room))), tail)
        rows.append(("disk", line))
    mem = system_memory()
    if "RAM" in mem:
        ram, swap = mem["RAM"], mem.get("swap_used", 0)
        squeezed = mem.get("compressed", 0) + swap if "available" not in mem else ram - mem["available"]
        ratio = squeezed / ram if ram else 0
        color = "red bold" if ratio > (0.5 if "available" not in mem else 0.9) else "yellow" if ratio > (0.25 if "available" not in mem else 0.75) else "green"
        detail = (f"{human_size(mem['available'])} available" if "available" in mem else
                  f"{human_size(mem.get('compressed', 0))} compressed")
        rows.append(("memory", Text.assemble(
            (detail, color), (f" of {human_size(ram)}" + (f" · {human_size(swap)} swapped" if swap else ""), "dim"))))
    try:
        load, cores = os.getloadavg()[0], os.cpu_count() or 1
        color = "red bold" if load > 2 * cores else "yellow" if load > cores else "green"
        rows.append(("cpu", Text.assemble((f"load {load:.1f}", color), (f" on {cores} cores", "dim"))))
    except OSError:
        pass
    return rows


def health_grid(disk_was: int | None = None) -> Padding:
    return _grid(health_rows(disk_was), styles=("dim",), wrap_last=False)


def banner() -> Group:
    return Group(Text("dev-cleaner", style="bold"), health_grid())


def render_summary(items: list[Item], threshold: str, aggressive: bool) -> Group:
    """The plan in one screen: one row per cleaner, never one per path."""
    running = [i for i in items if i.ram]
    disk = [i for i in items if not i.ram]
    measured = any(i.size is not None for i in disk) or bool(running)
    rows: list[tuple] = []
    for item in running:  # memory and CPU first: they are what makes the machine slow right now
        frees = human_size(item.size) + " RAM" + (f" · {item.cores:.1f} cores" if item.cores else "")
        rows.append(("cpu" if item.cores else "memory", item.note.split(" ", 1)[0], Text(frees, style="cyan"),
                     Text(f"{item.label}: {item.note}")))
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in disk:
        groups[item.group].append(item)
    for group, members in groups.items():
        labels = list(dict.fromkeys(i.label for i in members))
        detail = ", ".join(labels[:4]) + (f", +{len(labels) - 4} more" if len(labels) > 4 else "")
        row: list = [group, str(len(members))]
        if measured:
            known = sum(i.size or 0 for i in members)
            row.append(Text(human_size(known) + ("+" if any(i.size is None for i in members) else ""),
                            style=size_color(known))
                       if any(i.size is not None for i in members) else Text("—", style="dim"))
        rows.append((*row, Text(detail)))
    detail = f"{len(items)} target{'s' if len(items) != 1 else ''} · project builds idle over {threshold}"
    if any(i.size is not None for i in disk):
        total = sum(i.size or 0 for i in disk)
        detail += f" · frees {human_size(total)}" + ("+" if any(i.size is None for i in disk) else "")
    if aggressive:
        detail += " · aggressive"
    styles = ("bold", "dim", "", "dim") if measured else ("bold", "dim", "dim")
    return Group(_section("Will clean", detail), _grid(rows, styles=styles, right=(1, 2) if measured else (1,),
                                                       wrap_last=False))


def render_table(items: list[Item], threshold: str, aggressive: bool) -> Table:
    title = "reclaimable"
    if aggressive:
        title += " [red](aggressive)[/]"
    table = Table(
        title=f"[bold]{title}[/] [dim](project dirs idle >{threshold})[/]",
        **TABLE,
        title_justify="left", show_lines=False, expand=True,
    )
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("size", justify="right", min_width=9)
    table.add_column("age", justify="right", min_width=6)
    table.add_column("what", overflow="fold")

    # Group order: biggest group first.
    totals: dict[str, int] = defaultdict(int)
    for it in items:
        totals[it.group] += 0 if it.ram else it.size or 0
    order = sorted(totals, key=lambda g: -totals[g])

    now = time.time()
    n = 0
    for gi, g in enumerate(order):
        if gi:
            table.add_section()
        table.add_row("", "", "", Text.from_markup(
            f"[bold]{g}[/]" + (f"  [dim]{human_size(totals[g])}[/]" if totals[g] else "")))
        for it in (i for i in items if i.group == g):
            n += 1
            if it.mtime is not None:
                age_d = (now - it.mtime) / 86400
                age = Text(f"{age_d:.1f}d" if age_d >= 1 else f"{age_d * 24:.1f}h", style=age_color(age_d))
            else:
                age = Text("—", style="dim")
            what = Text()
            if it.is_project:
                what.append(tilde(it.project), style="")
                what.append(f"  {it.label}", style="dim")
            else:
                what.append(it.label, style="bold" if it.mode == "action" else "")
                if it.path:
                    what.append(f"  {tilde(it.path)}", style="dim")
            if it.tier == AGGRESSIVE:
                what.append("  aggressive", style="red dim")
            if it.note and not it.is_project:  # project rows: the dir name says it all
                what.append(f"  ({it.note})", style="dim italic")
            size = Text(human_size(it.size) + (" RAM" if it.ram else ""), style="cyan" if it.ram else size_color(it.size))
            table.add_row(str(n), size, age, what)

    total = sum(i.size or 0 for i in items if not i.ram)
    unknown = sum(1 for i in items if i.size is None and not i.ram)
    table.add_section()
    table.add_row(
        "", Text((human_size(total) + "+" if total else "not measured") if unknown else human_size(total),
                 style="bold green"), "",
        Text(f"{len(items)} item(s) — reclaimable", style="bold green"),
    )
    return table


_WARNING_TOPIC = re.compile(r"^([a-z][a-z ]{1,15}): (.+)$", re.S)


def render_notes(active: list[Item], extra: list[Item], threshold: str, warnings: list[str]) -> list[Group]:
    """What this run leaves alone and how to include it, then what the scan could not do."""
    out = []
    left: list[tuple] = []
    active = [i for i in active if i.mtime != float("inf")]
    if active:
        projects = {i.project for i in active}
        what = f"{len(active)} build dir(s) in {len(projects)} project(s) edited within {threshold}"
        if all(i.size is not None for i in active):
            total = sum(i.size or 0 for i in active)
            left.append(("--hours N", Text.assemble(what, " holding ", (human_size(total), size_color(total)))))
        else:
            left.append(("--hours N", Text(what + " (sizes skipped; use --audit to measure them)")))
    if extra:
        top = ", ".join(i.label + (f" {human_size(i.size)}" if i.size is not None else "") for i in extra[:3])
        more = f", +{len(extra) - 3} more" if len(extra) > 3 else ""
        space = (human_size(sum(i.size or 0 for i in extra)) + " in " if all(i.size is not None for i in extra) else "")
        left.append(("--aggressive", Text.assemble(
            f"{space}{len(extra)} dependency cache(s) / unused image(s)", (f": {top}{more}", "dim"))))
    if left:
        out.append(Group(_section("Left alone", "add the flag to include it"), _grid(left, styles=("cyan",))))
    if warnings:
        rows = []
        for w in warnings:
            m = _WARNING_TOPIC.match(w)
            rows.append((m.group(1), Text(m.group(2))) if m else ("note", Text(w)))
        out.append(Group(_section("Notes"), _grid(rows, styles=("yellow",))))
    return out


def render_groups_table() -> Table:
    t = Table(**TABLE, expand=True)
    t.add_column("group", style="bold")
    t.add_column("default", overflow="fold")
    t.add_column("--aggressive adds", overflow="fold", style="dim")
    for g in ALL_GROUPS:
        safe, aggr = GROUP_HELP.get(g, ("", ""))
        t.add_row(g, safe or "—", aggr or "—")
    return t


# ---------------------------------------------------------------------------
# Audit: where the disk went, and how much of it we cover
# ---------------------------------------------------------------------------

AUDIT_MIN = 1024**3
AUDIT_ROWS = 30


def audit_rows(du_out: str, root: Path, min_bytes: int = AUDIT_MIN) -> list[tuple[Path, int]]:
    """Reduce `du -k -d N` output to the dirs worth showing: big ones, as deep as the bulk goes."""
    return audit_pick(_du_sizes(du_out), root, min_bytes)


def _du_sizes(du_out: str) -> dict[Path, int]:
    size: dict[Path, int] = {}
    for line in du_out.splitlines():
        kb, _, name = line.partition("\t")
        if kb.isdigit():
            size[Path(name)] = int(kb) * 1024
    return size


def audit_sizes(root: Path, workers: int, on_done: Callable[[int, int, int], None] | None = None,
                timeout: float = 3600) -> tuple[dict[Path, int], list[Path]]:
    """`du -x -d 3` of root, as one `du` per second-level dir run in parallel.

    A single du over a home dir with millions of files is one thread for many
    minutes. The top two levels are listed here (loose files counted from their
    block counts, mount points left out like `du -x`), and each dir below gets
    its own du. Returns the sizes and the dirs whose du did not finish.
    """
    try:
        dev = os.stat(root).st_dev
    except OSError:
        return {}, []
    size: dict[Path, int] = defaultdict(int)
    jobs: list[Path] = []

    def listing(d: Path, depth: int) -> None:
        size[d] += 0
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                size[d] += st.st_blocks * 512
            elif st.st_dev == dev:
                if depth:
                    jobs.append(Path(e.path))
                else:
                    listing(Path(e.path), 1)

    listing(root, 0)
    failed: list[Path] = []
    measured = 0

    def measure(d: Path) -> tuple[Path, dict[Path, int] | None]:
        r = run(["du", "-x", "-k", "-d", "1", str(d)], timeout=timeout)
        if r is None:
            return d, None
        # du exits 1 on dirs the OS will not let it read: what it did total is still good,
        # and a dir it could not open at all (macOS privacy-protected ones) counts as empty.
        return d, _du_sizes(r.stdout) or {d: 0}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, done in enumerate(as_completed([pool.submit(measure, j) for j in jobs]), 1):
            d, sizes = done.result()
            if sizes is None:
                failed.append(d)
            else:
                measured += sizes.get(d, 0)
                size.update({p: v for p, v in sizes.items() if p != d})
                size[d] = sizes.get(d, 0)
                size[d.parent] += size[d]
            if on_done:
                on_done(n, len(jobs), measured)
    size[root] += sum(v for p, v in size.items() if p.parent == root and p != root)
    return dict(size), sorted(failed)


def audit_pick(size: dict[Path, int], root: Path, min_bytes: int = AUDIT_MIN) -> list[tuple[Path, int]]:
    kids: dict[Path, list[Path]] = defaultdict(list)
    for d in size:
        kids[d.parent].append(d)
    rows: list[tuple[Path, int]] = []

    def pick(d: Path) -> None:
        big = [k for k in kids.get(d, ()) if size[k] >= min_bytes]
        if big and sum(size[k] for k in big) >= size[d] / 2:
            for k in big:
                pick(k)
        elif size[d] >= min_bytes:
            rows.append((d, size[d]))

    if root in size:
        pick(root)
    return rows


def covered(d: Path, total: int, items: list[Item]) -> int:
    """How much of dir `d` the given items would free."""
    n = 0
    for it in items:
        if it.path is None:
            continue
        if it.path == d or it.path in d.parents:
            # A cache item takes this whole dir even when its size was not measured. A pruning
            # command of unknown yield (uv cache prune) and a sweep of some of a dir's children
            # (tmp, extra) promise nothing beyond what they measured.
            if it.size and it.path == d:
                n += it.size  # a sweep's measured share of its own dir, not the whole of it
            elif it.size or (it.size is None and not it.shadowed and it.group not in ("tmp", "extra")):
                return total
            continue
        if it.size and d in it.path.parents:
            n += it.size
    return min(n, total)


def run_audit(ctx: Context, to_clean: list[Item], active: list[Item], extra: list[Item]) -> None:
    home = HOME.resolve()
    tmp = [s.path for s in tmp_sweeps(ctx)]
    swept = [d for d in tmp if home not in (d, *d.parents) and not any(o in d.parents for o in tmp)]
    rows: list[tuple[Path, int]] = []
    with make_progress() as progress:
        task = progress.add_task(f"measuring {tilde(home)} (this walks everything; minutes on a full disk)",
                                 detail="", total=None)
        sizes, unmeasured = audit_sizes(home, ctx.size_jobs, lambda done, total, measured: progress.update(
            task, detail=f"{done}/{total} dirs · {human_size(measured)} so far"))
        rows += audit_pick(sizes, home)
        progress.update(task, description="measuring temp dirs")
        rows += [(d, n) for d, n in paths_size(swept, ctx.size_jobs).items() if n >= AUDIT_MIN]
    if not rows:
        console.print("[yellow]audit: could not measure anything (is `du` installed?)[/]")
        return
    rows.sort(key=lambda dn: -dn[1])
    vm = {p: name for name, p in docker_vm_disks()}

    table = Table(
        title="[bold]where the disk went[/] [dim](dirs over 1 GB, and what dev-cleaner frees in each)[/]",
        **TABLE, title_justify="left", expand=True,
    )
    table.add_column("size", justify="right", min_width=9)
    table.add_column("frees now", justify="right", min_width=9)
    table.add_column("--aggressive", justify="right", min_width=9)
    table.add_column("where", overflow="fold")

    def cell(v: int) -> Text:
        return Text(human_size(v), style=size_color(v)) if v else Text("—", style="dim")

    partial = set(ALL_GROUPS) - ctx.groups  # --only/--skip: "not covered" may just mean "not run"
    blind = 0
    for d, n in rows[:AUDIT_ROWS]:
        now, more, busy = covered(d, n, to_clean), covered(d, n, extra), covered(d, n, active)
        where = Text(tilde(d))
        disk = next((name for p, name in vm.items() if d == p or d in p.parents), None)
        if disk:
            where.append(f"  ({disk} VM disk: freed from inside, see the docker group)", style="dim italic")
        elif busy:
            where.append(f"  ({human_size(busy)} in active projects)", style="dim italic")
        elif not (now or more):
            blind += n
            where.append("  not covered" + (" by the groups run" if partial else ""), style="yellow")
        table.add_row(Text(human_size(n), style=size_color(n)), cell(now), cell(more), where)
    console.print()
    console.print(table)
    if len(rows) > AUDIT_ROWS:
        console.print(f"[dim]+{len(rows) - AUDIT_ROWS} smaller dir(s) not shown[/]")
    if unmeasured:
        console.print(Text(f"! could not measure {len(unmeasured)} dir(s), left out of the sizes above: "
                           + ", ".join(tilde(d) for d in unmeasured[:5])
                           + (f", +{len(unmeasured) - 5} more" if len(unmeasured) > 5 else ""), style="yellow"))
    if blind and partial:
        console.print(Text.from_markup(
            f"[yellow]![/] [dim]{human_size(blind)} is not covered by the groups run; skipped:[/] "
            f"[bold]{', '.join(g for g in ALL_GROUPS if g in partial)}[/][dim]. drop --only/--skip for the full picture.[/]"))
    elif blind:
        console.print(Text.from_markup(
            f"[yellow]![/] [dim]{human_size(blind)} sits in dirs dev-cleaner does not touch. project dirs: add them "
            f"to[/] [bold]roots[/] [dim](or pass[/] [bold]--root[/][dim]). disposable dirs of your own tools: "
            f"declare them under[/] [bold]\\[\\[extra]][/] [dim]in the config.[/]"))


# ---------------------------------------------------------------------------
# Dedupe: identical files across checkouts share their blocks (APFS clones)
# ---------------------------------------------------------------------------

DEDUPE_SKIP = {".git", ".hg", ".svn"}      # VCS internals are left exactly as their tool wrote them
DEDUPE_SETTLE = 600                        # seconds a file must have been quiet: nothing is still writing it


def _clone(src: str, dst: str) -> None:
    """Copy-on-write copy: `dst` shares `src`'s blocks until either one is written to."""
    if not IS_MAC:
        raise OSError(errno.ENOTSUP, "copy-on-write clones need APFS (macOS)")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.clonefile(os.fsencode(src), os.fsencode(dst), 1) != 0:  # 1 = CLONE_NOFOLLOW
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst)


def _digest(path: str) -> bytes | None:
    h = hashlib.blake2b(digest_size=20)
    try:
        with open(path, "rb", buffering=0) as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.digest()


def dedupe_scan(roots: list[Path], min_size: int, on_dir: Callable[[str, int], None] | None = None
                ) -> dict[int, list[tuple[str, os.stat_result]]]:
    """Regular files of ours, by size, wherever at least two share one. Hard links and symlinks stay out."""
    by_size: dict[int, list[tuple[str, os.stat_result]]] = defaultdict(list)
    uid, settled, seen, count = os.getuid(), time.time() - DEDUPE_SETTLE, set(), 0
    for root in roots:
        try:
            dev = os.stat(root).st_dev
        except OSError:
            continue
        stack = [str(root)]
        while stack:
            here = stack.pop()
            if on_dir is not None:
                on_dir(here, count)
            try:
                with os.scandir(here) as entries:
                    for e in entries:
                        try:
                            if e.is_symlink():
                                continue
                            st = e.stat(follow_symlinks=False)
                            if st.st_dev != dev:
                                continue
                            if stat.S_ISDIR(st.st_mode):
                                if e.name not in DEDUPE_SKIP:
                                    stack.append(e.path)
                            elif (stat.S_ISREG(st.st_mode) and st.st_size >= min_size and st.st_nlink == 1
                                  and st.st_uid == uid and st.st_mtime < settled and (dev, st.st_ino) not in seen):
                                seen.add((dev, st.st_ino))
                                by_size[st.st_size].append((e.path, st))
                                count += 1
                        except OSError:
                            continue
            except OSError:
                continue
    return {size: files for size, files in by_size.items() if len(files) > 1}


def _share(src: str, src_st: os.stat_result, dst: str, dst_st: os.stat_result) -> bool:
    """Replace `dst` with a clone of the identical `src`, keeping dst's mode and times. False = left alone."""
    try:
        a, b = os.lstat(src), os.lstat(dst)
        if ((a.st_size, a.st_mtime_ns) != (src_st.st_size, src_st.st_mtime_ns)
                or (b.st_size, b.st_mtime_ns, b.st_nlink) != (dst_st.st_size, dst_st.st_mtime_ns, 1)):
            return False  # either file changed since it was hashed
        parent = os.path.dirname(dst)
        parent_st = os.stat(parent)
        tmp = os.path.join(parent, f".dev-cleaner-clone-{os.getpid()}-{get_ident()}")
        _clone(src, tmp)
        try:
            os.chmod(tmp, stat.S_IMODE(b.st_mode))
            os.utime(tmp, ns=(b.st_atime_ns, b.st_mtime_ns))
            os.replace(tmp, dst)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        os.utime(parent, ns=(parent_st.st_atime_ns, parent_st.st_mtime_ns))  # the swap must not make a project look edited
        return True
    except OSError:
        return False


def dedupe_sets(by_size: dict[int, list[tuple[str, os.stat_result]]], workers: int,
                on_done: Callable[[int, int], None] | None = None) -> list[list[tuple[str, os.stat_result]]]:
    """Sets of byte-identical files (two or more each), biggest saving first."""
    files = [f for group in by_size.values() for f in group]
    sets: dict[tuple[int, bytes], list[tuple[str, os.stat_result]]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, (f, digest) in enumerate(zip(files, pool.map(lambda f: _digest(f[0]), files)), 1):
            if digest is not None:
                sets[(f[1].st_size, digest)].append(f)
            if on_done is not None and n % 500 == 0:
                on_done(n, len(files))
    out = [group for group in sets.values() if len(group) > 1]
    out.sort(key=lambda group: -group[0][1].st_size * (len(group) - 1))
    return out


def run_dedupe(roots: list[Path], min_size: int, workers: int, *, dry_run: bool, yes: bool) -> bool:
    roots = [r for r in (Path(r).expanduser().resolve() for r in roots) if r.is_dir()]
    if not roots:
        console.print("[red bold]error:[/] --dedupe needs existing dirs: pass --root PATH or set paths under \\[dedupe]")
        return False
    if not IS_MAC:
        console.print("[red bold]error:[/] --dedupe needs APFS copy-on-write clones (macOS)")
        return False
    console.print()
    with console.status("") as status:
        last = [0.0]

        def walking(here: str, count: int) -> None:
            if time.monotonic() - last[0] > 0.2:
                last[0] = time.monotonic()
                status.update(Text(f"listing files · {count:,} candidates · {tilde(here)}", no_wrap=True, overflow="ellipsis"))
        by_size = dedupe_scan(roots, min_size, walking)
        total = sum(len(g) for g in by_size.values())
        status.update(f"comparing {total:,} files")
        sets = dedupe_sets(by_size, workers, lambda n, m: status.update(f"comparing {n:,}/{m:,} files"))
    extra = sum(len(g) - 1 for g in sets)
    dup = sum(g[0][1].st_size * (len(g) - 1) for g in sets)
    if not sets:
        console.print("[bold]No duplicate files found.[/]")
        return True
    per_root: dict[Path, int] = defaultdict(int)
    for g in sets:
        for path, st in g[1:]:
            per_root[next((r for r in roots if path.startswith(str(r) + os.sep)), roots[0])] += st.st_size
    console.print(_section("Will share", f"{extra:,} duplicate files in {len(sets):,} sets · up to {human_size(dup)}"))
    console.print(_grid([(Text(human_size(n), style=size_color(n)), Text(tilde(r)))
                         for r, n in sorted(per_root.items(), key=lambda kv: -kv[1])], right=(0,)))
    console.print(Text("\n  Each duplicate becomes a copy-on-write clone of one original: same content, mode and times, "
                       "no file removed.\n  Copies already sharing blocks free nothing, so the real saving is "
                       "measured after. du keeps counting every copy.", style="dim"))
    if dry_run:
        console.print("\n[dim]dry-run — no changes made.[/]\n")
        return True
    if not yes and not Confirm.ask(f"\n[bold]Share these {extra:,} files?[/]", default=False):
        console.print("[yellow]aborted.[/]")
        return True
    free_before, started = shutil.disk_usage(HOME).free, time.monotonic()
    shared = skipped = 0
    with console.status("sharing") as status:
        with ThreadPoolExecutor(max_workers=DEFAULTS["clean_jobs"]) as pool:
            def one(group: list[tuple[str, os.stat_result]]) -> tuple[int, int]:
                (src, src_st), ok = group[0], 0
                for dst, dst_st in group[1:]:
                    ok += _share(src, src_st, dst, dst_st)
                return ok, len(group) - 1 - ok
            for n, (ok, bad) in enumerate(pool.map(one, sets), 1):
                shared, skipped = shared + ok, skipped + bad
                if n % 200 == 0:
                    status.update(f"sharing · {shared:,}/{extra:,} files")
    freed = max(0, shutil.disk_usage(HOME).free - free_before)
    console.print()
    console.print(Text.assemble(("Freed ", "bold"), (human_size(freed), "bold green"),
                                (f"  in {_elapsed(time.monotonic() - started)} · {shared:,} files now share their blocks", "dim"),
                                (f" · {skipped:,} left alone (changed, or in a read-only dir)" if skipped else "", "yellow")))
    console.print(health_grid(disk_was=free_before))
    return True


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


@dataclass
class CleanupReport:
    cleaned: int = 0
    deferred: int = 0
    failed: int = 0
    outcomes: dict[int, tuple[str, str | None]] = field(default_factory=dict, compare=False, repr=False)  # id(item) -> (status, detail)


def _delete(item: Item, roots: list[Path], cancelled: Event | None = None
            ) -> tuple[Item, str | None, str | CleanupDeferred | None]:
    """Execute one selected cleanup; busy/partial is distinct from failure or success."""
    try:
        _cleanup_thread.cancelled = cancelled
        if cancelled is not None and cancelled.is_set():
            raise CleanupDeferred("cancelled before cleanup started")
        if item.mode == "action" and item.action:
            return item, item.action(), None
        assert item.path is not None
        assert_deletable(item.path, roots)
        if item.mode == "clear":
            clear_dir(item.path)
        else:
            force_rmtree(item.path)
        return item, None, None
    except CleanupDeferred as error:
        return item, None, error
    except Exception as error:
        return item, None, str(error).strip().splitlines()[-1] if str(error).strip() else repr(error)


def clean_all(items: list[Item], workers: int, roots: list[Path], *, verbose: bool = False) -> CleanupReport:
    result = CleanupReport()
    cancelled = Event()
    started = time.monotonic()
    free_before = shutil.disk_usage(HOME).free
    slot_count = min(max(1, workers), len(items))
    slots = [("waiting", "", None, None) for _ in range(slot_count)]
    outcomes = {}

    def render():
        completed = result.cleaned + result.deferred + result.failed
        freed = max(0, shutil.disk_usage(HOME).free - free_before)
        heading = Text.assemble(
            ("Cleaning  ", "bold"), _bar(completed / max(1, len(items)), "green"),
            (f"  {completed}/{len(items)}", "bold"), ("  freed ", "dim"), (human_size(freed), "bold green"),
            (f"  {_elapsed(time.monotonic() - started)}", "dim"), no_wrap=True, overflow="ellipsis")
        if result.deferred:
            heading.append(f"  {result.deferred} incomplete", style="yellow")
        if result.failed:
            heading.append(f"  {result.failed} failed", style="red")
        table = _status_table()
        styles = {"running": "cyan", "done": "green", "incomplete": "yellow", "failed": "red", "waiting": "dim"}
        for state, label, began, ended in slots:
            elapsed = _elapsed((ended if ended is not None else time.monotonic()) - began) if began is not None else "—"
            table.add_row(Text(state, style=styles[state]), Text(elapsed, style="dim"), Text(label))
        return Group(heading, table)

    def report(item: Item, note: str | None, error: str | CleanupDeferred | None) -> None:
        outcomes[id(item)] = (item, note, error)
        if error is None:
            result.cleaned += 1
        elif isinstance(error, CleanupDeferred):
            result.deferred += 1
        else:
            result.failed += 1

    # A group with tool actions is one serial lane: Docker prunes must not compete
    # with each other, nor should a tool race filesystem deletes of its own caches.
    action_groups = {item.group for item in items if item.mode == "action" and not item.is_project}
    grouped: dict[str, list[Item]] = defaultdict(list)
    lanes = []
    for item in items:
        if item.group in action_groups and not item.is_project:
            grouped[item.group].append(item)
        else:
            lanes.append([item])
    for group_items in grouped.values():
        lanes.append([i for i in group_items if i.mode != "action"] +
                     [i for i in group_items if i.mode == "action"])
    # Global caches first; independent project builds can use all remaining workers.
    lanes.sort(key=lambda lane: (lane[0].is_project, -(sum(i.size or 0 for i in lane))))

    with Live(get_renderable=render, console=console, transient=True,
              auto_refresh=console.is_terminal, refresh_per_second=4, vertical_overflow="crop"):
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            waiting = iter(lanes)
            pending = {}

            def submit(lane: list[Item], slot: int) -> None:
                item = lane[0]
                label = f"{item.group}: {item.label}"
                if item.project is not None:
                    label += f"  {tilde(item.project)}"
                slots[slot] = ("running", label, time.monotonic(), None)
                pending[pool.submit(_delete, item, roots, cancelled)] = (lane, slot)

            try:
                for slot in range(min(slot_count, len(lanes))):
                    submit(next(waiting), slot)
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        lane, slot = pending.pop(future)
                        item, note, error = future.result()
                        report(item, note, error)
                        state = "done" if error is None else "incomplete" if isinstance(error, CleanupDeferred) else "failed"
                        slots[slot] = (state, item.label, slots[slot][2], time.monotonic())
                        remaining = lane[1:]
                        if isinstance(error, CleanupDeferred) and not isinstance(error, CleanupPartial):
                            for skipped in remaining:
                                report(skipped, None, CleanupDeferred("skipped after an incomplete cleanup in this group"))
                            remaining = []
                        if remaining:
                            submit(remaining, slot)
                        else:
                            following = next(waiting, None)
                            if following is not None:
                                submit(following, slot)
            finally:
                cancelled.set()
    # Successful paths are opt-in. Failures remain visible in normal output.
    rows = []
    if not verbose:
        counts: dict[str, int] = defaultdict(int)
        for item, _, error in outcomes.values():
            if error is None:
                counts[item.group] += 1
        if counts:
            rows.append((Text("✓", style="green"), "cleaned", Text(" · ".join(f"{g} {n}" for g, n in counts.items()))))
    for item in items:
        if id(item) not in outcomes:
            continue
        _, note, error = outcomes[id(item)]
        if error is None and not verbose:
            continue
        deferred = isinstance(error, CleanupDeferred)
        mark = Text("✓" if error is None else "!" if deferred else "✗",
                    style="green" if error is None else "yellow" if deferred else "red")
        what = Text(tilde(item.project) + "  " + item.label if item.is_project else item.label)
        if verbose and item.path is not None and not item.is_project:
            what.append("  " + tilde(item.path), style="dim")
        detail = str(error) if error is not None else note
        if detail:
            what.append("  " + detail, style="dim")
        rows.append((mark, item.group, what))
    if rows:
        console.print(_grid(rows, styles=("", "bold")))
    for key, (_, note, error) in outcomes.items():
        result.outcomes[key] = (("cleaned", note) if error is None else
                                ("incomplete" if isinstance(error, CleanupDeferred) else "failed", str(error)))
    return result


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------


@dataclass
class Choice:
    item: Item
    on: bool
    default: bool                    # False for targets the plan leaves alone until asked


_KEYS = {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left", b"OA": "up", b"OB": "down", b"OC": "right",
         b"OD": "left", b"[5~": "pgup", b"[6~": "pgdn", b"[H": "home", b"[F": "end", b"[1~": "home", b"[4~": "end"}
_KEY_ALIASES = {"k": "up", "j": "down", "l": "right", "h": "left", "\t": "right", " ": "space", "\r": "enter",
                "\n": "enter", "g": "home", "G": "end", "\x04": "q", "\x02": "pgup", "\x06": "pgdn"}


def read_key(fd: int, timeout: float = 0.5) -> str:
    """One keypress as a name; "" when nothing arrived, so the caller can redraw after a resize."""
    import select
    if not select.select([fd], [], [], timeout)[0]:
        return ""
    ch = os.read(fd, 1)
    if not ch:
        return "q"                   # stdin closed under us
    if ch == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        return _KEYS.get(os.read(fd, 8), "")
    key = ch.decode("utf-8", "ignore")
    return _KEY_ALIASES.get(key, key)


class Picker:
    """What the review screen holds: groups that fold open into targets, and which of them are ticked."""

    HELP = "↑↓ move · space toggle · →← open/close · a aggressive tier · n none · d reset · enter clean · q quit"

    def __init__(self, to_clean: list[Item], extra: list[Item], *, threshold: str, notes: list[str] = (),
                 headline: str = "", can_size: bool = False):
        self.choices = [Choice(i, True, True) for i in to_clean] + [Choice(i, False, False) for i in extra]
        self.threshold, self.notes, self.headline, self.can_size = threshold, list(notes), headline, can_size
        self.open: set[str] = set()
        self.cursor = self.top = 0
        self.confirming = self.done = self.want_sizes = False
        self.message = ""
        self.result: list[Item] | None = None
        rows = health_rows()         # read once: a keypress must not shell out for memory counters
        self.health, self.health_lines = _grid(rows, styles=("dim",), wrap_last=False), len(rows)
        self.regroup()

    def regroup(self) -> None:
        groups: dict[str, list[Choice]] = defaultdict(list)
        for choice in self.choices:
            groups[choice.item.group].append(choice)
        for members in groups.values():
            members.sort(key=lambda c: (not c.default, -(c.item.size if c.item.size is not None else -1)))
        order = {g: n for n, g in enumerate(ALL_GROUPS)}
        self.groups = dict(sorted(groups.items(), key=lambda kv: (
            -sum(c.item.size or 0 for c in kv[1] if c.default and not c.item.ram), order.get(kv[0], 99))))

    def rows(self) -> list[tuple[str, Choice | None]]:
        out: list[tuple[str, Choice | None]] = []
        for group, members in self.groups.items():
            out.append((group, None))
            if group in self.open:
                out += [(group, c) for c in members]
        return out

    def selected(self) -> list[Item]:
        chosen = [c.item for c in self.choices if c.on]
        # A full cache clear makes the prune of that same cache redundant.
        cleared = {i.group for i in chosen if i.tier == AGGRESSIVE}
        return [i for i in chosen if not (i.shadowed and i.group in cleared)]

    def key(self, key: str) -> None:
        if not key:
            return
        self.message = ""
        if self.confirming:
            self.confirming = False
            if key == "y":
                self.result, self.done = self.selected(), True
            return
        rows = self.rows()
        group, choice = rows[self.cursor]
        members = self.groups[group]
        if key in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "home": -len(rows), "end": len(rows)}[key]
            self.cursor = min(len(rows) - 1, max(0, self.cursor + step))
        elif key == "space":
            if choice is not None:
                choice.on = not choice.on
            elif any(c.on for c in members):
                for c in members:
                    c.on = False
            else:  # back on means back to the plan; a group that is all opt-in has nothing else to offer
                for c in members:
                    c.on = c.default or not any(m.default for m in members)
        elif key == "right" and choice is None:
            self.open.add(group)
        elif key == "left":
            self.open.discard(group)
            self.cursor = self.rows().index((group, None))
        elif key == "a":
            extras = [c for c in self.choices if not c.default]
            turn_on = not all(c.on for c in extras)
            for c in extras:
                c.on = turn_on
            if not extras:
                self.message = "nothing more to add: the aggressive tier is already in the plan"
        elif key == "n":
            for c in self.choices:
                c.on = False
        elif key == "d":
            for c in self.choices:
                c.on = c.default
        elif key == "s" and self.can_size:
            self.want_sizes = True
        elif key == "enter":
            if self.selected():
                self.confirming = True
            else:
                self.message = "nothing is selected"
        elif key in ("q", "esc"):
            self.done = True

    def _tally(self, choices: list[Choice]) -> Text:
        if choices and all(c.item.ram for c in choices):
            return Text(human_size(sum(c.item.size or 0 for c in choices if c.on)) + " RAM", style="cyan")
        disk = [c.item for c in choices if c.on and not c.item.ram]
        if not any(i.size is not None for i in disk):
            return Text("—", style="dim")
        known = sum(i.size or 0 for i in disk)
        return Text(human_size(known) + ("+" if any(i.size is None for i in disk) else ""), style=size_color(known))

    def render(self, width: int, height: int) -> Group:
        rows = self.rows()
        roomy = height >= 20         # a short terminal keeps the list and drops the context around it
        notes = self.notes[:3] if roomy else []
        help_line = self.HELP + (" · s measure sizes" if self.can_size else "")
        fixed = (6 + (len(help_line) - 1) // max(20, width) + (self.health_lines + 1 if roomy else 0)
                 + (len(notes) + 1 if notes else 0))
        room = max(3, height - fixed)
        self.top = min(max(0, len(rows) - room), max(0, min(self.top, self.cursor)))
        if self.cursor >= self.top + room:
            self.top = self.cursor - room + 1
        grid = Table.grid(padding=(0, 1), expand=True)
        for kw in (dict(width=1), dict(width=1), dict(width=3), dict(min_width=max(len(g) for g in self.groups)),
                   dict(justify="right", min_width=5), dict(justify="right", min_width=9)):
            grid.add_column(no_wrap=True, **kw)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        now = time.time()
        for n, (group, choice) in enumerate(rows[self.top:self.top + room], start=self.top):
            here = n == self.cursor
            pointer = Text("❯" if here else "", style="cyan bold")
            if choice is None:
                members = self.groups[group]
                on = sum(c.on for c in members)
                box = Text("[x]", style="green") if on == len(members) else Text(
                    "[-]", style="yellow") if on else Text("[ ]", style="dim")
                labels = list(dict.fromkeys(c.item.label for c in members if c.default or c.on)) or \
                    list(dict.fromkeys(c.item.label for c in members))
                detail = Text(", ".join(labels[:4]) + (f", +{len(labels) - 4} more" if len(labels) > 4 else ""), style="dim")
                off = sum(1 for c in members if not c.default and not c.on)
                if off:
                    detail.append(f"  +{off} aggressive, off", style="red dim")
                count = Text(f"{on}/{len(members)}" if on != len(members) else str(on), style="dim")
                grid.add_row(pointer, Text("▾" if group in self.open else "▸", style="dim"), box,
                             Text(group, style="bold"), count, self._tally(members), detail,
                             style="on grey19" if here else "")
                continue
            item = choice.item
            box = Text("[x]", style="green") if choice.on else Text("[ ]", style="dim")
            age = Text("")
            if item.mtime is not None and item.mtime != float("inf"):
                days = (now - item.mtime) / 86400
                age = Text(f"{days:.0f}d" if days >= 10 else f"{days:.1f}d" if days >= 1 else f"{days * 24:.0f}h",
                           style=age_color(days))
            size = Text(human_size(item.size) + (" RAM" if item.ram else ""),
                        style="cyan" if item.ram else size_color(item.size)) if item.size is not None else Text("—", style="dim")
            what = Text()
            if item.is_project:
                what.append(tilde(item.project)).append("  " + item.label, style="dim")
            else:
                what.append(item.label)
                if item.path:
                    what.append("  " + tilde(item.path), style="dim")
                if item.note:
                    what.append("  " + item.note, style="dim italic")
            if item.tier == AGGRESSIVE:
                what.append("  aggressive", style="red dim")
            grid.add_row(pointer, "", box, "", age, size, what, style="on grey19" if here else "")
        chosen = self.selected()
        below = len(rows) - self.top - room
        scroll = " · ".join(([f"↑ {self.top} above"] if self.top else []) + ([f"↓ {below} below"] if below > 0 else []))
        total = Text.assemble(("Selected ", "bold"), (f"{len(chosen)} of {len(self.choices)} targets", ""),
                              ("  frees ", "dim"), self._tally(self.choices))
        ram = sum(i.size or 0 for i in chosen if i.ram)
        if ram:
            total.append(f" and {human_size(ram)} RAM", style="cyan")
        if self.confirming:
            foot = Text.assemble((f"Clean these {len(chosen)} targets? ", "bold"), ("y", "bold green"),
                                 (" cleans · any other key goes back", "dim"))
        elif self.message:
            foot = Text(self.message, style="yellow")
        else:
            foot = Text(help_line, style="dim")
        parts: list = [Text.assemble(("dev-cleaner", "bold"), ("  review", "dim")),
                       *([self.health, Text("")] if roomy else []),
                       _section("Will clean", f"project builds idle over {self.threshold}"
                                + (f" · {self.headline}" if self.headline else "")),
                       Padding(grid, (0, 0, 0, 1)), Text("  " + scroll, style="dim"), total]
        if notes:
            parts.append(Text(""))
            parts += [Text("  " + n.replace("\n", " "), style="dim", no_wrap=True, overflow="ellipsis") for n in notes]
        return Group(*parts, Text(""), foot)


def can_interact() -> bool:
    """A person is at the keyboard and the terminal can redraw."""
    if not (sys.stdin.isatty() and console.is_terminal) or os.environ.get("TERM") in (None, "", "dumb"):
        return False
    try:
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def pick(picker: Picker, measure: Callable[[], None] | None = None) -> list[Item] | None:
    """Run the review screen until the user confirms a selection (returned) or quits (None)."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(console=console, screen=True, auto_refresh=False) as live:
            drawn = None
            while not picker.done:
                if drawn != console.size:    # first pass, a keypress or a resize: an idle screen is left alone
                    drawn = console.size
                    live.update(picker.render(*drawn), refresh=True)
                key = read_key(fd)
                if key:
                    picker.key(key)
                    drawn = None
                if picker.want_sizes and measure is not None:
                    live.stop()
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                    measure()
                    picker.want_sizes, picker.can_size = False, False
                    picker.regroup()
                    picker.cursor = picker.top = 0
                    tty.setcbreak(fd)
                    live.start()
                    drawn = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return picker.result


def item_json(item: Item, now: float) -> dict:
    return {"group": item.group, "label": item.label,
            "path": str(item.path) if item.path is not None else None,
            "project": str(item.project) if item.project is not None else None,
            "bytes": item.size, "frees": "memory" if item.ram else "disk", "tier": item.tier,
            "idle_days": round((now - item.mtime) / 86400, 2)
            if item.mtime is not None and item.mtime != float("inf") else None,
            "note": item.note or None}


# ---------------------------------------------------------------------------
# Init wizard
# ---------------------------------------------------------------------------


def init_wizard(path: Path, scan_root: Path | None = None) -> bool:
    """Scan ~, cluster projects by top-level dir, let the user pick + tune."""
    if path.exists():
        if not Confirm.ask(f"  [yellow]{path} exists. overwrite?[/]", default=False):
            return False

    home = (scan_root or HOME).expanduser().resolve()
    console.print(f"  [dim]scanning {home} for projects with build dirs (this may take a moment)…[/]")

    groups: dict[str, list[Path]] = defaultdict(list)
    progress = Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]found {task.fields[count]} projects"),
        TextColumn("[dim]{task.fields[detail]}"),
        console=console, transient=True,
    )
    seen: set[Path] = set()
    with progress:
        task = progress.add_task("", total=None, count=0, detail="")
        for _group, project, _build, _bd in find_projects(home):
            if project in seen:
                continue
            seen.add(project)
            try:
                rel = project.relative_to(home)
                top = rel.parts[0] if rel.parts else "."
            except ValueError:
                top = "(other)"
            groups[top].append(project)
            progress.update(task, count=len(seen), detail=tilde(project))

    total = len(seen)
    if total == 0:
        console.print("  [yellow]no projects found.[/] writing defaults.\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_config(DEFAULTS["roots"], DEFAULTS["days"]))
        console.print(f"  [green]✓[/] wrote [bold]{path}[/]\n")
        return True

    console.print(f"  [green]✓[/] found [bold]{total}[/] project(s) "
                  f"across [bold]{len(groups)}[/] cluster(s) under {tilde(home)}\n")

    # One root per top-level dir, not the tightest common ancestor: a narrow root
    # stops matching as soon as a sibling project (new clone, worktree) appears.
    suggestions: list[tuple[str, int, list[Path]]] = []
    for top, projects in groups.items():
        root = home if top in (".", "(other)") else home / top
        suggestions.append((tilde(root), len(projects), projects))
    suggestions.sort(key=lambda x: -x[1])

    selected: list[str] = []
    console.print("  [bold]include as scan root?[/] [dim](grouped by top-level dir)[/]")
    for label, count, projects in suggestions:
        more = f" [dim](+{count - 1} more)[/]" if count > 1 else ""
        console.print(f"    [dim]example:[/] {tilde(projects[0])}{more}")
        if Confirm.ask(f"    [cyan]{label}[/] [dim]({count} project(s))[/]", default=True):
            selected.append(label)
    if not selected:
        console.print("  [yellow]nothing selected; using defaults.[/]")
        selected = DEFAULTS["roots"]

    extra = Prompt.ask(
        "\n  [bold]extra dir names to ignore[/] [dim](comma-separated, blank to skip)[/]",
        default="", show_default=False,
    ).strip()
    ignores = [s.strip() for s in extra.split(",") if s.strip()] if extra else []
    days = FloatPrompt.ask("  [bold]min idle age in days[/]", default=DEFAULTS["days"])
    aggressive: bool | str = Confirm.ask(
        "  [bold]always wipe dependency download caches?[/] [dim](no = only when the disk is tight)[/]",
        default=False) or "auto"
    skip_raw = Prompt.ask(
        f"  [bold]groups to never run[/] [dim](comma-separated from: {', '.join(ALL_GROUPS)}; blank for none)[/]",
        default="", show_default=False,
    ).strip()
    skip = [s.strip() for s in skip_raw.split(",") if s.strip() in ALL_GROUPS] if skip_raw else []
    volumes = False
    if shutil.which("docker") and "docker" not in skip:
        volumes = Confirm.ask(
            "  [bold]prune unused anonymous docker volumes?[/] [dim](volumes hold data; usually no)[/]",
            default=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(selected, days, ignores, aggressive, skip, volumes))
    console.print(f"\n  [green]✓[/] wrote config to [bold]{path}[/]")
    console.print(f"  [dim]roots: {selected}[/]")
    if ignores:
        console.print(f"  [dim]ignore: {ignores}[/]")
    if skip:
        console.print(f"  [dim]skip: {skip}[/]")
    console.print("  [dim]edit it, or re-run [bold]dev-cleaner --init[/] any time.[/]\n")
    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _parse_groups(raw: str | None, flag: str) -> set[str]:
    if not raw:
        return set()
    names = {s.strip() for s in raw.split(",") if s.strip()}
    bad = names - set(ALL_GROUPS)
    if bad:
        console.print(f"[red bold]error:[/] unknown group(s) for {flag}: {', '.join(sorted(bad))}")
        console.print(f"[dim]valid: {', '.join(ALL_GROUPS)}[/]")
        raise SystemExit(2)
    return names


def main() -> int:
    p = argparse.ArgumentParser(
        prog="dev-cleaner",
        description="Sweep stale build dirs and dev caches so your disk stops filling up.",
        epilog="Groups: " + ", ".join(ALL_GROUPS) + ". See --list for what each does.",
    )
    p.add_argument("--root", action="append", default=None,
                   help="Where to search for projects (repeatable). Overrides config.")
    p.add_argument("--below-free-gib", type=int, metavar="GIB",
                   help="Only run cleanup when the home disk has fewer than this many GiB free")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--thorough", action="store_true", help="Search all projects without the normal 10-second scan budget")
    scope.add_argument("--caches-only", action="store_true", help="Skip project discovery; clean global caches and configured temp entries")
    p.add_argument("-v", "--verbose", action="store_true", help="Show every candidate path and cleanup result")
    age = p.add_mutually_exclusive_group()
    age.add_argument("--days", type=float, default=None, help="Project idle age in days (default 1)")
    age.add_argument("--hours", type=float, default=None, help="Project idle age in hours")
    p.add_argument("-n", "--dry-run", action="store_true", help="List what would be cleaned, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="Clean without asking; the way to run unattended")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--plain", action="store_true",
                     help="No review screen: print the plan and ask a yes/no question")
    out.add_argument("--json", action="store_true",
                     help="Print the plan (and with --yes, each result) as one JSON object on stdout; never asks")
    tier = p.add_mutually_exclusive_group()
    tier.add_argument("-a", "--aggressive", action="store_true",
                      help="Also clean dependency download caches and all unused docker images "
                           "(automatic once under 15%% of the disk is free)")
    tier.add_argument("--gentle", action="store_true",
                      help="Keep dependency caches and the configured idle age even when the disk is tight")
    p.add_argument("--only", metavar="GROUPS", help="Comma-separated groups to run (e.g. rust,docker)")
    p.add_argument("--skip", metavar="GROUPS", help="Comma-separated groups to skip")
    reports = p.add_mutually_exclusive_group()
    reports.add_argument("--audit", action="store_true",
                   help="Show the biggest dirs on disk and how much of each would be freed; changes nothing")
    reports.add_argument("--docker-audit", action="store_true",
                         help="Show Docker disk usage, container memory and per-builder caches; changes nothing")
    reports.add_argument("--mem", action="store_true",
                         help="Show memory by program, individual PIDs and silent terminal sessions; changes nothing")
    reports.add_argument("--cpu", action="store_true",
                         help="Sample which programs burn CPU now and which have for hours; changes nothing")
    p.add_argument("--cpu-sample", type=float, metavar="SECONDS", help="CPU sampling window (default 2; requires --cpu)")
    p.add_argument("--top", "--mem-top", dest="mem_top", type=int, metavar="N",
                   help="Rows per table (default 10; with --mem or --cpu)")
    p.add_argument("--filter", "--mem-filter", dest="mem_filter", metavar="TEXT",
                   help="Match PID, executable or command (with --mem or --cpu)")
    p.add_argument("--tree", "--mem-tree", dest="mem_tree", action="store_true",
                   help="Group by process family (with --mem or --cpu)")
    p.add_argument("--full", "--mem-full", dest="mem_full", action="store_true",
                   help="Show full paths and commands, wrapping to fit (with --mem or --cpu)")
    p.add_argument("--mem-sort", choices=("memory", "cpu", "growth"), help="Sort memory tables (default memory)")
    p.add_argument("--mem-watch", type=float, metavar="SECONDS", help="Refresh memory and show growth (requires --mem)")
    p.add_argument("--dedupe", action="store_true",
                   help="Make identical files across --root dirs (or [dedupe] paths) share disk blocks; removes nothing")
    p.add_argument("--list", action="store_true", help="List cleaner groups and exit")
    p.add_argument("--size-jobs", type=int, default=None, help="Parallel sizing workers")
    p.add_argument("--sizes", action="store_true", help="Measure directory and Docker cache sizes (slower; skipped by default)")
    p.add_argument("--clean-jobs", type=int, default=None, help="Parallel cleanup workers")
    p.add_argument("--command-timeout", type=float, metavar="SECONDS",
                   help="Time limit per cleanup command (default 60); incomplete commands are not retried")
    p.add_argument("--config", type=Path, default=None, help=f"Config path (default: {config_path()})")
    p.add_argument("--init", action="store_true", help="Interactive setup (or --init --yes for static defaults)")
    p.add_argument("--show-config", action="store_true", help="Print resolved config and exit")
    p.add_argument("--completion", choices=["bash", "zsh", "fish"], help="Print a shell completion script and exit")
    args = p.parse_args()
    if args.below_free_gib is not None:
        if args.below_free_gib <= 0:
            p.error("--below-free-gib requires a positive integer")
        if any((args.audit, args.docker_audit, args.mem, args.cpu, args.dedupe, args.init,
                args.show_config, args.list, args.completion)):
            p.error("--below-free-gib applies only to cleanup runs")
    if args.audit and args.caches_only:
        p.error("--audit cannot be combined with --caches-only")
    if args.json and any((args.audit, args.docker_audit, args.mem, args.cpu, args.dedupe, args.init, args.list,
                          args.show_config, args.completion)):
        p.error("--json describes a cleanup run; it cannot be combined with a report or setup flag")
    if not (args.mem or args.cpu) and any((args.mem_top is not None, args.mem_filter is not None,
                                           args.mem_tree, args.mem_full)):
        p.error("--top, --filter, --tree and --full require --mem or --cpu")
    if not args.mem and (args.mem_sort is not None or args.mem_watch is not None):
        p.error("--mem-sort and --mem-watch require --mem")
    if args.cpu_sample is not None and (not args.cpu or not math.isfinite(args.cpu_sample) or args.cpu_sample <= 0):
        p.error("--cpu-sample requires --cpu and a finite positive number of seconds")
    if args.mem_top is not None and args.mem_top < 1:
        p.error("--top must be a positive integer")
    if args.mem_watch is not None and (not math.isfinite(args.mem_watch) or args.mem_watch <= 0):
        p.error("--mem-watch must be a finite positive number of seconds")
    if args.mem_sort == "growth" and args.mem_watch is None:
        p.error("--mem-sort growth requires --mem-watch")

    if args.completion:
        sys.stdout.write(COMPLETIONS[args.completion])
        return 0

    if args.list:
        console.print(render_groups_table())
        return 0

    if args.json:
        console.file = sys.stderr    # stdout carries the JSON object and nothing else
    if console.width > MAX_WIDTH and not args.mem_full:
        console.width = MAX_WIDTH
    console.print(banner())
    if args.cpu:
        ok = run_cpu(sample=args.cpu_sample or 2.0, top=args.mem_top or 10, pattern=args.mem_filter or "",
                     by_tree=args.mem_tree, full=args.mem_full)
        console.print("\n[dim]cpu — no changes made.[/]\n")
        return 0 if ok else 1
    if args.docker_audit:
        ok = run_docker_audit()
        console.print("\n[dim]docker audit — no changes made.[/]\n")
        return 0 if ok else 1

    cfg_path = args.config or config_path()

    if args.init:
        if args.yes or not sys.stdin.isatty():
            if write_default_config(cfg_path, force=args.yes):
                console.print(f"  [green]✓[/] wrote starter config to [bold]{cfg_path}[/]\n")
            else:
                console.print("[yellow]aborted.[/]")
            return 0
        if init_wizard(cfg_path):
            return 0
        console.print("[yellow]aborted.[/]")
        return 0

    # Config: new path, else the old rust-clean config (roots/ignore/days carry over).
    legacy = False
    if not cfg_path.exists() and args.config is None and legacy_config_path().exists():
        cfg_path, legacy = legacy_config_path(), True
    cfg = {**DEFAULTS, **load_config(cfg_path)}
    docker_cfg = {**DEFAULTS["docker"], **(cfg.get("docker") or {})}

    roots = [Path(r).expanduser() for r in (args.root or cfg["roots"])]
    extra_skip = set(cfg.get("ignore") or [])
    size_jobs = args.size_jobs if args.size_jobs is not None else cfg["size_jobs"]
    clean_jobs = args.clean_jobs if args.clean_jobs is not None else cfg["clean_jobs"]
    command_timeout = args.command_timeout if args.command_timeout is not None else cfg["command_timeout"]
    if (not isinstance(command_timeout, (int, float)) or not math.isfinite(command_timeout)
            or command_timeout <= 0):
        p.error("--command-timeout / command_timeout must be a finite positive number of seconds")
    scan_seconds = cfg["scan_seconds"]
    if isinstance(scan_seconds, bool) or not isinstance(scan_seconds, (int, float)) or not 0 < scan_seconds < math.inf:
        p.error("scan_seconds must be a finite positive number of seconds")
    setting = cfg.get("aggressive")
    if setting != "auto" and not isinstance(setting, bool):
        p.error('aggressive must be true, false or "auto"')
    volumes = docker_cfg.get("volumes")
    docker_volumes: bool | str = "all" if volumes == "all" else bool(volumes)

    only = _parse_groups(args.only, "--only")
    skip = _parse_groups(args.skip, "--skip") | set(cfg.get("skip") or [])
    groups = (only or set(ALL_GROUPS)) - skip
    if not groups and not args.mem:
        console.print("[red bold]error:[/] every group is skipped; nothing to do")
        return 2

    if args.hours is not None:
        age_hours, threshold = args.hours, f"{args.hours:g}h"
    elif args.days is not None:
        age_hours, threshold = args.days * 24, f"{args.days:g}d"
    else:
        age_hours, threshold = cfg["days"] * 24, f"{cfg['days']:g}d"

    # Tier: explicit flags and true/false in the config win; "auto" follows the disk.
    level, free, total = disk_pressure()
    auto = setting == "auto" and not args.aggressive and not args.gentle and level != "ok"
    aggressive = not args.gentle and (args.aggressive or setting is True or auto)
    mode_note = ""
    if auto:
        mode_note = (f"under {(CRITICAL_FREE if level == 'critical' else TIGHT_FREE):.0%} free: "
                     "dependency caches and unused images included")
        if level == "critical" and args.hours is None and args.days is None and age_hours > CRITICAL_HOURS:
            age_hours, threshold = CRITICAL_HOURS, f"{CRITICAL_HOURS:g}h"
            mode_note += f", idle age cut to {threshold}"

    now = time.time()
    try:
        if not isinstance(cfg.get("tmp"), dict):
            raise ValueError("tmp must be a [tmp] table")
        tmp_cfg = {**DEFAULTS["tmp"], **cfg["tmp"]}
        sweeps = extra_sweeps(cfg.get("extra", []), now)
        tmp_cutoff = now - sweep_days(tmp_cfg["days"]) * 86400 if "days" in tmp_cfg else None
        if not isinstance(tmp_cfg["claude_scratch"], bool):
            raise ValueError("tmp.claude_scratch must be true or false")
    except ValueError as error:
        p.error(str(error))

    procs_cfg = cfg.get("procs") if isinstance(cfg.get("procs"), dict) else {}
    orphan_dirs = [Path(str(d)) for d in procs_cfg.get("orphan_dirs") or []]
    idle_session_days = procs_cfg.get("idle_session_days", 1.0)
    if idle_session_days is False:
        idle_session_days = None
    elif (idle_session_days is True or not isinstance(idle_session_days, (int, float))
          or not math.isfinite(idle_session_days) or idle_session_days <= 0):
        p.error("procs.idle_session_days must be a finite positive number of days, or false to leave sessions alone")
    stop_spinning = procs_cfg.get("spinning", True)
    if not isinstance(stop_spinning, bool):
        p.error("procs.spinning must be true or false")

    if args.show_config:
        src = ("legacy rust-clean config" if legacy else "config file") if cfg_path.exists() else "built-in defaults"
        console.print(f"  [dim]source:[/] {src} [dim]({cfg_path})[/]")
        console.print(f"  [bold]roots[/]       = {[tilde(r) for r in roots]}")
        console.print(f"  [bold]scan[/]        = {'thorough' if args.thorough or args.audit else 'caches only' if args.caches_only else f'{scan_seconds:g}-second budget'}")
        console.print(f"  [bold]ignore[/]      = {sorted(extra_skip)}")
        console.print(f"  [bold]days[/]        = {age_hours / 24:g}")
        console.print(f"  [bold]aggressive[/]  = {setting!r} [dim](this run: {aggressive}; disk {level})[/]")
        console.print(f"  [bold]groups[/]      = {[g for g in ALL_GROUPS if g in groups]}")
        console.print(f"  [bold]docker.volumes[/] = {docker_volumes}")
        console.print(f"  [bold]tmp.days[/]    = {tmp_cfg.get('days', age_hours / 24):g}")
        console.print(f"  [bold]tmp.claude_scratch[/] = {tmp_cfg['claude_scratch']}")
        for s in sweeps:
            days = (now - s.cutoff) / 86400 if s.cutoff is not None else age_hours / 24
            console.print(f"  [bold]extra[/]       = {escape(tilde(s.path))}; days={days:g}; "
                          f"include={escape(repr(list(s.include)))}; exclude={escape(repr(list(s.exclude)))}"
                          + (f"; keep={s.keep}" if s.keep else ""))
        console.print(f"  [bold]procs.orphan_dirs[/] = {[str(d) for d in orphan_dirs]}")
        console.print(f"  [bold]procs.idle_session_days[/] = "
                      f"{'off' if idle_session_days is None else format(idle_session_days, 'g')}")
        console.print(f"  [bold]procs.spinning[/] = {stop_spinning}")
        console.print(f"  [bold]size_jobs[/]   = {size_jobs}")
        console.print(f"  [bold]clean_jobs[/]  = {clean_jobs}\n")
        console.print(f"  [bold]command_timeout[/] = {command_timeout:g}s")
        return 0

    if legacy:
        console.print(f"  [dim]using legacy config {tilde(cfg_path)} — run[/] [bold]dev-cleaner --init[/] "
                      f"[dim]to migrate[/]")

    ctx = Context(
        roots=roots, cutoff=now - age_hours * 3600, extra_skip=extra_skip,
        groups=groups, aggressive=aggressive,
        docker_volumes=docker_volumes, size_jobs=size_jobs, sweeps=sweeps, orphan_dirs=orphan_dirs,
        idle_session_days=idle_session_days, stop_spinning=stop_spinning,
        measure_active=args.audit,
        command_timeout=command_timeout,
        measure_sizes=args.sizes or args.audit,
        tmp_cutoff=tmp_cutoff, tmp_claude_scratch=tmp_cfg["claude_scratch"],
        scan_seconds=None if args.thorough or args.audit else scan_seconds,
        caches_only=args.caches_only,
    )
    if args.dedupe:
        dedupe_cfg = {**DEFAULTS["dedupe"], **(cfg.get("dedupe") if isinstance(cfg.get("dedupe"), dict) else {})}
        min_kb = dedupe_cfg["min_kb"]
        if isinstance(min_kb, bool) or not isinstance(min_kb, (int, float)) or min_kb < 0:
            p.error("dedupe.min_kb must be a nonnegative number")
        ok = run_dedupe([Path(r) for r in (args.root or dedupe_cfg["paths"])], int(min_kb * 1024), size_jobs,
                        dry_run=args.dry_run, yes=args.yes)
        return 0 if ok else 1
    if args.mem:
        ok = run_mem(ctx, top=args.mem_top or 10, pattern=args.mem_filter or "", by_tree=args.mem_tree,
                     sort=args.mem_sort or "memory", watch=args.mem_watch, full=args.mem_full)
        console.print("\n[dim]mem — no changes made.[/]\n")
        return 0 if ok else 1
    skipped_reason = None
    if args.below_free_gib is not None:
        if total <= 0:
            skipped_reason = "disk_unmeasurable"
        elif free >= args.below_free_gib * 1024**3:
            skipped_reason = "enough_free_space"
    items = [] if skipped_reason else scan_all(ctx)
    to_clean, active, extra = partition(items, ctx)
    notes = render_notes(active, extra, threshold, ctx.warnings)

    if args.audit:
        run_audit(ctx, to_clean, active, extra)
        for n in notes:
            console.print()
            console.print(n)
        console.print("\n[dim]audit — no changes made.[/]\n")
        return 0

    def emit(dry_run: bool, result: CleanupReport | None = None, freed: int = 0, seconds: float = 0.0) -> None:
        targets = []
        for item in to_clean:
            status, detail = (result.outcomes.get(id(item), ("skipped", None)) if result else ("planned", None))
            targets.append({**item_json(item, now), "status": status, "detail": detail})
        report_free, report_total = free, total
        if not skipped_reason:
            usage = shutil.disk_usage(HOME)
            report_free, report_total = usage.free, usage.total
        print(json.dumps({
            "dry_run": dry_run, "tier": AGGRESSIVE if aggressive else SAFE, "disk_pressure": level,
            "idle_hours": age_hours, "disk": {"free_bytes": report_free, "total_bytes": report_total},
            **({"skipped_reason": skipped_reason} if skipped_reason else {}),
            "targets": targets,
            "left_alone": [{**item_json(i, now), "reason": "needs --aggressive"} for i in extra]
                          + [{**item_json(i, now), "reason": "recently active"} for i in active],
            "warnings": ctx.warnings,
            "result": None if result is None else {
                "cleaned": result.cleaned, "incomplete": result.deferred, "failed": result.failed,
                "freed_bytes": freed, "seconds": round(seconds, 1)},
        }, indent=2))

    interactive = not (args.yes or args.dry_run or args.plain or args.json) and can_interact()
    if not to_clean and not (interactive and extra):
        if skipped_reason == "disk_unmeasurable":
            console.print("[red]Could not measure free space on the home disk. Cleanup skipped.[/]")
        elif skipped_reason:
            console.print(f"Cleanup skipped: {human_size(free)} free, threshold {args.below_free_gib} GiB.")
        else:
            console.print("\n[bold]No cleanup candidates found in this scan.[/]")
        for n in notes:
            console.print()
            console.print(n)
        console.print()
        if args.json:
            emit(dry_run=args.dry_run or not args.yes)
        return 1 if skipped_reason == "disk_unmeasurable" else 0

    if interactive:
        def measure() -> None:
            with make_progress() as progress:
                size_items(to_clean + extra, size_jobs, progress)
            ctx.measure_sizes = True

        headline = ("disk is " + level + ", aggressive tier included" if auto else "aggressive tier included" if aggressive else "")
        chosen = pick(Picker(to_clean, extra, threshold=threshold, notes=ctx.warnings, headline=headline,
                             can_size=not ctx.measure_sizes), measure)
        if chosen is None:
            console.print("\n[yellow]Nothing cleaned.[/]\n")
            return 0
        to_clean = chosen
        aggressive = aggressive or any(i.tier == AGGRESSIVE for i in to_clean)
        console.print()
        console.print((render_table if args.verbose else render_summary)(to_clean, threshold, aggressive))
    else:
        console.print()
        if mode_note:
            console.print(Text.assemble(("Disk is " + level, "red bold" if level == "critical" else "yellow"),
                                        ("  " + mode_note + " · --gentle opts out", "dim")))
            console.print()
        console.print((render_table if args.verbose else render_summary)(to_clean, threshold, aggressive))
        for n in notes:
            console.print()
            console.print(n)
        more = ([] if args.verbose else ["--verbose lists every path"]) + (
            [] if ctx.measure_sizes else ["--sizes measures before cleaning"]) + ["--audit shows where the disk went"]
        console.print()
        console.print(Text("  " + " · ".join(more), style="dim"))

        if args.dry_run or (args.json and not args.yes):
            console.print("\n[dim]dry-run — no changes made.[/]\n")
            if args.json:
                emit(dry_run=True)
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                console.print("\n[yellow]Nothing cleaned:[/] no terminal to confirm on. "
                              "Pass [bold]--yes[/] to clean or [bold]--dry-run[/] to preview.\n")
                return 2
            if not Confirm.ask(f"\n[bold]Clean these {len(to_clean)} targets?[/]", default=False):
                console.print("[yellow]aborted.[/]")
                return 0

    free_before = shutil.disk_usage(HOME).free
    started = time.monotonic()
    console.print()
    result = clean_all(to_clean, clean_jobs, roots, verbose=args.verbose)
    usage = shutil.disk_usage(HOME)
    freed = max(0, usage.free - free_before)
    if args.json:
        emit(dry_run=False, result=result, freed=freed, seconds=time.monotonic() - started)

    est = sum(i.size or 0 for i in to_clean if not i.ram)
    head = Text.assemble(("Freed ", "bold"), (human_size(freed), "bold green"),
                         (f" (estimated {human_size(est)})" if ctx.measure_sizes else "", "dim"),
                         (f"  in {_elapsed(time.monotonic() - started)} · {result.cleaned} of {len(to_clean)} cleaned", "dim"))
    if result.deferred:
        head.append(f" · {result.deferred} incomplete", style="yellow")
    if result.failed:
        head.append(f" · {result.failed} failed", style="red")
    console.print()
    console.print(head)
    console.print(health_grid(disk_was=free_before))
    if usage.total and usage.free < 0.2 * usage.total:
        console.print(Text.assemble(
            ("\n  ", ""), (f"{human_size(usage.used)} is still in use.", "yellow"),
            (" dev-cleaner --audit shows where it sits and what no cleaner covers.", "dim")))
    return 1 if result.failed or result.deferred else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted.[/]")
        raise SystemExit(130)
