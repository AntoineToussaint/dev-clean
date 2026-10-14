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
        "skip": ["docker"], "size_jobs": 16, "clean_jobs": 4, "docker": {"volumes": True},
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
    monkeypatch.setattr(dc, "run", FakeDocker(outputs, set(fail)))


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
