from analytics.base import BaseAnalyzer
from analytics.text.processor import TextProcessor
from models.analysis_result import AnalysisResult



def get_reason(frequency, sources):
        if sources >= 2 and frequency >= 3:
            return "High frequency across multiple sources"

        if frequency >= 3 and sources == 1:
            return "Frequently mentioned but limited source diversity"

        return "Emerging trend"

        
class TrendAnalyzer(BaseAnalyzer):


    @property
    def analyzer_name(self) -> str:
        return "trend"

    def analyze(self, research_result) -> AnalysisResult:
        if not research_result.merged_evidence:
            return AnalysisResult(
                analyzer=self.analyzer_name,
                summary="No trends detected.",
                confidence=0.0,
                metadata={}
            )

        processor = TextProcessor()

        words = []
        word_sources = {}

        for evidence in research_result.merged_evidence:
            tokens = processor.process(evidence.content)

            for token in tokens:
                words.append(token)

                if token not in word_sources:
                    word_sources[token] = set()

                word_sources[token].add(evidence.source)

        counts = {}

        for word in words:
            counts[word] = counts.get(word, 0) + 1
        

        trends = [
            {
                "term": word,
                "frequency": count,
                "sources": len(word_sources.get(word, set())),
                "score": count + len(word_sources.get(word, set())),
                "reason": get_reason(
                    count,
                    len(word_sources.get(word,set()))
                )
            }
            for word, count in counts.items()
            if count > 1
        ]

        trends.sort(
            key=lambda trend: trend["score"],
            reverse=True
        )

        trend_names = [
            trend["term"]
            for trend in trends
        ]

        max_frequency = max(
            trend["frequency"]
            for trend in trends
        )

        max_sources = max(
            trend["sources"]
            for trend in trends
        )

        confidence = min(
            (max_frequency + max_sources) / 20,
            1.0
        )

        return AnalysisResult(
            analyzer=self.analyzer_name,
            summary=f"Detected trends: {', '.join(trend_names)}",
            confidence=confidence,
            metadata={
            "trends": trends
        }
    )
