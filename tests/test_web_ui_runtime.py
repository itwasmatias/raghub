import web_ui


class FakeRuntime:
    created_count = 0
    latest_instance = None

    def __init__(self):
        type(self).created_count += 1
        type(self).latest_instance = self
        self.load_history_calls = 0
        self.refresh_calls = 0
        self.load_status_message = "Failed to load history"
        self.load_status_details = {
            "success": False,
            "source_label": None,
            "games_loaded": 0,
            "players_loaded": 0,
            "records_loaded": 0,
            "message": "Failed to load history",
        }
        self.learning_summary = {
            "evaluated_alerts": 0,
            "accuracy": 0.0,
            "avg_confidence": 0.0,
            "avg_confidence_error": 0.0,
            "calibration_error": 0.0,
            "calibration_buckets": [],
            "hypothesis_updates": [],
            "reusable_knowledge": [],
        }

    def get_trending_players(self):
        return ["cached-player"]

    def load_history(self):
        self.load_history_calls += 1

    def refresh(self):
        self.refresh_calls += 1

    def get_load_status_message(self):
        return self.load_status_message

    def get_load_status_details(self):
        return self.load_status_details

    def get_learning_summary(self):
        return self.learning_summary


class FakeServer:
    latest = None

    def __init__(self, address, handler_class):
        self.address = address
        self.handler_class = handler_class
        self.served = False
        type(self).latest = self

    def serve_forever(self):
        self.served = True


def test_main_wires_basketball_runtime_callbacks(monkeypatch):
    FakeRuntime.created_count = 0
    FakeRuntime.latest_instance = None
    FakeServer.latest = None

    monkeypatch.setattr(web_ui, "BasketballDemoRuntime", FakeRuntime)
    monkeypatch.setattr(web_ui, "ThreadingHTTPServer", FakeServer)

    web_ui.main()

    assert FakeRuntime.created_count == 1
    runtime = FakeRuntime.latest_instance
    server = FakeServer.latest

    assert server is not None
    assert server.address == ("0.0.0.0", 8000)
    assert server.served is True

    handler_class = server.handler_class
    assert issubclass(handler_class, web_ui.FeatureUIHandler)

    assert handler_class.trending_player_provider() == ["cached-player"]

    handler_class.historical_load_callback()
    assert runtime.load_history_calls == 1

    handler_class.refresh_callback()
    assert runtime.refresh_calls == 1

    assert handler_class.historical_load_status_provider() == "Failed to load history"
    assert handler_class.historical_load_status_details_provider() == {
        "success": False,
        "source_label": None,
        "games_loaded": 0,
        "players_loaded": 0,
        "records_loaded": 0,
        "message": "Failed to load history",
    }
    assert handler_class.learning_summary_provider() == {
        "evaluated_alerts": 0,
        "accuracy": 0.0,
        "avg_confidence": 0.0,
        "avg_confidence_error": 0.0,
        "calibration_error": 0.0,
        "calibration_buckets": [],
        "hypothesis_updates": [],
        "reusable_knowledge": [],
    }


def test_handler_renders_historical_load_status_message():
    handler = object.__new__(web_ui.FeatureUIHandler)
    handler.trending_player_provider = lambda: []
    handler.historical_load_status_provider = lambda: "Source unavailable"

    html = web_ui.FeatureUIHandler.render_form(handler)

    assert "Load status:" in html
    assert "Source unavailable" in html
