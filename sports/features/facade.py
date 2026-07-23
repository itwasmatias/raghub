from models.sports.features.player_feature_view import PlayerFeatureView
from models.sports.player import Player
from sports.features.registry import FeatureRegistry


class FeatureFacade:
    def __init__(self, registry: FeatureRegistry) -> None:
        self.registry = registry

    def build(
        self,
        player: Player,
        feature_names: list[str],
    ) -> PlayerFeatureView:
        view = PlayerFeatureView()

        for feature_name in feature_names:
            builder = self.registry.get(feature_name)
            if builder is None:
                view.set_feature(feature_name, None)
            else:
                view.set_feature(feature_name, builder.build(player))

        return view
