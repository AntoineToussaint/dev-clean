"""Unit tests for dev_cleaner. Run with:  uv run --with pytest --with rich pytest -q"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

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
        "procs": {"orphan_dirs": []},
    }


def test_completions_mention_every_group():
    for shell, script in dc.COMPLETIONS.items():
        for g in dc.ALL_GROUPS:
            assert g in script, f"{shell} completion missing group {g}"
        assert "dev-cleaner" in script


def test_group_help_covers_every_group():
    assert set(dc.GROUP_HELP) == set(dc.ALL_GROUPS)
    assert set(dc.GROUP_EMOJI) == set(dc.ALL_GROUPS)


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
    assert "1 entries" in item.note and item.size > 0
    assert item.action() == "removed 1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["com.apple.thing", "new"]


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


# --- audit -------------------------------------------------------------------

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
                      aggressive=False, docker_volumes=volumes, size_jobs=2)


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
    out = ("41 1 501 02-01:20:38 2.5 4096 Wed Oct 21 12:30:00 2026 /Applications/My App/server\n"
           "42 41 501 00:01 0.0 1024 Sun Oct 25 12:30:00 2026 /usr/bin/worker\n"
           "malformed line\n")
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    a, b = dc.list_procs()
    assert a.exe == "/Applications/My App/server"
    assert a.started == "Wed Oct 21 12:30:00 2026"
    assert a.mem == 2 * 1024**3 and a.footprint
    assert a.age == 2 * 86400 + 4838 and a.cpu == 2.5
    assert b.mem == 1024**2 and not b.footprint


def test_linux_process_executable_resolution(monkeypatch):
    import subprocess
    monkeypatch.setattr(dc, "IS_MAC", False)
    monkeypatch.setattr(dc.os, "readlink", lambda p: "/opt/my app/server")
    out = "41 1 501 00:01 0.0 1024 Sun Oct 25 12:30:00 2026 truncated-comm\n"
    monkeypatch.setattr(dc, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, ""))
    [p] = dc.list_procs()
    assert p.exe == "/opt/my app/server" and not p.footprint


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
    [note] = dc.render_notes([item], [], "1d", [])
    assert "sizes skipped; use --audit" in note.plain
    assert "holding" not in note.plain


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
