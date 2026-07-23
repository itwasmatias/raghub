from analytics.trend import TrendAnalyzer
from models.analysis_result import AnalysisResult
from models.research_result import ResearchResult
from models.evidence import Evidence

def test_trend_analyzer_returns_analysis_result():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="test"
    )

    result = analyzer.analyze(research_result)

    assert isinstance(result, AnalysisResult)
    assert result.analyzer == "trend"


def test_trend_analyzer_handles_empty_research_result():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="test"
    )

    result = analyzer.analyze(research_result)

    assert result.summary == "No trends detected."
    assert result.confidence == 0.0

def test_trend_analyzer_detects_repeated_terms():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI market"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="Apple announces AI chip",
            content="Apple announces a new AI chip for computers.",
            source="news"
        ),
        Evidence(
            title="Article 2",
            snippet="Apple expands AI research",
            content="Apple invests heavily in AI research.",
            source="news"
        ),
    ]

    result = analyzer.analyze(research_result)

    trend_terms = [
        trend["term"]
        for trend in result.metadata["trends"]
    ]

    assert "apple" in trend_terms
    assert "ai" in trend_terms

def test_trend_analyzer_ignores_stop_words():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI market"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI is the future",
            content="The AI market is growing.",
            source="news"
        ),
        Evidence(
            title="Article 2",
            snippet="AI expands",
            content="The AI industry is growing.",
            source="news"
        ),
    ]

    result = analyzer.analyze(research_result)

    trend_terms = [
        trend["term"]
        for trend in result.metadata["trends"]
    ]

    assert "ai" in trend_terms
    assert "the" not in trend_terms
    assert "is" not in trend_terms

def test_trend_analyzer_normalizes_punctuation():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI growth",
            content="AI is growing.",
            source="news"
        ),
        Evidence(
            title="Article 2",
            snippet="AI expands",
            content="AI is growing!",
            source="news"
        ),
    ]

    result = analyzer.analyze(research_result)

    trend_terms = [
        trend["term"]
        for trend in result.metadata["trends"]
    ]

    assert "growing" in trend_terms
    assert "growing." not in trend_terms
    assert "growing!" not in trend_terms    

def test_trend_analyzer_returns_frequency_for_trends():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI growth",
            content="AI is growing.",
            source="news"
        ),
        Evidence(
            title="Article 2",
            snippet="AI expansion",
            content="AI is growing.",
            source="news"
        ),
    ]

    result = analyzer.analyze(research_result)

    trends = result.metadata["trends"]

    ai_trend = next(
        trend for trend in trends
        if trend["term"] == "ai"
    )

    assert ai_trend["frequency"] == 2

def test_trend_analyzer_calculates_confidence():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI growth",
            content="AI is growing.",
            source="news"
        ),
        Evidence(
            title="Article 2",
            snippet="AI expansion",
            content="AI is growing.",
            source="news"
        ),
        Evidence(
            title="Article 3",
            snippet="AI adoption",
            content="AI is growing.",
            source="news"
        ),
    ]

    result = analyzer.analyze(research_result)

    assert result.confidence == 0.2

def test_trend_analyzer_tracks_source_count():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI",
            content="AI is growing.",
            source="source1"
        ),
        Evidence(
            title="Article 2",
            snippet="AI",
            content="AI is growing.",
            source="source2"
        ),
    ]

    result = analyzer.analyze(research_result)

    ai_trend = next(
        trend for trend in result.metadata["trends"]
        if trend["term"] == "ai"
    )

    assert ai_trend["sources"] == 2

def test_trend_analyzer_confidence_uses_source_count():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(query="AI")

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI",
            content="AI AI AI is growing.",
            source="source1"
        ),
        Evidence(
            title="Article 2",
            snippet="AI",
            content="AI is growing.",
            source="source2"
        ),
    ]

    result = analyzer.analyze(research_result)

    assert result.confidence == 0.3

def test_trend_analyzer_ranks_trends():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
    Evidence(
        title="Article 1",
        snippet="Cloud",
        content="Cloud technology is growing.",
        source="source1"
    ),
    Evidence(
        title="Article 2",
        snippet="AI",
        content="AI AI AI AI is growing.",
        source="source2"
    ),
    Evidence(
        title="Article 3",
        snippet="AI",
        content="AI AI is growing.",
        source="source3"
    ),
]

    result = analyzer.analyze(research_result)

    trends = result.metadata["trends"]

    assert trends[0]["term"] == "ai"

def test_trend_analyzer_explains_trend_rank():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(
        query="AI"
    )

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI",
            content="AI AI AI is growing.",
            source="source1"
        ),
        Evidence(
            title="Article 2",
            snippet="AI",
            content="AI AI is growing.",
            source="source2"
        ),
    ]

    result = analyzer.analyze(research_result)

    top_trend = result.metadata["trends"][0]

    assert "reason" in top_trend

def test_trend_analyzer_explains_source_strength():
    analyzer = TrendAnalyzer()

    research_result = ResearchResult(query="AI")

    research_result.merged_evidence = [
        Evidence(
            title="Article 1",
            snippet="AI",
            content="AI AI AI",
            source="source1"
        ),
    ]

    result = analyzer.analyze(research_result)

    trend = result.metadata["trends"][0]

    assert "limited source diversity" in trend["reason"].lower()
