"""Crowley configuration contracts and service-specific resolvers."""

from .host import HostConfig, HostConfigError, host_config_path, load_host_config
from .resolved import (
    CompanionResolvedConfig,
    OpsResolvedConfig,
    ResolutionError,
    resolve_companion,
    resolve_ops,
)
from .tunables import (
    CompanionTunables,
    OpsTunables,
    TunablesError,
    load_companion_tunables,
)

__all__ = [
    "CompanionResolvedConfig",
    "CompanionTunables",
    "HostConfig",
    "HostConfigError",
    "OpsResolvedConfig",
    "OpsTunables",
    "ResolutionError",
    "TunablesError",
    "host_config_path",
    "load_companion_tunables",
    "load_host_config",
    "resolve_companion",
    "resolve_ops",
]
