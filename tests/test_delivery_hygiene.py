"""交付卫生硬门禁：死代码、占位实现、模块可达性与构建残留。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "glassmelt"

# 构建残留标记由片段拼出，避免把该类字样写进仓库本身。
FORBIDDEN_TOKEN = "".join(("ben", "zhi"))
PLACEHOLDERS = ("TODO", "FIXME", "XXX", "NotImplementedError")


def _deadcode_report() -> dict:
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "deadcode.py"), os.path.join(ROOT, "glassmelt")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_deadcode_gate_reports_no_unused_symbols():
    report = _deadcode_report()
    assert report["unused_symbols"] == []
    assert report["unreachable_modules"] == []
    assert report["files"] >= 50
    assert report["symbols"] >= 200


def test_production_scale_meets_the_capacity_target():
    total = 0
    files = 0
    for path in PACKAGE.rglob("*.py"):
        files += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith('"""'):
                continue
            total += 1
    assert files >= 50
    assert total >= 2600


def test_every_package_directory_has_an_init_module():
    missing = [
        str(path.relative_to(ROOT))
        for path in PACKAGE.rglob("*")
        if path.is_dir() and "__pycache__" not in path.parts and not (path / "__init__.py").is_file()
    ]
    assert missing == []


def test_pyproject_package_list_matches_tree():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    packages = [
        line.strip().strip('",')
        for line in pyproject.splitlines()
        if line.strip().startswith('"glassmelt')
    ]
    assert "glassmelt" in packages
    for package in packages:
        path = ROOT / package.replace(".", "/")
        assert path.is_dir(), package
        assert (path / "__init__.py").is_file(), package


def test_delivery_tree_carries_no_build_leftovers():
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        parts = set(path.relative_to(ROOT).parts)
        if parts & {".git", "__pycache__", ".pytest_cache", "var", "vendor"}:
            continue
        if path.is_dir():
            continue
        lowered = path.name.lower()
        if FORBIDDEN_TOKEN in lowered:
            offenders.append(f"文件名: {path.relative_to(ROOT)}")
            continue
        if path.suffix in {".pyc", ".png", ".jpg", ".xlsx", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lowered_text = text.lower()
        if FORBIDDEN_TOKEN in lowered_text:
            offenders.append(f"文件内容: {path.relative_to(ROOT)}")
        if path.parent == ROOT and lowered.startswith(("build_", "build-")):
            offenders.append(f"专用构建助手: {path.relative_to(ROOT)}")
    assert offenders == [], f"交付树混入构建残留: {offenders}"


def test_shell_scripts_are_not_shipped():
    scripts = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.sh")
        if ".git" not in path.parts and "vendor" not in path.parts
    ]
    assert scripts == [], f"交付树不应带 shell 构建脚本: {scripts}"


def test_source_tree_has_no_placeholder_markers():
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for marker in PLACEHOLDERS:
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert offenders == [], f"生产代码不应残留占位实现: {offenders}"
