from services.embedding_client import embed

print("Requesting embedding...")

vector = embed("Hello from Fedora!")

print(f"Dimensions: {len(vector)}")
print("First five values:")
print(vector[:5])
