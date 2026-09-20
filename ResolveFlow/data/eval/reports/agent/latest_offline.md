# Agent validation

Mode: offline_deterministic



Internal developer-visible regression set. Not an official benchmark or production measurement.

| Metric | Value |
| --- | --- |
| expected_outcome_rate | {"numerator": 12, "denominator": 12, "value": 1.0, "scope": "All internal regression scenarios; safe pauses count here, not as completed tasks"} |
| business_task_completion_rate | {"numerator": 7, "denominator": 12, "value": 0.5833333333333334, "scope": "All scenarios including intentional pauses/rejections; completion is requested-goal scoped"} |
| unassisted_completion_rate | {"numerator": 5, "denominator": 12, "value": 0.4166666666666667, "scope": "No reviewer; deterministic mode is NOT AI independent completion"} |
| correct_clarification_rate | {"numerator": 1, "denominator": 1, "value": 1.0, "scope": "Expected missing-information scenarios"} |
| correct_approval_pause_rate | {"numerator": 5, "denominator": 5, "value": 1.0, "scope": "Scenarios requiring review"} |
| correct_human_takeover_rate | {"numerator": 3, "denominator": 3, "value": 1.0, "scope": "Expected human handling, not AI task completion"} |
| checkpoint_recovery_rate | {"numerator": 2, "denominator": 2, "value": 1.0, "scope": "New runtime instance reads exactly the persisted task; not a SIGKILL test"} |
| unauthorized_operations | 0 |
| duplicate_operations | 0 |
| tool_errors | 0 |
| tool_error_correction_rate | null |
| fallback_count | 0 |
| user_confirmations | 7 |
| unconfirmed_operations | 0 |
| model_calls | 0 |
| input_tokens | null |
| output_tokens | null |
| cost_usd | null |
| latency_mean_ms | 5.272194500000002 |
| latency_p95_ms | 12.276499999999995 |

## Resume-safe bullets

- 为订阅客服 Agent 建立 12 个内部模拟回归场景，预期行为通过 12/12；覆盖查询、审批、部分完成和状态恢复。模式：offline_deterministic。
- 实现原生工具协议适配、受约束计划、审批绑定和独立业务状态核验；离线结果不代表真实模型解决率。

## Limitations
- Internal developer-visible regression set, not held-out model evaluation or official benchmark score
- Latency includes local harness approval, excludes real human wait; nearest-rank P95
- Operation counts concern simulated persisted entitlement/refund fields only
- No tool errors injected in this scenario suite; fault correction is covered by separate unit tests
- No real payment, tenant directory, user satisfaction or production throughput measured
