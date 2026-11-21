"""Run candidate tests against a patched tree, cheaply and in isolation.

The pinned checkout is read-only, so each commit gets an overlay directory in
/tmp: real directories only along the paths the patch touches, symlinks to the
pinned tree everywhere else (including __pycache__, so unchanged modules load
from their precompiled bytecode). A child forked from the main process (which
has third-party libraries loaded, but never sqlglot) imports sqlglot and the
tests from the overlay, runs the requested tests with per-test time limits and
reports which failed.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import sys
import time

FILE = object()
_COPY = object()


def build_overlay(dst: str, root: str, files: dict[str, bytes | None], copy_dirs=("tests",)) -> None:
    """files: relative path -> new content, or None for a deleted file.

    Python files under `copy_dirs` are copied (with their mtime, so the
    symlinked __pycache__ stays valid) rather than symlinked: a test that
    resolves its own path (Path(__file__).resolve()) must land in the overlay.
    """
    for top in copy_dirs:
        top_dir = os.path.join(root, top)
        for dirpath, dirnames, filenames in os.walk(top_dir):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "fixtures")]
            for name in filenames:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    files.setdefault(rel, _COPY)
    tree: dict = {}
    for rel, content in files.items():
        node = tree
        parts = [p for p in rel.split("/") if p]
        for part in parts[:-1]:
            child = node.get(part)
            if child is FILE or child is None:
                child = node[part] = {}
            node = child
        node[parts[-1]] = (FILE, rel)
    os.makedirs(dst, exist_ok=True)

    def fill(dst_dir: str, src_dir: str, node: dict):
        names = set(os.listdir(src_dir)) if os.path.isdir(src_dir) else set()
        for name in sorted(names | set(node)):
            src = os.path.join(src_dir, name)
            target = os.path.join(dst_dir, name)
            entry = node.get(name)
            if entry is None:
                os.symlink(src, target)
            elif isinstance(entry, tuple):
                content = files[entry[1]]
                if content is None:
                    continue
                if content is _COPY:
                    shutil.copy2(src, target)
                    continue
                with open(target, "wb") as handle:
                    handle.write(content)
            else:
                os.mkdir(target)
                fill(target, src, entry)

    fill(dst, root, tree)


def module_name(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    name = rel[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def _rehome_modules(root: str, overlay: str, warm: bool):
    """Point preloaded sqlglot modules at the overlay (warm), or drop them (cold)."""
    import importlib

    for name in list(sys.modules):
        if not (name in ("sqlglot", "tests") or name.startswith("sqlglot.") or name.startswith("tests.")):
            continue
        module = sys.modules[name]
        if not warm or name == "tests" or name.startswith("tests."):
            del sys.modules[name]
            continue

        def move(path):
            return overlay + path[len(root):] if isinstance(path, str) and path.startswith(root) else path

        if getattr(module, "__file__", None):
            module.__file__ = move(module.__file__)
        path = getattr(module, "__path__", None)
        if path is not None:
            module.__path__ = [move(p) for p in path]
        spec = getattr(module, "__spec__", None)
        if spec is not None:
            if spec.origin:
                spec.origin = move(spec.origin)
            if spec.submodule_search_locations is not None:
                spec.submodule_search_locations[:] = [move(p) for p in spec.submodule_search_locations]
    importlib.invalidate_caches()


def _child(overlay: str, root: str, files: dict, tests: list[str], limits: dict, check_modules: list[str], out_fd: int, memory_limit: int | None, warm: bool = False):
    result = {"failed": [], "import_errors": {}, "durations": {}, "error": None}
    try:
        if memory_limit:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            except (ValueError, OSError):
                pass
        build_overlay(overlay, root, dict(files))
        _rehome_modules(root, overlay, warm)
        os.chdir(overlay)
        sys.path[:] = [overlay] + [p for p in sys.path if os.path.realpath(p or ".") != os.path.realpath(root) and p not in ("", ".")]
        sys.dont_write_bytecode = True
        # Silence test output: the parent's stdout is the answer channel.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
        import importlib

        from selector.runner import run_tests

        for name in check_modules:
            signal.signal(signal.SIGALRM, _raise_timeout)
            signal.setitimer(signal.ITIMER_REAL, 20)
            try:
                importlib.import_module(name)
            except BaseException as error:  # noqa: BLE001
                result["import_errors"][name] = repr(error)[:300]
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        failed, durations = run_tests(tests, limit_of=lambda node: limits.get(node, 30.0))
        result["failed"] = sorted(failed)
        result["durations"] = {k: round(v, 4) for k, v in durations.items()}
    except BaseException as error:  # noqa: BLE001
        result["error"] = repr(error)[:500]
    data = json.dumps(result).encode()
    view = memoryview(data)
    while view:
        written = os.write(out_fd, view)
        view = view[written:]
    os.close(out_fd)


class _ImportTimeout(BaseException):
    pass


def _raise_timeout(signum, frame):
    raise _ImportTimeout()


class Job:
    def __init__(self, key, files, tests, limits, check_modules, budget, warm=False):
        self.warm = warm
        self.key = key
        self.files = files
        self.tests = tests
        self.limits = limits
        self.check_modules = check_modules
        self.budget = budget
        self.pid = None
        self.fd = None
        self.started = None
        self.buffer = b""
        self.result = None
        self.cpu = 0.0
        self.wall = 0.0


def run_jobs(jobs: list[Job], root: str, tmp_base: str, parallel: int, deadline: float, memory_limit: int | None = None, log=None):
    """Run jobs (fork per job), at most `parallel` at once. A job that overruns
    its budget or the global deadline is killed and gets result None."""
    import gc
    import select

    gc.collect()
    gc.freeze()  # children must not drag the parent's objects through their GC (copy-on-write)
    pending = list(jobs)
    running: dict[int, Job] = {}
    while pending or running:
        while pending and len(running) < parallel:
            job = pending.pop(0)
            if time.monotonic() > deadline:
                job.result = None
                continue
            read_fd, write_fd = os.pipe()
            overlay = os.path.join(tmp_base, f"ov_{job.key}")
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                code = 0
                try:
                    _child(overlay, root, job.files, job.tests, job.limits, job.check_modules, write_fd, memory_limit, job.warm)
                except BaseException:  # noqa: BLE001
                    code = 1
                os._exit(code)
            os.close(write_fd)
            job.pid, job.fd, job.started = pid, read_fd, time.monotonic()
            running[pid] = job
        if not running:
            break
        fds = {job.fd: job for job in running.values() if job.fd is not None}
        wait_for = 0.02
        if not fds:
            time.sleep(wait_for)  # every pipe is closed: children are exiting
        else:
            ready, _, _ = select.select(list(fds), [], [], wait_for)
            for fd in ready:
                job = fds[fd]
                chunk = os.read(fd, 1 << 16)
                if chunk:
                    job.buffer += chunk
                else:
                    os.close(fd)
                    job.fd = None
        now = time.monotonic()
        for pid, job in list(running.items()):
            done_pid, status, usage = os.wait4(pid, os.WNOHANG)
            if done_pid == pid:
                if job.fd is not None:
                    while True:
                        chunk = os.read(job.fd, 1 << 16)
                        if not chunk:
                            break
                        job.buffer += chunk
                    os.close(job.fd)
                    job.fd = None
                job.cpu = usage.ru_utime + usage.ru_stime
                job.wall = time.monotonic() - job.started
                try:
                    job.result = json.loads(job.buffer.decode()) if job.buffer else None
                except ValueError:
                    job.result = None
                del running[pid]
                shutil.rmtree(os.path.join(tmp_base, f"ov_{job.key}"), ignore_errors=True)
                if log:
                    log(f"verify {job.key}: {len(job.tests)} tests, cpu {job.cpu:.2f}s wall {job.wall:.2f}s -> "
                        f"{'none' if job.result is None else len(job.result.get('failed', []))} failed"
                        f"{' ERROR ' + str(job.result.get('error')) if job.result and job.result.get('error') else ''}"
                        f"{' IMPORT ' + str(list(job.result.get('import_errors', {}))) if job.result and job.result.get('import_errors') else ''}")
                continue
            over_budget = now - job.started > job.budget
            if over_budget or now > deadline:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status, usage = os.wait4(pid, 0)
                if job.fd is not None:
                    os.close(job.fd)
                    job.fd = None
                job.cpu = usage.ru_utime + usage.ru_stime
                job.wall = time.monotonic() - job.started
                job.result = None
                del running[pid]
                shutil.rmtree(os.path.join(tmp_base, f"ov_{job.key}"), ignore_errors=True)
                if log:
                    log(f"verify {job.key}: killed after {job.wall:.1f}s")
    return jobs
