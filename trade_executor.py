"""
trade_executor.py — A-5: Worker Pool for Order Execution.

Separates the fast scan loop from the slower order execution,
so that scanning 48 instruments is never blocked by a pending order.

Architecture:
  - Scan loop (fast, non-blocking) → pushes trade jobs to the queue
  - TradeExecutor (background thread) → pops jobs and executes orders
  - Max 3 concurrent orders towards Capital.com (respect rate-limit)

Usage:
    executor = TradeExecutor(capital_client, max_workers=3)
    executor.submit(job)  # Non-blocking, returns immediately
    executor.shutdown()   # Graceful shutdown
"""

import time
import threading
from queue import PriorityQueue, Empty
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from loguru import logger


@dataclass(order=True)
class TradeJob:
    """A trade order job with priority (lower = higher priority)."""
    priority: float                              # 1.0 - score (high score = low priority = executes first)
    timestamp: float = field(compare=False)      # submission time
    instrument: str = field(compare=False)
    direction: str = field(compare=False)
    size: float = field(compare=False)
    sl: float = field(compare=False)
    tp_levels: list = field(compare=False)
    callback: Optional[Callable] = field(default=None, compare=False, repr=False)
    metadata: dict = field(default_factory=dict, compare=False, repr=False)


class TradeExecutor:
    """
    A-5: Background thread pool for executing trades.
    
    Decouples scan (fast) from execution (IO-bound).
    """

    def __init__(self, capital_client, max_workers: int = 3, max_queue: int = 50):
        self._client = capital_client
        self._queue: PriorityQueue = PriorityQueue(maxsize=max_queue)
        self._max_workers = max_workers
        self._semaphore = threading.Semaphore(max_workers)
        self._running = True
        self._stats = {
            "submitted": 0,
            "executed": 0,
            "failed": 0,
            "rejected_full": 0,
        }

        # Start consumer thread
        self._thread = threading.Thread(target=self._consumer_loop, daemon=True, name="TradeExecutor")
        self._thread.start()
        logger.info(f"⚡ TradeExecutor started — max {max_workers} concurrent orders")

    def submit(self, job: TradeJob) -> bool:
        """
        Submit a trade job for execution. Non-blocking.
        Returns True if queued, False if queue is full.
        """
        if not self._running:
            return False

        try:
            self._queue.put_nowait(job)
            self._stats["submitted"] += 1
            logger.debug(
                f"📋 TradeExecutor queued: {job.instrument} {job.direction} "
                f"(priority={job.priority:.2f}, queue_size={self._queue.qsize()})"
            )
            return True
        except Exception:
            self._stats["rejected_full"] += 1
            logger.warning(
                f"⚠️ TradeExecutor queue full — {job.instrument} {job.direction} rejeté"
            )
            return False

    def _consumer_loop(self):
        """Main consumer loop — runs in background thread."""
        while self._running:
            try:
                job = self._queue.get(timeout=1)
            except Empty:
                continue

            # Acquire semaphore (wait for available slot)
            self._semaphore.acquire()
            # Execute in separate thread to allow concurrent orders
            t = threading.Thread(
                target=self._execute_job,
                args=(job,),
                daemon=True,
                name=f"TradeExec-{job.instrument}",
            )
            t.start()

    def _execute_job(self, job: TradeJob):
        """Execute a single trade job."""
        try:
            logger.info(
                f"🔥 TradeExecutor executing: {job.instrument} {job.direction} "
                f"size={job.size} sl={job.sl}"
            )

            results = []
            for i, tp in enumerate(job.tp_levels):
                deal_id = None

                # M41: Fast path — try pre-built flush (M43) or fast_market
                if i > 0 and hasattr(self, '_fast_exec') and self._fast_exec:
                    try:
                        # M43: check for pre-built order first
                        if hasattr(self, '_pre_builder') and self._pre_builder:
                            pb = self._pre_builder.consume(job.instrument)
                            if pb:
                                deal_ref = self._fast_exec.flush_pre_built(
                                    pb.body_bytes, pb.headers
                                )
                                if deal_ref:
                                    hdrs = self._client._headers()
                                    deal_id = self._fast_exec.fast_confirm_deal(
                                        deal_ref, hdrs
                                    )

                        # M41: Fast market order (direct TCP)
                        if deal_id is None:
                            hdrs = self._client._headers()
                            deal_ref = self._fast_exec.fast_market_order(
                                job.instrument, job.direction, job.size,
                                job.sl, tp, hdrs,
                            )
                            if deal_ref:
                                deal_id = self._fast_exec.fast_confirm_deal(
                                    deal_ref, hdrs
                                )
                    except Exception as _fast_e:
                        logger.debug(f"M41 fast path failed: {_fast_e}")

                # Standard path (first position = limit, fallback for fast fails)
                if deal_id is None:
                    if i == 0:
                        deal_id = self._client.place_limit_order(
                            job.instrument, job.direction, job.size, job.sl, tp,
                            timeout_s=15,
                        )
                    else:
                        deal_id = self._client.place_market_order(
                            job.instrument, job.direction, job.size, job.sl, tp,
                        )

                results.append(deal_id)
                if i < len(job.tp_levels) - 1:
                    time.sleep(0.3)

            # Call callback with results
            if job.callback:
                try:
                    job.callback(job, results)
                except Exception as cb_e:
                    logger.debug(f"TradeExecutor callback error: {cb_e}")

            if any(results):
                self._stats["executed"] += 1
                logger.info(f"✅ TradeExecutor done: {job.instrument} refs={results}")
            else:
                self._stats["failed"] += 1
                logger.warning(f"❌ TradeExecutor failed: {job.instrument} — all orders rejected")

        except Exception as e:
            self._stats["failed"] += 1
            logger.error(f"❌ TradeExecutor error {job.instrument}: {e}")
        finally:
            self._semaphore.release()
            self._queue.task_done()

    def shutdown(self, timeout: float = 10):
        """Graceful shutdown — waits for pending orders."""
        self._running = False
        logger.info("⏹️ TradeExecutor shutting down...")
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info(
            f"⏹️ TradeExecutor shutdown complete | "
            f"submitted={self._stats['submitted']} "
            f"executed={self._stats['executed']} "
            f"failed={self._stats['failed']}"
        )

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        return {**self._stats, "queue_size": self._queue.qsize()}
