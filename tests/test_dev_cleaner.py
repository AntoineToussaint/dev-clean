"""Unit tests for dev_cleaner. Run with:  uv run --with pytest --with rich pytest -q"""

from __future__ import annotations

import os
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
    items = [
        dc.Item("rust", "target/", tmp_path / "a", 10, dc.SAFE, mtime=stale, project=tmp_path),
        dc.Item("rust", "target/", tmp_path / "b", 20, dc.SAFE, mtime=time.time(), project=tmp_path),
        dc.Item("go", "go module cache", tmp_path / "c", 30, dc.AGGRESSIVE),
        dc.Item("python", "uv cache prune", tmp_path / "d", None, dc.SAFE, shadowed=True, mode="action"),
    ]
    to_clean, active, extra = dc.partition(items, ctx)
    assert [i.label for i in to_clean] == ["target/", "uv cache prune"]
    assert [i.size for i in active] == [20]
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
    }


def test_completions_mention_every_group():
    for shell, script in dc.COMPLETIONS.items():
        for g in dc.ALL_GROUPS:
            assert g in script, f"{shell} completion missing group {g}"
        assert "dev-cleaner" in script


def test_group_help_covers_every_group():
    assert set(dc.GROUP_HELP) == set(dc.ALL_GROUPS)
    assert set(dc.GROUP_EMOJI) == set(dc.ALL_GROUPS)
