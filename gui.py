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

import os
import queue
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
        root.geometry("720x560")
        root.minsize(640, 480)

        self.log_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

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
        url_entry = ttk.Entry(frm, textvariable=self.url_var)
        url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        url_entry.focus()

        # 출력 파일
        ttk.Label(frm, text="저장 파일").grid(row=2, column=0, sticky="w", **pad)
        default_out = str(self._default_downloads() / "소설.txt")
        self.out_var = tk.StringVar(value=default_out)
        ttk.Entry(frm, textvariable=self.out_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="찾아보기…", command=self.browse_out).grid(
            row=2, column=2, sticky="e", **pad)

        # 옵션들
        opt = ttk.LabelFrame(frm, text="옵션")
        opt.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        for c in range(6):
            opt.columnconfigure(c, weight=1)

        ttk.Label(opt, text="최소 지연(초)").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.min_delay = tk.DoubleVar(value=4.0)
        ttk.Spinbox(opt, from_=0, to=60, increment=1, width=6,
                    textvariable=self.min_delay).grid(row=0, column=1, sticky="w")

        ttk.Label(opt, text="최대 지연(초)").grid(row=0, column=2, sticky="w", padx=6)
        self.max_delay = tk.DoubleVar(value=9.0)
        ttk.Spinbox(opt, from_=0, to=120, increment=1, width=6,
                    textvariable=self.max_delay).grid(row=0, column=3, sticky="w")

        ttk.Label(opt, text="개수 제한(0=전체)").grid(row=0, column=4, sticky="w", padx=6)
        self.limit = tk.IntVar(value=0)
        ttk.Spinbox(opt, from_=0, to=100000, increment=1, width=8,
                    textvariable=self.limit).grid(row=0, column=5, sticky="w")

        ttk.Label(opt, text="프록시(선택)").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.proxy = tk.StringVar(value="")
        ttk.Entry(opt, textvariable=self.proxy).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=6)

        self.headful = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="창 표시", variable=self.headful).grid(
            row=1, column=3, sticky="w", padx=6)
        self.solve_cf = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="CF 우회", variable=self.solve_cf).grid(
            row=1, column=4, sticky="w", padx=6)
        self.auto_quota_retry = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="쿼터시 1시간후 자동재시도", variable=self.auto_quota_retry).grid(
            row=1, column=5, sticky="w", padx=6)

        # 버튼
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", padx=10)
        self.start_btn = ttk.Button(btns, text="시작", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="중지", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.install_btn = ttk.Button(btns, text="브라우저 설치",
                                      command=self.install_browser_click)
        self.install_btn.pack(side="left")
        ttk.Button(btns, text="로그 지우기", command=self.clear_log).pack(side="left", padx=6)

        # 진행률
        self.progress = ttk.Progressbar(frm, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=6)
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=3, sticky="w", padx=10)

        # 로그
        self.log_txt = tk.Text(frm, height=13, wrap="word", state="disabled")
        self.log_txt.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=10, pady=8)
        frm.rowconfigure(7, weight=1)
        sb = ttk.Scrollbar(frm, command=self.log_txt.yview)
        sb.grid(row=7, column=3, sticky="ns", pady=8)
        self.log_txt["yscrollcommand"] = sb.set

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_history_list()
        self.root.after(100, self._drain_queue)

    def refresh_history_list(self):
        novels = scrape_novel.get_cached_novels()
        self.cached_novels_map.clear()
        display_list = []
        for n in novels:
            total_str = f"{n['total']}" if n['total'] else "?"
            pct = f" ({int(n['done_count']/n['total']*100)}%)" if n['total'] else ""
            label = f"{n['title']} [{n['done_count']}/{total_str}화 완료]{pct}"
            self.cached_novels_map[label] = n
            display_list.append(label)
        self.history_cb["values"] = display_list
        if display_list and not self.history_var.get():
            self.history_var.set(display_list[0])

    def on_history_selected(self, event=None):
        label = self.history_var.get()
        item = self.cached_novels_map.get(label)
        if item:
            self.url_var.set(item["url"])
            safe_name = scrape_novel._safe_filename(item["title"])
            out_file = str(self._default_downloads() / f"{safe_name}.txt")
            if not self.out_var.get() or "소설.txt" in self.out_var.get():
                self.out_var.set(out_file)

    def load_history_click(self):
        label = self.history_var.get()
        if not label:
            messagebox.showinfo("이력 없음", "이전에 수집 중이던 소설 이력이 없습니다.")
            return
        self.on_history_selected()
        self.start()

    # ---- helpers ----
    @staticmethod
    def _default_downloads():
        d = Path.home() / "Downloads"
        return d if d.exists() else Path.home()

    def browse_out(self):
        init = Path(self.out_var.get() or (self._default_downloads() / "소설.txt"))
        path = filedialog.asksaveasfilename(
            title="저장 위치와 파일 이름 선택",
            defaultextension=".txt",
            initialdir=str(init.parent),
            initialfile=init.name,
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")],
        )
        if path:
            self.out_var.set(path)

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
        self.stop_btn["state"] = "normal"
        self.status_var.set("시작 중…")
        self.progress["value"] = 0

        params = dict(
            url=url, out_path=out,
            min_delay=float(self.min_delay.get()),
            max_delay=max(float(self.max_delay.get()), float(self.min_delay.get())),
            limit=int(self.limit.get()),
            proxy=self.proxy.get().strip() or None,
            headful=bool(self.headful.get()),
            solve_cf=bool(self.solve_cf.get()),
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
                log=self._log, should_stop=self.stop_event.is_set,
                on_progress=self._progress,
            )
            result["ok"] = True
            result["path"] = path
        except scrape_novel.QuotaError as e:
            if self.auto_quota_retry.get() and not self.stop_event.is_set():
                self._log(f"\n[쿼터 한도 대기] 일일 열람 제한에 도달했습니다: {e}")
                self._log("[자동 재시도 모드] 60분 후 자동으로 남아있는 화를 이어서 수집합니다...")
                import time
                wait_secs = 3600
                for s in range(wait_secs, 0, -1):
                    if self.stop_event.is_set():
                        break
                    if s % 300 == 0 or s == wait_secs or s <= 10:
                        mins = s // 60
                        self.status_var.set(f"쿼터 대기 중… {mins}분 후 재시도")
                    time.sleep(1)
                if not self.stop_event.is_set():
                    self._log("\n[*] 쿼터 대기 완료. 수집을 재개합니다!")
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
        self.refresh_history_list()
        if result["ok"]:
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
