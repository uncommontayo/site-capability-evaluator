from __future__ import annotations
from typing import Literal, Optional
from pydantic import AnyUrl, BaseModel, Field

Criticality = Literal["must", "should", "nice"]
Level = Literal[0, 1, 2, 3]
SourceType = Literal[
    "app", "marketing", "pricing", "docs", "login", "signup", "external", "other"
]
AccessOutcome = Literal["authenticated", "public-only", "partial", "blocked"]
InvestigationMode = Literal["live", "passed-in", "mixed"]


# ---------- Catalog (read-only, provided by the Catalog API) ----------

class CapabilityRequirement(BaseModel):
    capabilityId: str
    minLevel: Level
    criticality: Criticality


class Feature(BaseModel):
    id: str
    name: str
    question: str
    clusterId: str
    requires: list[CapabilityRequirement]


class AcceptanceCriterion(BaseModel):
    level: Level
    text: str


class Capability(BaseModel):
    id: str
    name: str
    description: str
    areaId: str
    acceptanceCriteria: list[AcceptanceCriterion]


class ArchetypeFeatureRef(BaseModel):
    featureId: str
    criticality: Criticality


class Archetype(BaseModel):
    id: str
    name: str
    description: str
    features: list[ArchetypeFeatureRef]


class FeatureCluster(BaseModel):
    id: str
    name: str


class InvestmentArea(BaseModel):
    id: str
    name: str
    layer: str


class Catalog(BaseModel):
    version: str
    featureClusters: list[FeatureCluster]
    features: list[Feature]
    capabilities: list[Capability]
    archetypes: list[Archetype]
    investmentAreas: list[InvestmentArea] = Field(default_factory=list)


# ---------- Evaluate request ----------

class Page(BaseModel):
    url: AnyUrl
    sourceType: SourceType
    text: Optional[str] = None
    html: Optional[str] = None
    title: Optional[str] = None
    httpStatus: Optional[int] = None
    notes: Optional[str] = None


class EvaluateContent(BaseModel):
    pages: list[Page] = Field(default_factory=list)
    extraSignals: Optional[dict] = None


class Credentials(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    secrets: Optional[dict[str, str]] = None


class SessionCookie(BaseModel):
    name: str
    value: str
    domain: str


class EvaluateAccess(BaseModel):
    loginUrl: Optional[AnyUrl] = None
    credentials: Optional[Credentials] = None
    sessionCookies: Optional[list[SessionCookie]] = None
    notes: Optional[str] = None


class LiveCrawlOptions(BaseModel):
    enabled: Optional[bool] = False
    maxPages: Optional[int] = None
    timeBudgetMs: Optional[int] = None


class EvaluateOptions(BaseModel):
    liveCrawl: Optional[LiveCrawlOptions] = None
    model: Optional[str] = None


class SiteRef(BaseModel):
    domain: str
    name: Optional[str] = None
    archetypeHint: Optional[str] = None


class EvaluateRequest(BaseModel):
    site: SiteRef
    content: Optional[EvaluateContent] = None
    access: Optional[EvaluateAccess] = None
    options: Optional[EvaluateOptions] = None
    catalogVersion: Optional[str] = None


# ---------- Evaluate response ----------

class ArchetypeResult(BaseModel):
    id: str
    confidence: float = Field(ge=0, le=1)


class InferredFeature(BaseModel):
    featureId: str
    present: bool
    criticality: Criticality
    confidence: float = Field(ge=0, le=1)
    evidence: str


class RequiredCapability(BaseModel):
    capabilityId: str
    minLevel: Level
    criticality: Criticality
    confidence: float = Field(ge=0, le=1)
    sourceFeatureIds: list[str]
    reasoning: str


class EvidenceSource(BaseModel):
    type: str
    url: Optional[AnyUrl] = None
    used: bool
    note: Optional[str] = None


class Investigation(BaseModel):
    mode: InvestigationMode
    accessOutcome: AccessOutcome
    evidenceSources: list[EvidenceSource] = Field(default_factory=list)
    pagesUsed: int


class EvaluateResponse(BaseModel):
    catalogVersion: str
    evaluatorVersion: str
    archetype: ArchetypeResult
    inferredFeatures: list[InferredFeature]
    requiredCapabilities: list[RequiredCapability]
    overallConfidence: float = Field(ge=0, le=1)
    investigation: Investigation
    trace: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: str
    message: str
    detail: Optional[dict] = None
