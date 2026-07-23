from sports.data.sources.nba_api_source import NbaApiSource


class FakeEndpoint:
    def __init__(self, payload):
        self._payload = payload

    def get_dict(self):
        return self._payload


def test_fetch_player_game_logs_normalizes_rows_without_network():
    payload = {
        "resultSets": [
            {
                "name": "PlayerGameLogs",
                "headers": [
                    "PLAYER_ID",
                    "PLAYER_NAME",
                    "TEAM_ID",
                    "TEAM_ABBREVIATION",
                    "TEAM_NAME",
                    "GAME_ID",
                    "GAME_DATE",
                    "MIN",
                    "PTS",
                    "REB",
                    "AST",
                    "FGM",
                    "FGA",
                    "FG3M",
                    "FG3A",
                    "FTM",
                    "FTA",
                ],
                "rowSet": [
                    [
                        2544,
                        "LeBron James",
                        1610612747,
                        "LAL",
                        "Los Angeles Lakers",
                        "0022300001",
                        "2024-10-22",
                        "35:10",
                        27,
                        8,
                        9,
                        10,
                        18,
                        2,
                        5,
                        5,
                        6,
                    ]
                ],
            }
        ]
    }
    seen_kwargs = {}

    def factory(**kwargs):
        seen_kwargs.update(kwargs)
        return FakeEndpoint(payload)

    source = NbaApiSource(endpoint_factory=factory)
    rows = source.fetch_player_game_logs("2023-24")

    assert seen_kwargs == {"season_nullable": "2023-24"}
    assert rows == [
        {
            "player_id": 2544,
            "player_name": "LeBron James",
            "team_id": 1610612747,
            "team_abbreviation": "LAL",
            "team_name": "Los Angeles Lakers",
            "game_id": "0022300001",
            "game_date": "2024-10-22",
            "minutes": "35:10",
            "pts": 27,
            "reb": 8,
            "ast": 9,
            "fgm": 10,
            "fga": 18,
            "fg3m": 2,
            "fg3a": 5,
            "ftm": 5,
            "fta": 6,
        }
    ]
