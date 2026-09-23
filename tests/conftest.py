import pytest

from agent_research_intelligence.governance.paths import Workspace
from agent_research_intelligence.governance.policy import POLICY, canonical


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path.resolve() / 'workspace'
    root.mkdir()
    ws = Workspace(root)
    ws.write('config/policies/safety.json', canonical(POLICY))
    return ws
