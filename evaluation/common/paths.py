"""按评测类型保存运行产物，并安全地查找新旧运行记录。"""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "pyproject.toml").is_file() and (parent / "backend").is_dir()
)


def reports_dir(project_root: Path) -> Path:
    return project_root / "evaluation/reports"


def validate_run_label(value: str | None) -> None:
    """标签不接受路径；Agent 标签仅写入清单，目录名由模型与时间生成。"""
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}", value)
    ):
        raise ValueError("运行标签最多 100 字符，仅允许字母数字、点、短横线和下划线，且不能以点开头")


def _model_name(model: str) -> str:
    """将模型名称转换为单个目录名，兼容供应商前缀和长模型名。"""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("._-") or "model"
    if len(name) > 80:
        name = name[:70] + "-" + hashlib.sha256(model.encode()).hexdigest()[:8]
    return name


def _check_path(project_root: Path, path: Path) -> None:
    """不沿项目内的符号链接读写报告，避免分类目录指向项目之外。"""
    current = project_root
    for part in path.relative_to(project_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("评测目录不能使用符号链接")


def create_run_dir(
    project_root: Path, suite: str, *, model: str, execute: bool,
    run_id: str | None = None, created_at: str | None = None,
) -> Path:
    """正式结果按 RAG/Agent 分类；预览和工程检查留在本地运行目录。"""
    validate_run_label(run_id)
    instant = datetime.fromisoformat(created_at) if created_at else datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("评测创建时间必须包含时区")
    stamp = instant.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d_%H-%M-%S")
    if suite in {"contracts", "business-coverage"}:
        parent = project_root / ".run/checks"
        name = run_id or f"{suite}_{stamp}"
    elif suite in {"rag", "dialogue", "text"}:
        category = "rag" if suite == "rag" else "agent"
        base = reports_dir(project_root) if execute else project_root / ".run/evaluation-previews"
        parent = base / category
        name = run_id if suite == "rag" and run_id else f"{_model_name(model)}_{stamp}"
    else:
        raise ValueError("未知评测类型")
    _check_path(project_root, parent)
    parent.mkdir(parents=True, exist_ok=True)
    # 显式指定的 RAG/工程目录不能覆盖；自动命名的同秒运行追加序号。
    explicit = run_id is not None and suite in {"rag", "contracts", "business-coverage"}
    for number in range(1, 10001):
        output = parent / (name if number == 1 else f"{name}_{number}")
        _check_path(project_root, output)
        try:
            output.mkdir()
            return output
        except FileExistsError:
            if explicit:
                raise FileExistsError("目标评测目录已经存在，请使用新的 --run 名称") from None
    raise FileExistsError("当前时间下的评测目录过多，请稍后重试")


def resolve_run_dir(
    project_root: Path, reference: str, *, category: str | None = None,
) -> Path | None:
    """支持分类目录或唯一运行名，兼容旧平铺记录；歧义时要求明确分类。"""
    if category not in {None, "agent", "rag"}:
        raise ValueError("未知评测分类")
    parts = reference.split("/") if isinstance(reference, str) else []
    if (len(parts) not in {1, 2}
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,149}", parts[-1])
            or (len(parts) == 2 and parts[0] not in {"rag", "agent"})):
        raise ValueError("请使用运行名称或 rag/运行名称、agent/运行名称")
    root = reports_dir(project_root)
    if len(parts) == 2:
        if category is not None and parts[0] != category:
            raise ValueError("来源评测分类不匹配")
        candidates = [root / reference]
    else:
        categories = (category,) if category else ("agent", "rag")
        candidates = [root / item / reference for item in categories]
        candidates += [root / reference, project_root / "docs/reports" / reference]
    matches = []
    for path in candidates:
        _check_path(project_root, path)
        if path.is_dir() and path not in (root / "agent", root / "rag"):
            matches.append(path)
    if len(matches) > 1:
        raise ValueError("运行名称对应多个目录，请使用 agent/运行名称或 rag/运行名称")
    return matches[0] if matches else None


def report_files(project_root: Path, filename: str) -> list[Path]:
    """查找已保存的产物；调用方仍需校验其原始指纹。"""
    root = reports_dir(project_root)
    files = []
    for pattern in (f"agent/*/{filename}", f"rag/*/{filename}", f"*/{filename}"):
        for path in root.glob(pattern):
            _check_path(project_root, path)
            if path.is_file():
                files.append(path)
    return sorted(set(files), reverse=True)
