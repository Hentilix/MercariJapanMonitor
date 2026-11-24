"""APScheduler 3.11 wiring.

Phase 5.3-B: one APScheduler job per monitor. This module only decides
WHEN to call each monitor's scan function — it knows nothing about
Mercari / DeepSeek / SMTP / filtering.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Monitor

log = logging.getLogger(__name__)


class ScanStatus(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    ALREADY_RUNNING = "already_running"
    INVALIDATED = "invalidated"
    STOPPING = "stopping"


@dataclass(frozen=True)
class ScanOutcome:
    status: ScanStatus
    summary: str | None = None


class MonitorScheduler:
    """One APScheduler job per monitor (Phase 5.3-B).

    scan_func returns the saved summary, or None if the result was invalidated.

    * schedule(monitor): create / reschedule / remove the job to match the
      monitor's enabled flag and interval_minutes. Job id: "monitor-{id}".
    * start(monitors): register all jobs, start the scheduler, and scan
      every enabled monitor once immediately (fire-and-forget tasks).
    * max_instances=1 + coalesce=True: the same monitor never runs two
      scans at once; a tick arriving while the previous scan is still
      running is SKIPPED (no queued catch-up scan).
    * Different monitors run independently — no global lock.
    * A failed scan is logged; other monitors and later ticks are unaffected.
    * stop() pauses, waits for in-flight scans, then shuts down.
    """

    def __init__(self, scan_func: Callable[[int], Awaitable[str | None]]) -> None:
        self._scan_func = scan_func
        self._tasks: dict[int, asyncio.Task[ScanOutcome]] = {}
        self._stopping = False
        self._scheduler = AsyncIOScheduler()

    def job_id(self, monitor_id: int) -> str:
        return f"monitor-{monitor_id}"

    def request_scan(self, monitor_id: int) -> asyncio.Task[ScanOutcome] | None:
        """Register an accepted scan before yielding to the event loop."""
        if self._stopping or self.is_running(monitor_id):
            return None
        task = asyncio.create_task(self._scan(monitor_id))
        self._tasks[monitor_id] = task
        task.add_done_callback(lambda completed: self._forget_scan(monitor_id, completed))
        return task

    def _forget_scan(self, monitor_id: int, task: asyncio.Task[ScanOutcome]) -> None:
        if self._tasks.get(monitor_id) is task:
            del self._tasks[monitor_id]

    async def run_monitor_now(self, monitor_id: int) -> ScanOutcome:
        if self._stopping:
            return ScanOutcome(ScanStatus.STOPPING)
        task = self.request_scan(monitor_id)
        if task is None:
            log.info("Monitor %s 本次扫描跳过：上一次扫描尚未结束", monitor_id)
            return ScanOutcome(ScanStatus.ALREADY_RUNNING)
        return await asyncio.shield(task)

    async def _scan(self, monitor_id: int) -> ScanOutcome:
        log.info("Monitor %s 开始扫描", monitor_id)
        try:
            summary = await self._scan_func(monitor_id)
            if summary is None:
                log.info("Monitor %s 本次扫描无可用结果", monitor_id)
                return ScanOutcome(ScanStatus.INVALIDATED)
            log.info("Monitor %s 扫描完成", monitor_id)
            return ScanOutcome(ScanStatus.COMPLETED, summary)
        except Exception as exc:
            log.exception("Monitor %s 扫描失败: %s", monitor_id, exc)
            return ScanOutcome(ScanStatus.FAILED)

    def is_running(self, monitor_id: int) -> bool:
        task = self._tasks.get(monitor_id)
        return task is not None and not task.done()

    def schedule(self, monitor: Monitor) -> None:
        """Create, reschedule, or remove the monitor's job from its config."""
        job_id = self.job_id(monitor.id)
        existing = self._scheduler.get_job(job_id)
        if not monitor.enabled:
            if existing is not None:
                self._scheduler.remove_job(job_id)
                log.info(
                    "Monitor %s (%s) 已从调度移除（已禁用）",
                    monitor.id,
                    monitor.name,
                )
            return
        trigger = IntervalTrigger(minutes=monitor.interval_minutes)
        if existing is not None:
            self._scheduler.reschedule_job(job_id, trigger=trigger)
            log.info(
                "Monitor %s (%s) 已重新调度：每 %d 分钟",
                monitor.id,
                monitor.name,
                monitor.interval_minutes,
            )
        else:
            self._scheduler.add_job(
                self.run_monitor_now,
                trigger,
                args=[monitor.id],
                id=job_id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
            )
            log.info(
                "Monitor %s (%s) 已加入调度：每 %d 分钟",
                monitor.id,
                monitor.name,
                monitor.interval_minutes,
            )

    def remove_monitor(self, monitor_id: int) -> None:
        """Remove the monitor's job. Other monitors are unaffected."""
        job_id = self.job_id(monitor_id)
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)
            log.info("Monitor %s 已从调度移除", monitor_id)

    def disable_monitor(self, monitor_id: int) -> None:
        """Stop scheduling. History/ignored data in the DB is untouched."""
        self.remove_monitor(monitor_id)

    async def enable_monitor(self, monitor: Monitor) -> None:
        """(Re)schedule the job and immediately scan once."""
        self.schedule(monitor)
        log.info("Monitor %s (%s) 已启用：立即扫描", monitor.id, monitor.name)
        await self.run_monitor_now(monitor.id)

    def reload_monitors(self, monitors: list[Monitor]) -> None:
        """Make the job set exactly match the given monitors. Idempotent:
        calling it repeatedly never creates duplicate jobs."""
        valid_ids = {monitor.id for monitor in monitors}
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("monitor-"):
                try:
                    job_monitor_id = int(job.id.split("-", 1)[1])
                except ValueError:
                    continue
                if job_monitor_id not in valid_ids:
                    self._scheduler.remove_job(job.id)
        for monitor in monitors:
            self.schedule(monitor)

    def start(self, monitors: list[Monitor] | None = None) -> None:
        """Start the scheduler. Must be called inside a running event loop.

        When `monitors` is given: register their jobs first, then scan
        every enabled monitor once immediately (fire-and-forget tasks).
        Disabled monitors get no job and no scan.
        """
        if monitors is not None:
            self.reload_monitors(monitors)
        self._scheduler.start()
        self._stopping = False
        if monitors is not None:
            for monitor in monitors:
                if monitor.enabled:
                    log.info(
                        "Monitor %s (%s) 已启动：立即扫描 + 每 %d 分钟",
                        monitor.id,
                        monitor.name,
                        monitor.interval_minutes,
                    )
                    self.request_scan(monitor.id)

    async def stop(self) -> None:
        """Pause, wait for in-flight scans, then shut down.

        Safe to call even if the scheduler was never started (APScheduler's
        pause()/shutdown() raise SchedulerNotRunningError in that state).
        """
        self._stopping = True
        if self._scheduler.running:
            self._scheduler.pause()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
