from fastapi import FastAPI
from api.routes import router as api_router

app = FastAPI(title="RAGHub API", version="1.0.0")

app.include_router(api_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Welcome to RAGHub!", "docs": "/docs"}
