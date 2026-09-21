"""The benchmark dataset lives outside the repository: `UE_BENCH_KNOWLEDGE_DIR` names its directory, the
in-repo path (a symlink on the machine that builds it) is the default, and a clone without it gets one line
naming the command that builds what it lacks, never a traceback. Nothing here opens the store."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from uranium_explorer.analyst import frozen as F
from uranium_explorer.bench import build as AB
from uranium_explorer.bench import dataset as D
from uranium_explorer.bench.cli import bench_app
from uranium_explorer.bench.interface import build as B
from uranium_explorer.paths import PATHS
from fake_bench import make_bench


def test_the_dataset_directory_is_the_in_repo_path_unless_the_environment_names_another(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(D.ENV, raising=False)
    assert D.knowledge_dir() == PATHS.pipeline / "knowledge" / "bench"
    assert B.interface_dir("v1") == PATHS.pipeline / "knowledge" / "bench" / "interface" / "v1"
    monkeypatch.setenv(D.ENV, str(tmp_path))
    assert D.knowledge_dir() == tmp_path                                  # read at call time, not at import
    assert B.interface_dir("v1") == tmp_path / "interface" / "v1"
    assert not B.is_built("v1")


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A public clone: the dataset directory is empty and no benchmark has been built."""
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    monkeypatch.setenv(D.ENV, str(dataset))
    monkeypatch.setattr(AB, "bench_dir", lambda version, root=None: (root or tmp_path / "built") / version)
    monkeypatch.setattr(B, "analyst_data_dir", lambda version: tmp_path / "built" / version)
    monkeypatch.setattr(F, "bench_root", lambda: tmp_path / "built")
    return dataset


def test_a_clone_without_the_dataset_gets_one_line_naming_the_build(clone, tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match=r"ue bench build --version v2"):
        B.analyst_dir("v2")
    with pytest.raises(FileNotFoundError, match=r"ue bench build --version v2"):
        F.load_bench("v2")
    with pytest.raises(FileNotFoundError, match=r"ue bench interface build --version v1"):
        B.load_items("v1", 1)
    (problem,) = B.audit("v1")
    assert "ue bench interface build --version v1" in problem and D.ENV in problem
    (problem,) = AB.audit("v2", root=tmp_path / "built")
    assert "ue bench build --version v2" in problem
    lines: list[str] = []
    assert B.show("v1", log=lines.append) == {} and "ue bench interface build --version v1" in lines[0]


def _text(result) -> str:
    """stdout and stderr together, whichever way this click version keeps them."""
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def test_the_cli_prints_that_line_and_exits_instead_of_a_traceback(clone) -> None:
    runner = CliRunner()
    r = runner.invoke(bench_app, ["interface", "build", "--version", "v1"])  # the real spec draws on analyst v2
    assert r.exit_code == 2, _text(r)
    assert "ue bench build --version v2" in _text(r) and "Traceback" not in _text(r)
    r = runner.invoke(bench_app, ["interface", "audit", "--version", "v1"])
    assert r.exit_code == 1 and "ue bench interface build --version v1" in _text(r)
    r = runner.invoke(bench_app, ["show", "--version", "v2"])
    assert r.exit_code == 0 and "ue bench build --version v2" in _text(r)


def test_the_scorer_reads_the_dataset_copy_when_the_build_is_absent_and_the_build_when_it_is_there(tmp_path, monkeypatch) -> None:
    copy = make_bench(tmp_path / "dataset")                # cells, key, held-out list and manifest, as the dataset holds them
    monkeypatch.setenv(D.ENV, str(copy.root))
    monkeypatch.setattr(F, "bench_root", lambda: tmp_path / "built")
    assert F.bench_dir(copy.version) == copy.dir
    assert F.load_bench(copy.version).key == copy.key
    built = make_bench(tmp_path / "b")
    monkeypatch.setattr(F, "bench_root", lambda: built.root)
    assert F.bench_dir(built.version) == built.dir           # the build carries the packs; it wins when present
