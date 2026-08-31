"""MissionaryX Revenue Bridge v0.1.

Governed authority layer allowing AI to safely act across applications:
- Common AppEvent contract
- Bounded revenue qualification (first-customer fit, no fabricated facts)
- Action proposal contract (AI proposes, never executes)
- Creator approval boundary (immutable cryptographic binding to proposal fingerprint)
- Outbound AppEffectAdapter boundary (no send/post on BaseConnector)
- Simulated adapters modeling SOMETHING_LANDED, NOTHING_LANDED, INDETERMINATE
- Strict prevention of blind retry on indeterminate effects
- Unified GitHub and Email support
- Phone-friendly status reports and deterministic creator test drive
"""

from revenue_bridge.events import AppEvent
from revenue_bridge.qualifier import (
    BoundedOffer,
    RevenueFitDecision,
    RevenueQualification,
    RevenueQualifier,
)
from revenue_bridge.proposals import (
    ActionProposal,
    ActionProposalState,
    ActionType,
    compile_action_proposal,
)
from revenue_bridge.approval import (
    ApprovalDecision,
    ApprovalInvalidatedError,
    ApprovalRequiredError,
    CreatorApprovalBoundary,
    CreatorApprovalRecord,
)
from revenue_bridge.effects import (
    AppEffectAdapter,
    ApprovedAppAction,
    BlindRetryRefusedError,
    EffectAdapterRegistry,
    EffectExecutionResult,
    EffectOutcomeMode,
    EffectReconciliationResult,
    EffectState,
    OutboundEffectError,
    RefusedDispatchError,
    SimulatedAppEffectAdapter,
)
from revenue_bridge.github import GitHubInboundNormalizer
from revenue_bridge.email import EmailInboundNormalizer
from revenue_bridge.inbox import RevenueInbox, RevenueOpportunity
from revenue_bridge.demo import run_creator_test_drive

__all__ = [
    "AppEvent",
    "BoundedOffer",
    "RevenueFitDecision",
    "RevenueQualification",
    "RevenueQualifier",
    "ActionProposal",
    "ActionProposalState",
    "ActionType",
    "compile_action_proposal",
    "ApprovalDecision",
    "ApprovalInvalidatedError",
    "ApprovalRequiredError",
    "CreatorApprovalBoundary",
    "CreatorApprovalRecord",
    "AppEffectAdapter",
    "ApprovedAppAction",
    "BlindRetryRefusedError",
    "EffectAdapterRegistry",
    "EffectExecutionResult",
    "EffectOutcomeMode",
    "EffectReconciliationResult",
    "EffectState",
    "OutboundEffectError",
    "RefusedDispatchError",
    "SimulatedAppEffectAdapter",
    "GitHubInboundNormalizer",
    "EmailInboundNormalizer",
    "RevenueInbox",
    "RevenueOpportunity",
    "run_creator_test_drive",
]
