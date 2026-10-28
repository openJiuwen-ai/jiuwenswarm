"""Gateway-side exit-code contract with the supervisor (``jiuwenswarm.app``).

Only the constants are vendored: the supervising loop itself runs in the
jiuwenswarm parent process, while the Gateway child just needs to agree on the
codes it reports and the env var it reads.
"""

from __future__ import annotations

# A supervised Gateway exits with this code to ask for a respawn (config reload
# failure). os.execv cannot be used under a supervisor: on Windows it starts a
# new PID and exits the old one with 0, which reads as the Gateway stopping.
GATEWAY_RESTART_EXIT_CODE = 75
# Children exit with this code (EX_SOFTWARE) when debug_dump's crash handler
# catches an unhandled main-thread exception. Windows cannot tell a plain
# Python fatal exit (1) from taskkill /F (1), so a crash the supervisor should
# respawn has to announce itself; native crashes bypass Python entirely and
# arrive as NTSTATUS codes instead.
CRASH_EXIT_CODE = 70
# Set to the supervisor's PID so a child can tell it is supervised, even when
# the variable leaks into an unrelated process through a saved environment.
SUPERVISOR_PID_ENV = "JIUWENSWARM_SUPERVISOR_PID"

__all__ = [
    "GATEWAY_RESTART_EXIT_CODE",
    "CRASH_EXIT_CODE",
    "SUPERVISOR_PID_ENV",
]
