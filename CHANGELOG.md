# 更新日志

## v2.0.2 (2026-11-17)

### 修复

- **固件浏览器点年份就闪退**（点 2020–2026 任意年份，窗口直接消失）
  - 根因：每次展开目录都会把 `DirFetch` 线程放进 `self._dir_threads`，线程结束后
    `deleteLater` 已把 C++ 对象回收；下次展开时列表里那个已失效的包装对象被调用
    `isFinished()`，抛 `RuntimeError: wrapped C/C++ object of type DirFetch has been deleted`
  - PyQt5 对槽函数里未捕获的异常直接 `qFatal()`，所以表现是**整个程序瞬间消失**
    （退出码 `0xC0000409`），日志里连一句提示都没有
  - 修复：新增 `prune_finished_threads()`（`RuntimeError` 安全过滤）+
    给所有界面槽套 `@slot_guard`（异常写 `extractor_error.log`，不再杀进程）

### 改进

- 目录加载时插入「加载中…」占位项，失败会移除并允许重新点击重试
- 镜像主机记忆：某个路径前缀命中过镜像后，下次直接先问那台主机
  （2015–2021 的年份目录在主站是空的，之前每次要白等一次超时）
- 目录请求超时 15s → 8s

### 新增

- `--expand-demo` 开发自检：自动展开根节点 → 第一个年份，打印子节点数后退出，
  用于回归「点年份闪退」这类问题
- 自检扩到 **23 项**（新增「已回收线程被过滤」用例）

## v2.0.1 (2026-11-17)

### 修复

- **对象存储预签名直链取不到真实文件名**：这类链接形如
  `https://xxx.cmecloud.cn/<32位ID>?response-content-disposition=attachment%3B%20filename%2A%3DUTF-8%27%27PLQ110_16.0.5.702.zip&X-Amz-Signature=...`
  - 路径段是纯 ID（没有扩展名），旧逻辑于是去问服务器，但用的是 `HEAD`；
    AWS SigV4 预签名链接**只对 GET 签名**，`HEAD` 必然 403，取不到头就退化成
    `主机名_哈希`（就是那个 `ykj-eos-...cmecloud.cn_848647c4`）
  - 现在三级兜底：① 直接解析 URL 查询串里的 `response-content-disposition`
    ② `HEAD` 响应头 ③ `Range: bytes=0-0` 的 GET 响应头（1 字节，不浪费流量）
  - 同一链接现在得到目录名 `PLQ110_16.0.5.702`
- 自检扩到 22 项：新增「预签名 query 参数取名」「HEAD 403 时用 GET 取名」两例

## v2.0.0 (2026-11-16)

首个 C++/Qt 迁移前的成熟版本，从"能跑"升级到"能分发"。

### 新增

- **多线程并行 Range 下载**：数据段按 8 MB 分块、线程池并发抓取，默认 8 并发（界面可调 1–16）；
  单块失败按 5/10/20/40/60 秒退避重试；单次下载 >256 MB 自动改用临时文件 + `mmap`，不再吃满内存
- **实时网速显示**：2 秒滑动窗口 + EWMA 平滑，250 ms 刷新 `速度 / 已下载 / 剩余时间`
- **镜像站按年份自动分流**：主站目录为空时自动尝试 `mirrors-obs-1`（2020–2021）、
  `mirrors-obs-2`（2015–2019），目录树会记住实际可用主机
- **可取消**：提取中途随时停止，关闭窗口先收尾线程
- **`--selftest` 自检**：20 项用例，含本地 Range 服务器 + 合成 Full OTA 端到端验证
- **`--screenshot`**：无头渲染窗口截图，用于回归与文档
- `build.ps1` 一键打包，`CHANGELOG.md`

### 修复

- **输出目录名乱码**：百分号编码按 UTF-8/GBK 双路解码；`cp1252`/`latin-1` 误解码的中文
  自动还原（`ç³»ç»Ÿ` → `系统`）；无文件名时读 `Content-Disposition`，再回退 `主机名_哈希`；
  Windows 非法字符、保留名（CON/PRN/NUL…）、超长名统一处理
- payload 头部按 manifest 长度自适应读取（原来固定 5 MB，manifest 偏大会解析出 0 个分区）
- 关闭窗口时 `QThread` 仍在运行被析构导致的 `0xC0000409` 崩溃
- 界面按钮文字被挤压截断（全局样式表 + 最小尺寸）
- `_write_extents` 数据不足时静默产出坏镜像 → 现在会记录并校验

### 变更

- 输出目录固定在 exe/脚本同级的 `extracted_images`
- ZERO 分区改为分块写零（内存占用从"整分区大小"降到 1 MB）
- 退役 v1 tkinter 版本 `extractor.py` 及打包残留脚本（历史见 `git log`）

### 验证

- 冻结版 exe 自检 20/20 通过
- 真实 OTA（Pixel 11 `cubs` 完整包）：40 个分区 0.9 s 解析；`boot.img` 仅下载 15.5 MB
  数据段，4.9 s 生成 64 MB 镜像，`ANDROID!` 头与 header_size 校验通过
- 真实镜像站：`lenomola/2020/` 主站空 → 自动切 `mirrors-obs-1` 列出 23 个机型目录

## v1.0.0

- tkinter / PyQt5 双版本：远端 ZIP 探测、payload 解析、按分区 Range 提取、稀疏落盘
