from __future__ import annotations
import os, unittest
from pathlib import Path
from unittest import mock
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QApplication,QWidget
from app import update_client
from app.update_client import UpdateInfo
from app.update_progress import UpdateController,UpdateState,_ACTIVE_UPDATE_THREADS
class ThreadBoundaryIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def test_finished_starts_installer_with_controller_retained_and_ends_zero_threads(self):
        parent=QWidget(); info=UpdateInfo("1.5.14",44,"key","setup.exe",10,sha256="a"*64)
        c=UpdateController(info,Path("updates"),Path("app.exe"),parent); parent._update_controller=c
        started=[]; original=c._start_installer_worker
        c._start_installer_worker=lambda:(started.append(parent._update_controller is c),original())[1]
        with mock.patch.object(update_client,"prepare_installer",return_value=Path("setup.exe")), mock.patch.object(update_client,"start_installer_for_update",side_effect=RuntimeError("stop")):
            c.start(); loop=QEventLoop(); c.finished.connect(loop.quit); QTimer.singleShot(3000,loop.quit); loop.exec()
        self.assertEqual(started,[True]); self.assertEqual(c.state,UpdateState.FAILED)
        self.assertEqual(c.active_thread_count,0); self.assertFalse(_ACTIVE_UPDATE_THREADS)
        parent.deleteLater(); self.app.processEvents()
if __name__=="__main__": unittest.main()
