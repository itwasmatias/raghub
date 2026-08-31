"""Revenue inbox and phone-friendly status reporting for MissionaryX Revenue Bridge v0.1.

This module provides the creator-facing revenue operational dashboard:
- Aggregates opportunities across inbound sources (GitHub, Email)
- Renders clean, phone-friendly status reports
- Tracks opportunity lifecycle from ingestion -> qualification -> proposal -> approval -> effect -> reconciliation
- Never invents completion percentages or unverified facts
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from revenue_bridge.approval import (
    ApprovalDecision,
    ApprovalRequiredError,
    CreatorApprovalBoundary,
    CreatorApprovalRecord,
)
from revenue_bridge.effects import (
    AppEffectAdapter,
    ApprovedAppAction,
    EffectExecutionResult,
    EffectReconciliationResult,
    EffectState,
)
from revenue_bridge.events import AppEvent
from revenue_bridge.proposals import (
    ActionProposal,
    ActionProposalState,
    compile_action_proposal,
)
from revenue_bridge.qualifier import (
    BoundedOffer,
    RevenueFitDecision,
    RevenueQualification,
    RevenueQualifier,
)


@dataclass
class RevenueOpportunity:
    """Consolidated state of a single customer revenue opportunity."""
    event: AppEvent
    qualification: RevenueQualification
    proposal: ActionProposal | None = None
    approval: CreatorApprovalRecord | None = None
    effect_result: EffectExecutionResult | None = None
    reconciliation_result: EffectReconciliationResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def opportunity_id(self) -> str:
        return self.event.event_id

    @property
    def source(self) -> str:
        return self.event.source_app

    @property
    def is_qualified(self) -> bool:
        return self.qualification.is_qualified

    @property
    def is_approved(self) -> bool:
        return self.approval is not None and self.approval.is_approved

    def next_creator_action(self) -> str:
        """Determine the next explicit human creator action required."""
        if not self.is_qualified:
            return "No action required (opportunity is unqualified / does not match pain profile)."

        if self.proposal is None:
            return "Compile action proposal for creator review."

        if self.approval is None:
            return f"Review proposed reply and execute approval command: `revenue-bridge approve {self.opportunity_id}`"

        if self.approval.decision == ApprovalDecision.REJECTED:
            return "Action was rejected by creator. Archive or revise proposal."

        if self.effect_result is None:
            return f"Execute approved dispatch: `revenue-bridge dispatch {self.opportunity_id}`"

        if self.effect_result.is_indeterminate:
            if self.reconciliation_result is None:
                return (
                    f"Action status is INDETERMINATE (connection timeout). "
                    f"Run reconciliation probe: `revenue-bridge reconcile {self.opportunity_id}`"
                )
            return f"Indeterminate effect reconciled ({self.reconciliation_result.resolved_state.value}). Await customer reply."

        if self.effect_result.is_landed:
            return "Outreach delivered successfully. Await customer reply to initiate paid engagement."

        return "Review outcome evidence."


class RevenueInbox:
    """In-memory operational inbox for MissionaryX revenue bridge."""

    def __init__(
        self,
        qualifier: RevenueQualifier | None = None,
        approval_boundary: CreatorApprovalBoundary | None = None,
    ) -> None:
        self.qualifier = qualifier or RevenueQualifier()
        self.approval_boundary = approval_boundary or CreatorApprovalBoundary()
        self._opportunities: dict[str, RevenueOpportunity] = {}

    def ingest(self, event: AppEvent) -> RevenueOpportunity:
        """Ingest and qualify an inbound AppEvent, generating a draft proposal if qualified."""
        qualification = self.qualifier.qualify(event)

        proposal = None
        if qualification.is_qualified:
            proposal = compile_action_proposal(event, qualification)

        opp = RevenueOpportunity(
            event=event,
            qualification=qualification,
            proposal=proposal,
        )
        self._opportunities[opp.opportunity_id] = opp
        return opp

    def get(self, opportunity_id: str) -> RevenueOpportunity | None:
        return self._opportunities.get(opportunity_id)

    def list_opportunities(self) -> list[RevenueOpportunity]:
        return list(self._opportunities.values())

    def get_top_opportunity(self) -> RevenueOpportunity | None:
        """Return the highest priority opportunity requiring creator attention."""
        # Prioritize qualified opportunities that need approval or action
        qualified_opps = [o for o in self._opportunities.values() if o.is_qualified]
        if not qualified_opps:
            return self._opportunities.values().__iter__().__next__() if self._opportunities else None

        # 1. Pending approval
        pending_approval = [o for o in qualified_opps if o.approval is None and o.proposal is not None]
        if pending_approval:
            return pending_approval[0]

        # 2. Indeterminate needing reconciliation
        indeterminate = [o for o in qualified_opps if o.effect_result and o.effect_result.is_indeterminate and o.reconciliation_result is None]
        if indeterminate:
            return indeterminate[0]

        # 3. Approved needing dispatch
        approved_ready = [o for o in qualified_opps if o.is_approved and o.effect_result is None]
        if approved_ready:
            return approved_ready[0]

        return qualified_opps[0]

    def approve(
        self,
        opportunity_id: str,
        creator_identity: str = "creator:matias",
        reason: str = "Approved by creator",
    ) -> CreatorApprovalRecord:
        """Issue creator approval for an opportunity's proposal."""
        opp = self._opportunities.get(opportunity_id)
        if opp is None:
            raise KeyError(f"Opportunity {opportunity_id} not found in inbox")
        if opp.proposal is None:
            raise ValueError(f"Opportunity {opportunity_id} does not have an action proposal to approve")

        approval = self.approval_boundary.grant_approval(
            proposal=opp.proposal,
            creator_identity=creator_identity,
            reason=reason,
        )
        opp.approval = approval
        opp.proposal = opp.proposal.with_state(ActionProposalState.APPROVED)
        return approval

    def reject(
        self,
        opportunity_id: str,
        creator_identity: str = "creator:matias",
        reason: str = "Rejected by creator",
    ) -> CreatorApprovalRecord:
        """Record rejection of an opportunity's proposal."""
        opp = self._opportunities.get(opportunity_id)
        if opp is None:
            raise KeyError(f"Opportunity {opportunity_id} not found in inbox")
        if opp.proposal is None:
            raise ValueError(f"Opportunity {opportunity_id} does not have an action proposal")

        approval = self.approval_boundary.reject_proposal(
            proposal=opp.proposal,
            creator_identity=creator_identity,
            reason=reason,
        )
        opp.approval = approval
        opp.proposal = opp.proposal.with_state(ActionProposalState.REJECTED)
        return approval

    def dispatch(
        self,
        opportunity_id: str,
        adapter: AppEffectAdapter,
    ) -> EffectExecutionResult:
        """Dispatch an approved opportunity action through an outbound adapter."""
        opp = self._opportunities.get(opportunity_id)
        if opp is None:
            raise KeyError(f"Opportunity {opportunity_id} not found in inbox")
        if opp.proposal is None:
            raise ValueError(f"Opportunity {opportunity_id} has no proposal")
        if opp.approval is None:
            raise ApprovalRequiredError(f"Opportunity {opportunity_id} has not been approved by creator")

        # Package into ApprovedAppAction
        action = ApprovedAppAction(
            action_id=f"act_{opp.proposal.proposal_id}",
            proposal=opp.proposal,
            approval=opp.approval,
            idempotency_key=f"ik_{opp.proposal.proposal_fingerprint[:16]}",
            effect_intent_id=f"intent_{opp.proposal.proposal_id}",
        )

        result = adapter.dispatch(action)
        opp.effect_result = result
        if result.is_landed:
            opp.proposal = opp.proposal.with_state(ActionProposalState.DISPATCHED)
        return result

    def reconcile(
        self,
        opportunity_id: str,
        adapter: AppEffectAdapter,
    ) -> EffectReconciliationResult:
        """Reconcile an indeterminate effect."""
        opp = self._opportunities.get(opportunity_id)
        if opp is None:
            raise KeyError(f"Opportunity {opportunity_id} not found in inbox")
        if opp.effect_result is None or not opp.effect_result.is_indeterminate:
            raise ValueError(f"Opportunity {opportunity_id} does not have an active indeterminate effect")

        intent_id = opp.effect_result.effect_intent_id
        idempotency_key = f"ik_{opp.proposal.proposal_fingerprint[:16]}" if opp.proposal else intent_id

        recon_result = adapter.reconcile(intent_id, idempotency_key)
        opp.reconciliation_result = recon_result
        if recon_result.resolved_state == EffectState.SOMETHING_LANDED and opp.proposal:
            opp.proposal = opp.proposal.with_state(ActionProposalState.DISPATCHED)
        return recon_result

    def format_phone_status(self, opportunity_id: str | None = None) -> str:
        """Format phone-friendly terminal and mobile status report."""
        if opportunity_id:
            opp = self.get(opportunity_id)
        else:
            opp = self.get_top_opportunity()

        if opp is None:
            return (
                "============================================================\n"
                "REVENUE BRIDGE — INBOX STATUS\n"
                "============================================================\n"
                "NO OPPORTUNITIES IN INBOX\n"
                "============================================================"
            )

        q = opp.qualification
        p = opp.proposal
        appr = opp.approval
        eff = opp.effect_result
        recon = opp.reconciliation_result

        # Format exact observed pain
        if q.verified_evidence:
            observed_pain = "\n".join(f"  • {e}" for e in q.verified_evidence)
        else:
            observed_pain = "  • (None observed)"

        # Format verified facts
        verified_lines = [
            f"  • Actor: {opp.event.actor}",
            f"  • Source App: {opp.event.source_app}",
            f"  • Event ID: {opp.event.event_id}",
            f"  • Observed: {opp.event.observed_at.isoformat()}",
        ]
        if q.pain_categories:
            verified_lines.append(f"  • Pain domains: {', '.join(q.pain_categories)}")

        # Format unknown facts
        unknown_lines = [f"  • {u}" for u in q.unknown_information]

        # Format bounded offer
        offer = q.proposed_offer
        if offer:
            offer_str = f"{offer.name} (${int(offer.price_usd)} for {offer.scope})"
        else:
            offer_str = "None (unqualified)"

        # Format action and approval states
        action_state = p.state.value.upper() if p else "N/A"
        if appr is None:
            approval_state = "PENDING_CREATOR_REVIEW"
        elif appr.decision == ApprovalDecision.APPROVED:
            approval_state = f"APPROVED (by {appr.creator_identity})"
        else:
            approval_state = f"REJECTED (by {appr.creator_identity})"

        # Format last effect / reconciliation evidence
        if recon is not None:
            last_effect_evidence = f"RECONCILED -> {recon.resolved_state.value.upper()} (Evidence: {recon.evidence_ref})"
        elif eff is not None:
            last_effect_evidence = f"{eff.state.value.upper()} (Evidence: {eff.evidence_ref})"
        else:
            last_effect_evidence = "None (no dispatch attempted)"

        report = (
            "============================================================\n"
            "REVENUE BRIDGE — INBOX STATUS\n"
            "============================================================\n"
            f"TOP OPPORTUNITY: {opp.opportunity_id} (Actor: @{opp.event.actor})\n"
            f"SOURCE:          {opp.source.upper()}\n"
            f"EXACT PAIN:\n{observed_pain}\n"
            f"FIT DECISION:    {q.fit_decision.value.upper()} ({q.reason})\n"
            "VERIFIED EVIDENCE:\n" + "\n".join(verified_lines) + "\n"
            "UNKNOWN INFORMATION:\n" + "\n".join(unknown_lines) + "\n"
            f"PROPOSED OFFER:  {offer_str}\n"
            f"ACTION STATE:    {action_state}\n"
            f"APPROVAL STATE:  {approval_state}\n"
            f"NEXT CREATOR:    {opp.next_creator_action()}\n"
            f"LAST EFFECT:     {last_effect_evidence}\n"
            "============================================================"
        )
        return report
