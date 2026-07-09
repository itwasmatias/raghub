from services.embedding_client import embed

print("Requesting embedding...")

vector = embed("Hello RAGHub!")

print(f"Embedding length: {len(vector)}")
print("First 5 values:")
print(vector[:5])
