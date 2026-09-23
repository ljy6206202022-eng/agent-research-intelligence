"""One-shot, resource-only diagnostics for an explicitly armed YouTube job.

The probe never reads messages, page bodies, credentials, or process command lines.
It has no authority to change resource limits or keep a background service alive.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from uuid import uuid4

import psutil

ARM = 'config/resource-probe/next-run.json'
OUT = 'data/resource-probe'


class ResourceProbe:
    def __init__(self, workspace, probe_id):
        self.ws = workspace
        self.probe_id = probe_id
        self.root = workspace.root / OUT
        self.samples = self.root / (probe_id + '.jsonl')
        self.stop_path = self.root / (probe_id + '.stop.json')
        self.stage = 'INITIALIZING'
        self.job_id = None
        self.guard_callsite = None
        self.guard_count = 0
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.initial_swap = psutil.swap_memory().used
        self.gateway_pid = self._gateway_pid()
        self.thread = None

    @classmethod
    def claim_next(cls, workspace):
        arm = workspace.root / ARM
        if not arm.is_file() or arm.is_symlink():
            return None
        value = json.loads(arm.read_text())
        if value != {'purpose': 'NEXT_REAL_YOUTUBE_RESEARCH_ONCE'}:
            raise ValueError('INVALID_RESOURCE_PROBE_ARM')
        probe_id = uuid4().hex
        consumed = workspace.root / OUT / (probe_id + '.arm.json')
        os.replace(arm, consumed)  # Exactly one normal job claims the diagnostic.
        probe = cls(workspace, probe_id)
        probe._write(probe.root / (probe_id + '.start.json'),
                     {'probe_id': probe_id, 'at_epoch': time.time(),
                      'classification': 'REAL_RESOURCE_DIAGNOSTIC',
                      'arm': str(consumed.relative_to(workspace.root))})
        probe.thread = threading.Thread(target=probe._sample_loop, daemon=True,
                                        name='one-shot-resource-probe')
        probe.thread.start()
        return probe

    @staticmethod
    def _gateway_pid():
        try:
            result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/ai.agent_runtime.gateway'],
                                    capture_output=True, text=True, timeout=1, check=False)
            match = re.search(r'(?m)^\s*pid = (\d+)\s*$', result.stdout)
            return int(match.group(1)) if match else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _pressure_free_percent():
        try:
            result = subprocess.run(['/usr/bin/memory_pressure', '-Q'], capture_output=True,
                                    text=True, timeout=1, check=False)
            match = re.search(r'System-wide memory free percentage:\s*(\d+)%', result.stdout)
            return int(match.group(1)) if match else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _rss(process):
        try:
            return process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    def _snapshot(self):
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        parent = psutil.Process(os.getpid())
        try:
            tree = [parent, *parent.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            tree = [parent]
        tree_rss = sum(value for p in tree if (value := self._rss(p)) is not None)
        categories = {name: {'count': 0, 'rss_bytes': 0} for name in
                      ('agent_runtime_gateway', 'chatgpt_codex', 'browser_media')}
        if self.gateway_pid is not None:
            try:
                gateway = psutil.Process(self.gateway_pid)
                members = [gateway, *gateway.children(recursive=True)]
                categories['agent_runtime_gateway']['count'] = len(members)
                categories['agent_runtime_gateway']['rss_bytes'] = sum(
                    value for p in members if (value := self._rss(p)) is not None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for p in psutil.process_iter(['pid', 'name']):
            try:
                name = (p.info['name'] or '').lower()
                if 'codex' in name or 'chatgpt' in name:
                    group = 'chatgpt_codex'
                elif any(term in name for term in ('chrome', 'chromium', 'firefox',
                                                  'safari', 'ffmpeg', 'yt-dlp')):
                    group = 'browser_media'
                else:
                    continue
                if (value := self._rss(p)) is not None:
                    categories[group]['count'] += 1
                    categories[group]['rss_bytes'] += value
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {'at_epoch': time.time(), 'stage': self.stage, 'job_id': self.job_id,
                'guard_callsite': self.guard_callsite, 'guard_count': self.guard_count,
                'system_available_bytes': memory.available,
                'system_memory_percent': memory.percent,
                'memory_pressure_free_percent': self._pressure_free_percent(),
                'swap_used_bytes': swap.used,
                'swap_delta_bytes': swap.used - self.initial_swap,
                'research_tree_rss_bytes': tree_rss,
                'research_tree_process_count': len(tree),
                'groups': categories}

    @staticmethod
    def _write(path, value):
        raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
        with path.open('xb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())

    def _sample_loop(self):
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    if self.stop_event.is_set():
                        break
                    value = self._snapshot()
                    with self.samples.open('ab') as out:
                        out.write((json.dumps(value, sort_keys=True) + '\n').encode())
                        out.flush()
            except (OSError, ValueError, psutil.Error):
                # Observation must never replace the original safety decision.
                pass
            self.stop_event.wait(1.0)

    def set_stage(self, stage, job_id=None):
        self.stage = stage
        if job_id is not None:
            self.job_id = job_id

    def note_guard(self, callsite):
        self.guard_callsite = callsite
        self.guard_count += 1

    def stop_snapshot(self, reason, available_bytes):
        self.stop_event.set()
        # The guard's exact measured value is retained even if the OS changes
        # between that check and the synchronous diagnostic snapshot.
        with self.lock:
            value = self._snapshot()
            value.update(reason=reason, guard_available_bytes=available_bytes,
                         guard_floor_bytes=6 * 1024**3, kind='IMMEDIATE_GUARD_STOP')
            self._write(self.stop_path, value)

    def close(self):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
