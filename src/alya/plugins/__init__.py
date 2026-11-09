"""Плагины с декларацией разрешений (Phase 8)."""

from alya.plugins.registry import (
    FORBIDDEN,
    PERMISSIONS,
    LoadedPlugin,
    PluginContext,
    PluginError,
    PluginHost,
    PluginPermissionError,
    load_plugins,
)

__all__ = ["FORBIDDEN", "PERMISSIONS", "LoadedPlugin", "PluginContext", "PluginError",
           "PluginHost", "PluginPermissionError", "load_plugins"]
