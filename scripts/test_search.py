"""
Simple integration test for the SearchService.
"""

from services.search import SearchService


def main():
    service = SearchService()

    print("=== RAGHub SearchService Test ===")

    print("\nRegistered connectors:")
    print(service.registry.list_sources())

    print("\nAttempting Wikipedia search...")

    try:
        results = service.search(
            source="wikipedia",
            query="Artificial Intelligence",
        )

        print(f"\nFound {len(results)} result(s):\n")

        for result in results:
            print(f"Title  : {result.title}")
            print(f"Source : {result.source}")
            print(f"URL    : {result.url}")
            print(f"Snippet: {result.snippet}")
            print("-" * 60)

    except Exception as exc:
        print("\nSearch failed.")
        print(type(exc).__name__)
        print(exc)


if __name__ == "__main__":
    main()
