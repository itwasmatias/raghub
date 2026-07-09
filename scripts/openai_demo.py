from dotenv import load_dotenv
from openai import OpenAI

print("1")
load_dotenv()

print("2")
client = OpenAI()

print("3")
response = client.embeddings.create(
    model="text-embedding-3-small",
    input="Hello world",
)

print("4")
print(len(response.data[0].embedding))
