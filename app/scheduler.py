"""APScheduler 3.11 wiring.

Phase 5.3-B: one APScheduler job per monitor. This module only decides
WHEN to call each monitor's scan function — it knows nothing about
Mercari / DeepSeek / SMTP / filtering.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import Monitor

log = logging.getLogger(__name__)


class ScanScheduler:
    """Legacy single-job scheduler (Phase 3). Kept for old tests/scripts.

    * max_instances=1 + coalesce=True: a tick never starts while the
      previous run is still going; missed ticks collapse into one run.
    * A failed job is logged and the next tick runs as usual — one bad
      scan never stops the monitor.
    * stop() pauses scheduling first, waits for the in-flight job to
      finish, then shuts the scheduler down.
      (APScheduler 3.11.3's shutdown(wait=True) cancels running jobs
      instead of waiting, so the wait is done here explicitly.)
    """

    def __init__(self, job_func, trigger: BaseTrigger) -> None:
        self._job_func = job_func
        self._running: asyncio.Task | None = None
        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            self._run,
            trigger=trigger,
            id="mercari_scan",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    async def _run(self) -> None:
        self._running = asyncio.current_task()
        try:
            await self._job_func()
        except Exception:
            log.exception("Scheduled scan failed")
        finally:
            self._running = None

    def start(self) -> None:
        """Start the scheduler. Must be called inside a running event loop."""
        self._scheduler.start()

    async def stop(self) -> None:
        """Pause, wait for the in-flight job, then shut down."""
        self._scheduler.pause()
        running = self._running
        if running is not None and not running.done():
            log.info("Waiting for the running scan to finish...")
            await running
        self._scheduler.shutdown(wait=False)


class MonitorScheduler:
    """One APScheduler job per monitor (Phase 5.3-B).

    scan_func: async callable(monitor_id) -> None

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

    def __init__(self, scan_func) -> None:
        self._scan_func = scan_func
        self._tasks: dict[int, asyncio.Task] = {}
        self._scheduler = AsyncIOScheduler()

    def job_id(self, monitor_id: int) -> str:
        return f"monitor-{monitor_id}"

    async def _run(self, monitor_id: int) -> None:
        if monitor_id in self._tasks:
            log.info("Monitor %s 本次扫描跳过：上一次扫描尚未结束", monitor_id)
            return
        self._tasks[monitor_id] = asyncio.current_task()
        log.info("Monitor %s 开始扫描", monitor_id)
        try:
            await self._scan_func(monitor_id)
            log.info("Monitor %s 扫描完成", monitor_id)
        except Exception as exc:
            log.exception("Monitor %s 扫描失败: %s", monitor_id, exc)
        finally:
            self._tasks.pop(monitor_id, None)

    def is_running(self, monitor_id: int) -> bool:
        return monitor_id in self._tasks

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
                self._run,
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
        if monitors is not None:
            for monitor in monitors:
                if monitor.enabled:
                    log.info(
                        "Monitor %s (%s) 已启动：立即扫描 + 每 %d 分钟",
                        monitor.id,
                        monitor.name,
                        monitor.interval_minutes,
                    )
                    asyncio.create_task(self.run_monitor_now(monitor.id))

    async def run_monitor_now(self, monitor_id: int) -> bool:
        """Run one scan immediately. False when one is already running."""
        if self.is_running(monitor_id):
            log.info("Monitor %s 本次扫描跳过：上一次扫描尚未结束", monitor_id)
            return False
        await self._run(monitor_id)
        return True

    async def stop(self) -> None:
        """Pause, wait for in-flight scans, then shut down.

        Safe to call even if the scheduler was never started (APScheduler's
        pause()/shutdown() raise SchedulerNotRunningError in that state).
        """
        if self._scheduler.running:
            self._scheduler.pause()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
