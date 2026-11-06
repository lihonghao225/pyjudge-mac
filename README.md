# PyJudge for macOS

一个用 Python + PySide6 编写的简易评测系统，参考 [Lemon](https://github.com/Project-LemonLime/Project_LemonLime) 与北航 [Arbiter](https://github.com/BUAA-ACM/Arbiter) 的交互习惯，面向 macOS 本地教学、练习与小型比赛场景。

> 本项目是**教学与本地练习用途的原型**，不是生产级沙箱评测机。若需要严格的隔离与并发能力，请使用 NOI Linux 上的 Arbiter、LemonLime 或开源评测平台（如 Hydro、UOJ）。

---

## 功能特性

- **图形界面**：三栏布局（题目树 / 成绩表 / 日志），支持深色模式。
- **并行评测**：基于 `ThreadPoolExecutor`，并发数可调（1–16）。
- **多比较器**：
  - 忽略行尾空白（默认）
  - 忽略全部空白
  - 浮点误差（相对误差，默认 `1e-6`）
  - 严格比较
- **SPJ 自定义比较器**：题目目录放 `checker.cpp` 即自动编译并调用。
- **资源限制**：
  - 墙钟超时（`subprocess.wait(timeout)`）
  - CPU 时间上限（`RLIMIT_CPU`）
  - 地址空间上限（`RLIMIT_AS`）
  - 输出文件大小上限（`RLIMIT_FSIZE`，默认 16 MB）
  - 峰值内存采样（依赖 `psutil`，5 ms 间隔）
- **编译缓存**：以源码 SHA1 为 key，避免重复编译。
- **增量评测**：支持「全部 / 选中选手 / 选中题目 / 只重跑非 AC」四种模式。
- **结果持久化**：每次评测后写入 `~/.pyjudge/last_results.json`，重启可恢复。
- **导出**：成绩表导出 CSV（UTF-8 BOM，Excel 直接打开不乱码），日志可另存。
- **状态色**：AC / WA / TLE / MLE / RE / CE / OLE 着色，双击单元格查看每个测试点明细。

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | macOS 12 及以上（Intel / Apple Silicon 均可） |
| Python | 3.10 及以上 |
| 编译器 | `clang++`（Xcode Command Line Tools 自带）或 `g++` |
| 可选依赖 | `psutil`（用于 CPU 时间与内存峰值采样） |

安装 Xcode 命令行工具（若尚未安装）：

```bash
xcode-select --install
