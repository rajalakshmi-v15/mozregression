from PyQt4.QtCore import QObject, QThread, pyqtSignal as Signal, \
    pyqtSlot as Slot, QTimer

from mozregui.global_prefs import get_prefs, apply_prefs
from mozregression.download_manager import BuildDownloadManager
from mozregression.test_runner import TestRunner
from mozregression.network import get_http_session
from mozregression.persist_limit import PersistLimit
from mozregression.errors import LauncherError


class GuiBuildDownloadManager(QObject, BuildDownloadManager):
    download_progress = Signal(object, int, int)
    download_started = Signal(object)
    download_finished = Signal(object, str)

    def __init__(self, destdir, persist_limit, **kwargs):
        QObject.__init__(self)
        persist_limit = PersistLimit(persist_limit)
        BuildDownloadManager.__init__(self, destdir,
                                      session=get_http_session(),
                                      persist_limit=persist_limit,
                                      **kwargs)

    def _download_started(self, task):
        self.download_started.emit(task)
        BuildDownloadManager._download_started(self, task)

    def _download_finished(self, task):
        try:
            self.download_finished.emit(task, task.get_dest())
        except RuntimeError:
            # in some cases, closing the application may destroy the
            # underlying c++ QObject, causing this signal to fail.
            # Skip this silently.
            pass
        BuildDownloadManager._download_finished(self, task)

    def focus_download(self, build_info):
        build_url, fname = self._extract_download_info(build_info)
        dest = self.get_dest(fname)
        build_info.build_file = dest
        # first, stop all downloads in background (except the one for this
        # build if any)
        self.cancel(cancel_if=lambda dl: dest != dl.get_dest())

        dl = self.download(build_url, fname)
        if dl:
            dl.set_progress(self.download_progress.emit)
        else:
            # file already downloaded.
            # emit the finished signal so bisection goes on
            self.download_finished.emit(None, dest)


class GuiTestRunner(QObject, TestRunner):
    evaluate_started = Signal(str)
    evaluate_finished = Signal()

    def __init__(self):
        QObject.__init__(self)
        TestRunner.__init__(self)
        self.verdict = None
        self.launcher = None
        self.launcher_kwargs = {}

    def evaluate(self, build_info, allow_back=False):
        try:
            self.launcher = self.create_launcher(build_info)
            self.launcher.start(**self.launcher_kwargs)
            build_info.update_from_app_info(self.launcher.get_app_info())
        except LauncherError, exc:
            self.evaluate_started.emit(str(exc))
        else:
            self.evaluate_started.emit('')

    def finish(self, verdict):
        if self.launcher:
            try:
                self.launcher.stop()
            except LauncherError:
                pass  # silently pass stop process error
            self.launcher.cleanup()
        self.verdict = verdict
        self.evaluate_finished.emit()


class AbstractBuildRunner(QObject):
    """
    Base class to run a build.

    Create the required test runner and build manager, along with a thread
    that should be used for blocking tasks.
    """
    running_state_changed = Signal(bool)
    worker_created = Signal(object)
    worker_class = None

    def __init__(self, mainwindow):
        QObject.__init__(self)
        self.mainwindow = mainwindow
        self.thread = None
        self.worker = None
        self.pending_threads = []
        self.test_runner = GuiTestRunner()
        self.download_manager = None

    def init_worker(self, fetch_config, options):
        """
        Create and initialize the worker.

        Should be subclassed to configure the worker, and should return the
        worker method that should start the work.
        """
        self.stop()

        # global preferences
        global_prefs = get_prefs()
        # apply the global prefs now
        apply_prefs(global_prefs)

        download_dir = global_prefs['persist']
        if not download_dir:
            download_dir = self.mainwindow.persist
        persist_limit = int(abs(global_prefs['persist_size_limit'])
                            * 1073741824)
        self.download_manager = GuiBuildDownloadManager(download_dir,
                                                        persist_limit)
        self.thread = QThread()

        # options for the app launcher
        launcher_kwargs = {}
        for name in ('profile', 'preferences'):
            if name in options:
                value = options[name]
                if value:
                    launcher_kwargs[name] = value

        # add add-ons paths to the app launcher
        launcher_kwargs['addons'] = options['addons']
        self.test_runner.launcher_kwargs = launcher_kwargs

        self.worker = self.worker_class(fetch_config, self.test_runner,
                                        self.download_manager)
        # Move self.bisector in the thread. This will
        # allow to the self.bisector slots (connected after the move)
        # to be automatically called in the thread.
        self.worker.moveToThread(self.thread)
        self.worker_created.emit(self.worker)

    def start(self, fetch_config, options):
        action = self.init_worker(fetch_config, options)
        assert callable(action), "%s should be callable" % action
        self.thread.start()
        # this will be called in the worker thread.
        QTimer.singleShot(0, action)
        self.running_state_changed.emit(True)

    @Slot()
    def stop(self, wait=True):
        self.test_runner.finish(None)
        if self.download_manager:
            self.download_manager.cancel()
        if self.thread:
            self.thread.quit()
            if wait:
                # wait for thread(s) completion - this is the case when
                # user close the application
                self.thread.wait()
                for thread in self.pending_threads:
                    thread.wait()
            else:
                # do not block, just keep track of the thread - we got here
                # when user uses the stop button.
                self.pending_threads.append(self.thread)
                self.thread.finished.connect(self._remove_pending_thread)
            self.thread = None
        self.running_state_changed.emit(False)

    @Slot()
    def _remove_pending_thread(self):
        for thread in self.pending_threads[:]:
            if thread.isFinished():
                self.pending_threads.remove(thread)
