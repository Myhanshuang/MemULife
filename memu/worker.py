"""Bounded concurrent write tasks, serial maintenance and event-driven scheduling."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time
from zoneinfo import ZoneInfo


class MaintenanceWorker:
    def __init__(self, service):
        self.service = service
        self.stopped = threading.Event()
        self.capacity = service.config.models.mllm.concurrency + service.config.models.embedding.concurrency
        self.pool = ThreadPoolExecutor(max_workers=self.capacity, thread_name_prefix='memu-model-task')
        self.index_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='memu-index')
        self.index_future = None
        self.futures = {}
        self.thread = threading.Thread(target=self._run, name="memu-maintenance", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopped.set()
        self.service.changed.set()
        # Provider calls have explicit timeouts; finish the current transaction before closing clients.
        self.thread.join()
        self.pool.shutdown(wait=True)
        self.index_pool.shutdown(wait=True)
        return True

    def _run(self):
        while not self.stopped.is_set():
            self.service.changed.clear()
            try:
                for key, future in list(self.futures.items()):
                    if future.done():
                        del self.futures[key]
                        future.result()
                if self.index_future is not None and self.index_future.done():
                    try:
                        self.index_future.result()
                    except Exception:
                        pass  # sync_index persists and logs its retry deadline.
                    self.index_future = None
                priority_deadline = self.service.source_priority_deadline()
                with self.service.repo.lock():
                    dirty = self.service.repo.state.get('index_dirty', False)
                    retry = self.service.repo.state.get('index_retry_at')
                if (priority_deadline is None and dirty and self.index_future is None and
                        (retry is None or retry <= time.time()) and not self.stopped.is_set()):
                    self.index_future = self.index_pool.submit(self.service.sync_index)
                    self.index_future.add_done_callback(lambda _: self.service.changed.set())
                if priority_deadline is None and len(self.futures) < self.capacity:
                    keys = self.service.prepare_writes(limit=self.capacity - len(self.futures),
                                                       exclude=self.futures, defer_index=True)
                    for key in keys:
                        if self.stopped.is_set():
                            break
                        future = self.pool.submit(self.service.process_write, key, defer_index=True)
                        self.futures[key] = future
                        future.add_done_callback(lambda _: self.service.changed.set())
                if self.stopped.is_set():
                    break
                if priority_deadline is not None:
                    self.service.changed.wait(max(0.05, priority_deadline - time.time()))
                    continue
                if not self.futures and self.index_future is None and not dirty:
                    self.service.schedule_daily()
                    self.service.refresh_due()
            except Exception as error:
                from .events import event
                from .retry import error_code
                event('maintenance_deferred', error_code=error_code(error))
                self.service.changed.wait(150)
                continue
            with self.service.repo.lock():
                due = []
                retry = self.service.repo.state.get('index_retry_at')
                if not self.futures and self.index_future is None and retry is None:
                    due = [job["due"] for job in self.service.repo.state["pending_views"].values()
                           if job["state"] == "queued" and self.service._dependencies_ready(job)]
                    due += [job['retry_at'] for job in self.service.repo.state['pending_views'].values()
                            if job['state'] == 'waiting' and self.service._dependencies_ready(job)]
                    discovery = self.service.repo.state['topic_registry']['job']
                    if discovery['state'] == 'queued':
                        due.append(discovery['due'])
                    elif discovery['state'] == 'waiting':
                        due.append(discovery['retry_at'])
                if retry is not None and self.index_future is None:
                    due.append(retry)
                replacements = self.service.repo.state.get('projection_updates', {})
                if retry is None and len(self.futures) < self.capacity and not (self.futures and replacements):
                    if hasattr(self.service.repo, 'next_write_due'):
                        write_due = self.service.repo.next_write_due(exclude=self.futures)
                        if write_due is not None:
                            due.append(write_due)
                    else:
                        for key, request in self.service.repo.state['writes'].items():
                            if key not in self.futures:
                                if request['state'] in {'queued', 'running'}:
                                    due.append(0)
                                elif request['state'] == 'waiting':
                                    due.append(request['retry_at'])
            now = datetime.now(ZoneInfo(self.service.config.repository.timezone))
            hour, minute = map(int, self.service.config.views.daily_at.split(":"))
            tomorrow = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if tomorrow <= now:
                tomorrow += timedelta(days=1)
            deadline = min(due + [tomorrow.timestamp()])
            self.service.changed.wait(max(0.05, deadline - time.time()))
