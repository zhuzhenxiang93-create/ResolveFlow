"""执行 ResolveFlow 数据资产的可复现静态校验。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset_loader import validate_data_assets


def main() -> None:
    errors = validate_data_assets(ROOT / "data")
    if errors:
        for error in errors:
            print(error)
        raise SystemExit(f"数据资产校验失败: {len(errors)} 项")
    print("数据资产校验通过")


if __name__ == "__main__":
    main()
