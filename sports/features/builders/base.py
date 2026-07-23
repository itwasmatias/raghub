from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from models.sports.player import Player


FeatureT_co = TypeVar("FeatureT_co", covariant=True)


class BaseFeatureBuilder(ABC, Generic[FeatureT_co]):
    """
    Shared contract for player feature builders.

    A builder owns one named feature and returns that feature's normalized model.
    Builders can be registered directly or discovered from plugin module paths.
    """

    @property
    @abstractmethod
    def feature_name(self) -> str:
        """Stable identifier used by the registry and feature view."""
        raise NotImplementedError

    @abstractmethod
    def build(self, player: Player) -> FeatureT_co:
        """Build one normalized feature model for ``player``."""
        raise NotImplementedError
