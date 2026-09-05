"""Fixture job source for deterministic testing of MissionaryX Job Scout v0.1.

No network access. Returns pre-defined postings covering all required test scenarios.
"""
from __future__ import annotations

from datetime import datetime, timezone

from job_scout.sources.base import JobSource, RawJobListing


_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
_STALE_DATE = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Fixture postings ---

FIXTURE_AGENT_ENGINEER = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/agent-engineer-1",
    application_url="https://fixture.invalid/apply/agent-engineer-1",
    company="NeuralLoop AI",
    title="Agent Workflow Engineer",
    location="Remote",
    posting_text="""
Agent Workflow Engineer — NeuralLoop AI (Remote)

About the role:
We build production AI agents that do real work. You will design, build, and ship
agentic workflows that orchestrate multiple AI models, handle tool calling and function
calling, and coordinate multi-agent pipelines.

Responsibilities:
- Build AI agents using Claude, OpenAI, and Codex
- Design agentic workflow orchestration systems
- Implement tool calling and MCP protocol integrations
- Build evals and evaluation pipelines for agent reliability
- Create human-in-the-loop approval mechanisms
- Rapid prototyping and demo delivery

Requirements:
- Strong Python skills
- Experience building with LLMs (Claude, OpenAI, GPT-4)
- Familiarity with agentic systems and agent orchestration
- Git, Linux, CLI-based development workflow

Preferred:
- Portfolio or open source project evidence of agent work
- Experience with RAG retrieval systems
- Knowledge of function calling and tool use patterns

We hire based on demonstrated work and portfolio. Degree preferred but not required.
Compensation: $130,000–$180,000. Fully remote.
""",
    captured_at=_NOW,
    posted_at="2026-09-01",
    salary_min=130000.0,
    salary_max=180000.0,
    remote=True,
    external_id="fixture-agent-1",
)

FIXTURE_WEIRD_TITLE_AGENT = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/ai-ops-specialist-2",
    application_url="https://fixture.invalid/apply/ai-ops-specialist-2",
    company="Coherent Systems",
    title="AI Operations Specialist",
    location="Remote",
    posting_text="""
AI Operations Specialist — Coherent Systems

Despite the title, this role is 90% agent engineering.

You will build and maintain agent workflows, design tool calling integrations,
coordinate multi-agent systems, and evaluate agent output quality. We use
Claude Code and Codex extensively. You will direct coding agents, review
AI-generated outputs, and build reliability mechanisms.

You will also build human-in-the-loop approval systems and implement agentic
workflow automation. Rapid prototyping is core to the job.

Requirements:
- Python, Linux, Git
- Experience with LLM APIs (OpenAI, Anthropic)
- Comfort building agent pipelines and orchestrating AI workflows
- Evaluation/evals mindset — you care about whether agents actually work

Portfolio of demonstrated projects matters more than resume credentials.
We value self-taught and nontraditional backgrounds. No degree required.
Remote. $120,000–$160,000.
""",
    captured_at=_NOW,
    posted_at="2026-09-02",
    salary_min=120000.0,
    salary_max=160000.0,
    remote=True,
    external_id="fixture-weird-title-2",
)

FIXTURE_EXPERIENCE_GAP = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/applied-ai-eng-3",
    application_url="https://fixture.invalid/apply/applied-ai-eng-3",
    company="Vertex AI Labs",
    title="Applied AI Engineer",
    location="Remote",
    posting_text="""
Applied AI Engineer — Vertex AI Labs (Remote)

Build production LLM applications, agent systems, and AI workflows.
You will work on tool calling, RAG retrieval pipelines, evals, and agentic automation.
We are an AI-native product company shipping real systems.

Requirements:
- 5+ years of professional Python engineering
- Experience with LLM APIs (GPT-4, Claude, Anthropic)
- Agent building and orchestration experience
- Evaluation and reliability mindset

Show your work. GitHub portfolio strongly preferred.
Degree preferred but not required if experience is strong.
$140,000–$190,000. Fully remote.
""",
    captured_at=_NOW,
    posted_at="2026-08-28",
    salary_min=140000.0,
    salary_max=190000.0,
    remote=True,
    external_id="fixture-experience-gap-3",
)

FIXTURE_GENERIC_AI_BACKEND = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/backend-ai-mention-4",
    application_url="https://fixture.invalid/apply/backend-ai-mention-4",
    company="TechCorp",
    title="Senior Backend Engineer",
    location="New York, NY",
    posting_text="""
Senior Backend Engineer — TechCorp

We are building scalable microservices for financial data processing.
You will design distributed systems, optimize database queries, and build
high-throughput data pipelines.

Some familiarity with AI/ML infrastructure a bonus.

Requirements:
- 7+ years backend engineering
- Go or Java strongly preferred
- Distributed systems expertise (Kafka, Redis, Postgres)
- Strong computer science fundamentals

Bachelor's degree required.
On-site, New York. $150,000–$200,000.
""",
    captured_at=_NOW,
    posted_at="2026-09-03",
    salary_min=150000.0,
    salary_max=200000.0,
    remote=False,
    external_id="fixture-generic-backend-4",
)

FIXTURE_ML_RESEARCH = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/ml-research-5",
    application_url="https://fixture.invalid/apply/ml-research-5",
    company="DeepResearch Institute",
    title="Machine Learning Research Scientist",
    location="San Francisco, CA",
    posting_text="""
ML Research Scientist — DeepResearch Institute

Conduct original research in reinforcement learning, neural architecture search,
and foundation model pre-training. Publish findings at top-tier venues (NeurIPS, ICML, ICLR).

Requirements:
- PhD required in Machine Learning, Computer Science, or related field
- 3+ years post-PhD research experience
- Publications at NeurIPS, ICML, or equivalent
- Deep expertise in PyTorch, CUDA, distributed training

On-site, San Francisco. $200,000–$350,000.
""",
    captured_at=_NOW,
    posted_at="2026-08-15",
    salary_min=200000.0,
    salary_max=350000.0,
    remote=False,
    external_id="fixture-ml-research-5",
)

FIXTURE_SENIOR_HARD_BLOCKER = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/vp-ai-6",
    application_url="https://fixture.invalid/apply/vp-ai-6",
    company="Enterprise AI Corp",
    title="VP of AI Engineering",
    location="Chicago, IL",
    posting_text="""
VP of AI Engineering — Enterprise AI Corp

Lead a team of 40+ engineers building enterprise AI platforms.
Drive AI strategy, manage executive stakeholders, and own $20M engineering budget.

Requires active top secret security clearance.

Requirements:
- 15+ years engineering leadership
- 8+ years managing large engineering organizations
- Active top secret clearance required
- MBA or equivalent preferred

On-site, Chicago. $350,000–$500,000.
""",
    captured_at=_NOW,
    posted_at="2026-09-01",
    salary_min=350000.0,
    salary_max=500000.0,
    remote=False,
    external_id="fixture-senior-hard-blocker-6",
)

FIXTURE_DUPLICATE_SOURCE_A = RawJobListing(
    source_name="fixture_source_a",
    source_url="https://fixture.invalid/source_a/jobs/ai-eval-7",
    application_url="https://acme-ats.invalid/apply/ai-eval-7",
    company="Acme AI",
    title="AI Evaluation Engineer",
    location="Remote",
    posting_text="""
AI Evaluation Engineer — Acme AI (Remote)

Build evaluation frameworks for our LLM applications.
Design evals, run benchmarks, and ensure agent reliability.
Tool calling, function calling, workflow automation.

Requirements:
- Python
- LLM API experience
- Evals or testing experience
Portfolio encouraged.
""",
    captured_at=_NOW,
    posted_at="2026-09-01",
    remote=True,
    external_id="fixture-eval-7",
)

FIXTURE_DUPLICATE_SOURCE_B = RawJobListing(
    source_name="fixture_source_b",
    source_url="https://fixture.invalid/source_b/jobs/ai-eval-7",
    application_url="https://acme-ats.invalid/apply/ai-eval-7",  # same URL → duplicate
    company="Acme AI",
    title="AI Evaluation Engineer",
    location="Remote",
    posting_text="""
AI Evaluation Engineer — Acme AI (Remote)

Build evaluation frameworks for our LLM applications.
Design evals, run benchmarks, and ensure agent reliability.
Tool calling, function calling, workflow automation.

Requirements:
- Python
- LLM API experience
- Evals or testing experience
Portfolio encouraged.
""",
    captured_at=_NOW,
    posted_at="2026-09-01",
    remote=True,
    external_id="fixture-eval-7",
)

FIXTURE_STALE = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/old-ai-role-8",
    application_url="https://fixture.invalid/apply/old-ai-role-8",
    company="Stale Tech",
    title="AI Workflow Engineer",
    location="Remote",
    posting_text="""
AI Workflow Engineer — Stale Tech (Remote)

Build agentic workflows, tool calling integrations, and agent evaluation systems.
Python, LLM APIs, rapid prototyping.
""",
    captured_at=_STALE_DATE,
    posted_at="2025-01-01",
    remote=True,
    external_id="fixture-stale-8",
)

FIXTURE_MISSING_SALARY = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/no-salary-9",
    application_url="https://fixture.invalid/apply/no-salary-9",
    company="Stealth AI",
    title="AI Agent Engineer",
    location="Remote",
    posting_text="""
AI Agent Engineer — Stealth AI (Remote)

We are building the next generation of AI agents. You will design and ship
agentic workflows, build tool calling systems, and evaluate agent reliability.

Python, Claude, OpenAI. Portfolio of agent work strongly preferred.
Compensation: competitive (not disclosed).
""",
    captured_at=_NOW,
    posted_at="2026-09-04",
    remote=True,
    external_id="fixture-no-salary-9",
)

FIXTURE_PREFERRED_DEGREE_ONLY = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/preferred-degree-10",
    application_url="https://fixture.invalid/apply/preferred-degree-10",
    company="OpenMind Labs",
    title="LLM Evaluation Engineer",
    location="Remote",
    posting_text="""
LLM Evaluation Engineer — OpenMind Labs (Remote)

Build evals for our LLM applications, run reliability benchmarks,
and design agent evaluation pipelines.

Requirements:
- Python, LLM API experience
- Agent/agentic systems familiarity
- Evals or testing background

Education:
- Bachelor's degree preferred (not required)

We value demonstrated work over credentials.
Remote. $110,000–$150,000.
""",
    captured_at=_NOW,
    posted_at="2026-09-03",
    salary_min=110000.0,
    salary_max=150000.0,
    remote=True,
    external_id="fixture-preferred-degree-10",
)

FIXTURE_MANDATORY_DEGREE = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/mandatory-degree-11",
    application_url="https://fixture.invalid/apply/mandatory-degree-11",
    company="Traditional Corp",
    title="AI Systems Engineer",
    location="Boston, MA",
    posting_text="""
AI Systems Engineer — Traditional Corp (Boston)

Build AI infrastructure for enterprise applications.

Requirements:
- Bachelor's required in Computer Science or Engineering
- 3+ years professional experience
- Python, cloud infrastructure

On-site, Boston. $120,000–$160,000.
""",
    captured_at=_NOW,
    posted_at="2026-08-30",
    salary_min=120000.0,
    salary_max=160000.0,
    remote=False,
    external_id="fixture-mandatory-degree-11",
)

FIXTURE_MCP_EVALS = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/mcp-evals-12",
    application_url="https://fixture.invalid/apply/mcp-evals-12",
    company="AgentStack",
    title="AI Agent Developer",
    location="Remote",
    posting_text="""
AI Agent Developer — AgentStack (Remote)

Build AI agents using MCP (Model Context Protocol), tool calling,
and function calling. Design evals for agent reliability and grounding.
Implement workflow automation and agent orchestration.

We use Claude Code and OpenAI Codex. Human-in-the-loop approval workflows
are part of our core architecture.

Requirements:
- Python
- MCP or equivalent agent/tool protocol experience
- LLM evals and reliability thinking
- Git, Linux

Portfolio and GitHub projects strongly preferred.
$120,000–$165,000. Fully remote.
""",
    captured_at=_NOW,
    posted_at="2026-09-05",
    salary_min=120000.0,
    salary_max=165000.0,
    remote=True,
    external_id="fixture-mcp-evals-12",
)

FIXTURE_PORTFOLIO_EMPHASIS = RawJobListing(
    source_name="fixture",
    source_url="https://fixture.invalid/jobs/portfolio-demo-13",
    application_url="https://fixture.invalid/apply/portfolio-demo-13",
    company="ShowYourWork.ai",
    title="Applied LLM Engineer",
    location="Remote",
    posting_text="""
Applied LLM Engineer — ShowYourWork.ai (Remote)

We do portfolio-based hiring. Send us your GitHub, demo, or working system.
We don't care about degrees. We care about what you've built.

You will build LLM applications, agent workflows, rapid prototypes, and demos.
Experience with tool calling, function calling, and agent evaluation a plus.

Requirements:
- Working demos or projects you can show us
- Python, LLM API experience
- Demonstrated agent work or AI workflow project

Portfolio is everything. Show your work.
$100,000–$145,000. Fully remote.
""",
    captured_at=_NOW,
    posted_at="2026-09-04",
    salary_min=100000.0,
    salary_max=145000.0,
    remote=True,
    external_id="fixture-portfolio-13",
)


ALL_FIXTURES: list[RawJobListing] = [
    FIXTURE_AGENT_ENGINEER,
    FIXTURE_WEIRD_TITLE_AGENT,
    FIXTURE_EXPERIENCE_GAP,
    FIXTURE_GENERIC_AI_BACKEND,
    FIXTURE_ML_RESEARCH,
    FIXTURE_SENIOR_HARD_BLOCKER,
    FIXTURE_DUPLICATE_SOURCE_A,
    FIXTURE_DUPLICATE_SOURCE_B,
    FIXTURE_STALE,
    FIXTURE_MISSING_SALARY,
    FIXTURE_PREFERRED_DEGREE_ONLY,
    FIXTURE_MANDATORY_DEGREE,
    FIXTURE_MCP_EVALS,
    FIXTURE_PORTFOLIO_EMPHASIS,
]


class FixtureJobSource(JobSource):
    """Deterministic fixture source returning pre-defined job postings."""

    def __init__(self, fixtures: list[RawJobListing] | None = None) -> None:
        self._fixtures = fixtures if fixtures is not None else ALL_FIXTURES

    @property
    def source_name(self) -> str:
        return "fixture"

    def fetch(self, queries: list[str], limit_per_query: int = 10) -> list[RawJobListing]:
        return list(self._fixtures)

    def health_check(self) -> bool:
        return True
