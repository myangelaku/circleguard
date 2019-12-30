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
import math
import time
# pylint: disable=no-name-in-module
from PyQt5.QtCore import Qt, QTimer, qInstallMessageHandler, QObject, pyqtSignal, QUrl, QMimeData
from PyQt5.QtWidgets import (QWidget, QFrame, QTabWidget, QTextEdit, QPushButton, QLabel, QScrollArea, QFrame, QProgressBar,
                             QVBoxLayout, QShortcut, QGridLayout, QApplication, QMainWindow, QSizePolicy, QComboBox)
from PyQt5.QtGui import QPalette, QColor, QIcon, QKeySequence, QTextCursor, QPainter, QDesktopServices, QDrag
# pylint: enable=no-name-in-module

# app needs to be initialized before settings is imported so QStandardPaths resolves
# corerctly with the applicationName
app = QApplication([])
app.setStyle("Fusion")
app.setApplicationName("Circleguard")

from circleguard import (Circleguard, set_options, Loader, NoInfoAvailableException,
                        ReplayMap, ReplayPath, User, Map, Check, MapUser, StealDetect)
from circleguard import __version__ as cg_version
from visualizer import VisualizerWindow
from utils import resource_path, run_update_check, MapRun, ScreenRun, LocalRun, VerifyRun
from widgets import (Threshold, set_event_window, InputWidget, ResetSettings, WidgetCombiner,
                     FolderChooser, IdWidgetCombined, Separator, OptionWidget, ButtonWidget,
                     CompareTopPlays, CompareTopUsers, LoglevelWidget, SliderBoxSetting,
                     TopPlays, BeatmapTest, ComparisonResult, LineEditSetting, EntryWidget,
                     RunWidget)

from settings import get_setting, set_setting, overwrite_config, overwrite_with_config_settings
import wizard
from version import __version__


log = logging.getLogger(__name__)

# save old excepthook
sys._excepthook = sys.excepthook

def write_log(message):
    log_dir = resource_path(get_setting("log_dir"))
    if not os.path.exists(log_dir):  # create dir if nonexistent
        os.makedirs(log_dir)
    directory = os.path.join(log_dir, "circleguard.log")
    with open(directory, 'a+') as f:  # append so it creates a file if it doesn't exist
        f.seek(0)
        data = f.read().splitlines(True)
    data.append(message+"\n")
    with open(directory, 'w+') as f:
        f.writelines(data[-1000:])  # keep file at 1000 lines

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
        self.current_state_label.setTextFormat(Qt.RichText)
        self.current_state_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.current_state_label.setOpenExternalLinks(True)
        # statusBar() is a qt function that will create a status bar tied to the window
        # if it doesnt exist, and access the existing one if it does.
        self.statusBar().addWidget(WidgetCombiner(self.progressbar, self.current_state_label))
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setContentsMargins(8, 2, 10, 2)

        self.main_window = MainWindow()
        self.main_window.main_tab.set_progressbar_signal.connect(self.set_progressbar)
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
        QShortcut(QKeySequence(Qt.CTRL+Qt.Key_Q), self, partial(self.cancel_all_runs, self))

        self.setWindowTitle(f"Circleguard v{__version__}")
        self.setWindowIcon(QIcon(str(resource_path("resources/logo.ico"))))
        self.start_timer()
        self.debug_window = None

        handler = Handler()
        logging.getLogger("circleguard").addHandler(handler)
        logging.getLogger("ossapi").addHandler(handler)
        logging.getLogger(__name__).addHandler(handler)
        formatter = logging.Formatter("[%(levelname)s] %(asctime)s.%(msecs)04d %(message)s (%(name)s, %(filename)s:%(lineno)d)", datefmt="%Y/%m/%d %H:%M:%S")
        handler.setFormatter(formatter)
        handler.new_message.connect(self.log)

        self.thread = threading.Thread(target=self._change_label_update)
        self.thread.start()

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
        if focused is not None and not isinstance(focused, QTextEdit):
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

    def _change_label_update(self):
        self.update_label(run_update_check())

    def increment_progressbar(self, increment):
        self.progressbar.setValue(self.progressbar.value() + increment)

    def set_progressbar(self, max_value):
        # if -1, reset progressbar and remove its text
        if max_value == -1:
            # removes cases where range was set to (0,0)
            self.progressbar.setRange(0, 1)
            # remove ``0%`` text on the progressbar (doesn't look good)
            self.progressbar.reset()
            return
        self.progressbar.setValue(0)
        self.progressbar.setRange(0, max_value)


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
        result_widget.button.clicked.connect(partial(self.main_window.main_tab.visualize, [result_widget.replay1, result_widget.replay2]))
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

    def cancel_all_runs(self):
        """called when lastWindowClosed signal emits. Cancel all our runs so
        we don't hang the application on loading/comparing while trying to quit"""
        for run in self.main_window.main_tab.runs:
            run.event.set()

    def on_application_quit(self):
        """Called when the app.aboutToQuit signal is emitted"""
        if self.debug_window != None:
            self.debug_window.close()
        if self.main_window.main_tab.visualizer_window != None:
            self.main_window.main_tab.visualizer_window.close()
        overwrite_config()

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


class MainWindow(QFrame):
    def __init__(self):
        super().__init__()

        self.tab_widget = QTabWidget()
        self.main_tab = MainTab()
        self.results_tab = ResultsTab()
        self.queue_tab = QueueTab()
        self.thresholds_tab = ThresholdsTab()
        self.settings_tab = SettingsTab()
        self.tab_widget.addTab(self.main_tab, "Main")
        self.tab_widget.addTab(self.results_tab, "Results")
        self.tab_widget.addTab(self.queue_tab, "Queue")
        self.tab_widget.addTab(self.thresholds_tab, "Thresholds")
        self.tab_widget.addTab(self.settings_tab, "Settings")
        # so when we switch from settings tab to main tab, whatever tab we're on gets changed if we delete our api key
        self.tab_widget.currentChanged.connect(self.main_tab.check_run_button)

        self.layout = QVBoxLayout()
        self.layout.addWidget(self.tab_widget)
        self.layout.setContentsMargins(10, 10, 10, 0)
        self.setLayout(self.layout)


class BorderWidget(QFrame):
    """
    For debugging classes that don't seem to be appearing. Adds a white border
    around the QFrame.

    To use, replace the inheritence of QFrame with BorderWidget. Possibly works
    with multiple inheritence (using this as a mixin), but I'm not sure.
    """
    def __init__(self):
        super().__init__()
        self.setFrameStyle(QFrame.Box)


class ScrollableLoadablesWidget(QFrame):
    def __init__(self):
        super().__init__()
        # needs to be an attribute to programatically add widgets
        self.layout = QVBoxLayout()
        # fill top-down
        self.layout.setAlignment(Qt.AlignTop)
        self.setLayout(self.layout)


class ScrollableDetectsWidget(QFrame):
    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout()
        self.layout.setAlignment(Qt.AlignTop)
        self.setLayout(self.layout)

class DetectW(BorderWidget):
    def __init__(self, name):
        super().__init__()
        self.layout = QGridLayout()

        title = QLabel()
        title.setText(f"{name}")
        self.layout.addWidget(title)
        self.setLayout(self.layout)

class StealDetectW(DetectW):
    def __init__(self):
        super().__init__("Replay Stealing Check")

class RelaxDetectW(DetectW):
    def __init__(self):
        super().__init__("Relax Check")

class CorrectionDetectW(DetectW):
    def __init__(self):
        super().__init__("Aim Correction Check")

class LoadableW(BorderWidget):
    """
    A widget representing a circleguard.Loadable, which can be dragged onto
    a DetectWidget. Keeps track of how many LoadableWidgets have been created
    as a static ID attribute.

    W standing for widget.
    """
    ID = 0
    remove_loadable_signal = pyqtSignal(int) # id of loadable to remove
    def __init__(self, name):
        super().__init__()
        LoadableW.ID += 1

        self.layout = QGridLayout()

        title = QLabel(self)
        self.loadable_id = LoadableW.ID
        t = "\t" # https://stackoverflow.com/a/44780467/12164878
        # double tabs on short names to align with longer ones
        title.setText(f"{name}{t+t if len(name) < 5 else t}(Id: {self.loadable_id})")

        self.cancel_button = QPushButton() # TODO add x icon
        self.cancel_button.pressed.connect(partial(lambda loadable_id: self.remove_loadable_signal.emit(loadable_id), self.loadable_id))
        self.layout.addWidget(title, 0, 0, 1, 7)
        self.layout.addWidget(self.cancel_button, 0, 7, 1, 1)
        self.setLayout(self.layout)

    # Resources for drag and drop operations:
    # qt tutorial           https://doc.qt.io/qt-5/qtwidgets-draganddrop-draggableicons-example.html
    # qdrag docs            https://doc.qt.io/qt-5/qdrag.html
    # real example code     https://lists.qt-project.org/pipermail/qt-interest-old/2011-June/034531.html
    # bad example code      https://stackoverflow.com/q/7737913/12164878
    def mouseMoveEvent(self, event):
        print(event.pos())
        self.drag = QDrag(self)
        self.drag.setHotSpot(event.pos())
        # https://stackoverflow.com/a/53538805/12164878
        pixmap = self.grab()
        # set pixmap as this widget so it looks like we're dragging it
        self.drag.setPixmap(pixmap)
        mime_data = QMimeData()
        self.drag.setMimeData(mime_data) # TODO fill with id
        self.drag.exec() # start the drag

class ReplayMapW(LoadableW):
    """
    W standing for Widget.
    """
    def __init__(self):
        super().__init__("ReplayMap")

        self.map_id_input = InputWidget("Map id", "", "id")
        self.user_id_input = InputWidget("User id", "", "id")
        self.mods_input = InputWidget("Mods (opt.)", "", "normal")

        self.layout.addWidget(self.map_id_input, 1, 0, 1, 8)
        self.layout.addWidget(self.user_id_input, 2, 0, 1, 8)
        self.layout.addWidget(self.mods_input, 3, 0, 1, 8)


class ReplayPathW(LoadableW):
    def __init__(self):
        super().__init__("ReplayPath")

        self.path_input = FolderChooser(".osr path", folder_mode=False, file_ending="osu! Replayfile (*.osr)")

        self.layout.addWidget(self.path_input)

class MapW(LoadableW):
    def __init__(self):
        super().__init__("Map")

        self.map_id_input = InputWidget("Map id", "", "id")
        self.span_input = InputWidget("Span", "", "normal")
        self.mods_input = InputWidget("Mods (opt.)", "", "normal")

        self.layout.addWidget(self.map_id_input, 1, 0, 1, 8)
        self.layout.addWidget(self.span_input, 2, 0, 1, 8)
        self.layout.addWidget(self.mods_input, 3, 0, 1, 8)

class UserW(LoadableW):
    def __init__(self):
        super().__init__("User")

        self.user_id_input = InputWidget("User id", "", "id")
        self.span_input = InputWidget("Span", "", "id")
        self.mods_input = InputWidget("Mods (opt.)", "", "normal")

        self.layout.addWidget(self.user_id_input, 1, 0, 1, 8)
        self.layout.addWidget(self.span_input, 2, 0, 1, 8)
        self.layout.addWidget(self.mods_input, 3, 0, 1, 8)

class MapUserW(LoadableW):
    def __init__(self):
        super().__init__("MapUser")

        self.map_id_input = InputWidget("Map id", "", "id")
        self.user_id_input = InputWidget("User id", "", "id")
        self.span_input = InputWidget("Span", "", "normal")
        self.mods_input = InputWidget("Mods (opt.)", "", "normal")

        self.layout.addWidget(self.map_id_input, 1, 0, 1, 8)
        self.layout.addWidget(self.user_id_input, 2, 0, 1, 8)
        self.layout.addWidget(self.span_input, 3, 0, 1, 8)
        self.layout.addWidget(self.mods_input, 4, 0, 1, 8)

class MainTab(QFrame):
    set_progressbar_signal = pyqtSignal(int)  # max progress
    increment_progressbar_signal = pyqtSignal(int)  # increment value
    update_label_signal = pyqtSignal(str)
    write_to_terminal_signal = pyqtSignal(str)
    add_comparison_result_signal = pyqtSignal(object)  # Result
    add_run_to_queue_signal = pyqtSignal(object) # Run object (or a subclass)
    update_run_status_signal = pyqtSignal(int, str) # run_id, status_str

    LOADABLES_COMBOBOX_REGISTRY = ["ReplayMap", "ReplayPath", "Map", "User", "MapUser"]
    DETECTS_COMBOBOX_REGISTRY = ["Steal", "Relax", "Correction"]

    def __init__(self):
        super().__init__()

        self.loadables_combobox = QComboBox(self)
        self.loadables_combobox.setInsertPolicy(QComboBox.NoInsert)
        for loadable in MainTab.LOADABLES_COMBOBOX_REGISTRY:
            self.loadables_combobox.addItem(loadable, loadable)
        self.loadables_button = QPushButton(self)
        self.loadables_button.pressed.connect(self.add_loadable)

        self.detects_combobox = QComboBox(self)
        self.detects_combobox.setInsertPolicy(QComboBox.NoInsert)
        for detect in MainTab.DETECTS_COMBOBOX_REGISTRY:
            self.detects_combobox.addItem(detect, detect)
        self.detects_button = QPushButton(self)
        self.detects_button.pressed.connect(self.add_detect)

        self.loadables_scrollarea = QScrollArea(self)
        self.loadables_scrollarea.setWidget(ScrollableLoadablesWidget())
        self.loadables_scrollarea.setWidgetResizable(True)

        self.detects_scrollarea = QScrollArea(self)
        self.detects_scrollarea.setWidget(ScrollableDetectsWidget())
        self.detects_scrollarea.setWidgetResizable(True)

        self.loadables = [] # for deleting later

        self.q = Queue()
        self.cg_q = Queue()
        self.helper_thread_running = False
        self.runs = [] # Run objects for canceling runs
        self.run_id = 0
        self.visualizer_window = None

        terminal = QTextEdit()
        terminal.setFocusPolicy(Qt.ClickFocus)
        terminal.setReadOnly(True)
        terminal.ensureCursorVisible()
        self.terminal = terminal

        self.run_button = QPushButton()
        self.run_button.setText("Run")
        self.run_button.clicked.connect(self.add_circleguard_run)

        layout = QGridLayout()
        layout.addWidget(self.loadables_combobox, 0, 0, 1, 7)
        layout.addWidget(self.loadables_button, 0, 7, 1, 1)
        layout.addWidget(self.detects_combobox, 0, 8, 1, 7)
        layout.addWidget(self.detects_button, 0, 15, 1, 1)
        layout.addWidget(self.loadables_scrollarea, 1, 0, 4, 8)
        layout.addWidget(self.detects_scrollarea, 1, 8, 4, 8)
        layout.addWidget(self.terminal, 5, 0, 2, 16)
        layout.addWidget(self.run_button, 6, 0, 1, 16)

        self.setLayout(layout)
        self.check_run_button() # disable run button if there is no api key

    def remove_loadable(self, loadable_id):
        # should only ever be one occurence, a comp + index works well enough
        loadables = [l for l in self.loadables if l.loadable_id == loadable_id]
        if not loadables: # sometimes an empty list, I don't know how if you need a loadable to click the delete button...
            return
        loadable = loadables[0]
        self.loadables.remove(loadable)
        self.loadables_scrollarea.widget().layout.removeWidget(loadable)
        # TODO
        # doing deleteLater here like we do in other places where we want to remove a
        # widget either segfaults or throws a very scary malloc memory error.
        #
        # EX:
        # Python(62285,0x10a9635c0) malloc: *** error for object 0x7fa589402890: pointer being freed was not allocated
        # Python(62285,0x10a9635c0) malloc: *** set a breakpoint in malloc_error_break to debug
        #
        # I think we're still leaving a reference to it somewhere,
        # but I don't know where - we remove it from the layout above.
        #
        # Hide is recommend by qt ("if necessary") https://doc.qt.io/qt-5/qlayout.html#removeWidget
        # but I'd feel better deleting it.
        loadable.hide()

    def add_loadable(self):
        button_data = self.loadables_combobox.currentData()
        if button_data == "ReplayMap":
            w = ReplayMapW()
        if button_data == "ReplayPath":
            w = ReplayPathW()
        if button_data == "Map":
            w = MapW()
        if button_data == "User":
            w = UserW()
        if button_data == "MapUser":
            w = MapUserW()
        self.loadables_scrollarea.widget().layout.addWidget(w)
        self.loadables.append(w) # for deleting it later
        w.remove_loadable_signal.connect(self.remove_loadable)

    def add_detect(self):
        button_data = self.detects_combobox.currentData()
        if button_data == "Steal":
            w = StealDetectW()
        if button_data == "Relax":
            w = RelaxDetectW()
        if button_data == "Correction":
            w = CorrectionDetectW()
        self.detects_scrollarea.widget().layout.addWidget(w)

    def write(self, message):
        self.terminal.append(str(message).strip())
        self.scroll_to_bottom()

    def scroll_to_bottom(self):
        cursor = QTextCursor(self.terminal.document())
        cursor.movePosition(QTextCursor.End)
        self.terminal.setTextCursor(cursor)

    def add_circleguard_run(self):
        current_tab = self.tabs.currentIndex()
        run_type = MainTab.TAB_REGISTRY[current_tab]["name"]

        # special case; visualize runs shouldn't get added to queue
        if run_type == "VISUALIZE":
            self.visualize([replay.data for replay in self.visualize_tab.replays])
            return

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

    def check_run_button(self):
        # this line causes a "QObject::startTimer: Timers cannot be started from another thread" print
        # statement even though no timer interaction is going on; not sure why it happens but it doesn't
        # impact functionality. Still might be worth looking into
        self.run_button.setEnabled(False if get_setting("api_key") == "" else True)


    def check_circleguard_queue(self):
        def _check_circleguard_queue(self):
            try:
                while True:
                    run = self.cg_q.get_nowait()
                    # occurs if run is canceled before being started, it will still stop
                    # before actually loading anything but we don't want the labels to flicker
                    if run.event.wait(0):
                        continue
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
            cache_path = resource_path(get_setting("cache_location"))
            should_cache = get_setting("caching")
            cg = Circleguard(get_setting("api_key"), cache_path, cache=should_cache, loader=TrackerLoader)
            def _ratelimited(length):
                message = get_setting("message_ratelimited")
                ts = datetime.now()
                self.write_to_terminal_signal.emit(message.format(s=length, ts=ts))
                self.update_label_signal.emit("Ratelimited")
                self.update_run_status_signal.emit(run.run_id, "Ratelimited")
            def _check_event(event):
                """
                Checks the given event to see if it is set. If it is, the run has been canceled
                through the queue tab or by the application being quit, and this thread exits
                through sys.exit(0). If the event is not set, returns silently.
                """
                if event.wait(0):
                    self.update_label_signal.emit("Canceled")
                    self.set_progressbar_signal.emit(-1)
                    # may seem dirty, but actually relatively clean since it only affects this thread.
                    # Any cleanup we may want to do later can occur here as well
                    sys.exit(0)

            cg.loader.ratelimit_signal.connect(_ratelimited)
            cg.loader.check_stopped_signal.connect(partial(_check_event, event))

            d = StealDetect(run.thresh)
            if type(run) is MapRun:
                m = Map(run.map_id, num=run.num)
                loadables = [m]
                if run.user_id:
                    user_replay = ReplayMap(run.map_id, run.user_id)
                    loadables.append(user_replay)
                check = Check(loadables, d)
            elif type(run) is ScreenRun:
                check = []
                user = User(run.user_id, run.num_top)
                cg.load_info(user)
                for r in user:
                    # 2-100 because the first one *is* ``r``.
                    m = Map(r.map_id, num=run.num_users)
                    remod_replays = MapUser(r.map_id, r.user_id, span="2-100")
                    remod_check = Check([r, remod_replays], d)
                    steal_check = Check([r, m], d)
                    check.append([remod_check, steal_check])
            elif type(run) is LocalRun:
                loadables = [ReplayPath(run.path / path) for path in os.listdir(run.path)]
                check = Check(loadables, d)
            elif type(run) is VerifyRun:
                r1 = ReplayMap(run.map_id, run.user_id_1)
                r2 = ReplayMap(run.map_id, run.user_id_2)
                check = Check([r1, r2], d)

            if type(run) is ScreenRun:
                num_to_load = 0
                # different from other runs; a list of list of check objects is
                # returned instead of a single Check because it checks for
                # remodding and replay  stealing for each top play of the user
                for check_list in check:
                    for check_ in check_list:
                        loadables = check_.all_replays()
                        num_to_load += len(loadables)
                self.set_progressbar_signal.emit(num_to_load)

                for check_list in check:
                    for check_ in check_list:
                        cg.load_info(check_)
                        loadables = check_.all_replays()

                        num_to_load = len(loadables)
                        timestamp = datetime.now()
                        self.write_to_terminal_signal.emit(get_setting("message_loading_replays").format(ts=timestamp, num_replays=num_to_load,
                                                                        map_id=loadables[0].map_id))
                        for replay in loadables:
                            _check_event(event)
                            cg.load(replay)
                            self.increment_progressbar_signal.emit(1)
                        check_.loaded = True

                        self.write_to_terminal_signal.emit(get_setting("message_starting_comparing").format(ts=timestamp, num_replays=num_to_load))
                        self.update_label_signal.emit("Comparing Replays")
                        self.update_run_status_signal.emit(run.run_id, "Comparing Replays")
                        for result in cg.run(check_):
                            _check_event(event)
                            self.q.put(result)

            else:
                cg.load_info(check)
                replays = check.all_replays()
                num_to_load = check.num_replays()
                self.set_progressbar_signal.emit(num_to_load)
                timestamp = datetime.now()
                if type(replays[0]) is ReplayPath:
                    cg.load(replays[0])
                    # Not perfect because local checks aren't guaranteed to have the same
                    # map id for every replay, but it's the best we can do right now.
                    # time spent loading one replay is worth getting the map id.
                    map_id = replays[0].map_id
                else:
                    map_id = replays[0].map_id

                self.write_to_terminal_signal.emit(get_setting("message_loading_replays").format(ts=timestamp, num_replays=num_to_load,
                                                                map_id=map_id))
                for replay in replays:
                    _check_event(event)
                    cg.load(replay)
                    self.increment_progressbar_signal.emit(1)
                check.loaded = True
                # change progressbar into an undetermined state
                # (animation with stripes sliding horizontally)
                # to indicate we're processing the data
                self.set_progressbar_signal.emit(0)
                timestamp = datetime.now()
                self.write_to_terminal_signal.emit(get_setting("message_starting_comparing").format(ts=timestamp, num_replays=num_to_load))
                self.update_label_signal.emit("Comparing Replays")
                self.update_run_status_signal.emit(run.run_id, "Comparing Replays")
                for result in cg.run(check):
                    _check_event(event)
                    self.q.put(result)

            self.set_progressbar_signal.emit(-1)  # resets progressbar so it's empty again
            timestamp = datetime.now()
            self.write_to_terminal_signal.emit(get_setting("message_finished_comparing").format(ts=timestamp, num_replays=num_to_load))

        except NoInfoAvailableException:
            self.write_to_terminal_signal.emit("No information found for those arguments. Please recheck your map/user id")
            self.set_progressbar_signal.emit(-1)

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

    def visualize(self, replays):
        self.visualizer_window = VisualizerWindow(replays=replays)
        self.visualizer_window.show()


class TrackerLoader(Loader, QObject):
    """
    A circleguard.Loader subclass that emits a signal when the loader is ratelimited.
    It inherits from QObject to allow us to use qt signals.
    """
    ratelimit_signal = pyqtSignal(int) # length of the ratelimit in seconds
    check_stopped_signal = pyqtSignal()
    # how often to emit check_stopped_signal when ratelimited, in seconds
    INTERVAL = 0.250

    def __init__(self, key, cacher=None):
        Loader.__init__(self, key, cacher)
        QObject.__init__(self)

    def _ratelimit(self, length):
        self.ratelimit_signal.emit(length)
        # how many times to wait for 1/4 second (rng standing for range)
        # we do this loop in order to tell run_circleguard to check if the run
        # was canceled, or the application quit, instead of hanging on a long
        # time.sleep
        rng = math.ceil(length / self.INTERVAL)
        for _ in range(rng):
            time.sleep(self.INTERVAL)
            self.check_stopped_signal.emit()


class VisualizeTab(QFrame):
    def __init__(self):
        super().__init__()
        self.result_frame = ResultsTab()
        self.result_frame.results.info_label.hide()
        self.map_id = None
        self.q = Queue()
        self.replays = []
        self.cg = Circleguard(get_setting("api_key"), resource_path(get_setting("cache_location")))
        self.info = QLabel(self)
        self.info.setText("Visualizes Replays. Has theoretically support for an arbitrary amount of replays.")
        self.file_chooser = FolderChooser("Add Replays", folder_mode=False, multiple_files=True,
                                            file_ending="osu! Replayfile (*osr)", display_path=False)
        self.file_chooser.path_signal.connect(self.add_files)
        self.folder_chooser = FolderChooser("Add Folder", display_path=False)
        self.folder_chooser.path_signal.connect(self.add_folder)
        layout = QGridLayout()
        layout.addWidget(self.info)
        layout.addWidget(self.file_chooser)
        layout.addWidget(self.folder_chooser)
        layout.addWidget(self.result_frame)

        self.setLayout(layout)

    def start_timer(self):
        timer = QTimer(self)
        timer.timeout.connect(self.run_timer)
        timer.start(250)

    def run_timer(self):
        self.add_widget()

    def add_files(self, paths):
        thread = threading.Thread(target=self._parse_replays, args=[paths])
        thread.start()
        self.start_timer()

    def add_folder(self, path):
        thread = threading.Thread(target=self._parse_folder, args=[path])
        thread.start()
        self.start_timer()

    def _parse_replays(self, paths):
        for path in paths:
            # guaranteed to end in .osr by our filter
            self._parse_replay(path)

    def _parse_folder(self, path):
        for f in os.listdir(path):  # os.walk seems unnecessary
            if f.endswith(".osr"):
                self._parse_replay(os.path.join(path, f))

    def _parse_replay(self, path):
        replay = ReplayPath(path)
        self.cg.load(replay)
        if self.map_id == None or len(self.replays) == 0:  # store map_id if nothing stored
            log.info(f"Changing map_id from {self.map_id} to {replay.map_id}")
            self.map_id = replay.map_id
        elif replay.map_id != self.map_id:  # ignore replay with diffrent map_ids
            log.error(f"replay {replay} doesn't match with current map_id ({replay.map_id} != {self.map_id})")
            return
        if not any(replay.replay_id == r.data.replay_id for r in self.replays):  # check if already stored
            log.info(f"adding new replay {replay} with replay id {replay.replay_id} on map {replay.map_id}")
            self.q.put(replay)
        else:
            log.info(f"skipping replay {replay} with replay id {replay.replay_id} on map {replay.map_id} since it's already saved")

    def add_widget(self):
        try:
            while True:
                replay = self.q.get(block=False)
                widget = EntryWidget(f"{replay.username}'s play with the id {replay.replay_id}", "Delete", replay)
                widget.pressed_signal.connect(self.remove_replay)
                self.replays.append(widget)
                self.result_frame.results.layout.addWidget(widget)
        except Empty:
            pass


    def remove_replay(self, data):
        replay_ids = [replay.data.replay_id for replay in self.replays]
        index = replay_ids.index(data.replay_id)
        self.result_frame.results.layout.removeWidget(self.replays[index])
        self.replays[index].deleteLater()
        self.replays[index] = None
        self.replays.pop(index)


class SettingsTab(QFrame):
    def __init__(self):
        super().__init__()
        self.qscrollarea = QScrollArea(self)
        self.qscrollarea.setWidget(ScrollableSettingsWidget())
        self.qscrollarea.setAlignment(Qt.AlignCenter)  # center in scroll area - maybe undesirable
        self.qscrollarea.setWidgetResizable(True)

        self.info = QLabel(self)
        self.info.setText(f"Frontend v{__version__}<br/>"
                          f"Backend v{cg_version}<br/>"
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

        self.apikey_widget = LineEditSetting("Api Key", "", "password", "api_key")
        self.darkmode = OptionWidget("Dark mode", "Come join the dark side", "dark_theme")
        self.darkmode.box.stateChanged.connect(self.reload_theme)
        self.visualizer_info = OptionWidget("Show Visualizer info", "", "visualizer_info")
        self.visualizer_bg = OptionWidget("Black Visualizer bg", "Reopen Visualizer for it to apply", "visualizer_bg")
        self.visualizer_bg.box.stateChanged.connect(self.reload_theme)
        self.cache = OptionWidget("Caching", "Downloaded replays will be cached locally", "caching")
        self.cache_location = FolderChooser("Cache Location", get_setting("cache_location"), folder_mode=False, file_ending="SQLite db files (*.db)")
        self.cache_location.path_signal.connect(partial(set_setting, "cache_location"))
        self.cache.box.stateChanged.connect(self.cache_location.switch_enabled)

        self.open_settings = ButtonWidget("Edit Settings File", "Open", "")
        self.open_settings.button.clicked.connect(self._open_settings)

        self.sync_settings = ButtonWidget("Sync Settings", "Sync", "")
        self.sync_settings.button.clicked.connect(self._sync_settings)

        self.loglevel = LoglevelWidget("")
        self.loglevel.level_combobox.currentIndexChanged.connect(self.set_loglevel)
        self.set_loglevel()  # set the default loglevel in cg, not just in gui

        self.rainbow = OptionWidget("Rainbow mode", "This is an experimental function, it may cause unintended behavior!", "rainbow_accent")
        self.rainbow.box.stateChanged.connect(self.switch_rainbow)

        self.wizard = ButtonWidget("Run Wizard", "Run", "")
        self.wizard.button.clicked.connect(self.show_wizard)

        self.layout = QVBoxLayout()
        self.layout.addWidget(Separator("General"))
        self.layout.addWidget(self.apikey_widget)
        self.layout.addWidget(self.cache)
        self.layout.addWidget(self.cache_location)
        self.layout.addWidget(self.open_settings)
        self.layout.addWidget(self.sync_settings)
        self.layout.addWidget(Separator("Appearance"))
        self.layout.addWidget(self.darkmode)
        self.layout.addWidget(self.visualizer_info)
        self.layout.addWidget(self.visualizer_bg)
        self.layout.addWidget(Separator("Debug"))
        self.layout.addWidget(self.loglevel)
        self.layout.addWidget(ResetSettings())
        self.layout.addWidget(Separator("Dev"))
        self.layout.addWidget(self.rainbow)
        self.layout.addWidget(self.wizard)
        self.layout.addWidget(BeatmapTest())

        self.setLayout(self.layout)

        self.cache_location.switch_enabled(get_setting("caching"))
        # we never actually set the theme to dark anywhere
        # (even if the setting is true), it should really be
        # in the main application but uh this works too
        self.reload_theme()

    def set_loglevel(self):
        for logger in logging.root.manager.loggerDict:
            logging.getLogger(logger).setLevel(self.loglevel.level_combobox.currentData())

    def next_color(self):
        (r, g, b) = colorsys.hsv_to_rgb(self._rainbow_counter, 1.0, 1.0)
        color = QColor(int(255 * r), int(255 * g), int(255 * b))
        switch_theme(get_setting("dark_theme"), color)
        self._rainbow_counter += self._rainbow_speed
        if self._rainbow_counter >= 1:
            self._rainbow_counter = 0

    def switch_rainbow(self, state):
        set_setting("rainbow_accent", 1 if state else 0)
        if get_setting("rainbow_accent"):
            self.timer.start(1000/15)
        else:
            self.timer.stop()
            switch_theme(get_setting("dark_theme"))

    def show_wizard(self):
        self.welcome.show()

    def reload_theme(self):
        switch_theme(get_setting("dark_theme"))

    def _open_settings(self):
        overwrite_config()  # generate file with latest changes
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_setting("config_location")))

    def _sync_settings(self):
        overwrite_with_config_settings()


class ResultsTab(QFrame):
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

class ThresholdsTab(QFrame):
    def __init__(self):
        super().__init__()
        self.qscrollarea = QScrollArea(self)
        self.qscrollarea.setWidget(ScrollableThresholdsWidget())
        # self.qscrollarea.setAlignment(Qt.AlignCenter)  # center in scroll area - maybe undesirable
        self.qscrollarea.setWidgetResizable(True)

        self.layout = QVBoxLayout()
        self.layout.addWidget(self.qscrollarea)
        self.setLayout(self.layout)

class ScrollableThresholdsWidget(QFrame):
    def __init__(self):
        super().__init__()
        self.threshold_steal = SliderBoxSetting("Stealing", "Comparisons that score below this will be stored so you can view them",
                "threshold_cheat", 30)
        self.threshold_display = SliderBoxSetting("Stealing Display", "Comparisons that score below this will be printed to the console",
                "threshold_display", 100)
        self.grid = QVBoxLayout()
        self.grid.addWidget(self.threshold_steal)
        self.grid.addWidget(self.threshold_display)
        self.grid.setAlignment(Qt.AlignTop)
        self.setLayout(self.grid)

def switch_theme(dark, accent=QColor(71, 174, 247)):
    set_setting("dark_theme", dark)
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
    # app is initialized at the top of the file
    WINDOW = WindowWrapper(app.clipboard())
    set_event_window(WINDOW)
    WINDOW.resize(900, 750)
    WINDOW.show()
    if not get_setting("ran"):
        welcome = wizard.WelcomeWindow()
        welcome.show()
        set_setting("ran", True)

    app.lastWindowClosed.connect(WINDOW.cancel_all_runs)
    app.aboutToQuit.connect(WINDOW.on_application_quit)
    app.exec_()
