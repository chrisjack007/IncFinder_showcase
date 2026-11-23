"""Own each worker process tree for cancellation and launcher termination."""
from contextlib import nullcontext, suppress
import os
import runpy
import signal
import subprocess
import sys
from threading import Thread
import time


class _WindowsJob:
    """Keep the worker and its descendants in a kill-on-close Job Object."""

    def __enter__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class BasicLimits(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                        ('min_working_set', c.c_size_t), ('max_working_set', c.c_size_t),
                        ('active_processes', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD),
                        ('scheduling', w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [('basic', BasicLimits), ('io_counters', c.c_uint64 * 6),
                        ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                        ('peak_process_memory', c.c_size_t), ('peak_job_memory', c.c_size_t)]

        class Accounting(c.Structure):
            _fields_ = [('user_time', c.c_int64), ('kernel_time', c.c_int64),
                        ('period_user_time', c.c_int64), ('period_kernel_time', c.c_int64),
                        ('page_faults', w.DWORD), ('processes', w.DWORD),
                        ('active_processes', w.DWORD), ('terminated_processes', w.DWORD)]

        self.accounting = Accounting()
        self.reference = c.byref(self.accounting)
        self.size = c.sizeof(self.accounting)
        self.api = c.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes, self.api.CreateJobObjectW.restype = (c.c_void_p, w.LPCWSTR), w.HANDLE
        self.api.SetInformationJobObject.argtypes = (w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.OpenProcess.argtypes, self.api.OpenProcess.restype = (w.DWORD, w.BOOL, w.DWORD), w.HANDLE
        self.api.AssignProcessToJobObject.argtypes, self.api.AssignProcessToJobObject.restype = (w.HANDLE, w.HANDLE), w.BOOL
        self.api.TerminateJobObject.argtypes, self.api.TerminateJobObject.restype = (w.HANDLE, w.UINT), w.BOOL
        self.api.QueryInformationJobObject.argtypes = (w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p)
        self.api.QueryInformationJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes, self.api.CloseHandle.restype = (w.HANDLE,), w.BOOL
        self.error = c.WinError
        self.job = self.api.CreateJobObjectW(None, None)
        if not self.job:
            raise self.error()
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.job, 9, c.byref(limits), c.sizeof(limits)):
            error = self.error()
            self.close()
            raise error
        return self

    def assign(self, pid):
        handle = self.api.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not handle:
            raise self.error()
        try:
            if not self.api.AssignProcessToJobObject(self.job, handle):
                raise self.error()
        finally:
            self.api.CloseHandle(handle)

    def terminate(self):
        if not self.api.TerminateJobObject(self.job, 1):
            raise self.error()
        expires = time.monotonic() + 10
        while self.api.QueryInformationJobObject(self.job, 1, self.reference, self.size, None):
            if not self.accounting.active_processes:
                return
            if time.monotonic() >= expires:
                raise RuntimeError('Timed out waiting for the encoding worker process tree')
            time.sleep(0.02)
        raise self.error()

    def close(self):
        if self.job:
            handle, self.job = self.job, None
            if not self.api.CloseHandle(handle):
                raise self.error()

    def __exit__(self, kind, value, traceback):
        self.close()


def _kill_group(pid):
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def run_process_tree(command, *, cwd, env, stdout):
    """Run an internal module after establishing its process ownership."""
    windows = os.name == 'nt'
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if windows else {'start_new_session': True}
    with (_WindowsJob() if windows else nullcontext()) as job:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout,
                                stderr=subprocess.STDOUT, **options)
        assigned = False
        try:
            if job:
                job.assign(proc.pid)
                assigned = True
            proc.stdin.write(b'\n')
            proc.stdin.flush()
            while True:
                try:
                    return proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            if assigned:
                job.terminate()
            elif windows:
                # The worker is still gated if assignment failed.
                if proc.poll() is None:
                    proc.kill()
            else:
                _kill_group(proc.pid)
            proc.wait()
            raise
        finally:
            with suppress(BrokenPipeError):
                proc.stdin.close()


def _watch_launcher(control):
    try:
        if not os.read(control, 1):
            _kill_group(os.getpgrp())
    finally:
        os.close(control)


def _entry():
    module, *args = sys.argv[1:]
    if os.read(0, 1) != b'\n':
        return
    if os.name != 'nt':
        # Keep the lifetime pipe separate from stdin for spawned descendants.
        control = os.dup(0)
        with open(os.devnull, 'rb') as unused:
            os.dup2(unused.fileno(), 0)
        Thread(target=_watch_launcher, args=(control,), daemon=True).start()
    sys.argv = [module, *args]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    _entry()
