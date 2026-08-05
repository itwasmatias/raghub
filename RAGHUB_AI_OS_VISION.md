# RAGHub AI Operating System Vision

## Canonical Vision

RAGHub is a federated, interactive AI operating system that unifies the HP Pavilion and HP 14 as autonomous compute nodes, with the iPhone serving as the conversational command center for directing, observing, and governing persistent AI workers.

## System Roles

### iPhone — Conversational Command Center

The iPhone is the primary interface for:

- defining goals
- directing persistent workers
- observing activity across both computers
- reviewing plans and evidence
- approving sensitive actions
- receiving alerts, reports, and requests for input
- pausing or terminating work

### HP Pavilion — Fedora Compute Node

The Fedora node is intended for:

- long-running services
- containers and isolated sandboxes
- databases and persistent memory
- research and data processing
- monitoring services
- background AI workers
- system coordination workloads

### HP 14 — Windows Compute Node

The Windows node is intended for:

- software development
- Windows application integration
- desktop automation
- interactive testing
- Windows-specific workloads
- additional persistent AI workers

## Federation

The two laptops operate as a coordinated compute federation rather than merely sharing files.

Each node should:

- advertise its hardware and software capabilities
- report availability and health
- declare permitted actions
- accept appropriately routed tasks
- preserve worker state
- return evidence and execution results
- remain independently governable

RAGHub should route work according to capability, availability, workload, security policy, and user intent.

## Core Architecture

RAGHub includes:

- Operations Room
- federation coordinator
- host supervisors
- persistent worker runtime
- task planner and router
- persistent memory
- knowledge and evidence systems
- event and automation engine
- capability broker
- sandbox execution layer
- policy and authorization layer
- audit and observability system
- intelligence applications such as SIP

## Persistent Workers

Persistent workers may specialize in:

- research
- software development
- data analysis
- forecasting
- monitoring
- system maintenance
- sports intelligence
- authorized security research
- workflow automation

Workers may maintain memory, revise plans, use approved tools, collaborate with other workers, and continue multi-stage objectives across sessions.

## Capability and Authorization Boundary

RAGHub may reason broadly, but it may affect computers only through explicitly authorized capabilities.

Agents should not receive unrestricted root, administrator, shell, filesystem, device, or network control.

Consequential actions must pass through a capability broker that provides:

- narrowly scoped operations
- machine-specific permissions
- path and service restrictions
- timeouts
- validation
- audit records
- confirmation requirements
- rollback when possible
- emergency stop controls

## Interaction Lifecycle

The intended lifecycle is:

Converse
→ Define Goal
→ Plan
→ Assign Workers
→ Route Work Across Nodes
→ Execute Through Approved Capabilities
→ Observe
→ Evaluate
→ Request Approval When Required
→ Record Evidence
→ Learn

## Product Structure

RAGHub is the federated AI operating system.

The Operations Room is its command, observation, and governance interface.

Host Supervisors connect each authorized computer to the federation.

Persistent Workers perform specialized tasks.

The Capability Broker governs actions affecting hardware, files, applications, services, and networks.

SIP is the first major vertical intelligence application operating within RAGHub.

## Mission Statement

RAGHub coordinates persistent AI workers, memory, tools, hardware, and intelligence applications across a trusted federation of personal computers. It allows the user to direct and govern the system conversationally from an iPhone while preserving authorization, observability, evidence, and control over every consequential action.
