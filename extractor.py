import os
import sys
import time
import hashlib
import bz2
import lzma
import re
import threading
from urllib.parse import urljoin
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
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


class FirmwareExtractorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("固件远程提取工具")
        self.root.geometry("900x780")
        try:
            self.root.iconbitmap(resource_path("pixel-logo.ico"))
        except Exception:
            pass

        main_frame = ttk.Frame(root, padding="20")
        main_frame.pack(fill="both", expand=True)

        # ── 网络设置 + 固件来源 (同一行) ──
        top_bar = ttk.Frame(main_frame)
        top_bar.pack(fill="x", pady=(0, 8))
        self.proxy_var = tk.BooleanVar(value=False)
        self.chk_proxy = ttk.Checkbutton(top_bar, text="开启代理", variable=self.proxy_var)
        self.chk_proxy.pack(side="left", padx=(0, 5))
        ttk.Label(top_bar, text="127.0.0.1 :").pack(side="left")
        self.port_entry = ttk.Entry(top_bar, width=8)
        self.port_entry.pack(side="left")
        self.port_entry.insert(0, "7897")

        ttk.Separator(top_bar, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Label(top_bar, text="固件来源:").pack(side="left", padx=(0, 5))
        self.source_var = tk.StringVar(value="lenomola")
        ttk.Radiobutton(top_bar, text="Lenomola (Moto)", variable=self.source_var, value="lenomola", command=self._on_source_changed).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(top_bar, text="Lenowow (Lenovo)", variable=self.source_var, value="lenowow", command=self._on_source_changed).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(top_bar, text="直接链接", variable=self.source_var, value="direct", command=self._on_source_changed).pack(side="left")

        # ── 固件浏览器 ──
        self.browser_frame = ttk.LabelFrame(main_frame, text="固件浏览器", padding=10)
        self.browser_frame.pack(fill="x", pady=5)

        # 型号搜索
        search_frame = ttk.Frame(self.browser_frame)
        search_frame.pack(fill="x", pady=(0, 5))
        ttk.Label(search_frame, text="型号搜索:").pack(side="left", padx=(0, 5))
        self.model_var = tk.StringVar()
        self.model_entry = ttk.Entry(search_frame, textvariable=self.model_var, width=18)
        self.model_entry.pack(side="left")
        self.model_entry.bind("<Return>", lambda e: self._search_model())

        # 目录树 (懒加载)
        tree_container = ttk.Frame(self.browser_frame)
        tree_container.pack(fill="x", pady=(5, 0))
        self.browser_tree = ttk.Treeview(tree_container, columns=("size",), show="tree headings", height=6)
        self.browser_tree.heading("#0", text="目录")
        self.browser_tree.heading("size", text="大小")
        self.browser_tree.column("#0", width=420)
        self.browser_tree.column("size", width=120, anchor="e")
        self.browser_tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.browser_tree.bind("<Double-1>", self._on_tree_double)
        tree_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.browser_tree.yview)
        self.browser_tree.configure(yscrollcommand=tree_scroll.set)
        self.browser_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # ── URL 输入 ──
        ttk.Label(main_frame, text="链接:").pack(anchor="w", pady=(10, 0))
        url_frame = ttk.Frame(main_frame)
        url_frame.pack(fill="x", pady=5)
        self.btn_fetch_ota = ttk.Button(url_frame, text="获取 OTA 列表", command=self.start_fetch_ota)
        self.btn_fetch_ota.pack(side="left", padx=(0, 5))
        self.btn_browser = ttk.Button(url_frame, text="加载固件", command=self._load_current_firmware_url)
        self.btn_browser.pack(side="left", padx=(0, 5))
        self.url_entry = ttk.Entry(url_frame)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.btn_list = ttk.Button(url_frame, text="解析列表", command=self.start_list_task)
        self.btn_list.pack(side="right")

        # ── 快捷勾选 ──
        ttk.Label(main_frame, text="快捷勾选:").pack(anchor="w", pady=(10, 0))
        quick_frame = ttk.Frame(main_frame)
        quick_frame.pack(fill="x")
        self.var_boot = tk.BooleanVar(value=True)
        self.var_init = tk.BooleanVar(value=True)
        self.var_vbmeta = tk.BooleanVar(value=False)
        self.var_vendor = tk.BooleanVar(value=False)
        ttk.Checkbutton(quick_frame, text="boot.img", variable=self.var_boot).pack(side="left", padx=10)
        ttk.Checkbutton(quick_frame, text="init_boot.img", variable=self.var_init).pack(side="left", padx=10)
        ttk.Checkbutton(quick_frame, text="vbmeta.img", variable=self.var_vbmeta).pack(side="left", padx=10)
        ttk.Checkbutton(quick_frame, text="vendor_boot.img", variable=self.var_vendor).pack(side="left", padx=10)

        # ── 文件列表状态 ──
        self.file_list_status = ttk.Label(main_frame, text="")
        self.file_list_status.pack(anchor="w", pady=(10, 0))

        # ── 文件列表 ──
        ttk.Label(main_frame, text="文件列表:").pack(anchor="w", pady=(5, 0))
        list_container = ttk.Frame(main_frame)
        list_container.pack(fill="x")
        self.list_canvas = tk.Canvas(list_container, background="white", highlightthickness=1, height=200)
        self.scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.list_canvas.yview)
        self.scrollbar_x = ttk.Scrollbar(list_container, orient="horizontal", command=self.list_canvas.xview)
        self.scrollable_frame = ttk.Frame(self.list_canvas)
        self._canvas_window = self.list_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.list_canvas.configure(yscrollcommand=self.scrollbar.set, xscrollcommand=self.scrollbar_x.set)
        self.list_canvas.pack(side="top", fill="both", expand=True, pady=(0, 0))
        self.scrollbar.pack(side="right", fill="y")
        self.scrollbar_x.pack(side="bottom", fill="x")
        self.list_canvas.bind("<Configure>", lambda e: self.list_canvas.itemconfig(self._canvas_window, width=max(e.width, self._grid_req_width)))
        self.scrollable_frame.bind("<Configure>", lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind("<Enter>", lambda e: self.list_canvas.bind_all("<MouseWheel>", lambda ev: self.list_canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")))
        self.list_canvas.bind("<Leave>", lambda e: self.list_canvas.unbind_all("<MouseWheel>"))

        self.file_vars = {}
        self._grid_req_width = 0
        self._manifest_data = None

        # ── 进度 ──
        self.progress = ttk.Progressbar(main_frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=10)

        # ── 提取按钮 ──
        self.btn_run = ttk.Button(main_frame, text="开始提取", command=self.start_extract_task)
        self.btn_run.pack(pady=5)

        # ── 日志 ──
        self.log_text = tk.Text(main_frame, height=4, state="disabled", font=("Consolas", 9), background="#f8f8f8")
        self.log_text.pack(fill="x", pady=5)

        self.ota_devices = []
        self._browser_path = {}

        self._on_source_changed()

    # ========== 网络层 ==========

    def get_proxies(self):
        if self.proxy_var.get():
            port = self.port_entry.get().strip()
            return {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
        return None

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def get_session(self):
        proxies = self.get_proxies()
        s = requests.Session()
        s.headers.update(HEADERS)
        s.verify = False
        s.trust_env = False
        if proxies:
            s.proxies.update(proxies)
        return s

    # ========== 文件列表网格 ==========

    def _populate_grid(self, items, var_dict):
        """items: [(key, label, size_bytes), ...]  size_bytes=None 表示大小已在标签中
        返回 (count, total_size)"""
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        var_dict.clear()

        items = sorted(items, key=lambda t: t[1].lower())
        n = len(items)
        if n == 0:
            self.file_list_status.config(text="")
            return 0, 0

        total_size = sum(s for _, _, s in items if s is not None)

        font = tkfont.nametofont("TkDefaultFont")
        max_label_w = max(font.measure(label) for _, label, _ in items)
        canvas_w = self.list_canvas.winfo_width()
        if canvas_w < 50:
            canvas_w = self.list_canvas.winfo_reqwidth()
        # 每列至少能容纳最长标签，列数随窗口宽度自适应
        cols = max(1, canvas_w // (max_label_w + 28))
        cols = min(cols, 6, n)
        rows = (n + cols - 1) // cols

        for i, (key, label, _) in enumerate(items):
            var = tk.BooleanVar(value=False)
            r, c = i % rows, i // rows
            ttk.Checkbutton(self.scrollable_frame, text=label, variable=var).grid(
                row=r, column=c, sticky="ew", padx=8, pady=1)
            var_dict[key] = var

        for c in range(cols):
            self.scrollable_frame.columnconfigure(c, weight=1, uniform="chk")

        # 记录内容所需宽度，让 canvas 窗口可超出可视区（靠横向滚动条查看）
        self._grid_req_width = cols * (max_label_w + 40)
        self.list_canvas.itemconfig(self._canvas_window, width=max(self._grid_req_width, self.list_canvas.winfo_width()))

        if total_size > 0:
            self.file_list_status.config(text=f"共 {n} 个, {total_size / 1024 / 1024:.1f} MB")
        else:
            self.file_list_status.config(text=f"共 {n} 个")

        return n, total_size

    # ========== h5ai 目录解析 ==========

    def fetch_dir_listing(self, url):
        """获取 h5ai 目录列表，返回 (dirs: [(name, href)], files: [(name, href)])"""
        session = self.get_session()
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        dirs = []
        files = []
        skip = {"..", "Parent Directory", "powered by h5ai", "modern browsers"}
        for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.IGNORECASE):
            href = match.group(1)
            name = match.group(2).strip()
            if name in skip:
                continue
            if href.endswith("/"):
                dirname = name.rstrip("/")
                if dirname:
                    dirs.append((dirname, href))
            elif href.endswith(".zip"):
                files.append((name, href))
        return sorted(set(dirs)), sorted(set(files), key=lambda x: x[0].lower())

    # ========== 固件浏览器 (目录树懒加载) ==========

    def _on_source_changed(self):
        source = self.source_var.get()
        self._current_source = source
        self._browser_path = {}
        self.browser_tree.delete(*self.browser_tree.get_children())

        if source == "direct":
            self.browser_frame.pack_forget()
        else:
            self.browser_frame.pack(fill="x", pady=5)
            self._load_tree_root()

    def _load_tree_root(self):
        source = self.source_var.get()
        if source == "direct":
            return
        base = f"{LOLINET_BASE}/{source}/"
        root_iid = self.browser_tree.insert("", "end", text=source, open=True)
        self._browser_path[root_iid] = base
        threading.Thread(target=self._browser_fetch_children, args=(root_iid,), daemon=True).start()

    def _browser_fetch_children(self, iid):
        """后台线程获取指定节点子项"""
        url = self._browser_path.get(iid)
        if not url:
            return
        try:
            dirs, files = self.fetch_dir_listing(url)
        except Exception as e:
            self.root.after(0, lambda: self.log(f"加载目录失败: {e}"))
            return
        self.root.after(0, lambda: self._browser_insert_children(iid, url, dirs, files))

    def _browser_insert_children(self, iid, base_url, dirs, files):
        if not self.browser_tree.exists(iid) or self.browser_tree.get_children(iid):
            return
        for name, href in dirs:
            sub = self.browser_tree.insert(iid, "end", text=name + "/", values=("",))
            self._browser_path[sub] = urljoin(base_url, href)
        for name, href in files:
            fname = href.rstrip("/").split("/")[-1]
            sub = self.browser_tree.insert(iid, "end", text=fname, values=("",))
            self._browser_path[sub] = urljoin(base_url, href)

    def _browser_tree_expand(self, iid):
        if not self.browser_tree.exists(iid) or self.browser_tree.get_children(iid):
            return
        threading.Thread(target=self._browser_fetch_children, args=(iid,), daemon=True).start()

    def _on_tree_open(self, event):
        iid = self.browser_tree.focus()
        self._browser_tree_expand(iid)

    def _on_tree_double(self, event):
        iid = self.browser_tree.identify_row(event.y)
        if not iid:
            return
        text = self.browser_tree.item(iid, "text")
        if text.endswith("/"):
            self._browser_tree_expand(iid)
            return
        href = self._browser_path.get(iid)
        if href and href.endswith(".zip"):
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, href)
            self.log(f"固件 URL 已填充: {href}")

    def _load_current_firmware_url(self):
        """手动触发加载当前选中固件节点的 URL"""
        iid = self.browser_tree.focus()
        if not iid:
            self.log("请先在固件浏览器中双击选中一个固件")
            return
        href = self._browser_path.get(iid)
        if href and href.endswith(".zip"):
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, href)
            self.log(f"固件 URL 已填充: {href}")

    def _search_model(self):
        """型号搜索：扫描当前已展开的目录树，找出包含型号的节点"""
        query = self.model_var.get().strip()
        if not query:
            return
        source = self.source_var.get()
        if source == "direct":
            self.log("请先选择 Lenomola 或 Lenowow 来源")
            return
        # 在整棵树中查找包含型号文本的节点
        q = query.lower()
        matches = []
        for iid in self.browser_tree.get_children(""):
            self._search_tree_node(iid, q, matches)
        if matches:
            self.browser_tree.see(matches[0])
            self.browser_tree.selection_set(matches[0])
            self.log(f"型号搜索: '{query}' → 找到 {len(matches)} 个匹配, 已选中第一个")
        else:
            self.log(f"型号搜索: '{query}' 未找到匹配。请先展开年份→设备目录")

    def _search_tree_node(self, iid, q, matches):
        text = self.browser_tree.item(iid, "text").lower()
        if q in text:
            matches.append(iid)
        for child in self.browser_tree.get_children(iid):
            self._search_tree_node(child, q, matches)

    # ========== Pixel OTA 获取 ==========

    def start_fetch_ota(self):
        self.btn_fetch_ota.config(state="disabled")
        threading.Thread(target=self.fetch_ota_list, daemon=True).start()

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
            self.log(f"找到 {len(devices)} 个机型, 共 {sum(len(d['versions']) for d in devices)} 个版本")
            self._show_ota_list_dialog(devices)
        except Exception as e:
            self.log(f"获取 OTA 列表失败: {e}")
        finally:
            self.btn_fetch_ota.config(state="normal")

    def _parse_ota_page(self, html):
        h2s = list(re.finditer(r'<h2[^>]*id="([a-zA-Z0-9_]+)"[^>]*>', html))
        devices = []
        for i, h in enumerate(h2s):
            cid = h.group(1)
            seg = html[h.start():h.start() + 500]
            dt = re.search(r'data-text="([^"]*)"', seg)
            name = dt.group(1) if dt else cid
            end = h2s[i + 1].start() if i + 1 < len(h2s) else len(html)
            block = html[h.start():end]
            raw_links = re.findall(r'https://googledownloads\.cn/dl/android/aosp/([a-zA-Z0-9_.\-]+)\.zip', block)
            seen = set()
            versions = []
            for fname in raw_links:
                if fname in seen:
                    continue
                seen.add(fname)
                link = f"https://googledownloads.cn/android/aosp/{fname}.zip"
                fparts = fname.split('-')
                ver = "-".join(fparts[1:]) if len(fparts) > 1 else fname
                versions.append((ver, link))
            if versions:
                devices.append({"codename": cid, "name": name, "versions": versions})
        return devices

    def _show_ota_list_dialog(self, devices):
        display_names = [f"{d['name']}  ({d['codename']})" for d in devices]

        top = tk.Toplevel(self.root)
        top.title("OTA 下载链接列表")
        top.geometry("900x520")
        top.transient(self.root)
        top.grab_set()
        frame = ttk.Frame(top, padding="10")
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="1. 选择机型:").pack(anchor="w")
        device_var = tk.StringVar(value=display_names[0] if display_names else "")
        device_combo = ttk.Combobox(frame, textvariable=device_var,
                                    values=display_names, state="readonly", width=50)
        device_combo.pack(anchor="w", fill="x", pady=5)

        ttk.Label(frame, text="2. 选择版本:").pack(anchor="w", pady=(10, 0))

        cols = ("版本", "下载链接")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)
        tree.heading("版本", text="版本")
        tree.heading("下载链接", text="下载链接")
        tree.column("版本", width=250)
        tree.column("下载链接", width=500)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True, pady=(5, 0))
        scrollbar.pack(side="right", fill="y", pady=(5, 0))

        def current_device():
            idx = display_names.index(device_var.get())
            return devices[idx]

        def refresh_versions():
            tree.delete(*tree.get_children())
            d = current_device()
            for ver, link in d["versions"]:
                tree.insert("", "end", values=(ver, link))

        device_combo.bind("<<ComboboxSelected>>", lambda e: refresh_versions())
        refresh_versions()

        def on_select():
            sel = tree.selection()
            if not sel:
                self.log("请先在列表中双击或选择一个版本")
                return
            item = tree.item(sel[0])
            link = item["values"][1]
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, link)
            top.destroy()
            self.log("已选择链接 (已转换为 /android/ 格式)")

        tree.bind("<Double-1>", lambda e: on_select())

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=8)
        ttk.Button(btn_frame, text="选择该版本", command=on_select).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="关闭", command=top.destroy).pack(side="right", padx=5)

    # ========== 解析列表 ==========

    def start_list_task(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self.btn_list.config(state="disabled")
        threading.Thread(target=self.list_files, args=(url,), daemon=True).start()

    def list_files(self, url):
        try:
            self.log(f"正在连接: {url}")
            session = self.get_session()

            for attempt in range(MAX_RETRIES):
                try:
                    with RemoteZip(url, session=session) as rz:
                        all_files = rz.namelist()
                        self.log(f"ZIP 内文件: {[f for f in all_files if not f.startswith('META-INF')]}")
                        if "payload.bin" in all_files:
                            info = rz.getinfo("payload.bin")
                            fname_bytes = info.filename.encode("utf-8")
                            payload_zip_offset = info.header_offset + 30 + len(fname_bytes) + len(info.extra)
                            self.log(f"payload.bin: offset={payload_zip_offset}")

                            resp = session.get(
                                url,
                                headers={**HEADERS, "Range": f"bytes={payload_zip_offset}-{payload_zip_offset + HEADER_DL_SIZE - 1}"},
                                timeout=HTTP_TIMEOUT,
                            )
                            dam, data_offset, version = parse_header(resp.content)
                            self.log(f"Version: {version}, block_size: {dam.block_size}, {len(dam.partitions)} partitions")

                            partitions = list_partitions(dam)
                            items = [(p['name'], f"{p['name']}  ({p['size_mb']:.1f} MB, {p['ops_count']} ops)", None) for p in partitions]
                            self._populate_grid(items, self.file_vars)
                            self.log(f"解析成功，共 {len(partitions)} 个分区")
                        else:
                            items = []
                            for f in sorted(all_files, key=lambda s: s.lower()):
                                f_info = rz.getinfo(f)
                                size_mb = f_info.file_size / 1024 / 1024
                                label = f"{f}  ({size_mb:.1f} MB)"
                                items.append((f, label, f_info.file_size))
                            self._populate_grid(items, self.file_vars)
                            self.log(f"解析成功，共 {len(all_files)} 个文件")
                    return
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str and attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        self.log(f"被限流，等待 {delay}s 后重试 ({attempt+1}/{MAX_RETRIES})...")
                        time.sleep(delay)
                    else:
                        raise
        except Exception as e:
            self.log(f"解析失败: {e}")
        finally:
            self.btn_list.config(state="normal")

    # ========== 提取 ==========

    def start_extract_task(self):
        url = self.url_entry.get().strip()
        if not url:
            self.log("请先输入或选择固件链接")
            return
        targets = []
        if self.var_boot.get(): targets.append("boot.img")
        if self.var_init.get(): targets.append("init_boot.img")
        if self.var_vbmeta.get(): targets.append("vbmeta.img")
        if self.var_vendor.get(): targets.append("vendor_boot.img")
        for filename, var in self.file_vars.items():
            if var.get(): targets.append(filename)
        targets = list(set(targets))
        if not targets:
            self.log("请至少选择一个文件")
            return
        self.btn_run.config(state="disabled")
        self.progress["value"] = 0
        threading.Thread(target=self.extract_process, args=(url, targets), daemon=True).start()

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

            # 检查是否是 payload.bin 包
            if self._manifest_data and self._manifest_data.get("url") == url:
                self._extract_payload(session, final_path, targets)
                return

            # 尝试作为 payload.bin 包解析
            try:
                with RemoteZip(url, session=session) as rz:
                    if "payload.bin" in rz.namelist():
                        info = rz.getinfo("payload.bin")
                        fname_bytes = info.filename.encode("utf-8")
                        payload_zip_offset = info.header_offset + 30 + len(fname_bytes) + len(info.extra)
                        resp = session.get(
                            url,
                            headers={**HEADERS, "Range": f"bytes={payload_zip_offset}-{payload_zip_offset + HEADER_DL_SIZE - 1}"},
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

            # 普通 ZIP 文件提取
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
                            with rz.open(f) as src, open(os.path.join(final_path, f), "wb") as dst:
                                f_info = rz.getinfo(f)
                                total_size = f_info.file_size
                                downloaded = 0
                                while True:
                                    chunk = src.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    downloaded += len(chunk)
                                    percent = ((index / len(found_files)) + (downloaded / total_size / len(found_files))) * 100
                                    self.root.after(0, lambda p=percent: self.progress.configure(value=p))
                            self.log(f"   [完成]")
                    messagebox.showinfo("成功", f"文件已保存至: {folder_name}")
                    try:
                        os.startfile(final_path)
                    except Exception:
                        pass
                    return
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str and attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        self.log(f"被限流，等待 {delay}s 后重试 ({attempt+1}/{MAX_RETRIES})...")
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
            self.root.after(0, lambda: self.btn_run.config(state="normal"))
            self.root.after(0, lambda: self.progress.configure(value=100))

    def _extract_payload(self, session, final_path, targets):
        try:
            md = self._manifest_data
            url = md["url"]
            payload_zip_offset = md["payload_zip_offset"]
            data_offset = md["data_offset"]
            dam = md["dam"]
            folder_name = os.path.basename(os.path.normpath(final_path))

            available = [p.partition_name for p in dam.partitions]
            to_extract = [t.replace(".img", "") for t in targets if t.replace(".img", "") in available]
            if not to_extract:
                self.log(f"所选分区均未找到。可用: {available}")
                return

            self.log(f"目标分区: {to_extract}")
            self.log("正在计算数据范围...")

            zip_ranges = get_zip_ranges_for_partitions(to_extract, dam, data_offset, payload_zip_offset)
            total_dl = sum(e - s + 1 for s, e, _ in zip_ranges)
            self.log(f"需要下载 {len(zip_ranges)} 个数据段, 共 {total_dl / 1024 / 1024:.1f} MB")

            downloaded = []
            for i, (r_start, r_end, part_names) in enumerate(zip_ranges):
                pns = ",".join(sorted(part_names))
                size_mb = (r_end - r_start + 1) / 1024 / 1024
                self.log(f"下载数据段 {i+1}/{len(zip_ranges)} ({size_mb:.1f} MB) -> [{pns}]")
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
                            self.root.after(0, lambda p=seg_progress: self.progress.configure(value=p))
                        resp.close()
                        data = b"".join(chunks)
                        if len(data) != expected_len:
                            raise Exception("下载长度不匹配")
                        break
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                            self.log(f"下载失败，{delay}s 后重试 ({attempt+1}/{MAX_RETRIES})...")
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
                                    compressed = dl["data"][local_offset:local_offset + op.data_length]
                                    if op.data_sha256_hash and hashlib.sha256(compressed).digest() != op.data_sha256_hash:
                                        raise PayloadError(f"SHA256 mismatch at op {op_idx}")
                                    found = True
                                    break

                            if not found:
                                raise PayloadError(f"Data for op {op_idx} not found")

                            if op.type == 0:
                                _write_extents(out_file, compressed, op.dst_extents, block_size)
                            elif op.type == 1:
                                _write_extents(out_file, bz2.BZ2Decompressor().decompress(compressed), op.dst_extents, block_size)
                            elif op.type == 8:
                                _write_extents(out_file, lzma.LZMADecompressor().decompress(compressed), op.dst_extents, block_size)
                            elif op.type == 14:
                                import zstandard
                                dec = zstandard.ZstdDecompressor().decompressobj()
                                _write_extents(out_file, dec.decompress(compressed), op.dst_extents, block_size)

                        elif op.type == 6:
                            for ext in op.dst_extents:
                                out_file.seek(ext.start_block * block_size)
                                out_file.write(b"\x00" * ext.num_blocks * block_size)
                        elif op.type == 7:
                            pass
                        else:
                            raise UnsupportedOpError(f"不支持的 op 类型 {op.type}")

                        op_progress = 50 + ((idx / total) + (op_idx / op_count / total)) * 50
                        self.root.after(0, lambda p=op_progress: self.progress.configure(value=p))

                self.log(f"  {part_name}.img -> {out_path}")

            self.log(f"\n提取完成! 保存至: {final_path}")
            messagebox.showinfo("完成", f"提取完成!\n保存至: {folder_name}")
            try:
                os.startfile(final_path)
            except Exception:
                pass

        except UnsupportedOpError as e:
            self.log(f"不支持的 OTA 类型: {e}")
        except Exception as e:
            self.log(f"payload 提取错误: {e}")

    def on_closing(self):
        self.root.destroy()


if __name__ == "__main__":
    import traceback as _tb
    try:
        root = tk.Tk()
        app = FirmwareExtractorGUI(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)
        root.mainloop()
    except Exception:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "extractor_error.log"), "w", encoding="utf-8") as f:
                f.write(_tb.format_exc())
        except Exception:
            pass
        raise
