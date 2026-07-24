from io import BytesIO

from web_ui import FeatureUIHandler


def _capture_get(handler: FeatureUIHandler, path: str) -> tuple[int, str]:
    captured: dict[str, int] = {}
    handler.path = path
    handler.wfile = BytesIO()
    handler.send_response = lambda status: captured.__setitem__("status", status)
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    FeatureUIHandler.do_GET(handler)

    return captured["status"], handler.wfile.getvalue().decode("utf-8")


def test_calibration_route_renders_every_bucket_from_detail_provider() -> None:
    handler = object.__new__(FeatureUIHandler)
    calls = {"count": 0}

    def calibration_detail_provider() -> dict[str, object]:
        calls["count"] += 1
        return {
            "evaluated_alerts": 12,
            "calibration_error": 0.087,
            "calibration_buckets": [
                {
                    "range": "0.00-0.20",
                    "count": 2,
                    "predicted": 0.15,
                    "observed": 0.0,
                },
                {
                    "range": "0.20-0.40",
                    "count": 3,
                    "predicted": 0.31,
                    "observed": 0.33,
                },
                {
                    "range": "0.40-0.60",
                    "count": 1,
                    "predicted": 0.55,
                    "observed": 1.0,
                },
                {
                    "range": "0.60-0.80",
                    "count": 4,
                    "predicted": 0.72,
                    "observed": 0.75,
                },
                {
                    "range": "0.80-1.00",
                    "count": 2,
                    "predicted": 0.91,
                    "observed": 1.0,
                },
            ],
        }

    handler.calibration_detail_provider = calibration_detail_provider

    status, html = _capture_get(handler, "/calibration")

    assert status == 200
    assert calls["count"] == 1
    assert "Calibration" in html
    assert "0.087" in html
    for label in (
        "0.00-0.20",
        "0.20-0.40",
        "0.40-0.60",
        "0.60-0.80",
        "0.80-1.00",
    ):
        assert label in html
    assert "Predicted" in html
    assert "Observed" in html
    assert "Count" in html


def test_player_detail_surfaces_top_hypothesis_update_status() -> None:
    handler = object.__new__(FeatureUIHandler)
    requested_player_ids: list[str] = []
    handler.player_detail_provider = lambda player_id: {
        "player_id": player_id,
        "player_name": "Ada Guard",
        "explanation": "Recent scoring is above the long-term baseline.",
        "games": [],
        "intelligence": None,
    }

    def top_hypothesis_provider(player_id: str) -> dict[str, object]:
        requested_player_ids.append(player_id)
        return {
            "lifecycle_id": 17,
            "hypothesis": "Extra minutes sustain the scoring increase.",
            "status": "supported",
            "support_rate": 0.8,
            "recommendation": "Increase confidence after another strong game.",
        }

    handler.top_hypothesis_provider = top_hypothesis_provider

    html = FeatureUIHandler.render_player_detail(handler, "p-17")

    assert requested_player_ids == ["p-17"]
    assert "Hypothesis" in html
    assert "Extra minutes sustain the scoring increase." in html
    assert "supported" in html
    assert "80%" in html
    assert "Increase confidence after another strong game." in html
