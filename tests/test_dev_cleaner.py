"""Unit tests for dev_cleaner. Run with:  uv run --with pytest --with rich pytest -q"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dev_cleaner as dc  # noqa: E402


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("s,base,expected", [
    ("660.4MB (100%)", 1000, 660_400_000),
    ("1.2GB", 1000, 1_200_000_000),
    ("0B", 1000, 0),
    ("6.4GB", 1024, int(6.4 * 1024**3)),
    ("12.5kB", 1000, 12_500),
    ("3.1 GiB", 1024, int(3.1 * 1024**3)),
    ("garbage", 1000, None),
])
def test_parse_size(s, base, expected):
    assert dc.parse_size(s, base=base) == expected


def test_human_size():
    assert dc.human_size(None) == "?"
    assert dc.human_size(0) == "0.0 B"
    assert dc.human_size(1536) == "1.5 KB"
    assert dc.human_size(3 * 1024**3) == "3.0 GB"


def test_tilde(monkeypatch):
    monkeypatch.setattr(dc, "HOME", Path("/Users/x"))
    assert dc.tilde("/Users/x/dev/foo") == "~/dev/foo"
    assert dc.tilde("/Users/x") == "~"
    assert dc.tilde("/Users/xy/dev") == "/Users/xy/dev"
    assert dc.tilde("/opt/thing") == "/opt/thing"


# --- project discovery -------------------------------------------------------

_real_disk_pressure = dc.disk_pressure


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """Runs must not depend on how full the machine running the tests is."""
    monkeypatch.setattr(dc, "disk_pressure", lambda path=None: ("ok", 500 * 1024**3, 1000 * 1024**3))


def mk(root: Path, *rel: str, files: dict[str, str] | None = None) -> Path:
    d = root.joinpath(*rel)
    d.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (d / name).write_text(content)
    return d


def test_find_projects_rules(tmp_path: Path):
    mk(tmp_path, "rs", files={"Cargo.toml": ""}); mk(tmp_path, "rs", "target", files={"x": "1"})
    mk(tmp_path, "js", files={"package.json": "{}"}); mk(tmp_path, "js", "node_modules", files={"x": "1"})
    mk(tmp_path, "js", ".next", files={"x": "1"})
    mk(tmp_path, "py", files={"pyproject.toml": ""}); mk(tmp_path, "py", ".venv", files={"x": "1"})
    # cmake build/ only counts with CMakeCache.txt
    mk(tmp_path, "cm-yes", files={"CMakeLists.txt": ""}); mk(tmp_path, "cm-yes", "build", files={"CMakeCache.txt": ""})
    mk(tmp_path, "cm-no", files={"CMakeLists.txt": ""}); mk(tmp_path, "cm-no", "build", files={"main.c": ""})
    # suffix markers
    mk(tmp_path, "cs", files={"App.csproj": ""}); mk(tmp_path, "cs", "obj", files={"x": "1"})
    mk(tmp_path, "tf", files={"main.tf": ""}); mk(tmp_path, "tf", ".terraform", files={"x": "1"})
    # a target/ with no Cargo.toml sibling is not a build dir
    mk(tmp_path, "random", "target", files={"x": "1"})
    # a bare venv (no project marker) must not be descended: its site-packages
    # is full of fake project markers.
    mk(tmp_path, "scratch", ".venv", "lib", "site-packages", "pkg",
       files={"pyproject.toml": "", "package.json": "{}"})
    mk(tmp_path, "scratch", ".venv", "lib", "site-packages", "pkg", "node_modules", files={"x": ""})

    found = {(g, str(b.relative_to(tmp_path)), bd.tier)
             for g, _p, b, bd in dc.find_projects(tmp_path)}
    assert found == {
        ("rust", "rs/target", dc.SAFE),
        ("node", "js/node_modules", dc.SAFE),
        ("node", "js/.next", dc.SAFE),
        ("python", "py/.venv", dc.SAFE),
        ("cmake", "cm-yes/build", dc.SAFE),
        ("dotnet", "cs/obj", dc.SAFE),
        ("terraform", "tf/.terraform", dc.AGGRESSIVE),
    }


def test_find_projects_skips_symlinked_build_dir(tmp_path: Path):
    real = mk(tmp_path, "elsewhere", files={"x": "1"})
    mk(tmp_path, "rs", files={"Cargo.toml": ""})
    os.symlink(real, tmp_path / "rs" / "target")
    assert list(dc.find_projects(tmp_path)) == []


def test_find_projects_respects_extra_skip(tmp_path: Path):
    mk(tmp_path, "archive", "rs", files={"Cargo.toml": ""}); mk(tmp_path, "archive", "rs", "target", files={"x": ""})
    mk(tmp_path, "live", "rs", files={"Cargo.toml": ""}); mk(tmp_path, "live", "rs", "target", files={"x": ""})
    found = {str(b.relative_to(tmp_path)) for _, _, b, _ in dc.find_projects(tmp_path, {"archive"})}
    assert found == {"live/rs/target"}


def test_home_level_skips_do_not_hide_nested_projects(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dc, "HOME", tmp_path)
    for rel in (("go", "pkg"), (".cache", "tool"),                       # home-level tool dirs: skipped
                ("repo", ".claude", "worktrees", "wt"), ("repo", ".cache", "env"), ("repo", "sdk", "go")):
        mk(tmp_path, *rel, files={"package.json": "{}"}); mk(tmp_path, *rel, "node_modules", files={"x": "1"})
    mk(tmp_path, "repo", ".git", "x", files={"package.json": "{}"}); mk(tmp_path, "repo", ".git", "x", "node_modules", files={"x": "1"})
    found = {b.relative_to(tmp_path.resolve()).as_posix() for _, _, b, _ in dc.find_projects(tmp_path)}
    assert found == {"repo/.claude/worktrees/wt/node_modules", "repo/.cache/env/node_modules",
                     "repo/sdk/go/node_modules"}


def test_find_projects_stops_at_mount_points(tmp_path: Path, monkeypatch):
    mk(tmp_path, "local", files={"package.json": "{}"}); mk(tmp_path, "local", "node_modules", files={"x": "1"})
    mk(tmp_path, "OrbStack", "vol", files={"package.json": "{}"})
    mk(tmp_path, "OrbStack", "vol", "node_modules", files={"x": "1"})
    real = os.lstat
    mount = str(tmp_path.resolve() / "OrbStack")

    def fake_lstat(p, *a, **kw):
        st = real(p, *a, **kw)
        if os.fspath(p) == mount:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st
    monkeypatch.setattr(dc.os, "lstat", fake_lstat)
    found = {b.relative_to(tmp_path.resolve()).as_posix() for _, _, b, _ in dc.find_projects(tmp_path)}
    assert found == {"local/node_modules"}


def test_project_activity_ignores_build_dirs(tmp_path: Path):
    proj = mk(tmp_path, "p", files={"Cargo.toml": "", "src.rs": ""})
    tgt = mk(proj, "target")
    old = time.time() - 30 * 86400
    for f in (proj, proj / "Cargo.toml", proj / "src.rs", tgt):
        os.utime(f, (old, old))
    (tgt / "fresh").write_text("1")  # cargo touches target/ constantly
    assert dc.project_activity(proj) == pytest.approx(old, abs=2)
    # editing a source file makes it active again
    (proj / "src.rs").write_text("x")
    assert dc.project_activity(proj) > old + 86400
    assert tgt.exists()


def test_nested_project_inherits_activity(tmp_path: Path):
    """A monorepo package is active if anything in the repo is."""
    repo = mk(tmp_path, "mono", files={"package.json": "{}"})
    mk(repo, "node_modules", files={"x": ""})
    pkg = mk(repo, "packages", "a", files={"package.json": "{}"})
    mk(pkg, "node_modules", files={"x": ""})
    old = time.time() - 30 * 86400
    for dirpath, dirnames, filenames in os.walk(repo):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (old, old))
    os.utime(repo, (old, old))
    # Touch a file at the repo root only.
    (repo / "README.md").write_text("fresh")

    ctx = dc.Context(roots=[tmp_path], cutoff=time.time() - 86400, extra_skip=set(),
                     groups=set(dc.ALL_GROUPS), aggressive=False, docker_volumes=False, size_jobs=2)
    with dc.make_progress() as progress:
        items = dc.scan_projects(ctx, progress)
    paths = {str(i.path.relative_to(tmp_path)) for i in items}
    assert paths == {"mono/node_modules", "mono/packages/a/node_modules"}
    assert all(i.mtime >= ctx.cutoff for i in items), "nested package must inherit root activity"
    to_clean, active, extra = dc.partition(items, ctx)
    assert to_clean == [] and len(active) == 2 and extra == []


def test_partition_tiers(tmp_path: Path):
    ctx = dc.Context(roots=[tmp_path], cutoff=time.time() - 86400, extra_skip=set(),
                     groups=set(dc.ALL_GROUPS), aggressive=False, docker_volumes=False, size_jobs=2)
    stale = time.time() - 10 * 86400
    mb = 1024**2
    items = [
        dc.Item("rust", "target/", tmp_path / "a", 10 * mb, dc.SAFE, mtime=stale, project=tmp_path),
        dc.Item("rust", "target/", tmp_path / "b", 20 * mb, dc.SAFE, mtime=time.time(), project=tmp_path),
        dc.Item("swift", ".build/", tmp_path / "e", 4096, dc.SAFE, mtime=stale, project=tmp_path),
        dc.Item("go", "go module cache", tmp_path / "c", 30 * mb, dc.AGGRESSIVE),
        dc.Item("python", "uv cache prune", tmp_path / "d", None, dc.SAFE, shadowed=True, mode="action"),
    ]
    to_clean, active, extra = dc.partition(items, ctx)
    assert [i.label for i in to_clean] == ["target/", "uv cache prune"]  # empty .build/ dropped
    assert [i.size for i in active] == [20 * mb]
    assert [i.label for i in extra] == ["go module cache"]

    ctx.aggressive = True
    to_clean, active, extra = dc.partition(items, ctx)
    assert [i.label for i in to_clean] == ["go module cache", "target/"]  # prune shadowed by clean
    assert extra == []


# --- deletion ----------------------------------------------------------------

def test_force_rmtree_readonly(tmp_path: Path):
    d = mk(tmp_path, "mod", "pkg", files={"a.go": "x"})
    os.chmod(d / "a.go", 0o444)
    os.chmod(d, 0o555)
    os.chmod(tmp_path / "mod", 0o555)
    dc.force_rmtree(tmp_path / "mod")
    assert not (tmp_path / "mod").exists()


def test_clear_dir_keeps_dir(tmp_path: Path):
    d = mk(tmp_path, "cache", "sub", files={"a": "x"})
    (tmp_path / "cache" / "top").write_text("y")
    os.symlink(tmp_path, tmp_path / "cache" / "link")
    dc.clear_dir(tmp_path / "cache")
    assert (tmp_path / "cache").is_dir()
    assert list((tmp_path / "cache").iterdir()) == []
    assert tmp_path.exists() and d.parent.exists()


def test_force_writable_changes_only_directories(tmp_path, monkeypatch):
    root = mk(tmp_path, "cache", "pkg", files={"dependency": "x"})
    external = mk(tmp_path, "external", files={"source": "keep"})
    (root / "link").symlink_to(external, target_is_directory=True)
    changed = []
    real_chmod = os.chmod
    def chmod(path, mode):
        changed.append(Path(path))
        return real_chmod(path, mode)
    monkeypatch.setattr(dc.os, "chmod", chmod)
    dc._force_writable(tmp_path / "cache")
    assert set(changed) == {tmp_path / "cache", root}
    assert (external / "source").read_text() == "keep"


def test_assert_deletable_guards(tmp_path: Path):
    with pytest.raises(RuntimeError):
        dc.assert_deletable(Path("/"), [])
    with pytest.raises(RuntimeError):
        dc.assert_deletable(dc.HOME, [])
    with pytest.raises(RuntimeError):
        dc.assert_deletable(tmp_path, [tmp_path])
    dc.assert_deletable(tmp_path / "x" / "y", [tmp_path])  # fine


# --- config --------------------------------------------------------------------

def test_render_config_roundtrip(tmp_path: Path):
    import tomllib
    text = dc.render_config(["~/dev"], 3.0, ["archive"], aggressive=True, skip=["docker"], docker_volumes=True)
    cfg = tomllib.loads(text)
    assert cfg == {
        "roots": ["~/dev"], "ignore": ["archive"], "days": 3.0, "aggressive": True,
        "skip": ["docker"], "size_jobs": 16, "clean_jobs": 4, "command_timeout": 60, "docker": {"volumes": True},
        "procs": {"orphan_dirs": [], "idle_session_days": 1, "spinning": True}, "tmp": {"claude_scratch": True},
    }


def test_completions_mention_every_group():
    for shell, script in dc.COMPLETIONS.items():
        for g in dc.ALL_GROUPS:
            assert g in script, f"{shell} completion missing group {g}"
        assert "dev-cleaner" in script


def test_group_help_covers_every_group():
    assert set(dc.GROUP_HELP) == set(dc.ALL_GROUPS)


# --- temp sweeps -------------------------------------------------------------

def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    for d, dirs, files in os.walk(p):
        for n in dirs + files:
            os.utime(os.path.join(d, n), (t, t))
    os.utime(p, (t, t))


def test_idle_since_sees_nested_activity(tmp_path: Path):
    d = mk(tmp_path, "job", "sub", files={"f": "x"})
    _age(tmp_path / "job", 5)
    assert dc.idle_since(tmp_path / "job") < time.time() - 4 * 86400
    (d / "fresh").write_text("y")
    assert dc.idle_since(tmp_path / "job") > time.time() - 60


def test_idle_since_leaves_sockets_and_symlinks(tmp_path: Path):
    import socket
    short = Path("/tmp") / f"dc-test-{os.getpid()}"  # AF_UNIX paths are short
    short.mkdir()
    try:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(short / "sock"))
        assert dc.idle_since(short) is None
        assert dc.idle_since(short / "sock") is None
        s.close()
    finally:
        shutil.rmtree(short)
    (tmp_path / "link").symlink_to(tmp_path)
    assert dc.idle_since(tmp_path / "link") is None


def test_scan_sweeps_removes_only_idle(tmp_path: Path):
    mk(tmp_path, "old", files={"a": "x" * 5000}); _age(tmp_path / "old", 5)
    mk(tmp_path, "new", files={"a": "x"})
    mk(tmp_path, "com.apple.thing", files={"a": "x"}); _age(tmp_path / "com.apple.thing", 5)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    [item] = dc.scan_sweeps([dc.Sweep("extra", "idle entries", tmp_path, (tmp_path,))], ctx)
    assert "1 entries" in item.note and item.size is None
    assert item.action() == "removed 1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["com.apple.thing", "new"]


def test_sweep_keeps_dirs_a_running_process_works_in(tmp_path: Path):
    import subprocess
    root = tmp_path.resolve()
    mk(root, "idle-db", "pgdata", files={"a": "x"}); _age(root / "idle-db", 5)
    mk(root, "abandoned", files={"a": "x"}); _age(root / "abandoned", 5)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    server = subprocess.Popen(["sleep", "60"], cwd=root / "idle-db" / "pgdata")
    try:
        assert root / "idle-db" in dc.live_cwds()
        [item] = dc.scan_sweeps([dc.Sweep("extra", "idle entries", root, (root,))], ctx)
        assert "1 entries" in item.note
        assert any("running process" in w for w in ctx.warnings)
        # a process that moves in after the scan is honoured at delete time too
        act = dc._sweep_action([root / "idle-db", root / "abandoned"], time.time() - 86400)
        assert act() == "removed 1, 1 back in use"
    finally:
        server.kill(); server.wait()
    assert sorted(p.name for p in root.iterdir()) == ["idle-db"]


def test_sweep_skips_entries_back_in_use(tmp_path: Path):
    mk(tmp_path, "old", files={"a": "x"}); _age(tmp_path / "old", 5)
    act = dc._sweep_action([tmp_path / "old"], time.time() - 86400)
    (tmp_path / "old" / "b").write_text("touched after the scan")
    assert act() == "removed 0, 1 back in use"
    assert (tmp_path / "old").exists()


def test_sweep_per_entry_cutoff(tmp_path: Path):
    mk(tmp_path, "two-days", files={"a": "x"}); _age(tmp_path / "two-days", 2)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    sw = dc.Sweep("extra", "idle entries", tmp_path, (tmp_path,), cutoff=time.time() - 3 * 86400)
    assert dc.scan_sweeps([sw], ctx) == []


def test_tmp_cleans_old_scratch_inside_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [tmp_path])
    session = mk(tmp_path, f"claude-{os.getuid()}", "-project", "session")
    scratch = mk(session, "scratchpad")
    old = mk(scratch, "old-checkout", files={"source": "old"})
    _age(old, 5)
    fresh = mk(scratch, "current-checkout", files={"source": "new"})
    tasks = mk(session, "tasks", files={"old.log": "done", "current.log": "running"})
    t = time.time() - 5 * 86400
    os.utime(tasks / "old.log", (t, t))
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    items = dc.scan_tmp(ctx)
    assert len(items) == 1
    assert "2 entries" in items[0].note
    assert items[0].action() == "removed 2"
    assert not old.exists() and not (tasks / "old.log").exists()
    assert fresh.exists() and (tasks / "current.log").exists()
    assert scratch.exists() and tasks.exists() and session.exists()


def test_tmp_scratch_granularity_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [tmp_path])
    session = mk(tmp_path, f"claude-{os.getuid()}", "-project", "session")
    old = mk(session, "scratchpad", "old", files={"data": "x"})
    _age(old, 5)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    ctx.tmp_claude_scratch = False
    assert dc.scan_tmp(ctx) == []
    _age(session, 5)
    [item] = dc.scan_tmp(ctx)
    assert item.action() == "removed 1"
    assert not session.exists()


def test_tmp_uses_separate_age_without_changing_extra(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [tmp_path])
    old = mk(tmp_path, "two-days", files={"data": "x"})
    _age(old, 2)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    ctx.tmp_cutoff = time.time() - 3 * 86400
    assert dc.scan_tmp(ctx) == []
    ctx.tmp_cutoff = time.time()
    [item] = dc.scan_tmp(ctx)
    assert item.action() == "removed 1" and not old.exists()


@pytest.mark.parametrize("level", ["claude", "project", "session", "scratchpad"])
def test_tmp_does_not_follow_symlinked_containers(tmp_path, monkeypatch, level):
    base = mk(tmp_path, "tmp")
    outside = mk(tmp_path, "outside", files={"important": "keep"})
    _age(outside, 5)
    monkeypatch.setattr(dc, "temp_roots", lambda: [base])
    parts = [f"claude-{os.getuid()}", "-project", "session", "scratchpad"]
    index = ["claude", "project", "session", "scratchpad"].index(level)
    parent = mk(base, *parts[:index])
    (parent / parts[index]).symlink_to(outside, target_is_directory=True)
    _age(base, 5)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    for item in dc.scan_tmp(ctx):
        item.action()
    assert (outside / "important").read_text() == "keep"


def test_extra_patterns_delete_only_selected_children(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [])
    root = mk(tmp_path, ".codefly", "logs", files={
        "old.log": "old", "old.log.1": "rotated", "current.log": "keep", "metadata.json": "keep"})
    _age(root, 5)
    keep = mk(tmp_path, ".codefly", "data", files={"database": "keep"})
    sweeps = dc.extra_sweeps([{"path": str(root), "days": 3,
                              "include": ["*.log", "*.log.*"], "exclude": ["current.log"]}], time.time())
    ctx = _docker_ctx()
    ctx.sweeps = sweeps
    [item] = dc.scan_extra(ctx)
    assert "2 entries" in item.note
    assert item.action() == "removed 2"
    assert sorted(p.name for p in root.iterdir()) == ["current.log", "metadata.json"]
    assert (keep / "database").read_text() == "keep"


def test_extra_empty_include_selects_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [])
    mk(tmp_path, "old", files={"data": "x"})
    _age(tmp_path, 5)
    ctx = _docker_ctx()
    ctx.sweeps = dc.extra_sweeps([{"path": str(tmp_path), "include": []}], time.time())
    assert dc.scan_extra(ctx) == []


def test_broad_sweep_cannot_delete_narrow_rule_exclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "temp_roots", lambda: [])
    narrow = mk(tmp_path, "nested", "logs", files={"old.log": "x", "keep.log": "keep"})
    _age(tmp_path, 5)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    ctx.sweeps = dc.extra_sweeps([
        {"path": str(tmp_path)},
        {"path": str(narrow), "exclude": ["keep.log"]},
        {"path": str(narrow), "exclude": ["keep.log"]},
    ], time.time())
    [item] = dc.scan_extra(ctx)
    assert item.action() == "removed 1"
    assert (narrow / "keep.log").exists() and not (narrow / "old.log").exists()


@pytest.mark.parametrize("entry", [
    {"include": "*.log"}, {"exclude": [42]}, {"include": ["logs/*.log"]},
    {"include": [".."]}, {"days": -1}, {"days": float("nan")}, {"days": True},
    {"keep": -1}, {"keep": 1.5}, {"keep": True}, {"keep": "2"},
])
def test_extra_rejects_invalid_rules(tmp_path, entry):
    with pytest.raises(ValueError):
        dc.extra_sweeps([{"path": str(tmp_path), **entry}], time.time())


@pytest.mark.parametrize("days,left", [
    (0, ["r4", "r5", "stable"]),          # keep the latest two, whatever their age
    (3.5, ["r3", "r4", "r5", "stable"]),  # outside the newest two AND idle past the age
])
def test_extra_keep_spares_the_newest_entries(tmp_path, monkeypatch, days, left):
    monkeypatch.setattr(dc, "tmp_sweeps", lambda ctx=None: [])
    for n in range(1, 6):
        mk(tmp_path, f"r{n}", files={"a": "x"}); _age(tmp_path / f"r{n}", 6 - n)   # r5 is the newest
    mk(tmp_path, "stable", files={"a": "x"}); _age(tmp_path / "stable", 30)       # excluded: not counted
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups={"extra"},
                     aggressive=False, docker_volumes=False, size_jobs=2)
    ctx.sweeps = dc.extra_sweeps([{"path": str(tmp_path), "days": days, "keep": 2, "exclude": ["stable"]}],
                                 time.time())
    [item] = dc.scan_extra(ctx)
    assert "newest 2 kept" in item.note
    item.action()
    assert sorted(p.name for p in tmp_path.iterdir()) == left


def test_extra_keep_larger_than_the_dir_removes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dc, "tmp_sweeps", lambda ctx=None: [])
    mk(tmp_path, "only", files={"a": "x"}); _age(tmp_path / "only", 9)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups={"extra"},
                     aggressive=False, docker_volumes=False, size_jobs=2)
    ctx.sweeps = dc.extra_sweeps([{"path": str(tmp_path), "days": 0, "keep": 3}], time.time())
    assert dc.scan_extra(ctx) == []


def test_sweep_skips_replaced_container(tmp_path):
    root = mk(tmp_path, "logs", files={"old.log": "x"})
    outside = mk(tmp_path, "outside", files={"old.log": "keep"})
    _age(root, 5)
    _age(outside, 5)
    action = dc._sweep_action([root / "old.log"], time.time() - 86400)
    root.rename(tmp_path / "original")
    root.symlink_to(outside, target_is_directory=True)
    assert action() == "removed 0, 1 back in use"
    assert (outside / "old.log").read_text() == "keep"


def test_show_config_displays_cleanup_filters(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text('''[tmp]
days = 0
claude_scratch = false
[[extra]]
path = "~/.codefly/logs"
days = 3
include = ["*.log"]
exclude = ["current.log"]
''')
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--config", str(config), "--show-config"])
    assert dc.main() == 0
    out = capsys.readouterr().out
    assert "tmp.days" in out and "tmp.claude_scratch" in out
    assert "*.log" in out and "current.log" in out and "days=3" in out and "keep=" not in out
    assert "procs.idle_session_days = 1" in out and "procs.spinning = True" in out


@pytest.mark.parametrize("value,ok", [("2", True), ("0.5", True), ("false", True), ("0", False), ("-1", False), ("true", False), ('"2"', False)])
def test_idle_session_days_must_be_a_positive_number(tmp_path, monkeypatch, capsys, value, ok):
    config = tmp_path / "config.toml"
    config.write_text(f"[procs]\nidle_session_days = {value}\n")
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--config", str(config), "--show-config"])
    if ok:
        assert dc.main() == 0
        assert f"procs.idle_session_days = {'off' if value == 'false' else value}" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit):
            dc.main()


# --- audit -------------------------------------------------------------------

def test_audit_sizes_match_one_du_and_stop_at_mount_points(tmp_path: Path, monkeypatch):
    import subprocess
    root = tmp_path.resolve()
    mk(root, "dev", "repo", "target", "debug", files={"big": "x" * 300_000})
    mk(root, "dev", "repo", "src", files={"main.rs": "x" * 5000})
    mk(root, "dev", files={"notes.txt": "x" * 9000})
    mk(root, "Library", "Caches", files={"blob": "x" * 70_000})
    mk(root, "empty")
    (root / "loose.bin").write_text("x" * 20_000)
    whole = dc._du_sizes(subprocess.run(["du", "-x", "-k", "-d", "3", str(root)], capture_output=True, text=True).stdout)
    seen = []
    sizes, failed = dc.audit_sizes(root, 4, lambda done, total, measured: seen.append((done, total)))
    assert failed == [] and seen[-1] == (2, 2)
    assert set(sizes) == set(whole)
    for d, expected in whole.items():   # dir entries themselves are not counted for the listed levels
        assert abs(sizes[d] - expected) <= 16 * 1024, d
    assert sizes[root / "dev" / "repo" / "target"] >= 300_000
    # a mounted child (the Docker VM's NFS export) is never walked, and a du that fails is reported
    real_scandir = os.scandir

    class Mounted:
        def __init__(self, e): self.path = e.path
        def stat(self, follow_symlinks=True): return SimpleNamespace(st_mode=0o040755, st_dev=-1, st_blocks=0)
    monkeypatch.setattr(dc.os, "scandir", lambda d: [Mounted(e) if e.name == "Library" else e for e in real_scandir(d)])
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: None)
    sizes, failed = dc.audit_sizes(root, 2)
    assert root / "Library" not in sizes and failed == [root / "dev" / "repo"]


def test_audit_rows_descends_where_the_bulk_is():
    g = 1024**2  # du -k reports KB: this many KB is 1 GB
    root = Path("/h")
    du = "\n".join(f"{kb}\t{p}" for kb, p in [
        (10 * g, "/h/a/x"), (1 * g, "/h/a/y"), (11 * g, "/h/a"),
        (3 * g, "/h/b/p"), (3 * g, "/h/b/q"), (8 * g, "/h/b"),   # 6 of 8 in big kids: descend
        (5 * g, "/h/c/z"), (12 * g, "/h/c"),                     # 5 of 12: keep /h/c whole
        (100, "/h/tiny"), (31 * g + 100, "/h"),
    ])
    rows = dict(dc.audit_rows(du, root))
    assert set(rows) == {Path("/h/a/x"), Path("/h/a/y"), Path("/h/b/p"), Path("/h/b/q"), Path("/h/c")}


def test_covered():
    items = [dc.Item("go", "cache", Path("/h/c/go-build"), 4), dc.Item("tmp", "t", Path("/h"), 0)]
    assert dc.covered(Path("/h/c"), 10, items) == 4
    assert dc.covered(Path("/h/c/go-build/x"), 10, items) == 10  # inside a cleaned dir
    assert dc.covered(Path("/h/d"), 10, items) == 0
    # an unmeasured cache still covers its dir; a pruning command of unknown yield does not
    unsized = [dc.Item("go", "go module cache", Path("/h/go/pkg/mod")),
               dc.Item("python", "uv cache prune", Path("/h/uv"), mode="action", shadowed=True)]
    assert dc.covered(Path("/h/go/pkg/mod"), 10, unsized) == 10
    assert dc.covered(Path("/h/uv/archive-v0"), 10, unsized) == 0
    # a sweep that removes 3 of its dir's 10 covers 3, not the dir
    assert dc.covered(Path("/h/logs"), 10, [dc.Item("extra", "idle entries", Path("/h/logs"), 3)]) == 3


# --- docker ------------------------------------------------------------------

class FakeDocker:
    """Stands in for `run`: maps a command's args (after the binary) to stdout."""

    def __init__(self, outputs: dict[tuple[str, ...], str], fail: set[tuple[str, ...]] = frozenset()):
        self.outputs, self.fail = outputs, fail

    def __call__(self, cmd, timeout=60, **kw):
        import subprocess
        key = tuple(cmd[1:])
        if key in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, self.outputs.get(key, ""), "")


DF = "\n".join(json.dumps(d) for d in [
    {"Type": "Images", "Reclaimable": "5GB (50%)"},
    {"Type": "Containers", "Reclaimable": "1GB (10%)"},
    {"Type": "Local Volumes", "Reclaimable": "3GB (30%)"},
    {"Type": "Build Cache", "Reclaimable": "2GB"},
])
BUILDX = "\n".join(json.dumps(d) for d in [
    {"Name": "default", "Driver": "docker", "Nodes": [{"Endpoint": "default"}]},
    {"Name": "lane", "Driver": "docker-container", "Nodes": [{"Endpoint": "orbstack"}]},
    {"Name": "farm", "Driver": "docker-container", "Nodes": [{"Endpoint": "ssh://box"}]},
])


def _docker_ctx(volumes=False):
    return dc.Context(roots=[], cutoff=0, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                      aggressive=False, docker_volumes=volumes, size_jobs=2, measure_sizes=True)


def _docker(monkeypatch, outputs, fail=frozenset()):
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(dc, "docker_vm_disks", lambda: [])
    local = "unix:///var/run/docker.sock"
    defaults = {("context", "inspect", "--format", "{{.Endpoints.docker.Host}}"): local,
                ("context", "inspect", "orbstack", "--format", "{{.Endpoints.docker.Host}}"): local}
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(dc, "run", FakeDocker({**defaults, **outputs}, set(fail)))


def test_scan_docker_covers_build_cache_and_container_builders(monkeypatch):
    _docker(monkeypatch, {
        ("system", "df", "--format", "{{json .}}"): DF,
        ("images", "-f", "dangling=true", "--format", "{{.Size}}"): "500MB\n",
        ("buildx", "ls", "--format", "{{json .}}"): BUILDX,
        ("buildx", "du", "--builder", "lane"): "ID\tRECLAIMABLE\nShared:\t1GB\nReclaimable:\t7.5GB\nTotal:\t8GB\n",
        ("volume", "ls", "-qf", "dangling=true"): "v1\nv2\n",
    })
    ctx = _docker_ctx()
    items = {i.label: i for i in dc.scan_docker(ctx)}
    assert items["docker system prune"].size == 1_500_000_000
    assert items["docker builder prune -a"].size == 2_000_000_000
    assert items["docker buildx prune (lane)"].size == 7_500_000_000
    assert "docker buildx prune (farm)" not in items  # remote builder: not our disk
    assert items["docker image prune -a"].size == 4_500_000_000
    assert not any("volume" in k for k in items)
    assert any("2 unused volume(s) holding 2.8 GB left alone" in w for w in ctx.warnings)


def test_scan_docker_volumes_all(monkeypatch):
    _docker(monkeypatch, {("system", "df", "--format", "{{json .}}"): DF,
                          ("volume", "ls", "-qf", "dangling=true"): "v1\n"})
    items = {i.label: i for i in dc.scan_docker(_docker_ctx("all"))}
    assert items["docker volume prune -a"].size == 3_000_000_000


def test_scan_docker_unknown_sizes_still_listed(monkeypatch):
    # `system df` breaks on a corrupt overlay snapshot; the prunes still work and must show.
    _docker(monkeypatch, {}, fail={("system", "df", "--format", "{{json .}}")})
    ctx = _docker_ctx()
    labels = {i.label: i.size for i in dc.scan_docker(ctx)}
    assert labels["docker system prune"] is None
    assert labels["docker image prune -a"] is None
    assert any("system df" in w for w in ctx.warnings)


def test_scan_docker_daemon_down_reports_vm_disk(monkeypatch, tmp_path: Path):
    disk = tmp_path / "data.img"
    with open(disk, "wb") as f:
        f.write(b"x")
    _docker(monkeypatch, {}, fail={("info", "--format", "{{.ServerVersion}}")})
    monkeypatch.setattr(dc, "docker_vm_disks", lambda: [("OrbStack", disk)])
    monkeypatch.setattr(dc, "file_disk_size", lambda p: 140 * 1024**3)
    ctx = _docker_ctx()
    assert dc.scan_docker(ctx) == []
    assert ctx.warnings == ["docker: daemon not reachable — skipped (140.0 GB in the OrbStack VM disk; start it and re-run)"]


# --- processes ---------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("00:07", 7), ("12:34", 754), ("01:02:03", 3723), ("02-01:20:38", 2 * 86400 + 4838),
])
def test_parse_etime(s, expected):
    assert dc.parse_etime(s) == expected


def test_footprints_parses_top(monkeypatch):
    import subprocess
    out = "Processes: 3 total\n\nPID    MEM\n0      129M-\n13444  2082M+\n704    1G\n39866  7376K\n"
    monkeypatch.setattr(dc, "IS_MAC", True)
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    assert dc._footprints() == {0: 129 * 1024**2, 13444: 2082 * 1024**2, 704: 1024**3, 39866: 7376 * 1024}


def _proc(pid, exe, ppid=1, age=3 * 86400, cpu=0.0, uid=None, mem=10 * 1024**2):
    return dc.Proc(pid, ppid, os.getuid() if uid is None else uid, age, cpu, mem, exe)


def test_find_orphans_selects_only_safe_ones():
    roots = ("/home/me/.tool/agents/", "/tmp/")
    procs = [
        _proc(1, "/home/me/.tool/agents/svc/postgres__0.0.1"),             # orphaned plugin
        _proc(2, "/tmp/go-build123/b001/accounts.test"),                    # leaked test helper
        _proc(3, "/home/me/.tool/agents/svc/go__0.0.1", ppid=500),          # still has its parent
        _proc(4, "/home/me/.tool/agents/svc/go__0.0.1", age=600),           # younger than the threshold
        _proc(5, "/home/me/.tool/agents/svc/go__0.0.1", cpu=40.0),          # doing work
        _proc(6, "/home/me/.tool/agents/svc/go__0.0.1", uid=os.getuid() + 1),
        _proc(7, "/usr/local/bin/some-daemon"),                             # orphan by design
        _proc(8, "/home/me/.tool/agents-other/x"),                          # prefix must end at a dir
    ]
    assert [p.pid for p in dc.find_orphans(procs, roots, min_age=86400)] == [1, 2]


def test_orphan_roots_cover_resolved_and_raw_temp(tmp_path: Path):
    ctx = dc.Context(roots=[], cutoff=0, extra_skip=set(), groups=set(), aggressive=False,
                     docker_volumes=False, size_jobs=2, orphan_dirs=[tmp_path / "agents"])
    roots = dc.orphan_roots(ctx)
    assert str(tmp_path / "agents") + "/" in roots
    assert "/tmp/" in roots and str(Path("/tmp").resolve()) + "/" in roots
    assert all(r.endswith("/") for r in roots)


def test_scan_procs_item_is_ram_and_not_summed(monkeypatch):
    procs = [_proc(1, "/tmp/x/a.test", mem=2 * 1024**3), _proc(2, "/tmp/x/a.test", mem=1024**3)]
    monkeypatch.setattr(dc, "list_procs", lambda: procs)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    [item] = dc.scan_procs(ctx)
    assert item.ram and item.size == 3 * 1024**3 and "a.test ×2" in item.note
    from rich.console import Console
    c = Console(record=True, width=120)
    c.print(dc.render_table([item, dc.Item("go", "cache", Path("/c"), 5 * 1024**3)], "1d", False))
    text = c.export_text()
    assert "3.0 GB RAM" in text
    assert "5.0 GB" in text and "8.0 GB" not in text  # RAM never added to the disk total


def test_stop_action_only_signals_what_still_qualifies(monkeypatch):
    a, b = _proc(11, "/tmp/x/a.test"), _proc(12, "/tmp/x/b.test")
    # By clean time: 11 is still an orphan; pid 12 now belongs to something else.
    monkeypatch.setattr(dc, "list_procs", lambda: [a, _proc(12, "/tmp/x/other", ppid=1)])
    sent, dead = [], set()

    def fake_kill(pid, sig):
        if sig == 0:
            if pid in dead:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        dead.add(pid)
    monkeypatch.setattr(dc.os, "kill", fake_kill)
    assert dc._stop_action([a, b], ("/tmp/",), 86400)() == "stopped 1"
    assert sent == [(11, dc.signal.SIGTERM)]

# --- idle terminal sessions ----------------------------------------------------

def _session_procs():
    from dataclasses import replace
    GB = 1024**3
    return [
        replace(_proc(10, "/bin/codex", ppid=5, mem=GB), tty="ttys001"),            # silent agent ...
        replace(_proc(11, "/bin/gopls", ppid=10, mem=GB), tty=""),                  # ... and its detached helper
        replace(_proc(20, "/bin/claude", ppid=5, mem=GB), tty="ttys002"),           # terminal printed an hour ago
        replace(_proc(30, "/bin/codex", ppid=5, mem=GB), tty="ttys003"),            # silent, but a child is computing
        replace(_proc(31, "/bin/make", ppid=30, cpu=80.0), tty="ttys003"),
        replace(_proc(40, "/bin/codex", ppid=5, mem=GB), tty="ttys004"),            # silent, but something just started
        replace(_proc(41, "/bin/sh", ppid=40, age=60), tty="ttys004"),
        replace(_proc(50, "/bin/zsh", ppid=5, mem=5 * 1024**2), tty="ttys005"),     # bare shell: nothing to reclaim
        replace(_proc(60, "/bin/codex", ppid=5, mem=GB, uid=os.getuid() + 1), tty="ttys006"),  # someone else's
        replace(_proc(70, "/bin/daemon", ppid=1, mem=GB), tty=""),                  # no terminal at all
    ]


def _fake_ttys(monkeypatch, idle=None):
    idle = {"ttys002": 3600, **(idle or {})}
    monkeypatch.setattr(dc, "tty_idle", lambda tty, now: idle.get(tty, 3 * 86400))
    monkeypatch.setattr(dc, "_own_ttys", lambda procs: set())


def test_idle_sessions_need_silence_no_cpu_and_no_new_process(monkeypatch):
    _fake_ttys(monkeypatch)
    sessions = dc.terminal_sessions(_session_procs())
    assert {s.tty for s in sessions} == {"ttys001", "ttys002", "ttys003", "ttys004", "ttys005"}
    [idle] = dc.idle_sessions(sessions, 2 * 86400)
    assert idle.tty == "ttys001" and {p.pid for p in idle.procs} == {10, 11} and idle.mem == 2 * 1024**3


def test_own_terminal_is_never_a_session_candidate(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(dc, "tty_idle", lambda tty, now: 9 * 86400)
    monkeypatch.setattr(dc.os, "getpid", lambda: 12)
    procs = [replace(_proc(10, "/bin/zsh", ppid=5, mem=1024**3), tty="ttys001"),
             replace(_proc(11, "/bin/uv", ppid=10), tty="ttys001"),
             replace(_proc(12, "/bin/python", ppid=11), tty="")]  # us, detached, under that terminal
    assert dc.terminal_sessions(procs) == []


def test_scan_procs_closes_silent_sessions_unless_turned_off(monkeypatch):
    _fake_ttys(monkeypatch)
    monkeypatch.setattr(dc, "list_procs", _session_procs)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    [item] = dc.scan_procs(ctx)   # on by default, at one day
    assert item.ram and item.size == 2 * 1024**3 and "1 silent ≥1d, 2 processes: codex ×1" in item.note
    ctx.idle_session_days = None
    assert dc.scan_procs(ctx) == []
    ctx.idle_session_days = 5   # nothing has been silent that long
    assert dc.scan_procs(ctx) == []


def test_close_sessions_rechecks_and_hangs_up(monkeypatch):
    from dataclasses import replace
    _fake_ttys(monkeypatch, {"ttys007": 3 * 86400, "ttys008": 3 * 86400})
    a = [replace(_proc(10, "/bin/codex", ppid=5, mem=1024**3), tty="ttys001"), replace(_proc(11, "/bin/gopls", ppid=10), tty="")]
    b = [replace(_proc(70, "/bin/codex", ppid=5, mem=1024**3), tty="ttys007")]
    c = [replace(_proc(80, "/bin/codex", ppid=5, mem=1024**3), tty="ttys008")]
    targets = dc.idle_sessions(dc.terminal_sessions(a + b + c), 86400)
    assert len(targets) == 3
    # By clean time: ttys007 printed something; ttys008's PID now belongs to another program.
    _fake_ttys(monkeypatch, {"ttys007": 5, "ttys008": 3 * 86400})
    monkeypatch.setattr(dc, "list_procs", lambda: a + b + [replace(c[0], started="later")])
    sent, dead = [], set()

    def fake_kill(pid, sig):
        if sig == 0:
            if pid in dead:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        dead.add(pid)
    monkeypatch.setattr(dc.os, "kill", fake_kill)
    assert dc._close_sessions_action(targets, 86400)() == "closed 1 session(s), 2 processes, 2 back in use"
    assert sorted(sent) == [(10, dc.signal.SIGHUP), (11, dc.signal.SIGTERM)]


def test_memory_view_lists_silent_sessions(monkeypatch):
    _fake_ttys(monkeypatch)
    out = _memory_text(monkeypatch, _session_procs(), commands={10: "/bin/codex resume abc"})
    assert "terminal sessions silent ≥1.0d" in out and "codex resume abc" in out and "ttys001" in out
    assert "1 silent session(s) hold 2.0 GB across 2 processes" in out and "offers to close them" in out
    assert "ttys003" not in out


def test_spinning_processes_are_ours_detached_and_still_burning(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(dc, "HOME", Path("/home/me"))
    hour = 3600
    spin = dict(age=20 * hour, cpu=25.0)
    procs = [
        replace(_proc(1, "/home/me/stack/.venv/bin/python", ppid=9, mem=1024**3, **spin), cpu_time=5 * hour),
        replace(_proc(2, "/tmp/go-build1/b001/x.test", ppid=9, **spin), cpu_time=5 * hour),
        replace(_proc(3, "/home/me/bin/build", ppid=9, **spin), cpu_time=5 * hour, tty="ttys001"),   # someone watches it
        replace(_proc(4, "/Applications/VM.app/Helper", ppid=1, **spin), cpu_time=15 * hour),       # an app
        replace(_proc(5, "/home/me/bin/server", ppid=9, age=20 * hour, cpu=0.0), cpu_time=5 * hour),  # calmed down
        replace(_proc(6, "/home/me/bin/new", ppid=9, age=hour, cpu=90.0), cpu_time=0.9 * hour),     # too young
        replace(_proc(7, "/home/me/bin/light", ppid=9, **spin), cpu_time=0.5 * hour),               # 2.5% average
        replace(_proc(8, "/home/me/bin/theirs", ppid=9, uid=os.getuid() + 1, **spin), cpu_time=5 * hour),
    ]
    assert [p.pid for p in dc.find_spinning(procs)] == [1, 2]
    monkeypatch.setattr(dc, "list_procs", lambda: procs)
    ctx = dc.Context(roots=[], cutoff=time.time() - 86400, extra_skip=set(), groups=set(dc.ALL_GROUPS),
                     aggressive=False, docker_volumes=False, size_jobs=2)
    [item] = dc.scan_procs(ctx)
    assert item.label == "spinning processes" and item.cores == pytest.approx(0.5) and "10.0 CPU-hours" in item.note
    from rich.console import Console
    c = Console(record=True, width=160)
    c.print(dc.render_summary([item, dc.Item("go", "cache", Path("/c"), 5 * 1024**3)], "1d", False))
    text = c.export_text()
    assert "cpu" in text and "🔥" not in text and "0.5 cores" in text and "5.0 GB" in text
    # by clean time one of them went quiet: only the other is stopped
    monkeypatch.setattr(dc, "list_procs", lambda: [procs[0], replace(procs[1], cpu=0.0)])
    sent, dead = [], set()

    def fake_kill(pid, sig):
        if sig == 0:
            if pid in dead:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        dead.add(pid)
    monkeypatch.setattr(dc.os, "kill", fake_kill)
    assert item.action() == "stopped 1, 1 no longer spinning"
    assert sent == [(1, dc.signal.SIGTERM)]
    ctx.stop_spinning = False
    assert dc.scan_procs(ctx) == []


# --- cpu ---------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [("0:00.02", 0.02), ("290:00.71", 17400.71), ("01:02:03", 3723),
                                          ("2-01:02:03", 2 * 86400 + 3723)])
def test_parse_cputime(text, seconds):
    assert dc.parse_cputime(text) == pytest.approx(seconds)


def test_cpu_rates_use_the_sample_not_the_lifetime_average():
    from dataclasses import replace
    spinning = replace(_proc(10, "/bin/spin"), cpu_time=1000.0, started="a")
    quiet = replace(_proc(20, "/bin/quiet"), cpu_time=50_000.0, started="a")
    reused = replace(_proc(30, "/bin/old"), cpu_time=900.0, started="a")
    after = [replace(spinning, cpu_time=1004.0), quiet, replace(reused, cpu_time=1.0, started="b"),
             replace(_proc(40, "/bin/new", age=1), cpu_time=1.0)]
    rates = dc.cpu_rates([spinning, quiet, reused], after, 2.0)
    assert rates == {10: 200.0, 20: 0.0, 40: 50.0}   # a recycled PID is not compared with its predecessor
    assert dc.cpu_rates([spinning], after, 0) == {}


def test_cpu_report_ranks_by_sample_and_flags_sustained_burners(monkeypatch):
    from dataclasses import replace
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc.os, "getloadavg", lambda: (60.0, 50.0, 40.0))
    monkeypatch.setattr(dc.os, "cpu_count", lambda: 10)
    hour = 3600
    before = [replace(_proc(10, "/opt/app/server", age=20 * hour), cpu_time=5 * hour),      # 25% for 20 h
              replace(_proc(20, "/usr/bin/builder", age=60), cpu_time=10.0),                # busy right now
              replace(_proc(30, "/usr/libexec/mds_stores", age=100 * hour, uid=os.getuid() + 1), cpu_time=20 * hour),
              replace(_proc(40, "/opt/idle", age=100 * hour), cpu_time=60.0)]
    after = [replace(before[0], cpu_time=5 * hour + 0.5), replace(before[1], cpu_time=14.0),
             replace(before[2], cpu_time=20 * hour + 1), before[3]]
    output = StringIO()
    Console(file=output, width=200).print(dc.render_cpu(before, after, 2.0, commands={10: "/opt/app/server --port 1"}))
    out = output.getvalue()
    assert "load: 60.0 50.0 40.0 (1/5/15 min) on 10 cores" in out and "6.0× the cores" in out
    assert "sampled 2.0s: 2.8 of 10 cores busy" in out
    now, sustained = out.split("sustained:")
    assert now.index("builder") < now.index("mds_stores") < now.index("server --port 1")
    assert "2 processes" in sustained and "server --port 1" in sustained and "mds_stores" in sustained
    assert "builder" not in sustained and "/opt/idle" not in sustained
    assert "Spotlight is indexing" in out and "(uid " in out
    output = StringIO()
    Console(file=output, width=200).print(dc.render_cpu(before, after, 2.0, pattern="absent"))
    assert "No processes match" in output.getvalue()


def test_cpu_flag_samples_and_changes_nothing(monkeypatch, capsys):
    from dataclasses import replace
    a = replace(_proc(10, "/opt/app/server"), cpu_time=10.0)
    samples = iter([[a], [replace(a, cpu_time=11.0)]])
    monkeypatch.setattr(dc, "list_procs", lambda footprints=True: next(samples))
    monkeypatch.setattr(dc, "process_commands", lambda: {})
    slept = []
    monkeypatch.setattr(dc.time, "sleep", slept.append)
    monkeypatch.setattr(dc, "scan_all", lambda ctx: pytest.fail("--cpu must not scan"))
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--cpu", "--cpu-sample", "0.5", "--top", "3"])
    assert dc.main() == 0
    assert 0.5 in slept and "cpu — no changes made" in capsys.readouterr().out


# --- resource diagnostics ----------------------------------------------------

def _memory_text(monkeypatch, procs, **options):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "system_memory", lambda: {"RAM": 32 * 1024**3, "swap_total": 0, "swap_used": 0})
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    output = StringIO()
    c = Console(file=output, width=180)
    c.print(dc.render_memory(procs, ctx, **options))
    return output.getvalue()


def test_list_procs_preserves_paths_and_start_identity(monkeypatch):
    import subprocess
    monkeypatch.setattr(dc, "IS_MAC", True)
    monkeypatch.setattr(dc, "_footprints", lambda: {41: 2 * 1024**3})
    out = ("41 1 501 02-01:20:38 2.5 4096 ttys012 290:00.71 Wed Oct 21 12:30:00 2026 /Applications/My App/server\n"
           "42 41 501 00:01 0.0 1024 ?? 0:00.02 Sun Oct 25 12:30:00 2026 /usr/bin/worker\n"
           "malformed line\n")
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    a, b = dc.list_procs()
    assert a.exe == "/Applications/My App/server"
    assert a.started == "Wed Oct 21 12:30:00 2026"
    assert a.mem == 2 * 1024**3 and a.footprint
    assert a.age == 2 * 86400 + 4838 and a.cpu == 2.5
    assert b.mem == 1024**2 and not b.footprint
    assert (a.tty, a.cpu_time, b.tty, b.cpu_time) == ("ttys012", 290 * 60 + 0.71, "", 0.02)


def test_linux_process_executable_resolution(monkeypatch):
    import subprocess
    monkeypatch.setattr(dc, "IS_MAC", False)
    monkeypatch.setattr(dc.os, "readlink", lambda p: "/opt/my app/server")
    out = "41 1 501 00:01 0.0 1024 pts/3 01-02:03:04 Sun Oct 25 12:30:00 2026 truncated-comm\n"
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    [p] = dc.list_procs()
    assert p.exe == "/opt/my app/server" and not p.footprint
    assert p.tty == "pts/3" and p.cpu_time == 86400 + 2 * 3600 + 3 * 60 + 4


def test_group_procs_separates_executables_and_families():
    procs = [_proc(10, "/opt/a/node", ppid=1), _proc(11, "/opt/a/node", ppid=10),
             _proc(12, "/opt/b/node", ppid=11), _proc(20, "/opt/a/node", ppid=1),
             _proc(21, "/usr/bin/root-worker", ppid=10, uid=os.getuid() + 1),
             _proc(30, "/bin/worker", ppid=999)]
    programs = {root.exe: {p.pid for p in members} for root, members in dc.group_procs(procs)}
    assert programs["/opt/a/node"] == {10, 11, 20}
    assert programs["/opt/b/node"] == {12}
    families = {root.pid: {p.pid for p in members} for root, members in dc.group_procs(procs, True)}
    assert families == {10: {10, 11, 12}, 20: {20}, 21: {21}, 30: {30}}
    # A malformed ancestry cycle and its descendants form one deterministic family.
    cyclic = [_proc(4, "/a", ppid=5), _proc(5, "/b", ppid=4), _proc(3, "/c", ppid=5)]
    [(root, members)] = dc.group_procs(cyclic, True)
    assert root.pid == 4 and {p.pid for p in members} == {3, 4, 5}


def test_memory_deltas_ignore_pid_reuse_and_metric_changes():
    from dataclasses import replace
    a = replace(_proc(10, "/a", mem=100), started="first")
    b = replace(_proc(20, "/b", mem=200), started="first")
    c = replace(_proc(30, "/c", mem=300), started="first")
    now = [replace(a, mem=150), replace(b, mem=500, started="second"),
           replace(c, mem=800, footprint=True), _proc(40, "/new")]
    assert dc.memory_deltas(now, [a, b, c]) == {10: 50}


def test_memory_view_includes_small_hogs_and_command_filter(monkeypatch):
    procs = [_proc(10, "/opt/node", mem=150 * 1024**2), _proc(11, "/opt/node", mem=10 * 1024**2)]
    out = _memory_text(monkeypatch, procs, commands={10: "node dev.js --port 3000", 11: "node other.js"},
                       pattern="DEV.JS", full=True)
    assert "150.0 MB" in out and "dev.js --port 3000" in out
    assert "1 / 2 processes" in out and "other.js" not in out
    assert "not reclaimable RAM" in out
    assert "No processes match" in _memory_text(monkeypatch, procs, pattern="absent")


def test_family_filter_preserves_parent_and_counts_orphan_children(monkeypatch):
    procs = [_proc(10, "/tmp/agent", mem=100 * 1024**2),
             _proc(11, "/usr/bin/postgres", ppid=10, mem=1024**3)]
    out = _memory_text(monkeypatch, procs, by_tree=True, pattern="postgres", full=True)
    assert "/tmp/agent" in out and "root PID" in out
    assert "with same-owner descendants: 2 processes, 1.1 GB" in out
    assert "PPID 1 alone" in out and "--only procs --dry-run" in out


def test_growth_sort_prefers_growth_over_size(monkeypatch):
    from dataclasses import replace
    old = [_proc(10, "/large", mem=3 * 1024**3), _proc(20, "/growing", mem=100 * 1024**2)]
    now = [replace(old[0], mem=2 * 1024**3), replace(old[1], mem=300 * 1024**2), _proc(30, "/new")]
    out = _memory_text(monkeypatch, now, previous=old, sort="growth", top=1, full=True, elapsed=5)
    assert "/growing" in out and "/large" not in out
    assert "+200.0 MB" in out and "Δ over 5.0s" in out


def test_system_memory_mac_and_missing_commands(monkeypatch):
    import subprocess
    monkeypatch.setattr(dc, "IS_MAC", True)
    outputs = {("sysctl", "-n", "hw.memsize"): str(48 * 1024**3),
               ("vm_stat",): "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages occupied by compressor: 10.\n",
               ("sysctl", "-n", "vm.swapusage"): "total = 10240.00M  used = 8704.00M  free = 1536.00M"}
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, outputs[tuple(cmd)], ""))
    assert dc.system_memory() == {"RAM": 48 * 1024**3, "compressed": 163840,
                                  "swap_total": 10 * 1024**3, "swap_used": int(8.5 * 1024**3)}
    monkeypatch.setattr(dc, "run", lambda *a, **kw: None)
    assert dc.system_memory() == {}


def test_system_memory_linux(monkeypatch):
    monkeypatch.setattr(dc, "IS_MAC", False)
    monkeypatch.setattr(dc.Path, "read_text", lambda p: "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 50 kB\n")
    assert dc.system_memory() == {"RAM": 1000 * 1024, "available": 400 * 1024,
                                  "swap_total": 200 * 1024, "swap_used": 150 * 1024}


def test_watch_samples_growth_without_cleaning(monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    monkeypatch.setattr(dc, "system_memory", lambda: {})
    monkeypatch.setattr(dc, "process_commands", lambda: {})
    samples = iter([[_proc(10, "/a", mem=1024**2)], [_proc(10, "/a", mem=2 * 1024**2)]])
    monkeypatch.setattr(dc, "list_procs", lambda: next(samples))
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(dc.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        dc.run_mem(_docker_ctx(), watch=5, sort="growth")
    assert sleeps == [5, 5]
    assert "+1.0 MB" in dc.console.file.getvalue()


@pytest.mark.parametrize("args", [
    ["--mem-watch", "5"], ["--mem", "--mem-top", "0"], ["--mem", "--mem-watch", "nan"],
    ["--mem", "--mem-watch", "inf"], ["--mem", "--mem-watch", "-1"],
    ["--mem", "--mem-sort", "growth"], ["--mem", "--docker-audit"],
    ["--top", "3"], ["--tree"], ["--cpu-sample", "2"], ["--cpu", "--cpu-sample", "0"],
    ["--cpu", "--mem"], ["--cpu", "--mem-watch", "5"], ["--cpu", "--mem-sort", "cpu"],
])
def test_invalid_diagnostic_options_fail_before_scanning(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", *args])
    with pytest.raises(SystemExit) as e:
        dc.main()
    assert e.value.code == 2


def test_docker_audit_deduplicates_builders_and_only_queries_local_running(monkeypatch):
    from io import StringIO
    from rich.console import Console
    builders = [
        {"Name": "lane", "Driver": "docker-container", "LastActivity": "2026-10-01T00:00:00Z",
         "Nodes": [{"Endpoint": "orbstack", "Status": "running"}]},
        {"Name": "down", "Driver": "docker-container", "Nodes": [{"Endpoint": "orbstack", "Status": "stopped"}]},
        {"Name": "farm", "Driver": "docker-container", "Nodes": [{"Endpoint": "remote-context", "Status": "running"}]},
    ]
    _docker(monkeypatch, {
        ("info", "--format", "{{.MemTotal}}"): str(16 * 1024**3),
        ("system", "df", "--format", "{{json .}}"): DF,
        ("stats", "--no-stream", "--format", "{{json .}}"): json.dumps({"Name": "db", "MemUsage": "1GiB / 16GiB", "CPUPerc": "2%"}),
        ("buildx", "ls", "--format", "{{json .}}"): "\n".join(json.dumps(b) for b in [*builders, builders[0]]),
        ("buildx", "du", "--builder", "lane"): "Reclaimable:\t7.5GB\n",
        ("context", "inspect", "remote-context", "--format", "{{.Endpoints.docker.Host}}"): "ssh://remote",
    })
    fake, calls = dc.run, []
    def record(cmd, **kw):
        calls.append(tuple(cmd[1:]))
        return fake(cmd, **kw)
    monkeypatch.setattr(dc, "run", record)
    output = StringIO()
    monkeypatch.setattr(dc, "console", Console(file=output, width=200))
    assert dc.run_docker_audit()
    assert calls.count(("buildx", "du", "--builder", "lane")) == 1
    assert not any(c[:2] == ("buildx", "du") and c[-1] != "lane" for c in calls)
    assert not any(word in ("prune", "rm", "stop", "restart", "kill") for call in calls for word in call)
    text = output.getvalue()
    assert "1.0 GB across 1 container" in text
    assert "7.0 GB" in text and "not running; cache not queried" in text
    assert "remote or unverified; not queried" in text
    assert "capacity, not host memory consumption" in text


def test_docker_cleanup_skips_remote_context(monkeypatch):
    _docker(monkeypatch, {("context", "inspect", "--format", "{{.Endpoints.docker.Host}}"): "ssh://remote"})
    ctx = _docker_ctx()
    assert dc.scan_docker(ctx) == []
    assert "remote" in ctx.warnings[0]


def test_same_executable_recycled_pid_is_not_stopped(monkeypatch):
    from dataclasses import replace
    old = replace(_proc(11, "/tmp/a.test"), started="first")
    monkeypatch.setattr(dc, "list_procs", lambda: [replace(old, started="second")])
    def unexpected_signal(*args):
        pytest.fail("a recycled PID must not be signalled")
    monkeypatch.setattr(dc.os, "kill", unexpected_signal)
    assert dc._stop_action([old], ("/tmp/",), 86400)() == "stopped 0"


def test_family_filter_keeps_database_descendants(monkeypatch):
    procs = [_proc(10, "/tmp/codefly-agent", mem=100 * 1024**2),
             _proc(11, "/usr/bin/postgres", ppid=10, mem=1024**3)]
    out = _memory_text(monkeypatch, procs, by_tree=True, pattern="codefly", full=True)
    assert "2 / 2 processes" in out and "/usr/bin/postgres" in out
    assert "1.1 GB" in out


def test_family_stops_at_user_service_manager():
    procs = [_proc(10, "/usr/lib/systemd/systemd"), _proc(11, "/app/a", ppid=10),
             _proc(12, "/app/b", ppid=10), _proc(13, "/app/worker", ppid=11)]
    families = {root.pid: {p.pid for p in members} for root, members in dc.group_procs(procs, True)}
    assert families == {10: {10}, 11: {11, 13}, 12: {12}}


def test_force_kill_does_not_signal_pid_reused_during_grace_period(monkeypatch):
    from dataclasses import replace
    old = replace(_proc(11, "/tmp/a.test"), started="first")
    scans = iter([[old], [replace(old, started="second")]])
    monkeypatch.setattr(dc, "list_procs", lambda: next(scans))
    times = iter([0, 9])
    monkeypatch.setattr(dc.time, "time", lambda: next(times))
    sent = []
    monkeypatch.setattr(dc.os, "kill", lambda pid, sig: sent.append((pid, sig)) if sig else None)
    assert dc._stop_action([old], ("/tmp/",), 86400)() == "stopped 1"
    assert sent == [(11, dc.signal.SIGTERM)]


def test_mem_mode_never_enters_cleanup(monkeypatch, tmp_path):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO()))
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--mem", "--config", str(tmp_path / "none.toml"),
                                     "--mem-filter", "claude", "--mem-top", "3", "--mem-tree"])
    options = {}
    def fake_mem(ctx, **kw):
        options.update(kw)
        return True
    monkeypatch.setattr(dc, "run_mem", fake_mem)
    monkeypatch.setattr(dc, "scan_all", lambda ctx: pytest.fail("read-only memory mode must not scan cleanup items"))
    assert dc.main() == 0
    assert options["pattern"] == "claude" and options["top"] == 3 and options["by_tree"]


def test_docker_environment_endpoint_priority(monkeypatch):
    _docker(monkeypatch, {("context", "inspect", "chosen", "--format", "{{.Endpoints.docker.Host}}"): "unix:///chosen.sock"})
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote")
    assert dc.docker_endpoint("docker") == "ssh://remote"
    monkeypatch.setenv("DOCKER_CONTEXT", "chosen")
    assert dc.docker_endpoint("docker") == "unix:///chosen.sock"


# --- concurrent read-only probes --------------------------------------------

def test_builder_cache_queries_are_bounded_and_concurrent(monkeypatch):
    import threading
    barrier = threading.Barrier(dc.PROBE_JOBS, timeout=5)
    lock = threading.Lock()
    active, peak = 0, 0
    names = [f"builder-{i}" for i in range(dc.PROBE_JOBS * 2)]
    def measure(docker, name):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()  # succeeds only if the worker cap is filled concurrently
            return names.index(name)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(dc, "_buildx_reclaimable", measure)
    assert dc._builder_cache_sizes("docker", names) == dict(zip(names, range(len(names))))
    assert peak == dc.PROBE_JOBS


def test_docker_audit_overlaps_disk_stats_and_builder_inventory(monkeypatch):
    import subprocess
    import threading
    from io import StringIO
    from rich.console import Console
    _docker(monkeypatch, {("info", "--format", "{{.MemTotal}}"): str(16 * 1024**3)})
    monkeypatch.setattr(dc, "console", Console(file=StringIO()))
    barrier = threading.Barrier(3, timeout=5)
    completed = []
    def disk(docker):
        barrier.wait()
        completed.append("disk")
        return {}
    def builders(docker):
        barrier.wait()
        completed.append("builders")
        return [], {}, {}
    fake = dc.run
    def run(cmd, **kw):
        if cmd[1] == "stats":
            barrier.wait()
            completed.append("memory")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return fake(cmd, **kw)
    monkeypatch.setattr(dc, "_docker_df", disk)
    monkeypatch.setattr(dc, "_builder_inventory", builders)
    monkeypatch.setattr(dc, "run", run)
    assert dc.run_docker_audit()
    assert set(completed) == {"disk", "builders", "memory"}


def test_global_scans_overlap_projects_and_keep_result_order(monkeypatch):
    import threading
    barrier = threading.Barrier(4, timeout=5)
    def scanner(name):
        def scan(ctx):
            ctx.warnings.append(name)
            barrier.wait()
            return [dc.Item("rust", name, size=1)]
        return scan
    def projects(ctx, progress):
        barrier.wait()
        return [dc.Item("rust", "projects", size=1)]
    monkeypatch.setattr(dc, "GLOBAL_SCANNERS", {"rust": scanner("rust"), "docker": scanner("docker")})
    monkeypatch.setattr(dc, "scan_known_caches", scanner("caches"))
    monkeypatch.setattr(dc, "scan_projects", projects)
    ctx = _docker_ctx()
    ctx.groups = {"rust", "docker"}
    ctx.warnings = ["existing"]
    items = dc.scan_all(ctx)
    assert [item.label for item in items] == ["projects", "rust", "docker", "caches"]
    assert ctx.warnings == ["existing", "rust", "docker", "caches"]


def test_parallel_scanner_failure_does_not_hide_other_results(monkeypatch):
    def broken(ctx):
        raise RuntimeError("tool failed")
    monkeypatch.setattr(dc, "GLOBAL_SCANNERS", {"docker": broken})
    monkeypatch.setattr(dc, "scan_known_caches", lambda ctx: [dc.Item("tools", "cache", size=1)])
    ctx = _docker_ctx()
    ctx.groups = {"docker"}
    assert [item.label for item in dc.scan_all(ctx)] == ["cache"]
    assert ctx.warnings == ["docker: scan failed (tool failed) — skipped"]


def test_activity_check_returns_without_walking_known_active_project(tmp_path, monkeypatch):
    project = mk(tmp_path, "active", files={"Cargo.toml": ""})
    monkeypatch.setattr(dc.os, "walk", lambda *a, **kw: pytest.fail("an active directory does not need a source walk"))
    assert dc.project_activity(project, time.time() - 86400) >= time.time() - 86400


def test_activity_check_stops_after_first_recent_source(tmp_path, monkeypatch):
    project = mk(tmp_path, "active", files={"old.rs": "", "recent.rs": ""})
    old = time.time() - 30 * 86400
    os.utime(project / "old.rs", (old, old))
    os.utime(project, (old, old))
    def walk(*args, **kw):
        yield str(project), [], ["old.rs", "recent.rs"]
        pytest.fail("one recent source already establishes that the project is active")
    monkeypatch.setattr(dc.os, "walk", walk)
    assert dc.project_activity(project, time.time() - 86400) >= time.time() - 86400


def test_default_sizing_skips_active_builds_but_audit_can_measure_them(tmp_path, monkeypatch):
    cutoff = time.time() - 86400
    items = [dc.Item("node", "node_modules/", tmp_path / "active", project=tmp_path / "p", mtime=time.time()),
             dc.Item("rust", "target/", tmp_path / "stale", project=tmp_path / "q", mtime=cutoff - 1)]
    measured = []
    def size(path):
        measured.append(path)
        return 123
    monkeypatch.setattr(dc, "dir_size", size)
    with dc.make_progress() as progress:
        dc.size_items(items, 2, progress, cutoff=cutoff)
    assert measured == [tmp_path / "stale"]
    assert items[0].size is None and items[1].size == 123
    with dc.make_progress() as progress:
        dc.size_items(items, 2, progress)
    assert measured == [tmp_path / "stale", tmp_path / "active"]
    assert items[0].size == 123


def test_skipped_active_sizes_are_labelled_unmeasured():
    item = dc.Item("node", "node_modules/", Path("/p/node_modules"), project=Path("/p"))
    from rich.console import Console
    c = Console(record=True, width=120)
    c.print(*dc.render_notes([item], [], "1d", []))
    text = c.export_text()
    assert "sizes skipped; use --audit" in text and "--hours N" in text
    assert "holding" not in text


def test_discovery_reports_progress_without_waiting_for_project_matches(tmp_path):
    mk(tmp_path, "empty", "nested", files={"source.txt": ""})
    visited = []
    assert list(dc.find_projects(tmp_path, visit=visited.append)) == []
    assert set(visited) == {tmp_path, tmp_path / "empty", tmp_path / "empty/nested"}


# --- bounded, concurrent cleanup ---------------------------------------------

def test_cleanup_command_timeout_stops_client_and_plugin(tmp_path):
    import subprocess
    child_pid = tmp_path / "child.pid"
    script = ("import subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              f"Path({str(child_pid)!r}).write_text(str(child.pid)); time.sleep(30)")
    start = time.monotonic()
    with pytest.raises(dc.CleanupDeferred, match="timed out after 1s; client stopped"):
        dc._cmd_action([sys.executable, "-c", script], timeout=1)()
    assert time.monotonic() - start < 5
    assert child_pid.exists(), "test must start a child to verify plugin cancellation"
    pid = int(child_pid.read_text())
    try:
        # An orphan may briefly be a zombie waiting for init to reap it.
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        assert not state or state.startswith("Z"), f"plugin survived timeout: {state}"
    finally:
        try:
            os.kill(pid, dc.signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cleanup_command_success_and_busy_cache_error():
    action = dc._cmd_action([sys.executable, "-c", "print('Total reclaimed: 100MB')"],
                            summary=lambda out: out.split(": ")[1])
    assert action() == "100MB"
    action = dc._cmd_action([sys.executable, "-c",
                            "import sys; sys.stderr.write('go: unlinkat cache: directory not empty'); sys.exit(1)"])
    with pytest.raises(dc.CleanupDeferred, match="cache changed during cleanup"):
        action()


def test_clear_dir_keeps_busy_entries_and_cleans_the_rest(tmp_path, monkeypatch):
    import errno
    busy = mk(tmp_path, "busy", files={"still-active": "data"})
    mk(tmp_path, "idle", files={"cached": "data"})
    (tmp_path / "loose").write_text("data")
    real = dc.force_rmtree
    def remove(path):
        if path == busy:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        real(path)
    monkeypatch.setattr(dc, "force_rmtree", remove)
    with pytest.raises(dc.CleanupDeferred, match="1 busy cache entries left in place"):
        dc.clear_dir(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["busy"]
    assert (busy / "still-active").read_text() == "data"


def test_cleanup_groups_overlap_but_same_backend_is_serial(monkeypatch):
    import threading
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    barrier = threading.Barrier(2, timeout=5)
    first_done = threading.Event()
    def first():
        barrier.wait()
        first_done.set()
        return "done"
    def independent():
        barrier.wait()
        return "done"
    def second():
        assert first_done.is_set(), "same-backend operations must stay serial"
        return "done"
    items = [dc.Item("docker", "first", mode="action", action=first),
             dc.Item("docker", "second", mode="action", action=second),
             dc.Item("brew", "independent", mode="action", action=independent)]
    assert dc.clean_all(items, 2, []) == dc.CleanupReport(cleaned=3)


def test_incomplete_backend_is_skipped_without_blocking_other_groups(monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    def timeout():
        raise dc.CleanupDeferred("timed out")
    items = [dc.Item("docker", "slow", mode="action", action=timeout),
             dc.Item("docker", "later", mode="action", action=lambda: pytest.fail("no overlapping daemon prune")),
             dc.Item("go", "healthy", mode="action", action=lambda: "done")]
    assert dc.clean_all(items, 2, []) == dc.CleanupReport(cleaned=1, deferred=2)
    assert "skipped after an incomplete cleanup" in dc.console.file.getvalue()


def test_partial_sweep_does_not_skip_the_other_sweeps_of_its_group(monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    ran = []
    def partial():
        raise dc.CleanupPartial("removed 317, 19 could not be removed")
    items = [dc.Item("tmp", "idle temp files", mode="action", action=partial),
             dc.Item("tmp", "idle Claude Code scratch", mode="action", action=lambda: ran.append("scratch"))]
    assert dc.clean_all(items, 2, []) == dc.CleanupReport(cleaned=1, deferred=1)
    assert ran == ["scratch"]


def test_cleanup_cancellation_does_not_wait_for_command_deadline():
    import threading
    cancelled = threading.Event()
    item = dc.Item("docker", "slow", mode="action", action=dc._cmd_action(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=60))
    timer = threading.Timer(0.2, cancelled.set)
    start = time.monotonic()
    timer.start()
    try:
        _, _, error = dc._delete(item, [], cancelled)
        assert isinstance(error, dc.CleanupDeferred) and "cancelled" in str(error)
        assert time.monotonic() - start < 3
    finally:
        timer.cancel()
        dc._cleanup_thread.cancelled = None


def test_cleanup_failure_is_counted_separately_from_deferred(monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    def broken():
        raise RuntimeError("permission denied")
    result = dc.clean_all([dc.Item("go", "broken", mode="action", action=broken)], 1, [])
    assert result == dc.CleanupReport(failed=1)
    assert dc.clean_all([], 1, []) == dc.CleanupReport()


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf"])
def test_command_timeout_validation(monkeypatch, tmp_path, seconds):
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--command-timeout", seconds,
                                     "--config", str(tmp_path / "none.toml")])
    with pytest.raises(SystemExit) as error:
        dc.main()
    assert error.value.code == 2


def test_ctrl_c_cancels_concurrent_cleanup_clients(monkeypatch):
    import threading
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    started = threading.Event()
    command = dc._cmd_action([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
    def action():
        started.set()
        return command()
    def interrupt(*args, **kwargs):
        assert started.wait(2)
        raise KeyboardInterrupt
    monkeypatch.setattr(dc, "wait", interrupt)
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        dc.clean_all([dc.Item("docker", "slow", mode="action", action=action)], 2, [])
    assert time.monotonic() - start < 3


def test_docker_cleanup_actions_use_configured_timeout(monkeypatch):
    _docker(monkeypatch, {("system", "df", "--format", "{{json .}}"): DF})
    timeouts = []
    def command(cmd, timeout=60, **kwargs):
        timeouts.append(timeout)
        return lambda: None
    monkeypatch.setattr(dc, "_cmd_action", command)
    ctx = _docker_ctx()
    ctx.command_timeout = 17
    assert dc.scan_docker(ctx)
    assert timeouts and set(timeouts) == {17}


# --- fast inventory and stable terminal layout -------------------------------

def test_normal_scan_never_sizes_directories(monkeypatch):
    ctx = _docker_ctx()
    ctx.groups = {"go"}
    ctx.measure_sizes = False
    monkeypatch.setattr(dc, "GLOBAL_SCANNERS", {"go": lambda c: [dc.Item("go", "cache", Path("/cache"))]})
    monkeypatch.setattr(dc, "scan_known_caches", lambda c: [])
    monkeypatch.setattr(dc, "size_items", lambda *a, **kw: pytest.fail("normal cleanup must not recursively size caches"))
    [item] = dc.scan_all(ctx)
    assert item.size is None


def test_fast_docker_inventory_never_queries_disk_usage(monkeypatch):
    _docker(monkeypatch, {("buildx", "ls", "--format", "{{json .}}"): BUILDX})
    fake = dc.run
    def run(cmd, **kw):
        assert tuple(cmd[1:3]) not in {("system", "df"), ("buildx", "du")}
        return fake(cmd, **kw)
    monkeypatch.setattr(dc, "run", run)
    ctx = _docker_ctx()
    ctx.measure_sizes = False
    items = dc.scan_docker(ctx)
    assert any(item.label == "docker buildx prune (lane)" for item in items)
    assert all(item.size is None for item in items)
    assert not any("failed" in warning for warning in ctx.warnings)


def test_fast_temp_sweep_never_sizes_entries(tmp_path, monkeypatch):
    mk(tmp_path, "old", files={"data": "x"})
    _age(tmp_path / "old", 5)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    ctx.measure_sizes = False
    monkeypatch.setattr(dc, "paths_size", lambda *a: pytest.fail("sweeps must not start hidden size walks"))
    [item] = dc.scan_sweeps([dc.Sweep("extra", "cache", tmp_path, (tmp_path,))], ctx)
    assert item.size is None and "1 entries" in item.note


def test_timed_out_sizing_does_not_restart_in_python(monkeypatch):
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/usr/bin/du")
    monkeypatch.setattr(dc, "run", lambda *a, **kw: None)
    monkeypatch.setattr(dc.os, "walk", lambda *a, **kw: pytest.fail("do not repeat a timed-out size walk"))
    assert dc.dir_size(Path("/huge-cache")) is None


def test_scan_progress_rows_do_not_move_or_wrap():
    from io import StringIO
    from rich.console import Console
    c = Console(file=StringIO(), width=60)
    progress = dc.ScanProgress(console=c, auto_refresh=False)
    def snapshot():
        output = StringIO()
        Console(file=output, width=60).print(progress.get_renderable())
        return output.getvalue().splitlines()
    def check():
        lines = snapshot()
        assert len(lines) == 3
        assert lines[0].startswith("Projects")
        assert lines[1].startswith("Tools")
        assert lines[2].startswith("Sizes")
        assert all(len(line) <= 60 for line in lines)
    check()
    a = progress.add_task("checking docker", detail="x" * 500)
    b = progress.add_task("scanning ~ (100,000 dirs)", detail="/very/long/path/" * 100)
    check()
    progress.remove_task(a)
    check()
    progress.remove_task(b)
    progress.add_task("sizing 100 dir(s)", total=100, detail="/very/long/path/" * 100)
    check()


def test_cleanup_live_rows_are_fixed_and_results_print_afterwards(monkeypatch):
    from io import StringIO
    from rich.console import Console
    output = StringIO()
    monkeypatch.setattr(dc, "console", Console(file=output, width=60))
    frames = []
    render = None
    class CaptureLive:
        def __init__(self, *, get_renderable, **kwargs):
            nonlocal render
            render = get_renderable
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def capture():
        frame = StringIO()
        Console(file=frame, width=60).print(render())
        frames.append(frame.getvalue().splitlines())
        assert output.getvalue() == "", "completion logs must wait until the live display closes"
        return "done"
    monkeypatch.setattr(dc, "Live", CaptureLive)
    items = [dc.Item("go", "first " + "x" * 500, mode="action", action=capture),
             dc.Item("go", "second", mode="action", action=capture)]
    assert dc.clean_all(items, 2, [], verbose=True) == dc.CleanupReport(cleaned=2)
    assert frames and {len(frame) for frame in frames} == {3}  # heading plus two reserved slots
    assert all(len(line) <= 60 for frame in frames for line in frame)
    assert output.getvalue().index("first") < output.getvalue().index("second")


def test_unmeasured_total_is_not_presented_as_zero_bytes():
    from io import StringIO
    from rich.console import Console
    output = StringIO()
    Console(file=output, width=120).print(dc.render_table([dc.Item("go", "cache", Path("/cache"))], "1d", False))
    assert "not measured" in output.getvalue()
    assert "0.0 B" not in output.getvalue()


def test_project_discovery_budget_and_breadth_first(tmp_path, monkeypatch):
    deep = mk(tmp_path, "forest", "a", "b", "c")
    near = mk(tmp_path, "near", files={"Cargo.toml": ""})
    mk(near, "target", files={"artifact": "x"})
    visited = []
    list(dc.find_projects(tmp_path, visit=visited.append))
    assert visited.index(near.resolve()) < visited.index(deep.resolve())
    monkeypatch.setattr(dc.os, "scandir", lambda *a: pytest.fail("expired scans must not walk"))
    assert list(dc.find_projects(tmp_path, deadline=0)) == []


def test_empty_build_dirs_are_not_candidates(tmp_path):
    project = mk(tmp_path, "fixture", files={"Cargo.toml": ""})
    mk(project, "target")
    assert list(dc.find_projects(tmp_path)) == []


def test_expired_activity_cannot_qualify_for_cleanup(tmp_path):
    project = mk(tmp_path, "stale", files={"Cargo.toml": ""})
    _age(project, 30)
    ctx = _docker_ctx()
    ctx.cutoff = time.time() - 86400
    activity = dc.project_activity(project, ctx.cutoff, deadline=0)
    item = dc.Item("rust", "target/", project / "target", project=project, mtime=activity)
    clean, active, _ = dc.partition([item], ctx)
    assert clean == [] and active == [item]


def test_unreadable_activity_cannot_qualify_for_cleanup(tmp_path, monkeypatch):
    project = mk(tmp_path, "stale", files={"Cargo.toml": ""})
    _age(project, 30)
    def walk(*args, onerror, **kwargs):
        onerror(PermissionError("denied"))
        return iter([])
    monkeypatch.setattr(dc.os, "walk", walk)
    assert dc.project_activity(project, time.time() - 86400) == float("inf")


def test_caches_only_never_discovers_projects(monkeypatch):
    monkeypatch.setattr(dc, "scan_projects", lambda *a: pytest.fail("cache-only must not discover projects"))
    monkeypatch.setattr(dc, "GLOBAL_SCANNERS", {"rust": lambda ctx: [dc.Item("rust", "cache")]})
    monkeypatch.setattr(dc, "scan_known_caches", lambda ctx: [])
    ctx = _docker_ctx()
    ctx.groups = {"rust"}
    ctx.caches_only = True
    assert [i.label for i in dc.scan_all(ctx)] == ["cache"]


def test_scan_command_deadline_does_not_affect_cleanup(monkeypatch):
    import subprocess
    timeouts = []
    def run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(dc.subprocess, "run", run)
    dc._scan_thread.deadline = time.monotonic() + 0.5
    try:
        dc.run(["probe"], timeout=60)
        assert 0 < timeouts[-1] <= 0.5
        dc._scan_thread.deadline = 0
        assert dc.run(["probe"]) is None
        assert len(timeouts) == 1
    finally:
        dc._scan_thread.deadline = None
    dc.run(["probe"], timeout=60)
    assert timeouts[-1] == 60


def test_project_deletes_overlap_package_manager_action(tmp_path, monkeypatch):
    import threading
    barrier = threading.Barrier(3, timeout=5)
    def remove(item, roots, cancelled):
        barrier.wait()
        return item, None, None
    monkeypatch.setattr(dc, "_delete", remove)
    items = [dc.Item("node", "node_modules/", project=tmp_path / str(n)) for n in range(2)]
    items.append(dc.Item("node", "pnpm prune", mode="action"))
    assert dc.clean_all(items, 3, []) == dc.CleanupReport(cleaned=3)


def test_global_cache_is_scheduled_before_projects(tmp_path, monkeypatch):
    order = []
    def remove(item, roots, cancelled):
        order.append(item.label)
        return item, None, None
    monkeypatch.setattr(dc, "_delete", remove)
    items = [dc.Item("rust", "target/", project=tmp_path), dc.Item("go", "global cache")]
    dc.clean_all(items, 1, [])
    assert order == ["global cache", "target/"]


def test_compact_preview_and_cleanup_do_not_dump_paths(tmp_path, monkeypatch):
    from io import StringIO
    from rich.console import Console
    output = StringIO()
    monkeypatch.setattr(dc, "console", Console(file=output, width=80))
    items = [dc.Item("node", "node_modules/", project=tmp_path / ("very-long-path-" * 20) / str(n))
             for n in range(200)]
    dc.console.print(dc.render_summary(items, "1d", False))
    assert len(output.getvalue().splitlines()) < 10
    assert "200" in output.getvalue() and "very-long-path" not in output.getvalue()
    assert "0.0 B" not in output.getvalue()
    output.seek(0); output.truncate()
    monkeypatch.setattr(dc, "_delete", lambda item, roots, cancelled: (item, None, None))
    assert dc.clean_all(items, 4, []).cleaned == 200
    assert output.getvalue().split() == ["✓", "cleaned", "node", "200"]


def test_brew_does_not_repeat_cleanup_walk_without_sizes(monkeypatch):
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/bin/brew")
    monkeypatch.setattr(dc, "run", lambda *a, **kw: pytest.fail("no Homebrew dry-run walk"))
    ctx = _docker_ctx()
    ctx.measure_sizes = False
    items = dc.scan_brew(ctx)
    assert [(i.label, i.tier) for i in items] == [
        ("brew cleanup --prune=all", dc.SAFE), ("brew autoremove", dc.AGGRESSIVE)]


def test_partial_temp_cleanup_is_incomplete(tmp_path, monkeypatch):
    good = mk(tmp_path, "good", files={"old": "x"})
    bad = mk(tmp_path, "bad", files={"old": "x"})
    _age(tmp_path, 30)
    real = dc.force_rmtree
    def remove(path):
        if path == bad:
            raise PermissionError("denied")
        real(path)
    monkeypatch.setattr(dc, "force_rmtree", remove)
    with pytest.raises(dc.CleanupDeferred, match="removed 1, 1 could not be removed"):
        dc._sweep_action([good, bad], time.time() - 86400)()
    assert not good.exists() and bad.exists()


@pytest.mark.parametrize("flags,budget,caches_only", [
    ([], dc.SCAN_SECONDS, False),
    (["--thorough"], None, False),
    (["--caches-only"], dc.SCAN_SECONDS, True),
    (["--audit"], None, False),
])
def test_cli_scan_modes(monkeypatch, tmp_path, flags, budget, caches_only):
    captured = []
    monkeypatch.setattr(dc, "scan_all", lambda ctx: captured.append(ctx) or [])
    monkeypatch.setattr(dc, "run_audit", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--dry-run", "--config", str(tmp_path / "config"), *flags])
    assert dc.main() == 0
    assert captured[0].scan_seconds == budget
    assert captured[0].caches_only == caches_only


def test_budget_exhaustion_keeps_discovered_candidates_and_reports_partial_scan(tmp_path, monkeypatch):
    project = mk(tmp_path, "stale", files={"Cargo.toml": ""})
    target = mk(project, "target", files={"artifact": "x"})
    _age(project, 30)
    ticks = [0.0]
    monkeypatch.setattr(dc.time, "monotonic", lambda: ticks[0])
    def discover(*args, **kwargs):
        yield "rust", project, target, dc.BuildDir("target")
        ticks[0] = 11.0
    monkeypatch.setattr(dc, "find_projects", discover)
    ctx = _docker_ctx()
    ctx.roots = [tmp_path]
    ctx.groups = {"rust"}
    ctx.cutoff = time.time() - 86400
    with dc.make_progress() as progress:
        items = dc.scan_projects(ctx, progress)
    assert len(items) == 1
    assert dc.partition(items, ctx)[0] == []
    assert any("deeper projects were not seen" in warning for warning in ctx.warnings)
    assert any("incomplete activity checks" in warning for warning in ctx.warnings)


@pytest.mark.parametrize("free, setting, argv, expect_aggressive, expect_hours", [
    (500, '"auto"', [], False, 24),              # roomy: safe tier, configured age
    (100, '"auto"', [], True, 24),               # tight: dependency caches too
    (10, '"auto"', [], True, 6),                 # critical: and a shorter idle age
    (10, '"auto"', ["--hours", "48"], True, 48), # an explicit age is never overridden
    (10, '"auto"', ["--gentle"], False, 24),
    (10, "false", [], False, 24),                # pinned off in the config
    (500, "true", [], True, 24),
    (500, '"auto"', ["--aggressive"], True, 24),
])
def test_tier_follows_free_disk_unless_pinned(tmp_path, monkeypatch, free, setting, argv, expect_aggressive, expect_hours):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    gib = 1024**3
    level = "critical" if free < 50 else "tight" if free < 150 else "ok"
    monkeypatch.setattr(dc, "disk_pressure", lambda path=None: (level, free * gib, 1000 * gib))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"aggressive = {setting}\n")
    seen = {}
    def scan(ctx):
        seen["aggressive"], seen["hours"] = ctx.aggressive, round((time.time() - ctx.cutoff) / 3600)
        return []
    monkeypatch.setattr(dc, "scan_all", scan)
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--config", str(cfg), "--dry-run", *argv])
    assert dc.main() == 0
    assert seen == {"aggressive": expect_aggressive, "hours": expect_hours}


def test_disk_pressure_levels(monkeypatch):
    usage = lambda free: SimpleNamespace(total=1000, used=1000 - free, free=free)
    for free, level in ((500, "ok"), (149, "tight"), (49, "critical")):
        monkeypatch.setattr(dc.shutil, "disk_usage", lambda path, free=free: usage(free))
        assert _real_disk_pressure()[0] == level


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS clones")
def test_dedupe_shares_identical_files_and_keeps_each_ones_metadata(tmp_path, monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=120))
    monkeypatch.setattr(dc, "DEDUPE_SETTLE", -60)  # the fixtures were written just now
    body = os.urandom(64 * 1024)
    a = mk(tmp_path, "w1", "node_modules", files={})
    b = mk(tmp_path, "w2", "node_modules", files={})
    for d in (a, b):
        (d / "lib.js").write_bytes(body)
    (b / "other.js").write_bytes(os.urandom(64 * 1024))        # same size, different content
    (b / "linked.js").write_bytes(body)
    os.link(b / "linked.js", b / "linked-twin.js")              # hard links are never touched
    mk(tmp_path, "w2", ".git", files={})
    (tmp_path / "w2" / ".git" / "pack").write_bytes(body)        # VCS internals neither
    os.chmod(b / "lib.js", 0o640)
    os.utime(b / "lib.js", ns=(10**18, 10**18))
    os.utime(b, ns=(2 * 10**18, 2 * 10**18))
    other, git_ino, linked_ino = (b / "other.js").read_bytes(), (tmp_path / "w2/.git/pack").stat().st_ino, (b / "linked.js").stat().st_ino
    inodes = {(a / "lib.js").stat().st_ino, (b / "lib.js").stat().st_ino}

    assert dc.run_dedupe([tmp_path], 16 * 1024, 2, dry_run=True, yes=True)
    assert {(a / "lib.js").stat().st_ino, (b / "lib.js").stat().st_ino} == inodes  # dry run changes nothing

    assert dc.run_dedupe([tmp_path], 16 * 1024, 2, dry_run=False, yes=True)
    assert "1 files now share" in dc.console.file.getvalue()
    assert {(a / "lib.js").stat().st_ino, (b / "lib.js").stat().st_ino} != inodes  # one of the two was replaced
    for f in (a / "lib.js", b / "lib.js"):
        assert f.read_bytes() == body
    st = (b / "lib.js").stat() if (b / "lib.js").stat().st_mtime_ns == 10**18 else None
    if st is not None:  # w2's copy was the one replaced: its own mode and times survive, and so does its dir's
        assert st.st_mode & 0o777 == 0o640 and b.stat().st_mtime_ns == 2 * 10**18
    assert (b / "other.js").read_bytes() == other
    assert (b / "linked.js").stat().st_ino == linked_ino and (tmp_path / "w2/.git/pack").stat().st_ino == git_ino
    assert not [f for f in b.iterdir() if f.name.startswith(".dev-cleaner-clone")]
    # copy-on-write: writing one copy leaves the other intact
    (a / "lib.js").write_bytes(b"changed")
    assert (b / "lib.js").read_bytes() == body


def test_dedupe_leaves_a_file_that_changed_after_hashing(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"x" * 100)
    stale = os.lstat(b)
    b.write_bytes(b"y" * 100)
    os.utime(b, ns=(1, 1))
    assert dc._share(str(a), os.lstat(a), str(b), stale) is False
    assert b.read_bytes() == b"y" * 100


# ---------------------------------------------------------------------------
# Review screen and unattended output
# ---------------------------------------------------------------------------


def _picker(**kw):
    plan = [dc.Item("node", "node_modules", path=Path("/w/a/node_modules"), project=Path("/w/a"), mtime=0, size=3 * 1024**3),
            dc.Item("node", "node_modules", path=Path("/w/b/node_modules"), project=Path("/w/b"), mtime=0, size=1024**3),
            dc.Item("node", "pnpm store prune", mode="action", shadowed=True),
            dc.Item("go", "go build cache", path=Path("/c/go-build"))]
    extra = [dc.Item("node", "npm cache", path=Path("/c/npm"), tier=dc.AGGRESSIVE)]
    return dc.Picker(plan, extra, threshold="1d", **kw), plan, extra


def _keys(picker, *keys):
    for key in keys:
        picker.key(key)


def test_picker_starts_with_the_plan_and_needs_y_to_confirm():
    picker, plan, _ = _picker()
    assert picker.selected() == plan
    _keys(picker, "enter", "x")                     # any key but y backs out of the confirmation
    assert not picker.done and not picker.confirming
    _keys(picker, "enter", "y")
    assert picker.done and picker.result == plan


def test_picker_quit_returns_nothing_and_empty_selection_cannot_be_confirmed():
    picker, _, _ = _picker()
    _keys(picker, "n", "enter")
    assert not picker.confirming and picker.message == "nothing is selected"
    picker.key("q")
    assert picker.done and picker.result is None


def test_picker_toggles_groups_targets_and_the_aggressive_tier():
    picker, plan, extra = _picker()
    assert list(picker.groups) == ["node", "go"]     # biggest measured group first
    picker.key("space")                              # the node group off
    assert picker.selected() == [plan[3]]
    picker.key("space")                              # back on restores the plan, not the opt-in cache
    assert picker.selected() == plan
    _keys(picker, "right", "down", "space")          # open the group, untick its first target
    assert plan[0] not in picker.selected() and plan[1] in picker.selected()
    picker.key("a")                                  # the full cache clear replaces the prune of that cache
    assert extra[0] in picker.selected() and plan[2] not in picker.selected()
    picker.key("d")
    assert picker.selected() == plan
    _keys(picker, "left")
    assert picker.rows()[picker.cursor] == ("node", None) and "node" not in picker.open


def test_picker_renders_within_the_terminal_and_keeps_the_cursor_visible():
    from io import StringIO
    from rich.console import Console
    picker, _, _ = _picker(notes=["projects: search stopped at its 10s budget"], can_size=True)
    _keys(picker, "right", "end")
    out = StringIO()
    Console(file=out, width=80, height=12, force_terminal=False).print(picker.render(80, 12))
    text = out.getvalue()
    assert "❯" in text and "go build cache" in text and "above" in text
    assert "Selected 4 of 5 targets" in text and "s measure sizes" in " ".join(text.split())
    assert all(len(line) <= 80 for line in text.splitlines()) and len(text.splitlines()) <= 12


def _run_main(monkeypatch, tmp_path, argv, items, *, tty=False):
    from io import StringIO
    from rich.console import Console
    cfg = tmp_path / "config.toml"
    cfg.write_text('roots = []\naggressive = false\n')
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=100))
    monkeypatch.setattr(dc, "scan_all", lambda ctx: items)
    monkeypatch.setattr(dc.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--config", str(cfg), *argv])
    return dc.main()


def test_without_a_terminal_nothing_is_cleaned_unless_yes(monkeypatch, tmp_path):
    ran = []
    items = [dc.Item("go", "go build cache", mode="action", action=lambda: ran.append(1) or "ok")]
    assert _run_main(monkeypatch, tmp_path, [], items) == 2
    assert not ran and "Pass --yes" in dc.console.file.getvalue()
    assert _run_main(monkeypatch, tmp_path, ["--yes"], items) == 0
    assert ran == [1]


def test_json_reports_the_plan_then_each_result(monkeypatch, tmp_path, capsys):
    import json
    ran = []
    def busy():
        raise dc.CleanupDeferred("in use")
    items = [dc.Item("go", "go build cache", mode="action", action=lambda: ran.append(1) or "ok"),
             dc.Item("brew", "brew cleanup", mode="action", action=busy),
             dc.Item("node", "npm cache", path=tmp_path / "npm", tier=dc.AGGRESSIVE)]
    assert _run_main(monkeypatch, tmp_path, ["--json"], items) == 0
    plan = json.loads(capsys.readouterr().out)        # stdout is the JSON object and nothing else
    assert plan["dry_run"] and plan["result"] is None and not ran
    assert [t["status"] for t in plan["targets"]] == ["planned", "planned"]
    assert plan["left_alone"][0]["label"] == "npm cache" and plan["left_alone"][0]["reason"] == "needs --aggressive"

    assert _run_main(monkeypatch, tmp_path, ["--json", "--yes"], items) == 1
    done = json.loads(capsys.readouterr().out)
    assert {t["label"]: (t["status"], t["detail"]) for t in done["targets"]} == {
        "go build cache": ("cleaned", "ok"), "brew cleanup": ("incomplete", "in use")}
    assert done["result"]["cleaned"] == 1 and done["result"]["incomplete"] == 1 and not done["dry_run"]


def test_review_screen_decides_what_is_cleaned(monkeypatch, tmp_path):
    ran = []
    items = [dc.Item("go", "go build cache", mode="action", action=lambda: ran.append("go") or None),
             dc.Item("brew", "brew cleanup", mode="action", action=lambda: ran.append("brew") or None)]
    monkeypatch.setattr(dc, "can_interact", lambda: True)
    monkeypatch.setattr(dc, "pick", lambda picker, measure=None: None)
    assert _run_main(monkeypatch, tmp_path, [], items, tty=True) == 0 and not ran
    monkeypatch.setattr(dc, "pick", lambda picker, measure=None: [picker.choices[1].item])
    assert _run_main(monkeypatch, tmp_path, [], items, tty=True) == 0
    assert ran == ["brew"]
    # --plain and --yes never open the review screen
    monkeypatch.setattr(dc, "pick", lambda *a, **k: pytest.fail("review screen opened"))
    assert _run_main(monkeypatch, tmp_path, ["--yes"], items, tty=True) == 0


def test_health_line_keeps_its_numbers_on_a_narrow_terminal(monkeypatch):
    from io import StringIO
    from rich.console import Console
    monkeypatch.setattr(dc, "console", Console(file=StringIO(), width=80))
    dc.console.print(dc.health_grid(disk_was=271 * 1024**3))
    disk = next(line for line in dc.console.file.getvalue().splitlines() if "disk" in line)
    assert "…" not in disk and disk.rstrip().endswith("free")


@pytest.mark.parametrize("free_gib, should_scan", [(39, True), (40, False), (41, False)])
def test_free_space_guard(monkeypatch, tmp_path, capsys, free_gib, should_scan):
    monkeypatch.setattr(dc, "disk_pressure", lambda: ("tight", free_gib * 1024**3, 500 * 1024**3))
    scans = []
    monkeypatch.setattr(dc, "scan_all", lambda ctx: scans.append(ctx) or [])
    monkeypatch.setattr(sys, "argv", ["dev-cleaner", "--config", str(tmp_path / "empty.toml"),
                                     "--below-free-gib", "40", "--json", "--yes", "--dry-run"])
    assert dc.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    if not should_scan:
        assert report["disk"]["free_bytes"] == free_gib * 1024**3
    assert bool(scans) == should_scan
    assert (report.get("skipped_reason") == "enough_free_space") == (not should_scan)
