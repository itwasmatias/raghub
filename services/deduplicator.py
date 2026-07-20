from models.evidence import Evidence


class Deduplicator:
    """Remove duplicate evidence while preserving order."""

    @staticmethod
    def deduplicate(
        evidence: list[Evidence],
    ) -> list[Evidence]:
        seen: set[str | tuple[str, str]] = set()
        unique: list[Evidence] = []

        for item in evidence:
            if item.url:
                key = item.url
            else:
                key = (item.source, item.title)

            if key in seen:
                continue

            seen.add(key)
            unique.append(item)

        return unique