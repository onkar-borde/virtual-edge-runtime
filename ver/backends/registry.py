"""Backend registry.

Backends register themselves here. The runtime picks one by name, by the
VER_BACKEND env var, or by autodetection (highest priority that reports
available()).
"""

from __future__ import annotations

import os
from typing import Type

from ..hal.base import Backend
from ..hal.errors import BackendNotAvailable

_REGISTRY: dict[str, tuple[int, Type[Backend]]] = {}


def register(cls: Type[Backend], priority: int = 0) -> Type[Backend]:
    """Register a backend. Higher priority wins during autodetect.

    Mock sits at priority -100 so it's always the last resort but never
    an outright failure.
    """
    _REGISTRY[cls.name] = (priority, cls)
    return cls


def available_backends() -> list[str]:
    return [name for name, (_, cls) in _REGISTRY.items() if cls.available()]


def all_backends() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str) -> Type[Backend]:
    if name not in _REGISTRY:
        raise BackendNotAvailable(
            f"unknown backend {name!r}; known: {', '.join(all_backends()) or 'none'}"
        )
    _, cls = _REGISTRY[name]
    if not cls.available():
        raise BackendNotAvailable(f"backend {name!r} cannot run on this machine")
    return cls


def autodetect() -> Type[Backend]:
    """Pick the best backend that actually works here."""
    override = os.environ.get("VER_BACKEND")
    if override:
        return get(override)

    candidates = sorted(
        ((prio, cls) for prio, cls in _REGISTRY.values() if cls.available()),
        key=lambda item: item[0],
        reverse=True,
    )
    if not candidates:
        raise BackendNotAvailable("no backend is available on this machine")
    return candidates[0][1]


def _load_builtins() -> None:
    from .mock.backend import MockBackend

    register(MockBackend, priority=-100)

    try:
        from .laptop.backend import LaptopBackend
    except ImportError:
        pass
    else:
        register(LaptopBackend, priority=10)

    try:
        from .esp32.backend import ESP32Backend
    except ImportError:
        pass
    else:
        # Below laptop on purpose. An ESP32 on USB is not a platform you
        # run on -- it's a peripheral of the host. The laptop backend
        # reaches through it for pins. Registered anyway so it can be
        # selected explicitly (VER_BACKEND=esp32) and tested directly.
        register(ESP32Backend, priority=0)


_load_builtins()
