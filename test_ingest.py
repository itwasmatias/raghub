from ingest.ingest import ingest_document

with open("sample.txt", "r") as f:
    text = f.read()

ingest_document(
    title="Sample Document",
    source="local",
    text=text,
)

print("Done!")
