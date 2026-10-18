"""Namespaced cache keys that can be invalidated per namespace.

Django's cache API cannot delete "all keys with a prefix" (the file and memcached
backends do not support it), so each namespace has a *generation* number stored in the
cache itself and embedded in every key. Bumping the generation makes every old entry
unreachable at once, for all processes sharing the cache; the stale entries simply
expire. Throttle counters and other cache users are untouched.
"""

from django.core.cache import cache

NAMESPACES: tuple[str, ...] = ("route", "response", "geocode")


def _generation_key(namespace: str) -> str:
    return f"cache-generation:{namespace}"


def generation(namespace: str) -> int:
    """Current generation of ``namespace`` (created as 1 on first use)."""
    if namespace not in NAMESPACES:
        raise ValueError(f"Unknown cache namespace {namespace!r}")
    value = cache.get(_generation_key(namespace))
    if value is None:
        cache.add(_generation_key(namespace), 1, timeout=None)
        value = cache.get(_generation_key(namespace), 1)
    return int(value)


def namespaced_key(namespace: str, digest: str) -> str:
    """Cache key for ``digest`` within ``namespace``'s current generation."""
    return f"{namespace}:g{generation(namespace)}:{digest}"


def invalidate(namespace: str) -> int:
    """Invalidate every entry in ``namespace``; returns the new generation."""
    new = generation(namespace) + 1
    cache.set(_generation_key(namespace), new, timeout=None)
    return new
