"""Narrow, Windows-only native display boundary."""

import ctypes
import os
from abc import ABC, abstractmethod

from federation.power_action import PowerAction


_HWND_BROADCAST = 0xFFFF
_WM_SYSCOMMAND = 0x0112
_SC_MONITORPOWER = 0xF170
_SMTO_ABORTIFHUNG = 0x0002
_DISPLAY_PARAMETERS = {
    PowerAction.DISPLAY_OFF: 2,
    PowerAction.DISPLAY_ON: -1,
}
_MAX_TIMEOUT_MS = 5000


class PlatformProbe(ABC):
    """Typed platform check used before any Windows DLL is loaded."""

    @abstractmethod
    def is_windows(self):
        """Return whether the current process is running on Windows."""


class SystemPlatformProbe(PlatformProbe):
    def is_windows(self):
        return os.name == "nt"


class NativeDisplayApi(ABC):
    """The only native operation exposed to the governed adapter."""

    timeout_ms: int

    @abstractmethod
    def invoke(self, action):
        """Attempt one typed display action."""


class WindowsNativeError(Exception):
    """A safe native-boundary failure without raw Windows error text."""


class WindowsPlatformError(WindowsNativeError):
    def __init__(self, code):
        super().__init__("Windows native display calls require Windows")
        self.code = code


def _load_user32():
    return ctypes.WinDLL("user32", use_last_error=True)


class WindowsNativeDisplayApi(NativeDisplayApi):
    """Trusted low-level primitive with no caller-controlled native constants.

    This backend does not perform authorization. Trusted composition must own
    it through WindowsDisplayAdapter; CLI, transport, user input, and arbitrary
    callbacks must never receive this object or its invoke method.
    """

    def __init__(self, *, platform, timeout_ms):
        from federation.windows_display_adapter import WindowsDisplayFailureCode

        if not isinstance(platform, PlatformProbe):
            raise TypeError("platform must be a PlatformProbe")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError("timeout_ms must be an integer")
        if not 1 <= timeout_ms <= _MAX_TIMEOUT_MS:
            raise ValueError("timeout_ms must be between 1 and 5000")
        if not platform.is_windows():
            raise WindowsPlatformError(
                WindowsDisplayFailureCode.UNSUPPORTED_PLATFORM
            )
        self.timeout_ms = timeout_ms
        self._user32 = _load_user32()
        self._send_message_timeout = self._user32.SendMessageTimeoutW
        self._send_message_timeout.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_size_t),
        )
        self._send_message_timeout.restype = ctypes.c_size_t

    def invoke(self, action):
        if not isinstance(action, PowerAction):
            raise TypeError("action must be a PowerAction")
        try:
            parameter = _DISPLAY_PARAMETERS[action]
        except KeyError as exc:
            raise ValueError("native action must be a display action") from exc
        message_result = ctypes.c_size_t()
        try:
            result = self._send_message_timeout(
                _HWND_BROADCAST,
                _WM_SYSCOMMAND,
                _SC_MONITORPOWER,
                parameter,
                _SMTO_ABORTIFHUNG,
                self.timeout_ms,
                ctypes.byref(message_result),
            )
        except OSError as exc:
            raise WindowsNativeError("bounded native display call failed") from exc
        return bool(result)
