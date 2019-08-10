import os
import sys
from pathlib import Path
from multiprocessing.pool import ThreadPool
from queue import Queue, Empty
from functools import partial
import logging
import colorsys
import traceback
import threading
from datetime import datetime
# pylint: disable=no-name-in-module
from PyQt5.QtCore import Qt, QTimer, qInstallMessageHandler, QObject, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QTabWidget, QTextEdit, QPushButton, QLabel, QScrollArea, QFrame, QProgressBar,
                             QVBoxLayout, QShortcut, QGridLayout, QApplication, QMainWindow, QSizePolicy)
from PyQt5.QtGui import QPalette, QColor, QIcon, QKeySequence, QTextCursor, QPainter
# pylint: enable=no-name-in-module

from circleguard import Circleguard, set_options, loader
from circleguard import __version__ as cg_version
from visualizer import VisualizerWindow
from utils import resource_path, write_log, MapRun, ScreenRun, LocalRun, VerifyRun
from widgets import (Threshold, set_event_window, InputWidget, ResetSettings, WidgetCombiner,
                     FolderChooser, IdWidgetCombined, Separator, OptionWidget, ButtonWidget,
                     CompareTopPlays, CompareTopUsers, LoglevelWidget, SliderBoxSetting,
                     TopPlays, BeatmapTest, StringFormatWidget, ComparisonResult, LineEditSetting,
                     RunWidget)

from settings import get_setting, update_default
import wizard
from version import __version__

log = logging.getLogger(__name__)


# save old excepthook
sys._excepthook = sys.excepthook

# this allows us to log any and all exceptions thrown to a log file -
# pyqt likes to eat exceptions and quit silently
def my_excepthook(exctype, value, tb):
    # call original excepthook before ours
    write_log("sys.excepthook error\n"
              "Type: " + str(value) + "\n"
              "Value: " + str(value) + "\n"
              "Traceback: " + "".join(traceback.format_tb(tb)) + '\n')
    sys._excepthook(exctype, value, tb)

sys.excepthook = my_excepthook

# sys.excepthook doesn't persist across threads
# (http://bugs.python.org/issue1230540). This is a hacky workaround that overrides
# the threading init method to use our excepthook.
# https://stackoverflow.com/a/31622038
threading_init = threading.Thread.__init__
def init(self, *args, **kwargs):
    threading_init(self, *args, **kwargs)
    run_original = self.run

    def run_with_except_hook(*args2, **kwargs2):
        try:
            run_original(*args2, **kwargs2)
        except Exception:
            sys.excepthook(*sys.exc_info())
    self.run = run_with_except_hook
threading.Thread.__init__ = init


# logging methodology heavily adapted from https://stackoverflow.com/questions/28655198/best-way-to-display-logs-in-pyqt
class Handler(QObject, logging.Handler):
    new_message = pyqtSignal(object)

    def __init__(self):
        super().__init__()

    def emit(self, record):
        message = self.format(record)
        self.new_message.emit(message)


class WindowWrapper(QMainWindow):
    def __init__(self, clipboard):
        super().__init__()

        self.clipboard = clipboard
        self.progressbar = QProgressBar()
        self.progressbar.setFixedWidth(250)
        self.current_state_label = QLabel("Idle")
        # statusBar() is a qt function that will create a status bar tied to the window
        # if it doesnt exist, and access the existing one if it does.
        self.statusBar().addWidget(WidgetCombiner(self.progressbar, self.current_state_label))
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setContentsMargins(8, 2, 10, 3)

        self.main_window = MainWindow()
        self.main_window.main_tab.reset_progressbar_signal.connect(self.reset_progressbar)
        self.main_window.main_tab.increment_progressbar_signal.connect(self.increment_progressbar)
        self.main_window.main_tab.update_label_signal.connect(self.update_label)
        self.main_window.main_tab.add_comparison_result_signal.connect(self.add_comparison_result)
        self.main_window.main_tab.write_to_terminal_signal.connect(self.main_window.main_tab.write)
        self.main_window.main_tab.add_run_to_queue_signal.connect(self.add_run_to_queue)
        self.main_window.main_tab.update_run_status_signal.connect(self.update_run_status)
        self.main_window.queue_tab.cancel_run_signal.connect(self.cancel_run)

        self.setCentralWidget(self.main_window)
        QShortcut(QKeySequence(Qt.CTRL+Qt.Key_Right), self, self.tab_right)
        QShortcut(QKeySequence(Qt.CTRL+Qt.Key_Left), self, self.tab_left)
        QShortcut(QKeySequence(Qt.CTRL+Qt.Key_Q), self, lambda: sys.exit(1))

        self.setWindowTitle(f"Circleguard (Backend v{cg_version} / Frontend v{__version__})")
        self.setWindowIcon(QIcon(str(resource_path("resources/logo.ico"))))
        self.start_timer()
        self.debug_window = None

        handler = Handler()
        logging.getLogger("circleguard").addHandler(handler)
        logging.getLogger("ossapi").addHandler(handler)
        logging.getLogger(__name__).addHandler(handler)
        formatter = logging.Formatter("[%(levelname)s] %(asctime)s  %(message)s ", datefmt="%Y/%m/%d %I:%M:%S %p")
        handler.setFormatter(formatter)
        handler.new_message.connect(self.log)

    # I know, I know...we have a stupid amount of layers.
    # WindowWrapper -> MainWindow -> MainTab -> Tabs
    def tab_right(self):
        tabs = self.main_window.main_tab.tabs
        tabs.setCurrentIndex(tabs.currentIndex() + 1)

    def tab_left(self):
        tabs = self.main_window.main_tab.tabs
        tabs.setCurrentIndex(tabs.currentIndex() - 1)

    def mousePressEvent(self, event):
        focused = self.focusWidget()
        if focused is not None:
            focused.clearFocus()
        super(WindowWrapper, self).mousePressEvent(event)

    def start_timer(self):
        timer = QTimer(self)
        timer.timeout.connect(self.run_timer)
        timer.start(250)

    def run_timer(self):
        """
        check for stderr messages (because logging prints to stderr not stdout, and
        it's nice to have stdout reserved) and then print cg results
        """
        self.main_window.main_tab.print_results()
        self.main_window.main_tab.check_circleguard_queue()

    def log(self, message):
        """
        Message is the string message sent to the io stream
        """

        if get_setting("log_save"):
            write_log(message)

        # TERMINAL / BOTH
        if get_setting("log_output") in [1, 3]:
            self.main_window.main_tab.write(message)

        # NEW WINDOW / BOTH
        if get_setting("log_output") in [2, 3]:
            if self.debug_window and self.debug_window.isVisible():
                self.debug_window.write(message)
            else:
                self.debug_window = DebugWindow()
                self.debug_window.show()
                self.debug_window.write(message)

    def update_label(self, text):
        self.current_state_label.setText(text)

    def increment_progressbar(self, increment):
        self.progressbar.setValue(self.progressbar.value() + increment)

    def reset_progressbar(self, max_value):
        if not max_value == -1:
            self.progressbar.setValue(0)
            self.progressbar.setRange(0, max_value)
        else:
            self.progressbar.setRange(0, 100)
            self.progressbar.reset()

    def add_comparison_result(self, result):
        # this right here could very well lead to some memory issues. I tried to avoid
        # leaving a reference to the replays in this method, but it's quite possible
        # things are still not very clean. Ideally only ComparisonResult would have a
        # reference to the two replays, and result should have no references left.
        r1 = result.replay1
        r2 = result.replay2
        timestamp = datetime.now()
        text = get_setting("string_result_text").format(ts=timestamp, similarity=result.similarity,
                                                        r=result, r1=r1, r2=r2)
        result_widget = ComparisonResult(text, result, r1, r2)
        # set button signal connections (visualize and copy template to clipboard)
        result_widget.button.clicked.connect(partial(self.main_window.main_tab.visualize, result_widget.replay1, result_widget.replay2))
        result_widget.button_clipboard.clicked.connect(partial(self.copy_to_clipboard,
                get_setting("template_replay_steal").format(r=result_widget.result, r1=result_widget.replay1, r2=result_widget.replay2)))
        # remove info text if shown
        if not self.main_window.results_tab.results.info_label.isHidden():
            self.main_window.results_tab.results.info_label.hide()
        self.main_window.results_tab.results.layout.addWidget(result_widget)

    def copy_to_clipboard(self, text):
        self.clipboard.setText(text)

    def add_run_to_queue(self, run):
        self.main_window.queue_tab.add_run(run)

    def update_run_status(self, run_id, status):
        self.main_window.queue_tab.update_status(run_id, status)

    def cancel_run(self, run_id):
        self.main_window.main_tab.runs[run_id].event.set()

class DebugWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Debug Output")
        self.setWindowIcon(QIcon(str(resource_path("resources/logo.ico"))))
        terminal = QTextEdit()
        terminal.setReadOnly(True)
        terminal.ensureCursorVisible()
        self.terminal = terminal
        self.setCentralWidget(self.terminal)
        self.resize(800, 350)

    def write(self, message):
        self.terminal.append(message)
        self.scroll_to_bottom()

    def scroll_to_bottom(self):
        cursor = QTextCursor(self.terminal.document())
        cursor.movePosition(QTextCursor.End)
        self.terminal.setTextCursor(cursor)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.tab_widget = QTabWidget()
        self.main_tab = MainTab()
        self.results_tab = ResultsTab()
        self.queue_tab = QueueTab()
        self.settings_tab = SettingsTab()
        self.tab_widget.addTab(self.main_tab, "Main Tab")
        self.tab_widget.addTab(self.results_tab, "Results")
        self.tab_widget.addTab(self.queue_tab, "Queue")
        self.tab_widget.addTab(self.settings_tab, "Settings")
        # so when we switch from settings tab to main tab, whatever tab we're on gets changed if we delete our api key
        self.tab_widget.currentChanged.connect(self.main_tab.switch_run_button)

        self.main_layout = QVBoxLayout()
        self.main_layout.addWidget(self.tab_widget)
        self.main_layout.setContentsMargins(0, 10, 0, 0)
        self.setLayout(self.main_layout)


class MainTab(QWidget):
    reset_progressbar_signal = pyqtSignal(int)  # max progress
    increment_progressbar_signal = pyqtSignal(int)  # increment value
    update_label_signal = pyqtSignal(str)
    write_to_terminal_signal = pyqtSignal(str)
    add_comparison_result_signal = pyqtSignal(object)  # Result
    add_run_to_queue_signal = pyqtSignal(object) # Run object (or a subclass)
    update_run_status_signal = pyqtSignal(int, str) # run_id, status_str

    TAB_REGISTER = [
        {"name": "MAP"},
        {"name": "SCREEN"},
        {"name": "LOCAL"},
        {"name": "VERIFY"},
    ]

    def __init__(self):
        super().__init__()

        self.q = Queue()
        self.cg_q = Queue()
        self.helper_thread_running = False
        self.runs = [] # Run objects for canceling runs
        self.run_id = 0
        tabs = QTabWidget()
        self.map_tab = MapTab()
        self.screen_tab = ScreenTab()
        self.local_tab = LocalTab()
        self.verify_tab = VerifyTab()
        tabs.addTab(self.map_tab, "Check Map")
        tabs.addTab(self.screen_tab, "Screen User")
        tabs.addTab(self.local_tab, "Check Local Replays")
        tabs.addTab(self.verify_tab, "Verify")
        self.tabs = tabs
        self.tabs.currentChanged.connect(self.switch_run_button)

        terminal = QTextEdit()
        terminal.setFocusPolicy(Qt.ClickFocus)
        terminal.setReadOnly(True)
        terminal.ensureCursorVisible()
        self.terminal = terminal

        self.run_button = QPushButton()
        self.run_button.setText("Run")
        self.run_button.clicked.connect(self.add_circleguard_run)

        layout = QVBoxLayout()
        layout.addWidget(tabs)
        layout.addWidget(self.terminal)
        layout.addWidget(self.run_button)
        self.setLayout(layout)
        self.switch_run_button()  # disable run button if there is no api key

    def write(self, message):
        self.terminal.append(str(message).strip())
        self.scroll_to_bottom()

    def scroll_to_bottom(self):
        cursor = QTextCursor(self.terminal.document())
        cursor.movePosition(QTextCursor.End)
        self.terminal.setTextCursor(cursor)

    def add_circleguard_run(self):
        current_tab = self.tabs.currentIndex()
        run_type = MainTab.TAB_REGISTER[current_tab]["name"]
        m = self.map_tab
        s = self.screen_tab
        l = self.local_tab
        v = self.verify_tab

        # retrieve every possible variable to avoid nasty indentation (and we might
        # use it for something later). Shouldn't cause any performance issues.

        m_map_id = m.id_combined.map_id.field.text()
        m_map_id = int(m_map_id) if m_map_id != "" else None
        m_user_id = m.id_combined.user_id.field.text()
        m_user_id = int(m_user_id) if m_user_id != "" else None
        m_num = m.compare_top.slider.value()
        m_thresh = m.threshold.slider.value()

        s_user_id = s.user_id.field.text()
        s_user_id = int(s_user_id) if s_user_id != "" else None
        s_num_top = s.compare_top_map.slider.value()
        s_num_users = s.compare_top_users.slider.value()
        s_thresh = s.threshold.slider.value()

        l_path = resource_path(Path(l.folder_chooser.path))
        l_map_id = l.id_combined.map_id.field.text()
        l_map_id = int(l_map_id) if l_map_id != "" else None
        l_user_id = l.id_combined.user_id.field.text()
        l_user_id = int(l_user_id) if l_user_id != "" else None
        l_num = l.compare_top.slider.value()
        l_thresh = l.threshold.slider.value()


        v_map_id = v.map_id.field.text()
        v_map_id = int(v_map_id) if v_map_id != "" else None
        v_user_id_1 = v.user_id_1.field.text()
        v_user_id_1 = int(v_user_id_1) if v_user_id_1 != "" else None
        v_user_id_2 = v.user_id_2.field.text()
        v_user_id_2 = int(v_user_id_2) if v_user_id_2 != "" else None
        v_thresh = v.threshold.slider.value()

        event = threading.Event()

        if run_type == "MAP":
            run = MapRun(self.run_id, event, m_map_id, m_user_id, m_num, m_thresh)
        elif run_type == "SCREEN":
            run = ScreenRun(self.run_id, event, s_user_id, s_num_top, s_num_users, s_thresh)
        elif run_type == "LOCAL":
            run = LocalRun(self.run_id, event, l_path, l_map_id, l_user_id, l_num, l_thresh)
        elif run_type == "VERIFY":
            run = VerifyRun(self.run_id, event, v_map_id, v_user_id_1, v_user_id_2, v_thresh)

        self.runs.append(run)
        self.add_run_to_queue_signal.emit(run)
        self.cg_q.put(run)
        self.run_id += 1

        # called every 1/4 seconds by timer, but force a recheck to not wait for that delay
        self.check_circleguard_queue()

    def switch_run_button(self):
        # this line causes a "QObject::startTimer: Timers cannot be started from another thread" print
        # statement even though no timer interaction is going on; not sure why it happens but it doesn't
        # impact functionality. Still might be worth looking into
        self.run_button.setEnabled(False if get_setting("api_key") == "" else True)


    def check_circleguard_queue(self):
        def _check_circleguard_queue(self):
            try:
                while True:
                    run = self.cg_q.get_nowait()
                    thread = threading.Thread(target=self.run_circleguard, args=[run])
                    self.helper_thread_running = True
                    thread.start()
                    # run sequentially to not confuse user with terminal output
                    thread.join()
            except Empty:
                self.helper_thread_running = False
                return

        # don't launch another thread running cg if one is already running,
        # or else multiple runs will occur at once (defeats the whole purpose
        # of sequential runs)
        if not self.helper_thread_running:
            # have to do a double thread use if we start the threads in
            # the main thread and .join, it will block the gui thread (very bad).
            thread = threading.Thread(target=_check_circleguard_queue, args=[self])
            thread.start()



    def run_circleguard(self, run):
        self.update_label_signal.emit("Loading Replays")
        self.update_run_status_signal.emit(run.run_id, "Loading Replays")
        event = run.event
        try:
            set_options(cache=get_setting("caching"))
            cg = Circleguard(get_setting("api_key"), resource_path(os.path.join(get_setting("cache_dir"), "cache.db")))

            if type(run) is MapRun:
                check = cg.create_map_check(run.map_id, u=run.user_id, num=run.num, thresh=run.thresh)
            elif type(run) is ScreenRun:
                check = cg.create_user_check(run.user_id, run.num_top, run.num_users, thresh=run.thresh)
            elif type(run) is LocalRun:
                check = cg.create_local_check(run.path, map_id=run.map_id, u=run.user_id, num=run.num, thresh=run.thresh)
            elif type(run) is VerifyRun:
                check = cg.create_verify_check(run.map_id, run.user_id_1, run.user_id_2, thresh=run.thresh)

            # user_check convenience method comes with some caveats; a list
            # of list of check objects is returned instead of a single Check
            # because it checks for remodding and replay stealing for
            # each top play of the user
            if type(run) is ScreenRun:
                num_to_load = 0
                for check_list in check:
                    for check_ in check_list:
                        num_to_load += len(check_.all_replays())
            else:
                num_to_load = len(check.all_replays())
            self.reset_progressbar_signal.emit(num_to_load)
            cg.loader.new_session(num_to_load)
            timestamp = datetime.now()
            self.write_to_terminal_signal.emit(get_setting("message_loading_replays").format(ts=timestamp, num_replays=num_to_load))
            if type(run) is ScreenRun:
                for check_list in check:
                    for check_ in check_list:
                        for replay in check_.all_replays():
                            if event.wait(0):
                                self.update_label_signal.emit("Canceled")
                                self.reset_progressbar_signal.emit(-1)
                                return
                            cg.load(check_, replay)
                            self.increment_progressbar_signal.emit(1)
                        check_.loaded = True
            else:
                for replay in check.all_replays():
                    if event.wait(0):
                        self.update_label_signal.emit("Canceled")
                        self.reset_progressbar_signal.emit(-1)
                        return
                    cg.load(check, replay)
                    self.increment_progressbar_signal.emit(1)
                check.loaded = True


            self.reset_progressbar_signal.emit(0)  # changes progressbar into a "progressing" state
            timestamp = datetime.now()
            self.write_to_terminal_signal.emit(get_setting("message_starting_comparing").format(ts=timestamp, num_replays=num_to_load))
            self.update_label_signal.emit("Comparing Replays")
            self.update_run_status_signal.emit(run.run_id, "Comparing Replays")
            if type(run) is ScreenRun:
                for check_list in check:
                    for check_ in check_list:
                        for result in cg.run(check_):
                            if event.wait(0):
                                self.update_label_signal.emit("Canceled")
                                self.reset_progressbar_signal.emit(-1)
                                return
                            self.q.put(result)
            else:
                for result in cg.run(check):
                    if event.wait(0):
                        self.update_label_signal.emit("Canceled")
                        self.reset_progressbar_signal.emit(-1)
                        return
                    self.q.put(result)
            self.reset_progressbar_signal.emit(-1)  # resets progressbar so it's empty again

            timestamp = datetime.now()
            self.write_to_terminal_signal.emit(get_setting("message_finished_comparing").format(ts=timestamp, num_replays=num_to_load))

        except Exception:
            log.exception("Error while running circlecore. Please "
                          "report this to the developers through discord or github.\n")

        self.update_label_signal.emit("Idle")
        self.update_run_status_signal.emit(run.run_id, "Finished")

    def print_results(self):
        try:
            while True:
                result = self.q.get_nowait()
                # self.visualize(result.replay1, result.replay2)
                if result.ischeat:
                    timestamp = datetime.now()
                    r1 = result.replay1
                    r2 = result.replay2
                    msg = get_setting("message_cheater_found").format(ts=timestamp, similarity=result.similarity,
                                                                      r=result, r1=r1, r2=r2)
                    self.write(msg)
                    QApplication.beep()
                    QApplication.alert(self)
                    # add to Results Tab so it can be played back on demand
                    self.add_comparison_result_signal.emit(result)

                elif result.similarity < get_setting("threshold_display"):
                    timestamp = datetime.now()
                    r1 = result.replay1
                    r2 = result.replay2
                    msg = get_setting("message_no_cheater_found").format(ts=timestamp, similarity=result.similarity,
                                                                      r=result, r1=r1, r2=r2)
                    self.write(msg)
        except Empty:
            pass

    def visualize(self, replay1, replay2):
        self.visualizer_window = VisualizerWindow(replays=(replay1, replay2))
        self.visualizer_window.show()


class MapTab(QWidget):
    def __init__(self):
        super().__init__()

        self.info = QLabel(self)
        self.info.setText("Compares the top plays on a Map's leaderboard.\nIf a user is given, "
                          "it will compare that user's play on the map against the other top plays of the map.")

        self.id_combined = IdWidgetCombined()
        self.compare_top = CompareTopUsers(2)

        self.threshold = Threshold()  # ThresholdCombined()
        layout = QGridLayout()
        layout.addWidget(self.info, 0, 0, 1, 1)
        layout.addWidget(self.id_combined, 1, 0, 2, 1)
        layout.addWidget(self.compare_top, 4, 0, 1, 1)
        layout.addWidget(self.threshold, 5, 0, 1, 1)

        self.setLayout(layout)


class ScreenTab(QWidget):
    def __init__(self):
        super().__init__()
        self.info = QLabel(self)
        self.info.setText("Compares each of a user's top plays against that map's leaderboard.")

        self.user_id = InputWidget("User Id", "User id, as seen in the profile url", type_="id")
        self.compare_top_users = CompareTopUsers(1)
        self.compare_top_map = CompareTopPlays()
        self.threshold = Threshold()  # ThresholdCombined()

        layout = QGridLayout()
        layout.addWidget(self.info, 0, 0, 1, 1)
        layout.addWidget(self.user_id, 1, 0, 1, 1)
        # leave space for inserting the user user top plays widget
        layout.addWidget(self.compare_top_map, 3, 0, 1, 1)
        layout.addWidget(self.compare_top_users, 4, 0, 1, 1)
        layout.addWidget(self.threshold, 5, 0, 1, 1)
        self.layout = layout
        self.setLayout(self.layout)


class LocalTab(QWidget):
    def __init__(self):
        super().__init__()
        self.info = QLabel(self)
        self.info.setText("Compares osr files in a given folder.\n"
                          "If a Map is given, it will compare the osrs against the leaderboard of that map.\n"
                          "If both a user and a map are given, it will compare the osrs against the user's "
                          "score on that map.")
        self.folder_chooser = FolderChooser("Replay folder", get_setting("local_replay_dir"))
        self.folder_chooser.path_signal.connect(self.update_dir)
        self.id_combined = IdWidgetCombined()
        self.compare_top = CompareTopUsers(1)
        self.threshold = Threshold()  # ThresholdCombined()
        self.id_combined.map_id.field.textChanged.connect(self.switch_compare)
        self.switch_compare()

        self.grid = QGridLayout()
        self.grid.addWidget(self.info, 0, 0, 1, 1)
        self.grid.addWidget(self.folder_chooser, 1, 0, 1, 1)
        self.grid.addWidget(self.id_combined, 2, 0, 2, 1)
        self.grid.addWidget(self.compare_top, 4, 0, 1, 1)
        self.grid.addWidget(self.threshold, 5, 0, 1, 1)

        self.setLayout(self.grid)

    def switch_compare(self):
        self.compare_top.update_user(self.id_combined.map_id.field.text() != "")

    def update_dir(self, path):
        update_default("local_replay_dir", path)


class VerifyTab(QWidget):
    def __init__(self):
        super().__init__()
        self.info = QLabel(self)
        self.info.setText("Checks if the given user's replays on a map are steals of each other.")

        self.map_id = InputWidget("Map Id", "Beatmap id, not the mapset id!", type_="id")
        self.user_id_1 = InputWidget("User Id #1", "User id, as seen in the profile url", type_="id")
        self.user_id_2 = InputWidget("User Id #2", "User id, as seen in the profile url", type_="id")

        self.threshold = Threshold()  # ThresholdCombined()

        layout = QGridLayout()
        layout.addWidget(self.info, 0, 0, 1, 1)
        layout.addWidget(self.map_id, 1, 0, 1, 1)
        layout.addWidget(self.user_id_1, 2, 0, 1, 1)
        layout.addWidget(self.user_id_2, 3, 0, 1, 1)
        layout.addWidget(self.threshold, 4, 0, 1, 1)

        self.setLayout(layout)


class SettingsTab(QWidget):
    def __init__(self):
        super(SettingsTab, self).__init__()
        self.qscrollarea = QScrollArea(self)
        self.qscrollarea.setWidget(ScrollableSettingsWidget())
        self.qscrollarea.setAlignment(Qt.AlignCenter)  # center in scroll area - maybe undesirable
        self.qscrollarea.setWidgetResizable(True)

        self.info = QLabel(self)
        self.info.setText(f"Backend Version: {cg_version}<br/>"
                          f"Frontend Version: {__version__}<br/>"
                          f"Found a bug or want to request a feature? "
                          f"Open an issue <a href=\"https://github.com/circleguard/circleguard/issues\">here</a>!")
        self.info.setTextFormat(Qt.RichText)
        self.info.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.info.setOpenExternalLinks(True)
        self.info.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout()
        layout.addWidget(self.info)
        layout.addWidget(self.qscrollarea)
        self.setLayout(layout)


class ScrollableSettingsWidget(QFrame):
    """
    This class contains all of the actual settings content - SettingsTab just has a
    QScrollArea wrapped around this widget so that it can be scrolled down.
    """
    def __init__(self):
        super().__init__()
        self._rainbow_speed = 0.005
        self._rainbow_counter = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_color)
        self.welcome = wizard.WelcomeWindow()
        self.welcome.SetupPage.darkmode.box.stateChanged.connect(switch_theme)
        self.welcome.SetupPage.caching.box.stateChanged.connect(partial(update_default, "caching"))

        self.apikey_widget = LineEditSetting("Api Key", "", "password", "api_key")

        self.cheat_thresh = SliderBoxSetting("Cheat Threshold", "Comparisons that score below this will be stored so you can view them",
                                                "threshold_cheat", 30)
        self.display_thresh = SliderBoxSetting("Display Threshold", "Comparisons that score below this will be printed to the textbox",
                                                "threshold_display", 100)

        self.darkmode = OptionWidget("Dark mode", "Come join the dark side")
        self.darkmode.box.stateChanged.connect(switch_theme)

        self.cache = OptionWidget("Caching", "Downloaded replays will be cached locally")
        self.cache.box.stateChanged.connect(partial(update_default, "caching"))

        self.cache_dir = FolderChooser("Cache Path", get_setting("cache_dir"))
        self.cache_dir.path_signal.connect(partial(update_default, "cache_dir"))
        self.cache.box.stateChanged.connect(self.cache_dir.switch_enabled)

        self.loglevel = LoglevelWidget("")
        self.loglevel.level_combobox.currentIndexChanged.connect(self.set_circleguard_loglevel)
        self.set_circleguard_loglevel()  # set the default loglevel in cg, not just in gui

        self.rainbow = OptionWidget("Rainbow mode", "This is an experimental function, it may cause unintended behavior!")
        self.rainbow.box.stateChanged.connect(self.switch_rainbow)

        self.wizard = ButtonWidget("Run Wizard", "")
        self.wizard.button.clicked.connect(self.show_wizard)

        self.grid = QVBoxLayout()
        self.grid.addWidget(Separator("General"))
        self.grid.addWidget(self.apikey_widget)
        self.grid.addWidget(self.cheat_thresh)
        self.grid.addWidget(self.display_thresh)
        self.grid.addWidget(self.cache)
        self.grid.addWidget(self.cache_dir)
        self.grid.addWidget(Separator("Appearance"))
        self.grid.addWidget(self.darkmode)
        self.grid.addWidget(Separator("Debug"))
        self.grid.addWidget(self.loglevel)
        self.grid.addWidget(ResetSettings())
        self.grid.addWidget(Separator("Message Format"))
        self.grid.addWidget(StringFormatWidget(""))
        self.grid.addWidget(Separator("Dev"))
        self.grid.addWidget(self.rainbow)
        self.grid.addWidget(self.wizard)
        self.grid.addWidget(BeatmapTest())

        self.setLayout(self.grid)

        old_theme = get_setting("dark_theme")  # this is needed because switch_theme changes the setting
        self.darkmode.box.setChecked(-1)  # force-runs switch_theme if the DARK_THEME is False
        self.darkmode.box.setChecked(old_theme)
        self.cache.box.setChecked(get_setting("caching"))
        self.cache_dir.switch_enabled(get_setting("caching"))
        self.rainbow.box.setChecked(get_setting("rainbow_accent"))

    def set_circleguard_loglevel(self):
        set_options(loglevel=self.loglevel.level_combobox.currentData())

    def next_color(self):
        (r, g, b) = colorsys.hsv_to_rgb(self._rainbow_counter, 1.0, 1.0)
        color = QColor(int(255 * r), int(255 * g), int(255 * b))
        switch_theme(get_setting("dark_theme"), color)
        self._rainbow_counter += self._rainbow_speed
        if self._rainbow_counter >= 1:
            self._rainbow_counter = 0

    def switch_rainbow(self, state):
        update_default("rainbow_accent", 1 if state else 0)
        if get_setting("rainbow_accent"):
            self.timer.start(1000/15)
        else:
            self.timer.stop()
            switch_theme(get_setting("dark_theme"))

    def show_wizard(self):
        self.welcome.show()


class ResultsTab(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout()
        self.qscrollarea = QScrollArea(self)
        self.results = ResultsFrame()
        self.qscrollarea.setWidget(self.results)
        self.qscrollarea.setWidgetResizable(True)


        layout.addWidget(self.qscrollarea)
        self.setLayout(layout)


class ResultsFrame(QFrame):
    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout()
        # we want widgets to fill from top down,
        # being vertically centered looks weird
        self.layout.setAlignment(Qt.AlignTop)
        self.info_label = QLabel("After running Comparisons, this tab will fill up with results")
        self.layout.addWidget(self.info_label)
        self.setLayout(self.layout)


class QueueTab(QFrame):
    cancel_run_signal = pyqtSignal(int) # run_id

    def __init__(self):
        super().__init__()

        self.run_widgets = []
        layout = QVBoxLayout()
        self.qscrollarea = QScrollArea(self)
        self.queue = QueueFrame()
        self.qscrollarea.setWidget(self.queue)
        self.qscrollarea.setWidgetResizable(True)
        layout.addWidget(self.qscrollarea)
        self.setLayout(layout)

    def add_run(self, run):
        run_w = RunWidget(run)
        run_w.button.pressed.connect(partial(self.cancel_run, run.run_id))
        self.run_widgets.append(run_w)
        self.queue.layout.addWidget(run_w)

    def update_status(self, run_id, status):
        self.run_widgets[run_id].update_status(status)

    def cancel_run(self, run_id):
        self.cancel_run_signal.emit(run_id)
        self.run_widgets[run_id].cancel()

class QueueFrame(QFrame):

    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout()
        # we want widgets to fill from top down,
        # being vertically centered looks weird
        self.layout.setAlignment(Qt.AlignTop)
        self.setLayout(self.layout)



def switch_theme(dark, accent=QColor(71, 174, 247)):
    update_default("dark_theme", dark)
    if dark:
        dark_p = QPalette()

        dark_p.setColor(QPalette.Window, QColor(53, 53, 53))
        dark_p.setColor(QPalette.WindowText, Qt.white)
        dark_p.setColor(QPalette.Base, QColor(25, 25, 25))
        dark_p.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        dark_p.setColor(QPalette.ToolTipBase, QColor(53, 53, 53))
        dark_p.setColor(QPalette.ToolTipText, Qt.white)
        dark_p.setColor(QPalette.Text, Qt.white)
        dark_p.setColor(QPalette.Button, QColor(53, 53, 53))
        dark_p.setColor(QPalette.ButtonText, Qt.white)
        dark_p.setColor(QPalette.BrightText, Qt.red)
        dark_p.setColor(QPalette.Highlight, accent)
        dark_p.setColor(QPalette.Inactive, QPalette.Highlight, Qt.lightGray)
        dark_p.setColor(QPalette.HighlightedText, Qt.black)
        dark_p.setColor(QPalette.Disabled, QPalette.Text, Qt.darkGray)
        dark_p.setColor(QPalette.Disabled, QPalette.ButtonText, Qt.darkGray)
        dark_p.setColor(QPalette.Disabled, QPalette.Highlight, Qt.darkGray)
        dark_p.setColor(QPalette.Disabled, QPalette.Base, QColor(53, 53, 53))
        dark_p.setColor(QPalette.Link, accent)
        dark_p.setColor(QPalette.LinkVisited, accent)

        app.setPalette(dark_p)
        app.setStyleSheet("QToolTip { color: #ffffff; "
                          "background-color: #2a2a2a; "
                          "border: 1px solid white; }"
                          "QLabel {font-weight: Normal; }")
    else:
        app.setPalette(app.style().standardPalette())
        updated_palette = QPalette()
        # fixes inactive items not being greyed out
        updated_palette.setColor(QPalette.Disabled, QPalette.ButtonText, Qt.darkGray)
        updated_palette.setColor(QPalette.Highlight, accent)
        updated_palette.setColor(QPalette.Disabled, QPalette.Highlight, Qt.darkGray)
        updated_palette.setColor(QPalette.Inactive, QPalette.Highlight, Qt.darkGray)
        updated_palette.setColor(QPalette.Link, accent)
        updated_palette.setColor(QPalette.LinkVisited, accent)
        app.setPalette(updated_palette)
        app.setStyleSheet("QToolTip { color: #000000; "
                          "background-color: #D5D5D5; "
                          "border: 1px solid white; }"
                          "QLabel {font-weight: Normal; }")


if __name__ == "__main__":
    # create and open window
    app = QApplication([])
    app.setStyle("Fusion")
    WINDOW = WindowWrapper(app.clipboard())
    set_event_window(WINDOW)
    WINDOW.resize(600, 500)
    WINDOW.show()
    if not get_setting("ran"):
        welcome = wizard.WelcomeWindow()
        welcome.SetupPage.darkmode.box.stateChanged.connect(switch_theme)
        welcome.SetupPage.caching.box.stateChanged.connect(partial(update_default,"caching"))
        welcome.show()
        update_default("ran", True)
    app.exec_()
