"""Namespaced cache keys that can be invalidated per namespace.

Django's cache API cannot delete "all keys with a prefix" (the file and memcached
backends do not support it), so each namespace has a *generation* number stored in the
cache itself and embedded in every key. Bumping the generation makes every old entry
unreachable at once, for all processes sharing the cache; the stale entries simply
expire. Throttle counters and other cache users are untouched.

Generations never go backwards: a missing counter (first use, or evicted - the file
cache culls random entries when full) is re-created from the current time in
nanoseconds, so it can never collide with keys written under an older generation.
"""

import time

from django.core.cache import cache

NAMESPACES: tuple[str, ...] = ("route", "response", "geocode")


def _generation_key(namespace: str) -> str:
    return f"cache-generation:{namespace}"


def generation(namespace: str) -> int:
    """Current generation of ``namespace`` (created from the clock on first use)."""
    if namespace not in NAMESPACES:
        raise ValueError(f"Unknown cache namespace {namespace!r}")
    value = cache.get(_generation_key(namespace))
    if value is None:
        cache.add(_generation_key(namespace), _now_ns(), timeout=None)
        value = cache.get(_generation_key(namespace), _now_ns())
    return int(value)


def _now_ns() -> int:
    return time.time_ns()


def namespaced_key(namespace: str, digest: str) -> str:
    """Cache key for ``digest`` within ``namespace``'s current generation."""
    return f"{namespace}:g{generation(namespace)}:{digest}"


def invalidate(namespace: str) -> int:
    """Invalidate every entry in ``namespace``; returns the new generation."""
    new = max(generation(namespace) + 1, _now_ns())
    cache.set(_generation_key(namespace), new, timeout=None)
    return new
