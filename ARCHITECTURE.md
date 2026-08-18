# Architecture Context and Status

This file preserves several architectural layers from the repository's history.
They are labeled explicitly so historical RAGHub/SIP descriptions are not
mistaken for the current MissionaryX control model, and future direction is not
mistaken for implemented behavior.

# Legacy/Historical RAGHub Architecture

The following RAGHub material is retained as historical context. Statements such
as "Today" describe that earlier architecture and are not current MissionaryX
runtime claims.

## Overview

RAGHub is a modular, connector-driven AI research platform.

The platform is designed so that new capabilities can be added through well-defined interfaces instead of modifying the core application.

Every major subsystem has a single responsibility.

---

# High-Level Architecture

```
                    User Interfaces
     ┌─────────────────────────────────────────┐
     │ Web │ REST API │ CLI │ Mobile │ Future │
     └─────────────────────────────────────────┘
                       │
                       ▼
              Intelligence Engine
                       │
                       ▼
               Knowledge Engine
                       │
      ┌────────────────┼────────────────┐
      │                │                │
 Connectors      Local Knowledge   External Services
      │                │                │
Wikipedia      PostgreSQL        Embedding Provider
PubMed         pgvector          LLM Provider
arXiv          Documents         Future AI Models
SEC
NASA
NOAA
GitHub
User Files
Future Sources
```

---

# Core Subsystems

## 1. User Experience Layer

Responsible for interacting with users.

Examples:

- Flask Web UI
- REST API
- Command Line Interface
- Mobile Interface
- Future desktop applications

The UI never communicates directly with data sources.

It communicates only with the Intelligence Engine.

---

## 2. Intelligence Engine

Responsible for reasoning.

Responsibilities include:

- Query planning
- Retrieval
- Prompt construction
- Multi-source synthesis
- Citation generation
- Report generation
- Future AI agents

This layer decides **how** research is performed.

---

## 3. Knowledge Engine

Responsible for processing knowledge.

Responsibilities:

- Document normalization
- Chunking
- Embedding generation
- Metadata management
- Vector indexing
- Search preparation

This layer prepares information for AI reasoning.

---

## 4. Connector Layer

Responsible for acquiring information.

Every connector should expose a common interface.

Examples:

- Wikipedia
- arXiv
- PubMed
- SEC EDGAR
- NASA
- NOAA
- OpenAlex
- Crossref
- GitHub
- User Documents
- Local Directories
- Future APIs

Adding a connector should not require changes to the Intelligence Engine.

---

# Connector Standard

Every connector should answer these questions:

- Can I search?
- Can I retrieve?
- What metadata is available?
- What type of content is returned?
- How should this source be cited?

This common interface allows the platform to treat different knowledge sources consistently.

---

# Data Flow

Research follows a consistent pipeline.

```
User Question
      │
      ▼
Intelligence Engine
      │
      ▼
Connector Selection
      │
      ▼
Information Retrieval
      │
      ▼
Knowledge Processing
      │
      ▼
Embeddings & Indexing
      │
      ▼
Retrieval & Ranking
      │
      ▼
Prompt Construction
      │
      ▼
Language Model
      │
      ▼
Grounded Response
```

---

# Design Principles

Every subsystem should have one responsibility.

Prefer interfaces over implementations.

Prefer composition over duplication.

Capabilities should be modular.

Knowledge should remain source-aware.

The platform should evolve by adding connectors rather than rewriting existing code.

---

# Technology Independence

The architecture intentionally avoids depending on specific technologies.

Examples:

Embedding Provider

Today:
- OpenAI

Future:
- Local embedding models
- Alternative providers

Language Models

Today:
- OpenAI

Future:
- Local LLMs
- Additional cloud providers

Storage

Today:
- PostgreSQL
- pgvector

Future:
- Alternative vector databases

The architecture should remain stable even as implementations change.

---

# Future Expansion

Planned capabilities include:

- Multi-agent research
- Long-running research projects
- Scheduled ingestion
- Automatic knowledge updates
- Research workspaces
- Citation management
- Collaborative research
- Mobile dashboard
- Plugin ecosystem

---

# Engineering Philosophy

Design capabilities instead of features.

Optimize for clarity before optimization.

Keep modules loosely coupled.

Every new feature should strengthen the architecture.

Build systems that can evolve.

---

# Guiding Question

Before implementing a change, ask:

> Does this improve the architecture without increasing unnecessary complexity?

If yes, continue.

If not, redesign first.
# Legacy/Historical RAGHub/SIP v1.0 Architecture

```text
NBA and sportsbook providers
            |
            v
Ingestion, canonical identity normalization, and local snapshots
            |
            v
SQLite intelligence store and source-health ledger
            |
            v
Evidence + canonical Situation lifecycle kernel
            |
            v
Player features + hypothesis evaluation
            |
            v
Forecast + evidence-weighted opportunity ranking
            |
            v
Bounded autonomous planner
            |
            v
Flask APIs and decision-focused Situation Room
            |
            v
Outcome evaluation, calibration, and research memory
```

## Primary contracts

- `SQLiteIntelligenceStore`: normalized provider records, provenance, freshness,
  incremental state, and cached-snapshot preservation.
- `IntelligenceLifecycleService`: append-only lifecycle events and reconstructed
  Situation state.
- `MarketNormalizer`: canonical games, teams, players, books, markets, outcomes,
  lines, and timestamps.
- `NBAPlayerOpportunityLoop`: evidence-gated NBA observation through monitoring.
- `OpportunityRankingEngine`: explained score using value, completeness,
  evidence, freshness, stability, uncertainty, and historical reliability.
- `BoundedIntelligencePlanner`: permission, budget, retry, approval, and audit
  guardrails.
- `NBAReplayDemo`: deterministic Observe-to-Learn release demonstration.

## Data classification

Observed source facts, derived analytical claims, model forecasts, qualitative
context, and historical replay data retain distinct labels. Empty live sources
remain empty; they are never replaced with fictional production rows.

## Future/Target Federated AI Operating System Direction

The historical RAGHub target direction, including Fedora and Windows compute
nodes, an iPhone command center, persistent workers, host supervisors, and a
capability broker, is documented in:

`RAGHUB_AI_OS_VISION.md`

---

# Current MissionaryX Architecture

The current repository contains MissionaryX contracts organized around four
independent truths: **Mission**, **Authority**, **Access/Credential**, and
**Effect**. Each truth maintains its own invariants and boundaries. This section
describes implemented contracts and explicitly qualifies integration scope; it
does not claim that every repository entry point uses the entire control model.

## The Four Independent Truths

### 1. MISSION TRUTH (Mission Runtime)

**Location**: `federation/mission_runtime.py`, `federation/mission_runtime_store.py`, `federation/mission_state.py`

**Purpose**: Manages mission lifecycle, state transitions, checkpointing, and recovery.

**Key Invariants**:
- Mission state transitions are durable and auditable
- Mission lifecycle is independent of task lifecycle
- Checkpoint and resume operations preserve mission continuity
- Mission reference ≠ mission completion
- Task failure ≠ effect failure
- Recovery evidence is preserved for all mission state changes

**Responsibilities**:
- Mission creation and initialization
- Durable mission state persistence
- Checkpoint and resume operations
- Mission lifecycle event recording
- Recovery evidence preservation

**Does NOT Handle**:
- Authority evaluation (handled by Authority Truth)
- Credential retrieval (handled by Access/Credential Truth)
- Effect execution (handled by Effect Truth)

---

### 2. AUTHORITY TRUTH (Agent Identity & Delegation)

**Location**: `federation/agent_identity.py`, `federation/agent_identity_registry.py`, `federation/delegation_grant.py`, `federation/delegation_grant_registry.py`, `federation/authority_evaluator.py`, `federation/agent_authority_store.py`

**Purpose**: Manages agent identities, delegation grants, and authority evaluation.

**Key Invariants**:
- Identity ≠ authority
- Connection ≠ authority
- Authentication ≠ authority
- AgentIdentity is immutable once created
- DelegationGrant lifecycle is deterministic and auditable
- Authority evaluation is bound to grant grantee identity
- Delegation grants are explicit and scoped

**Critical Distinctions**:
```
Identity:       Who an agent is (immutable)
Authority:      What an agent may do (granted through delegation)
Connection:     Network/transport state (ephemeral)
Authentication: Proof of identity (session-scoped)
```

**Responsibilities**:
- Agent identity creation and registry
- Delegation grant issuance and management
- Authority evaluation for requested actions
- Grant grantee identity binding
- Authority store persistence

**Does NOT Handle**:
- Credential retrieval authorization (handled by Access/Credential Truth)
- Mission state management (handled by Mission Truth)
- Effect execution (handled by Effect Truth)

---

### 3. ACCESS / CREDENTIAL TRUTH (Access & Credential Broker)

**Location**: `federation/access_credential_broker.py`, `federation/access_credential_store.py`, `federation/access_connection.py`, `federation/access_requirement.py`, `federation/credential_backend.py`, `federation/credential_broker.py`, `federation/authentication_session.py`

**Purpose**: Separates access authorization from credential retrieval and records
provider/canonical scope bindings. The newer `AccessCredentialBroker` receives an
injected `CredentialBackend` reference, but `authorize()` only evaluates the
resolved connection, provider/scope, and delegated-authority context. It returns
non-secret `AccessCredentialAuthorization` and does not resolve or return reusable
credentials.

**Key Invariants**:
- Authorization ≠ credential retrieval
- Credential retrieval ≠ effect dispatch
- Access requirements are evaluated independently
- Backend selection and routing remain separate from credential authorization
- Canonical digest binds credentials to specific contexts
- The newer authorization result is non-secret and performs no raw-secret resolution
- The inherited legacy `CredentialBroker` remains a separate public raw-secret boundary
- Authentication ≠ authorization

**Critical Distinctions**:
```
Access Authorization:  Permission to request credentials
Credential Retrieval:  Obtaining secrets from backend
Effect Dispatch:       Using credentials for effects
Provider Scope:        Backend constraint binding
Canonical Scope:       Cryptographic context binding
```

**Responsibilities of the newer AccessCredentialBroker**:
- Resolve the domain, connection, grant, and grantee records needed for a request
- Evaluate connection lifecycle, requested provider, required provider scopes, and
  delegated authority
- Return non-secret authorization evidence for the evaluated credential-use request
- Receive an injected `CredentialBackend` reference without selecting it or calling
  `resolve_credential()` during `authorize()`

**Currently distinct responsibilities**:
- The newer broker does not select or route credential backends; its caller supplies
  one backend reference. Backend routing would require a separate future integration.
- The newer broker does not manage authentication sessions; session lifecycle remains
  in `federation/authentication_session.py` and related connection workflows.
- The newer broker does not retrieve or inject raw credentials. Low-level backend
  resolution and the inherited legacy broker are separate boundaries; any future
  credential-use integration must preserve the authorization/retrieval separation.

**Inherited legacy debt**: `federation.credential_broker.CredentialLease` remains
root-exported and publicly exposes `secret_bytes()` and `secret_text()`. That
legacy API predates Access & Credential Broker v0.1; the newer broker did not
introduce it and must not be described as eliminating the repository-wide
raw-secret boundary.

**Does NOT Handle**:
- Authority-policy definition or grant persistence (handled by Authority Truth; the
  broker invokes its evaluator for the request context)
- Effect execution (handled by Effect Truth)
- Mission lifecycle (handled by Mission Truth)

---

### 4. EFFECT TRUTH (Governed Effect Gateway / DurableEffectStore)

**Location**: `federation/effect_gateway.py`, `federation/effect_boundary.py`, `federation/durable_effect_store.py`, `federation/effect_safety.py`, `pavilionos/canonical_adapter.py`, `pavilionos/canonical_coordinator.py`

**Purpose**: Manages governed effect dispatch, effect boundaries, durable effect persistence, and crash-safe effect storage.

**Key Invariants**:
- Dispatch ≠ effect success
- Effects are governed and bounded
- Effect records are append-only and crash-safe
- Schema migrations are atomic and convergent
- Concurrent migrations converge to consistent state
- Effect provenance is preserved
- Effect boundaries enforce safety policies

**Critical Distinctions**:
```
Effect Dispatch:      Initiating governed effect execution
Effect Success:       Successful completion of effect
Effect Durability:    Crash-safe persistence of effect record
Effect Boundary:      Safety and governance enforcement
Schema Migration:     Safe evolution of effect storage
```

**Responsibilities**:
- Governed effect dispatch through boundaries
- Effect execution coordination
- Durable, append-only effect persistence
- Schema migration convergence
- Effect provenance recording
- Crash atomicity guarantees
- Concurrent access coordination

**Does NOT Handle**:
- Authority evaluation (handled by Authority Truth)
- Credential retrieval (handled by Access/Credential Truth)
- Mission lifecycle (handled by Mission Truth)

---

## MissionaryX Control Model and Intended Governed Path

The MissionaryX control model defines the following intended governed path. It
is a contract for integrations that adopt the model, not proof of universal
production wiring across every repository entry point:

```
1. ControlDomain
   └─> Establishes top-level control scope
       (federation/control_domain.py, federation/control_domain_registry.py)

2. Principal
   └─> Represents the authorized user or system

3. AgentIdentity (AUTHORITY TRUTH)
   └─> Immutable identity of the acting agent
       (federation/agent_identity.py)

4. Mission (MISSION TRUTH)
   └─> Durable mission state and lifecycle
       (federation/mission_runtime.py)

5. DelegationGrant (AUTHORITY TRUTH)
   └─> Explicit authority grant for mission actions
       (federation/delegation_grant.py)

6. Access/Credential Decision (ACCESS/CREDENTIAL TRUTH)
   └─> When required: credential retrieval authorization
       (federation/access_credential_broker.py)

7. Governed Effect Boundary (EFFECT TRUTH)
   └─> Effect governance and safety enforcement
       (federation/effect_gateway.py)

8. Provider/Tool Dispatch
   └─> Actual effect execution via provider

9. Evidence
   └─> Effect result and provenance recording

10. Reconciliation
    └─> Mission state update based on effect outcome
```

**Currently verified Pavilion integration**: `CanonicalPavilionCoordinator`
constructs the canonical intent, reservation, and dispatch records and routes
Pavilion local-shell actions through `GovernedEffectGateway` before invoking its
adapter. This establishes a concrete Pavilion canonical-gateway integration; it
does not prove that every repository entry point is routed through that class.

**Critical Principle**: Operations claiming this governed control model must
preserve each truth's invariants along the path they actually use.

---

## Architectural Invariants

These invariants must hold for governed MissionaryX operations implementing this
control model:

### Identity vs. Authority
- `identity != authority`
- An agent's identity is immutable
- Authority is granted through explicit DelegationGrants
- Identity alone does not confer any operational authority

### Connection vs. Authority
- `connection != authority`
- Network connectivity does not imply authorization
- Connection state is ephemeral; authority is durable

### Authentication vs. Authority
- `authentication != authority`
- Proving identity is separate from having authority to act
- Authentication is session-scoped; authority is grant-scoped

### Authorization vs. Credential Retrieval
- `authorization != credential retrieval`
- Permission to request credentials is evaluated before retrieval
- Credential backends are selected based on provider scope
- Canonical scope binds credentials to specific contexts

### Credential Retrieval vs. Effect Dispatch
- `credential retrieval != effect dispatch`
- Obtaining credentials does not automatically trigger effects
- Effects are governed through separate effect boundaries
- Credentials are inputs to governed effect execution

### Dispatch vs. Effect Success
- `dispatch != effect success`
- Initiating an effect does not guarantee success
- Effect results are recorded separately from dispatch
- Failures are preserved as evidence

### Task Failure vs. Effect Failure
- `task failure != effect failure`
- Tasks may fail for reasons unrelated to effects
- Effects may succeed even if tasks fail
- Mission runtime distinguishes task and effect lifecycles

### Mission Reference vs. Mission Completion
- `mission reference != mission completion`
- Missions exist independently of completion state
- Mission state is durable and survives failures
- Checkpointing preserves incomplete mission state

---

## Subsystem Boundaries

### Protected Implementation Files

The following files implement core MissionaryX subsystem boundaries and should not be modified without careful architectural review:

<!-- PROTECTED_INVENTORY_START -->

**Effect Truth**:
- `federation/durable_effect_store.py`
- `federation/effect_gateway.py`
- `federation/effect_boundary.py`
- `federation/effect_safety.py`
- `federation/canonical_digest.py`
- `pavilionos/canonical_adapter.py`
- `pavilionos/canonical_coordinator.py`

**Authority Truth**:
- `federation/agent_identity.py`
- `federation/agent_identity_registry.py`
- `federation/authority_evaluator.py`
- `federation/delegation_grant.py`
- `federation/delegation_grant_registry.py`
- `federation/agent_authority_store.py`

**Access/Credential Truth**:
- `federation/access_credential_broker.py`
- `federation/access_connection.py`
- `federation/access_credential_store.py`
- `federation/access_requirement.py`
- `federation/authentication_session.py`
- `federation/credential_backend.py`
- `federation/credential_broker.py`

**Mission Truth**:
- `federation/mission_state.py`
- `federation/mission_runtime.py`
- `federation/mission_runtime_store.py`

<!-- PROTECTED_INVENTORY_END -->

Changes to these files should preserve the architectural invariants documented above.

---

## Future Concepts

The following are planned or conceptual and are NOT YET IMPLEMENTED:

- Fully autonomous mission planning (bounded autonomy exists)
- Multi-mission coordination primitives
- Cross-domain authority delegation
- Distributed effect consensus
- Real-time mission state synchronization across nodes

All future work must preserve the four independent truths and their invariants.
