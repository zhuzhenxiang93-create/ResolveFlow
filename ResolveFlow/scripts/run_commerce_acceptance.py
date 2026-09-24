"""Run concrete commerce scenarios and emit per-test evidence, no paid API calls."""
import json
import inspect
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
class Report(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.records=[]
    def addSuccess(self,test):
        super().addSuccess(test)
        method=getattr(test,test._testMethodName)
        source=inspect.getsource(method)
        self.records.append({'scenario':test.id(),'status':'passed',
            'identity':'alice; reviewer for approvals; bob for isolation assertions',
            'preconditions':'fresh temporary SQLite; CommerceStore.seed(alice); 5 goods and 3 subscriptions',
            'steps_and_expected_assertions':source,
            'execution_layer':'ConversationService.send / CommerceConversation' if 'ConversationAcceptance' in test.id() else 'CommerceStore deterministic service calls',
            'expected_model_calls':0,
            'actual_result':'All listed assertions passed in this run',
            'forbidden_side_effects':'No real payment or external fulfilment; individual safety assertions are in the recorded source',
            'evidence':{'file':'tests/test_commerce.py','line':inspect.getsourcelines(method)[1]}})
    def addFailure(self,test,err):super().addFailure(test,err);self.records.append({'scenario':test.id(),'status':'failed','evidence':self._exc_info_to_string(err,test)})
    def addError(self,test,err):super().addError(test,err);self.records.append({'scenario':test.id(),'status':'error','evidence':self._exc_info_to_string(err,test)})
suite=unittest.defaultTestLoader.loadTestsFromName('test_commerce')
result=unittest.TextTestRunner(verbosity=2,resultclass=Report).run(suite)
report={'timestamp':datetime.now(timezone.utc).isoformat(),'scope':'Deterministic simulated commerce and local SQLite memory; not real-model or browser results','total':result.testsRun,'passed':result.wasSuccessful(),'scenarios':result.records,'test_source':'tests/test_commerce.py'}
path=ROOT/'wiki/acceptance/commerce-scenarios.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(0 if result.wasSuccessful() else 1)
