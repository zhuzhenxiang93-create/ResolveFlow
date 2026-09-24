import tempfile
import unittest
from pathlib import Path
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
from mcp.knowledge_base import KnowledgeBase
ONNXMiniLM_L6_V2.DOWNLOAD_PATH = Path(__file__).resolve().parents[1]/'data/eval/.cache/onnx_models'/ONNXMiniLM_L6_V2.MODEL_NAME

class CommerceKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.k=KnowledgeBase(chroma_host='127.0.0.1',chroma_port=65535,chroma_path=self.tmp.name,load_default_docs=False)
        self.doc=dict(id='goods-manual',domain='goods',version='v1',effective_at='2026-01-01',source='demo',title='RF-Z9',content='RF-Z9 灯光长按三秒切换。',topic='spec')
        self.k.add_documents([self.doc])
    def test_version_replacement_metadata_scope_and_conflict(self):
        hit=self.k.search('RF-Z9 灯光',3,allowed_document_ids=self.k.document_ids('goods'))[0]
        self.assertEqual((hit['domain'],hit['version'],hit['source']),('goods','v1','demo'))
        self.assertEqual(self.k.document_ids('subscription'),set())
        with self.assertRaises(ValueError):self.k.add_documents([{**self.doc,'content':'RF-Z9 长按五秒'}])
        self.k.add_documents([{**self.doc,'version':'v2','content':'RF-Z9 长按五秒'}])
        hits=self.k.search('RF-Z9',3)
        self.assertTrue(all(h['version']=='v2' and '三秒' not in h['content'] for h in hits))
        with self.assertRaises(ValueError):self.k.add_documents([self.doc])
        with self.assertRaises(ValueError):self.k.add_documents([{**self.doc,'version':'v3','effective_at':'2099-01-01'}])
