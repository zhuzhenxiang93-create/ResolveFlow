"""
亮点：端到端 Agent 评测框架

核心问题：如何评测端到端 Agent？

评测维度：
  1. 意图识别准确率 —— 预测意图 vs 标注意图，计算 Accuracy / F1
  2. 响应质量评分 —— 用 LLM 作为评判者（LLM-as-Judge），
     从相关性、准确性、完整性、有用性四个维度打分
  3. 端到端对话评测 —— 模拟完整多轮对话，评估整体体验
  4. 回归测试 —— 与历史基线对比，防止性能退化

LLM-as-Judge 是评测 Agent 质量的关键技术：
  人工标注成本高、主观性强；用 LLM 评判可以规模化、可重复。
"""
import asyncio
import json
import logging
import pathlib
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.llm_client import LLMClient
from core.intent_recognizer import IntentCategory, IntentRecognizer
from evaluation.baseline import meets_final_thresholds
from evaluation.rules import evaluate_hard_rules

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class IntentTestCase:
    message:          str
    expected_intent:  str
    context:          Optional[Dict[str, Any]] = None
    test_id:          str = ""
    source:           str = "inline"
    synthetic:        bool = False
    expected_group:   Optional[str] = None
    expected_primary_agent: Optional[str] = None


@dataclass
class QualityScores:
    """LLM-as-Judge 评分结果。"""
    relevance:    float   # 相关性：回答是否针对问题
    accuracy:     float   # 准确性：信息是否正确
    completeness: float   # 完整性：是否完整解决问题
    helpfulness:  float   # 有用性：用户是否能据此行动
    judge_failed: bool = False
    error: Optional[str] = None

    @property
    def overall(self) -> float:
        return statistics.mean([self.relevance, self.accuracy, self.completeness, self.helpfulness])


@dataclass
class EvalResult:
    test_id:    str
    passed:     bool
    scores:     Dict[str, float]
    detail:     str = ""
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    """评测报告。"""
    timestamp:        str
    total:            int
    passed:           int
    pass_rate:        float
    avg_scores:       Dict[str, float]
    regressions:      List[str]          # 相比基线退化的指标
    recommendations:  List[str]
    results:          List[EvalResult]
    failure_groups:   Dict[str, Any] = field(default_factory=dict)
    report_path:      Optional[str] = None
    baseline_promoted: bool = False


# ── LLM-as-Judge ─────────────────────────────────────────────────────────────

class LLMJudge:
    """
    用 LLM 评判 Agent 响应质量。

    为什么用 LLM 而不是人工？
    - 可规模化：数千条测试用例自动评测
    - 可重复：相同输入得到稳定评分
    - 多维度：同时评估相关性、准确性等多个维度

    注意：LLM Judge 本身也有偏差，建议定期用人工标注校准。
    """

    JUDGE_PROMPT = """你是一个客服质量评估专家。请对以下客服响应进行评分。

用户问题: {question}
Agent 响应: {response}
{context_section}

请从以下四个维度评分（0.0-1.0），返回 JSON：
- relevance: 响应是否直接针对用户问题（0=完全无关，1=完全相关）
- accuracy: 信息是否准确无误（0=明显错误，1=完全正确）
- completeness: 是否完整解决了用户需求（0=完全没解决，1=完全解决）
- helpfulness: 用户能否据此采取行动（0=毫无帮助，1=非常有帮助）

只返回 JSON，例如: {{"relevance": 0.9, "accuracy": 0.8, "completeness": 0.7, "helpfulness": 0.85}}"""

    def __init__(self, client: LLMClient, model: str):
        self._client = client
        self._model  = model

    async def judge(
        self,
        question: str,
        response: str,
        context: Optional[str] = None,
    ) -> QualityScores:
        ctx_section = f"背景信息: {context}" if context else ""
        prompt = self.JUDGE_PROMPT.format(
            question=question,
            response=response,
            context_section=ctx_section,
        )
        prompt = self._clean_text(prompt)
        try:
            raw = await self._client.create(
                max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e])
            return QualityScores(
                relevance=float(data.get("relevance", 0.5)),
                accuracy=float(data.get("accuracy", 0.5)),
                completeness=float(data.get("completeness", 0.5)),
                helpfulness=float(data.get("helpfulness", 0.5)),
            )
        except Exception as ex:
            logger.warning(f"LLM Judge 失败: {ex}")
            return QualityScores(
                0.5, 0.5, 0.5, 0.5,
                judge_failed=True,
                error=str(ex),
            )

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")


# ── 意图识别评测 ──────────────────────────────────────────────────────────────

class IntentEvaluator:
    """评测意图识别的准确率和 F1。"""

    def __init__(self, recognizer: IntentRecognizer):
        self._recognizer = recognizer

    async def evaluate(self, cases: List[IntentTestCase]) -> Dict[str, Any]:
        predictions, ground_truth = [], []
        case_details: List[Dict[str, Any]] = []

        for case in cases:
            result = await self._recognizer.recognize(case.message)
            predicted = result.intent.value
            predictions.append(predicted)
            ground_truth.append(case.expected_intent)
            case_details.append({
                "id": case.test_id,
                "message": case.message,
                "expected": case.expected_intent,
                "predicted": predicted,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "source": case.source,
                "synthetic": case.synthetic,
                "expected_group": case.expected_group,
                "expected_primary_agent": case.expected_primary_agent,
            })

        # 纯 Python 计算指标
        correct = sum(p == g for p, g in zip(predictions, ground_truth))
        accuracy = correct / len(predictions) if predictions else 0.0

        # 每类 F1
        labels = sorted(set(ground_truth + predictions))
        per_class: Dict[str, Dict[str, float]] = {}
        for label in labels:
            tp = sum(p == label and g == label for p, g in zip(predictions, ground_truth))
            fp = sum(p == label and g != label for p, g in zip(predictions, ground_truth))
            fn = sum(p != label and g == label for p, g in zip(predictions, ground_truth))
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec  = tp / (tp + fn) if (tp + fn) else 0.0
            f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            per_class[label] = {"precision": prec, "recall": rec, "f1": f1}

        macro_f1 = statistics.mean(v["f1"] for v in per_class.values()) if per_class else 0.0

        return {
            "accuracy":   round(accuracy, 4),
            "macro_f1":   round(macro_f1, 4),
            "per_class":  per_class,
            "total":      len(cases),
            "correct":    correct,
            "cases":      case_details,
        }


# ── 端到端评测器 ──────────────────────────────────────────────────────────────

class EndToEndEvaluator:
    """
    端到端 Agent 评测。

    评测流程：
      1. 运行意图识别评测（准确率/F1）
      2. 运行对话质量评测（LLM-as-Judge）
      3. 与历史基线对比（回归检测）
      4. 生成可操作的优化建议
    """

    # 质量及格线
    PASS_THRESHOLD = 0.75

    def __init__(
        self,
        orchestrator,
        recognizer: IntentRecognizer,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        baseline_path: Optional[str] = None,
        reports_dir: Optional[str] = None,
        provider: Optional[str] = None,
        knowledge_resolver: Optional[
            Callable[[str, Optional[IntentCategory]], Awaitable[Tuple[str, bool]]]
        ] = None,
    ):
        client = LLMClient(api_key=api_key, base_url=base_url, model=model, provider=provider)

        self._orchestrator     = orchestrator
        self._recognizer       = recognizer
        self._judge            = LLMJudge(client, model)
        self._intent_evaluator = IntentEvaluator(recognizer)
        self._history:         List[EvalReport] = []
        self._baseline_path = pathlib.Path(baseline_path) if baseline_path else None
        self._reports_dir = pathlib.Path(reports_dir) if reports_dir else None
        self._baseline: Optional[EvalReport] = self._load_baseline()
        self._knowledge_resolver = knowledge_resolver

    async def run(
        self,
        intent_cases:    Optional[List[IntentTestCase]] = None,
        dialog_cases:    Optional[List[Dict[str, Any]]] = None,
        dataset_name: str = "inline",
        promote_baseline: bool = False,
    ) -> EvalReport:
        """
        运行完整评测。

        intent_cases: 意图识别测试用例
        dialog_cases:
          - 单轮: [{"question": "..."}]
          - 多轮: [{"turns": ["第一轮", "第二轮", ...]}]
        """
        results: List[EvalResult] = []
        all_scores: Dict[str, List[float]] = {}

        # 1. 意图识别评测
        intent_metrics: Dict[str, Any] = {}
        if intent_cases:
            intent_metrics = await self._intent_evaluator.evaluate(intent_cases)
            passed = intent_metrics["accuracy"] >= self.PASS_THRESHOLD
            results.append(EvalResult(
                test_id="intent_recognition",
                passed=passed,
                scores={"accuracy": intent_metrics["accuracy"], "macro_f1": intent_metrics["macro_f1"]},
                detail=f"准确率 {intent_metrics['accuracy']:.1%}，Macro-F1 {intent_metrics['macro_f1']:.3f}",
                metadata={
                    "total": intent_metrics.get("total", 0),
                    "correct": intent_metrics.get("correct", 0),
                    "cases": intent_metrics.get("cases", []),
                },
            ))

        # 2. 对话质量评测（调用 orchestrator 产出回复，再用 LLM Judge 评分）
        if dialog_cases:
            for i, case in enumerate(dialog_cases):
                case_results = await self._evaluate_dialog_case(case, i)
                results.extend(case_results)
                for r in case_results:
                    for key, value in r.scores.items():
                        if isinstance(value, (int, float)):
                            all_scores.setdefault(key, []).append(float(value))

        # 3. 汇总
        avg_scores = {
            k: round(statistics.mean(v), 4) for k, v in all_scores.items() if v
        }
        if intent_metrics:
            avg_scores["intent_accuracy"] = intent_metrics["accuracy"]

        passed_count = sum(1 for r in results if r.passed)
        pass_rate    = passed_count / len(results) if results else 0.0

        # 4. 回归检测
        regressions = self._detect_regressions(avg_scores)

        # 5. 优化建议
        recommendations = self._recommendations(avg_scores, intent_metrics)

        report = EvalReport(
            timestamp=datetime.now().isoformat(),
            total=len(results),
            passed=passed_count,
            pass_rate=round(pass_rate, 4),
            avg_scores=avg_scores,
            regressions=regressions,
            recommendations=recommendations,
            results=results,
            failure_groups=self._build_failure_groups(results, intent_metrics.get("cases", [])),
        )
        if promote_baseline and self._meets_final_thresholds(report.avg_scores):
            self._promote_baseline(report)
            report.baseline_promoted = True
        report.report_path = self._save_report(report, dataset_name)
        self._history.append(report)
        return report

    async def _evaluate_dialog_case(self, case: Dict[str, Any], case_idx: int) -> List[EvalResult]:
        """评测单轮或多轮对话用例。"""
        from agents.agent_orchestrator import Request as OrcReq

        questions = self._dialog_turns(case)
        if not questions:
            return []

        conv_id = str(case.get("conv_id") or f"eval_{case_idx}")
        user_id = str(case.get("user_id") or "eval_user")
        history: List[Dict[str, str]] = []
        results: List[EvalResult] = []

        for turn_idx, question in enumerate(questions):
            expectation = self._turn_expectation(case, turn_idx)
            history_context = self._history_context(history)
            recognized = await self._recognizer.recognize(question, history=history[-6:] if history else None)
            knowledge_text, knowledge_used = "", False
            if self._knowledge_resolver is not None:
                knowledge_text, knowledge_used = await self._knowledge_resolver(question, recognized.intent)
            context_parts = [history_context, knowledge_text]
            context = "\n\n".join(item for item in context_parts if item)
            orch_req = OrcReq(
                message=question,
                user_id=user_id,
                conv_id=conv_id,
                context=context,
                history=history[-6:] if history else None,
                entities=recognized.entities,
                intent=recognized.intent,
                intent_group=recognized.intent_group,
                urgency=recognized.urgency,
                intent_confidence=recognized.confidence,
            )
            orch_result = await self._orchestrator.run(orch_req)
            actual_answer = orch_result.response

            scores = await self._judge.judge(question, actual_answer, context=context or None)
            hard_checks = self._hard_checks(
                expectation=expectation,
                response=actual_answer,
                actual_agent=orch_result.primary_agent.value if orch_result.primary_agent else orch_result.agent_type.value,
                actual_agent_types=[agent.value for agent in orch_result.agent_types],
                actual_escalated=orch_result.escalated,
                actual_entities=recognized.entities,
                knowledge_used=knowledge_used,
                has_history=bool(history),
            )
            passed = scores.overall >= self.PASS_THRESHOLD and hard_checks["passed"]

            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": actual_answer})

            test_id = f"dialog_{case_idx}" if len(questions) == 1 else f"dialog_{case_idx}_turn_{turn_idx}"
            results.append(EvalResult(
                test_id=test_id,
                passed=passed,
                scores={
                    "relevance": scores.relevance,
                    "accuracy": scores.accuracy,
                    "completeness": scores.completeness,
                    "helpfulness": scores.helpfulness,
                    "overall": scores.overall,
                    **hard_checks["scores"],
                },
                detail=f"Q: {question[:30]}... → 综合评分 {scores.overall:.3f}",
                metadata={
                    "question": question,
                    "response": actual_answer,
                    "agent_type": orch_result.agent_type.value,
                    "intent": orch_result.intent.value if orch_result.intent else None,
                    "expected_intent": expectation.get("expected_intent"),
                    "expected_primary_agent": expectation.get("expected_primary_agent"),
                    "expected_supporting_agents": expectation.get("expected_supporting_agents", []),
                    "actual_agent_types": [agent.value for agent in orch_result.agent_types],
                    "scenario_type": expectation.get("scenario_type", "single_agent"),
                    "source": expectation.get("source", case.get("source", "inline")),
                    "synthetic": expectation.get("synthetic", case.get("synthetic", False)),
                    "knowledge_used": knowledge_used,
                    "hard_checks": hard_checks["detail"],
                    "turn": turn_idx,
                    "conv_id": conv_id,
                    "judge_failed": scores.judge_failed,
                    "judge_error": scores.error,
                },
            ))

        return results

    @staticmethod
    def _dialog_turns(case: Dict[str, Any]) -> List[str]:
        turns = case.get("turns")
        if isinstance(turns, list):
            return [str(t) for t in turns if str(t).strip()]
        question = case.get("question")
        return [str(question)] if question else []

    @staticmethod
    def _turn_expectation(case: Dict[str, Any], turn_idx: int) -> Dict[str, Any]:
        """合并用例级默认期望与当前轮次期望。"""
        expectation = dict(case)
        per_turn = case.get("turn_expectations")
        if isinstance(per_turn, list) and turn_idx < len(per_turn) and isinstance(per_turn[turn_idx], dict):
            expectation.update(per_turn[turn_idx])
        return expectation

    @staticmethod
    def _hard_checks(
        expectation: Dict[str, Any],
        response: str,
        actual_agent: str,
        actual_agent_types: List[str],
        actual_escalated: bool,
        actual_entities: Dict[str, List[str]],
        knowledge_used: bool,
        has_history: bool,
    ) -> Dict[str, Any]:
        """规则检查优先于 LLM Judge，确保关键业务约束不可被语言流畅度掩盖。"""
        return evaluate_hard_rules(
            expectation,
            response,
            actual_agent,
            actual_agent_types,
            actual_escalated,
            actual_entities,
            knowledge_used,
            has_history,
        )

    @staticmethod
    def _history_context(history: List[Dict[str, str]]) -> str:
        if not history:
            return ""
        lines = [f"{m['role']}: {m['content']}" for m in history[-8:]]
        return "[评测多轮历史]\n" + "\n".join(lines)

    def _detect_regressions(self, current: Dict[str, float]) -> List[str]:
        """与上一次评测对比，找出退化超过 5% 的指标。"""
        prev_report = self._history[-1] if self._history else self._baseline
        if prev_report is None:
            return []
        prev = prev_report.avg_scores
        regressions = []
        for metric, value in current.items():
            if metric in prev and prev[metric] > 0:
                delta = (value - prev[metric]) / prev[metric]
                if delta < -0.05:
                    regressions.append(
                        f"{metric}: {prev[metric]:.3f} → {value:.3f} (退化 {abs(delta):.1%})"
                    )
        return regressions

    def _recommendations(
        self,
        scores: Dict[str, float],
        intent_metrics: Dict[str, Any],
    ) -> List[str]:
        recs = []
        if scores.get("intent_accuracy", 1.0) < 0.90:
            recs.append("意图识别准确率 < 90%：增加 Few-shot 示例，或对低 F1 的意图类别补充训练数据")
        if scores.get("relevance", 1.0) < 0.75:
            recs.append("相关性偏低：检查 Agent system_prompt，确保 Agent 聚焦于用户问题")
        if scores.get("completeness", 1.0) < 0.75:
            recs.append("完整性偏低：Agent 可能过早结束回答，考虑在 prompt 中要求提供完整解决方案")
        if scores.get("helpfulness", 1.0) < 0.75:
            recs.append("有用性偏低：回答可能过于抽象，考虑要求 Agent 提供具体操作步骤")
        if scores.get("routing_accuracy", 1.0) < 0.90:
            recs.append("路由准确率 < 90%：检查意图到 Agent 的映射和领域关键词权重")
        if scores.get("escalation_recall", 1.0) < 0.85:
            recs.append("高风险人工升级召回率 < 85%：为账户安全和异常扣款补充强制升级规则")
        if scores.get("must_not_include_violation_rate", 0.0) > 0:
            recs.append("出现禁用表述或不当承诺：先收紧 Billing Agent 的话术规则，再做回归评测")
        if not recs:
            recs.append("所有指标均达标，继续保持")
        return recs

    @property
    def history(self) -> List[EvalReport]:
        return self._history

    def _load_baseline(self) -> Optional[EvalReport]:
        if not self._baseline_path or not self._baseline_path.exists():
            return None
        try:
            data = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            return self._report_from_dict(data)
        except Exception as ex:
            logger.warning(f"读取评测基线失败: {ex}")
            return None

    def _save_report(self, report: EvalReport, dataset_name: str) -> Optional[str]:
        if not self._reports_dir:
            return None
        try:
            safe_dataset = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in dataset_name)
            self._reports_dir.mkdir(parents=True, exist_ok=True)
            path = self._reports_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_{safe_dataset}.json"
            path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
            return str(path)
        except Exception as ex:
            logger.warning(f"保存评测报告失败: {ex}")
            return None

    def _promote_baseline(self, report: EvalReport) -> None:
        if not self._baseline_path:
            return
        try:
            self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self._baseline_path.write_text(
                json.dumps(asdict(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._baseline = report
        except Exception as ex:
            logger.warning(f"保存评测基线失败: {ex}")

    @staticmethod
    def _meets_final_thresholds(scores: Dict[str, float]) -> bool:
        return meets_final_thresholds(scores)

    @staticmethod
    def _report_from_dict(data: Dict[str, Any]) -> EvalReport:
        return EvalReport(
            timestamp=data.get("timestamp", ""),
            total=int(data.get("total", 0)),
            passed=int(data.get("passed", 0)),
            pass_rate=float(data.get("pass_rate", 0.0)),
            avg_scores=dict(data.get("avg_scores", {})),
            regressions=list(data.get("regressions", [])),
            recommendations=list(data.get("recommendations", [])),
            results=[
                EvalResult(
                    test_id=r.get("test_id", ""),
                    passed=bool(r.get("passed", False)),
                    scores=dict(r.get("scores", {})),
                    detail=r.get("detail", ""),
                    metadata=dict(r.get("metadata", {})),
                )
                for r in data.get("results", [])
            ],
            failure_groups=dict(data.get("failure_groups", {})),
            report_path=data.get("report_path"),
            baseline_promoted=bool(data.get("baseline_promoted", False)),
        )

    @staticmethod
    def _build_failure_groups(
        results: List[EvalResult],
        intent_case_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """按意图、Agent、来源和是否合成汇总失败样本，便于下一轮数据补齐。"""
        groups: Dict[str, Dict[str, List[str]]] = {
            "by_intent": {}, "by_agent": {}, "by_source": {}, "by_synthetic": {}, "by_scenario_type": {}
        }
        for result in results:
            if result.passed:
                continue
            metadata = result.metadata
            dimensions = {
                "by_intent": metadata.get("expected_intent") or metadata.get("intent") or "unknown",
                "by_agent": metadata.get("expected_primary_agent") or metadata.get("agent_type") or "unknown",
                "by_source": metadata.get("source", "inline"),
                "by_synthetic": str(bool(metadata.get("synthetic", False))).lower(),
                "by_scenario_type": metadata.get("scenario_type", "single_agent"),
            }
            for group, value in dimensions.items():
                groups[group].setdefault(str(value), []).append(result.test_id)
        for item in intent_case_details or []:
            if item.get("expected") == item.get("predicted"):
                continue
            test_id = item.get("id") or f"intent:{item.get('message', '')[:24]}"
            dimensions = {
                "by_intent": item.get("expected", "unknown"),
                "by_agent": item.get("expected_primary_agent") or "unknown",
                "by_source": item.get("source", "inline"),
                "by_synthetic": str(bool(item.get("synthetic", False))).lower(),
            }
            for group, value in dimensions.items():
                groups[group].setdefault(str(value), []).append(test_id)
        return groups


# ── 内置测试用例（开箱即用）──────────────────────────────────────────────────

DEFAULT_INTENT_CASES: List[IntentTestCase] = [
    IntentTestCase("我的订单什么时候到？",       "logistics"),
    IntentTestCase("帮我取消订单",               "request"),
    IntentTestCase("你们服务太差了！",            "complaint"),
    IntentTestCase("应用一直报500错误",           "technical_crash"),
    IntentTestCase("为什么扣了两次款？",          "payment_issue"),
    IntentTestCase("我要投诉，转人工！",          "human_handoff"),
    IntentTestCase("你好",                        "greeting"),
    IntentTestCase("修改我的邮箱地址",            "account"),
    IntentTestCase("帮我开发票",                  "invoice"),
    IntentTestCase("退款多久到账？",              "refund"),
    IntentTestCase("登录一直报401",               "technical_login"),

    # ── 补充用例：覆盖此前评测集里零覆盖的 8 个意图类别 ────────────────────────
    # 原始 11 条只覆盖了 11/19 类，Macro-F1/Accuracy 从未真正检验过下面这些类别。

    # query（通用查询，区别于 order_status/logistics 这类细粒度查询）
    IntentTestCase("你们的会员等级是怎么划分的？",         "query"),
    IntentTestCase("APP 支持哪些语言切换？",                "query"),
    IntentTestCase("你们家客服在线时间是几点到几点？",      "query"),
    IntentTestCase("怎么查看我以前买过的所有商品记录？",    "query"),

    # escalation（要求升级处理，区别于直接说"转人工"的 human_handoff）
    IntentTestCase("这次服务体验太差了，我要投诉，必须有人认真处理", "escalation"),
    IntentTestCase("麻烦把我这个诉求反馈给你们经理，前台客服解决不了", "escalation"),
    IntentTestCase("同样的问题我反馈第三次了，希望能升级处理，不要再让我重复描述", "escalation"),
    IntentTestCase("你们这个态度让我很不满，我需要跟负责人当面说清楚", "escalation"),

    # technical（通用技术问题，区别于 technical_login/technical_crash）
    IntentTestCase("扫码支付这个功能好像坏了，一直转圈加载不出来", "technical"),
    IntentTestCase("APP 上的搜索功能搜不到我想要的商品，是不是出问题了", "technical"),
    IntentTestCase("消息推送一直收不到，是不是设置有问题",   "technical"),
    IntentTestCase("同步到我其他设备上的数据总是对不上",     "technical"),

    # billing（通用账单问题，区别于 refund/invoice/payment_issue）
    IntentTestCase("我想问一下连续包月和单次购买价格差多少", "billing"),
    IntentTestCase("优惠券用不了，提示已过期但明明还没到期", "billing"),
    IntentTestCase("你们的会员费是每个月自动续费吗，怎么关掉", "billing"),
    IntentTestCase("账单上有一笔我不认识的收费项目，能说明一下是什么吗", "billing"),

    # feedback（正面反馈）
    IntentTestCase("这次客服响应特别快，问题一下子就解决了，很满意", "feedback"),
    IntentTestCase("商品质量比想象中好很多，包装也很用心，给你们点赞", "feedback"),
    IntentTestCase("APP 新版本改版之后好用多了，体验感提升不少", "feedback"),
    IntentTestCase("第一次网购体验这么顺畅，客服态度也很好，会继续回购的", "feedback"),

    # order_status（订单处理进度，区别于 logistics 的配送/物流进度）
    IntentTestCase("我下的这笔订单显示在处理中，具体是卡在哪个环节了？", "order_status"),
    IntentTestCase("订单页面一直显示待发货，是不是没安排上",   "order_status"),
    IntentTestCase("怎么查我这笔订单现在到哪一步了，付款成功了吗", "order_status"),
    IntentTestCase("订单是不是已经进入打包阶段了，还是说还没开始处理", "order_status"),

    # account_security（账户安全）
    IntentTestCase("我刚收到一条短信说账号在异地登录了，是不是被盗号了", "account_security"),
    IntentTestCase("想开启两步验证，但是设置里找不到入口",     "account_security"),
    IntentTestCase("怀疑账号密码泄露了，想尽快重置一下",       "account_security"),
    IntentTestCase("登录时提示有陌生设备访问，这个正常吗",     "account_security"),

    # other（真正无法归类的输入，用于检验低置信度兜底是否生效）
    IntentTestCase("你们家养猫吗",                            "other"),
    IntentTestCase("今天天气怎么样",                          "other"),
    IntentTestCase("随便问问，你们公司在哪个城市",            "other"),
    IntentTestCase("哈哈开个玩笑而已",                        "other"),
]

DEFAULT_DIALOG_CASES: List[Dict[str, Any]] = [
    {"question": "我的订单 #12345 还没到，已经超时了"},
    {"question": "应用登录一直报错 401"},
    {"question": "为什么这个月多扣了 50 块钱？"},
    {"question": "帮我把收货地址改成北京市朝阳区"},
    {"turns": ["你好，我想退款", "订单号是 #12345", "退款多久能到账？"]},
]
