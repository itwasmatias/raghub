# RAGHub Development Roadmap

## Vision

RAGHub is evolving from a Retrieval-Augmented Generation (RAG) application into a modular AI research platform capable of connecting multiple knowledge sources, organizing information, and assisting users with transparent, evidence-based research.

---

# Guiding Philosophy

We will build RAGHub in progressive phases.

Each phase should produce a working, testable system while laying the foundation for future capabilities.

Progress is measured by capabilities, not lines of code.

---

# Phase 0 — Foundation ✅

Status: Complete

## Infrastructure

- Fedora application server
- PostgreSQL
- pgvector
- Flask application
- Windows embedding service
- OpenAI embedding integration
- VS Code Remote SSH
- Tailscale networking
- iPhone SSH administration

## Documentation

- README.md
- MISSION.md
- ARCHITECTURE.md

---

# Phase 1 — Platform Foundation

Status: In Progress

## Goals

- Create connector architecture
- Build BaseConnector interface
- Standardize document model
- Improve ingestion pipeline
- Standardize metadata
- Improve project organization

Deliverable:

A modular platform ready to support multiple knowledge sources.

---

# Phase 2 — Public Knowledge Connectors

Goals

Implement production-quality connectors.

Priority order:

1. Wikipedia
2. arXiv
3. PubMed
4. SEC EDGAR
5. OpenAlex
6. Crossref
7. NASA
8. NOAA
9. USGS

Deliverable:

Multiple searchable public knowledge repositories.

---

# Phase 3 — Knowledge Engine

Goals

- Unified ingestion pipeline
- Document normalization
- Metadata enrichment
- Chunking improvements
- Embedding pipeline
- Duplicate detection
- Incremental updates

Deliverable:

A reusable knowledge processing engine.

---

# Phase 4 — Intelligence Engine

Goals

- Retrieval improvements
- Reranking
- Prompt optimization
- Citation generation
- Multi-source reasoning
- Source comparison
- Evidence ranking

Deliverable:

An explainable AI research assistant.

---

# Phase 5 — Research Workspace

Goals

Support long-running research projects.

Features:

- Saved projects
- Saved searches
- Notes
- Collections
- Tags
- Research history

Deliverable:

Persistent research sessions.

---

# Phase 6 — User Experience

Goals

Expand available interfaces.

Interfaces:

- Improved web UI
- REST API
- CLI
- Mobile dashboard
- Future desktop application

Deliverable:

Access RAGHub from multiple devices.

---

# Phase 7 — Automation

Goals

- Scheduled ingestion
- Automatic updates
- Background indexing
- Notifications
- Research monitoring

Deliverable:

A continuously updating knowledge platform.

---

# Phase 8 — AI Agents

Goals

Support autonomous workflows.

Examples:

- Literature reviews
- Regulatory monitoring
- Scientific discovery
- Competitive analysis
- Multi-step research

Deliverable:

Autonomous AI research assistants.

---

# Phase 9 — Plugin Ecosystem

Goals

Allow third-party extensions.

Examples:

- Custom connectors
- Custom retrieval methods
- Custom embedding providers
- Community plugins

Deliverable:

An extensible ecosystem.

---

# Current Sprint

Focus:

- Architecture refinement
- Connector framework
- BaseConnector implementation
- First connector (Wikipedia)

---

# Success Metrics

The platform should eventually:

- Connect to many knowledge sources.
- Preserve source attribution.
- Produce explainable answers.
- Support multiple AI providers.
- Support multiple embedding providers.
- Scale without major architectural changes.
- Remain understandable and maintainable.

---

# Engineering Rule

Never sacrifice architecture for short-term speed.

Every feature should strengthen the platform.

If a shortcut creates long-term complexity, redesign first.

---

# North Star

Build a research platform that helps people understand the world through transparent, modular, and trustworthy AI.
