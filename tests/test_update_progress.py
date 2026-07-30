from __future__ import annotations
import os, unittest
from pathlib import Path
from unittest import mock
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from app import gui, update_client
from app.update_client import UpdateInfo
from app.update_progress import UpdateController, UpdateState

def spin_until(predicate, timeout_ms=3000):
    loop = QEventLoop(); timer = QTimer(); timer.setInterval(5)
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    timer.start(); QTimer.singleShot(timeout_ms, loop.quit); loop.exec(); timer.stop()
    return predicate()

class UpdateStateMachineIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.parent = QWidget(); self.info = UpdateInfo("1.5.14", 44, "key", "setup.exe", 10, sha256="a"*64)
        self.app.setProperty(gui.UPDATE_SHUTDOWN_COMMITTED_PROPERTY, False)
    def tearDown(self): self.parent.deleteLater(); self.app.processEvents()
    def controller(self, preflight=lambda: True):
        c = UpdateController(self.info, Path("updates"), Path("app.exe"), self.parent, preflight, True)
        self.parent._update_controller = c; return c
    def test_progress_100_and_download_result_do_not_close_or_accept(self):
        c=self.controller(); d=c.progress_dialog; d.show(); d.show_progress(10,10)
        with mock.patch.object(d,"close",wraps=d.close) as close, mock.patch.object(d,"accept",wraps=d.accept) as accept:
            c._save_installer_result("setup.exe"); self.app.processEvents()
            close.assert_not_called(); accept.assert_not_called(); self.assertTrue(d.isVisible())
    def test_dialog_does_not_own_controller_and_controller_survives_dialog_deletion(self):
        c=self.controller(); d=c.progress_dialog
        self.assertIs(c.parent(),self.parent); self.assertIsNot(d.parent(),c)
        d.setParent(None); d.deleteLater(); self.app.sendPostedEvents(None,0)
        self.assertIs(self.parent._update_controller,c)
    def test_success_path_exact_states_no_dialog_action_and_quits_last(self):
        c=self.controller(); states=[]; c.state_changed.connect(lambda _o,n:states.append(n)); d=c.progress_dialog
        def prepare(_i,_d,progress_callback,stage_callback,cancel_check):
            progress_callback(10,10)
            for s,m in (("verify_file","size"),("verify_sha256","sha"),("verify_pe","pe"),("installer_ready","publish")): stage_callback(s,m)
            return Path("setup.exe")
        with mock.patch.object(update_client,"prepare_installer",side_effect=prepare), mock.patch.object(update_client,"start_installer_for_update",return_value=123), mock.patch.object(gui,"quit_app_for_update") as quit_app, mock.patch.object(d,"close",wraps=d.close) as close, mock.patch.object(d,"accept",wraps=d.accept) as accept, mock.patch.object(d,"reject",wraps=d.reject) as reject, mock.patch.object(d,"hide",wraps=d.hide) as hide, mock.patch.object(d,"deleteLater",wraps=d.deleteLater) as delete:
            c.start(); self.assertTrue(spin_until(lambda:c.state==UpdateState.SHUTDOWN_COMMITTED)); quit_app.assert_called_once_with()
            for action in (close,accept,reject,hide,delete): action.assert_not_called()
        self.assertEqual(states,[s.value for s in (UpdateState.DOWNLOADING,UpdateState.VERIFYING_SIZE,UpdateState.VERIFYING_SHA256,UpdateState.VERIFYING_PE,UpdateState.PUBLISHING_INSTALLER,UpdateState.WAITING_DOWNLOAD_THREAD_FINISHED,UpdateState.LAUNCHING_INSTALLER,UpdateState.WAITING_INSTALLER_CONFIRMATION,UpdateState.SHUTDOWN_COMMITTED)])
        self.assertEqual(c.active_thread_count,0)
    def test_failure_keeps_app_and_enables_close_after_thread_finished(self):
        c=self.controller()
        with mock.patch.object(update_client,"prepare_installer",side_effect=RuntimeError("bad")), mock.patch.object(gui,"quit_app_for_update") as quit_app:
            c.start(); self.assertTrue(spin_until(lambda:c.state==UpdateState.FAILED))
        quit_app.assert_not_called(); self.assertEqual(c.active_thread_count,0)
        self.assertEqual(c.progress_dialog.cancel_button.text(),"閉じる"); self.assertTrue(c.progress_dialog.cancel_button.isEnabled())
    def test_quit_not_called_before_installer_confirmation(self):
        c=self.controller()
        with mock.patch.object(gui,"quit_app_for_update") as quit_app:
            c._transition(UpdateState.LAUNCHING_INSTALLER); quit_app.assert_not_called()
            c._transition(UpdateState.WAITING_INSTALLER_CONFIRMATION); quit_app.assert_not_called()
if __name__=="__main__": unittest.main()
