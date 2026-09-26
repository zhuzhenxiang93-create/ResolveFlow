"""Opt-in real-provider smoke test for the isolated host; no secrets in output."""
import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow paid provider requests')
    parser.add_argument('--env-file', help='Existing project configuration; never modified')
    parser.add_argument('--output', help='Write sanitized evidence JSON')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required to spend model API budget')
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    if not all(os.getenv(k) for k in ('LLM_API_KEY', 'LLM_MODEL')):
        parser.error('Configure LLM_API_KEY and LLM_MODEL; optional LLM_PROVIDER / LLM_BASE_URL')
    os.environ.update(RESOLVEFLOW_DEMO_MODE='true', AGENT_USE_LLM='1')
    logging.disable(logging.CRITICAL)
    from fastapi.testclient import TestClient
    from api.portfolio_demo import create_app
    evidence = {'checked_at': datetime.now(timezone.utc).isoformat(),
                'model': os.environ['LLM_MODEL'], 'retrieval': 'lexical', 'simulated': True, 'checks': []}
    with tempfile.TemporaryDirectory() as directory:
        os.environ['RESOLVEFLOW_DEMO_DIR'] = directory
        with TestClient(create_app()) as client:
            session = client.post('/demo/session').json()
            headers = {'Authorization': 'Bearer ' + session['userToken']}
            prompts = ['商品退款政策是什么？',
                       '把 Pro 会员重复扣的钱退掉，而且登录一直报 401。',
                       '请用知识库检索工具核对商品退货所需材料，并用错误码工具查询401。']
            for index, prompt in enumerate(prompts):
                response = client.post('/chat', headers=headers, json={'message': prompt})
                response.raise_for_status()
                result = response.json()
                support = result.get('support_result') or {}
                operations = (result.get('commerce_case') or {}).get('operations', [])
                sources = sorted({s['document_id'] for s in result.get('sources', [])})
                ok_tools = {t['tool_name'] for t in support.get('tool_traces', []) if t['success']}
                if index == 0:
                    passed = bool(sources) and not operations
                elif index == 1:
                    passed = (len(operations) == 1 and operations[0]['quote']['amount_minor'] == 9900
                              and operations[0]['quote']['payment_id'].endswith('-P2')
                              and operations[0]['status'] == 'awaiting_confirmation'
                              and support.get('agent_type') == 'technical' and 'lookup_error_code' in ok_tools)
                else:
                    passed = {'knowledge_search', 'lookup_error_code'} <= ok_tools
                passed = passed and not support.get('diagnostics') and result.get('interpretation', {}).get('mode') != 'model_error'
                check = {'prompt': prompt, 'passed': bool(passed), 'mode': result.get('interpretation', {}).get('mode'),
                         'amounts_minor': [a['quote']['amount_minor'] for a in operations],
                         'support_agent': support.get('agent_type'), 'tools': sorted(ok_tools),
                         'sources': sources, 'degradations': result.get('degradations', [])}
                evidence['checks'].append(check)
                print(json.dumps(check, ensure_ascii=False), flush=True)
    if args.output:
        Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    return 0 if all(c['passed'] for c in evidence['checks']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
