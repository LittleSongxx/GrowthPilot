"""提供稳定的测试夹具根目录，不受测试模块嵌套层级影响。"""

from pathlib import Path

ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file() and (parent / "backend").is_dir()
)
