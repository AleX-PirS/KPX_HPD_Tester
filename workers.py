from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str, str)
    finished = pyqtSignal()


class FunctionWorker(QRunnable):
    """Run one blocking hardware call outside the GUI thread."""

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as error:
            self.signals.error.emit(str(error), traceback.format_exc())
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class HardwareTaskRunner:
    """Serialize hardware operations while keeping the UI responsive.

    A single worker thread is intentional: MGPDLab and VISA instruments are
    synchronous resources, and serializing access prevents overlapping commands
    from different GUI actions. Existing non-GUI test scripts remain unaffected.
    """

    def __init__(self):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)

    def submit(
        self,
        fn: Callable[..., Any],
        *args,
        on_result=None,
        on_error=None,
        on_finished=None,
        **kwargs,
    ) -> FunctionWorker:
        worker = FunctionWorker(fn, *args, **kwargs)

        if on_result is not None:
            worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        if on_finished is not None:
            worker.signals.finished.connect(on_finished)

        self.pool.start(worker)
        return worker
