from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from models.sports.features.trend import TrendFeatures
from models.sports.features.shooting import ShootingFeatures
from models.sports.features.usage import UsageFeatures


@dataclass
class PlayerFeatureView:
    """
    Aggregate view returned by the feature facade.

    Known feature families are typed explicitly. Additional plugin features are
    retained in ``extra_features`` and remain available through attribute access.
    """

    _known_features: ClassVar[frozenset[str]] = frozenset(
        {"trend", "shooting", "usage"}
    )

    trend: Optional[TrendFeatures] = None
    shooting: Optional[ShootingFeatures] = None
    usage: Optional[UsageFeatures] = None
    extra_features: dict[str, Any] = field(default_factory=dict)

    def set_feature(self, name: str, value: Any) -> None:
        if name in self._known_features:
            setattr(self, name, value)
            return

        self.extra_features[name] = value

    def __getattr__(self, name: str) -> Any:
        try:
            extra_features = object.__getattribute__(
                self,
                "extra_features",
            )
        except AttributeError:
            raise AttributeError(name) from None

        try:
            return extra_features[name]
        except KeyError as error:
            raise AttributeError(name) from error
