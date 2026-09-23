"""Stop Windows blanking the screen or sleeping while the watcher is running.

There are two independent things that turn a screen off, and a live trading page
needs both held back:

1. **The browser's own idea of idleness.** Handled in the page by the
   `navigator.wakeLock` screen lock — see `report.html`. That is enough while the
   tab is visible and focused.
2. **Windows' power policy.** The wake lock is released the moment the tab is
   hidden, so a minimised browser, a locked workstation or a second virtual
   desktop lets the display timer and the sleep timer run again. Only a process
   holding `SetThreadExecutionState` prevents that, and that has to be this
   process, because it is the one that is always running.

So the button in the UI drives both: the page takes the wake lock, and it calls
this module through `/api/awake` for the half the page cannot reach.

## What this does and does not do

`ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED` tells Windows "keep the
system and the display up until I say otherwise". It is the same call a video
player makes.

It does **not** override:

* the **lock screen** — a workstation lock timer is a security setting and is not
  an idle timer; the machine stays awake behind the lock;
* a **laptop lid close** or a manual sleep;
* **battery-saver** policies that force the display off regardless.

It is per-thread state and it is cleared when the process exits, so nothing is
left behind if the watcher is killed rather than stopped — Windows resumes normal
power management on its own. `release()` is still called on shutdown so the
change is undone immediately rather than at process teardown.

On any non-Windows platform every function here is a no-op that reports itself as
unavailable, rather than pretending to have worked.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

# winbase.h
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

_lock = threading.Lock()
_state = {"on": False, "reason": ""}


def available() -> bool:
    """True only where `SetThreadExecutionState` actually exists."""
    return sys.platform == "win32"


def _set(flags: int) -> bool:
    if not available():
        return False
    import ctypes

    # Returns the PREVIOUS state, or 0 on failure. 0 is the only error signal
    # this API gives — there is no GetLastError contract for it.
    prev = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    return prev != 0


def acquire(display: bool = True) -> bool:
    """Keep the machine (and by default the screen) awake. Idempotent.

    ``display=False`` keeps the system running but lets the monitor blank — right
    for a headless box you only want to keep ticking, wrong for the case this was
    built for, which is watching a chart.
    """
    with _lock:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if display:
            flags |= ES_DISPLAY_REQUIRED
        ok = _set(flags)
        _state["on"] = bool(ok)
        _state["reason"] = "" if ok else (
            "not supported on this platform" if not available()
            else "SetThreadExecutionState refused")
        if ok:
            log.info("keep-awake ON (system%s)", " + display" if display else "")
        return ok


def release() -> bool:
    """Hand power management back to Windows. Safe to call when never acquired."""
    with _lock:
        ok = _set(ES_CONTINUOUS)          # continuous with no requirements = clear
        _state["on"] = False
        _state["reason"] = ""
        if ok:
            log.info("keep-awake OFF")
        return ok


def status() -> dict:
    with _lock:
        return {
            "supported": available(),
            "on": bool(_state["on"]),
            "reason": _state["reason"],
            "note": ("Holds the display and sleep timers off. Does NOT stop the "
                     "workstation lock, a lid close, or battery saver."),
        }


def toggle(on: bool, display: bool = True) -> dict:
    (acquire(display=display) if on else release())
    return status()
