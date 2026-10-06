"""Run the Product Evaluation over the recruiting-demo Commerce chain.

    PYTHONPATH=. python scripts/run_product_eval.py [--live] [--label NAME] [--save-baseline]

Default mode is offline rules (deterministic, no API key). --live uses the
configured model exactly like `make portfolio-demo` with AGENT_USE_LLM=1.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation import product_evaluator, product_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use the configured LLM (AGENT_USE_LLM=1, LLM_*)")
    parser.add_argument("--env-file", help="optional dotenv file with LLM_* settings for --live")
    parser.add_argument("--label", default="current")
    parser.add_argument("--save-baseline", action="store_true", help="also store this run as product_eval_baseline.json")
    parser.add_argument("--only", nargs="*", help="run a subset of case ids (does not write reports)")
    parser.add_argument("--no-doc", action="store_true")
    parser.add_argument("--dataset", default="main", choices=["main", "holdout", "memory"],
                        help="main = 80-case set; holdout = paraphrase set written before fixes, never tuned on")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.live:
        if args.env_file:
            from dotenv import load_dotenv
            load_dotenv(args.env_file, override=False)
        os.environ["AGENT_USE_LLM"] = "1"
        for key in ("LLM_API_KEY", "LLM_MODEL"):
            if not os.getenv(key):
                parser.error(f"--live requires {key}")
    data_path = {"main": product_evaluator.DATASET, "holdout": product_evaluator.HOLDOUT, "memory": product_evaluator.MEMORY}[args.dataset]
    cases = product_evaluator.load_cases(data_path)
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
    mode, results = product_evaluator.run(cases, live=args.live)
    report = product_report.build(mode, results, args.label, data_path=data_path)
    if not args.only:
        product_report.write(report, save_baseline=args.save_baseline, write_doc=not args.no_doc and not args.live,
                             dataset=args.dataset + ("_live" if args.live else ""))
    m = report["metrics"]
    print(f"mode={mode} cases={len(results)} passed={m['task_success_rate']['numerator']}")
    for key in product_report.HEADLINE:
        print(f"  {key}: {m[key].get('value')}")
    print("bad cases:", report["bad_case_summary"]["total"],
          {k: v["count"] for k, v in report["bad_case_summary"]["by_failure_type"].items() if v["count"]})
    for b in report["bad_cases"]:
        print(f"  ✗ {b['case_id']} [{b['failure_type']}] {b['root_cause'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
