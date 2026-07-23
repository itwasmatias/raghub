from typing import Any, Optional

from sports.features.builders.base import BaseFeatureBuilder


class FeatureRegistry:
    """
    Registry of feature builders with plugin discovery support.
    Mirrors the ConnectorRegistry pattern: builders can be registered
    individually or discovered automatically from plugin module paths.
    """

    def __init__(self) -> None:
        self._builders: dict[str, BaseFeatureBuilder[Any]] = {}
        self._plugin_paths: list[str] = []
        self._loaded_plugin_paths: set[str] = set()

    def register(
        self,
        name: str,
        builder: BaseFeatureBuilder[Any],
    ) -> None:
        """Register a builder under an explicit name."""
        self._builders[name] = builder

    def register_builder(self, builder: BaseFeatureBuilder[Any]) -> None:
        """Register a builder using its feature_name property."""
        name = builder.feature_name
        if name in self._builders:
            raise ValueError(
                f"Builder for feature '{name}' is already registered"
            )
        self._builders[name] = builder

    def register_plugin_path(self, plugin_path: str) -> None:
        """Register a dotted module path for plugin discovery."""
        if plugin_path not in self._plugin_paths:
            self._plugin_paths.append(plugin_path)

    def load_plugins(self) -> None:
        """
        Import plugin modules from registered paths and register any
        BaseFeatureBuilder subclasses they expose.
        """
        for plugin_path in self._plugin_paths:
            if plugin_path in self._loaded_plugin_paths:
                continue

            module = __import__(plugin_path, fromlist=["*"])
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseFeatureBuilder)
                    and attr is not BaseFeatureBuilder
                ):
                    self.register_builder(attr())

            self._loaded_plugin_paths.add(plugin_path)

    def get(self, name: str) -> Optional[BaseFeatureBuilder[Any]]:
        """Return a registered builder by feature name."""
        return self._builders.get(name)

    def get_all(self) -> dict[str, BaseFeatureBuilder[Any]]:
        """Return all registered builders."""
        return self._builders

    def has(self, name: str) -> bool:
        """Return True when a builder is registered for the given feature."""
        return name in self._builders

    def list_features(self) -> list[str]:
        """Return all registered feature names in sorted order."""
        return sorted(self._builders.keys())
