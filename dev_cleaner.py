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
import json
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

HOME = Path.home()
IS_MAC = sys.platform == "darwin"
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
MAC_CACHES = HOME / "Library" / "Caches"

SAFE = "safe"
AGGRESSIVE = "aggressive"

# ---------------------------------------------------------------------------
# Scanning rules
# ---------------------------------------------------------------------------

# Never descend into these while looking for projects. Dot-dirs are *not*
# blanket-skipped: some (e.g. ~/.pilot) legitimately contain projects.
SKIP_DIRS = {
    # macOS
    ".Trash", "Library", "Pictures", "Music", "Movies",
    # Linux
    "snap", ".var",
    # VCS / build tooling
    ".git", ".hg", ".svn",
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

GROUP_EMOJI = {
    "rust": "🦀", "node": "📦", "python": "🐍", "jvm": "☕", "swift": "🕊️", "elixir": "💧",
    "zig": "⚡", "ocaml": "🐫", "haskell": "λ", "dart": "🎯", "cmake": "🔧", "dotnet": "🟣",
    "terraform": "🏗️", "php": "🐘", "go": "🐹", "docker": "🐳", "brew": "🍺", "xcode": "🍎",
    "tools": "🧰", "tmp": "🗑️", "extra": "📁", "procs": "🧟",
}

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
    "aggressive": False,
    "skip": [],
    "size_jobs": 16,
    "clean_jobs": 4,
    "docker": {"volumes": False},
    "extra": [],
    "procs": {"orphan_dirs": []},
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "dev-cleaner" / "config.toml"


def legacy_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "rust-clean" / "config.toml"


def render_config(roots: list[str], days: float, ignore: list[str] | None = None,
                  aggressive: bool = False, skip: list[str] | None = None,
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

# Also clean dependency download caches (cargo registry, go modcache, npm/pip,
# gradle, ...) and all unused docker images. Same as --aggressive.
aggressive = {str(aggressive).lower()}

# Cleaner groups to never run. See `dev-cleaner --list`.
skip = {skip!r}

# Parallel workers when sizing dirs (I/O bound).
size_jobs = 16

# Parallel delete workers (disk bound; keep modest).
clean_jobs = 4

[docker]
# Prune unused volumes too. Off by default: volumes hold data.
# true = anonymous volumes only, "all" = named ones as well.
volumes = {json.dumps(docker_volumes)}

[procs]
# Orphaned processes (parent gone, idle past `days`) are stopped when their
# executable lives in a temp dir. List more dirs whose executables are only
# ever meant to run under a parent, e.g. a tool's plugin dir.
orphan_dirs = []

# Disposable dirs of your own: children idle longer than `days` are removed
# (`days` is optional and defaults to the global one above).
# [[extra]]
# path = "~/Library/Caches/mytool"
# days = 3
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
    opts="--root --days --hours --dry-run --yes --aggressive --only --skip --audit --mem --list --size-jobs --clean-jobs --config --init --show-config --completion -n -y -a -h --help"
    case "$prev" in
        --root|--config) COMPREPLY=( $(compgen -f -- "$cur") ); return ;;
        --completion) COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") ); return ;;
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
        '--only[Only run these groups]:groups:_values -s , group {_GROUPS_WS}' \
        '--skip[Skip these groups]:groups:_values -s , group {_GROUPS_WS}' \
        '--audit[Show the biggest dirs and how much of each would be freed]' \
        '--mem[Show which programs hold the memory and how many are orphaned]' \
        '--list[List cleaner groups and exit]' \
        '--size-jobs[Parallel sizing workers]:N:' \
        '--clean-jobs[Parallel delete workers]:N:' \
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
complete -c dev-cleaner -l only -d "Only run these groups" -x -a "{_GROUPS_WS}"
complete -c dev-cleaner -l skip -d "Skip these groups" -x -a "{_GROUPS_WS}"
complete -c dev-cleaner -l audit -d "Show the biggest dirs and how much of each would be freed"
complete -c dev-cleaner -l mem -d "Show which programs hold the memory and how many are orphaned"
complete -c dev-cleaner -l list -d "List cleaner groups and exit"
complete -c dev-cleaner -l size-jobs -d "Parallel sizing workers" -x
complete -c dev-cleaner -l clean-jobs -d "Parallel delete workers" -x
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
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def dir_size(path: Path) -> int:
    """Disk usage in bytes. Uses `du -sk` (fast, counts blocks) when available."""
    if shutil.which("du"):
        r = run(["du", "-sk", str(path)], timeout=600)
        if r is not None and r.stdout.strip():
            try:
                return int(r.stdout.split()[0]) * 1024
            except ValueError:
                pass
    total = 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
    return total


def same_fs(path: Path, dev: int) -> bool:
    """False across a mount point. ~/OrbStack, for one, is the docker VM's live filesystem."""
    try:
        return os.lstat(path).st_dev == dev
    except OSError:
        return False


def paths_size(paths: list[Path], workers: int) -> dict[Path, int]:
    """Disk usage of many paths at once: a few batched `du` calls instead of one each."""
    if not shutil.which("du"):
        return {p: dir_size(p) for p in paths}

    def batch(chunk: list[Path]) -> dict[Path, int]:
        out: dict[Path, int] = {}
        r = run(["du", "-sk", *map(str, chunk)], timeout=1800)
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
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
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


def clear_dir(path: Path) -> None:
    """Remove a directory's contents but keep the directory itself."""
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            force_rmtree(child)
        else:
            child.unlink()


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
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Project scanner
# ---------------------------------------------------------------------------


def find_projects(root: Path, extra_skip: set[str] | None = None):
    """Yield (group, project_dir, build_dir, BuildDir) for every build dir under root."""
    root = root.expanduser().resolve()
    skip = SKIP_DIRS | ALWAYS_PRUNE | (extra_skip or set())
    try:
        dev = os.stat(root).st_dev
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        fset = set(filenames)
        here = Path(dirpath)
        matched: set[str] = set()
        for rule in RULES:
            if not rule.matches(fset):
                continue
            for bd in rule.dirs:
                if bd.name not in dirnames or bd.name in matched:
                    continue
                p = here / bd.name
                if p.is_symlink() or not p.is_dir():
                    continue
                if bd.require and not (p / bd.require).exists():
                    continue
                matched.add(bd.name)
                yield rule.group, here, p, bd
        dirnames[:] = [d for d in dirnames if d not in skip and d not in matched and same_fs(here / d, dev)]


def project_activity(project: Path) -> float:
    """Latest mtime across a project's sources, ignoring build dirs and VCS.

    Proxy for "when was this last worked on". Build-dir mtimes are useless:
    cargo rewrites target/.rustc_info.json on every invocation, node tooling
    touches node_modules/.cache, etc.
    """
    prune = ALL_BUILD_DIR_NAMES | ALWAYS_PRUNE | {".git", ".hg", ".svn"}
    latest = 0.0
    try:
        latest = project.stat().st_mtime
    except OSError:
        pass
    dev = os.stat(project).st_dev if project.exists() else 0
    for dirpath, dirnames, filenames in os.walk(project, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in prune and same_fs(Path(dirpath) / d, dev)]
        for f in filenames:
            try:
                m = os.lstat(os.path.join(dirpath, f)).st_mtime
                if m > latest:
                    latest = m
            except OSError:
                continue
    return latest


def scan_projects(ctx: Context, progress: Progress) -> list[Item]:
    found: list[tuple[str, Path, Path, BuildDir]] = []
    seen: set[Path] = set()
    for root in ctx.roots:
        if not root.exists():
            console.print(f"[dim]skip[/] [yellow]{root}[/] (does not exist)")
            continue
        task = progress.add_task(f"scanning {tilde(root)}", detail="", total=None)
        for group, project, build, bd in find_projects(root, ctx.extra_skip):
            progress.update(task, detail=tilde(project))
            if build in seen:
                continue
            seen.add(build)
            if group in ctx.groups:
                found.append((group, project, build, bd))
        progress.remove_task(task)

    if not found:
        return []

    # A nested project (monorepo package, workspace member) inherits activity
    # from its outermost enclosing project: if you're working anywhere in the
    # repo, nothing in it gets swept.
    projects = sorted({p for _, p, _, _ in found}, key=lambda p: len(p.parts))
    tops: list[Path] = []
    top_of: dict[Path, Path] = {}
    for p in projects:
        parent = next((t for t in tops if p == t or t in p.parents), None)
        if parent is None:
            tops.append(p)
            parent = p
        top_of[p] = parent

    task = progress.add_task(f"measuring activity in {len(tops)} project(s)",
                             detail="", total=len(tops))
    activity: dict[Path, float] = {}
    with ThreadPoolExecutor(max_workers=ctx.size_jobs) as pool:
        futs = {pool.submit(project_activity, t): t for t in tops}
        for fut in as_completed(futs):
            t = futs[fut]
            activity[t] = fut.result()
            progress.update(task, detail=tilde(t))
            progress.advance(task)
    progress.remove_task(task)

    items = []
    for group, project, build, bd in found:
        items.append(Item(
            group=group, label=bd.name + "/", path=build, tier=bd.tier,
            mtime=activity[top_of[project]], note=bd.note, mode="rmtree", project=project,
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
        if not any(path.iterdir()):
            return None
    except OSError:
        return None
    return Item(group=group, label=label, path=path, tier=tier, note=note, mode=mode)


def _cmd_action(cmd: list[str], timeout: float = 1800, cwd: Path | None = None,
                summary: Callable[[str], str] | None = None) -> Callable[[], str | None]:
    def act() -> str | None:
        r = run(cmd, timeout=timeout, cwd=cwd)
        if r is None:
            raise RuntimeError(f"{cmd[0]} failed to run")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"exit {r.returncode}")
        out = r.stdout.strip()
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
            it.mode, it.action = "action", _cmd_action([go, "clean", "-cache"])
        items.append(it)
    it = _dir_item("go", "go module cache", gomod, AGGRESSIVE, "re-downloaded")
    if it:
        if go:
            it.mode, it.action = "action", _cmd_action([go, "clean", "-modcache"])
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
                action=_cmd_action([pnpm, "store", "prune"], summary=lambda o: o.splitlines()[-1] if o else None),
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
                action=_cmd_action([uv, "cache", "prune"], summary=lambda o: o.splitlines()[-1] if o else None),
            ))
        it = _dir_item("python", "uv cache", uv_dir, AGGRESSIVE, "wheels + sources; re-downloaded")
        if it:
            if uv:
                it.mode, it.action = "action", _cmd_action([uv, "cache", "clean"])
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


def _container_builders(docker: str) -> list[str]:
    """buildx builders running in their own container: `docker system prune` never touches their cache."""
    r = run([docker, "buildx", "ls", "--format", "{{json .}}"], timeout=30)
    names: list[str] = []
    if r is None or r.returncode != 0:
        return names
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        endpoints = [n.get("Endpoint") or "" for n in d.get("Nodes") or []]
        remote = endpoints and all(e.startswith(("ssh://", "tcp://")) for e in endpoints)
        if d.get("Driver") == "docker-container" and not remote and d.get("Name") not in names:
            names.append(d["Name"])
    return names


def _buildx_reclaimable(docker: str, builder: str) -> int | None:
    r = run([docker, "buildx", "du", "--builder", builder], timeout=120)
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"^Reclaimable:\s*(\S+)", r.stdout, re.M)
    return parse_size(m.group(1)) if m else None


def scan_docker(ctx: Context) -> list[Item]:
    docker = shutil.which("docker")
    if not docker:
        return []
    held = ", ".join(f"{human_size(sz)} in the {name} VM disk" for name, sz in
                     ((name, file_disk_size(p)) for name, p in docker_vm_disks()) if sz >= 10 * 1024**3)
    info = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=15)
    if info is None or info.returncode != 0:
        ctx.warnings.append("docker: daemon not reachable — skipped"
                            + (f" ({held}; start it and re-run)" if held else ""))
        return []
    if held:
        ctx.warnings.append(f"docker: {held} — the file shrinks as images, volumes and build cache go")

    df = _docker_df(docker)
    if not df:
        ctx.warnings.append("docker: `docker system df` failed — some sizes unknown")

    def reclaimable(t: str) -> int | None:
        return parse_size(df[t]["Reclaimable"]) if t in df else None

    dangling = None
    r = run([docker, "images", "-f", "dangling=true", "--format", "{{.Size}}"], timeout=30)
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
                              action=_cmd_action([docker, *args], summary=summarize)))

    items: list[Item] = []
    containers = reclaimable("Containers")
    prune("docker system prune", None if None in (containers, dangling) else containers + dangling, SAFE,
          "stopped containers, dangling images, unused networks", "system", "prune", "-f")

    cache = reclaimable("Build Cache")
    if cache is None:
        ctxname = run([docker, "context", "show"], timeout=15)
        if ctxname and ctxname.returncode == 0 and ctxname.stdout.strip():
            cache = _buildx_reclaimable(docker, ctxname.stdout.strip())
    prune("docker builder prune -a", cache, SAFE, "build cache; rebuilt on demand",
          "builder", "prune", "-a", "-f")
    for name in _container_builders(docker):
        prune(f"docker buildx prune ({name})", _buildx_reclaimable(docker, name), SAFE,
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


def scan_brew(ctx: Context) -> list[Item]:
    brew = shutil.which("brew")
    if not brew:
        return []
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
            action=_cmd_action([brew, "cleanup", "-s", "--prune=all"], timeout=900,
                               summary=lambda o: f"removed {sum(1 for l in o.splitlines() if l.startswith('Removing'))} item(s)"),
        ))
    r = run([brew, "autoremove", "-n"], timeout=120)
    if r and r.returncode == 0 and "Would autoremove" in r.stdout:
        formulae = [l.strip() for l in r.stdout.splitlines()[1:] if l.strip()]
        items.append(Item(
            group="brew", label="brew autoremove", size=None, tier=AGGRESSIVE,
            note=f"{len(formulae)} formula(e) no longer needed by anything: "
                 + ", ".join(formulae[:6]) + (" …" if len(formulae) > 6 else ""),
            mode="action", action=_cmd_action([brew, "autoremove"], timeout=600),
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
            action=_cmd_action([xcrun, "simctl", "delete", "unavailable"], timeout=300),
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


def idle_since(path: Path, depth: int = 2, budget: int = 2000) -> float | None:
    """Newest mtime in `path` down to `depth` levels; None when it must be left alone.

    Left alone: other users' files, symlinks, sockets/fifos/devices and any dir
    holding one (a live program's IPC). A dir's own mtime only moves when a
    direct child comes or goes, hence the shallow look inside.
    """
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


def tmp_sweeps() -> list[Sweep]:
    out: list[Sweep] = []
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
            out.append(Sweep("tmp", "idle temp files", base, (base,)))
    # Claude Code: <tmp>/claude-<uid>/<project>/<session>/{scratchpad,tasks}.
    for base in bases:
        claude = base / f"claude-{os.getuid()}"
        if claude.is_dir():
            projects = tuple(p for p in claude.iterdir() if p.name.startswith("-") and p.is_dir())
            out.append(Sweep("tmp", "idle Claude Code scratch", claude, (claude, *projects),
                             note="session scratchpads and task output"))
    return out


def _sweep_action(entries: list[Path], cutoff: float) -> Callable[[], str | None]:
    def remove(p: Path) -> bool | None:
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
        with ThreadPoolExecutor(max_workers=DEFAULTS["clean_jobs"]) as pool:
            results = list(pool.map(remove, entries))
        removed, failed, skipped = results.count(True), results.count(False), results.count(None)
        if failed and not removed:
            raise RuntimeError(f"could not remove any of {failed} entries")
        return (f"removed {removed}" + (f", {failed} failed" if failed else "")
                + (f", {skipped} back in use" if skipped else ""))
    return act


def scan_sweeps(sweeps: list[Sweep], ctx: Context) -> list[Item]:
    # A dir that is itself swept is never a candidate of its parent.
    reserved = {c for s in [*tmp_sweeps(), *ctx.sweeps] for c in s.containers}
    items = []
    for sw in sweeps:
        cutoff = sw.cutoff if sw.cutoff is not None else ctx.cutoff
        stale: list[Path] = []
        for c in sw.containers:
            try:
                names, dev = os.listdir(c), os.stat(c).st_dev
            except OSError:
                continue
            for name in names:
                p = c / name
                if p in reserved or name in TMP_KEEP_NAMES or name.startswith(TMP_KEEP_PREFIXES):
                    continue
                if not same_fs(p, dev):
                    continue
                m = idle_since(p)
                if m is not None and m < cutoff:
                    stale.append(p)
        if not stale:
            continue
        sizes = paths_size(stale, ctx.size_jobs)
        top = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
        note = f"{len(stale)} entries" + (
            "; biggest: " + ", ".join(f"{p.name[:28]} {human_size(n)}" for p, n in top) if top else "")
        items.append(Item(
            group=sw.group, label=sw.label, path=sw.path, size=sum(sizes.values()), tier=SAFE,
            note=f"{sw.note}; {note}" if sw.note else note, mode="action",
            action=_sweep_action(stale, cutoff),
        ))
    return items


def scan_tmp(ctx: Context) -> list[Item]:
    return scan_sweeps(tmp_sweeps(), ctx)


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
    cpu: float                       # % right now
    mem: int                         # bytes: footprint incl. compressed/swapped where known, else RSS
    exe: str

    @property
    def name(self) -> str:
        return os.path.basename(self.exe)


def parse_etime(s: str) -> float:
    """ps etime, [[dd-]hh:]mm:ss → seconds."""
    days, _, rest = s.rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _footprints() -> dict[int, int]:
    """pid → memory footprint. RSS collapses once a machine swaps, which is exactly when this matters."""
    if not IS_MAC:
        return {}
    r = run(["top", "-l", "1", "-n", "100000", "-stats", "pid,mem"], timeout=60)
    out: dict[int, int] = {}
    for line in (r.stdout.splitlines() if r and r.returncode == 0 else ()):
        m = re.fullmatch(r"\s*(\d+)\s+(\d+)([BKMG])[+-]?\s*", line)
        if m:
            out[int(m.group(1))] = int(m.group(2)) * 1024 ** "BKMG".index(m.group(3))
    return out


def list_procs() -> list[Proc]:
    r = run(["ps", "-A", "-o", "pid=,ppid=,uid=,etime=,%cpu=,rss=,comm="], timeout=30)
    if r is None or r.returncode != 0:
        return []
    foot = _footprints()
    procs = []
    for line in r.stdout.splitlines():
        f = line.split(None, 6)
        if len(f) < 7:
            continue
        try:
            pid, exe = int(f[0]), f[6].strip()
            if not IS_MAC:  # Linux `comm` is a bare, truncated name
                try:
                    exe = os.readlink(f"/proc/{pid}/exe")
                except OSError:
                    pass
            procs.append(Proc(pid, int(f[1]), int(f[2]), parse_etime(f[3]), float(f[4].replace(",", ".")),
                              foot.get(pid, int(f[5]) * 1024), exe))
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


def _stop_action(targets: list[Proc], roots: tuple[str, ...], min_age: float) -> Callable[[], str | None]:
    def act() -> str | None:
        # Pids get recycled and orphans get adopted: only signal what still qualifies.
        want = {(p.pid, p.exe) for p in targets}
        live = [p for p in find_orphans(list_procs(), roots, min_age) if (p.pid, p.exe) in want]

        def alive(p: Proc) -> bool:
            try:
                os.kill(p.pid, 0)
                return True
            except OSError:
                return False

        def send(procs: list[Proc], sig: int) -> None:
            for p in procs:
                try:
                    os.kill(p.pid, sig)
                except OSError:
                    pass

        send(live, signal.SIGTERM)  # lets a supervisor tear its own children down
        deadline = time.time() + 8
        while time.time() < deadline and any(alive(p) for p in live):
            time.sleep(0.25)
        stuck = [p for p in live if alive(p)]
        send(stuck, signal.SIGKILL)
        return f"stopped {len(live)}" + (f" ({len(stuck)} forced)" if stuck else "")
    return act


def scan_procs(ctx: Context) -> list[Item]:
    roots, min_age = orphan_roots(ctx), time.time() - ctx.cutoff
    orphans = find_orphans(list_procs(), roots, min_age)
    if not orphans:
        return []
    return [Item(
        group="procs", label="orphaned processes", size=sum(p.mem for p in orphans), tier=SAFE, ram=True,
        note=f"{len(orphans)} idle, parent gone: {_name_counts(orphans)}",
        mode="action", action=_stop_action(orphans, roots, min_age),
    )]


def run_mem(ctx: Context) -> None:
    """Read-only: what holds the memory, and how much of it is leaked."""
    procs = list_procs()
    if not procs:
        console.print("[yellow]mem: could not list processes[/]")
        return
    if IS_MAC:
        swap = run(["sysctl", "-n", "vm.swapusage"], timeout=10)
        m = re.search(r"total = ([\d.]+M)\s+used = ([\d.]+M)", swap.stdout) if swap else None
        if m:
            total, used = (parse_size(x + "B", base=1024) or 0 for x in m.groups())
            style = "red bold" if total and used / total > 0.5 else "green"
            console.print(Text.from_markup(
                f"  [dim]swap:[/] [{style}]{human_size(used)}[/] [dim]of {human_size(total)} in use"
                + (" — the machine is out of RAM; the kernel burns CPU compressing and paging[/]"
                   if style != "green" else "[/]")))
    by_name: dict[str, list[Proc]] = defaultdict(list)
    for p in procs:
        by_name[p.name].append(p)
    rows = sorted(by_name.items(), key=lambda kv: -sum(p.mem for p in kv[1]))

    table = Table(title="[bold]🧠  where the memory went[/] [dim](by program)[/]", box=ROUNDED,
                  border_style="magenta", header_style="bold magenta", title_justify="left", expand=True)
    table.add_column("memory", justify="right", min_width=9)
    table.add_column("procs", justify="right")
    table.add_column("idle >1d", justify="right")
    table.add_column("oldest", justify="right")
    table.add_column("program", overflow="fold")
    for name, ps_ in rows[:20]:
        mem = sum(p.mem for p in ps_)
        if mem < 256 * 1024**2:
            break
        old = sum(1 for p in ps_ if p.age >= 86400 and p.cpu <= 1.0)
        days = max(p.age for p in ps_) / 86400
        table.add_row(Text(human_size(mem), style=size_color(mem)), str(len(ps_)),
                      Text(str(old), style="yellow" if old >= 5 else "dim") if old else Text("—", style="dim"),
                      Text(f"{days:.1f}d", style=age_color(days) if len(ps_) > 1 else "dim"), name)
    console.print()
    console.print(table)
    orphans = find_orphans(procs, orphan_roots(ctx), time.time() - ctx.cutoff)
    if orphans:
        console.print(Text.from_markup(
            f"[yellow]![/] [bold]{len(orphans)}[/] [dim]orphaned process(es) holding[/] "
            f"[bold]{human_size(sum(p.mem for p in orphans))}[/][dim]: {escape(_name_counts(orphans))}. "
            f"stop them with[/] [bold]dev-cleaner --only procs[/]"))
    console.print("[dim]many old idle copies of one program (agent sessions, language servers) are sessions "
                  "nobody closed: close them where they were opened.[/]")


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
    "tools": ("ccache, kubectl cache", "playwright/puppeteer/cypress browsers, electron, deno, trivy, helm, bazel, JetBrains caches"),
    "tmp": ("entries of /tmp and $TMPDIR, and Claude Code session scratch, idle past the threshold", ""),
    "extra": ("idle children of the dirs listed under [[extra]] in the config", ""),
    "procs": ("stops idle orphaned processes (parent gone) running from a temp dir or a [procs] orphan_dirs dir", ""),
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]{task.description}"),
        TextColumn("[dim]{task.fields[detail]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def size_items(items: list[Item], workers: int, progress: Progress) -> None:
    # Shadowed prune-style items (pnpm/uv prune) stay "?" — the whole dir size would overstate them.
    todo = [i for i in items if i.size is None and i.path is not None and not i.shadowed]
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
    with make_progress() as progress:
        if ctx.groups & set(PROJECT_GROUPS):
            items += scan_projects(ctx, progress)
        for name, fn in GLOBAL_SCANNERS.items():
            if name not in ctx.groups:
                continue
            task = progress.add_task(f"checking {name}", detail="", total=None)
            try:
                items += fn(ctx)
            except Exception as e:  # one broken tool must not sink the run
                ctx.warnings.append(f"{name}: scan failed ({e}) — skipped")
            progress.remove_task(task)
        items += scan_known_caches(ctx)
        size_items(items, ctx.size_jobs, progress)

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


def disk_line(path: Path = HOME) -> Text:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return Text("")
    pct = u.used / u.total * 100 if u.total else 0
    color = "red bold" if u.free < 20 * 1024**3 or pct > 90 else "yellow" if pct > 80 else "green"
    return Text.from_markup(
        f"[dim]disk:[/] [{color}]{human_size(u.free)} free[/] [dim]of {human_size(u.total)} "
        f"({pct:.0f}% used)[/]"
    )


def banner() -> Panel:
    title = Text("🧹  dev-cleaner", style="bold magenta")
    subtitle = Text("\nsweep stale build dirs & dev caches so your disk stops filling up", style="dim")
    return Panel(Group(title, subtitle, disk_line()), box=ROUNDED, border_style="magenta",
                 padding=(0, 2))


def render_table(items: list[Item], threshold: str, aggressive: bool) -> Table:
    title = "🧹  reclaimable"
    if aggressive:
        title += " [red](aggressive)[/]"
    table = Table(
        title=f"[bold]{title}[/] [dim](project dirs idle >{threshold})[/]",
        box=ROUNDED, border_style="magenta", header_style="bold magenta",
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
            f"{GROUP_EMOJI.get(g, '')} [bold]{g}[/]" + (f"  [dim]{human_size(totals[g])}[/]" if totals[g] else "")))
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
    unknown = sum(1 for i in items if i.size is None)
    table.add_section()
    table.add_row(
        "", Text(human_size(total) + ("+" if unknown else ""), style="bold green"), "",
        Text(f"{len(items)} item(s) — reclaimable", style="bold green"),
    )
    return table


def render_notes(active: list[Item], extra: list[Item], threshold: str, warnings: list[str]) -> list[Text]:
    out = []
    if active:
        total = sum(i.size or 0 for i in active)
        projects = {i.project for i in active}
        out.append(Text.from_markup(
            f"[dim]skipped[/] [bold]{len(active)}[/] [dim]build dir(s) in {len(projects)} active project(s) "
            f"holding[/] [{size_color(total)}]{human_size(total)}[/] "
            f"[dim](edited within {threshold}). include them with[/] [bold]--hours N[/]"
        ))
    if extra:
        total = sum(i.size or 0 for i in extra)
        top = ", ".join(f"{i.label} {human_size(i.size)}" for i in extra[:4] if i.size)
        more = f", +{len(extra) - 4} more" if len(extra) > 4 else ""
        out.append(Text.from_markup(
            f"[dim]another[/] [{size_color(total)}]{human_size(total)}[/] [dim]in {len(extra)} dependency "
            f"cache(s) / unused image(s) with[/] [bold]--aggressive[/][dim]: {top}{more}[/]"
        ))
    for w in warnings:
        out.append(Text.from_markup(f"[yellow]![/] [dim]{escape(w)}[/]"))
    return out


def render_groups_table() -> Table:
    t = Table(box=ROUNDED, border_style="magenta", header_style="bold magenta", expand=True)
    t.add_column("group", style="bold")
    t.add_column("default", overflow="fold")
    t.add_column("--aggressive adds", overflow="fold", style="dim")
    for g in ALL_GROUPS:
        safe, aggr = GROUP_HELP.get(g, ("", ""))
        t.add_row(f"{GROUP_EMOJI.get(g, '')} {g}", safe or "—", aggr or "—")
    return t


# ---------------------------------------------------------------------------
# Audit: where the disk went, and how much of it we cover
# ---------------------------------------------------------------------------

AUDIT_MIN = 1024**3
AUDIT_DEPTH = 3
AUDIT_ROWS = 30


def audit_rows(du_out: str, root: Path, min_bytes: int = AUDIT_MIN) -> list[tuple[Path, int]]:
    """Reduce `du -k -d N` output to the dirs worth showing: big ones, as deep as the bulk goes."""
    size: dict[Path, int] = {}
    for line in du_out.splitlines():
        kb, _, name = line.partition("\t")
        if kb.isdigit():
            size[Path(name)] = int(kb) * 1024
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
        if it.path is None or not it.size:
            continue
        if it.path == d or d in it.path.parents:
            n += it.size
        elif it.path in d.parents:
            return total
    return min(n, total)


def run_audit(ctx: Context, to_clean: list[Item], active: list[Item], extra: list[Item]) -> None:
    home = HOME.resolve()
    tmp = [s.path for s in tmp_sweeps()]
    swept = [d for d in tmp if home not in (d, *d.parents) and not any(o in d.parents for o in tmp)]
    rows: list[tuple[Path, int]] = []
    with make_progress() as progress:
        task = progress.add_task(f"measuring {tilde(home)} (this walks everything; minutes on a full disk)",
                                 detail="", total=None)
        r = run(["du", "-x", "-k", "-d", str(AUDIT_DEPTH), str(home)], timeout=3600)
        if r is not None:  # du exits 1 on the dirs macOS won't let it read; the rest is still good
            rows += audit_rows(r.stdout, home)
        progress.update(task, description="measuring temp dirs")
        rows += [(d, n) for d, n in paths_size(swept, ctx.size_jobs).items() if n >= AUDIT_MIN]
    if not rows:
        console.print("[yellow]audit: could not measure anything (is `du` installed?)[/]")
        return
    rows.sort(key=lambda dn: -dn[1])
    vm = {p: name for name, p in docker_vm_disks()}

    table = Table(
        title="[bold]🔎  where the disk went[/] [dim](dirs over 1 GB, and what dev-cleaner frees in each)[/]",
        box=ROUNDED, border_style="magenta", header_style="bold magenta", title_justify="left", expand=True,
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
# Cleaning
# ---------------------------------------------------------------------------


def _delete(item: Item, roots: list[Path]) -> tuple[Item, str | None, str | None]:
    """→ (item, result note, error)."""
    try:
        if item.mode == "action" and item.action:
            return item, item.action(), None
        assert item.path is not None
        assert_deletable(item.path, roots)
        if item.mode == "clear":
            clear_dir(item.path)
        else:
            force_rmtree(item.path)
        return item, None, None
    except Exception as e:
        return item, None, str(e).strip().splitlines()[-1] if str(e).strip() else repr(e)


def clean_all(items: list[Item], workers: int, roots: list[Path]) -> int:
    failed = 0
    progress = Progress(
        SpinnerColumn(style="green"),
        TextColumn("[bold]cleaning[/] [dim]{task.fields[detail]}[/]"),
        BarColumn(complete_style="green", finished_style="bold green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    fs_items = [i for i in items if i.mode != "action"]
    cmd_items = [i for i in items if i.mode == "action"]

    def report(item: Item, note: str | None, err: str | None) -> None:
        nonlocal failed
        label = f"{escape(tilde(item.project))} [dim]{escape(item.label)}[/]" if item.is_project else \
            f"{escape(item.label)}" + (f" [dim]{escape(tilde(item.path))}[/]" if item.path else "")
        if err is None:
            console.print(f"  [green]✓[/] {label}" + (f"  [dim]{note}[/]" if note else ""))
        else:
            failed += 1
            console.print(f"  [red]✗[/] {label} [dim]— {escape(err)}[/]")

    with progress:
        task = progress.add_task("", total=len(items), detail="")
        # Filesystem deletes in parallel (disk-bound), external commands one at a time.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_delete, i, roots) for i in fs_items]
            for fut in as_completed(futs):
                report(*fut.result())
                progress.advance(task)
        for it in cmd_items:
            progress.update(task, detail=it.label)
            report(*_delete(it, roots))
            progress.advance(task)
    return failed


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
    aggressive = Confirm.ask(
        "  [bold]aggressive by default?[/] [dim](also wipe dependency download caches)[/]", default=False)
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
    age = p.add_mutually_exclusive_group()
    age.add_argument("--days", type=float, default=None, help="Project idle age in days (default 1)")
    age.add_argument("--hours", type=float, default=None, help="Project idle age in hours")
    p.add_argument("-n", "--dry-run", action="store_true", help="List what would be cleaned, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    p.add_argument("-a", "--aggressive", action="store_true",
                   help="Also clean dependency download caches and all unused docker images")
    p.add_argument("--only", metavar="GROUPS", help="Comma-separated groups to run (e.g. rust,docker)")
    p.add_argument("--skip", metavar="GROUPS", help="Comma-separated groups to skip")
    p.add_argument("--audit", action="store_true",
                   help="Show the biggest dirs on disk and how much of each would be freed; changes nothing")
    p.add_argument("--mem", action="store_true",
                   help="Show which programs hold the memory and how many are orphaned; changes nothing")
    p.add_argument("--list", action="store_true", help="List cleaner groups and exit")
    p.add_argument("--size-jobs", type=int, default=None, help="Parallel sizing workers")
    p.add_argument("--clean-jobs", type=int, default=None, help="Parallel delete workers")
    p.add_argument("--config", type=Path, default=None, help=f"Config path (default: {config_path()})")
    p.add_argument("--init", action="store_true", help="Interactive setup (or --init --yes for static defaults)")
    p.add_argument("--show-config", action="store_true", help="Print resolved config and exit")
    p.add_argument("--completion", choices=["bash", "zsh", "fish"], help="Print a shell completion script and exit")
    args = p.parse_args()

    if args.completion:
        sys.stdout.write(COMPLETIONS[args.completion])
        return 0

    if args.list:
        console.print(render_groups_table())
        return 0

    console.print(banner())

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
    aggressive = args.aggressive or bool(cfg.get("aggressive"))
    volumes = docker_cfg.get("volumes")
    docker_volumes: bool | str = "all" if volumes == "all" else bool(volumes)

    only = _parse_groups(args.only, "--only")
    skip = _parse_groups(args.skip, "--skip") | set(cfg.get("skip") or [])
    groups = (only or set(ALL_GROUPS)) - skip
    if not groups:
        console.print("[red bold]error:[/] every group is skipped; nothing to do")
        return 2

    if args.hours is not None:
        age_hours, threshold = args.hours, f"{args.hours:g}h"
    elif args.days is not None:
        age_hours, threshold = args.days * 24, f"{args.days:g}d"
    else:
        age_hours, threshold = cfg["days"] * 24, f"{cfg['days']:g}d"

    now = time.time()
    sweeps = []
    for e in cfg.get("extra") or []:
        if not isinstance(e, dict) or not e.get("path"):
            console.print(f"[yellow]warning:[/] ignoring \\[\\[extra]] entry without a path: {escape(repr(e))}")
            continue
        d = Path(str(e["path"])).expanduser()
        sweeps.append(Sweep("extra", "idle entries", d, (d,),
                            cutoff=now - float(e["days"]) * 86400 if "days" in e else None))

    procs_cfg = cfg.get("procs") if isinstance(cfg.get("procs"), dict) else {}
    orphan_dirs = [Path(str(d)) for d in procs_cfg.get("orphan_dirs") or []]

    if args.show_config:
        src = ("legacy rust-clean config" if legacy else "config file") if cfg_path.exists() else "built-in defaults"
        console.print(f"  [dim]source:[/] {src} [dim]({cfg_path})[/]")
        console.print(f"  [bold]roots[/]       = {[tilde(r) for r in roots]}")
        console.print(f"  [bold]ignore[/]      = {sorted(extra_skip)}")
        console.print(f"  [bold]days[/]        = {age_hours / 24:g}")
        console.print(f"  [bold]aggressive[/]  = {aggressive}")
        console.print(f"  [bold]groups[/]      = {[g for g in ALL_GROUPS if g in groups]}")
        console.print(f"  [bold]docker.volumes[/] = {docker_volumes}")
        console.print(f"  [bold]extra[/]       = {[tilde(s.path) for s in sweeps]}")
        console.print(f"  [bold]procs.orphan_dirs[/] = {[str(d) for d in orphan_dirs]}")
        console.print(f"  [bold]size_jobs[/]   = {size_jobs}")
        console.print(f"  [bold]clean_jobs[/]  = {clean_jobs}\n")
        return 0

    if legacy:
        console.print(f"  [dim]using legacy config {tilde(cfg_path)} — run[/] [bold]dev-cleaner --init[/] "
                      f"[dim]to migrate[/]")

    ctx = Context(
        roots=roots, cutoff=now - age_hours * 3600, extra_skip=extra_skip,
        groups=groups, aggressive=aggressive,
        docker_volumes=docker_volumes, size_jobs=size_jobs, sweeps=sweeps, orphan_dirs=orphan_dirs,
    )
    if args.mem:
        run_mem(ctx)
        console.print("\n[dim]mem — no changes made.[/]\n")
        return 0
    items = scan_all(ctx)
    to_clean, active, extra = partition(items, ctx)
    notes = render_notes(active, extra, threshold, ctx.warnings)

    if args.audit:
        run_audit(ctx, to_clean, active, extra)
        for n in notes:
            console.print(n)
        console.print("\n[dim]audit — no changes made.[/]\n")
        return 0

    if not to_clean:
        console.print("\n[bold green]✨ nothing to clean — you're tidy![/]" if not (active or extra)
                      else "\n[bold yellow]nothing to clean at this level.[/]")
        for n in notes:
            console.print(n)
        console.print()
        return 0

    console.print()
    console.print(render_table(to_clean, threshold, aggressive))
    for n in notes:
        console.print(n)

    if args.dry_run:
        console.print("\n[dim]dry-run — no changes made.[/]\n")
        return 0

    if not args.yes and not Confirm.ask("\n[bold]proceed?[/]", default=False):
        console.print("[yellow]aborted.[/]")
        return 0

    free_before = shutil.disk_usage(HOME).free
    console.print()
    failed = clean_all(to_clean, clean_jobs, roots)
    free_after = shutil.disk_usage(HOME).free
    freed = max(0, free_after - free_before)

    est = sum(i.size or 0 for i in to_clean if not i.ram)
    body = (f"[bold green]✓ cleaned {len(to_clean) - failed}[/]/[bold]{len(to_clean)}[/] item(s)"
            + (f"  ([red]{failed} failed[/])" if failed else "")
            + f"  •  freed [bold green]{human_size(freed)}[/] [dim](estimated {human_size(est)})[/]")
    console.print(Panel(Group(Text.from_markup(body), disk_line()),
                        border_style="yellow" if failed else "green", box=ROUNDED))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted.[/]")
        raise SystemExit(130)
