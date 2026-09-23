from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from agent_research_intelligence.governance.policy import DataClass


class ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["evidence", "dossier", "proposal", "private_snapshot"]
    classification: DataClass
    text: str = Field(min_length=1, max_length=1_000_000)
    source_refs: tuple[str, ...] = ()

    @property
    def authority(self) -> str:
        return {
            "evidence": "EXTERNAL_EVIDENCE", "dossier": "RESEARCH_OUTPUT",
            "proposal": "PROPOSAL_ONLY", "private_snapshot": "READ_ONLY_PROJECT_MATERIAL",
        }[self.kind]
