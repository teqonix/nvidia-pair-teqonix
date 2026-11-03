# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
"""Environment-derived runtime configuration for the gufo-masquerade shim.

Every knob is an environment variable with a default that works for the
amd-ai-core-two deployment (shim on ``127.0.0.1:8081``, GUFO on
``127.0.0.1:18080``), so the process needs no configuration to start. The
knobs exist so the deployment can be tuned without a code change.

Parsing rules are intentionally forgiving:

*   :func:`env_bool` treats anything that is not ``1/true/yes/on`` (case and
    whitespace insensitive) as ``False``, so ``SHIM_ACCESS_LOG=0`` does what
    operators expect.
*   :func:`env_int` raises :class:`ValueError` for garbage rather than
    silently reverting to the default: a typo in a timeout should crash the
    shim at startup (systemd restarts it, the operator sees the message)
    rather than run with an unintended value forever.

:func:`describe` is the redaction-safe projection used by the shim's own
diagnostics endpoint; it must never grow a raw secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

#: Single source of truth for the shim version. Reported on ``/props``
#: (``masquerade.shim_version``) and ``/shim/config``; keep in sync with the
#: ``SHIM_IMAGE`` tag used by ops/env.sample when bumping.
DEFAULT_VERSION = "1.1.0"

#: Values accepted (lower-cased, stripped) by :func:`env_bool` as truthy.
TRUTHY_VALUES = ("1", "true", "yes", "on")

#: Supported values of ``SHIM_PULL_POLICY``; see :attr:`Config.pull_policy`.
PULL_POLICIES = ("present-ok", "fake", "reject")


def env_bool(name: str, default: bool, environ: dict[str, str] | None = None) -> bool:
    """Read ``name`` from ``environ`` as a boolean.

    Args:
        name: Environment variable name.
        default: Returned when the variable is absent.
        environ: Mapping to read from; defaults to :data:`os.environ`.

    Returns:
        ``True`` when the value is one of :data:`TRUTHY_VALUES` after
        stripping and lower-casing; anything else (including empty or
        whitespace-only) is ``False``.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUTHY_VALUES


def env_int(name: str, default: int, environ: dict[str, str] | None = None) -> int:
    """Read ``name`` from ``environ`` as an integer.

    An absent or blank variable yields ``default``. A non-integer value
    raises :class:`ValueError` with the offending variable named, so a
    misconfiguration is loud at startup instead of silent at runtime.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None


def env_choice(
    name: str, default: str, allowed: tuple[str, ...],
    environ: dict[str, str] | None = None,
) -> str:
    """Read ``name`` from ``environ`` constrained to ``allowed``.

    Unknown values raise :class:`ValueError` listing what is accepted. This
    guards behaviour switches (e.g. the pull policy) where silently running
    with a different policy would be worse than not running.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in allowed:
        raise ValueError(
            f"{name}={raw!r} is not one of {', '.join(allowed)}"
        )
    return value


@dataclass(frozen=True)
class Config:
    """Immutable, fully-typed view of the shim's environment.

    Attributes:
        listen_host: Interface the shim binds. Loopback is the deployment
            shape: PAIR talks to the shim on the same host, and exposure is
            controlled upstream (Podman port maps / reverse proxy).
        listen_port: TCP port the shim binds (PAIR's llamacpp slot).
        gufo_base_url: Base URL of the GUFO upstream, no trailing slash.
        gufo_api_key: Bearer token injected on every upstream request; the
            shim holds it so PAIR (whose llamacpp profile has no credential
            binding) never needs to know it. Empty means no auth upstream.
        router_role: The ``role`` value PAIR's readiness matcher requires.
            Overridable only to re-point the shim at a differently-shaped
            manifest without a code change.
        pull_policy: How ``POST /models`` answers a pull request:

            ``present-ok``
                202 + terminal SSE frames if the model is already resident in
                GUFO, otherwise 501 with operator instructions.
            ``fake``
                Simulated download with progress frames, for demos and for
                exercising PAIR's progress UI. Never for production: it tells
                the operator a model arrived when it did not.
            ``reject``
                Always 501.
        refuse_unload_serving: When ``True`` (default), ``POST /models/unload``
            and ``DELETE /models`` refuse (409) to evict the model GUFO is
            actively serving. Acking it would be a lie that surfaces later as
            inference from a model PAIR believes is gone.
        state_path: File where the deleted-model overlay is persisted so a
            shim restart does not resurrect models PAIR removed.
        connect_timeout_s: Per-request upstream connect timeout. Short, so a
            dead GUFO fails fast.
        discovery_timeout_s: Read timeout for control-plane probes
            (``/props``, ``/models``, ``/health``).
        read_timeout_s: Read timeout for relayed inference. Must exceed the
            longest sane generation.
        inventory_ttl_s: How long a cached GUFO ``/models`` response may be
            trusted for the inventory views.
        fake_bytes: Synthetic download size reported by the ``fake`` policy.
        fake_ticks: Number of progress frames emitted by the ``fake`` policy.
        fake_tick_s: Delay between fake progress frames.
        access_log: Emit uvicorn access logs (noisy next to PAIR's 5s polls).
        shim_version: Reported on ``/props`` and the introspection surface.
    """

    environ: dict[str, str] = field(default_factory=os.environ.copy, repr=False)

    listen_host: str = field(init=False)
    listen_port: int = field(init=False)
    gufo_base_url: str = field(init=False)
    gufo_api_key: str = field(init=False)
    router_role: str = field(init=False)
    pull_policy: str = field(init=False)
    refuse_unload_serving: bool = field(init=False)
    state_path: str = field(init=False)
    connect_timeout_s: float = field(init=False)
    discovery_timeout_s: float = field(init=False)
    read_timeout_s: float = field(init=False)
    inventory_ttl_s: float = field(init=False)
    sse_keepalive_s: float = field(init=False)
    fake_bytes: int = field(init=False)
    fake_ticks: int = field(init=False)
    fake_tick_s: float = field(init=False)
    access_log: bool = field(init=False)
    log_level: str = field(init=False)
    shim_version: str = DEFAULT_VERSION

    def __post_init__(self) -> None:
        env = self.environ
        # The dataclass is frozen; derived fields are populated once here via
        # object.__setattr__ (the standard frozen-dataclass pattern), so the
        # instance is immutable after construction and safe to share.
        object.__setattr__(self, "listen_host", env.get("SHIM_HOST", "127.0.0.1"))
        object.__setattr__(self, "listen_port", env_int("SHIM_PORT", 8081, env))
        object.__setattr__(
            self, "gufo_base_url", env.get("GUFO_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
        )
        object.__setattr__(self, "gufo_api_key", env.get("GUFO_API_KEY", ""))
        object.__setattr__(self, "router_role", env.get("SHIM_ROUTER_ROLE", "router"))
        object.__setattr__(
            self, "pull_policy", env_choice("SHIM_PULL_POLICY", "present-ok", PULL_POLICIES, env)
        )
        object.__setattr__(
            self, "refuse_unload_serving", env_bool("SHIM_REFUSE_UNLOAD_SERVING", True, env)
        )
        object.__setattr__(
            self, "state_path", env.get("SHIM_STATE_PATH", "/var/lib/gufo-masquerade/state.json")
        )
        object.__setattr__(self, "connect_timeout_s", env_int("SHIM_CONNECT_TIMEOUT_S", 5, env))
        object.__setattr__(self, "discovery_timeout_s", env_int("SHIM_DISCOVERY_TIMEOUT_S", 10, env))
        object.__setattr__(self, "read_timeout_s", env_int("SHIM_READ_TIMEOUT_S", 1800, env))
        object.__setattr__(self, "inventory_ttl_s", env_int("SHIM_INVENTORY_TTL_S", 2, env))
        object.__setattr__(self, "sse_keepalive_s", env_int("SHIM_SSE_KEEPALIVE_S", 5, env))
        object.__setattr__(self, "fake_bytes", env_int("SHIM_FAKE_BYTES", 4_000_000_000, env))
        object.__setattr__(self, "fake_ticks", env_int("SHIM_FAKE_TICKS", 10, env))
        object.__setattr__(self, "fake_tick_s", env_int("SHIM_FAKE_TICK_MS", 200, env) / 1000.0)
        object.__setattr__(self, "access_log", env_bool("SHIM_ACCESS_LOG", False, env))
        object.__setattr__(
            self, "log_level", env.get("SHIM_LOG_LEVEL", "info").strip().lower() or "info"
        )

    @property
    def gufo_headers(self) -> dict[str, str]:
        """Auth headers to inject on every upstream request (empty when no key)."""
        if self.gufo_api_key:
            return {"Authorization": f"Bearer {self.gufo_api_key}"}
        return {}

    def upstream_timeout(self, read: float | None = None) -> Any:
        """Build an ``httpx.Timeout`` for upstream calls.

        Args:
            read: Read timeout in seconds; ``None`` selects
                :attr:`read_timeout_s` (inference-shaped calls).
        """
        import httpx  # local import: config stays importable without deps

        return httpx.Timeout(
            self.connect_timeout_s,
            read=self.read_timeout_s if read is None else read,
        )

    def describe(self) -> dict[str, Any]:
        """Redaction-safe snapshot for ``GET /shim/config``.

        Never add :attr:`gufo_api_key` here; ``api_key_configured`` is the
        projection operators need.
        """
        return {
            "listen": f"{self.listen_host}:{self.listen_port}",
            "gufo_base_url": self.gufo_base_url,
            "router_role": self.router_role,
            "pull_policy": self.pull_policy,
            "refuse_unload_serving": self.refuse_unload_serving,
            "state_path": self.state_path,
            "api_key_configured": bool(self.gufo_api_key),
            "shim_version": self.shim_version,
        }


#: The process-wide configuration. Tests construct their own :class:`Config`
#: with an explicit ``environ`` instead of mutating this.
CONFIG = Config()
