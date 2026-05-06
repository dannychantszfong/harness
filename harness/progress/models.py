from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


class FeatureStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSING = "passing"
    FAILING = "failing"


class EvaluationResult(BaseModel):
    design_quality: float
    originality: float
    craft: float
    functionality: float
    overall_score: float
    feedback: str
    passed: bool
    iteration: int
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


class SprintContract(BaseModel):
    """Negotiated success criteria between generator and evaluator before a sprint."""
    feature_id: str
    acceptance_criteria: list[str]
    out_of_scope: list[str]
    agreed_at: datetime = Field(default_factory=datetime.utcnow)


class Feature(BaseModel):
    id: str
    name: str
    description: str
    status: FeatureStatus = FeatureStatus.PENDING
    priority: int = 0  # lower = higher priority
    sprint_contract: Optional[SprintContract] = None
    evaluation_history: list[EvaluationResult] = Field(default_factory=list)
    implementation_notes: Optional[str] = None
    implemented_at: Optional[datetime] = None
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    @property
    def latest_evaluation(self) -> Optional[EvaluationResult]:
        if self.evaluation_history:
            return self.evaluation_history[-1]
        return None

    @property
    def iteration_count(self) -> int:
        return len(self.evaluation_history)


class ProjectProgress(BaseModel):
    project_name: str
    brief: str
    spec: Optional[str] = None
    features: list[Feature] = Field(default_factory=list)
    current_feature_id: Optional[str] = None
    total_cost_usd: float = 0.0
    total_tokens_used: int = 0
    session_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    @property
    def pending_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.PENDING]

    @property
    def passing_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.PASSING]

    @property
    def failing_features(self) -> list[Feature]:
        return [f for f in self.features if f.status == FeatureStatus.FAILING]

    @property
    def completion_pct(self) -> float:
        if not self.features:
            return 0.0
        passing = len(self.passing_features)
        return round(passing / len(self.features) * 100, 1)

    def get_feature(self, feature_id: str) -> Optional[Feature]:
        for f in self.features:
            if f.id == feature_id:
                return f
        return None

    def next_pending_feature(self) -> Optional[Feature]:
        pending = sorted(self.pending_features, key=lambda f: f.priority)
        return pending[0] if pending else None
