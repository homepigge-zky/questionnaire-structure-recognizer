from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from questionnaire_structure_recognizer.api.app import Store


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = PROJECT_ROOT / "data"
BUSINESS_DIRECTORIES = ("files", "results", "cache", "diagnostics", "exports", "tmp", "backups", "logs")
DATABASE_FILES = ("qsr.db", "qsr.db-wal", "qsr.db-shm", "app.db", "app.db-wal", "app.db-shm")
PROJECT_RUNTIME_ARTIFACTS = (
    ".acceptance-data", ".knowledge-api-acceptance", ".tmp-e2e",
    ".tmp-workflow-check-2", ".ui-workflow-data", ".tmp-manufacturing-regression.docx",
    "evaluation/actual-standard-render", "evaluation/actual-standard-v4-render",
    "evaluation/actual-template-v5-render", "evaluation/filled-v7-render",
    "evaluation/original-render", "evaluation/workflow-acceptance-20260909",
    "evaluation/actual_knowledge_draft_import.json", "evaluation/actual_upload_tree_result.json",
    "evaluation/actual_workflow_exports.json", "evaluation/filled_docx_manifest.json",
)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def reset_business_data(data_dir: Path, confirmed: bool = False) -> dict:
    data = data_dir.resolve()
    if not confirmed:
        raise RuntimeError("必须显式传入 --yes-reset-business-data")
    if data.name != "data" or not _inside(data, PROJECT_ROOT):
        raise RuntimeError(f"拒绝清理项目外或名称异常的目录：{data}")

    removed_files = 0
    for name in BUSINESS_DIRECTORIES:
        directory = (data / name).resolve()
        if not _inside(directory, data):
            raise RuntimeError(f"目录越界：{directory}")
        if directory.exists():
            removed_files += sum(1 for item in directory.rglob("*") if item.is_file())
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    removed_databases = 0
    for name in DATABASE_FILES:
        path = (data / name).resolve()
        if not _inside(path, data):
            raise RuntimeError(f"数据库路径越界：{path}")
        if path.exists():
            path.unlink()
            removed_databases += 1

    removed_artifacts = 0
    for relative in PROJECT_RUNTIME_ARTIFACTS:
        path = (PROJECT_ROOT / relative).resolve()
        if not _inside(path, PROJECT_ROOT):
            raise RuntimeError(f"项目产物路径越界：{path}")
        if path.is_dir():
            removed_artifacts += sum(1 for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
            removed_artifacts += 1

    # Recreate schema only. Store never inserts demo or business rows.
    store = Store(data, "", auth_mode="local")
    store.pool.shutdown(wait=False, cancel_futures=True)
    with store.connect() as db:
        counts = {
            row["name"]: db.execute(f'SELECT COUNT(*) FROM "{row["name"]}"').fetchone()[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
    if any(counts.values()):
        raise RuntimeError(f"重建后的数据库不是空库：{counts}")
    return {"data_dir": str(data), "removed_files": removed_files, "removed_databases": removed_databases, "removed_artifacts": removed_artifacts, "table_counts": counts}


def main() -> None:
    parser = argparse.ArgumentParser(description="一次性清空问卷结构识别器的业务数据")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--yes-reset-business-data", action="store_true")
    args = parser.parse_args()
    result = reset_business_data(args.data_dir, args.yes_reset_business_data)
    print(f"已重置：{result['data_dir']}")
    print(f"删除业务目录文件：{result['removed_files']}；删除数据库文件：{result['removed_databases']}；删除旧验收产物：{result['removed_artifacts']}")
    print("所有业务表记录数：0")


if __name__ == "__main__":
    main()
