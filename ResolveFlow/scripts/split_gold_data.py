"""按稳定样本 ID 生成 dev/holdout 数据切分清单。"""
import hashlib
import json
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "data" / "golden"
SPLITS = ROOT / "data" / "splits"


def _golden_ids() -> List[str]:
    ids: List[str] = []
    for path in sorted(GOLDEN.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ids.append(json.loads(line)["id"])
    return ids


def main() -> None:
    ids = _golden_ids()
    if len(ids) != len(set(ids)):
        raise ValueError("golden 数据存在重复 ID，无法切分")
    dev = []
    holdout = []
    for item in ids:
        bucket = int(hashlib.sha256(item.encode("utf-8")).hexdigest(), 16) % 10
        (dev if bucket < 7 else holdout).append(item)
    if not dev or not holdout:
        raise ValueError("数据切分为空")
    SPLITS.mkdir(parents=True, exist_ok=True)
    (SPLITS / "dev_ids.json").write_text(json.dumps(sorted(dev), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (SPLITS / "holdout_ids.json").write_text(json.dumps(sorted(holdout), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"split gold data: dev={len(dev)} holdout={len(holdout)}")


if __name__ == "__main__":
    main()
