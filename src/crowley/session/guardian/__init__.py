"""Process-lifetime input guardian package.

Lock hierarchy:

1. ``InputLease._lock`` protects the lease descriptor and is acquired only by
   lease acquire/release; it is never acquired under ``InputGuardian._lock``.
2. ``InputGuardian._lock`` protects generation, action state, physical holds,
   process registries, calibration tickets, and watchdog publication. Blocking
   ancestry lookups, physical release, revocation callbacks, and process waits
   run outside it. The bounded ``start`` and ``release_admission_gate``
   callbacks used by admission methods execute under it and must be
   nonblocking; they are the exception needed to linearize registration.
3. The independent watchdog owns its pipe-state and release state in its
   process; it never acquires the lease or guardian locks.

Observation snapshots state under ``InputGuardian._lock``, performs ordered
correlation phases without that lock, and commits only if the same generation
and action are still current under the lock. Revocation releases physical
input before invoking callbacks.
"""

from .core import InputGuardian, _watchdog_entry  # noqa: F401
from .correlation import ActionCorrelation
from .lease import GuardianError, InputLease

__all__ = ["ActionCorrelation", "GuardianError", "InputGuardian", "InputLease"]
