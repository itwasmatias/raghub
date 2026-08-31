MISSION: MissionaryX Revenue Bridge v0.1

BUSINESS OBJECTIVE
Help MissionaryX produce its first paid customer.

Revenue Bridge is not the commercial product by itself. It is the first operational use of MissionaryX as the governed authority layer that allows AI to safely act across applications.

SUCCESS PATH:
real opportunity/reply
  -> normalize evidence
  -> qualify revenue fit
  -> prepare AI-action proposal
  -> creator approval boundary
  -> governed external effect
  -> reconciliation/evidence
  -> paying customer

VERIFIED BASELINE
Worktree:
  /home/matias/missionaryx-revenue-bridge-v0-1
Branch:
  feature/revenue-bridge-v0-1
Baseline:
  5a4a36b29780707b9f265b57d3148153cc4cfd8c

Existing architecture already has:
- BaseConnector: inbound/read/search abstraction
- ConnectorRegistry: inbound connector registry
- authoritative effect dispatch concepts
- existing MissionaryX authority/evidence patterns

Separate MissionaryX repositories have already demonstrated:
- email.send effect intent + reconciliation
- indeterminate "may have landed" effect handling
- authority-gated credential requests bound to domain, mission, agent, grant, connection, provider, capability, and resource

Do NOT modify those other repositories.

ARCHITECTURAL RULE
Do not turn BaseConnector into a send/post interface.

Keep:
  inbound App Connector -> normalized AppEvent

Separate:
  approved AppAction -> outbound AppEffectAdapter -> effect evidence/reconciliation

REQUIRED V0.1

1. COMMON APP EVENT CONTRACT

Create a normalized immutable event representation appropriate to project conventions.

It must preserve at minimum:
- event_id
- source/app
- event_type
- actor
- conversation/thread identifier
- content
- source URL/resource
- observed timestamp
- raw/provenance evidence reference
- capabilities available from source
- uncertainty

Missing evidence must remain unknown; never fabricate fields.

2. REVENUE QUALIFICATION

Add a bounded qualifier for first-customer opportunities.

It should recognize explicit evidence of pain relevant to:
- Claude Code
- Codex
- Gemini
- AI-agent reliability
- interrupted agent work
- automation failures
- Python automation
- Linux development/tooling
- AI-generated-code verification

Output must separate:
- VERIFIED evidence
- UNKNOWN information
- fit decision
- reason
- proposed bounded offer

Default first-customer offer:
  MissionaryX Reliability Check
  approximately $50 for one clearly bounded problem

Do not fabricate willingness to pay, company identity, compensation, contact information, or customer intent.

3. ACTION PROPOSAL CONTRACT

Introduce a normalized proposal such as:
- reply
- email response
- follow-up

Proposal must include:
- event/opportunity being acted on
- intended target/resource
- proposed content
- reason
- required capability
- evidence references
- creator-approval requirement
- state

AI/model output is always a proposal, never authority.

4. CREATOR APPROVAL BOUNDARY

External effects must be impossible without explicit creator approval.

Approval must bind to the exact proposed action/content/resource, not merely "approve this conversation."

Changing material action content after approval must invalidate approval.

Represent approval as evidence, not execution.

5. OUTBOUND APP EFFECT BOUNDARY

Create a separate outbound adapter interface appropriate to this repository.

It must support:
- capability declaration
- dispatch
- reconciliation/status

Do not add send/post methods to BaseConnector.

No production/live adapter may be enabled in v0.1.

Use deterministic recording/simulated adapters for tests and creator demo.

Model these cases:
- successful effect
- definite rejection/nothing landed
- connection loss / effect may have landed -> INDETERMINATE until reconciled

Never blindly retry an indeterminate effect.

6. GITHUB FIRST REAL INBOUND SLICE

Implement the first useful revenue input around GitHub.

Prefer the smallest safe implementation consistent with repository architecture:
- normalize GitHub issue/comment-like evidence
- allow deterministic fixture/input ingestion
- if a bounded unauthenticated public GitHub read can be added cleanly without credentials or broad scraping, it may be supported
- no GitHub posting, commenting, authentication, credential use, or account mutation

The v0.1 creator workflow should be able to turn GitHub opportunity/reply evidence into a qualified revenue opportunity and prepared action proposal.

7. EMAIL CONTRACT

Add email as the second application shape through the SAME common AppEvent/AppAction contracts.

For v0.1:
- deterministic inbound email fixture/provider is sufficient
- simulated outbound email effect is sufficient

Do NOT connect a real mailbox, request credentials, send email, or assume Gmail/Outlook access.

The goal is to prove GitHub and email both use the same MissionaryX contracts.

8. REVENUE INBOX / PHONE-FRIENDLY STATUS

Provide a creator-facing command/demo/report showing:

REVENUE BRIDGE
- top opportunity
- source
- exact observed pain
- fit
- verified vs unknown evidence
- proposed ~$50 bounded offer
- draft/prepared action state
- approval state
- next creator action
- last effect/reconciliation evidence if applicable

Do not invent completion percentages.

9. CREATOR TEST DRIVE

Provide a deterministic creator demo proving:

A. GitHub-like opportunity arrives
B. normalized to AppEvent
C. revenue qualifier identifies exact evidence
D. proposal is prepared
E. dispatch before approval is refused
F. creator approval is recorded
G. simulated adapter executes
H. evidence records what actually happened
I. indeterminate scenario does not blindly retry
J. email-shaped event follows the same common contract

This must be simple enough to run with one documented command.

10. TESTS

Add deterministic tests covering at minimum:
- normalized contracts
- provenance retention
- unknown evidence honesty
- qualification/rejection
- no fabricated customer facts
- proposal/action separation
- approval binding
- modification after approval invalidates authority
- dispatch without approval refused
- successful simulated dispatch
- definite rejection
- indeterminate effect reconciliation
- no blind retry after uncertain effect
- GitHub-shaped inbound event
- email-shaped inbound event
- common contract works for both
- phone-friendly revenue report
- creator demo

Run the project's appropriate existing test suite plus new focused tests if shell/tool approval permits.

Also run whitespace/syntax/static checks appropriate to this repository if available.

HARD SAFETY CONSTRAINTS
- No live email.
- No live GitHub comment/post.
- No Reddit adapter.
- No Facebook adapter.
- No credential retrieval or modification.
- No account creation.
- No payment action.
- No contract acceptance.
- No CAPTCHA or anti-bot bypass.
- No destructive Git operations.
- Do not reset, clean, rebase, force-push, push, commit, delete, or overwrite completed work.
- Do not edit any worktree except /home/matias/missionaryx-revenue-bridge-v0-1.
- Preserve existing architecture/tests.
- If an external effect is uncertain, reconcile rather than retry.

DO NOT EXPAND SCOPE
Do not build a universal connector marketplace.
Do not add Slack, Discord, Facebook, Reddit, CRM, or other adapters.
Do not redesign unrelated MissionaryX infrastructure.

COMMERCIAL ACCEPTANCE
The technical milestone is only useful if it leaves MissionaryX ready to take one real opportunity/reply, qualify it, prepare a truthful ~$50 offer, and stop at creator approval.

Models propose.
Tools execute.
Evidence decides.
