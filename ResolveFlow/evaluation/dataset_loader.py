"""ResolveFlow 金标数据集的读取与静态校验。"""
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REQUIRED_FIELDS = {
    "id",
    "source",
    "source_license",
    "language",
    "domain",
    "expected_intent",
    "expected_group",
    "expected_primary_agent",
    "expected_supporting_agents",
    "entities",
    "should_use_knowledge",
    "should_escalate",
    "must_include",
    "must_not_include",
    "review_status",
    "reviewer",
    "created_at",
    "synthetic",
}

VALID_AGENTS = {"general", "technical", "billing", "escalation"}
VALID_INTENTS = {
    "query", "complaint", "request", "greeting", "escalation", "technical",
    "billing", "account", "feedback", "order_status", "logistics", "refund",
    "invoice", "payment_issue", "account_security", "technical_login",
    "technical_crash", "human_handoff", "other",
}
VALID_REVIEW_STATUSES = {"pending_human_review", "approved", "rejected"}
SENSITIVE_PATTERNS = ("@", "手机号", "联系电话", "身份证", "银行卡号")


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读取 UTF-8 JSONL，并把文件位置附在记录上以便追溯。"""
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as ex:
            raise ValueError(f"{path}:{line_number} 不是合法 JSON: {ex.msg}") from ex
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number} 必须是 JSON 对象")
        record["_dataset_file"] = path.name
        record["_dataset_line"] = line_number
        records.append(record)
    return records


def load_billing_profile(
    data_root: Path,
    split: str = "all",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """加载 Billing MVP 的意图与多轮对话金标数据。"""
    golden = data_root / "golden"
    intent_cases = load_jsonl(golden / "billing_intents_zh.jsonl")
    intent_cases.extend(load_jsonl(golden / "billing_adversarial_zh.jsonl"))
    dialog_cases = load_jsonl(golden / "billing_dialogs_zh.jsonl")
    intent_cases, dialog_cases = _apply_split(data_root, intent_cases, dialog_cases, split)
    validate_records(intent_cases, kind="intent")
    validate_records(dialog_cases, kind="dialog")
    return intent_cases, dialog_cases


def load_multi_agent_profile(
    data_root: Path,
    split: str = "all",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """加载四类 Agent 与跨 Agent 场景的统一金标集。"""
    golden = data_root / "golden"
    intent_files = [
        "general_intents_zh.jsonl",
        "general_adversarial_zh.jsonl",
        "technical_intents_zh.jsonl",
        "technical_adversarial_zh.jsonl",
        "billing_intents_zh.jsonl",
        "billing_adversarial_zh.jsonl",
        "escalation_intents_zh.jsonl",
        "escalation_adversarial_zh.jsonl",
    ]
    dialog_files = [
        "general_dialogs_zh.jsonl",
        "technical_dialogs_zh.jsonl",
        "billing_dialogs_zh.jsonl",
        "escalation_dialogs_zh.jsonl",
        "cross_agent_dialogs_zh.jsonl",
    ]
    intent_cases = _load_many(golden, intent_files)
    dialog_cases = _load_many(golden, dialog_files)
    intent_cases, dialog_cases = _apply_split(data_root, intent_cases, dialog_cases, split)
    validate_records(intent_cases, kind="intent")
    validate_records(dialog_cases, kind="dialog")
    return intent_cases, dialog_cases


def _load_many(directory: Path, filenames: List[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for filename in filenames:
        records.extend(load_jsonl(directory / filename))
    return records


def _apply_split(
    data_root: Path,
    intent_cases: List[Dict[str, Any]],
    dialog_cases: List[Dict[str, Any]],
    split: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if split == "all":
        return intent_cases, dialog_cases
    if split not in {"dev", "holdout"}:
        raise ValueError("split 仅支持 all、dev 或 holdout")
    manifest = data_root / "splits" / f"{split}_ids.json"
    if not manifest.exists():
        raise ValueError(f"缺少数据切分清单: {manifest}")
    ids = set(json.loads(manifest.read_text(encoding="utf-8")))
    return (
        [record for record in intent_cases if record["id"] in ids],
        [record for record in dialog_cases if record["id"] in ids],
    )


def stratified_limit(records: List[Dict[str, Any]], field: str, per_value: int) -> List[Dict[str, Any]]:
    """按标签稳定抽样，供有费用上限的在线冒烟评测使用。"""
    if per_value <= 0:
        return records
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in sorted(records, key=lambda item: item["id"]):
        grouped.setdefault(str(record.get(field, "unknown")), []).append(record)
    return [item for values in grouped.values() for item in values[:per_value]]


def validate_records(records: Iterable[Dict[str, Any]], kind: str) -> List[str]:
    """返回全部校验错误，供 CI 与数据生成脚本共同使用。"""
    errors: List[str] = []
    seen_ids = set()
    for record in records:
        location = _location(record)
        missing = sorted(REQUIRED_FIELDS - set(record))
        if missing:
            errors.append(f"{location}: 缺少字段 {', '.join(missing)}")
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            errors.append(f"{location}: id 必须是非空字符串")
        elif record_id in seen_ids:
            errors.append(f"{location}: 重复 id {record_id}")
        else:
            seen_ids.add(record_id)
        if record.get("expected_intent") not in VALID_INTENTS:
            errors.append(f"{location}: 未知 expected_intent {record.get('expected_intent')}")
        if record.get("expected_primary_agent") not in VALID_AGENTS:
            errors.append(f"{location}: 未知 expected_primary_agent")
        supporting_agents = record.get("expected_supporting_agents")
        if not isinstance(supporting_agents, list) or any(agent not in VALID_AGENTS for agent in supporting_agents):
            errors.append(f"{location}: expected_supporting_agents 不合法")
        elif record.get("expected_primary_agent") in supporting_agents:
            errors.append(f"{location}: 主 Agent 不应同时作为辅助 Agent")
        if record.get("review_status") not in VALID_REVIEW_STATUSES:
            errors.append(f"{location}: review_status 不合法")
        for field in ("entities", "must_include", "must_not_include"):
            if not isinstance(record.get(field), (dict, list)):
                errors.append(f"{location}: {field} 类型不合法")
        for field in ("should_use_knowledge", "should_escalate", "synthetic"):
            if not isinstance(record.get(field), bool):
                errors.append(f"{location}: {field} 必须是布尔值")

        if kind == "intent":
            if not isinstance(record.get("text"), str) or not record["text"].strip():
                errors.append(f"{location}: 单轮样本需要非空 text")
        elif kind == "dialog":
            turns = record.get("turns")
            if not isinstance(turns, list) or len(turns) < 2 or not all(isinstance(item, str) and item.strip() for item in turns):
                errors.append(f"{location}: 多轮样本需要至少两条非空 turns")
            expectations = record.get("turn_expectations")
            if expectations is not None and (not isinstance(expectations, list) or len(expectations) != len(turns)):
                errors.append(f"{location}: turn_expectations 必须与 turns 等长")

        text = "\n".join(_text_values(record))
        if any(pattern in text for pattern in SENSITIVE_PATTERNS):
            errors.append(f"{location}: 疑似包含敏感标识符")
    return errors


def validate_data_assets(data_root: Path) -> List[str]:
    """校验所有可提交数据、来源登记、稳定切分与运行时模板隔离。"""
    errors: List[str] = []
    records: List[Dict[str, Any]] = []
    golden = data_root / "golden"
    processed = data_root / "processed"
    for path in sorted(golden.glob("*.jsonl")):
        rows = load_jsonl(path)
        kind = "dialog" if any("turns" in row for row in rows) else "intent"
        errors.extend(validate_records(rows, kind))
        records.extend(rows)
    for path in sorted(processed.glob("*.jsonl")):
        rows = load_jsonl(path)
        errors.extend(validate_records(rows, "intent"))
        records.extend(rows)

    seen_ids = set()
    for record in records:
        record_id = record.get("id")
        if record_id in seen_ids:
            errors.append(f"全局重复 id: {record_id}")
        seen_ids.add(record_id)

    registered_sources = _registered_source_ids(data_root / "sources" / "registry.yaml")
    for record in records:
        if record.get("source") not in registered_sources:
            errors.append(f"{_location(record)}: source 未在 registry.yaml 登记: {record.get('source')}")

    errors.extend(_validate_splits(data_root, golden))
    return errors


def _registered_source_ids(path: Path) -> set:
    if not path.exists():
        return set()
    return {
        match.group(1).strip('"')
        for match in re.finditer(r"^\s*-\s+id:\s*([^\s#]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
    }


def _validate_splits(data_root: Path, golden: Path) -> List[str]:
    errors: List[str] = []
    split_dir = data_root / "splits"
    try:
        dev_ids = set(json.loads((split_dir / "dev_ids.json").read_text(encoding="utf-8")))
        holdout_ids = set(json.loads((split_dir / "holdout_ids.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as ex:
        return [f"无法读取数据切分清单: {ex}"]
    overlap = dev_ids & holdout_ids
    if overlap:
        errors.append(f"dev/holdout ID 重叠: {sorted(overlap)[:5]}")

    golden_records = _load_many(golden, [path.name for path in sorted(golden.glob("*.jsonl"))])
    golden_ids = {record["id"] for record in golden_records}
    manifest_ids = dev_ids | holdout_ids
    if manifest_ids != golden_ids:
        errors.append("切分清单与全部金标 ID 不一致")

    holdout_texts = []
    for record in golden_records:
        if record["id"] not in holdout_ids:
            continue
        holdout_texts.extend(value for value in _text_values(record) if len(value) >= 24)
    runtime_root = data_root.parent
    for directory in (runtime_root / "agents", runtime_root / "core", runtime_root / "skills"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.suffix not in {".py", ".md", ".json"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for text in holdout_texts:
                if text in content:
                    errors.append(f"holdout 文本泄漏到运行时模板: {path.relative_to(runtime_root)}")
                    break
    return errors


def _text_values(record: Dict[str, Any]) -> List[str]:
    values = []
    for field in ("text", "turns", "must_include", "must_not_include"):
        value = record.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    return values


def _location(record: Dict[str, Any]) -> str:
    filename = record.get("_dataset_file", "<memory>")
    line = record.get("_dataset_line", "?")
    return f"{filename}:{line}"
