from flask import Flask, Response, request

from services.answer import AnswerService
from services.retrieve import RetrievalService


def create_app():
    app = Flask(__name__)
    answer_service = AnswerService()

    @app.route("/")
    def home():
        return {
            "application": "RAGHub",
            "status": "running",
            "version": "1.0.0",
        }

    @app.route("/health")
    def health():
        return {"status": "healthy", "project": "raghub"}

    @app.route("/api/answer/stream")
    def stream_answer():
        query = request.args.get("q") or request.args.get("query") or ""
        if not query:
            return {"error": "q is required"}, 400

        source = request.args.get("source", "wikipedia")
        limit = request.args.get("limit", default=10, type=int)
        session_id = request.args.get("session_id")

        def generate():
            try:
                yield from answer_service.stream_answer(
                    query=query,
                    source=source,
                    limit=limit,
                    session_id=session_id,
                )
            except TypeError:
                yield from answer_service.stream_answer(
                    query=query,
                    source=source,
                    limit=limit,
                )
                
        return Response(generate(), mimetype="text/plain")
    @app.route("/api/answer")
    def answer():
        query = request.args.get("q") or request.args.get("query") or ""
        if not query:
            return {"error": "q is required"}, 400

        source = request.args.get("source", "wikipedia")
        limit = request.args.get("limit", default=10, type=int)
        session_id = request.args.get("session_id")
        providers = request.args.get("providers")
        if providers:
            answer_service.retrieval_service = RetrievalService(provider_names=[name.strip() for name in providers.split(",") if name.strip()])
        try:
            return answer_service.answer(query=query, source=source, limit=limit, session_id=session_id)
        except TypeError:
            return answer_service.answer(query=query, source=source, limit=limit)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
