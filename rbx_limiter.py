"""
RBX Limiter - Roblox instance manager with per-instance CPU / RAM limits.
Skin: ScroomMacro art from scroom_assets.py (keep that file next to this one).

Windows only for real limiting (pure ctypes, no pip installs needed).
On other OSes it starts in a demo mode with fake instances so the UI can be previewed.

How the limits work
  CPU  : each Roblox process is put in its own Windows Job Object with a hard CPU-rate cap
         (percent of the WHOLE machine, e.g. 12% on an 8-core PC ~= 1 core).
  RAM  : HARD CAP -> hard maximum working set (Windows pages the rest out, game keeps running)
         TRIM     -> empties the working set every 30 s (softer, RAM creeps back up between trims)
  Prio : process priority class (Idle / Below normal / Normal).
Limits are removed again when you close the app with the mushroom button.
Run as administrator for RAM hard caps (the app offers to relaunch elevated).
"""
import atexit
import faulthandler
import json
import os
import subprocess
import sys
import threading
import time
import traceback

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    raise SystemExit("tkinter is missing - install Python from python.org (it includes tkinter).")

try:
    import scroom_assets as ART
except ImportError:
    raise SystemExit("scroom_assets.py must be in the same folder as rbx_limiter.py")

IS_WIN = sys.platform == "win32"
IS_COMPILED = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def own_exe():
    """Path of the .exe the user launched (works for PyInstaller and Nuitka onefile)."""
    if "__compiled__" in globals():
        return os.path.abspath(sys.argv[0])
    return sys.executable
APP_NAME = "RBX Limiter"
ROBLOX_EXES = {"robloxplayerbeta.exe"}
TICK_MS = 1000
TILE_MAX = (480, 360)    # biggest tile size used by TILE
GRACE_S = 60             # seconds a new instance loads before RAM cap / FPS freeze apply
EXIT_REASONS = {
    0xC0000005: "access violation",
    0xC0000017: "out of memory",
    0xC000012D: "out of memory (pagefile full)",
    0xC00000FD: "stack overflow",
    0xC0000409: "Roblox closed itself (fail-fast)",
    0xC000001D: "illegal instruction",
    0xC0000374: "heap corruption",
    0xE06D7363: "C++ exception",
    0x40010004: "killed by Windows",
    0xC000013A: "closed with Ctrl+C / shutdown",
}
TRIM_EVERY_S = 30
REENFORCE_EVERY_S = 10


# =====================================================================================
#  Win32 layer
# =====================================================================================
class Win32:
    demo = False

    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        self.C, self.W = C, W
        k32 = C.WinDLL("kernel32", use_last_error=True)
        adv = C.WinDLL("advapi32", use_last_error=True)
        u32 = C.WinDLL("user32", use_last_error=True)
        ntdll = C.WinDLL("ntdll")
        self.k32, self.adv, self.u32, self.ntdll = k32, adv, u32, ntdll
        H, DW, BOOL, SZ = W.HANDLE, W.DWORD, W.BOOL, C.c_size_t

        class PROCESSENTRY32W(C.Structure):
            _fields_ = [("dwSize", DW), ("cntUsage", DW), ("th32ProcessID", DW),
                        ("th32DefaultHeapID", C.c_void_p), ("th32ModuleID", DW),
                        ("cntThreads", DW), ("th32ParentProcessID", DW),
                        ("pcPriClassBase", C.c_long), ("dwFlags", DW),
                        ("szExeFile", C.c_wchar * 260)]

        class PMC_EX(C.Structure):
            _fields_ = [("cb", DW), ("PageFaultCount", DW), ("PeakWorkingSetSize", SZ),
                        ("WorkingSetSize", SZ), ("QuotaPeakPagedPoolUsage", SZ),
                        ("QuotaPagedPoolUsage", SZ), ("QuotaPeakNonPagedPoolUsage", SZ),
                        ("QuotaNonPagedPoolUsage", SZ), ("PagefileUsage", SZ),
                        ("PeakPagefileUsage", SZ), ("PrivateUsage", SZ)]

        class VM_COUNTERS_EX(C.Structure):
            _fields_ = [("PeakVirtualSize", SZ), ("VirtualSize", SZ), ("PageFaultCount", W.ULONG),
                        ("PeakWorkingSetSize", SZ), ("WorkingSetSize", SZ),
                        ("QuotaPeakPagedPoolUsage", SZ), ("QuotaPagedPoolUsage", SZ),
                        ("QuotaPeakNonPagedPoolUsage", SZ), ("QuotaNonPagedPoolUsage", SZ),
                        ("PagefileUsage", SZ), ("PeakPagefileUsage", SZ), ("PrivateUsage", SZ)]

        class CPU_RATE(C.Structure):
            _fields_ = [("ControlFlags", DW), ("CpuRate", DW)]

        class LUID(C.Structure):
            _fields_ = [("LowPart", DW), ("HighPart", C.c_long)]

        class TOKEN_PRIVILEGES(C.Structure):
            _fields_ = [("PrivilegeCount", DW), ("Luid", LUID), ("Attributes", DW)]

        self.PE, self.PMC, self.VMC, self.CPU_RATE = PROCESSENTRY32W, PMC_EX, VM_COUNTERS_EX, CPU_RATE
        self.LUID, self.TP = LUID, TOKEN_PRIVILEGES

        class MEMSTAT(C.Structure):
            _fields_ = [("dwLength", DW), ("dwMemoryLoad", DW), ("ullTotalPhys", C.c_ulonglong),
                        ("ullAvailPhys", C.c_ulonglong), ("ullTotalPageFile", C.c_ulonglong),
                        ("ullAvailPageFile", C.c_ulonglong), ("ullTotalVirtual", C.c_ulonglong),
                        ("ullAvailVirtual", C.c_ulonglong), ("ullAvailExtendedVirtual", C.c_ulonglong)]
        self.MEMSTAT = MEMSTAT

        class MONITORINFO(C.Structure):
            _fields_ = [("cbSize", DW), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", DW)]
        self.MONITORINFO = MONITORINFO

        def sig(fn, res, *args):
            fn.restype, fn.argtypes = res, list(args)

        sig(k32.CreateToolhelp32Snapshot, H, DW, DW)
        sig(k32.Process32FirstW, BOOL, H, C.POINTER(PROCESSENTRY32W))
        sig(k32.Process32NextW, BOOL, H, C.POINTER(PROCESSENTRY32W))
        sig(k32.OpenProcess, H, DW, BOOL, DW)
        sig(k32.CloseHandle, BOOL, H)
        sig(k32.GetProcessTimes, BOOL, H, *[C.POINTER(W.FILETIME)] * 4)
        sig(k32.K32GetProcessMemoryInfo, BOOL, H, C.POINTER(PMC_EX), DW)
        sig(k32.K32EmptyWorkingSet, BOOL, H)
        sig(k32.CreateJobObjectW, H, C.c_void_p, W.LPCWSTR)
        sig(k32.AssignProcessToJobObject, BOOL, H, H)
        sig(k32.SetInformationJobObject, BOOL, H, C.c_int, C.c_void_p, DW)
        sig(k32.SetPriorityClass, BOOL, H, DW)
        sig(k32.GetPriorityClass, DW, H)
        sig(k32.IsProcessInJob, BOOL, H, H, C.POINTER(BOOL))
        sig(k32.QueryInformationJobObject, BOOL, H, C.c_int, C.c_void_p, DW, C.POINTER(DW))
        sig(k32.SetProcessWorkingSetSizeEx, BOOL, H, SZ, SZ, DW)
        sig(k32.GetProcessWorkingSetSizeEx, BOOL, H, C.POINTER(SZ), C.POINTER(SZ), C.POINTER(DW))
        sig(k32.TerminateProcess, BOOL, H, W.UINT)
        sig(k32.GetExitCodeProcess, BOOL, H, C.POINTER(DW))
        sig(k32.GlobalMemoryStatusEx, BOOL, C.c_void_p)
        sig(k32.CreateMutexW, H, C.c_void_p, BOOL, W.LPCWSTR)
        sig(k32.GetCurrentProcess, H)
        sig(adv.OpenProcessToken, BOOL, H, DW, C.POINTER(H))
        sig(adv.LookupPrivilegeValueW, BOOL, W.LPCWSTR, W.LPCWSTR, C.POINTER(LUID))
        sig(adv.AdjustTokenPrivileges, BOOL, H, BOOL, C.POINTER(TOKEN_PRIVILEGES), DW, C.c_void_p, C.c_void_p)
        sig(ntdll.NtQueryInformationProcess, C.c_long, H, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG))
        self.WNDENUMPROC = C.WINFUNCTYPE(BOOL, W.HWND, W.LPARAM)
        sig(u32.EnumWindows, BOOL, self.WNDENUMPROC, W.LPARAM)
        sig(u32.GetWindowThreadProcessId, DW, W.HWND, C.POINTER(DW))
        sig(u32.IsWindowVisible, BOOL, W.HWND)
        sig(u32.ShowWindow, BOOL, W.HWND, C.c_int)
        sig(u32.ShowWindowAsync, BOOL, W.HWND, C.c_int)
        sig(u32.SetWindowPos, BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
        sig(u32.SystemParametersInfoW, BOOL, W.UINT, W.UINT, C.c_void_p, W.UINT)
        sig(u32.MonitorFromWindow, H, W.HWND, DW)
        sig(u32.MonitorFromRect, H, C.POINTER(W.RECT), DW)
        sig(u32.GetMonitorInfoW, BOOL, H, C.POINTER(MONITORINFO))
        sig(u32.MessageBoxW, C.c_int, W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT)
        sig(u32.SetForegroundWindow, BOOL, W.HWND)
        sig(u32.GetParent, W.HWND, W.HWND)
        sig(u32.GetWindowLongW, C.c_long, W.HWND, C.c_int)
        sig(u32.SetWindowLongW, C.c_long, W.HWND, C.c_int, C.c_long)
        sig(u32.GetForegroundWindow, W.HWND)
        sig(u32.IsIconic, BOOL, W.HWND)
        sig(u32.IsWindow, BOOL, W.HWND)
        sig(u32.BringWindowToTop, BOOL, W.HWND)
        sig(u32.AttachThreadInput, BOOL, DW, DW, BOOL)
        sig(u32.GetWindowTextLengthW, C.c_int, W.HWND)
        sig(u32.GetWindowTextW, C.c_int, W.HWND, W.LPWSTR, C.c_int)
        sig(u32.SendMessageTimeoutW, C.c_void_p, W.HWND, W.UINT, W.WPARAM, W.LPCWSTR, W.UINT, W.UINT,
            C.POINTER(C.c_size_t))
        sig(k32.GetCurrentThreadId, DW)
        sig(k32.QueryFullProcessImageNameW, BOOL, H, DW, W.LPWSTR, C.POINTER(DW))
        sig(ntdll.NtSuspendProcess, C.c_long, H)
        sig(ntdll.NtResumeProcess, C.c_long, H)
        try:
            C.WinDLL("winmm").timeBeginPeriod(1)   # 1 ms timer resolution for the FPS limiter
        except Exception:
            pass

        class KEYBDINPUT(C.Structure):
            _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", DW), ("time", DW),
                        ("dwExtraInfo", C.c_void_p)]

        class MOUSEINPUT(C.Structure):
            _fields_ = [("dx", C.c_long), ("dy", C.c_long), ("mouseData", DW), ("dwFlags", DW),
                        ("time", DW), ("dwExtraInfo", C.c_void_p)]

        class _U(C.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(C.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", DW), ("u", _U)]

        self.INPUT = INPUT
        sig(u32.SendInput, W.UINT, W.UINT, C.POINTER(INPUT), C.c_int)

        self.cpu_count = os.cpu_count() or 1
        self._mutexes = []
        self._hwnds = {}           # pid -> cached top-level window
        self._hwnd_lock = threading.Lock()

    # ---- helpers ------------------------------------------------------------------
    def _err(self, what):
        code = self.C.get_last_error()
        return f"{what}: {'access denied' if code == 5 else 'error ' + str(code)}"

    def is_admin(self):
        try:
            return bool(self.C.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def relaunch_admin(self):
        if IS_COMPILED:
            exe, params = own_exe(), ""
        else:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            if not os.path.exists(exe):
                exe = sys.executable
            params = f'"{os.path.abspath(__file__)}"'
        r = self.C.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return r > 32

    def enable_privileges(self):
        tok = self.W.HANDLE()
        if not self.adv.OpenProcessToken(self.k32.GetCurrentProcess(), 0x20 | 0x8, self.C.byref(tok)):
            return
        for name in ("SeDebugPrivilege", "SeIncreaseBasePriorityPrivilege", "SeIncreaseQuotaPrivilege"):
            tp = self.TP()
            if self.adv.LookupPrivilegeValueW(None, name, self.C.byref(tp.Luid)):
                tp.PrivilegeCount, tp.Attributes = 1, 0x2
                self.adv.AdjustTokenPrivileges(tok, False, self.C.byref(tp), 0, None, None)
        self.k32.CloseHandle(tok)

    # ---- processes ----------------------------------------------------------------
    def list_pids(self, names):
        snap = self.k32.CreateToolhelp32Snapshot(0x2, 0)
        out = set()
        if not snap or snap == self.W.HANDLE(-1).value:
            return out
        pe = self.PE()
        pe.dwSize = self.C.sizeof(pe)
        ok = self.k32.Process32FirstW(snap, self.C.byref(pe))
        while ok:
            if pe.szExeFile.lower() in names:
                out.add(pe.th32ProcessID)
            ok = self.k32.Process32NextW(snap, self.C.byref(pe))
        self.k32.CloseHandle(snap)
        return out

    def open_proc(self, pid):
        """Open with as many rights as we can get. Returns (handle, rights) or (None, 0)."""
        QLI, QI, SQ, SI, T, SYN = 0x1000, 0x400, 0x100, 0x200, 0x1, 0x100000
        for rights in (QLI | QI | SQ | SI | T | SYN, QLI | SQ | SI | T | SYN, QLI | SQ | T | SYN,
                       QLI | SI | SYN, QLI | SYN):
            h = self.k32.OpenProcess(rights, False, pid)
            if h:
                return h, rights
        return None, 0

    def close(self, h):
        if h:
            self.k32.CloseHandle(h)

    def open_suspend(self, pid):
        return self.k32.OpenProcess(0x800, False, pid) or None   # PROCESS_SUSPEND_RESUME

    def suspend(self, h):
        return self.ntdll.NtSuspendProcess(h) == 0

    def resume(self, h):
        return self.ntdll.NtResumeProcess(h) == 0

    def exe_path(self, h):
        buf = self.C.create_unicode_buffer(1024)
        n = self.W.DWORD(1024)
        return buf.value if h and self.k32.QueryFullProcessImageNameW(h, 0, buf, self.C.byref(n)) else None

    def memory(self):
        """(ram_used_fraction, commit_used_bytes, commit_limit_bytes) for the whole PC.
        Commit = what programs have reserved; when it hits the limit (RAM + pagefile),
        Windows refuses new memory and programs like Roblox crash."""
        m = self.MEMSTAT()
        m.dwLength = self.C.sizeof(m)
        if not self.k32.GlobalMemoryStatusEx(self.C.byref(m)):
            return None
        return (1 - m.ullAvailPhys / m.ullTotalPhys, m.ullTotalPageFile - m.ullAvailPageFile,
                m.ullTotalPageFile, m.ullTotalPhys)

    def age(self, h):
        """Seconds since the process started (None if unknown)."""
        ft = [self.W.FILETIME() for _ in range(4)]
        if not self.k32.GetProcessTimes(h, *[self.C.byref(f) for f in ft]):
            return None
        created = (ft[0].dwHighDateTime << 32 | ft[0].dwLowDateTime) / 1e7 - 11644473600
        return max(0.0, time.time() - created)

    def exit_code(self, h):
        code = self.W.DWORD()
        return code.value if h and self.k32.GetExitCodeProcess(h, self.C.byref(code)) else None

    def alive(self, h):
        code = self.W.DWORD()
        return bool(self.k32.GetExitCodeProcess(h, self.C.byref(code))) and code.value == 259

    def cpu_seconds(self, h):
        ft = [self.W.FILETIME() for _ in range(4)]
        if not self.k32.GetProcessTimes(h, *[self.C.byref(f) for f in ft]):
            return None
        val = lambda f: (f.dwHighDateTime << 32 | f.dwLowDateTime) / 1e7
        return val(ft[2]) + val(ft[3])

    def mem_bytes(self, h):
        pmc = self.PMC()
        pmc.cb = self.C.sizeof(pmc)
        if self.k32.K32GetProcessMemoryInfo(h, self.C.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize
        vmc = self.VMC()  # fallback that only needs QUERY_LIMITED_INFORMATION
        if self.ntdll.NtQueryInformationProcess(h, 3, self.C.byref(vmc), self.C.sizeof(vmc), None) == 0:
            return vmc.WorkingSetSize
        return None

    # ---- limits -------------------------------------------------------------------
    def create_job(self, pid=None):
        # Named per PID: if this app crashes and restarts, it re-opens the SAME job instead
        # of stacking a second one, so it can still change or remove the old CPU cap.
        name = f"Local\\RBXLimiter_job_{pid}" if pid else None
        return self.k32.CreateJobObjectW(None, name) or None

    def in_job(self, job, h):
        inside = self.W.BOOL()
        return bool(job and self.k32.IsProcessInJob(h, job, self.C.byref(inside)) and inside.value)

    def assign_job(self, job, h):
        return None if self.k32.AssignProcessToJobObject(job, h) else self._err("cpu")

    def set_job_cpu(self, job, pct):
        info = self.CPU_RATE()
        if pct is None:
            info.ControlFlags, info.CpuRate = 0, 0
        else:
            info.ControlFlags = 0x1 | 0x4  # ENABLE | HARD_CAP
            info.CpuRate = max(1, min(10000, int(round(pct * 100))))
        ok = self.k32.SetInformationJobObject(job, 15, self.C.byref(info), self.C.sizeof(info))
        return None if ok else self._err("cpu")

    # ---- read back what is really in effect (used for the LIMIT / PARTIAL label) --
    def check_cpu(self, job, h, pct):
        inside = self.W.BOOL()
        if not job or not self.k32.IsProcessInJob(h, job, self.C.byref(inside)) or not inside.value:
            return False
        info = self.CPU_RATE()
        if not self.k32.QueryInformationJobObject(job, 15, self.C.byref(info), self.C.sizeof(info), None):
            return False
        return bool(info.ControlFlags & 0x1) and abs(info.CpuRate - int(round(pct * 100))) <= 1

    def check_priority(self, h, prio):
        want = {"idle": 0x40, "below": 0x4000, "normal": 0x20}[prio]
        cur = self.k32.GetPriorityClass(h)
        if cur == 0:
            return None                                   # can't read
        return cur == want

    def check_ws_cap(self, h, cap_bytes):
        SZ = self.C.c_size_t
        mn, mx, fl = SZ(), SZ(), self.W.DWORD()
        if not self.k32.GetProcessWorkingSetSizeEx(h, self.C.byref(mn), self.C.byref(mx), self.C.byref(fl)):
            return None
        return bool(fl.value & 0x4) and abs(mx.value - cap_bytes) < (1 << 20)

    def set_priority(self, h, prio):
        cls = {"idle": 0x40, "below": 0x4000, "normal": 0x20}[prio]
        return None if self.k32.SetPriorityClass(h, cls) else self._err("prio")

    def set_ws_cap(self, h, cap_bytes):
        SZ = self.C.c_size_t
        if cap_bytes is None:
            mn, mx, fl = SZ(), SZ(), self.W.DWORD()
            if not self.k32.GetProcessWorkingSetSizeEx(h, self.C.byref(mn), self.C.byref(mx), self.C.byref(fl)):
                return self._err("ram")
            if not fl.value & 0x4:  # hard max not enabled -> nothing to undo
                return None
            ok = self.k32.SetProcessWorkingSetSizeEx(h, mn.value, mx.value, 0x8 | 0x2)
        else:
            mn = min(16 << 20, cap_bytes // 4)
            ok = self.k32.SetProcessWorkingSetSizeEx(h, mn, cap_bytes, 0x4 | 0x2)  # HARD MAX, soft min
        return None if ok else self._err("ram")

    def trim_self(self):
        """Hand this app's own unused memory back to Windows."""
        self.k32.K32EmptyWorkingSet(self.k32.GetCurrentProcess())

    def trim(self, h):
        return None if self.k32.K32EmptyWorkingSet(h) else self._err("trim")

    def terminate(self, pid):
        h = self.k32.OpenProcess(0x1, False, pid)
        if not h:
            return self._err("kill")
        ok = self.k32.TerminateProcess(h, 0)
        err = None if ok else self._err("kill")
        self.k32.CloseHandle(h)
        return err

    def get_title(self, pid):
        hwnd = self.find_window(pid)
        if not hwnd:
            return None
        buf = self.C.create_unicode_buffer(256)
        self.u32.GetWindowTextW(hwnd, buf, 256)
        return buf.value

    def set_title(self, pid, text):
        hwnd = self.find_window(pid)
        if not hwnd:
            return False
        res = self.C.c_size_t()
        # WM_SETTEXT, SMTO_ABORTIFHUNG, 150 ms max
        return bool(self.u32.SendMessageTimeoutW(hwnd, 0x000C, 0, text, 0x2, 150, self.C.byref(res)))

    def window_pid(self, hwnd):
        if not hwnd:
            return None
        p = self.W.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, self.C.byref(p))
        return p.value or None

    def foreground_pid(self):
        return self.window_pid(self.u32.GetForegroundWindow())

    def find_window(self, pid):
        """Cached: one full EnumWindows per instance, not one per lookup."""
        with self._hwnd_lock:
            h = self._hwnds.get(pid)
            if h and self.u32.IsWindow(h) and self.window_pid(h) == pid and self.u32.IsWindowVisible(h):
                return h
            h = self._enum_window(pid)
            if h:
                self._hwnds[pid] = h
            else:
                self._hwnds.pop(pid, None)
            if len(self._hwnds) > 200:
                self._hwnds.clear()
            return h

    def _enum_window(self, pid):
        found = []

        def cb(hwnd, _):
            p = self.W.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, self.C.byref(p))
            if p.value == pid and self.u32.IsWindowVisible(hwnd) and self.u32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
                return False
            return True

        self.u32.EnumWindows(self.WNDENUMPROC(cb), 0)
        return found[0] if found else None

    def force_foreground(self, hwnd):
        """SetForegroundWindow is blocked for background apps; attach to the
        foreground thread's input first so Windows allows the switch."""
        if not hwnd or not self.u32.IsWindow(hwnd):
            return False
        fg = self.u32.GetForegroundWindow()
        if fg == hwnd:
            return True
        me = self.k32.GetCurrentThreadId()
        other = self.u32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(other and other != me and self.u32.AttachThreadInput(me, other, True))
        self.u32.SetForegroundWindow(hwnd)
        if attached:
            self.u32.AttachThreadInput(me, other, False)
        return self.u32.GetForegroundWindow() == hwnd

    def focus(self, pid):
        hwnd = self.find_window(pid)
        if hwnd:
            if self.u32.IsIconic(hwnd):
                self.u32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE, never blocks on a busy window
                time.sleep(0.05)
            self.force_foreground(hwnd)
        return bool(hwnd)

    def _key(self, scan, up):
        inp = self.INPUT()
        inp.type = 1  # INPUT_KEYBOARD
        inp.ki.wScan = scan
        inp.ki.dwFlags = 0x8 | (0x2 if up else 0)  # KEYEVENTF_SCANCODE (| KEYUP)
        return self.u32.SendInput(1, self.C.byref(inp), self.C.sizeof(inp)) == 1

    # ---- small tiled windows ----------------------------------------------------------
    def monitor_area(self, hwnd):
        """Work area (screen minus taskbar) of the monitor `hwnd` is on - any monitor,
        any position, rotated or not."""
        mon = self.u32.MonitorFromWindow(hwnd, 2) if hwnd else None   # MONITOR_DEFAULTTONEAREST
        mi = self.MONITORINFO()
        mi.cbSize = self.C.sizeof(mi)
        if mon and self.u32.GetMonitorInfoW(mon, self.C.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
        return self.work_area()

    def area_exists(self, area):
        """Is that rectangle still on a connected monitor?"""
        r = self.W.RECT(*area)
        return bool(self.u32.MonitorFromRect(self.C.byref(r), 0))   # MONITOR_DEFAULTTONULL

    def work_area(self):
        r = self.W.RECT()
        if self.u32.SystemParametersInfoW(0x30, 0, self.C.byref(r), 0):   # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
        return 0, 0, 1920, 1040

    def is_minimized(self, pid):
        hwnd = self.find_window(pid)
        return bool(hwnd and self.u32.IsIconic(hwnd))

    def place_window(self, pid, x, y, w, h):
        """Un-minimize (without stealing focus) and move/resize. Asynchronous, so a frozen
        or busy Roblox can never hang this app."""
        hwnd = self.find_window(pid)
        if not hwnd:
            return False
        if self.u32.IsIconic(hwnd):
            self.u32.ShowWindowAsync(hwnd, 4)            # SW_SHOWNOACTIVATE
        # SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        return bool(self.u32.SetWindowPos(hwnd, None, x, y, w, h, 0x4 | 0x10 | 0x4000))

    def tap_space(self, pid, keys=((0x39, 0),)):
        """Bring the instance's window forward, press `keys` [(scancode, pause_after_s)],
        put things back.
        Runs on a worker thread. Returns None or an error string."""
        hwnd = self.find_window(pid)
        if not hwnd:
            return "no window"
        was_min = bool(self.u32.IsIconic(hwnd))
        if was_min:
            self.u32.ShowWindowAsync(hwnd, 9)
            time.sleep(0.35)
        if not self.force_foreground(hwnd):
            time.sleep(0.15)
            if not self.force_foreground(hwnd):
                if was_min:
                    self.u32.ShowWindowAsync(hwnd, 6)
                return "couldn't focus window"
        time.sleep(0.25)          # let Roblox register focus
        ok = True
        for scan, pause in keys:
            ok = self._key(scan, False) and ok
            time.sleep(0.06)
            ok = self._key(scan, True) and ok
            time.sleep(pause)
        time.sleep(0.15)
        if was_min:
            self.u32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE
        return None if ok else "key blocked (run as admin?)"

    def get_foreground(self):
        return self.u32.GetForegroundWindow()

    # ---- multi instance -----------------------------------------------------------
    def multi_on(self):
        if self._mutexes:
            return None
        for name in ("ROBLOX_singletonMutex", "ROBLOX_singletonEvent"):
            h = self.k32.CreateMutexW(None, True, name)
            if h:
                self._mutexes.append(h)
        return None if self._mutexes else "multi: close Roblox first"

    def single_instance(self):
        """Two copies of the limiter would fight over the same processes."""
        self._single = self.k32.CreateMutexW(None, False, "Local\\RBXLimiterApp")
        return self.C.get_last_error() != 183   # ERROR_ALREADY_EXISTS

    def message_box(self, text):
        self.u32.MessageBoxW(None, text, APP_NAME, 0x30)

    def multi_off(self):
        for h in self._mutexes:
            self.k32.CloseHandle(h)
        self._mutexes = []

    # ---- window chrome --------------------------------------------------------------
    def window_fixups(self, root):
        """Borderless windows get no taskbar button by default - give them one."""
        hwnd = self.u32.GetParent(root.winfo_id())
        style = self.u32.GetWindowLongW(hwnd, -20)
        self.u32.SetWindowLongW(hwnd, -20, (style & ~0x80) | 0x40000)
        root.withdraw()
        root.after(10, root.deiconify)
        return hwnd

    def minimize(self, hwnd):
        self.u32.ShowWindow(hwnd, 6)


class DemoSystem:
    """Fake backend so the UI runs (and can be previewed) off Windows."""
    demo = True

    def __init__(self):
        import random
        self.r = random.Random(7)
        self.cpu_count = 8
        self.procs = {14520: [0.0, 1.31e9], 18844: [0.0, 0.96e9], 21012: [0.0, 1.62e9], 9132: [0.0, 0.74e9]}
        self.caps = {}

    def is_admin(self): return True
    def relaunch_admin(self): return False
    def enable_privileges(self): pass
    def list_pids(self, names): return set(self.procs)
    def open_proc(self, pid): return pid, 0
    def close(self, h): pass
    def exe_path(self, h): return None
    def open_suspend(self, pid): return pid
    def suspend(self, h): return True
    def resume(self, h): return True
    def alive(self, h): return h in self.procs
    def memory(self): return (0.62, 21.4 * 2**30, 31.9 * 2**30, 16 * 2**30)
    def age(self, h): return 9999
    def exit_code(self, h): return self.__dict__.get("codes", {}).get(h, 0)

    def cpu_seconds(self, h):
        p = self.procs.get(h)
        if not p:
            return None
        cap = self.caps.get(h)
        now = time.time()
        last = p[2] if len(p) > 2 else now
        rate = self.r.uniform(0.6, 1.4) if cap is None else cap / 100 * self.cpu_count * self.r.uniform(0.85, 1)
        p[0] += rate * (now - last)
        p[2:] = [now]
        return p[0]

    def mem_bytes(self, h): return self.procs[h][1] if h in self.procs else None
    def create_job(self, pid=None): return {"pid": None}
    def in_job(self, job, h): return job.get("pid") == h
    def window_pid(self, hwnd): return None
    def foreground_pid(self): return None
    def single_instance(self): return True
    def message_box(self, text): print(text)
    def assign_job(self, job, h): job["pid"] = h; return None
    def set_job_cpu(self, job, pct): self.caps[job.get("pid")] = pct; return None
    def set_priority(self, h, prio): return None
    def check_cpu(self, job, h, pct): return bool(job and job.get("pid") == h and self.caps.get(h) == pct)
    def check_priority(self, h, prio): return True
    def check_ws_cap(self, h, cap): return True
    def set_ws_cap(self, h, cap): return None
    def trim(self, h): return None
    def trim_self(self): pass
    def terminate(self, pid): self.procs.pop(pid, None); return None
    def focus(self, pid): return True
    def work_area(self): return 0, 0, 1920, 1040
    def monitor_area(self, hwnd): return self.__dict__.get("mon", (0, 0, 1920, 1040))
    def area_exists(self, area): return True
    def is_minimized(self, pid): return pid in self.__dict__.get("mins", set())
    def place_window(self, pid, x, y, w, h):
        self.__dict__.setdefault("placed", {})[pid] = (x, y, w, h)
        self.__dict__.get("mins", set()).discard(pid)
        return True
    def get_foreground(self): return None
    def get_title(self, pid): return self.__dict__.setdefault("titles", {}).get(pid, "Roblox")
    def set_title(self, pid, text): self.__dict__.setdefault("titles", {})[pid] = text; return True
    def force_foreground(self, hwnd): return True
    def tap_space(self, pid, keys=()): time.sleep(0.2); return None
    def multi_on(self): return None
    def multi_off(self): pass
    def window_fixups(self, root): return None
    def minimize(self, hwnd): pass


# =====================================================================================
#  Engine
# =====================================================================================
class Throttler(threading.Thread):
    """Per-process FPS limiter: keeps a process suspended and lets it run for a short
    slice `fps` times per second (each slice renders ~1 frame). Instances are staggered
    across the period so they don't all wake at once. Everything is resumed on stop."""
    SLICE = 0.04

    def __init__(self, sysapi):
        super().__init__(daemon=True)
        self.s = sysapi
        self.lock = threading.RLock()
        self.handles = {}      # pid -> handle
        self.suspended = set()
        self.targets, self.fps = [], 0
        self.held = set()
        self.errors = {}
        self.stop_ev = threading.Event()

    # called from other threads ---------------------------------------------------
    def configure(self, pids, fps):
        self.targets, self.fps = list(pids), int(fps)

    def hold(self, pid):
        with self.lock:
            self.held.add(pid)
            self._resume(pid)

    def release(self, pid):
        with self.lock:
            self.held.discard(pid)

    def wake(self, pids):
        """Resume processes we may have left frozen (e.g. after a crash of this app)."""
        for pid in pids:
            h = self.s.open_suspend(pid)
            if h:
                self.s.resume(h)
                self.s.close(h)

    def stop(self):
        self.stop_ev.set()
        if self.is_alive():
            self.join(timeout=2)
        self.release_all()

    # internals -------------------------------------------------------------------
    def _proc(self, pid):
        h = self.handles.get(pid)
        if h is None and pid not in self.errors:
            h = self.s.open_suspend(pid)
            if h:
                self.handles[pid] = h
            else:
                self.errors[pid] = "fps: access denied"
        return h

    def _suspend(self, pid):
        with self.lock:
            if pid in self.held or pid in self.suspended:
                return
            h = self._proc(pid)
            if h and self.s.suspend(h):
                self.suspended.add(pid)
            elif h:
                self.errors[pid] = "fps: suspend blocked"

    def _resume(self, pid):
        with self.lock:
            if pid in self.suspended:
                h = self.handles.get(pid)
                if h:
                    self.s.resume(h)
                self.suspended.discard(pid)

    def _drop_missing(self, keep):
        with self.lock:
            for pid in list(self.handles):
                if pid not in keep:
                    self._resume(pid)
                    self.s.close(self.handles.pop(pid))
            for pid in list(self.errors):
                if pid not in keep:
                    del self.errors[pid]

    def release_all(self):
        self._drop_missing(())

    def run(self):
        try:
            while not self.stop_ev.is_set():
                try:
                    self._cycle()
                except Exception:
                    self.stop_ev.wait(0.5)
        finally:
            self.release_all()

    def _cycle(self):
        targets, fps = self.targets, self.fps
        self._drop_missing(set(targets) if fps else ())
        try:
            fg = self.s.foreground_pid()
        except Exception:
            fg = None
        if fg in self.suspended:
            self._resume(fg)                      # you clicked into it -> let it run
        targets = [p for p in targets if p not in self.errors and p != fg]
        if not fps or not targets:
            self.stop_ev.wait(0.1)
            return
        period = 1.0 / fps
        slice_ = min(self.SLICE, period * 0.5)
        start = time.perf_counter()
        events = []
        for k, pid in enumerate(targets):
            off = period * k / len(targets)
            events.append((off, 0, pid))              # 0 = resume
            events.append((off + slice_, 1, pid))     # 1 = suspend
        events.sort()
        for at, kind, pid in events:
            wait = start + at - time.perf_counter()
            if wait > 0 and self.stop_ev.wait(wait):
                break
            if wait < -0.5 and kind == 0:
                break                            # fell far behind (PC busy) - start a fresh cycle
            (self._resume if kind == 0 else self._suspend)(pid)
        rest = start + period - time.perf_counter()
        if rest > 0:
            self.stop_ev.wait(rest)


class Instance:
    def __init__(self, pid, idx):
        self.pid, self.idx = pid, idx
        self.h, self.rights = None, 0
        self.job = None
        self.job_assigned = False
        self.exempt = False
        self.limited = False
        self.errors = []
        self.cpu = 0.0
        self.mem = None
        self._last = None
        self.first_seen = time.time()
        self.age = None
        self.grace_done = False
        self.missing = []          # limits that should be on but Windows says aren't
        self.active = []
        self._last_try = 0.0


class Engine:
    def __init__(self, sysapi, cfg):
        self.s, self.cfg = sysapi, cfg
        self.inst = {}
        self._next_idx = 1
        self._last_trim = time.time()
        self._last_enforce = time.time()
        self.message = ""
        self.afk_next = time.time() + cfg["afk_min"] * 60
        self.afk_busy = False
        self._last_titles = 0
        self._afk_started = 0.0
        self.killed = set()
        self.crashes = []
        self.mem = None
        self._mem_warned = False
        self._last_gfx = 0.0
        self.throttle = Throttler(sysapi)
        self.throttle.wake(sysapi.list_pids(ROBLOX_EXES))   # never leave a frozen Roblox behind
        self.throttle.start()
        atexit.register(self.throttle.release_all)
        # Earlier versions wrote a global FPS flag into Roblox's files - remove it so FREE
        # instances aren't capped.
        try:
            write_fps_flag(roblox_setting_files(), 0)
        except Exception:
            pass

    # ---- low-memory graphics (all instances, takes effect when an instance starts) ----
    def low_gfx_apply(self, quiet=False):
        exes = [self.s.exe_path(i.h) for i in self.inst.values() if i.h]
        files = roblox_setting_files(exes)
        if self.cfg["low_gfx"]:
            changed, failed = write_flags(files, LOWMEM_FLAGS)
            set_roblox_quality_low()
        else:
            changed, failed = write_flags(files, remove=LOWMEM_FLAGS)
        self._last_gfx = time.time()
        if quiet:
            return
        if not files:
            self.message = "Roblox install not found"
        elif failed:
            self.message = f"{failed} Roblox file(s) blocked - run as admin"
        else:
            self.message = ("low-mem graphics ON - restart instances" if self.cfg["low_gfx"]
                            else "low-mem graphics OFF - restart instances")

    # ---- FPS cap (per LIMIT instance) ------------------------------------------------
    def fps_update(self):
        fps = self.cfg["fps_cap"] if self.cfg["master"] else 0
        pids = [i.pid for i in self.inst.values() if i.h and not i.exempt and not self.loading(i)]
        self.throttle.configure(pids, fps)

    # ---- anti-AFK --------------------------------------------------------------------
    def afk_reset(self):
        self.afk_next = time.time() + self.cfg["afk_min"] * 60

    def afk_left(self):
        return max(0, self.afk_next - time.time())

    def tile_area(self):
        a = self.cfg.get("tile_area")
        if isinstance(a, list) and len(a) == 4 and a[2] > a[0] and a[3] > a[1] and self.s.area_exists(a):
            return tuple(a)
        return self.s.work_area()

    def tile(self, only_minimized=False, area=None):
        """Put every non-FREE instance in a small window on a grid filling the screen.
        Small windows render tiny frames (less memory) and never need restoring."""
        insts = [i for i in self.ordered() if not i.exempt]
        if not insts:
            return 0
        L, T, Rt, B = area or self.tile_area()
        W, H = Rt - L, B - T
        n = len(insts)
        best = None
        for cols in range(1, n + 1):
            rows = -(-n // cols)
            tw, th = W // cols, H // rows
            score = min(tw / 4, th / 3)                  # keep roughly 4:3
            if best is None or score > best[0]:
                best = (score, cols, tw, th)
        _, cols, tw, th = best
        w, h = min(tw, TILE_MAX[0]), min(th, TILE_MAX[1])
        moved = 0
        for k, i in enumerate(insts):
            if only_minimized and not self.s.is_minimized(i.pid):
                continue
            x, y = L + (k % cols) * tw, T + (k // cols) * th
            if self.s.place_window(i.pid, x, y, w, h):
                moved += 1
        return moved

    def afk_run(self):
        """Press Space in every instance (on a worker thread so the UI never freezes)."""
        if self.afk_busy and time.time() - self._afk_started < 180:
            return
        targets = [i.pid for i in self.ordered() if not (self.cfg["afk_skip_free"] and i.exempt)]
        labels = {i.pid: self.label(i) for i in self.inst.values()}
        self.afk_reset()
        if not targets:
            return
        self.afk_busy = True
        self._afk_started = time.time()
        name, keys = AFK_KEYS[self.cfg["afk_key"]]
        self.message = f"anti-AFK: pressing {name} in {len(targets)}..."

        def work():
            done, last_err = 0, None
            # Restoring a minimized Roblox (and minimizing it again) makes it rebuild its
            # graphics every time, and that memory isn't given back. So minimized windows are
            # put into small tiles once and left there.
            try:
                if self.tile(only_minimized=True):
                    time.sleep(1.0)
            except Exception:
                log_error("anti-AFK tile")
            prev = self.s.get_foreground()
            prev_pid = self.s.window_pid(prev)
            if prev_pid in labels:
                self.throttle.hold(prev_pid)     # don't focus back onto a frozen window
            try:
                for pid in targets:
                    self.throttle.hold(pid)
                    try:
                        e = self.s.tap_space(pid, keys)
                    finally:
                        self.throttle.release(pid)
                    if e:
                        last_err = f"{labels.get(pid, pid)}: {e}"
                    else:
                        done += 1
            except Exception:
                log_error("anti-AFK")
            finally:
                try:
                    if prev:
                        self.s.force_foreground(prev)
                finally:
                    if prev_pid in labels:
                        self.throttle.release(prev_pid)
                stamp = time.strftime("%H:%M")
                self.message = f"anti-AFK {stamp}: {done}/{len(targets)} ok" + (f" - {last_err}" if last_err else "")
                self.afk_busy = False

        threading.Thread(target=work, daemon=True).start()

    def ordered(self):
        return sorted(self.inst.values(), key=lambda i: i.idx)

    # ---- names (saved per slot #, because Windows gives every launch a new PID) -----
    def name(self, i):
        return self.cfg["names"].get(str(i.idx), "")

    def label(self, i):
        return self.name(i) or str(i.pid)

    def set_name(self, i, text):
        text = " ".join(text.split())[:24]
        if text:
            self.cfg["names"][str(i.idx)] = text
        else:
            self.cfg["names"].pop(str(i.idx), None)
        self._last_titles = 0

    def sync_titles(self, reset=False):
        for i in self.inst.values():
            n = "" if reset else self.name(i)
            want = f"Roblox - {n}" if n else "Roblox"
            cur = self.s.get_title(i.pid)
            if cur is None or cur == want or not (cur == "Roblox" or cur.startswith("Roblox - ")):
                continue           # no window yet, already right, or the game set its own title
            self.throttle.hold(i.pid)
            try:
                self.s.set_title(i.pid, want)
            finally:
                self.throttle.release(i.pid)

    def tick(self):
        now = time.time()
        pids = self.s.list_pids(ROBLOX_EXES)
        for pid in list(self.inst):
            i = self.inst[pid]
            if pid not in pids or (i.h and not self.s.alive(i.h)):
                self.report_exit(i)
                self.s.close(i.h)
                if i.job:
                    self.s.close(i.job)
                del self.inst[pid]
        for pid in sorted(pids - set(self.inst)):
            used = {x.idx for x in self.inst.values()}
            slot = next(n for n in range(1, len(used) + 2) if n not in used)   # lowest free slot
            i = Instance(pid, slot)
            i.h, i.rights = self.s.open_proc(pid)
            i.age = self.s.age(i.h) if i.h else None
            self.inst[pid] = i
            if not self.cfg["auto"]:
                i.exempt = True          # auto-limit off -> new instances start FREE
            elif self.cfg["master"]:
                self.apply(i)
        for i in self.inst.values():
            if not i.h:
                continue
            t = self.s.cpu_seconds(i.h)
            if t is not None:
                if i._last:
                    dt = now - i._last[1]
                    if dt > 0:
                        i.cpu = max(0.0, (t - i._last[0]) / (dt * self.s.cpu_count) * 100)
                i._last = (t, now)
            i.mem = self.s.mem_bytes(i.h)
        self.check_memory()
        if self.cfg["low_gfx"] and now - self._last_gfx >= 60:
            self.low_gfx_apply(quiet=True)          # Roblox updates create new folders
        self.fps_update()
        if now - self._last_titles >= 3:
            self._last_titles = now
            self.sync_titles()
        if self.cfg["afk_on"] and now >= self.afk_next:
            self.afk_run()
        if self.cfg["master"] and self.cfg["ram_mode"] == "trim" and now - self._last_trim >= TRIM_EVERY_S:
            self._last_trim = now
            self.trim_all(quiet=True)
        full = now - self._last_enforce >= REENFORCE_EVERY_S
        if full:
            self._last_enforce = now
        for i in self.inst.values():
            self.verify(i)
            if not self.cfg["master"] or i.exempt or not i.h:
                continue
            # retry quickly while something is missing (Roblox resets some settings while
            # starting up), and re-push everything every 10 s anyway
            ended = not i.grace_done and not self.loading(i)
            if ended:
                i.grace_done = True              # finished loading -> apply RAM cap / FPS now
            if (i.missing and now - i._last_try >= 2) or full or ended:
                self.apply(i, quiet=True)
                self.verify(i)

    def loading(self, i):
        """New instances get GRACE_S seconds to load before the RAM cap / FPS freeze kick in
        (squeezing Roblox while it loads is the most likely moment to break it)."""
        if i.age is None:
            return False
        return i.age + (time.time() - i.first_seen) < GRACE_S

    def expected(self, i=None):
        """Which limits should be active on a LIMIT instance with the current settings."""
        want = ["cpu", "prio"]
        heavy = i is None or not self.loading(i)
        if self.cfg["ram_mode"] == "hard" and heavy:
            want.append("ram")
        if self.cfg["fps_cap"] and heavy:
            want.append("fps")
        return want

    # ---- crash reasons + memory -----------------------------------------------------
    def report_exit(self, i):
        if i.pid in self.killed:
            self.killed.discard(i.pid)
            return
        code = self.s.exit_code(i.h) if i.h else None
        if code in (None, 0, 259, 1):           # normal close / leave game / unknown
            return
        why = EXIT_REASONS.get(code, f"code 0x{code:08X}")
        mem = self.s.memory()
        note = ""
        if mem and mem[1] / mem[2] > 0.9:
            note = " - memory was almost full"
        self.message = f"\u26a0 {self.label(i)} crashed ({why}){note} {time.strftime('%H:%M')}"
        self.crashes.append((time.time(), self.label(i), why))
        log_error(f"Roblox instance '{self.label(i)}' (pid {i.pid}) crashed: {why}{note}; "
                  f"{len(self.inst)} instances open, memory "
                  + (f"{mem[1] / 2**30:.1f}/{mem[2] / 2**30:.1f} GB committed" if mem else "?"), ())

    def check_memory(self):
        mem = self.s.memory()
        if not mem:
            return
        self.mem = mem
        level = mem[1] / mem[2]
        if level > 0.9 and not self._mem_warned:
            self._mem_warned = True
            self.message = "\u26a0 memory almost full - more instances will crash"
        elif level < 0.85:
            self._mem_warned = False

    def verify(self, i):
        if not self.cfg["master"] or i.exempt or not i.h:
            i.active, i.missing = [], []
            return
        ok = []
        for part in self.expected(i):
            if part not in self.expected(i):
                continue
            if part == "cpu":
                good = i.job_assigned and self.s.check_cpu(i.job, i.h, self.cfg["cpu_cap"])
            elif part == "prio":
                good = self.s.check_priority(i.h, self.cfg["priority"])
                if good is None:
                    good = i.limited and not any(e.startswith("prio") for e in i.errors)
            elif part == "ram":
                good = self.s.check_ws_cap(i.h, self.cfg["ram_cap"] << 20)
                if good is None:
                    good = i.limited and not any(e.startswith("ram") for e in i.errors)
            else:  # fps
                good = i.pid in self.throttle.targets and i.pid not in self.throttle.errors
            if good:
                ok.append(part)
        i.active = ok
        i.missing = [p for p in self.expected(i) if p not in ok]

    def apply(self, i, quiet=False):
        i._last_try = time.time()
        if not i.h:
            i.errors = ["open: access denied"]
            i.limited = False
            return
        if i.exempt or not self.cfg["master"]:
            return self.unlimit(i)
        errs, ok = [], 0
        # CPU via job object
        if i.job is None:
            i.job = self.s.create_job(i.pid)
            if i.job and self.s.in_job(i.job, i.h):
                i.job_assigned = True            # left over from before a restart
        if i.job and not i.job_assigned:
            e = self.s.assign_job(i.job, i.h)
            if e:
                errs.append(e)
            else:
                i.job_assigned = True
        if i.job_assigned:
            e = self.s.set_job_cpu(i.job, self.cfg["cpu_cap"])
            if e:
                errs.append(e)
            else:
                ok += 1
        # priority
        e = self.s.set_priority(i.h, self.cfg["priority"])
        if e:
            errs.append(e)
        else:
            ok += 1
        # RAM
        mode = self.cfg["ram_mode"]
        e = self.s.set_ws_cap(i.h, self.cfg["ram_cap"] << 20 if mode == "hard" and "ram" in self.expected(i) else None)
        if e:
            errs.append(e)
        if mode == "trim" and not quiet:
            self.s.trim(i.h)
        i.errors = errs
        i.limited = ok > 0
        self.fps_update()

    def unlimit(self, i):
        if i.job_assigned:
            self.s.set_job_cpu(i.job, None)
        if i.h:
            self.s.set_priority(i.h, "normal")
            self.s.set_ws_cap(i.h, None)
        i.limited = False
        i.errors = []
        self.fps_update()

    def apply_all(self):
        for i in self.inst.values():
            self.apply(i)
        bad = [i for i in self.inst.values() if i.errors]
        if bad:
            self.message = f"{len(bad)} instance(s) partly blocked - " + bad[0].errors[0]
        elif self.inst:
            self.message = "limits applied" if self.cfg["master"] else "limits removed"

    def restore_all(self):
        for i in self.inst.values():
            self.unlimit(i)

    def trim_all(self, quiet=False):
        freed = 0
        for i in self.inst.values():
            if i.h and not i.exempt:
                before = i.mem or 0
                self.s.trim(i.h)
                after = self.s.mem_bytes(i.h) or before
                i.mem = after
                freed += max(0, before - after)
        if not quiet:
            self.message = f"trimmed {freed / 2**20:,.0f} MB"

    def kill(self, pid):
        i = self.inst.get(pid)
        self.killed.add(pid)
        e = self.s.terminate(pid)
        self.message = e or f"killed {self.label(i) if i else pid}"

    def kill_all(self):
        for pid in list(self.inst):
            self.killed.add(pid)
            self.s.terminate(pid)
        self.message = "killed all instances"


# Anti-AFK key choices: (label, [(scancode, pause after)]).
# ESC x2 opens then closes the Roblox menu: counts as input, character doesn't move.
AFK_KEYS = {
    "esc2": ("ESC x2", ((0x01, 0.45), (0x01, 0.1))),
    "space": ("SPACE", ((0x39, 0.1),)),
    "both": ("ESC+SPACE", ((0x01, 0.45), (0x01, 0.35), (0x39, 0.1))),
}
AFK_ORDER = ["esc2", "space", "both"]


# =====================================================================================
#  FPS cap (Roblox FastFlag file - applies to every instance started after it is written)
# =====================================================================================
FPS_FLAG = "DFIntTaskSchedulerTargetFps"
FPS_STEPS = [0, 1, 2, 3, 5, 10, 15, 20, 30]   # 0 = off
LAUNCHERS = ("Roblox", "Bloxstrap", "Fishstrap", "Voidstrap")


def roblox_setting_files(running_exes=()):
    """Every ClientAppSettings.json that Roblox (or a Bloxstrap-style launcher) will read."""
    dirs = set()
    roots = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots += [os.path.join(la, name) for name in LAUNCHERS]
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if pf:
            roots.append(os.path.join(pf, "Roblox"))
    for root in roots:
        vers = os.path.join(root, "Versions")
        if os.path.isdir(vers):
            for v in os.listdir(vers):
                d = os.path.join(vers, v)
                if os.path.isfile(os.path.join(d, "RobloxPlayerBeta.exe")):
                    dirs.add(d)
        mods = os.path.join(root, "Modifications")      # launchers copy this over on every launch
        if root.split(os.sep)[-1] != "Roblox" and os.path.isdir(mods):
            dirs.add(mods)
    for exe in running_exes:
        if exe:
            dirs.add(os.path.dirname(exe))
    return sorted(os.path.join(d, "ClientSettings", "ClientAppSettings.json") for d in dirs)


# Roblox settings that make each instance use less memory (lower textures, no anti-
# aliasing, lowest graphics quality). Roblox only accepts flags on its allow-list;
# anything it doesn't accept is simply ignored, it never breaks the game.
LOWMEM_FLAGS = {
    "DFFlagTextureQualityOverrideEnabled": True,
    "DFIntTextureQualityOverride": 0,          # lowest texture resolution (biggest saving)
    "FIntDebugForceMSAASamples": 0,            # no anti-aliasing buffers
    "DFIntDebugFRMQualityLevelOverride": 1,    # graphics quality 1
    "FIntRenderGrassDetailStrands": 0,
    "FFlagDisablePostFx": True,
}


def write_flags(files, set_flags=None, remove=()):
    """Merge `set_flags` into / drop `remove` keys from each ClientAppSettings.json,
    keeping every other flag. Returns (files_changed, files_failed)."""
    set_flags = set_flags or {}
    changed = failed = 0
    for path in files:
        try:
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                data = json.loads(txt) if txt else {}
                if not isinstance(data, dict):
                    data = {}
            elif not set_flags:
                continue
            want = dict(data)
            for k in remove:
                want.pop(k, None)
            want.update(set_flags)
            if want == data and os.path.isfile(path):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(want, f, indent=2)
            changed += 1
        except Exception:
            failed += 1
    return changed, failed


def set_roblox_quality_low():
    """Set the in-game Graphics Quality slider to 1 in Roblox's saved settings
    (used by every account on this PC). Returns True if a settings file was changed."""
    import glob
    import re
    la = os.environ.get("LOCALAPPDATA")
    if not la:
        return False
    done = False
    for path in glob.glob(os.path.join(la, "Roblox", "GlobalBasicSettings_*.xml")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                xml = f.read()
            new = re.sub(r'(<int name="GraphicsQualityLevel">)\d+(</int>)', r"\g<1>1\g<2>", xml)
            new = re.sub(r'(<token name="SavedQualityLevel">)\d+(</token>)', r"\g<1>1\g<2>", new)
            if new != xml:
                if not os.path.exists(path + ".rbxlimiter.bak"):
                    with open(path + ".rbxlimiter.bak", "w", encoding="utf-8") as f:
                        f.write(xml)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
                done = True
        except Exception:
            pass
    return done


def write_fps_flag(files, fps):
    """Set (fps>0) or remove (fps==0) the FPS flag, keeping any other flags in the file.
    Returns (files_changed, files_failed)."""
    changed = failed = 0
    for path in files:
        try:
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                data = json.loads(txt) if txt else {}
                if not isinstance(data, dict):
                    data = {}
            elif not fps:
                continue
            want = dict(data)
            if fps:
                want[FPS_FLAG] = int(fps)
            else:
                want.pop(FPS_FLAG, None)
            if want == data and os.path.isfile(path):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(want, f, indent=2)
            changed += 1
        except Exception:
            failed += 1
    return changed, failed


# =====================================================================================
#  Settings
# =====================================================================================
DEFAULTS = {"master": True, "cpu_cap": 15, "ram_cap": 1024, "ram_mode": "hard", "priority": "below",
            "auto": True, "multi": False, "topmost": True, "pos": None,
            "afk_on": False, "afk_min": 15, "afk_skip_free": True, "afk_key": "esc2", "fps_cap": 0, "scale_step": None, "extra": 0,
            "names": {}, "tile_area": None, "low_gfx": False}


def cfg_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RBXLimiter")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(cfg_path(), "r", encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    if not isinstance(cfg.get("names"), dict):
        cfg["names"] = {}
    cfg["names"] = {str(k): str(v) for k, v in cfg["names"].items()}
    if cfg.get("fps_cap") not in FPS_STEPS:
        cfg["fps_cap"] = 0
    if cfg.get("afk_key") not in AFK_KEYS:
        cfg["afk_key"] = "esc2"
    return cfg


def save_cfg(cfg):
    try:
        with open(cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except Exception:
        pass


# =====================================================================================
#  UI
# =====================================================================================
DARK = "#423529"
WOOD = "#e8ad72"
PAPER = "#d7cbbf"
STRIPE = "#cbbdaf"
INK = "#3a2e24"
SOFT = "#85735f"
REDC = "#8e1b12"
GREEN = "#3d6b2c"
TAG_INK = "#5a4632"
FONT = "Consolas"

BG_W, BG_H = ART.BG_SIZE
LIST_TOP, ROW_H = 250, 32

RAM_MODES = [("hard", "HARD CAP"), ("trim", "TRIM 30s"), ("off", "OFF")]
PRIOS = [("idle", "IDLE"), ("below", "BELOW NORM"), ("normal", "NORMAL")]
SLIDERS = {"cpu_cap": (1, 100, 1), "ram_cap": (256, 4096, 64), "afk_min": (1, 19, 1)}


# Window scale steps. Pixel art only scales cleanly by whole-number ratios,
# so each step is zoom(num) then subsample(den).
SCALES = [(1, 2), (3, 5), (2, 3), (3, 4), (4, 5), (1, 1), (5, 4), (4, 3), (3, 2), (2, 1)]
STRETCH_Y = 401          # parchment row where the scroll is stretched / shrunk
STRIP = (397, 406)       # rows that tile seamlessly
BASE_ROWS = 7
MIN_ROWS, MAX_ROWS = 3, 30


def photo_copy(dst, src, frm, to):
    """Tk 'photo copy'. If the target box is bigger than the source box, Tk tiles it."""
    dst.tk.call(dst, "copy", src, "-from", *frm, "-to", *to)


def scale_photo(img, step):
    num, den = SCALES[step]
    if num != 1:
        img = img.zoom(num)
    if den != 1:
        img = img.subsample(den)
    return img


def build_background(extra):
    """Frame art with the parchment made `extra` design pixels taller (or shorter)."""
    src = tk.PhotoImage(data=ART.BG_PNG)
    if extra == 0:
        return src
    h = BG_H + extra
    out = tk.PhotoImage(width=BG_W, height=h)
    if extra > 0:
        photo_copy(out, src, (0, 0, BG_W, STRETCH_Y), (0, 0))
        photo_copy(out, src, (0, STRIP[0], BG_W, STRIP[1]), (0, STRETCH_Y, BG_W, STRETCH_Y + extra))
        photo_copy(out, src, (0, STRETCH_Y, BG_W, BG_H), (0, STRETCH_Y + extra))
    else:
        photo_copy(out, src, (0, 0, BG_W, STRETCH_Y + extra), (0, 0))
        photo_copy(out, src, (0, STRETCH_Y, BG_W, BG_H), (0, STRETCH_Y + extra))
    return out


class App:
    def __init__(self, sysapi):
        self.s = sysapi
        self.cfg = load_cfg()
        self.eng = Engine(sysapi, self.cfg)
        self.scroll = 0
        self.hits = []
        self.drag = None
        self.slider = None
        self.hwnd = None
        self.arm, self.arm_until = None, 0

        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor: real pixels everywhere
            except Exception:
                try:
                    import ctypes
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
            sysapi.enable_privileges()

        self.root = root = tk.Tk()
        root.title(APP_NAME)
        avail = root.winfo_screenheight() - 48
        step = self.cfg.get("scale_step")
        if not isinstance(step, int) or not 0 <= step < len(SCALES):
            fits = [i for i, (n, d) in enumerate(SCALES) if BG_H * n / d <= avail and n <= d]
            step = fits[-1] if fits else 0
        extra = self.cfg.get("extra")
        extra = extra if isinstance(extra, int) else 0
        self.step, self.E = step, max((MIN_ROWS - BASE_ROWS) * ROW_H, min((MAX_ROWS - BASE_ROWS) * ROW_H, extra))
        self.S = SCALES[step][0] / SCALES[step][1]
        self.off = 0
        self.resizing = False
        self.grip_click = 0
        W, H = self.win_size()

        root.overrideredirect(True)
        root.configure(bg=ART.KEY_COLOR)
        try:
            root.wm_attributes("-transparentcolor", ART.KEY_COLOR)
        except tk.TclError:
            pass
        root.wm_attributes("-topmost", bool(self.cfg["topmost"]))
        pos = self.cfg.get("pos")
        if pos and 0 <= pos[0] < root.winfo_screenwidth() - 40 and 0 <= pos[1] < root.winfo_screenheight() - 40:
            x, y = pos
        else:
            x, y = (root.winfo_screenwidth() - W) // 2, max(0, (root.winfo_screenheight() - H) // 2 - 10)
        root.geometry(f"{W}x{H}+{x}+{y}")

        self.cv = cv = tk.Canvas(root, width=W, height=H, bg=ART.KEY_COLOR, highlightthickness=0, bd=0)
        cv.pack()
        try:
            # app icon (title bar / taskbar / Alt+Tab); falls back to the mushroom
            big = getattr(ART, "ICON_PNG", ART.RED_PNG)
            small = getattr(ART, "ICON_PNG_32", big)
            self._icons = [tk.PhotoImage(data=big), tk.PhotoImage(data=small)]
            root.iconphoto(True, *self._icons)
        except Exception:
            pass
        self.build_chrome()

        cv.bind("<ButtonPress-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_motion)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Motion>", self.on_hover)
        cv.bind("<MouseWheel>", self.on_wheel)
        cv.bind("<Control-MouseWheel>", lambda e: self.zoom(1 if e.delta > 0 else -1))
        root.bind("<Control-plus>", lambda e: self.zoom(1))
        root.bind("<Control-equal>", lambda e: self.zoom(1))
        root.bind("<Control-minus>", lambda e: self.zoom(-1))
        root.bind("<Control-Key-0>", lambda e: self.reset_size())
        cv.bind("<Button-4>", lambda e: self.wheel(-1))
        cv.bind("<Button-5>", lambda e: self.wheel(1))
        root.bind("<Escape>", lambda e: None if getattr(self, "editing", None) else self.minimize())
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.report_callback_exception = self._tk_error
        root.bind("<Map>", self._on_map)

        if self.cfg["multi"]:
            self.eng.message = self.s.multi_on() or "multi-instance on"
        if sysapi.demo:
            self.eng.message = "DEMO MODE (not on Windows)"
        elif not sysapi.is_admin():
            self.eng.message = "tip: run as admin for RAM caps"

        self.eng.tick()
        self.redraw()
        root.after(60, self._fix_window)
        root.after(TICK_MS, self.loop)

    # ---- geometry helpers (design coords -> screen) --------------------------------
    def sx(self, v):
        return int(round(v * self.S))

    def font(self, px, bold=True):
        return (FONT, -max(7, int(round(px * self.S))), "bold" if bold else "normal")

    @property
    def rows(self):
        return BASE_ROWS + self.E // ROW_H

    def win_size(self):
        return int(BG_W * self.S), int((BG_H + self.E) * self.S)

    def build_chrome(self):
        """(Re)create the background art for the current scale / height."""
        self.cv.delete("all")
        self.img_bg = scale_photo(build_background(self.E), self.step)
        self.img_red = scale_photo(tk.PhotoImage(data=ART.RED_PNG), self.step)
        self.img_grey = scale_photo(tk.PhotoImage(data=ART.GREY_PNG), self.step)
        self.cv.create_image(0, 0, image=self.img_bg, anchor="nw")
        self.cv.create_image(self.sx(90), self.sx(167), image=self.img_grey, anchor="center")
        self.cv.create_image(self.sx(322), self.sx(150), image=self.img_red, anchor="center")

    def apply_size(self, step, extra, save=True):
        lo, hi = (MIN_ROWS - BASE_ROWS) * ROW_H, (MAX_ROWS - BASE_ROWS) * ROW_H
        step = max(0, min(len(SCALES) - 1, step))
        extra = max(lo, min(hi, extra))
        if (step, extra) == (self.step, self.E):
            return
        self.step, self.E = step, extra
        self.S = SCALES[step][0] / SCALES[step][1]
        W, H = self.win_size()
        self.root.geometry(f"{W}x{H}")
        self.cv.config(width=W, height=H)
        self.build_chrome()
        self.redraw()
        if save:
            self.cfg["scale_step"], self.cfg["extra"] = step, extra
            self.save()

    def zoom(self, d):
        self.apply_size(self.step + d, self.E)

    def reset_size(self):
        self.apply_size(SCALES.index((1, 1)), 0)

    def T(self, x, y, text, px=12, fill=INK, anchor="center", bold=True):
        y += self.off
        self.cv.create_text(self.sx(x), self.sx(y), text=text, font=self.font(px, bold),
                            fill=fill, anchor=anchor, tags="dyn")

    def R(self, x0, y0, x1, y1, fill="", outline="", width=0):
        y0, y1 = y0 + self.off, y1 + self.off
        self.cv.create_rectangle(self.sx(x0), self.sx(y0), self.sx(x1), self.sx(y1), fill=fill,
                                 outline=outline, width=max(0, round(width * self.S)) if width else 0, tags="dyn")

    def hit(self, x0, y0, x1, y1, fn, kind="click"):
        self.hits.append((x0, y0 + self.off, x1, y1 + self.off, fn, kind))

    def find_hit(self, e):
        x, y = e.x / self.S, e.y / self.S
        for h in reversed(self.hits):
            if h[0] <= x <= h[2] and h[1] <= y <= h[3]:
                return h
        return None

    # ---- drawing -------------------------------------------------------------------
    def redraw(self):
        self.cv.delete("dyn")
        self.hits = []
        self.draw_tag()
        self.draw_top_buttons()
        self.draw_list()
        self.draw_settings()

    def draw_tag(self):
        insts = self.eng.ordered()
        cpu = sum(i.cpu for i in insts)
        mem = sum(i.mem or 0 for i in insts) / 2**30
        self.T(193, 36, "RBX", 24, TAG_INK)
        self.T(193, 58, "LIMITER", 12, TAG_INK)
        self.R(160, 69, 227, 71, fill=TAG_INK)
        self.T(193, 84, f"{len(insts)} OPEN", 12, INK)
        self.T(193, 100, f"CPU {cpu:4.1f}%", 11, INK)
        self.T(193, 115, f"RBX {mem:4.1f}G", 11, INK)
        m = self.eng.mem
        if m:
            lvl = m[1] / m[2]
            col = REDC if lvl > 0.9 else ("#a4561e" if lvl > 0.8 else GREEN)
            self.T(193, 130, f"MEM {lvl * 100:3.0f}%", 11, col)
            self.hit(150, 122, 237, 138, self.memory_info)
        self.R(160, 140, 227, 141, fill=TAG_INK)
        if self.s.is_admin():
            self.T(193, 156, "ADMIN", 10, GREEN)
        else:
            self.T(193, 151, "NOT ADMIN", 10, REDC)
            self.T(193, 165, "click: elevate", 8, SOFT, bold=False)
            self.hit(150, 142, 237, 175, self.elevate)

    def draw_top_buttons(self):
        on = self.cfg["master"]
        for dx, dy, col in ((1, 1, DARK), (0, 0, "#c9f59f" if on else "#ffb4a8")):
            self.T(90 + dx, 152 + dy, "LIMITS", 11, col)
            self.T(90 + dx, 176 + dy, "ON" if on else "OFF", 20, col)
        self.hit(50, 127, 130, 207, self.toggle_master)
        # minimize glyph + close mushroom
        self.R(266, 154, 288, 159, fill=DARK)
        self.hit(258, 132, 296, 170, self.minimize)
        self.hit(300, 131, 344, 168, self.quit)

    def draw_list(self):
        self.T(60, 225, "NAME", 11, WOOD, "w")
        self.T(170, 225, "CPU", 11, WOOD, "e")
        self.T(234, 225, "RAM", 11, WOOD, "e")
        self.T(274, 225, "MODE", 11, WOOD)
        self.T(328, 225, "KILL", 9, WOOD)
        insts = self.eng.ordered()
        self.scroll = max(0, min(self.scroll, len(insts) - self.rows))
        if not insts:
            self.T(193, 330, "no Roblox running", 13, SOFT)
            self.T(193, 352, "instances show up here", 10, SOFT, bold=False)
            self.T(193, 368, "automatically", 10, SOFT, bold=False)
        for r, i in enumerate(insts[self.scroll:self.scroll + self.rows]):
            y0 = LIST_TOP + r * ROW_H
            cy = y0 + ROW_H / 2
            if r % 2:
                self.R(37, y0, 351, y0 + ROW_H, fill=STRIPE)
            self.T(42, cy, f"{i.idx}", 9, SOFT, "w")
            nm = self.eng.name(i)
            if nm:
                self.T(58, cy - 6, nm if len(nm) <= 10 else nm[:9] + "\u2026", 12, INK, "w")
                self.T(58, cy + 8, f"pid {i.pid}", 8, SOFT, "w", bold=False)
            else:
                self.T(58, cy - 5, str(i.pid), 12, INK, "w")
                self.T(58, cy + 8, "click to name", 8, SOFT, "w", bold=False)
            self.hit(38, y0, 130, y0 + ROW_H, lambda i=i, y=cy: self.start_rename(i, y))
            self.hit(132, y0, 240, y0 + ROW_H, lambda p=i.pid: self.focus(p))
            self.T(170, cy, f"{i.cpu:4.1f}%", 12, REDC if i.cpu > self.cfg["cpu_cap"] * 1.15 and i.limited else INK, "e")
            m = (i.mem or 0) / 2**20
            mtxt = "?" if i.mem is None else (f"{m / 1024:.2f}G" if m >= 1000 else f"{m:.0f}M")
            self.T(234, cy, mtxt, 12, INK, "e")
            # mode pill
            if not self.cfg["master"]:
                txt, bg, fg, ol = "OFF", PAPER, SOFT, SOFT
            elif i.exempt:
                txt, bg, fg, ol = "FREE", PAPER, DARK, DARK
            elif i.h and self.eng.loading(i) and not i.missing:
                txt, bg, fg, ol = "LOAD", "#6d5a45", "#f4ead9", "#6d5a45"
            elif not i.h or not i.active:
                txt, bg, fg, ol = "DENIED", REDC, "#ffe6d0", REDC
            elif i.missing:
                txt, bg, fg, ol = "PARTIAL", "#a4561e", "#ffe6d0", "#a4561e"
            else:
                txt, bg, fg, ol = "LIMIT", DARK, WOOD, DARK
            self.R(244, y0 + 7, 304, y0 + 25, fill=bg, outline=ol, width=2)
            self.T(274, cy, txt, 10, fg)
            self.hit(244, y0 + 4, 304, y0 + 28, lambda i=i: self.toggle_exempt(i))
            if self.arm == i.pid and time.time() < self.arm_until:
                self.R(316, y0 + 7, 341, y0 + 25, fill=REDC)
                self.T(328, cy, "?", 12, "#ffe6d0")
            else:
                self.T(328, cy, "✕", 14, REDC)
            self.hit(316, y0 + 4, 342, y0 + 28, lambda p=i.pid: self.kill(p))
        if len(insts) > self.rows:
            span = self.rows * ROW_H
            th = span * self.rows / len(insts)
            ty = LIST_TOP + span * self.scroll / len(insts)
            self.R(346, LIST_TOP, 349, LIST_TOP + span, fill=STRIPE)
            self.R(346, ty, 349, ty + th, fill=DARK)

    def draw_slider(self, key, y):
        lo, hi, _ = SLIDERS[key]
        x0, x1 = 50, 338
        frac = (self.cfg[key] - lo) / (hi - lo)
        kx = x0 + frac * (x1 - x0)
        dim = (key == "ram_cap" and self.cfg["ram_mode"] != "hard") or (key == "afk_min" and not self.cfg["afk_on"])
        self.R(x0, y - 4, x1, y + 4, fill=PAPER, outline=DARK, width=2)
        self.R(x0 + 2, y - 2, kx, y + 2, fill=SOFT if dim else "#c98b52")
        self.R(kx - 5, y - 9, kx + 5, y + 9, fill=DARK)
        self.R(kx - 2, y - 6, kx + 2, y + 6, fill=SOFT if dim else WOOD)
        self.hit(x0 - 8, y - 12, x1 + 8, y + 12, key, "slider")

    def draw_pill(self, y, label, value, fn):
        self.T(45, y, label, 12, INK, "w")
        self.R(214, y - 10, 342, y + 10, fill=DARK)
        self.T(224, y, "◀", 9, WOOD)
        self.T(332, y, "▶", 9, WOOD)
        self.T(278, y, value, 11, WOOD)
        self.hit(214, y - 12, 342, y + 12, fn)

    def draw_check(self, y, label, key, fn=None, hit_to=340, x=45):
        on = bool(self.cfg[key])
        self.R(x, y - 7, x + 14, y + 7, fill=PAPER, outline=DARK, width=2)
        if on:
            self.R(x + 4, y - 3, x + 10, y + 3, fill=REDC)
        self.T(x + 23, y, label, 12, INK, "w")
        self.hit(x - 5, y - 9, hit_to, y + 9, fn or (lambda: self.toggle(key)))

    def draw_button(self, x0, x1, y0, label, fn, danger=False):
        self.R(x0, y0, x1, y0 + 24, fill=REDC if danger else DARK)
        self.R(x0 + 2, y0 + 21, x1 - 2, y0 + 22, fill="#5e1009" if danger else "#2c231b")
        self.T((x0 + x1) / 2, y0 + 12, label, 11, "#ffe6d0" if danger else WOOD)
        self.hit(x0, y0, x1, y0 + 24, fn)

    def draw_bar(self, y, title, right=""):
        self.R(35, y, 351, y + 20, fill=DARK)
        self.R(35, y + 20, 351, y + 22, fill=WOOD)
        self.R(35, y + 22, 351, y + 24, fill=DARK)
        self.T(45, y + 10, title, 11, WOOD, "w")
        if right:
            self.T(343, y + 10, right, 9, WOOD, "e", bold=False)

    def draw_grip(self):
        """Resize handle hanging under the bottom-right corner ornament."""
        x1, y0 = 376, 966
        for k in range(4):
            self.R(x1 - 7 * (k + 1), y0 + 7 * k, x1, y0 + 7 * (k + 1), fill=DARK)
            self.R(x1 - 7 * (k + 1) + 2, y0 + 7 * k + 2, x1 - 7 * (k + 1) + 5, y0 + 7 * k + 5, fill=WOOD)
        self.hit(344, y0 - 4, 380, y0 + 30, "grip", "grip")

    def draw_settings(self):
        self.off = self.E
        try:
            self._draw_settings()
            self.draw_grip()
        finally:
            self.off = 0

    def _draw_settings(self):
        self.draw_bar(482, "LIMITS", "per instance")

        cap = self.cfg["cpu_cap"]
        self.T(45, 520, "CPU CAP", 12, INK, "w")
        self.T(342, 520, f"{cap}%  \u2248{cap / 100 * self.s.cpu_count:.1f} cores", 12, INK, "e")
        self.draw_slider("cpu_cap", 538)

        ram_on = self.cfg["ram_mode"] == "hard"
        self.T(45, 558, "RAM CAP", 12, INK, "w")
        self.T(342, 558, f"{self.cfg['ram_cap']:,} MB" if ram_on else "(hard cap off)", 12,
               INK if ram_on else SOFT, "e")
        self.draw_slider("ram_cap", 576)

        self.draw_pill(600, "RAM MODE", dict(RAM_MODES)[self.cfg["ram_mode"]], self.cycle_ram)
        self.draw_pill(624, "PRIORITY", dict(PRIOS)[self.cfg["priority"]], self.cycle_prio)
        fps = self.cfg["fps_cap"]
        self.draw_pill(648, "FPS CAP", f"~{fps} FPS" if fps else "OFF", lambda: None)
        self.hits.pop()
        self.hit(214, 636, 278, 660, lambda: self.step_fps(-1))
        self.hit(278, 636, 342, 660, lambda: self.step_fps(1))
        self.T(110, 648, "LIMIT only", 8, SOFT, "w", bold=False)
        self.draw_check(670, "auto-limit new", "auto", hit_to=210)
        self.draw_check(670, "on top", "topmost", self.toggle_topmost, x=230)
        self.draw_check(689, "multi-inst", "multi", self.toggle_multi, hit_to=150)
        self.draw_check(689, "low-mem gfx", "low_gfx", self.toggle_low_gfx, x=170)

        if self.cfg["afk_on"]:
            left = int(self.eng.afk_left())
            right = "pressing..." if self.eng.afk_busy else f"next in {left // 60}:{left % 60:02d}"
        else:
            right = "off"
        self.draw_bar(700, "ANTI-AFK", right)
        self.draw_check(737, "press key every", "afk_on", self.toggle_afk, hit_to=250)
        m = self.cfg["afk_min"]
        self.T(342, 737, f"{m} min", 12, INK if self.cfg["afk_on"] else SOFT, "e")
        self.draw_slider("afk_min", 756)
        self.draw_check(777, "skip FREE", "afk_skip_free", hit_to=150)
        self.T(158, 777, "KEY", 11, INK, "w")
        self.R(190, 767, 342, 787, fill=DARK)
        self.T(200, 777, "\u25c0", 9, WOOD)
        self.T(332, 777, "\u25b6", 9, WOOD)
        self.T(266, 777, AFK_KEYS[self.cfg["afk_key"]][0], 11, WOOD)
        self.hit(155, 765, 342, 789, self.cycle_afk_key)

        self.draw_button(45, 114, 795, "APPLY", self.apply_all)
        self.draw_button(119, 188, 795, "TILE", self.tile_now)
        self.draw_button(193, 266, 795, "AFK NOW", self.afk_now)
        sure = self.arm == "all" and time.time() < self.arm_until
        self.draw_button(271, 342, 795, "SURE?" if sure else "KILL ALL", self.kill_all, danger=True)
        msg = self.eng.message
        if len(msg) > 46:
            msg = msg[:45] + "\u2026"
        self.T(193, 834, msg, 10, SOFT, bold=False)

    # ---- actions -------------------------------------------------------------------
    def save(self):
        save_cfg(self.cfg)

    def toggle_master(self):
        self.cfg["master"] = not self.cfg["master"]
        self.eng.apply_all()
        self.save()

    def toggle(self, key):
        self.cfg[key] = not self.cfg[key]
        self.save()

    def toggle_afk(self):
        self.toggle("afk_on")
        self.eng.afk_reset()
        self.eng.message = f"anti-AFK on - every {self.cfg['afk_min']} min" if self.cfg["afk_on"] else "anti-AFK off"

    def step_fps(self, d):
        i = FPS_STEPS.index(self.cfg["fps_cap"]) if self.cfg["fps_cap"] in FPS_STEPS else 0
        self.cfg["fps_cap"] = FPS_STEPS[max(0, min(len(FPS_STEPS) - 1, i + d))]
        self.save()
        self.eng.fps_update()
        fps = self.cfg["fps_cap"]
        self.eng.message = f"FPS cap ~{fps} on LIMIT instances" if fps else "FPS cap off"

    def cycle_afk_key(self):
        k = AFK_ORDER[(AFK_ORDER.index(self.cfg["afk_key"]) + 1) % len(AFK_ORDER)]
        self.cfg["afk_key"] = k
        self.save()
        self.eng.message = f"anti-AFK key: {AFK_KEYS[k][0]}"

    def afk_now(self):
        if self.eng.afk_busy:
            return
        if not self.eng.inst:
            self.eng.message = "no instances to press"
            return
        self.eng.afk_run()

    def toggle_low_gfx(self):
        self.toggle("low_gfx")
        self.eng.low_gfx_apply()

    def toggle_topmost(self):
        self.toggle("topmost")
        self.root.wm_attributes("-topmost", bool(self.cfg["topmost"]))

    def toggle_multi(self):
        self.toggle("multi")
        if self.cfg["multi"]:
            self.eng.message = self.s.multi_on() or "multi-instance on - launch Roblox now"
        else:
            self.s.multi_off()
            self.eng.message = "multi-instance off"

    def cycle(self, key, options):
        keys = [k for k, _ in options]
        self.cfg[key] = keys[(keys.index(self.cfg[key]) + 1) % len(keys)]
        self.eng.apply_all()
        self.save()

    def start_rename(self, i, cy):
        self.end_rename(save=True)
        e = tk.Entry(self.cv, font=self.font(12), bg="#f4ead9", fg=INK, relief="flat", bd=0,
                     highlightthickness=max(1, self.sx(2)), highlightcolor=DARK, highlightbackground=DARK,
                     insertbackground=INK)
        e.insert(0, self.eng.name(i))
        e.select_range(0, "end")
        self.cv.create_window(self.sx(56), self.sx(cy), window=e, anchor="w", width=self.sx(180),
                              height=self.sx(22), tags="edit")
        self.editing = (i, e)
        e.bind("<Return>", lambda _e: (self.end_rename(save=True), "break")[1])
        e.bind("<Escape>", lambda _e: (self.end_rename(save=False), "break")[1])
        e.bind("<FocusOut>", lambda _e: self.end_rename(save=True))
        e.focus_force()
        self.eng.message = "type a name, Enter = save, Esc = cancel"

    def end_rename(self, save=True):
        ed, self.editing = getattr(self, "editing", None), None
        if not ed:
            return
        i, e = ed
        if save:
            try:
                self.eng.set_name(i, e.get())
                self.save()
                self.eng.message = f"slot {i.idx} named '{self.eng.label(i)}'" if self.eng.name(i) else f"slot {i.idx} name cleared"
            except tk.TclError:
                pass
        self.cv.delete("edit")
        try:
            e.destroy()
        except tk.TclError:
            pass
        self.redraw()

    def memory_info(self):
        m = self.eng.mem
        if m:
            self.eng.message = (f"committed {m[1] / 2**30:.1f} of {m[2] / 2**30:.1f} GB "
                                f"(RAM {m[3] / 2**30:.0f} GB + pagefile)")

    def tile_now(self):
        hwnd = self.hwnd
        if not hwnd and IS_WIN:
            try:
                hwnd = self.s.u32.GetParent(self.root.winfo_id())
            except Exception:
                hwnd = None
        area = self.s.monitor_area(hwnd)
        self.cfg["tile_area"] = list(area)
        self.save()
        n = self.eng.tile(area=area)
        self.eng.message = f"tiled {n} window(s) small" if n else "nothing to tile (FREE ones are skipped)"

    def cycle_ram(self):
        self.cycle("ram_mode", RAM_MODES)

    def cycle_prio(self):
        self.cycle("priority", PRIOS)

    def toggle_exempt(self, i):
        if not self.cfg["master"]:
            self.eng.message = "turn LIMITS on first"
            return
        if not i.exempt and i.missing and getattr(self, "armed_free", None) != i.pid:
            # first click on a DENIED / PARTIAL pill explains what's wrong; second click frees it
            self.armed_free = i.pid
            why = i.errors[0] if i.errors else "Windows reset it, retrying"
            self.eng.message = f"{self.eng.label(i)}: missing {'+'.join(i.missing).upper()} - {why}"
            return
        self.armed_free = None
        i.exempt = not i.exempt
        self.eng.apply(i)
        self.eng.verify(i)
        if i.exempt:
            self.eng.message = f"{self.eng.label(i)}: free (no limits)"
        elif i.missing:
            self.eng.message = f"{self.eng.label(i)}: missing {'+'.join(i.missing).upper()}" + (f" - {i.errors[0]}" if i.errors else "")
        else:
            self.eng.message = f"{self.eng.label(i)}: limited"

    def apply_all(self):
        self.eng.apply_all()

    def trim_now(self):
        self.eng.trim_all()

    def armed(self, what):
        """Kill actions need a second click within 3 s."""
        if self.arm == what and time.time() < self.arm_until:
            self.arm = None
            return True
        self.arm, self.arm_until = what, time.time() + 3
        self.eng.message = "click again to confirm kill"
        self.root.after(3100, self.redraw)
        return False

    def kill(self, pid):
        if self.armed(pid):
            self.eng.kill(pid)
            self.eng.tick()

    def kill_all(self):
        if self.armed("all"):
            self.eng.kill_all()
            self.eng.tick()

    def focus(self, pid):
        self.eng.throttle.hold(pid)
        try:
            if not self.s.focus(pid):
                self.eng.message = "no window found for that instance"
        finally:
            self.eng.throttle.release(pid)   # stays unfrozen while it's the foreground window

    def elevate(self):
        if self.s.relaunch_admin():
            self.quit()
        else:
            self.eng.message = "elevation cancelled"

    def minimize(self):
        if self.hwnd:
            self.s.minimize(self.hwnd)
        else:
            try:
                self.root.overrideredirect(False)
                self.root.iconify()
            except tk.TclError:
                pass

    def _on_map(self, _e):
        if not self.hwnd:
            try:
                self.root.overrideredirect(True)
            except tk.TclError:
                pass

    def _fix_window(self):
        try:
            self.hwnd = self.s.window_fixups(self.root)
        except Exception:
            self.hwnd = None

    def quit(self):
        self.clean_exit = True
        try:
            self.end_rename(save=True)
            self.eng.throttle.stop()
            self.eng.sync_titles(reset=True)
            self.eng.restore_all()
            self.s.multi_off()
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.save()
        finally:
            self.root.destroy()

    # ---- events --------------------------------------------------------------------
    def set_slider(self, key, ex):
        lo, hi, step = SLIDERS[key]
        frac = min(1, max(0, (ex / self.S - 50) / (338 - 50)))
        v = lo + round(frac * (hi - lo) / step) * step
        if v != self.cfg[key]:
            self.cfg[key] = int(v)
            self.redraw()

    def on_press(self, e):
        if getattr(self, "editing", None):
            self.end_rename(save=True)
        h = self.find_hit(e)
        if h is None:
            self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        elif h[5] == "grip":
            if time.time() - self.grip_click < 0.35:  # double-click = reset
                self.grip_click = 0
                self.reset_size()
            else:
                self.grip_click = time.time()
                self.resizing = True
        elif h[5] == "slider":
            self.slider = h[4]
            self.set_slider(self.slider, e.x)
        else:
            h[4]()
            self.redraw()

    def on_motion(self, e):
        if self.resizing:
            w = max(80, e.x_root - self.root.winfo_x())
            h = max(80, e.y_root - self.root.winfo_y())
            # width picks the scale step (closest), height picks the number of list rows
            target = w / BG_W
            step = min(range(len(SCALES)), key=lambda i: abs(SCALES[i][0] / SCALES[i][1] - target))
            sc = SCALES[step][0] / SCALES[step][1]
            extra = int(round((h / sc - BG_H) / ROW_H)) * ROW_H
            self.apply_size(step, extra, save=False)
        elif self.slider:
            self.set_slider(self.slider, e.x)
        elif self.drag:
            self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def on_release(self, _e):
        if self.resizing:
            self.resizing = False
            self.cfg["scale_step"], self.cfg["extra"] = self.step, self.E
            self.save()
        if self.slider:
            if self.slider == "afk_min":
                self.eng.afk_reset()
                self.eng.message = f"anti-AFK every {self.cfg['afk_min']} min"
                self.slider = None
                self.save()
                self.redraw()
                return
            self.slider = None
            self.eng.apply_all()
            self.save()
            self.redraw()
        if self.drag:
            self.drag = None
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.save()

    def on_hover(self, e):
        h = self.find_hit(e)
        cur = ("size_nw_se" if IS_WIN else "bottom_right_corner") if h and h[5] == "grip" else ("hand2" if h else "")
        if self.cv.cget("cursor") != cur:
            self.cv.config(cursor=cur)

    def on_wheel(self, e):
        self.wheel(-1 if e.delta > 0 else 1)

    def wheel(self, d):
        self.scroll += d
        self.redraw()

    def loop(self):
        t0 = time.perf_counter()
        if t0 - getattr(self, "_last_self_trim", 0) > 60:
            self._last_self_trim = t0
            try:
                self.s.trim_self()
            except Exception:
                pass
        try:
            self.eng.tick()
        except Exception as ex:  # never let the loop die
            log_error("tick")
            self.eng.message = f"error: {ex} (see crash.log)"
        try:
            if not self.slider:
                self.redraw()
        except Exception:
            log_error("redraw")
        finally:
            # always reschedule; if the PC is busy, back off a little instead of piling up
            spent = int((time.perf_counter() - t0) * 1000)
            self.root.after(max(TICK_MS - spent, 250), self.loop)

    def _tk_error(self, *args):
        log_error("tk callback", args)
        self.eng.message = "an error was logged to crash.log"

    def run(self):
        self.root.mainloop()


# =====================================================================================
#  Crash logging + watchdog
# =====================================================================================
_LOG_FILE = None


def log_path():
    return os.path.join(os.path.dirname(cfg_path()), "crash.log")


def log_error(where, exc_info=None):
    try:
        if exc_info and len(exc_info) == 3 and exc_info[2] is not None:
            tb = "".join(traceback.format_exception(*exc_info))
        elif exc_info is None:
            tb = traceback.format_exc()
        else:
            tb = ""
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{tb}")
        if os.path.getsize(log_path()) > 512 * 1024:          # keep the log small
            with open(log_path(), "r", encoding="utf-8", errors="ignore") as f:
                tail = f.read()[-256 * 1024:]
            with open(log_path(), "w", encoding="utf-8") as f:
                f.write(tail)
    except Exception:
        pass


def setup_crash_logging():
    global _LOG_FILE
    try:
        _LOG_FILE = open(log_path(), "a", encoding="utf-8")
        faulthandler.enable(file=_LOG_FILE, all_threads=True)   # hard crashes (access violations)
    except Exception:
        pass
    sys.excepthook = lambda *a: log_error("uncaught", a)
    threading.excepthook = lambda a: log_error(f"thread {a.thread.name if a.thread else '?'}",
                                               (a.exc_type, a.exc_value, a.exc_traceback))


def child_cmd():
    if IS_COMPILED:
        return [own_exe(), "--child"]
    return [sys.executable, os.path.abspath(__file__), "--child"]


def supervise():
    """Tiny watchdog process: runs the real app as a child. If the app ever dies
    unexpectedly it un-freezes every Roblox instance and starts the app again."""
    sysapi = Win32()
    crashes = []
    while True:
        try:
            child = subprocess.Popen(child_cmd())
            time.sleep(3)
            sysapi.trim_self()            # the watchdog itself only needs a few hundred KB
            code = child.wait()
        except Exception:
            log_error("supervisor: could not start app")
            return
        if code in (0, 3):                # 0 = closed normally, 3 = already running
            return
        log_error(f"app exited with code {code} - un-freezing Roblox and restarting", ())
        try:
            Throttler(sysapi).wake(sysapi.list_pids(ROBLOX_EXES))
        except Exception:
            pass
        now = time.time()
        crashes = [t for t in crashes if now - t < 300] + [now]
        if len(crashes) >= 5:
            sysapi.message_box("RBX Limiter crashed 5 times in 5 minutes and was stopped.\n\n"
                               f"Details: {log_path()}")
            return
        time.sleep(2)


def main():
    setup_crash_logging()
    if IS_WIN and "--child" not in sys.argv and "--no-watchdog" not in sys.argv:
        return supervise()
    sysapi = Win32() if IS_WIN else DemoSystem()
    if not sysapi.single_instance():
        sysapi.message_box("RBX Limiter is already running.")
        sys.exit(3)
    app = None
    try:
        app = App(sysapi)
        app.run()
    except Exception:
        log_error("fatal")
    finally:
        if app is not None:
            try:
                app.eng.throttle.stop()          # make sure nothing stays frozen
            except Exception:
                pass
    if app is None or not getattr(app, "clean_exit", False):
        sys.exit(1)                     # died / window vanished -> watchdog restarts it


if __name__ == "__main__":
    main()
