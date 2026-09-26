#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
연재 소설 스크래퍼 GUI (Tkinter, 추가 설치 불필요).

- URL 붙여넣기
- 저장할 파일 이름/위치를 직접 선택
- 실시간 로그 + 진행률 표시, 중지 버튼
- 스크래핑은 백그라운드 스레드에서 실행되어 창이 멈추지 않음

실행:  python gui.py
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

# frozen(exe) 실행 시 patchright 가 브라우저를 exe 임시폴더에서 찾는 문제 해결.
# scrapling 을 임포트하기 전에 표준 설치 위치(%LOCALAPPDATA%\ms-playwright)를 지정.
if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(_local) / "ms-playwright")

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402


def _startup_fail(exc):
    """시작 단계(주로 import) 실패를 파일로 남기고 사용자에게 알림."""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for target in (Path(sys.executable).parent / "시작오류.txt",
                   Path(os.environ.get("TEMP", ".")) / "소설스크래퍼_시작오류.txt"):
        try:
            target.write_text(tb, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    try:
        r = tk.Tk(); r.withdraw()
        messagebox.showerror(
            "시작 오류",
            "프로그램 시작에 실패했습니다.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "자세한 내용은 실행 파일과 같은 폴더의 '시작오류.txt' 를 확인하세요.")
        r.destroy()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(1)


try:
    import scrape_novel  # noqa: E402
except Exception as _e:  # noqa: BLE001
    _startup_fail(_e)


def install_browser(log=print):
    """번들된 patchright 드라이버로 chromium 을 내려받아 표준 위치에 설치.
    이미 설치돼 있으면 빠르게 확인만 하고 끝난다."""
    from patchright._impl._driver import (compute_driver_executable,
                                          get_driver_env)
    node, cli = compute_driver_executable()
    env = get_driver_env()
    log("[*] 브라우저(chromium) 설치/확인 중… (최초 1회, 수백 MB)")
    proc = subprocess.Popen(
        [node, cli, "install", "chromium"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("    " + line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"브라우저 설치 실패 (코드 {proc.returncode})")
    log("[완료] 브라우저 준비 완료.")


def _friendly_error(msg):
    """브라우저 미설치 등 흔한 오류를 알기 쉬운 안내로 변환."""
    if "인증이 필요" in msg or "연속" in msg and "본문" in msg:
        return msg  # 이미 사람이 읽을 수 있는 안내문
    if "Executable doesn't exist" in msg or "patchright install" in msg:
        return (
            "스텔스 브라우저(chromium)가 설치되어 있지 않습니다.\n\n"
            "아래 [브라우저 설치] 버튼을 눌러 최초 1회 설치하세요.\n"
            "(인터넷 연결 필요, 수백 MB 다운로드)"
        )
    return msg


class ScraperGUI:
    def __init__(self, root):
        self.root = root
        root.title("연재 소설 스크래퍼")
        root.geometry("1300x820")
        root.minsize(1050, 640)

        # 텍스트 크기 1.5배 확대 (기본 9pt -> 14pt)
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(font_name).configure(size=14, family="맑은 고딕")
            except Exception:
                pass

        self.style = ttk.Style()
        self.style.configure(".", font=("맑은 고딕", 14))
        self.style.configure("TLabelframe.Label", font=("맑은 고딕", 14, "bold"))
        self.style.configure("Treeview", font=("맑은 고딕", 11), rowheight=30)
        self.style.configure("Treeview.Heading", font=("맑은 고딕", 12, "bold"))

        self.log_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        # 메인 가로 분할 (좌: 메인 작업창, 우: 펼쳐진 연재 소설 목록)
        main_pane = ttk.PanedWindow(root, orient="horizontal")
        main_pane.pack(fill="both", expand=True, padx=8, pady=8)

        # -------------------------------------------------------------
        # 좌측 프레임: 메인 스크래퍼 작업 영역
        # -------------------------------------------------------------
        frm = ttk.Frame(main_pane)
        main_pane.add(frm, weight=3)
        frm.columnconfigure(1, weight=1)

        pad = {"padx": 12, "pady": 6}

        # 최근 수집 이력 (이어받기)
        ttk.Label(frm, text="이전 수집 이력").grid(row=0, column=0, sticky="w", **pad)
        self.cached_novels_map = {}
        self.history_var = tk.StringVar()
        self.history_cb = ttk.Combobox(frm, textvariable=self.history_var, state="readonly")
        self.history_cb.grid(row=0, column=1, sticky="ew", **pad)
        self.history_cb.bind("<<ComboboxSelected>>", self.on_history_selected)
        ttk.Button(frm, text="이어서 수집", command=self.load_history_click).grid(
            row=0, column=2, sticky="e", **pad)

        # URL
        ttk.Label(frm, text="소설 목록 URL").grid(row=1, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(frm, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        self.url_entry.focus()
        self.url_entry.bind("<FocusOut>", self._on_url_entry_event)
        self.url_entry.bind("<Return>", self._on_url_entry_event)

        # 기본 저장 폴더
        ttk.Label(frm, text="기본 저장 폴더").grid(row=2, column=0, sticky="w", **pad)
        self.download_dir_var = tk.StringVar(value=str(scrape_novel.get_default_download_dir()))
        self.dir_entry = ttk.Entry(frm, textvariable=self.download_dir_var)
        self.dir_entry.grid(row=2, column=1, sticky="ew", **pad)
        self.dir_entry.bind("<FocusOut>", self._on_dir_entry_change)
        self.dir_entry.bind("<Return>", self._on_dir_entry_change)

        dir_btns = ttk.Frame(frm)
        dir_btns.grid(row=2, column=2, sticky="e", **pad)
        ttk.Button(dir_btns, text="폴더 변경…", command=self.browse_dir).pack(side="left", padx=(0, 6))
        ttk.Button(dir_btns, text="구글 드라이브", command=self.set_google_drive_dir).pack(side="left")

        # 출력 파일
        ttk.Label(frm, text="저장 파일").grid(row=3, column=0, sticky="w", **pad)
        default_out = str(self.get_current_download_dir() / "소설.epub")
        self.out_var = tk.StringVar(value=default_out)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="찾아보기…", command=self.browse_out).grid(
            row=3, column=2, sticky="e", **pad)

        # 저장 포맷
        fmt_frm = ttk.Frame(frm)
        fmt_frm.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(frm, text="저장 포맷").grid(row=4, column=0, sticky="w", **pad)
        self.file_format = tk.StringVar(value="epub")
        ttk.Radiobutton(fmt_frm, text="EPUB", value="epub",
                        variable=self.file_format, command=self._on_format_changed).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(fmt_frm, text="TEXT", value="txt",
                        variable=self.file_format, command=self._on_format_changed).pack(side="left", padx=(0, 24))
        self.also_save_other = tk.BooleanVar(value=False)
        self.is_ongoing_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(fmt_frm, text="연재 소설로 등록",
                        variable=self.is_ongoing_var).pack(side="left")

        # 옵션들
        opt = ttk.LabelFrame(frm, text="옵션")
        opt.grid(row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=10)
        for c in range(6):
            opt.columnconfigure(c, weight=1)

        ttk.Label(opt, text="최소 지연(초)").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.min_delay = tk.DoubleVar(value=15.0)
        ttk.Spinbox(opt, from_=0, to=60, increment=1, width=7,
                    textvariable=self.min_delay).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(opt, text="최대 지연(초)").grid(row=0, column=2, sticky="w", padx=8)
        self.max_delay = tk.DoubleVar(value=20.0)
        ttk.Spinbox(opt, from_=0, to=120, increment=1, width=7,
                    textvariable=self.max_delay).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(opt, text="개수 제한(0=전체)").grid(row=0, column=4, sticky="w", padx=8)
        self.limit = tk.IntVar(value=0)
        ttk.Spinbox(opt, from_=0, to=100000, increment=1, width=9,
                    textvariable=self.limit).grid(row=0, column=5, sticky="w", padx=4)

        ttk.Label(opt, text="프록시(선택)").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.proxy = tk.StringVar(value="")
        ttk.Entry(opt, textvariable=self.proxy).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=6)

        self.headful = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="창 표시", variable=self.headful).grid(
            row=1, column=3, sticky="w", padx=8)
        self.solve_cf = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="CF 우회", variable=self.solve_cf).grid(
            row=1, column=4, sticky="w", padx=8)
        self.auto_quota_retry = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="쿼터시 자정(0시)후 자동재시도", variable=self.auto_quota_retry).grid(
            row=1, column=5, sticky="w", padx=8)

        self.auto_clipboard = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="클립보드 자동 감지", variable=self.auto_clipboard).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        self.auto_start_on_clipboard = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="감지 시 즉시 스크랩 시작", variable=self.auto_start_on_clipboard).grid(
            row=2, column=3, columnspan=3, sticky="w", padx=8, pady=4)

        # 버튼
        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="ew", padx=12, pady=8)
        self.start_btn = ttk.Button(btns, text="시작", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="중지", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.batch_update_btn = ttk.Button(btns, text="연재중 소설 업데이트", command=self.update_ongoing_click)
        self.batch_update_btn.pack(side="left", padx=(0, 8))
        self.install_btn = ttk.Button(btns, text="브라우저 설치",
                                      command=self.install_browser_click)
        self.install_btn.pack(side="left")
        ttk.Button(btns, text="로그 지우기", command=self.clear_log).pack(side="left", padx=8)

        # 진행률
        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", padx=12, pady=8)
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.status_var).grid(
            row=8, column=0, columnspan=3, sticky="w", padx=12)

        # 로그
        self.log_txt = tk.Text(frm, height=13, wrap="word", state="disabled",
                               font=("Consolas", 13))
        self.log_txt.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=12, pady=8)
        frm.rowconfigure(9, weight=1)
        sb = ttk.Scrollbar(frm, command=self.log_txt.yview)
        sb.grid(row=9, column=3, sticky="ns", pady=8)
        self.log_txt["yscrollcommand"] = sb.set

        # -------------------------------------------------------------
        # 우측 프레임: 펼쳐진 연재중 소설 목록
        # -------------------------------------------------------------
        right_frm = ttk.LabelFrame(main_pane, text=" 연재중 소설 목록 ", padding=10)
        main_pane.add(right_frm, weight=1)

        top_ong_frm = ttk.Frame(right_frm)
        top_ong_frm.pack(fill="x", pady=(0, 6))
        self.ongoing_count_lbl = ttk.Label(top_ong_frm, text="등록된 소설: 0편", font=("맑은 고딕", 12, "bold"))
        self.ongoing_count_lbl.pack(side="left")

        # 우측 상단 '?' 헬프 버튼
        self.help_btn = ttk.Button(top_ong_frm, text="?", width=3, command=self.open_help_window)
        self.help_btn.pack(side="right", padx=(6, 0))
        ttk.Button(top_ong_frm, text="새로고침", command=self.refresh_ongoing_list, width=8).pack(side="right")

        tree_frm = ttk.Frame(right_frm)
        tree_frm.pack(fill="both", expand=True)

        columns = ("title", "total", "fmt")
        self.ongoing_tree = ttk.Treeview(tree_frm, columns=columns, show="headings", selectmode="browse")
        self.ongoing_tree.heading("title", text="소설 제목")
        self.ongoing_tree.heading("total", text="회차")
        self.ongoing_tree.heading("fmt", text="포맷")

        self.ongoing_tree.column("title", width=190, minwidth=110, anchor="w")
        self.ongoing_tree.column("total", width=65, minwidth=50, anchor="center")
        self.ongoing_tree.column("fmt", width=55, minwidth=45, anchor="center")

        tree_sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.ongoing_tree.yview)
        self.ongoing_tree.configure(yscrollcommand=tree_sb.set)

        self.ongoing_tree.pack(side="left", fill="both", expand=True)
        tree_sb.pack(side="right", fill="y")

        self.ongoing_tree.bind("<<TreeviewSelect>>", self.on_ongoing_tree_selected)
        self.ongoing_tree.bind("<Double-1>", self.on_ongoing_tree_double_click)

        ttk.Label(right_frm, text="※ 소설을 선택하면 입력창에 자동 로드됩니다.",
                  font=("맑은 고딕", 10), foreground="#666666").pack(anchor="w", pady=(6, 6))

        ong_btn_frm = ttk.Frame(right_frm)
        ong_btn_frm.pack(fill="x", pady=(4, 0))

        ttk.Button(ong_btn_frm, text="선택 소설 불러오기", command=self.load_selected_ongoing).pack(fill="x", pady=2)
        ttk.Button(ong_btn_frm, text="선택 소설 업데이트", command=self.update_selected_ongoing_click).pack(fill="x", pady=2)
        ttk.Button(ong_btn_frm, text="전체 연재작 업데이트", command=self.update_ongoing_click).pack(fill="x", pady=2)
        ttk.Button(ong_btn_frm, text="선택 소설 목록에서 제거", command=self.remove_ongoing_click).pack(fill="x", pady=2)

        self.ongoing_items_map = {}

        self._last_clipboard = ""
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_history_list()
        self.refresh_ongoing_list()
        self.root.after(100, self._drain_queue)
        self.root.after(600, self._check_clipboard)

    def refresh_history_list(self):
        novels = scrape_novel.get_cached_novels()
        self.cached_novels_map.clear()
        display_list = []
        for n in novels:
            clean_title = re.sub(r"[\r\n\t\s]+", " ", n.get('title') or "").strip()
            total_str = f"{n['total']}" if n['total'] else "?"
            pct = f" ({int(n['done_count']/n['total']*100)}%)" if n['total'] else ""
            label = f"{clean_title} [{n['done_count']}/{total_str}화 완료]{pct}"
            self.cached_novels_map[label] = n
            display_list.append(label)
        self.history_cb["values"] = display_list
        if display_list and not self.history_var.get():
            self.history_var.set(display_list[0])

    def _on_format_changed(self):
        curr = self.out_var.get().strip()
        if not curr:
            return
        p = Path(curr)
        new_ext = ".epub" if self.file_format.get() == "epub" else ".txt"
        if p.suffix.lower() in [".txt", ".epub"]:
            self.out_var.set(str(p.with_suffix(new_ext)))

    def on_history_selected(self, event=None):
        label = self.history_var.get()
        item = self.cached_novels_map.get(label)
        if item:
            self.url_var.set(item["url"])
            safe_name = scrape_novel._safe_filename(item["title"])
            ext = ".epub" if self.file_format.get() == "epub" else ".txt"
            out_file = str(self.get_current_download_dir() / f"{safe_name}{ext}")
            out_file = re.sub(r"[\r\n\t]+", "", out_file)
            self.out_var.set(out_file)

    def load_history_click(self):
        label = self.history_var.get()
        if not label:
            messagebox.showinfo("이력 없음", "이전에 수집 중이던 소설 이력이 없습니다.")
            return
        self.on_history_selected()
        self.start()

    def refresh_ongoing_list(self):
        novels = scrape_novel.get_ongoing_novels()
        self.ongoing_items_map = {}
        if hasattr(self, "ongoing_tree"):
            for item in self.ongoing_tree.get_children():
                self.ongoing_tree.delete(item)

        if hasattr(self, "ongoing_count_lbl"):
            self.ongoing_count_lbl.config(text=f"등록된 소설: {len(novels)}편")

        for n in novels:
            raw_title = n.get("title") or ""
            clean_title = scrape_novel.format_ongoing_filename(raw_title, 0)
            clean_title = re.sub(r"[\r\n\t\s]+", " ", clean_title).strip()
            total_str = f"{n.get('total')}화" if n.get("total") else "-"
            fmt_str = (n.get("format") or "epub").upper()
            if hasattr(self, "ongoing_tree"):
                item_id = self.ongoing_tree.insert("", "end", values=(clean_title, total_str, fmt_str))
                self.ongoing_items_map[item_id] = n

    def _get_selected_ongoing_novel(self):
        if not hasattr(self, "ongoing_tree"):
            return None
        selected = self.ongoing_tree.selection()
        if not selected:
            return None
        return self.ongoing_items_map.get(selected[0])

    def on_ongoing_tree_selected(self, event=None):
        item = self._get_selected_ongoing_novel()
        if item:
            self._apply_novel_info(item)

    def on_ongoing_tree_double_click(self, event=None):
        item = self._get_selected_ongoing_novel()
        if item:
            self._apply_novel_info(item)
            self._log(f"[*] 연재중 소설 로드 완료: {item.get('title')}")

    def load_selected_ongoing(self):
        item = self._get_selected_ongoing_novel()
        if not item:
            messagebox.showinfo("선택 필요", "불러올 연재중 소설을 목록에서 선택하세요.")
            return
        self._apply_novel_info(item)
        messagebox.showinfo("로드 완료", f"'{item.get('title')}' 소설 설정이 입력창에 로드되었습니다.")

    def _apply_novel_info(self, item):
        self.url_var.set(item.get("url", ""))
        if item.get("out_path"):
            self.out_var.set(item["out_path"])
            p = Path(item["out_path"])
            if p.suffix.lower() == ".epub":
                self.file_format.set("epub")
            elif p.suffix.lower() == ".txt":
                self.file_format.set("txt")
        if "also_save_other" in item:
            self.also_save_other.set(bool(item["also_save_other"]))
        self.is_ongoing_var.set(True)

    def update_selected_ongoing_click(self):
        item = self._get_selected_ongoing_novel()
        if not item:
            messagebox.showinfo("선택 필요", "업데이트할 연재중 소설을 목록에서 선택하세요.")
            return
        self._apply_novel_info(item)
        novel_title = item.get("title", "선택된 소설")
        if not messagebox.askyesno("소설 업데이트", f"'{novel_title}' 소설의 최신 연재분을 업데이트할까요?"):
            return
        self.start()

    def remove_ongoing_click(self):
        item = self._get_selected_ongoing_novel()
        if not item:
            messagebox.showinfo("선택 필요", "제거할 연재중 소설을 목록에서 선택하세요.")
            return
        novel_title = item.get("title", "선택된 소설")
        if messagebox.askyesno("연재중 목록에서 제거", f"'{novel_title}' 소설을 연재중 목록에서 제거할까요?\n(소설이 완결되었거나 더 이상 자동 업데이트를 원하지 않을 때 제거합니다. 기존에 저장된 파일은 그대로 보존됩니다.)"):
            scrape_novel.remove_ongoing_novel(item.get("novel_id") or item.get("url"))
            self.refresh_ongoing_list()
            self._log(f"[*] 연재중 목록에서 제거 완료: {novel_title}")
            if self.url_var.get().strip() == item.get("url", "").strip():
                self.is_ongoing_var.set(False)
            messagebox.showinfo("완료", f"'{novel_title}' 소설이 연재중 목록에서 제거되었습니다.")

    def update_ongoing_click(self):
        novels = scrape_novel.get_ongoing_novels()
        if not novels:
            messagebox.showinfo("목록 비어있음", "등록된 연재중 소설이 없습니다.\n소설을 다운로드할 때 '연재중 소설로 등록'을 체크하시면 이곳에 등록됩니다.")
            return

        titles = [n.get("title", "제목미상") for n in novels]
        summary = "\n".join(f"• {t}" for t in titles[:10])
        if len(titles) > 10:
            summary += f"\n... 외 {len(titles) - 10}편"

        if not messagebox.askyesno("연재중 소설 업데이트", f"등록된 연재중 소설 총 {len(novels)}편의 최신 연재분을 순차적으로 업데이트합니다:\n\n{summary}\n\n지금 시작할까요?"):
            return

        self.stop_event.clear()
        self.start_btn["state"] = "disabled"
        if hasattr(self, "batch_update_btn"):
            self.batch_update_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.status_var.set("연재중 소설 일괄 업데이트 시작 중…")
        self.progress["value"] = 0

        common_params = dict(
            min_delay=float(self.min_delay.get()),
            max_delay=max(float(self.max_delay.get()), float(self.min_delay.get())),
            limit=int(self.limit.get()),
            proxy=self.proxy.get().strip() or None,
            headful=bool(self.headful.get()),
            solve_cf=bool(self.solve_cf.get()),
        )
        self.worker = threading.Thread(target=self._run_batch_update, args=(novels,), kwargs=common_params, daemon=True)
        self.worker.start()

    def _run_batch_update(self, novels, **common_params):
        total_novels = len(novels)
        success_count = 0
        failed_novels = []

        self._log("\n" + "=" * 50)
        self._log(f"[*] 연재중 소설 일괄 업데이트 시작 (총 {total_novels}편)")
        self._log("=" * 50)

        for i, novel in enumerate(novels, start=1):
            if self.stop_event.is_set():
                self._log("[중지] 사용자에 의해 연재중 소설 업데이트가 중지되었습니다.")
                break

            title = novel.get("title") or f"소설_{novel.get('novel_id', i)}"
            url = novel.get("url")
            out_path = novel.get("out_path")
            also_save_other = bool(novel.get("also_save_other", False))

            self._log(f"\n[{i}/{total_novels}] '{title}' 업데이트 확인 중…")
            self.status_var.set(f"[{i}/{total_novels}] {title} 업데이트 중…")

            try:
                path = scrape_novel.scrape(
                    url, out_path=out_path,
                    also_save_other=also_save_other,
                    log=self._log, should_stop=self.stop_event.is_set,
                    on_progress=self._progress,
                    **common_params
                )
                success_count += 1
                try:
                    nid = scrape_novel.parse_novel_id(url)
                    latest_ep = scrape_novel.get_latest_done_episode(nid)
                    if latest_ep > 0:
                        new_path = scrape_novel.rename_ongoing_file(path, latest_ep, also_save_other=also_save_other)
                        if new_path and new_path.exists():
                            path = str(new_path)
                            self._log(f"[*] 연재중 파일명 최종화 갱신: {new_path.name}")
                    final_p = Path(path)
                    novel["total"] = latest_ep if latest_ep > 0 else novel.get("total", 0)
                    novel["out_path"] = path
                    novel["title"] = scrape_novel.format_ongoing_filename(final_p.stem, 0)
                    scrape_novel.save_ongoing_novel(novel)
                except Exception:
                    pass
            except (scrape_novel.QuotaError, scrape_novel.BlockedError) as e:
                self._log(f"[열람 제한] '{title}' 작업 중 제한 발생: {e}")
                failed_novels.append((title, str(e)))
                if self.auto_quota_retry.get() and not self.stop_event.is_set():
                    wait_secs = scrape_novel.seconds_until_midnight(target_minute=1)
                    cur_h = wait_secs // 3600
                    cur_m = (wait_secs % 3600) // 60
                    self._log(f"[자동 재시도 모드] 일일 쿼터 리셋 시점(자정 00:01)까지 대기합니다. (약 {cur_h}시간 {cur_m}분 후 재개)")
                    for s in range(wait_secs, 0, -1):
                        if self.stop_event.is_set():
                            break
                        if s % 60 == 0 or s == wait_secs or s <= 10:
                            h = s // 3600
                            m = (s % 3600) // 60
                            rem_str = f"{h}시간 {m}분" if h > 0 else f"{m}분 {s % 60}초"
                            self.status_var.set(f"자정 쿼터 리셋 대기 중… {rem_str} 후 재개 (00:01)")
                        import time
                        time.sleep(1)
                    if not self.stop_event.is_set():
                        self._log(f"\n[*] 자정 리셋 완료. '{title}'부터 업데이트를 재개합니다!")
                        return self._run_batch_update(novels[i-1:], **common_params)
                break
            except Exception as e:  # noqa: BLE001
                self._log(f"[오류] '{title}' 업데이트 실패: {e}")
                failed_novels.append((title, str(e)))

        msg = f"\n[*] 연재중 소설 일괄 업데이트 완료! (성공: {success_count}/{total_novels}"
        if failed_novels:
            msg += f", 실패: {len(failed_novels)}편)"
        else:
            msg += ")"
        self._log(msg)

        batch_result = {
            "ok": True,
            "path": None,
            "error": None,
            "is_batch": True,
            "summary": f"연재중 소설 {total_novels}편 중 {success_count}편 업데이트 완료" + (f"\n(실패: {len(failed_novels)}편)" if failed_novels else "")
        }
        self.log_q.put(("done", batch_result))

    # ---- helpers ----
    def get_current_download_dir(self):
        if hasattr(self, "download_dir_var"):
            d = self.download_dir_var.get().strip()
            if d:
                p = Path(d)
                try:
                    if p.exists() and p.is_dir():
                        return p
                except Exception:
                    pass
        return scrape_novel.get_default_download_dir()

    @staticmethod
    def _default_downloads():
        return scrape_novel.get_default_download_dir()

    def _on_dir_entry_change(self, event=None):
        raw = self.download_dir_var.get().strip()
        if raw:
            p = Path(raw)
            try:
                p.mkdir(parents=True, exist_ok=True)
                self.set_download_dir(str(p))
            except Exception:
                pass

    def set_download_dir(self, folder):
        p = Path(folder)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.download_dir_var.set(str(p))
        scrape_novel.save_settings({"download_dir": str(p)})
        self._log(f"[*] 기본 저장 폴더 설정: {p}")

        # 현재 지정된 파일명을 새 폴더 경로로 갱신
        cur_out = self.out_var.get().strip()
        if cur_out:
            file_name = Path(cur_out).name
        else:
            ext = ".epub" if self.file_format.get() == "epub" else ".txt"
            file_name = f"소설{ext}"
        self.out_var.set(str(p / file_name))

    def browse_dir(self):
        init_dir = str(self.get_current_download_dir())
        folder = filedialog.askdirectory(
            title="기본 저장 폴더 선택 (구글 드라이브 폴더 가능)",
            initialdir=init_dir
        )
        if folder:
            self.set_download_dir(folder)

    def set_google_drive_dir(self):
        gdrive = scrape_novel.detect_google_drive_dir()
        if gdrive and gdrive.exists():
            self.set_download_dir(str(gdrive))
            messagebox.showinfo(
                "구글 드라이브 설정 완료",
                f"기본 저장 폴더가 구글 드라이브로 설정되었습니다:\n\n{gdrive}"
            )
        else:
            if messagebox.askyesno(
                "구글 드라이브 폴더 선택",
                "구글 드라이브 기본 위치가 자동으로 감지되지 않았습니다.\n"
                "(Google Drive 데스크톱 앱이 실행 중이어야 합니다)\n\n"
                "탐색기에서 직접 구글 드라이브 폴더(예: G:\\내 드라이브)를 선택하시겠습니까?"
            ):
                folder = filedialog.askdirectory(title="구글 드라이브 폴더를 선택하세요")
                if folder:
                    self.set_download_dir(folder)

    def browse_out(self):
        ext = ".epub" if self.file_format.get() == "epub" else ".txt"
        init = Path(self.out_var.get() or (self.get_current_download_dir() / f"소설{ext}"))
        ftypes = [("EPUB 전자책", "*.epub"), ("텍스트 파일", "*.txt"), ("모든 파일", "*.*")] if ext == ".epub" else [("텍스트 파일", "*.txt"), ("EPUB 전자책", "*.epub"), ("모든 파일", "*.*")]
        path = filedialog.asksaveasfilename(
            title="저장 위치와 파일 이름 선택",
            defaultextension=ext,
            initialdir=str(init.parent),
            initialfile=init.name,
            filetypes=ftypes,
        )
        if path:
            self.out_var.set(path)
            if path.lower().endswith(".epub"):
                self.file_format.set("epub")
            elif path.lower().endswith(".txt"):
                self.file_format.set("txt")

    def clear_log(self):
        self.log_txt["state"] = "normal"
        self.log_txt.delete("1.0", "end")
        self.log_txt["state"] = "disabled"

    def _log(self, msg):
        self.log_q.put(("log", msg))

    def _progress(self, done, total):
        self.log_q.put(("progress", (done, total)))

    def _drain_queue(self):
        try:
            while True:
                kind, data = self.log_q.get_nowait()
                if kind == "log":
                    self.log_txt["state"] = "normal"
                    self.log_txt.insert("end", data + "\n")
                    self.log_txt.see("end")
                    self.log_txt["state"] = "disabled"
                elif kind == "detected_title":
                    url, title = data
                    if self.url_var.get() == url:
                        safe_name = scrape_novel._safe_filename(title)
                        ext = ".epub" if self.file_format.get() == "epub" else ".txt"
                        out_file = str(self.get_current_download_dir() / f"{safe_name}{ext}")
                        out_file = re.sub(r"[\r\n\t]+", "", out_file)
                        self.out_var.set(out_file)
                        self.status_var.set("대기 중")
                        self._log(f"[*] 소설 제목 확인: {title} -> 저장 파일: {safe_name}{ext}")
                        try:
                            nid = scrape_novel.parse_novel_id(url)
                            if scrape_novel.is_ongoing_novel(nid):
                                self.is_ongoing_var.set(True)
                        except Exception:
                            pass
                        if self.auto_start_on_clipboard.get() and not (self.worker and self.worker.is_alive()):
                            self.start()
                elif kind == "progress":
                    done, total = data
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.status_var.set(f"{done} / {total} 화")
                elif kind == "done":
                    self._on_finished(data)
                elif kind == "install_done":
                    self.install_btn["state"] = "normal"
                    self.start_btn["state"] = "normal"
                    if data:
                        self.status_var.set("브라우저 설치 오류")
                        messagebox.showerror("오류", data)
                    else:
                        self.status_var.set("브라우저 준비 완료")
                        messagebox.showinfo("완료", "브라우저 준비 완료. 이제 스크래핑을 시작할 수 있습니다.")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _on_url_entry_event(self, event=None):
        url = self.url_var.get().strip()
        if url and "/novel/" in url:
            self._update_title_and_filename(url, auto_start=False)

    def _check_clipboard(self):
        if self.auto_clipboard.get() and not (self.worker and self.worker.is_alive()):
            try:
                clip = self.root.clipboard_get().strip()
                if clip and clip != self._last_clipboard:
                    self._last_clipboard = clip
                    m = re.search(r"https?://[^\s]+/novel/\d+[^\s]*", clip)
                    if m:
                        detected_url = m.group(0)
                        if detected_url != self.url_var.get():
                            self._on_new_url_detected(detected_url)
            except Exception:
                pass
        self.root.after(700, self._check_clipboard)

    def _on_new_url_detected(self, url):
        self.url_var.set(url)
        self._log(f"\n[클립보드 감지] 소설 URL 자동 입력: {url}")
        self._update_title_and_filename(url, auto_start=self.auto_start_on_clipboard.get())

    def _update_title_and_filename(self, url, auto_start=False):
        cached_title = None
        try:
            novel_id = scrape_novel.parse_novel_id(url)
            cache_dir = scrape_novel.CACHE_ROOT / novel_id
            state_file = cache_dir / "state.json"
            if state_file.exists():
                data = json.loads(state_file.read_text(encoding="utf-8"))
                cached_title = data.get("title")
            if scrape_novel.is_ongoing_novel(novel_id):
                self.is_ongoing_var.set(True)
            else:
                self.is_ongoing_var.set(False)
        except Exception:
            pass

        ext = ".epub" if self.file_format.get() == "epub" else ".txt"
        if cached_title:
            safe_name = scrape_novel._safe_filename(cached_title)
            out_file = str(self.get_current_download_dir() / f"{safe_name}{ext}")
            out_file = re.sub(r"[\r\n\t]+", "", out_file)
            self.out_var.set(out_file)
            self._log(f"[*] 소설 제목 확인(이력): {cached_title} -> 저장 파일: {safe_name}{ext}")
            if auto_start and not (self.worker and self.worker.is_alive()):
                self.start()
        else:
            self.status_var.set("소설 제목 확인 중…")
            def _fetch_title():
                title = scrape_novel.quick_fetch_novel_title(url)
                self.log_q.put(("detected_title", (url, title)))
            threading.Thread(target=_fetch_title, daemon=True).start()

    # ---- run control ----
    def start(self):
        url = self.url_var.get().strip()
        out = self.out_var.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "소설 목록 URL을 입력하세요.")
            return
        if "/novel/" not in url:
            messagebox.showwarning("URL 확인", "newtoki 소설 목록 URL 형식이 아닙니다.\n예: https://newtoki1.org/novel/62637")
            return
        if not out:
            messagebox.showwarning("입력 필요", "저장할 파일 경로를 지정하세요.")
            return

        self.stop_event.clear()
        self.start_btn["state"] = "disabled"
        if hasattr(self, "batch_update_btn"):
            self.batch_update_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.status_var.set("시작 중…")
        self.progress["value"] = 0

        is_ongoing = bool(self.is_ongoing_var.get())
        if is_ongoing:
            try:
                nid = scrape_novel.parse_novel_id(url)
                scrape_novel.save_ongoing_novel({
                    "novel_id": nid,
                    "title": Path(out).stem,
                    "url": url,
                    "out_path": out,
                    "format": self.file_format.get(),
                    "also_save_other": bool(self.also_save_other.get()),
                })
                self.refresh_ongoing_list()
            except Exception:
                pass
        else:
            try:
                nid = scrape_novel.parse_novel_id(url)
                if scrape_novel.is_ongoing_novel(nid):
                    scrape_novel.remove_ongoing_novel(nid)
                    self.refresh_ongoing_list()
            except Exception:
                pass

        params = dict(
            url=url, out_path=out,
            min_delay=float(self.min_delay.get()),
            max_delay=max(float(self.max_delay.get()), float(self.min_delay.get())),
            limit=int(self.limit.get()),
            proxy=self.proxy.get().strip() or None,
            headful=bool(self.headful.get()),
            solve_cf=bool(self.solve_cf.get()),
            also_save_other=bool(self.also_save_other.get()),
            is_ongoing=is_ongoing,
        )
        self.worker = threading.Thread(target=self._run, kwargs=params, daemon=True)
        self.worker.start()

    def _run(self, **params):
        result = {"ok": False, "path": None, "error": None}
        try:
            path = scrape_novel.scrape(
                params["url"], out_path=params["out_path"],
                min_delay=params["min_delay"], max_delay=params["max_delay"],
                limit=params["limit"], proxy=params["proxy"],
                headful=params["headful"], solve_cf=params["solve_cf"],
                also_save_other=params.get("also_save_other", False),
                log=self._log, should_stop=self.stop_event.is_set,
                on_progress=self._progress,
            )
            result["ok"] = True
            result["path"] = path

            # 연재중 소설 등록 및 최종화 번호 파일명 갱신
            if params.get("is_ongoing"):
                try:
                    nid = scrape_novel.parse_novel_id(params["url"])
                    latest_ep = scrape_novel.get_latest_done_episode(nid)
                    if latest_ep > 0:
                        new_path = scrape_novel.rename_ongoing_file(
                            path, latest_ep, also_save_other=params.get("also_save_other", False)
                        )
                        if new_path and new_path.exists():
                            path = str(new_path)
                            result["path"] = path
                            self._log(f"[*] 연재중 파일명 최종화 갱신: {new_path.name}")
                    final_p = Path(path)
                    scrape_novel.save_ongoing_novel({
                        "novel_id": nid,
                        "title": scrape_novel.format_ongoing_filename(final_p.stem, 0),
                        "url": params["url"],
                        "out_path": str(final_p),
                        "format": "epub" if final_p.suffix.lower() == ".epub" else "txt",
                        "also_save_other": bool(params.get("also_save_other", False)),
                        "total": latest_ep if latest_ep > 0 else 0,
                    })
                except Exception:
                    pass
        except (scrape_novel.QuotaError, scrape_novel.BlockedError) as e:
            if self.auto_quota_retry.get() and not self.stop_event.is_set():
                import time
                wait_secs = scrape_novel.seconds_until_midnight(target_minute=1)
                h = wait_secs // 3600
                m = (wait_secs % 3600) // 60
                time_desc = f"{h}시간 {m}분" if h > 0 else f"{m}분"
                self._log(f"\n[열람 제한 감지] {e}")
                self._log(f"[자동 재시도 모드] 일일 쿼터 리셋 시점(자정 00:01)까지 대기합니다. (약 {time_desc} 후 재개)")
                for s in range(wait_secs, 0, -1):
                    if self.stop_event.is_set():
                        break
                    if s % 60 == 0 or s == wait_secs or s <= 10:
                        cur_h = s // 3600
                        cur_m = (s % 3600) // 60
                        rem_str = f"{cur_h}시간 {cur_m}분" if cur_h > 0 else f"{cur_m}분 {s % 60}초"
                        self.status_var.set(f"자정 쿼터 리셋 대기 중… {rem_str} 후 재시도 (00:01)")
                    time.sleep(1)
                if not self.stop_event.is_set():
                    self._log("\n[*] 자정 쿼터 리셋 대기 완료. 수집을 재개합니다!")
                    return self._run(**params)
            result["error"] = str(e)
            self._log(f"[오류] {e}")
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
            self._log(f"[오류] {e}")
        self.log_q.put(("done", result))

    def _on_finished(self, result):
        self.start_btn["state"] = "normal"
        self.stop_btn["state"] = "disabled"
        if hasattr(self, "batch_update_btn"):
            self.batch_update_btn["state"] = "normal"
        self.refresh_history_list()
        self.refresh_ongoing_list()
        if result.get("is_batch"):
            self.status_var.set("연재중 업데이트 완료")
            messagebox.showinfo("업데이트 완료", result.get("summary", "연재중 소설 업데이트가 완료되었습니다."))
        elif result["ok"]:
            if result.get("path"):
                self.out_var.set(result["path"])
            self.status_var.set("완료")
            if messagebox.askyesno("완료", f"저장 완료:\n{result['path']}\n\n폴더를 열까요?"):
                self._open_folder(result["path"])
        elif result["error"]:
            self.status_var.set("오류")
            messagebox.showerror("오류", _friendly_error(result["error"]))

    @staticmethod
    def _open_folder(path):
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(Path(path))])
        except Exception:  # noqa: BLE001
            pass

    def install_browser_click(self):
        self.install_btn["state"] = "disabled"
        self.start_btn["state"] = "disabled"
        self.status_var.set("브라우저 설치 중…")

        def worker():
            err = None
            try:
                install_browser(self._log)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                self._log(f"[오류] {e}")
            self.log_q.put(("install_done", err))

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("중지 요청됨… 현재 챕터 마무리 중")
        self.stop_btn["state"] = "disabled"

    def open_help_window(self):
        if hasattr(self, "help_win") and self.help_win and self.help_win.winfo_exists():
            self.help_win.deiconify()
            self.help_win.lift()
            self.help_win.focus_force()
            return

        self.help_win = tk.Toplevel(self.root)
        win = self.help_win
        win.title("소설 스크래퍼 사용 안내서")
        win.geometry("780x860")
        win.minsize(620, 520)

        # 모달이 아닌 독립 팝업 (grab_set 미사용)
        # 메인 창과 함께 최소화/복원되도록 transient 설정
        win.transient(self.root)

        # 상단 헤더 영역
        header = ttk.Frame(win, padding=(18, 14, 18, 10))
        header.pack(fill="x")
        title_lbl = ttk.Label(header, text="📖 소설 스크래퍼 사용 안내서", font=("맑은 고딕", 16, "bold"))
        title_lbl.pack(side="left")
        close_btn = ttk.Button(header, text="닫기 (ESC)", command=win.destroy)
        close_btn.pack(side="right")

        # 본문 프레임
        body_frm = ttk.Frame(win, padding=(18, 0, 18, 10))
        body_frm.pack(fill="both", expand=True)

        txt = tk.Text(body_frm, wrap="word", font=("맑은 고딕", 11), padx=14, pady=14,
                      bg="#FFFFFF", fg="#1E293B", relief="solid", bd=1)
        sb = ttk.Scrollbar(body_frm, command=txt.yview)
        txt["yscrollcommand"] = sb.set

        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 스타일 태그
        txt.tag_configure("h1", font=("맑은 고딕", 13, "bold"), foreground="#1D4ED8", spacing1=14, spacing3=6)
        txt.tag_configure("body", font=("맑은 고딕", 11), spacing1=2, spacing3=2)
        txt.tag_configure("bullet", font=("맑은 고딕", 11), lmargin1=16, lmargin2=32, spacing1=3, spacing3=3)
        txt.tag_configure("subbullet", font=("맑은 고딕", 10), lmargin1=32, lmargin2=48, spacing1=2, spacing3=2, foreground="#475569")
        txt.tag_configure("highlight", font=("맑은 고딕", 11, "bold"), foreground="#B45309")
        txt.tag_configure("tip", font=("맑은 고딕", 10, "bold"), foreground="#059669")

        def add_h1(text):
            txt.insert("end", f"\n■ {text}\n", "h1")

        def add_bullet(lead, body=""):
            txt.insert("end", f"• {lead} ", "highlight")
            if body:
                txt.insert("end", f"{body}\n", "bullet")
            else:
                txt.insert("end", "\n", "bullet")

        def add_subbullet(text):
            txt.insert("end", f"  - {text}\n", "subbullet")

        def add_p(text):
            txt.insert("end", f"{text}\n", "body")

        # -----------------------------------------------------------------
        # 안내서 상세 내용
        # -----------------------------------------------------------------
        add_h1("1. 기본 저장 폴더 및 구글 드라이브 연동")
        add_bullet("기본 저장 폴더 지정", "소설 파일이 저장될 위치를 원하는 폴더로 변경할 수 있습니다.")
        add_subbullet("[폴더 변경…] 버튼을 클릭하여 PC 내 원하는 폴더(D드라이브, 외장하드 등)를 선택합니다.")
        add_subbullet("지정된 폴더는 자동 저장되어 프로그램을 다시 켜도 계속 유지됩니다.")
        add_bullet("구글 드라이브(Google Drive) 폴더 지정", "")
        add_subbullet("[구글 드라이브] 버튼을 누르면 PC에 연결된 구글 드라이브(G:\\내 드라이브 등)를 자동 감지하여 설정합니다.")
        add_subbullet("구글 드라이브로 지정 시 다운로드된 소설이 스마트폰/태블릿의 구글 드라이브 앱과 즉시 자동 동기화됩니다.")

        add_h1("2. 기본 소설 다운로드 방법")
        add_bullet("1단계: 소설 URL 복사 (클립보드 자동 감지)", "")
        add_subbullet("웹 브라우저에서 소설 목록 페이지의 주소를 복사합니다.")
        add_subbullet("프로그램이 클립보드를 실시간 감지하여 소설 제목 확인 및 저장 파일명을 자동으로 채웁니다.")
        add_subbullet("수동 입력 시 '소설 목록 URL' 칸에 붙여넣고 Enter 또는 다른 곳을 클릭하세요.")
        add_bullet("2단계: 저장 포맷 선택", "")
        add_subbullet("EPUB 또는 TEXT 포맷을 선택합니다.")
        add_bullet("3단계: 수집 시작", "")
        add_subbullet("[시작] 버튼을 누르면 브라우저를 통해 본문 수집이 진행됩니다.")
        add_subbullet("사이트에 표지 이미지가 있는 경우 전자책(EPUB) 표지로 자동 포함됩니다.")

        add_h1("3. 이어서 수집 (이어받기)")
        add_bullet("중단된 작업 이어받기", "")
        add_subbullet("수집 중 [중지] 버튼을 눌렀거나, 사이트 제한/네트워크 오류로 중단된 경우 사용합니다.")
        add_subbullet("상단의 '이전 수집 이력' 목록에서 해당 소설을 선택하고 [이어서 수집]을 누릅니다.")
        add_subbullet("이미 수집 완료된 회차는 자동으로 건너뛰고, 남은 회차만 고속으로 이어서 다운로드합니다.")

        add_h1("4. 연재 소설 관리 및 자동 업데이트")
        add_bullet("[연재 소설로 등록] 체크", "")
        add_subbullet("소설을 다운로드할 때 체크하면 우측의 「연재중 소설 목록」에 자동 보관됩니다.")
        add_bullet("우측 패널 기능 활용", "")
        add_subbullet("소설 선택/더블클릭: 목록에서 소설을 클릭하면 해당 소설의 설정이 입력창에 즉시 로드됩니다.")
        add_subbullet("[선택 소설 업데이트]: 선택한 소설의 최신 연재분만 즉시 이어받습니다.")
        add_subbullet("[전체 연재작 업데이트]: 등록된 모든 연재 소설을 순차적으로 일괄 업데이트합니다.")
        add_subbullet("[선택 소설 목록에서 제거]: 완결되었거나 더 이상 업데이트를 확인하지 않을 소설을 목록에서 제외합니다.")
        add_bullet("최종화 번호 자동 반영 (파일명 자동 변경)", "")
        add_subbullet("수집 또는 업데이트 완료 시 파일명 끝에 자동으로 최신 화수 번호가 붙습니다.")
        add_subbullet("예: '소설제목.epub' → '소설제목 [150화].epub'")
        add_subbullet("이후 180화까지 추가 업데이트되면 '소설제목 [180화].epub'로 스마트하게 자동 교체됩니다.")

        add_h1("5. 주요 옵션 가이드")
        add_bullet("최소 / 최대 지연(초)", "회차 사이의 대기 시간입니다. 사이트 차단 방지를 위해 기본 15~20초를 권장합니다.")
        add_bullet("개수 제한(0=전체)", "0은 전체 완결/최신화까지 수집하며, 특정 숫자 입력 시 해당 화수만큼만 수집합니다.")
        add_bullet("창 표시 / CF 우회", "Cloudflare 보안 검사나 캡차가 뜨는 사이트인 경우 활성화합니다.")
        add_bullet("쿼터시 자정(0시)후 자동재시도", "일일 열람 제한에 도달했을 때 켜두면, 자정(00:01)에 자동으로 풀리는 시점을 기다려 수집을 재개합니다.")
        add_bullet("클립보드 자동 감지", "브라우저에서 주소 복사 시 자동으로 가져오는 편리 기능입니다.")

        add_h1("6. 문제 해결 및 팁")
        add_bullet("Q. 본문이 비어 있는 게시물이 있어요.", "")
        add_subbullet("공지나 삭제된 게시물 등 본문이 없는 회차는 자동으로 건너뛰며, 연속 실패가 아닐 경우 쿼터 초과로 오인하지 않고 정상 진행됩니다.")
        add_bullet("Q. '열람 제한' 또는 '차단' 오류가 발생해요.", "")
        add_subbullet("해당 사이트의 일일 열람 쿼터에 도달한 것입니다. 이미 받은 화수는 파일로 안전하게 보존되어 있으므로 자정 이후 [이어서 수집]하시면 됩니다.")

        add_p("\n")
        txt.config(state="disabled")

        # 하단 팁 바
        footer = ttk.Frame(win, padding=(18, 6, 18, 12))
        footer.pack(fill="x")
        ttk.Label(footer, text="💡 이 안내서 창을 띄워둔 상태로 메인 창의 모든 기능을 자유롭게 조작할 수 있습니다.",
                  font=("맑은 고딕", 10), foreground="#475569").pack(side="left")
        ttk.Button(footer, text="닫기", command=win.destroy, width=10).pack(side="right")

        win.bind("<Escape>", lambda e: win.destroy())

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("종료", "작업이 진행 중입니다. 종료할까요?\n(진행 상황은 저장되어 다음에 이어받습니다.)"):
                return
            self.stop_event.set()
        self.root.destroy()


def main():
    # 빌드 검증용: 얼어붙은(frozen) exe가 의존성을 제대로 담았는지 확인.
    # --windowed 빌드는 stdout이 없으므로 종료코드로 판정(0=성공, 3=실패).
    if "--selftest" in sys.argv:
        try:
            import scrapling.fetchers  # noqa: F401
            os._exit(0)
        except Exception as e:  # noqa: BLE001
            try:
                base = Path(getattr(sys, "_MEIPASS", ".")).parent
                (Path(sys.executable).parent / "selftest_error.txt").write_text(
                    repr(e), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            os._exit(3)

    # 빌드 검증용: frozen exe가 실제로 브라우저를 띄워 1화를 뽑는지 확인
    if "--scrapetest" in sys.argv:
        import tempfile
        url = sys.argv[sys.argv.index("--scrapetest") + 1]
        out = os.path.join(tempfile.gettempdir(), "scrapetest.txt")
        try:
            p = scrape_novel.scrape(url, out_path=out, limit=1,
                                    min_delay=0, max_delay=0)
            os._exit(0 if os.path.getsize(p) > 500 else 4)
        except Exception:  # noqa: BLE001
            import traceback
            try:
                (Path(sys.executable).parent / "scrapetest_error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            os._exit(5)

    if "--install-browser" in sys.argv:
        try:
            install_browser(print)
            os._exit(0)
        except Exception:  # noqa: BLE001
            os._exit(6)

    root = tk.Tk()
    try:  # 고해상도 화면 선명하게 (Windows)
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    ScraperGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
