"""Windows active-console session observation boundary."""

import ctypes
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from federation.power_action import normalize_timestamp
from federation.windows_display_native import PlatformProbe, WindowsPlatformError


_NO_ACTIVE_CONSOLE_SESSION = 0xFFFFFFFF
_UOI_FLAGS = 1
_WSF_VISIBLE = 0x0001


@dataclass(slots=True, frozen=True)
class WindowsSessionSnapshot:
    process_session_id: int
    active_console_session_id: int | None
    process_in_active_console_session: bool
    visible_interactive_window_station: bool
    idle_seconds: float
    locally_prohibited: bool
    session_locked: bool
    observed_at: datetime

    def __post_init__(self):
        if isinstance(self.process_session_id, bool) or not isinstance(
            self.process_session_id, int
        ):
            raise TypeError("process_session_id must be an integer")
        if self.process_session_id < 0:
            raise ValueError("process_session_id must not be negative")
        if self.active_console_session_id is not None and (
            isinstance(self.active_console_session_id, bool)
            or not isinstance(self.active_console_session_id, int)
            or self.active_console_session_id < 0
        ):
            raise ValueError("active_console_session_id is invalid")
        for field in (
            "process_in_active_console_session",
            "visible_interactive_window_station",
            "locally_prohibited",
            "session_locked",
        ):
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"{field} must be a bool")
        if isinstance(self.idle_seconds, bool) or not isinstance(
            self.idle_seconds, (int, float)
        ):
            raise TypeError("idle_seconds must be numeric")
        if not 0 <= self.idle_seconds < float("inf"):
            raise ValueError("idle_seconds must be finite and non-negative")
        object.__setattr__(
            self,
            "idle_seconds",
            float(self.idle_seconds),
        )
        object.__setattr__(
            self,
            "observed_at",
            normalize_timestamp(self.observed_at, "observed_at"),
        )


class WindowsSessionProbe(ABC):
    @abstractmethod
    def observe(self):
        """Observe the current helper process and active console session."""


class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))


class _UserObjectFlags(ctypes.Structure):
    _fields_ = (
        ("fInherit", ctypes.c_int),
        ("fReserved", ctypes.c_int),
        ("dwFlags", ctypes.c_uint),
    )


class NativeWindowsSessionProbe(WindowsSessionProbe):
    """Standard-library Win32 probe for the current process session only."""

    def __init__(self, *, platform, clock):
        from federation.windows_display_adapter import WindowsDisplayFailureCode

        if not isinstance(platform, PlatformProbe):
            raise TypeError("platform must be a PlatformProbe")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not platform.is_windows():
            raise WindowsPlatformError(
                WindowsDisplayFailureCode.UNSUPPORTED_PLATFORM
            )
        self._clock = clock
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

    def observe(self):
        process_session = ctypes.c_uint()
        process_id = self._kernel32.GetCurrentProcessId()
        if not self._kernel32.ProcessIdToSessionId(
            process_id,
            ctypes.byref(process_session),
        ):
            raise WindowsPlatformError("session_observation_failed")
        active = self._kernel32.WTSGetActiveConsoleSessionId()
        active_session = (
            None if active == _NO_ACTIVE_CONSOLE_SESSION else int(active)
        )
        station = self._user32.GetProcessWindowStation()
        flags = _UserObjectFlags()
        needed = ctypes.c_uint()
        visible = bool(
            station
            and self._user32.GetUserObjectInformationW(
                station,
                _UOI_FLAGS,
                ctypes.byref(flags),
                ctypes.sizeof(flags),
                ctypes.byref(needed),
            )
            and flags.dwFlags & _WSF_VISIBLE
        )
        last_input = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
        if not self._user32.GetLastInputInfo(ctypes.byref(last_input)):
            raise WindowsPlatformError("session_observation_failed")
        elapsed_ms = (
            int(self._kernel32.GetTickCount64()) - int(last_input.dwTime)
        )
        input_desktop = self._user32.OpenInputDesktop(0, False, 0x0100)
        locked = not bool(input_desktop)
        if input_desktop:
            self._user32.CloseDesktop(input_desktop)
        return WindowsSessionSnapshot(
            process_session_id=int(process_session.value),
            active_console_session_id=active_session,
            process_in_active_console_session=(
                active_session is not None
                and process_session.value == active_session
            ),
            visible_interactive_window_station=visible,
            idle_seconds=max(0.0, elapsed_ms / 1000.0),
            locally_prohibited=False,
            session_locked=locked,
            observed_at=self._clock(),
        )
