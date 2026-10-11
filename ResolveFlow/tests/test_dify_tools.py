"""Execute the portable Dify Code tool contract locally, without a model."""
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("readonly_dispatch", Path(__file__).resolve().parents[2] / "dify/readonly_dispatch.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ReadonlyToolTests(unittest.TestCase):
    def run_tool(self, role, op, args=None, context=None, documents=None):
        return json.loads(module.main(role, op, "登录401，应扣99元，实扣198元", json.dumps(args or {}),
            json.dumps(context or {}), documents or [])["result"])

    def test_unknown_role_and_mutations_denied(self):
        for role in ["commerce", "general", "technical", "billing", "escalation", "admin"]:
            for op in ["confirm", "approve", "settle", "refund", "reset_password"]:
                self.assertFalse(self.run_tool(role, op)["success"])

    def test_cross_role_lookup_denied(self):
        self.assertFalse(self.run_tool("billing", "lookup_error_code", {"code":"401"})["success"])
        self.assertTrue(self.run_tool("technical", "lookup_error_code", {"code":"401"})["success"])

    def test_amount_difference_is_only_user_provided(self):
        out=self.run_tool("billing", "compare_amounts", {"expected":99, "actual":198})
        self.assertEqual(out["data"]["difference"], "99.00")
        self.assertIn("not verified",out["data"]["basis"])
        self.assertFalse(self.run_tool("billing", "compare_amounts", {"expected":"NaN","actual":198})["success"])

    def test_handoff_does_not_claim_connected(self):
        out=self.run_tool("escalation", "create_handoff_summary")
        self.assertFalse(out["data"]["handoff_connected"])

    def test_knowledge_sources_include_distinct_segments(self):
        docs=[{"title":"policy","content":"7天", "metadata":{"document_id":"a","segment_id":str(i)}} for i in range(2)]
        out=self.run_tool("general", "knowledge_search", documents=docs)
        self.assertEqual(len({s["document_id"] for s in out["data"]["sources"]}),2)

class WorkflowRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml
        graph=yaml.safe_load((Path(__file__).resolve().parents[2] / 'dify/resolveflow-workflow.yml').read_text())['workflow']['graph']
        cls.nodes={n['id']:n['data'] for n in graph['nodes']}

    def parse(self, proposal, message):
        namespace={}
        exec(self.nodes['parse']['code'],namespace)
        return namespace['main'](json.dumps(proposal,ensure_ascii=False),message)

    def test_refund_and_login_do_not_add_billing(self):
        p={'intents':[{'domain':'subscription','operation':'refund','reference':'Pro'}],
           'support_roles':['technical','billing'],'support_tasks':{'technical':'登录401'},'general_remainder':'登录401'}
        out=self.parse(p,'把Pro重复扣的钱退掉，而且登录401')
        self.assertEqual(json.loads(out['agent_plan']),['commerce','technical'])
        self.assertNotIn('退款',json.loads(out['technical_task'])['message'])

    def test_amount_comparison_does_not_add_commerce_query(self):
        p={'intents':[{'domain':'unknown','operation':'read','reference':'账单'}],
           'support_roles':['billing','escalation']}
        out=self.parse(p,'请比对账单：应扣99元，实扣198元，转人工')
        self.assertEqual(json.loads(out['agent_plan']),['billing','escalation'])
        self.assertEqual(json.loads(out['proposal'])['intents'],[])

    def test_supervisor_commerce_role_is_derived_from_validated_intents(self):
        p={'intents':[{'domain':'subscription','operation':'refund','reference':'Pro'}],
           'support_roles':['Commerce','technical'],'support_tasks':{'technical':'登录401'}}
        out=self.parse(p,'退Pro，登录401')
        self.assertEqual(json.loads(out['agent_plan']),['commerce','technical'])
        p['intents']=[]
        self.assertEqual(json.loads(self.parse(p,'登录401')['agent_plan']),['technical'])
        p['support_roles']=['admin']
        with self.assertRaises(ValueError):self.parse(p,'自动批准退款')
