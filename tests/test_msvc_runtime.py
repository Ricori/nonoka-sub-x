from pathlib import Path
from types import SimpleNamespace

import pytest

from finesub_bootstrap.environment import RuntimeEnvironment
import nonoka_x.runtime_install as runtime_install
from nonoka_x.msvc_runtime import (
    REDIST_URL,
    explain_import_failure,
    explaining,
    msvc_runtime_problem,
)
from nonoka_x.runtime_install import ProgressRuntimeEnvironment

C10_FAILURE = (
    'OSError: [WinError 1114] Error loading "H:\\Program\\Nonoka X\\finesub\\runtime\\python.staging'
    '\\Lib\\site-packages\\torch\\lib\\c10.dll" or one of its dependencies.'
)


def _windows(tmp_path: Path, *, dll: bool = True) -> Path:
    system32 = tmp_path / "System32"
    system32.mkdir()
    if dll:
        (system32 / "msvcp140.dll").write_bytes(b"MZ")
    return tmp_path


def test_only_windows_is_checked(tmp_path: Path) -> None:
    assert msvc_runtime_problem(platform="darwin", system_root=tmp_path) is None


def test_a_missing_msvcp140_is_refused_with_the_redistributable_link(tmp_path: Path) -> None:
    problem = msvc_runtime_problem(platform="win32", system_root=_windows(tmp_path, dll=False))
    assert problem is not None
    assert "msvcp140.dll" in problem
    assert REDIST_URL in problem


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ((14, 29, 30139, 0), True),
        ((14, 38, 33135, 0), True),
        ((14, 40, 33810, 0), False),
        ((14, 44, 35211, 0), False),
    ],
)
def test_an_old_msvcp140_is_refused(tmp_path: Path, version: tuple[int, ...], refused: bool) -> None:
    problem = msvc_runtime_problem(
        platform="win32", system_root=_windows(tmp_path), read_version=lambda path: version
    )
    assert (problem is not None) is refused
    if refused:
        assert ".".join(map(str, version)) in problem


def test_an_unreadable_version_does_not_block_the_install(tmp_path: Path) -> None:
    root = _windows(tmp_path)
    assert msvc_runtime_problem(platform="win32", system_root=root, read_version=lambda path: None) is None

    def unreadable(path: Path):
        raise OSError("access denied")

    assert msvc_runtime_problem(platform="win32", system_root=root, read_version=unreadable) is None


def test_a_dll_load_failure_says_what_to_do_and_keeps_the_original() -> None:
    explained = explain_import_failure(C10_FAILURE)
    assert REDIST_URL in explained
    assert C10_FAILURE in explained
    assert explain_import_failure("[WinError 126] 找不到指定的模块。").startswith("PyTorch")


def test_other_import_failures_pass_through_unchanged() -> None:
    detail = "ModuleNotFoundError: No module named 'silero_vad'"
    assert explain_import_failure(detail) == detail
    validate = explaining(lambda python: (False, f"Python 运行环境缺少必需依赖：{detail}"))
    assert validate(Path("python.exe")) == (False, f"Python 运行环境缺少必需依赖：{detail}")
    assert explaining(lambda python: (True, ""))(Path("python.exe")) == (True, "")


def test_the_validator_rewords_a_c10_failure() -> None:
    healthy, detail = explaining(lambda python: (False, f"Python 运行环境缺少必需依赖：{C10_FAILURE}"))(
        Path("python.exe")
    )
    assert not healthy
    assert REDIST_URL in detail


def _bare_runtime(tmp_path: Path, state: str) -> ProgressRuntimeEnvironment:
    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(runtime=tmp_path / "runtime", cache=tmp_path / "cache")
    runtime.development_python = None
    runtime.condition = lambda: "missing"
    runtime.status = lambda: SimpleNamespace(state=state)
    return runtime


def test_an_install_without_a_usable_vc_runtime_stops_before_downloading(tmp_path: Path, monkeypatch) -> None:
    rebuilt: list[bool] = []
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda: "缺少 VC++ 运行库")
    monkeypatch.setattr(
        RuntimeEnvironment, "_install_locked", lambda self, uv, **kwargs: rebuilt.append(True) or "built"
    )

    runtime = _bare_runtime(tmp_path, "missing")
    with pytest.raises(RuntimeError, match="缺少 VC\\+\\+ 运行库"):
        runtime._install_locked(Path("uv.exe"), stage=None, log=None, should_pause=None)
    assert rebuilt == []

    # A runtime that already works is not taken away by the check.
    ready = _bare_runtime(tmp_path, "ready")
    assert ready._install_locked(Path("uv.exe"), stage=None, log=None, should_pause=None) == "built"
    with pytest.raises(RuntimeError):
        ready._install_locked(Path("uv.exe"), stage=None, log=None, should_pause=None, force=True)
