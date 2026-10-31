"""Keep unattended Windows SDK output independent of a console or pipe reader.

The finance SDK writes through the UCRT, bypassing Python logging. A console
selection (or an undrained inherited pipe) can otherwise block its calling
thread indefinitely. Configure this once at application startup, before native
libraries/worker threads start. Application file logging and logbus are separate.
"""
import os
import sys

_configured = False
_original_streams = ()


def isolate_runtime_output() -> None:
    """Redirect CRT descriptors, Win32 handles and Python streams to NUL.

    Never flush the old streams: their destination may already be blocked.
    This changes only this process, including when launched with pythonw.
    """
    global _configured, _original_streams
    if sys.platform != 'win32' or _configured:
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.SetStdHandle.restype = wintypes.BOOL
    fd = os.open(os.devnull, os.O_WRONLY | os.O_BINARY)
    try:
        # Python and the SDK both use UCRT descriptors. dup2 also supports
        # destinations without an existing descriptor (pythonw/no console).
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        for number, descriptor in ((-11, 1), (-12, 2)):
            if not kernel.SetStdHandle(number, msvcrt.get_osfhandle(descriptor)):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        # A no-console process may allocate NUL as descriptor 1 or 2 itself.
        if fd not in (1, 2):
            os.close(fd)
    # WindowsConsoleIO caches its original console handle; dup2 alone does not
    # redirect it. Retain originals so replacing them cannot flush/close them.
    _original_streams = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    sys.stdout = sys.__stdout__ = open(os.devnull, 'w', encoding='utf-8')
    sys.stderr = sys.__stderr__ = open(os.devnull, 'w', encoding='utf-8')
    _configured = True
