import os
import sys
import time
import hashlib
import bz2
import lzma
import re
import threading
from urllib.parse import urljoin

# PyQt5 平台插件路径修复（PyInstaller 打包时指向 _MEIPASS）
def _qt_plugin_path():
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "PyQt5", "Qt5", "plugins")
    project_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(
        project_dir, ".venv", "Lib", "site-packages", "PyQt5", "Qt5", "plugins",
    )

_qp = _qt_plugin_path()
if os.path.isdir(_qp):
    os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _qp)

from PyQt5.QtCore import (QObject, QThread, pyqtSignal, Qt, QSize,
                          QTimer, QUrl)
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QGroupBox, QLabel,
                             QLineEdit, QPushButton, QCheckBox, QRadioButton,
                             QButtonGroup, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QProgressBar, QPlainTextEdit,
                             QScrollArea, QFrame, QComboBox, QDialog,
                             QMessageBox, QSizePolicy)
from PyQt5.QtGui import QFont, QIcon
import urllib3
import requests
from remotezip import RemoteZip
from payload_extractor import (
    parse_header, list_partitions, get_zip_ranges_for_partitions,
    _write_extents, PayloadError, UnsupportedOpError,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_RETRIES = 5
RETRY_DELAYS = [5, 10, 20, 40, 60]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
}
OTA_URL = "https://developers.google.cn/android/ota"
HTTP_TIMEOUT = 60
HEADER_DL_SIZE = 5 * 1024 * 1024
LOLINET_BASE = "https://mirrors.lolinet.com/firmware"


def resource_path(name):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


class NetworkWorker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
    list_result = pyqtSignal(list)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class _MainInvoker(QObject):
    """Run callables on the GUI thread via an always-thread-safe signal.

    QTimer.singleShot cannot be used from threads without a Qt event loop
    (plain threading.Thread) and from QThread workers the callback fires on
    the wrong thread. A queued signal emitted into the main thread is the
    reliable way to marshal UI updates back to the GUI thread.
    """

    _invoke_sig = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._invoke_sig.connect(self._dispatch, Qt.QueuedConnection)

    def _dispatch(self, fn):
        # Runs on the GUI thread.
        fn()


class FirmwareExtractorGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("固件远程提取工具")
        self.resize(900, 780)
        try:
            self.setWindowIcon(QIcon(resource_path("app.ico")))
        except Exception:
            pass

        central = QWidget()
        self.setCentralWidget(central)
        self.main_layout = QVBoxLayout(central)
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.main_layout.setSpacing(8)

        self._build_top_bar()
        self._build_browser()
        self._build_url_row()
        self._build_quick_checks()
        self._build_file_list()
        self._build_progress()
        self._build_log()

        self.ota_devices = []
        self._browser_path = {}
        self._manifest_data = None
        self._file_vars = {}
        self._current_worker = None
        self._invoker = _MainInvoker(self)

        self._load_tree_root()

    # ========== UI building ==========

    def _build_top_bar(self):
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.proxy_var = False
        self.chk_proxy = QCheckBox("开启代理")
        self.chk_proxy.toggled.connect(lambda v: setattr(self, "proxy_var", v))
        top_bar.addWidget(self.chk_proxy)

        self.port_entry = QLineEdit("7897")
        self.port_entry.setFixedWidth(60)
        top_bar.addWidget(QLabel("127.0.0.1 :"))
        top_bar.addWidget(self.port_entry)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFrameShadow(QFrame.Sunken)
        top_bar.addWidget(sep)

        top_bar.addWidget(QLabel("固件来源:"))

        self.source_var = "lenomola"
        self.source_group = QButtonGroup()
        for text, val in [("Lenomola (Moto)", "lenomola"),
                          ("Lenowow (Lenovo)", "lenowow"),
                          ("直接链接", "direct")]:
            rb = QRadioButton(text)
            rb.setChecked(val == "lenomola")
            rb.toggled.connect(
                lambda checked, v=val: self._on_source_changed(v) if checked else None
            )
            self.source_group.addButton(rb)
            top_bar.addWidget(rb)

        self.main_layout.addLayout(top_bar)

    def _build_browser(self):
        group = QGroupBox("固件浏览器")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("型号搜索:"))
        self.model_entry = QLineEdit()
        self.model_entry.setFixedWidth(150)
        self.model_entry.returnPressed.connect(self._search_model)
        search_row.addWidget(self.model_entry)
        search_row.addStretch()
        group_layout.addLayout(search_row)

        self.browser_tree = QTreeWidget()
        self.browser_tree.setHeaderLabels(["目录", "大小"])
        self.browser_tree.header().setStretchLastSection(False)
        self.browser_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.browser_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.browser_tree.setColumnWidth(0, 420)
        self.browser_tree.setColumnWidth(1, 120)
        self.browser_tree.setIndentation(20)
        self.browser_tree.setAnimated(True)
        self.browser_tree.itemExpanded.connect(self._on_tree_open)
        self.browser_tree.itemClicked.connect(self._on_tree_click)
        self.browser_tree.itemDoubleClicked.connect(self._on_tree_double)
        group_layout.addWidget(self.browser_tree)

        self.main_layout.addWidget(group)

    def _build_url_row(self):
        url_row = QHBoxLayout()
        url_row.setSpacing(6)

        self.btn_fetch_ota = QPushButton("获取 OTA 列表")
        self.btn_fetch_ota.clicked.connect(self.start_fetch_ota)
        url_row.addWidget(self.btn_fetch_ota)

        self.btn_browser = QPushButton("加载固件")
        self.btn_browser.clicked.connect(self._load_current_firmware_url)
        url_row.addWidget(self.btn_browser)

        self.url_entry = QLineEdit()
        self.url_entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        url_row.addWidget(self.url_entry)

        self.btn_list = QPushButton("解析列表")
        self.btn_list.clicked.connect(self.start_list_task)
        url_row.addWidget(self.btn_list)

        self.btn_run = QPushButton("开始提取")
        self.btn_run.clicked.connect(self.start_extract_task)
        url_row.addWidget(self.btn_run)

        self.main_layout.addLayout(url_row)

    def _build_quick_checks(self):
        quick_row = QHBoxLayout()
        quick_row.setSpacing(12)

        self.var_boot = True
        self.var_init = True
        self.var_vbmeta = False
        self.var_vendor = False

        for text, var_attr in [("boot.img", "var_boot"),
                               ("init_boot.img", "var_init"),
                               ("vbmeta.img", "var_vbmeta"),
                               ("vendor_boot.img", "var_vendor")]:
            cb = QCheckBox(text)
            cb.setChecked(getattr(self, var_attr))
            cb.stateChanged.connect(
                lambda state, attr=var_attr: setattr(self, attr, state == Qt.Checked)
            )
            quick_row.addWidget(cb)

        self.main_layout.addLayout(quick_row)

    def _build_file_list(self):
        self.file_list_status = QLabel("")
        self.main_layout.addWidget(self.file_list_status)

        self.main_layout.addWidget(QLabel("文件列表:"))

        self.file_scroll = QScrollArea()
        self.file_scroll.setWidgetResizable(True)
        self.file_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.file_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.file_scroll.setFrameShape(QFrame.StyledPanel)

        self.file_inner = QWidget()
        self.file_grid = QGridLayout(self.file_inner)
        self.file_grid.setSpacing(4)
        self.file_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.file_scroll.setWidget(self.file_inner)

        self.main_layout.addWidget(self.file_scroll, stretch=1)

    def _build_progress(self):
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.main_layout.addWidget(self.progress)

    def _build_log(self):
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setFont(QFont("Consolas", 9))
        self.log_area.setMaximumHeight(120)
        self.log_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.main_layout.addWidget(self.log_area)

    # ========== Network helpers ==========

    def get_proxies(self):
        if self.proxy_var:
            port = self.port_entry.text().strip()
            return {
                "http": f"http://127.0.0.1:{port}",
                "https": f"http://127.0.0.1:{port}",
            }
        return None

    def get_session(self):
        proxies = self.get_proxies()
        s = requests.Session()
        s.headers.update(HEADERS)
        s.verify = False
        s.trust_env = False
        if proxies:
            s.proxies.update(proxies)
        return s

    def log(self, msg):
        if QThread.currentThread() is self.thread():
            self.log_area.appendPlainText(msg)
        else:
            self._invoker._invoke_sig.emit(
                lambda m=msg: self.log_area.appendPlainText(m)
            )

    def invoke_main(self, fn):
        """Run `fn` on the GUI thread; safe to call from any worker thread."""
        self._invoker._invoke_sig.emit(fn)

    # ========== Worker thread management ==========

    def _start_worker(self, fn, *args, **kwargs):
        worker = NetworkWorker(fn, *args, **kwargs)
        thread = QThread()
        worker.moveToThread(thread)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        worker.error.connect(lambda msg: self.log(f"错误: {msg}"))
        worker.log.connect(self.log)
        worker.progress.connect(lambda p: self.progress.setValue(int(p)))
        thread.started.connect(worker.run)
        self._current_worker = worker
        self._current_thread = thread
        thread.start()
        return worker

    # ========== File list grid ==========

    def _populate_grid(self, items, var_dict):
        while self.file_grid.count():
            item = self.file_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        var_dict.clear()

        items = sorted(items, key=lambda t: t[1].lower())
        n = len(items)
        if n == 0:
            self.file_list_status.setText("")
            return 0, 0

        total_size = sum(s for _, _, s in items if s is not None)

        # 单列布局：一项一行，从上到下，竖向滚动交给 QScrollArea
        for i, (key, label, _) in enumerate(items):
            cb = QCheckBox(label)
            cb.stateChanged.connect(
                lambda state, k=key: var_dict.__setitem__(k, state == Qt.Checked)
            )
            self.file_grid.addWidget(cb, i, 0)
            var_dict[key] = False

        if total_size > 0:
            self.file_list_status.setText(
                f"共 {n} 个, {total_size / 1024 / 1024:.1f} MB"
            )
        else:
            self.file_list_status.setText(f"共 {n} 个")

        return n, total_size

    # ========== h5ai directory listing ==========

    def fetch_dir_listing(self, url):
        session = self.get_session()
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        dirs = []
        files = []
        skip = {"..", "Parent Directory", "powered by h5ai", "modern browsers"}
        for match in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.IGNORECASE
        ):
            href = match.group(1)
            name = match.group(2).strip()
            if name in skip:
                continue
            if href.endswith("/"):
                dirname = name.rstrip("/")
                if dirname:
                    dirs.append((dirname, urljoin(url, href)))
            elif href.endswith(".zip"):
                files.append((name, urljoin(url, href)))
        return sorted(set(dirs)), sorted(set(files), key=lambda x: x[0].lower())

    # ========== Firmware browser (tree, lazy) ==========

    def _load_tree_root(self):
        source = self.source_var
        if source == "direct":
            return
        base = f"{LOLINET_BASE}/{source}/"
        root = QTreeWidgetItem(self.browser_tree, [source, ""])
        root.setData(0, Qt.UserRole, base)
        self._browser_path[id(root)] = base
        self._fetch_tree_children(root)

    def _fetch_tree_children(self, item):
        url = self._browser_path.get(id(item))
        if not url:
            return
        if item.childCount() > 0:
            return
        threading.Thread(
            target=self._tree_fetch_thread, args=(item, url), daemon=True
        ).start()

    def _tree_fetch_thread(self, item, url):
        try:
            dirs, files = self.fetch_dir_listing(url)
        except Exception as e:
            self.log(f"加载目录失败: {e}")
            return
        self.invoke_main(lambda: self._tree_insert_children(item, dirs, files))

    def _tree_insert_children(self, item, dirs, files):
        if item.childCount() > 0:
            return
        for name, href in dirs:
            child = QTreeWidgetItem(item, [name + "/", ""])
            child.setData(0, Qt.UserRole, href)
            self._browser_path[id(child)] = href
        for name, href in files:
            fname = href.rstrip("/").split("/")[-1]
            child = QTreeWidgetItem(item, [fname, ""])
            child.setData(0, Qt.UserRole, href)
            self._browser_path[id(child)] = href

    def _on_tree_open(self, item):
        self._fetch_tree_children(item)

    def _on_tree_click(self, item, column):
        """单击即生效：zip 填链接，目录展开并懒加载下一级。"""
        href = item.data(0, Qt.UserRole)
        if not href:
            return
        if href.endswith(".zip"):
            self.url_entry.setText(href)
            self.log(f"固件 URL 已填充: {href}")
        else:
            # setExpanded 触发 itemExpanded -> 懒加载子级；已展开时为空操作
            item.setExpanded(True)

    def _on_tree_double(self, item, column):
        href = item.data(0, Qt.UserRole)
        if href and href.endswith(".zip"):
            self.url_entry.setText(href)
            self.log(f"固件 URL 已填充: {href}")

    def _load_current_firmware_url(self):
        items = self.browser_tree.selectedItems()
        if not items:
            self.log("请先在固件浏览器中选中一个固件")
            return
        href = items[0].data(0, Qt.UserRole)
        if href and href.endswith(".zip"):
            self.url_entry.setText(href)
            self.log(f"固件 URL 已填充: {href}")
        else:
            self.log("当前选中的是目录，请展开后选择 zip 固件文件")

    def _search_model(self):
        query = self.model_entry.text().strip()
        if not query:
            return
        source = self.source_var
        if source == "direct":
            self.log("请先选择 Lenomola 或 Lenowow 来源")
            return
        q = query.lower()
        matches = []
        for i in range(self.browser_tree.topLevelItemCount()):
            self._search_tree_node(self.browser_tree.topLevelItem(i), q, matches)
        if matches:
            self.browser_tree.scrollToItem(matches[0])
            self.browser_tree.setCurrentItem(matches[0])
            self.log(
                f"型号搜索: '{query}' → 找到 {len(matches)} 个匹配, 已选中第一个"
            )
        else:
            self.log(
                f"型号搜索: '{query}' 未找到匹配。请先展开年份→设备目录"
            )

    def _search_tree_node(self, item, q, matches):
        text = item.text(0).lower()
        if q in text:
            matches.append(item)
        for i in range(item.childCount()):
            self._search_tree_node(item.child(i), q, matches)

    def _on_source_changed(self, source):
        self.source_var = source
        self._browser_path = {}
        self.browser_tree.clear()
        if source != "direct":
            self._load_tree_root()

    # ========== Pixel OTA ==========

    def start_fetch_ota(self):
        self.btn_fetch_ota.setEnabled(False)
        self._start_worker(self.fetch_ota_list)

    def fetch_ota_list(self):
        try:
            self.log("正在获取 OTA 列表...")
            resp = self.get_session().get(OTA_URL, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                self.log(f"获取失败: HTTP {resp.status_code}")
                return
            html = resp.text
            devices = self._parse_ota_page(html)
            if not devices:
                self.log("未找到 OTA 下载链接")
                return
            self.ota_devices = devices
            self.log(
                f"找到 {len(devices)} 个机型, 共 {sum(len(d['versions']) for d in devices)} 个版本"
            )
            self.invoke_main(lambda: self._show_ota_list_dialog(devices))
        except Exception as e:
            self.log(f"获取 OTA 列表失败: {e}")
        finally:
            self.invoke_main(lambda: self.btn_fetch_ota.setEnabled(True))

    def _parse_ota_page(self, html):
        h2s = list(re.finditer(r'<h2[^>]*id="([a-zA-Z0-9_]+)"[^>]*>', html))
        devices = []
        for i, h in enumerate(h2s):
            cid = h.group(1)
            seg = html[h.start() : h.start() + 500]
            dt = re.search(r'data-text="([^"]*)"', seg)
            name = dt.group(1) if dt else cid
            end = h2s[i + 1].start() if i + 1 < len(h2s) else len(html)
            block = html[h.start() : end]
            raw_links = re.findall(
                r"https://googledownloads\.cn/dl/android/aosp/([a-zA-Z0-9_.\-]+)\.zip",
                block,
            )
            seen = set()
            versions = []
            for fname in raw_links:
                if fname in seen:
                    continue
                seen.add(fname)
                link = f"https://googledownloads.cn/android/aosp/{fname}.zip"
                fparts = fname.split("-")
                ver = "-".join(fparts[1:]) if len(fparts) > 1 else fname
                versions.append((ver, link))
            if versions:
                devices.append({"codename": cid, "name": name, "versions": versions})
        return devices

    def _show_ota_list_dialog(self, devices):
        dialog = QDialog(self)
        dialog.setWindowTitle("OTA 下载链接列表")
        dialog.resize(900, 520)
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        device_combo = QComboBox()
        display_names = [f"{d['name']}  ({d['codename']})" for d in devices]
        device_combo.addItems(display_names)
        layout.addWidget(QLabel("1. 选择机型:"))
        layout.addWidget(device_combo)

        layout.addWidget(QLabel("2. 选择版本:"))

        tree = QTreeWidget()
        tree.setHeaderLabels(["版本", "下载链接"])
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        tree.setColumnWidth(0, 250)
        tree.setColumnWidth(1, 500)
        tree.setIndentation(20)
        tree.setAnimated(True)
        layout.addWidget(tree, stretch=1)

        def refresh_versions():
            tree.clear()
            idx = device_combo.currentIndex()
            if idx < 0 or idx >= len(devices):
                return
            for ver, link in devices[idx]["versions"]:
                QTreeWidgetItem(tree, [ver, link])

        device_combo.currentIndexChanged.connect(refresh_versions)
        refresh_versions()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        def on_select():
            sel = tree.selectedItems()
            if not sel:
                self.log("请先在列表中双击或选择一个版本")
                return
            link = sel[0].text(1)
            self.url_entry.setText(link)
            self.log("已选择链接 (已转换为 /android/ 格式)")
            dialog.accept()

        tree.itemDoubleClicked.connect(lambda item, col: on_select())

        btn_select = QPushButton("选择该版本")
        btn_select.clicked.connect(on_select)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.reject)
        btn_row.addWidget(btn_select)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        dialog.exec_()

    # ========== Parse list ==========

    def start_list_task(self):
        url = self.url_entry.text().strip()
        if not url:
            return
        self.btn_list.setEnabled(False)
        self._start_worker(self.list_files, url)

    def list_files(self, url):
        try:
            self.log(f"正在连接: {url}")
            session = self.get_session()

            for attempt in range(MAX_RETRIES):
                try:
                    with RemoteZip(url, session=session) as rz:
                        all_files = rz.namelist()
                        self.log(
                            f"ZIP 内文件: {[f for f in all_files if not f.startswith('META-INF')]}"
                        )
                        if "payload.bin" in all_files:
                            info = rz.getinfo("payload.bin")
                            fname_bytes = info.filename.encode("utf-8")
                            payload_zip_offset = (
                                info.header_offset + 30 + len(fname_bytes) + len(info.extra)
                            )
                            self.log(f"payload.bin: offset={payload_zip_offset}")

                            resp = session.get(
                                url,
                                headers={
                                    **HEADERS,
                                    "Range": f"bytes={payload_zip_offset}-{payload_zip_offset + HEADER_DL_SIZE - 1}",
                                },
                                timeout=HTTP_TIMEOUT,
                            )
                            dam, data_offset, version = parse_header(resp.content)
                            self.log(
                                f"Version: {version}, block_size: {dam.block_size}, {len(dam.partitions)} partitions"
                            )

                            partitions = list_partitions(dam)
                            items = [
                                (
                                    p["name"],
                                    f"{p['name']}  ({p['size_mb']:.1f} MB, {p['ops_count']} ops)",
                                    None,
                                )
                                for p in partitions
                            ]
                            self.invoke_main(lambda it=items: self._populate_grid(it, self._file_vars))
                            self.log(f"解析成功，共 {len(partitions)} 个分区")
                        else:
                            items = []
                            for f in sorted(all_files, key=lambda s: s.lower()):
                                f_info = rz.getinfo(f)
                                size_mb = f_info.file_size / 1024 / 1024
                                label = f"{f}  ({size_mb:.1f} MB)"
                                items.append((f, label, f_info.file_size))
                            self.invoke_main(lambda it=items: self._populate_grid(it, self._file_vars))
                            self.log(f"解析成功，共 {len(all_files)} 个文件")
                    return
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str and attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        self.log(
                            f"被限流，等待 {delay}s 后重试 ({attempt+1}/{MAX_RETRIES})..."
                        )
                        time.sleep(delay)
                    else:
                        raise
        except Exception as e:
            self.log(f"解析失败: {e}")
        finally:
            self.invoke_main(lambda: self.btn_list.setEnabled(True))

    # ========== Extract ==========

    def start_extract_task(self):
        url = self.url_entry.text().strip()
        if not url:
            self.log("请先输入或选择固件链接")
            return
        targets = []
        if self.var_boot:
            targets.append("boot.img")
        if self.var_init:
            targets.append("init_boot.img")
        if self.var_vbmeta:
            targets.append("vbmeta.img")
        if self.var_vendor:
            targets.append("vendor_boot.img")
        for filename, var in self._file_vars.items():
            if var:
                targets.append(filename)
        targets = list(set(targets))
        if not targets:
            self.log("请至少选择一个文件")
            return
        self.btn_run.setEnabled(False)
        self.progress.setValue(0)
        self._start_worker(self.extract_process, url, targets)

    def extract_process(self, url, targets):
        try:
            clean_url = url.split("?")[0]
            zip_name = clean_url.split("/")[-1]
            folder_name = zip_name.replace(".zip", "").replace(".tgz", "")
            for c in '<>:"/\\|?*':
                folder_name = folder_name.replace(c, "_")

            save_dir = os.path.join(os.getcwd(), "extracted_images")
            final_path = os.path.join(save_dir, folder_name)
            os.makedirs(final_path, exist_ok=True)

            session = self.get_session()

            if self._manifest_data and self._manifest_data.get("url") == url:
                self._extract_payload(session, final_path, targets)
                return

            try:
                with RemoteZip(url, session=session) as rz:
                    if "payload.bin" in rz.namelist():
                        info = rz.getinfo("payload.bin")
                        fname_bytes = info.filename.encode("utf-8")
                        payload_zip_offset = (
                            info.header_offset + 30 + len(fname_bytes) + len(info.extra)
                        )
                        resp = session.get(
                            url,
                            headers={
                                **HEADERS,
                                "Range": f"bytes={payload_zip_offset}-{payload_zip_offset + HEADER_DL_SIZE - 1}",
                            },
                            timeout=HTTP_TIMEOUT,
                        )
                        dam, data_offset, version = parse_header(resp.content)
                        self._manifest_data = {
                            "url": url,
                            "payload_zip_offset": payload_zip_offset,
                            "data_offset": data_offset,
                            "dam": dam,
                        }
                        self._extract_payload(session, final_path, targets)
                        return
            except Exception:
                pass

            for attempt in range(MAX_RETRIES):
                try:
                    with RemoteZip(url, session=session) as rz:
                        all_files = rz.namelist()
                        found_files = [f for f in targets if f in all_files]
                        if not found_files:
                            self.log("包内未找到所选文件")
                            return

                        for index, f in enumerate(found_files):
                            self.log(f"提取: {f}")
                            with rz.open(f) as src, open(
                                os.path.join(final_path, f), "wb"
                            ) as dst:
                                f_info = rz.getinfo(f)
                                total_size = f_info.file_size
                                downloaded = 0
                                while True:
                                    chunk = src.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    downloaded += len(chunk)
                                    percent = (
                                        (index / len(found_files))
                                        + (downloaded / total_size / len(found_files))
                                    ) * 100
                                    self.invoke_main(lambda p=percent: self.progress.setValue(int(p)))
                            self.log("   [完成]")
                    self.invoke_main(
                        lambda: QMessageBox.information(
                            self, "成功", f"文件已保存至: {folder_name}"
                        )
                    )
                    try:
                        os.startfile(final_path)
                    except Exception:
                        pass
                    return
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str and attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        self.log(
                            f"被限流，等待 {delay}s 后重试 ({attempt+1}/{MAX_RETRIES})..."
                        )
                        time.sleep(delay)
                    else:
                        raise
        except UnsupportedOpError as e:
            self.log(f"不支持的 OTA 类型: {e}")
            self.log("提示: 仅支持 Full OTA (完整包)，增量包不支持。")
        except Exception as e:
            self.log(f"提取错误: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.invoke_main(lambda: self.btn_run.setEnabled(True))
            self.invoke_main(lambda: self.progress.setValue(100))

    def _extract_payload(self, session, final_path, targets):
        try:
            md = self._manifest_data
            url = md["url"]
            payload_zip_offset = md["payload_zip_offset"]
            data_offset = md["data_offset"]
            dam = md["dam"]
            folder_name = os.path.basename(os.path.normpath(final_path))

            available = [p.partition_name for p in dam.partitions]
            to_extract = [
                t.replace(".img", "") for t in targets if t.replace(".img", "") in available
            ]
            if not to_extract:
                self.log(f"所选分区均未找到。可用: {available}")
                return

            self.log(f"目标分区: {to_extract}")
            self.log("正在计算数据范围...")

            zip_ranges = get_zip_ranges_for_partitions(
                to_extract, dam, data_offset, payload_zip_offset
            )
            total_dl = sum(e - s + 1 for s, e, _ in zip_ranges)
            self.log(
                f"需要下载 {len(zip_ranges)} 个数据段, 共 {total_dl / 1024 / 1024:.1f} MB"
            )

            downloaded = []
            for i, (r_start, r_end, part_names) in enumerate(zip_ranges):
                pns = ",".join(sorted(part_names))
                size_mb = (r_end - r_start + 1) / 1024 / 1024
                self.log(
                    f"下载数据段 {i+1}/{len(zip_ranges)} ({size_mb:.1f} MB) -> [{pns}]"
                )
                for attempt in range(MAX_RETRIES):
                    try:
                        resp = session.get(
                            url,
                            headers={**HEADERS, "Range": f"bytes={r_start}-{r_end}"},
                            stream=True,
                            timeout=(HTTP_TIMEOUT, 120),
                        )
                        if resp.status_code not in (206, 200):
                            resp.close()
                            raise Exception(f"HTTP {resp.status_code}")
                        expected_len = r_end - r_start + 1
                        chunks = []
                        received = 0
                        for chunk in resp.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            chunks.append(chunk)
                            received += len(chunk)
                            seg_progress = (i + received / expected_len) / len(zip_ranges) * 50
                            self.invoke_main(
                                lambda p=seg_progress: self.progress.setValue(int(p))
                            )
                        resp.close()
                        data = b"".join(chunks)
                        if len(data) != expected_len:
                            raise Exception("下载长度不匹配")
                        break
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                            self.log(
                                f"下载失败，{delay}s 后重试 ({attempt+1}/{MAX_RETRIES})..."
                            )
                            time.sleep(delay)
                        else:
                            raise
                downloaded.append({"data": data, "zip_start": r_start, "zip_end": r_end})

            self.log("正在提取分区...")
            total = len(to_extract)

            for idx, part_name in enumerate(to_extract):
                part = None
                for p in dam.partitions:
                    if p.partition_name == part_name:
                        part = p
                        break
                if part is None:
                    continue

                out_path = os.path.join(final_path, f"{part.partition_name}.img")
                block_size = dam.block_size

                with open(out_path, "wb") as out_file:
                    op_count = len(part.operations)
                    for op_idx, op in enumerate(part.operations):
                        if op.type in (0, 1, 8, 14):
                            payload_abs = payload_zip_offset + data_offset + op.data_offset
                            op_end = payload_abs + op.data_length - 1

                            found = False
                            for dl in downloaded:
                                buf_start = dl["zip_start"]
                                buf_end = dl["zip_end"]
                                if buf_start <= payload_abs <= buf_end and buf_start <= op_end <= buf_end:
                                    local_offset = payload_abs - buf_start
                                    compressed = dl["data"][local_offset : local_offset + op.data_length]
                                    if op.data_sha256_hash and hashlib.sha256(compressed).digest() != op.data_sha256_hash:
                                        raise PayloadError(f"SHA256 mismatch at op {op_idx}")
                                    found = True
                                    break

                            if not found:
                                raise PayloadError(f"Data for op {op_idx} not found")

                            if op.type == 0:
                                _write_extents(out_file, compressed, op.dst_extents, block_size)
                            elif op.type == 1:
                                _write_extents(
                                    out_file, bz2.BZ2Decompressor().decompress(compressed), op.dst_extents, block_size
                                )
                            elif op.type == 8:
                                _write_extents(
                                    out_file, lzma.LZMADecompressor().decompress(compressed), op.dst_extents, block_size
                                )
                            elif op.type == 14:
                                import zstandard

                                dec = zstandard.ZstdDecompressor().decompressobj()
                                _write_extents(
                                    out_file, dec.decompress(compressed), op.dst_extents, block_size
                                )

                        elif op.type == 6:
                            for ext in op.dst_extents:
                                out_file.seek(ext.start_block * block_size)
                                out_file.write(b"\x00" * ext.num_blocks * block_size)
                        elif op.type == 7:
                            pass
                        else:
                            raise UnsupportedOpError(f"不支持的 op 类型 {op.type}")

                        op_progress = 50 + ((idx / total) + (op_idx / op_count / total)) * 50
                        self.invoke_main(
                            lambda p=op_progress: self.progress.setValue(int(p))
                        )

                self.log(f"  {part_name}.img -> {out_path}")

            self.log(f"\n提取完成! 保存至: {final_path}")
            self.invoke_main(
                lambda: QMessageBox.information(
                    self, "完成", f"提取完成!\n保存至: {folder_name}"
                )
            )
            try:
                os.startfile(final_path)
            except Exception:
                pass

        except UnsupportedOpError as e:
            self.log(f"不支持的 OTA 类型: {e}")
        except Exception as e:
            self.log(f"payload 提取错误: {e}")

    def closeEvent(self, event):
        # _current_worker is a NetworkWorker (QObject) which has no quit()/wait();
        # the QThread is stored in _current_thread.
        thread = getattr(self, "_current_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait(2000)
        event.accept()


if __name__ == "__main__":
    import traceback as _tb
    try:
        app = QApplication(sys.argv)
        window = FirmwareExtractorGUI()
        window.show()
        sys.exit(app.exec_())
    except Exception:
        try:
            with open(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "extractor_error.log"
                ),
                "w",
                encoding="utf-8",
            ) as f:
                _tb.print_exc(file=f)
        except Exception:
            pass
        raise
