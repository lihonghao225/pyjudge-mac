#!/usr/bin/env python3
"""
PyJudge for macOS —— 全面优化版
类似 Lemon / Arbiter 的简易评测系统

依赖:
    pip install PySide6
    pip install psutil        # 强烈建议，用于 CPU 时间与内存峰值采样

用法:
    python3 pyjudge_mac.py
"""

import csv
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QThread, Signal, QSettings, QTimer,
)
from PySide6.QtGui import QColor, QFont, QAction, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem, QFileDialog,
    QMessageBox, QLabel, QHeaderView, QSplitter, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QSpinBox, QComboBox, QCheckBox,
    QDialog, QDialogButtonBox, QProgressBar, QStatusBar,
    QListWidget, QListWidgetItem, QAbstractItemView, QMenu,
    QLineEdit, QFormLayout, QToolBar,
)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ============================================================
# 常量
# ============================================================

MAX_OUTPUT_BYTES = 16 * 1024 * 1024      # 单测试点输出上限
MAX_PARALLEL = 16
CXX = "clang++" if platform.system() == "Darwin" else "g++"
CXX_STD = "-std=c++17"

SRC_EXTS = {".cpp", ".cc", ".cxx", ".c++", ".c"}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


# ============================================================
# 数据模型
# ============================================================

@dataclass
class TestCase:
    input_file: Path
    answer_file: Path
    time_limit: float = 1.0
    memory_limit: int = 256


@dataclass
class Problem:
    name: str
    dir: Path
    cases: list[TestCase] = field(default_factory=list)
    checker: Optional[Path] = None       # SPJ: checker.cpp
    problem_type: str = "standard"       # standard / output_only

    @classmethod
    def load(cls, problem_dir: Path) -> "Problem":
        cfg_path = problem_dir / "config.json"
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                p = cls(
                    name=cfg.get("name", problem_dir.name),
                    dir=problem_dir,
                    problem_type=cfg.get("type", "standard"),
                )
                dtl = cfg.get("time_limit", 1.0)
                dml = cfg.get("memory_limit", 256)
                for case in cfg.get("cases", []):
                    p.cases.append(TestCase(
                        input_file=problem_dir / case["input"],
                        answer_file=problem_dir / case["answer"],
                        time_limit=case.get("time_limit", dtl),
                        memory_limit=case.get("memory_limit", dml),
                    ))
                checker = problem_dir / "checker.cpp"
                if checker.exists():
                    p.checker = checker
                if p.cases:
                    return p
            except Exception:
                pass
        # 自动扫描
        p = cls(name=problem_dir.name, dir=problem_dir)
        for inf in sorted(problem_dir.glob("*.in"),
                          key=lambda x: _natural_key(x.stem)):
            ansf = inf.with_suffix(".ans")
            if ansf.exists():
                p.cases.append(TestCase(inf, ansf))
        checker = problem_dir / "checker.cpp"
        if checker.exists():
            p.checker = checker
        return p


@dataclass
class CaseResult:
    index: int
    status: str = "Waiting"
    time_ms: float = 0.0
    cpu_ms: float = 0.0
    memory_kb: float = 0.0
    message: str = ""
    score: float = 0.0
    max_score: float = 0.0


@dataclass
class JudgeResult:
    status: str = "Waiting"
    score: float = 0.0
    total_score: float = 100.0
    time_ms: float = 0.0
    cpu_ms: float = 0.0
    max_memory_kb: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)
    message: str = ""

    def to_dict(self):
        d = asdict(self)
        d["cases"] = [asdict(c) for c in self.cases]
        return d

    @classmethod
    def from_dict(cls, d):
        cases = [CaseResult(**c) for c in d.get("cases", [])]
        d = {k: v for k, v in d.items() if k != "cases"}
        return cls(cases=cases, **d)


# ============================================================
# 比较器
# ============================================================

def compare_output(expected: str, actual: str, mode: str,
                   float_eps: float = 1e-6) -> tuple[bool, str]:
    if mode == "ignore_all_whitespace":
        if re.sub(r"\s+", "", expected) == re.sub(r"\s+", "", actual):
            return True, ""
        return False, "忽略空白后仍不同"

    if mode == "float_eps":
        el, al = expected.strip().split(), actual.strip().split()
        if len(el) != len(al):
            return False, f"token 数不同: {len(el)} vs {len(al)}"
        for i, (x, y) in enumerate(zip(el, al), 1):
            try:
                fx, fy = float(x), float(y)
            except ValueError:
                if x != y:
                    return False, f"第 {i} 个 token 不同: {x!r} vs {y!r}"
                continue
            if abs(fx - fy) > float_eps * max(1.0, abs(fx), abs(fy)):
                return False, f"第 {i} 个 token 超误差: {x} vs {y}"
        return True, ""

    strict = (mode == "strict")
    el = expected.splitlines() if strict else expected.strip().splitlines()
    al = actual.splitlines() if strict else actual.strip().splitlines()
    if len(el) != len(al):
        return False, f"行数不同: {len(el)} vs {len(al)}"
    for i, (e, a) in enumerate(zip(el, al), 1):
        if strict:
            if e != a:
                return False, f"第 {i} 行不同:\n期望 {e[:120]!r}\n实际 {a[:120]!r}"
        else:
            if e.rstrip() != a.rstrip():
                return False, f"第 {i} 行不同:\n期望 {e[:120]!r}\n实际 {a[:120]!r}"
    return True, ""


def run_checker(checker_exe: Path, input_path: Path, out_path: Path,
                ans_path: Path, timeout: float = 10.0) -> tuple[bool, str]:
    """SPJ：checker input output answer → 返回 (通过?, 说明)"""
    try:
        cp = subprocess.run(
            [str(checker_exe), str(input_path), str(out_path), str(ans_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "checker 超时"
    except Exception as e:
        return False, f"checker 执行失败: {e}"
    if cp.returncode == 0:
        return True, ""
    return False, (cp.stderr or cp.stdout or "checker 返回非 0")[:500]


# ============================================================
# 评测线程
# ============================================================

class JudgeWorker(QThread):
    log = Signal(str, str)
    case_done = Signal(int, int, int, object)
    problem_done = Signal(int, int, object)
    all_done = Signal()

    def __init__(self, tasks, contestants, problems, sources,
                 work_dir, cache_dir, compare_mode="trailing_whitespace",
                 float_eps=1e-6, max_parallel=1):
        super().__init__()
        self.tasks = tasks                      # [(ci, pi), ...]
        self.contestants = contestants
        self.problems = problems
        self.sources = sources
        self.work_dir = work_dir
        self.cache_dir = cache_dir
        self.compare_mode = compare_mode
        self.float_eps = float_eps
        self.max_parallel = max(1, min(max_parallel, MAX_PARALLEL))
        self._stop = False
        self._lock = threading.Lock()
        self._done_count = 0

    def stop(self):
        self._stop = True

    def _log(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.emit(level, f"[{ts}] {msg}")

    def run(self):
        total = len(self.tasks)
        self._log("INFO", f"开始评测 {total} 个任务，并发 {self.max_parallel}，"
                          f"编译器 {CXX}")
        try:
            with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
                futs = {}
                for ci, pi in self.tasks:
                    if self._stop:
                        break
                    fut = pool.submit(
                        self._judge_one, ci, pi,
                        self.sources.get((ci, pi)),
                        self.contestants[ci], self.problems[pi],
                    )
                    futs[fut] = (ci, pi)
                for fut in as_completed(futs):
                    ci, pi = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = JudgeResult(status="RE", message=f"内部错误: {e}")
                    self.problem_done.emit(ci, pi, res)
                    with self._lock:
                        self._done_count += 1
        finally:
            self._log("INFO", "评测结束" if not self._stop else "评测已停止")
            self.all_done.emit()

    # ---------- 单题 ----------

    def _judge_one(self, ci, pi, source, contestant, problem) -> JudgeResult:
        res = JudgeResult()
        res.total_score = 100.0
        max_per = 100.0 / max(len(problem.cases), 1)

        if source is None:
            res.status = "No Source"
            res.message = "未找到源码文件"
            res.cases = [CaseResult(i, "Skipped", max_score=max_per)
                         for i in range(len(problem.cases))]
            return res

        if self._stop:
            return res

        # 编译（带缓存）
        try:
            exe = self._compile_cached(source, contestant, problem)
        except CompileError as e:
            res.status = "CE"
            res.message = e.message
            res.cases = [CaseResult(i, "Skipped", max_score=max_per)
                         for i in range(len(problem.cases))]
            self._log("ERR", f"CE {contestant.name}/{problem.name}")
            return res

        # SPJ
        checker_exe = None
        if problem.checker:
            try:
                checker_exe = self._compile_checker(problem)
            except Exception as e:
                self._log("WARN", f"checker 编译失败 {problem.name}: {e}")

        # 逐点
        case_results = []
        exe_dir = self.work_dir / f"{contestant.name}_{problem.name}_{ci}_{pi}"
        exe_dir.mkdir(parents=True, exist_ok=True)

        for i, tc in enumerate(problem.cases):
            if self._stop:
                break
            cr = self._run_case(i, exe, tc, exe_dir, checker_exe)
            cr.max_score = max_per
            cr.score = max_per if cr.status == "AC" else 0.0
            case_results.append(cr)
            self.case_done.emit(ci, pi, i, cr)
            if self._stop:
                break

        res.cases = case_results
        res.score = sum(c.score for c in case_results)
        res.time_ms = sum(c.time_ms for c in case_results)
        res.cpu_ms = sum(c.cpu_ms for c in case_results)
        res.max_memory_kb = max((c.memory_kb for c in case_results), default=0)
        if any(c.status != "AC" for c in case_results):
            bad = next(c for c in case_results if c.status != "AC")
            res.status = bad.status
            res.message = bad.message
        elif case_results:
            res.status = "AC"
            res.message = "全部通过"
        return res

    # ---------- 编译 ----------

    def _compile_cached(self, source: Path, contestant, problem) -> Path:
        key = sha1_file(source)
        cached = self.cache_dir / f"{key}"
        if cached.exists():
            return cached
        exe_dir = self.work_dir / f"build_{key[:8]}"
        exe_dir.mkdir(parents=True, exist_ok=True)
        exe = exe_dir / "prog"
        cmd = [CXX, CXX_STD, "-O2", "-o", str(exe), str(source)]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise CompileError("编译超时")
        if cp.returncode != 0:
            raise CompileError(cp.stderr[-2000:])
        shutil.copy2(exe, cached)
        return cached

    def _compile_checker(self, problem: Problem) -> Path:
        checker_src = problem.checker
        key = sha1_file(checker_src)
        cached = self.cache_dir / f"checker_{key}"
        if cached.exists():
            return cached
        exe = self.cache_dir / f"checker_{key}.tmp"
        cmd = [CXX, CXX_STD, "-O2", "-o", str(exe), str(checker_src)]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr[-800:])
        exe.rename(cached)
        return cached

    # ---------- 单测试点 ----------

    def _run_case(self, idx, exe, tc, work_dir, checker_exe) -> CaseResult:
        cr = CaseResult(index=idx)
        out_path = work_dir / f"case{idx}.out"
        err_path = work_dir / f"case{idx}.err"

        def limit_resources():
            try:
                cpu = int(tc.time_limit) + 1
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
                mem = tc.memory_limit * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
                resource.setrlimit(resource.RLIMIT_FSIZE,
                                   (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
            except Exception:
                pass

        wall_start = time.perf_counter()
        proc = None
        peak_rss = [0]
        cpu_total = [0.0]

        try:
            with open(tc.input_file, "rb") as fin, \
                 open(out_path, "wb") as fout, \
                 open(err_path, "wb") as ferr:
                proc = subprocess.Popen(
                    [str(exe)], stdin=fin, stdout=fout, stderr=ferr,
                    preexec_fn=limit_resources,
                )
                if HAS_PSUTIL:
                    t = threading.Thread(
                        target=self._sample, args=(proc, peak_rss, cpu_total),
                        daemon=True)
                    t.start()
                try:
                    proc.wait(timeout=tc.time_limit)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                    cr.status = "TLE"
                    cr.message = f"超过时间限制 {tc.time_limit}s"
                    cr.time_ms = tc.time_limit * 1000
                    return cr
        except Exception as e:
            cr.status = "RE"
            cr.message = str(e)
            return cr
        finally:
            if proc and proc.poll() is None:
                try: proc.kill()
                except Exception: pass

        cr.time_ms = (time.perf_counter() - wall_start) * 1000
        cr.memory_kb = peak_rss[0]
        cr.cpu_ms = cpu_total[0] * 1000

        # 输出超限
        try:
            if out_path.stat().st_size >= MAX_OUTPUT_BYTES:
                cr.status = "OLE"
                cr.message = f"输出超过 {MAX_OUTPUT_BYTES // (1024*1024)}MB"
                return cr
        except FileNotFoundError:
            pass

        if cr.memory_kb > tc.memory_limit * 1024:
            cr.status = "MLE"
            cr.message = f"内存 {cr.memory_kb/1024:.1f}MB > {tc.memory_limit}MB"
            return cr

        rc = proc.returncode
        if rc != 0:
            if rc == -signal.SIGXFSZ:
                cr.status = "OLE"
                cr.message = "文件大小超限"
            elif rc == -signal.SIGKILL:
                cr.status = "MLE"
                cr.message = "进程被杀死（疑似内存超限）"
            else:
                cr.status = "RE"
                try:
                    cr.message = err_path.read_text(errors="replace")[:500]
                except Exception:
                    pass
            return cr

        # 判定
        if checker_exe:
            ok, why = run_checker(checker_exe, tc.input_file, out_path,
                                  tc.answer_file)
        else:
            try:
                expected = tc.answer_file.read_text(errors="replace")
                actual = out_path.read_text(errors="replace")
            except Exception as e:
                cr.status = "RE"
                cr.message = f"读取输出失败: {e}"
                return cr
            ok, why = compare_output(expected, actual,
                                     self.compare_mode, self.float_eps)

        if ok:
            cr.status = "AC"
            cr.message = "正确"
        else:
            cr.status = "WA"
            cr.message = why
        return cr

    @staticmethod
    def _sample(proc, peak_rss, cpu_total):
        try:
            p = psutil.Process(proc.pid)
            while proc.poll() is None:
                try:
                    rss = p.memory_info().rss // 1024
                    if rss > peak_rss[0]:
                        peak_rss[0] = rss
                    ct = p.cpu_times()
                    cpu_total[0] = ct.user + ct.system
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.005)
        except Exception:
            pass


class CompileError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


# ============================================================
# 详情对话框
# ============================================================

STATUS_COLORS = {
    "AC": "#c8e6c9", "WA": "#ffcdd2", "TLE": "#fff9c4",
    "MLE": "#f8bbd0", "RE": "#ffccbc", "CE": "#d1c4e9",
    "OLE": "#ffe0b2", "Skipped": "#eeeeee", "No Source": "#e0e0e0",
}


def status_color(s):
    return STATUS_COLORS.get(s, "#ffffff")


class CaseDetailDialog(QDialog):
    def __init__(self, parent, title, result: JudgeResult):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 500)
        layout = QVBoxLayout(self)

        info = QLabel(
            f"状态: {result.status}   得分: {result.score:.1f} / "
            f"{result.total_score:.0f}   用时: {result.time_ms:.1f}ms   "
            f"CPU: {result.cpu_ms:.1f}ms   峰值内存: "
            f"{result.max_memory_kb/1024:.1f}MB"
        )
        info.setStyleSheet("font-weight:600; padding:4px;")
        layout.addWidget(info)

        table = QTableWidget(len(result.cases), 5)
        table.setHorizontalHeaderLabels(
            ["测试点", "状态", "墙钟(ms)", "CPU(ms)", "内存(MB)"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for r, cr in enumerate(result.cases):
            table.setItem(r, 0, QTableWidgetItem(str(cr.index + 1)))
            st = QTableWidgetItem(cr.status)
            st.setBackground(QColor(status_color(cr.status)))
            table.setItem(r, 1, st)
            table.setItem(r, 2, QTableWidgetItem(f"{cr.time_ms:.1f}"))
            table.setItem(r, 3, QTableWidgetItem(f"{cr.cpu_ms:.1f}"))
            table.setItem(r, 4, QTableWidgetItem(f"{cr.memory_kb/1024:.1f}"))
        layout.addWidget(table)

        detail = QTextEdit()
        detail.setReadOnly(True)
        detail.setFont(QFont("Menlo", 11))
        def on_sel():
            r = table.currentRow()
            if 0 <= r < len(result.cases):
                cr = result.cases[r]
                detail.setPlainText(
                    f"测试点 {cr.index+1}\n状态: {cr.status}\n"
                    f"墙钟: {cr.time_ms:.2f}ms\nCPU: {cr.cpu_ms:.2f}ms\n"
                    f"内存: {cr.memory_kb/1024:.2f}MB\n\n{cr.message}")
        table.itemSelectionChanged.connect(on_sel)
        layout.addWidget(QLabel("详情:"))
        layout.addWidget(detail, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyJudge for macOS —— 优化版")
        self.resize(1320, 820)

        self.problems: list[Problem] = []
        self.contestants: list[Path] = []
        self.sources: dict[tuple[int, int], Path] = {}
        self.results: dict[tuple[int, int], JudgeResult] = {}
        self.worker: Optional[JudgeWorker] = None

        self.app_dir = Path.home() / ".pyjudge"
        self.work_dir = Path(tempfile.mkdtemp(prefix="pyjudge_"))
        self.cache_dir = self.app_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = self.app_dir / "last_results.json"

        self.settings = QSettings("PyJudge", "PyJudgeMac")

        self._build_menu()
        self._build_ui()
        self._build_statusbar()
        self._restore_state()
        self._load_results_snapshot()

    # ---------- 菜单 ----------

    def _build_menu(self):
        mb = self.menuBar()

        m = mb.addMenu("文件")
        a = QAction("添加题目…", self); a.setShortcut(QKeySequence("Ctrl+O"))
        a.triggered.connect(self.add_problem); m.addAction(a)
        a = QAction("添加选手…", self); a.setShortcut(QKeySequence("Ctrl+Shift+O"))
        a.triggered.connect(self.add_contestants); m.addAction(a)
        m.addSeparator()
        a = QAction("导出成绩 CSV…", self); a.triggered.connect(self.export_csv)
        m.addAction(a)
        a = QAction("保存日志…", self); a.triggered.connect(self.save_log)
        m.addAction(a)
        m.addSeparator()
        a = QAction("退出", self); a.setShortcut(QKeySequence.Quit)
        a.triggered.connect(self.close); m.addAction(a)

        m = mb.addMenu("运行")
        a = QAction("开始评测（全部）", self); a.setShortcut(QKeySequence("F5"))
        a.triggered.connect(lambda: self.start_judge("all")); m.addAction(a)
        a = QAction("只评测选中选手", self)
        a.triggered.connect(lambda: self.start_judge("selected_contestant"))
        m.addAction(a)
        a = QAction("只评测选中题目", self)
        a.triggered.connect(lambda: self.start_judge("selected_problem"))
        m.addAction(a)
        a = QAction("只重跑非 AC", self)
        a.triggered.connect(lambda: self.start_judge("non_ac"))
        m.addAction(a)
        m.addSeparator()
        a = QAction("停止", self); a.setShortcut(QKeySequence("Shift+F5"))
        a.triggered.connect(self.stop_judge); m.addAction(a)
        a = QAction("清空结果", self); a.triggered.connect(self.clear_results)
        m.addAction(a)

        m = mb.addMenu("工具")
        a = QAction("清空编译缓存", self)
        a.triggered.connect(self.clear_cache); m.addAction(a)
        a = QAction("清理上次结果", self)
        a.triggered.connect(self.clear_snapshot); m.addAction(a)

        m = mb.addMenu("帮助")
        a = QAction("关于", self); a.triggered.connect(self._about)
        m.addAction(a)

    def _about(self):
        QMessageBox.about(
            self, "关于 PyJudge",
            f"PyJudge for macOS 优化版\n\n"
            f"编译器: {CXX}\n"
            f"psutil: {'已安装' if HAS_PSUTIL else '未安装（CPU/内存采样降级）'}\n"
            f"缓存目录: {self.cache_dir}\n"
            f"上次结果: {self.results_path}"
        )

    # ---------- 界面 ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 顶部工具条
        bar = QHBoxLayout()
        for text, slot in [
            ("添加题目", self.add_problem),
            ("添加选手", self.add_contestants),
            ("开始", lambda: self.start_judge("all")),
            ("停止", self.stop_judge),
            ("清空", self.clear_results),
        ]:
            b = QPushButton(text); b.clicked.connect(slot); b.setMinimumHeight(30)
            bar.addWidget(b)

        bar.addSpacing(10)
        bar.addWidget(QLabel("比较器:"))
        self.cmb_compare = QComboBox()
        self.cmb_compare.addItems(
            ["忽略行尾空白", "忽略全部空白", "浮点误差", "严格比较"])
        bar.addWidget(self.cmb_compare)

        bar.addSpacing(6)
        bar.addWidget(QLabel("并发:"))
        self.spin_parallel = QSpinBox()
        self.spin_parallel.setRange(1, MAX_PARALLEL)
        self.spin_parallel.setValue(2)
        bar.addWidget(self.spin_parallel)

        bar.addSpacing(6)
        self.chk_filter = QCheckBox("只看非 AC")
        bar.addWidget(self.chk_filter)
        bar.addStretch()
        root.addLayout(bar)

        # 三栏
        splitter = QSplitter(Qt.Horizontal)

        # 左：题目树
        left = QWidget(); ll = QVBoxLayout(left)
        ll.addWidget(QLabel("题目与测试点"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["项目", "信息"])
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        ll.addWidget(self.tree)
        splitter.addWidget(left)

        # 中：结果
        mid = QWidget(); ml = QVBoxLayout(mid)
        top = QHBoxLayout()
        top.addWidget(QLabel("成绩表"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索选手…")
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search)
        ml.addLayout(top)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._open_cell_detail)
        self.table.setSortingEnabled(True)
        ml.addWidget(self.table)
        splitter.addWidget(mid)

        # 右：日志
        right = QWidget(); rl = QVBoxLayout(right)
        logbar = QHBoxLayout()
        logbar.addWidget(QLabel("日志"))
        self.cmb_level = QComboBox()
        self.cmb_level.addItems(["全部", "WARN+", "ERR"])
        self.cmb_level.currentIndexChanged.connect(self._apply_log_filter)
        logbar.addWidget(self.cmb_level)
        rl.addLayout(logbar)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo", 11))
        rl.addWidget(self.log_view)
        splitter.addWidget(right)
        splitter.setSizes([300, 620, 400])
        root.addWidget(splitter, 1)

        self._log_entries = []   # (level, text)

    def _build_statusbar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.status.addPermanentWidget(self.progress)
        self.status.showMessage("就绪")

    # ---------- 状态持久化 ----------

    def _restore_state(self):
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self.cmb_compare.setCurrentIndex(int(self.settings.value("cmp", 0)))
        self.spin_parallel.setValue(int(self.settings.value("par", 2)))
        last_problem = self.settings.value("last_problem_dir")
        if last_problem and Path(last_problem).exists():
            p = Problem.load(Path(last_problem))
            if p.cases:
                self.problems.append(p)
                self._refresh_tree()
        last_contestants = self.settings.value("last_contestants_dir")
        if last_contestants and Path(last_contestants).exists():
            base = Path(last_contestants)
            self.contestants = sorted(
                [x for x in base.iterdir() if x.is_dir()],
                key=lambda x: x.name)
            self._scan_sources()
            self._refresh_table()

    def _save_state(self):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("cmp", self.cmb_compare.currentIndex())
        self.settings.setValue("par", self.spin_parallel.value())
        if self.problems:
            self.settings.setValue("last_problem_dir",
                                   str(self.problems[-1].dir))

    # ---------- 结果快照 ----------

    def _save_results_snapshot(self):
        data = {
            "contestants": [nfc(c.name) for c in self.contestants],
            "problems": [p.name for p in self.problems],
            "results": {
                f"{ci}_{pi}": r.to_dict()
                for (ci, pi), r in self.results.items()
            },
        }
        try:
            self.results_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass

    def _load_results_snapshot(self):
        if not self.results_path.exists():
            return
        try:
            data = json.loads(self.results_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if (data.get("contestants") != [nfc(c.name) for c in self.contestants]
                or data.get("problems") != [p.name for p in self.problems]):
            return
        for key, rd in data.get("results", {}).items():
            ci, pi = key.split("_")
            self.results[(int(ci), int(pi))] = JudgeResult.from_dict(rd)
        for (ci, pi), r in self.results.items():
            if ci < len(self.contestants) and pi < len(self.problems):
                self._paint_cell(ci, pi, r)
        self._refresh_totals()
        self._log("INFO", "已恢复上次评测结果")

    def clear_snapshot(self):
        try:
            self.results_path.unlink()
        except Exception:
            pass
        self._log("INFO", "已删除结果快照")

    def clear_cache(self):
        if QMessageBox.question(self, "确认", "清空编译缓存？") \
                != QMessageBox.Yes:
            return
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._log("INFO", "编译缓存已清空")

    # ---------- 题目 / 选手 ----------

    def add_problem(self):
        d = QFileDialog.getExistingDirectory(self, "选择题目的文件夹")
        if not d:
            return
        p = Problem.load(Path(d))
        if not p.cases:
            QMessageBox.warning(self, "提示", f"{d} 中没有测试数据。")
            return
        self.problems.append(p)
        self._refresh_tree(); self._scan_sources(); self._refresh_table()
        self._log("INFO", f"添加题目 {p.name}（{len(p.cases)} 点）")

    def add_contestants(self):
        d = QFileDialog.getExistingDirectory(self, "选择选手根目录")
        if not d:
            return
        base = Path(d)
        self.contestants = sorted(
            [x for x in base.iterdir() if x.is_dir()],
            key=lambda x: x.name)
        self._scan_sources(); self._refresh_table()
        self.settings.setValue("last_contestants_dir", d)
        self._log("INFO", f"添加 {len(self.contestants)} 位选手")

    def _scan_sources(self):
        self.sources.clear()
        for ci, cdir in enumerate(self.contestants):
            files = [f for f in cdir.rglob("*")
                     if f.suffix.lower() in SRC_EXTS]
            for pi, prob in enumerate(self.problems):
                for f in files:
                    if nfc(prob.name).lower() in nfc(f.stem).lower():
                        self.sources[(ci, pi)] = f
                        break

    def _refresh_tree(self):
        self.tree.clear()
        for prob in self.problems:
            item = QTreeWidgetItem([prob.name, f"{len(prob.cases)} 点"])
            item.setData(0, Qt.UserRole, ("problem", prob))
            for i, tc in enumerate(prob.cases, 1):
                item.addChild(QTreeWidgetItem(
                    [f"测试点 {i}", f"{tc.time_limit}s / {tc.memory_limit}MB"]))
            self.tree.addTopLevelItem(item)
        self.tree.expandAll()

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        if data and data[0] == "problem":
            prob = data[1]
            a = menu.addAction(f"移除题目 {prob.name}")
            a.triggered.connect(lambda: self._remove_problem(prob))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _remove_problem(self, prob):
        if prob in self.problems:
            self.problems.remove(prob)
            self._refresh_tree(); self._scan_sources(); self._refresh_table()

    def _refresh_table(self):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setRowCount(len(self.contestants))
        self.table.setColumnCount(len(self.problems) + 1)
        self.table.setHorizontalHeaderLabels(
            [p.name for p in self.problems] + ["总分"])
        self.table.setVerticalHeaderLabels(
            [nfc(c.name) for c in self.contestants])
        for ci in range(len(self.contestants)):
            for pi in range(len(self.problems)):
                self._paint_cell(ci, pi, self.results.get((ci, pi)))
            self.table.setItem(ci, len(self.problems),
                               QTableWidgetItem("0"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        self._refresh_totals()

    def _paint_cell(self, ci, pi, res: Optional[JudgeResult]):
        if res is None:
            it = QTableWidgetItem("—")
            it.setBackground(QColor("#ffffff"))
        else:
            it = QTableWidgetItem(f"{res.status}\n{res.score:.0f}")
            it.setBackground(QColor(status_color(res.status)))
            it.setToolTip(res.message or res.status)
            it.setData(Qt.UserRole, res.score)
        it.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(ci, pi, it)

    def _refresh_totals(self):
        for ci in range(len(self.contestants)):
            total = sum(self.results.get((ci, p), JudgeResult()).score
                        for p in range(len(self.problems)))
            it = QTableWidgetItem(f"{total:.0f}")
            it.setTextAlignment(Qt.AlignCenter)
            it.setData(Qt.UserRole, total)
            self.table.setItem(ci, len(self.problems), it)

    def _apply_filter(self):
        text = self.search.text().strip().lower()
        for ci in range(self.table.rowCount()):
            name = nfc(self.contestants[ci].name).lower()
            self.table.setRowHidden(ci, bool(text) and text not in name)

    # ---------- 评测 ----------

    def start_judge(self, mode="all"):
        if self.worker and self.worker.isRunning():
            return
        if not self.problems or not self.contestants:
            QMessageBox.warning(self, "提示", "请先添加题目与选手。")
            return

        self._scan_sources()

        # 构造任务
        if mode == "all":
            tasks = [(ci, pi)
                     for ci in range(len(self.contestants))
                     for pi in range(len(self.problems))]
        elif mode == "selected_contestant":
            ci = self.table.currentRow()
            if ci < 0:
                QMessageBox.information(self, "提示", "请先在表格里选中一行。")
                return
            tasks = [(ci, pi) for pi in range(len(self.problems))]
        elif mode == "selected_problem":
            item = self.tree.currentItem()
            if not item:
                QMessageBox.information(self, "提示", "请先在题目树里选中题目。")
                return
            prob = None
            d = item.data(0, Qt.UserRole)
            if d and d[0] == "problem":
                prob = d[1]
            elif item.parent():
                d = item.parent().data(0, Qt.UserRole)
                if d and d[0] == "problem":
                    prob = d[1]
            if prob is None:
                return
            pi = self.problems.index(prob)
            tasks = [(ci, pi) for ci in range(len(self.contestants))]
        elif mode == "non_ac":
            tasks = []
            for ci in range(len(self.contestants)):
                for pi in range(len(self.problems)):
                    r = self.results.get((ci, pi))
                    if r is None or r.status != "AC":
                        tasks.append((ci, pi))
            if not tasks:
                QMessageBox.information(self, "提示", "没有需要重跑的题目。")
                return
        else:
            return

        modes = {"忽略行尾空白": "trailing_whitespace",
                 "忽略全部空白": "ignore_all_whitespace",
                 "浮点误差": "float_eps",
                 "严格比较": "strict"}
        cmp_mode = modes[self.cmb_compare.currentText()]

        self.log_view.clear(); self._log_entries.clear()
        self.worker = JudgeWorker(
            tasks, self.contestants, self.problems, self.sources,
            self.work_dir, self.cache_dir,
            compare_mode=cmp_mode, float_eps=1e-6,
            max_parallel=self.spin_parallel.value(),
        )
        self.worker.log.connect(self._log)
        self.worker.problem_done.connect(self._on_problem_done)
        self.worker.all_done.connect(self._on_all_done)
        self.progress.setMaximum(len(tasks))
        self.progress.setValue(0)
        self.worker.start()

    def stop_judge(self):
        if self.worker:
            self.worker.stop()
            self._log("WARN", "正在停止…")

    def clear_results(self):
        self.results.clear()
        self._refresh_table()
        self.log_view.clear(); self._log_entries.clear()
        self.progress.setValue(0)
        self.status.showMessage("已清空")

    def _on_problem_done(self, ci, pi, res: JudgeResult):
        self.results[(ci, pi)] = res
        self._paint_cell(ci, pi, res)
        self._refresh_totals()
        self.progress.setValue(self.progress.value() + 1)
        self._log("INFO", f"{nfc(self.contestants[ci].name)}/"
                          f"{self.problems[pi].name}: {res.status} "
                          f"{res.score:.1f}")
        self._save_results_snapshot()

    def _on_all_done(self):
        self.status.showMessage("评测完成", 5000)
        self._log("INFO", "全部完成")

    def _open_cell_detail(self, row, col):
        if col >= len(self.problems):
            return
        res = self.results.get((row, col))
        if not res:
            return
        dlg = CaseDetailDialog(
            self, f"{nfc(self.contestants[row].name)} / "
                  f"{self.problems[col].name}", res)
        dlg.exec()

    # ---------- 导出 ----------

    def export_csv(self):
        if not self.results:
            QMessageBox.information(self, "提示", "还没有结果。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", "scores.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["选手"] + [p.name for p in self.problems] + ["总分"])
            for ci, c in enumerate(self.contestants):
                row = [nfc(c.name)]; tot = 0
                for pi in range(len(self.problems)):
                    r = self.results.get((ci, pi))
                    if r:
                        row.append(f"{r.score:.1f} ({r.status})")
                        tot += r.score
                    else:
                        row.append("—")
                row.append(f"{tot:.1f}")
                w.writerow(row)
        self._log("INFO", f"已导出 {path}")

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存日志", "judge.log", "Log (*.log *.txt)")
        if not path:
            return
        Path(path).write_text(
            "\n".join(t for _, t in self._log_entries), encoding="utf-8")

    # ---------- 日志 ----------

    def _log(self, level, msg):
        self._log_entries.append((level, msg))
        self._append_log_html(level, msg)

    def _append_log_html(self, level, msg):
        if not self._level_pass(level):
            return
        color = {"INFO": "#1f2937", "WARN": "#b45309",
                 "ERR": "#b91c1c", "DEBUG": "#6b7280"}.get(level, "#111827")
        safe = (msg.replace("&", "&amp;")
                  .replace("<", "&lt;").replace(">", "&gt;"))
        self.log_view.append(
            f'<span style="color:{color};white-space:pre-wrap">'
            f'[{level}] {safe}</span>')
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _level_pass(self, level):
        idx = self.cmb_level.currentIndex() if hasattr(self, "cmb_level") else 0
        if idx == 1:   # WARN+
            return level in ("WARN", "ERR")
        if idx == 2:
            return level == "ERR"
        return True

    def _apply_log_filter(self):
        self.log_view.clear()
        for level, msg in self._log_entries:
            self._append_log_html(level, msg)

    # ---------- 清理 ----------

    def closeEvent(self, event):
        self._save_state()
        self._save_results_snapshot()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        event.accept()


# ============================================================
# 入口
# ============================================================

def main():
    if sys.platform == "darwin":
        os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("PyJudge")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
