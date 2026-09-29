from pydantic import BaseModel
from typing import List

class ActionItem(BaseModel):
    what: str
    owner: str
    priority: str

class PostMortem(BaseModel):
    incident_id: str
    summary: str
    timeline_reconstruction: str
    root_cause: str
    contributing_factors: List[str]
    evidence_citations: List[str]
    action_items: List[ActionItem]
    similar_past_incidents: List[str]
    confidence: str
