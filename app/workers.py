"""线程池任务与轮询工具。

GUI 主线程只做界面渲染，所有 HTTP / 子进程操作都通过这里投递到线程池，
结果通过 Qt 信号回到主线程。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from common.logging_setup import get_logger

log = get_logger("app.workers")


class TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    done = Signal()


class Task(QRunnable):
    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict, label: str = ""):
        super().__init__()
        self.setAutoDelete(False)  # 生命周期由 TaskRunner 管理，保证信号不会中途销毁
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.label = label
        self.signals = TaskSignals()

    def run(self) -> None:  # pragma: no cover - 线程中执行
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            message = str(exc) or repr(exc)
            if self.label:
                log.warning("任务 [%s] 失败: %s", self.label, message)
            self.signals.failed.emit(message)
        else:
            if self.label:
                log.debug("任务 [%s] 完成", self.label)
            self.signals.finished.emit(result)
        finally:
            self.signals.done.emit()


class TaskRunner(QObject):
    """投递后台任务；``run_tracked`` 可保证同一 key 不会并发执行。"""

    def __init__(self, parent: Optional[QObject] = None, max_threads: int = 6):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, int(max_threads)))
        self._tasks: Dict[int, Task] = {}
        self._keys: Dict[str, int] = {}
        self._counter = 0

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_ok: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
        label: str = "",
        key: Optional[str] = None,
        **kwargs: Any
    ) -> bool:
        """投递任务；若 ``key`` 对应的任务仍在运行则直接返回 False。"""
        if key and key in self._keys:
            return False

        task = Task(fn, args, kwargs, label=label or (key or ""))
        self._counter += 1
        task_id = self._counter
        self._tasks[task_id] = task
        if key:
            self._keys[key] = task_id

        if on_ok is not None:
            task.signals.finished.connect(on_ok)
        if on_error is not None:
            task.signals.failed.connect(on_error)
        if on_done is not None:
            task.signals.done.connect(on_done)
        # 最后再清理引用，保证上面的回调都能拿到有效对象
        task.signals.done.connect(lambda: self._cleanup(task_id))

        self.pool.start(task)
        return True

    def _cleanup(self, task_id: int) -> None:
        task = self._tasks.pop(task_id, None)
        if task is None:
            return
        for key, value in list(self._keys.items()):
            if value == task_id:
                self._keys.pop(key, None)

    def is_busy(self, key: str) -> bool:
        return key in self._keys

    def busy_keys(self) -> list:
        return list(self._keys.keys())

    def shutdown(self) -> None:
        self._tasks.clear()
        self._keys.clear()
        try:
            self.pool.clear()
            # 池线程若在等待期间被销毁，Qt 同样会 failfast；HTTP 任务通常毫秒级，
            # 10 秒上限足够覆盖个别慢请求，避免线程还在跑时池对象先死
            self.pool.waitForDone(10000)
        except Exception:
            pass


class Poller(QObject):
    """周期任务：上一次执行完成后才开始计时下一次，避免任务堆积。"""

    tick = Signal(object)
    error = Signal(str)
    started = Signal()
    stopped = Signal()

    def __init__(
        self,
        runner: TaskRunner,
        fn: Callable[[], Any],
        interval_ms: int = 3000,
        label: str = "poll",
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.runner = runner
        self.fn = fn
        self.interval_ms = max(500, int(interval_ms))
        self.label = label
        self._timer = QTimer(self)
        self._timer.setInterval(self.interval_ms)
        self._timer.timeout.connect(self._fire)
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, immediate: bool = True) -> None:
        if self._running:
            return
        self._running = True
        self._timer.start()
        self.started.emit()
        if immediate:
            QTimer.singleShot(0, self._fire)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        self.stopped.emit()

    def set_interval(self, interval_ms: int) -> None:
        self.interval_ms = max(500, int(interval_ms))
        self._timer.setInterval(self.interval_ms)

    def _fire(self) -> None:
        if not self._running:
            return
        if self.runner.is_busy(self.label):
            return
        self.runner.run(
            self.fn,
            on_ok=self.tick.emit,
            on_error=self.error.emit,
            label=self.label,
            key=self.label,
        )


__all__ = ["Poller", "Task", "TaskRunner", "TaskSignals"]
