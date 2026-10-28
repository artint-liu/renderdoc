"""Process spawning that works in both hosting environments.

qrenderdoc's embedded python (3.6) may lack the ``subprocess`` module, so
this tries it first and falls back to a direct CreateProcessW call through
ctypes. Created processes get CREATE_NO_WINDOW (ignored by GUI apps like
qrenderdoc, prevents console flashes for console helpers).
"""
import ctypes
import os

CREATE_NO_WINDOW = 0x08000000


def _env_block(extra_env):
    """Build a UTF-16 Windows environment block (sorted, double-NUL ended)
    from the parent environment plus extra overrides."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    items = ["%s=%s" % (k, v) for k, v in sorted(env.items())]
    return ("\0".join(items) + "\0\0").encode("utf-16-le")


def _spawn_ctypes(cmdline, workdir, extra_env=None):
    from ctypes import wintypes

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateProcessW.restype = wintypes.BOOL
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION)]

    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    # first parameter is the app name, second the full command line; passing
    # the app name explicitly avoids path quirks with spaces
    exe = cmdline.split(" ", 1)[0].strip('"')
    env_buf = None
    if extra_env:
        block = _env_block(extra_env)
        env_buf = ctypes.create_string_buffer(block, len(block))
    ok = k32.CreateProcessW(exe, cmdline, None, None, False,
                            CREATE_NO_WINDOW, env_buf, workdir or None,
                            ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise OSError("CreateProcessW failed: error %d (cmdline=%r)"
                      % (ctypes.get_last_error(), cmdline))
    pid = int(pi.dwProcessId)
    k32.CloseHandle(pi.hProcess)
    k32.CloseHandle(pi.hThread)
    return pid


def spawn(exe, args="", workdir=None, extra_env=None):
    """Launch ``exe`` (quoted command line built from args) detached; returns
    the new process id. extra_env overrides environment variables for the
    child. Raises OSError on failure."""
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        raise OSError("executable not found: %s" % exe)
    cmdline = '"%s"' % exe
    if args:
        cmdline += " " + args
    workdir = os.path.abspath(workdir) if workdir else None
    try:
        import subprocess
        env = None
        if extra_env:
            env = dict(os.environ)
            env.update(extra_env)
        proc = subprocess.Popen(cmdline, cwd=workdir, env=env,
                                creationflags=CREATE_NO_WINDOW)
        return proc.pid
    except ImportError:
        return _spawn_ctypes(cmdline, workdir, extra_env)
