"""Application execution context: one commerce store, historical tasks read-only."""
import json
from pathlib import Path
from business.commerce import CommerceStore

class ExecutionContext:
    def __init__(self, path, client=None):
        self.path, self.client = str(path), client
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.store = CommerceStore(self.path)

    def connect(self):
        return self.store.connect()

    def get(self, task_id, owner):
        if task_id.startswith('C-'):
            return self.store.get_case(owner, task_id)
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
                raise ValueError('Task not found')
            row=db.execute('SELECT body FROM tasks WHERE id=? AND owner=?',(task_id,owner)).fetchone()
        if not row:raise ValueError('Task not found')
        task=json.loads(row[0])
        return {**task,'read_only':True,'execution_retired':True,
                'migration_note':'历史任务仅供查询；旧确认和审批不能授权新操作，请在统一对话中重新申请。'}
