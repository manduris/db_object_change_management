"""Main window: saved connections and Oracle change scripts."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from app.connection_store import ConnectionStore, StoreData
from app.models import ConnectionProfile, connection_fingerprint
from app.oracle_runner import (
    OracleSession,
    format_db_error,
    format_elapsed,
    request_cancel,
    run_script,
)
from app.sample_script import SAMPLE_SCRIPT
from app.script_splitter import (
    find_risks,
    preview_sql,
    split_oracle_script,
    statement_label,
    summarize_statements,
)
from app.theme import (
    ACCENT,
    ACCENT_HOVER,
    BG,
    CARD,
    CARD_ON,
    DANGER,
    DANGER_HOVER,
    EDITOR,
    LINE,
    MUTED,
    OK,
    PANEL,
    SECONDARY,
    SECONDARY_HOVER,
    STATUS_BG,
    TEXT,
    WARN,
)
from app.ui_connections import ConnectionManager

_STATUS_STYLE = {
    "connected": (OK, "연결됨"),
    "connecting": (WARN, "연결 중"),
    "failed": ("#f87171", "실패"),
    "disconnected": (MUTED, "끊김"),
}


def _text_widget(widget):
    return getattr(widget, "_textbox", widget)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class MainWindow(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=BG)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("오라클 오브젝트 변경 관리")
        self.geometry("1280x820")
        self.minsize(1100, 700)

        self._closed = False
        self.store = ConnectionStore()
        self.profiles: list[ConnectionProfile] = []
        self.sessions: dict[str, OracleSession] = {}
        self.pending: set[str] = set()
        self.connect_gen: dict[str, int] = {}
        self.active_id: str | None = None
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self._tx_busy = False
        self._run_session_id: str | None = None
        self._deferred_connect: set[str] = set()
        self.result_sets: list[tuple] = []
        self._result_labels: dict[str, int] = {}
        self.current_file: Path | None = None
        self.editor_dirty = False
        self._manager: ConnectionManager | None = None

        self.font_ui = ctk.CTkFont(family="Malgun Gothic", size=13)
        self.font_bold = ctk.CTkFont(family="Malgun Gothic", size=18, weight="bold")
        self.font_small = ctk.CTkFont(family="Malgun Gothic", size=12)
        self.font_mono = ctk.CTkFont(family="Consolas", size=13)
        self.commit_var = ctk.BooleanVar(value=True)
        self.stop_var = ctk.BooleanVar(value=True)

        self._setup_style()
        self._build()
        data = self._load_profiles()
        self._choose_initial_active(data)
        self._refresh_cards()
        self._refresh_header()
        self._sync_buttons()
        if self.profiles:
            self._set_status("저장된 연결에 곧 접속합니다.")
        else:
            self._set_status("저장된 연결이 없습니다. 연결 관리에서 데이터베이스를 추가하세요.")
        if data.warning:
            warning = data.warning
            self.after(300, lambda: messagebox.showwarning("연결 설정", warning))
        self.after(200, self._autoconnect_all)
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()
        self._center()

    def _setup_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Result.Treeview",
            background=EDITOR,
            fieldbackground=EDITOR,
            foreground=TEXT,
            borderwidth=0,
            rowheight=26,
            font=("Consolas", 11),
        )
        style.configure(
            "Result.Treeview.Heading",
            background="#1e293b",
            foreground="#cbd5e1",
            relief="flat",
            font=("Malgun Gothic", 11, "bold"),
        )
        style.map(
            "Result.Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=LINE,
            troughcolor=BG,
            borderwidth=0,
            arrowcolor=TEXT,
        )
        style.configure(
            "Horizontal.TScrollbar",
            background=LINE,
            troughcolor=BG,
            borderwidth=0,
            arrowcolor=TEXT,
        )

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_menu()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 8))
        body.grid_columnconfigure(0, minsize=300, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12, width=300)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(left, text="DB 연결", font=self.font_bold, text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 0)
        )
        ctk.CTkLabel(
            left,
            text="저장된 연결은 프로그램을 열 때마다 자동으로 접속합니다.",
            font=self.font_small,
            text_color=MUTED,
            wraplength=270,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 8))
        buttons = ctk.CTkFrame(left, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        ctk.CTkButton(
            buttons,
            text="연결 관리",
            width=120,
            font=self.font_ui,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.open_manager,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            buttons,
            text="모두 연결",
            width=110,
            font=self.font_ui,
            fg_color=SECONDARY,
            hover_color=SECONDARY_HOVER,
            command=self._autoconnect_all,
        ).pack(side="left")
        self.list = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.list.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.count_label = ctk.CTkLabel(
            left, text="", font=self.font_small, text_color=MUTED, anchor="w"
        )
        self.count_label.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))

        right = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)
        self._build_right(right)

        status_bar = ctk.CTkFrame(self, fg_color=STATUS_BG, corner_radius=0, height=32)
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(
            status_bar, text="준비", font=self.font_small, text_color=MUTED, anchor="w"
        )
        self.status.grid(row=0, column=0, sticky="ew", padx=14, pady=4)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="SQL 열기", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="저장", command=self.save_file, accelerator="Ctrl+S")
        file_menu.add_command(label="다른 이름으로 저장", command=lambda: self.save_file(save_as=True))
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self._on_close)
        menubar.add_cascade(label="파일", menu=file_menu)

        connection_menu = tk.Menu(menubar, tearoff=0)
        connection_menu.add_command(label="연결 관리", command=self.open_manager)
        connection_menu.add_command(label="모두 연결", command=self._autoconnect_all)
        connection_menu.add_command(label="선택 연결 다시 연결", command=self.reconnect_active)
        menubar.add_cascade(label="DB 연결", menu=connection_menu)

        run_menu = tk.Menu(menubar, tearoff=0)
        run_menu.add_command(label="문장 검사", command=self.inspect_script)
        run_menu.add_command(label="실행", command=self.execute, accelerator="Ctrl+Enter")
        run_menu.add_command(label="중지", command=self.stop)
        run_menu.add_separator()
        run_menu.add_command(label="예제 스크립트 넣기", command=self.insert_sample)
        menubar.add_cascade(label="실행", menu=run_menu)
        self.configure(menu=menubar)

    def _build_right(self, right: ctk.CTkFrame) -> None:
        header = ctk.CTkFrame(right, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        header.grid_columnconfigure(0, weight=1)
        self.header_name = ctk.CTkLabel(
            header, text="선택된 연결 없음", font=self.font_bold, text_color=TEXT, anchor="w"
        )
        self.header_name.grid(row=0, column=0, sticky="w")
        self.reconnect_button = ctk.CTkButton(
            header,
            text="이 연결 다시 연결",
            width=140,
            font=self.font_ui,
            fg_color=SECONDARY,
            hover_color=SECONDARY_HOVER,
            command=self.reconnect_active,
        )
        self.reconnect_button.grid(row=0, column=1, sticky="e")
        self.header_detail = ctk.CTkLabel(
            header,
            text="",
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.header_detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(10, 0))
        button_specs = (
            ("열기", self.open_file, "open_button", SECONDARY, SECONDARY_HOVER, 70),
            ("저장", self.save_file, "save_button", SECONDARY, SECONDARY_HOVER, 70),
            ("예제", self.insert_sample, "sample_button", SECONDARY, SECONDARY_HOVER, 70),
            ("검사", self.inspect_script, "inspect_button", SECONDARY, SECONDARY_HOVER, 70),
            ("실행", self.execute, "execute_button", ACCENT, ACCENT_HOVER, 80),
            ("중지", self.stop, "stop_button", DANGER, DANGER_HOVER, 70),
            ("커밋", self.commit_active, "commit_button", SECONDARY, SECONDARY_HOVER, 70),
            ("롤백", self.rollback_active, "rollback_button", SECONDARY, SECONDARY_HOVER, 70),
        )
        for text, command, attr, color, hover, width in button_specs:
            button = ctk.CTkButton(
                bar,
                text=text,
                width=width,
                font=self.font_ui,
                fg_color=color,
                hover_color=hover,
                command=command,
            )
            button.pack(side="left", padx=(0, 6))
            setattr(self, attr, button)
        self.file_label = ctk.CTkLabel(bar, text="새 스크립트", font=self.font_small, text_color=MUTED)
        self.file_label.pack(side="right")

        options = ctk.CTkFrame(right, fg_color="transparent")
        options.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 0))
        options.grid_columnconfigure(0, weight=1)
        checks = ctk.CTkFrame(options, fg_color="transparent")
        checks.grid(row=0, column=0, sticky="w")
        ctk.CTkCheckBox(
            checks,
            text="성공 시 커밋",
            variable=self.commit_var,
            font=self.font_small,
        ).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(
            checks,
            text="오류 시 중단",
            variable=self.stop_var,
            font=self.font_small,
        ).pack(side="left")
        ctk.CTkLabel(
            options,
            text=(
                "테이블·인덱스·데이터 변경은 세미콜론(;)으로 구분합니다. "
                "프로시저, 함수, 패키지, 트리거, 익명 블록은 끝에 슬래시(/) 한 줄을 넣습니다. "
                "Ctrl+Enter 로 실행합니다."
            ),
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=860,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        work = ctk.CTkFrame(right, fg_color="transparent")
        work.grid(row=3, column=0, sticky="nsew", padx=14, pady=(8, 12))
        work.grid_columnconfigure(0, weight=1)
        work.grid_rowconfigure(0, weight=3)
        work.grid_rowconfigure(1, weight=2)

        self.editor = ctk.CTkTextbox(
            work,
            font=self.font_mono,
            fg_color=EDITOR,
            text_color=TEXT,
            wrap="none",
            height=240,
            border_width=1,
            border_color=LINE,
        )
        self.editor.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        editor_inner = _text_widget(self.editor)
        editor_inner.configure(insertbackground=TEXT, undo=True, maxundo=-1)
        editor_inner.bind("<<Modified>>", self._on_editor_modified)
        editor_inner.edit_modified(False)

        self.tabs = ctk.CTkTabview(work, fg_color=PANEL)
        self.tabs.grid(row=1, column=0, sticky="nsew")
        log_tab = self.tabs.add("실행 로그")
        result_tab = self.tabs.add("조회 결과")
        log_tab.grid_rowconfigure(0, weight=1)
        log_tab.grid_columnconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(
            log_tab,
            font=self.font_mono,
            fg_color=EDITOR,
            text_color=TEXT,
            wrap="word",
            height=160,
            border_width=0,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        log_inner = _text_widget(self.log)
        for name, color in (
            ("info", TEXT),
            ("ok", OK),
            ("err", "#f87171"),
            ("warn", WARN),
            ("sql", "#93c5fd"),
            ("muted", MUTED),
        ):
            log_inner.tag_config(name, foreground=color)
        log_inner.bind("<Key>", self._on_log_key)
        log_inner.bind("<<Paste>>", lambda _event: "break")

        result_tab.grid_columnconfigure(0, weight=1)
        result_tab.grid_rowconfigure(1, weight=1)
        result_bar = ctk.CTkFrame(result_tab, fg_color="transparent")
        result_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.result_picker = ctk.CTkOptionMenu(
            result_bar,
            values=["조회 결과 없음"],
            command=self._on_pick_result,
            width=280,
            font=self.font_small,
        )
        self.result_picker.pack(side="left")
        self.result_note = ctk.CTkLabel(
            result_bar, text="", font=self.font_small, text_color=MUTED
        )
        self.result_note.pack(side="left", padx=10)

        holder = tk.Frame(result_tab, bg=EDITOR, highlightthickness=0)
        holder.grid(row=1, column=0, sticky="nsew")
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(holder, style="Result.Treeview", show="tree headings", selectmode="browse")
        y_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(holder, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("odd", background=EDITOR)
        self.tree.tag_configure("even", background="#111827")
        self.tree["columns"] = ()
        self.tree.heading("#0", text="#")
        self.tree.column("#0", width=56, stretch=False, anchor="e")

    def _bind_shortcuts(self) -> None:
        for sequence, handler in (
            ("<Control-Return>", self._shortcut_execute),
            ("<Control-o>", self._shortcut_open),
            ("<Control-O>", self._shortcut_open),
            ("<Control-s>", self._shortcut_save),
            ("<Control-S>", self._shortcut_save),
        ):
            self.bind(sequence, handler)
            self.editor.bind(sequence, handler)

    def _shortcut_execute(self, _event=None):
        self.execute()
        return "break"

    def _shortcut_open(self, _event=None):
        self.open_file()
        return "break"

    def _shortcut_save(self, _event=None):
        self.save_file()
        return "break"

    def _center(self) -> None:
        self.update_idletasks()
        width, height = 1280, 820
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _on_editor_modified(self, _event=None) -> None:
        inner = _text_widget(self.editor)
        if inner.edit_modified():
            self.editor_dirty = True
            inner.edit_modified(False)

    def _on_log_key(self, event):
        navigation = {
            "Left",
            "Right",
            "Up",
            "Down",
            "Next",
            "Prior",
            "Home",
            "End",
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
        }
        if event.keysym in navigation:
            return None
        if (event.state & 0x4) and event.keysym.lower() in {"c", "a"}:
            return None
        return "break"

    def _load_profiles(self) -> StoreData:
        data = self.store.load()
        self.profiles = data.connections
        return data

    def _choose_initial_active(self, data: StoreData) -> None:
        if data.last_active_id and self._profile(data.last_active_id) is not None:
            self.active_id = data.last_active_id
        elif self.profiles:
            self.active_id = self.profiles[0].id
        else:
            self.active_id = None

    def _profile(self, profile_id: str | None) -> ConnectionProfile | None:
        if profile_id is None:
            return None
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _connection_status(self, profile_id: str) -> tuple[str, str]:
        if profile_id in self.pending:
            return "connecting", ""
        session = self.sessions.get(profile_id)
        if session and session.connected:
            return "connected", ""
        if session and session.error:
            return "failed", session.error
        return "disconnected", ""

    def _refresh_cards(self) -> None:
        if not self.winfo_exists():
            return
        for child in self.list.winfo_children():
            child.destroy()
        if not self.profiles:
            ctk.CTkLabel(
                self.list,
                text="저장된 연결이 없습니다.\n연결 관리에서 추가하세요.",
                font=self.font_small,
                text_color=MUTED,
                justify="left",
            ).pack(anchor="w", padx=8, pady=8)
        for profile in self.profiles:
            self._build_card(profile)
        self._update_count_label()

    def _build_card(self, profile: ConnectionProfile) -> None:
        selected = profile.id == self.active_id
        status, message = self._connection_status(profile.id)
        color, status_text = _STATUS_STYLE[status]
        card = ctk.CTkFrame(
            self.list,
            fg_color=CARD_ON if selected else CARD,
            border_width=2 if selected else 1,
            border_color=ACCENT if selected else LINE,
            corner_radius=10,
        )
        card.pack(fill="x", padx=4, pady=4)
        card.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            card, text=profile.name, font=self.font_ui, text_color=TEXT, anchor="w"
        )
        name.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))
        state = ctk.CTkLabel(
            card, text=f"● {status_text}", font=self.font_small, text_color=color, anchor="e"
        )
        state.grid(row=0, column=1, sticky="e", padx=10, pady=(8, 0))
        detail = ctk.CTkLabel(
            card,
            text=profile.target_text(),
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
        )
        detail.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8 if not message else 0))
        if message:
            short = " ".join(message.split())
            if len(short) > 120:
                short = short[:117] + "..."
            note = ctk.CTkLabel(
                card,
                text=short,
                font=self.font_small,
                text_color="#f87171",
                anchor="w",
                justify="left",
                wraplength=250,
            )
            note.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 8))

        def select(profile_id: str = profile.id) -> None:
            self._select(profile_id)

        _bind_click(card, select)

    def _update_count_label(self) -> None:
        ok = fail = pending = 0
        for profile in self.profiles:
            status, _message = self._connection_status(profile.id)
            if status == "connecting":
                pending += 1
            elif status == "connected":
                ok += 1
            elif status == "failed":
                fail += 1
        if not self.profiles:
            self.count_label.configure(text="")
            return
        self.count_label.configure(text=f"연결됨 {ok}  ·  실패 {fail}  ·  연결 중 {pending}")

    def _refresh_header(self) -> None:
        profile = self._profile(self.active_id)
        if profile is None:
            self.header_name.configure(text="선택된 연결 없음")
            self.header_detail.configure(text="왼쪽에서 연결을 선택하거나 연결 관리에서 추가하세요.")
            return
        self.header_name.configure(text=profile.name)
        session = self.sessions.get(profile.id)
        if profile.id in self.pending:
            detail = "연결하는 중..."
        elif session and session.connected and session.info is not None:
            detail = session.info.summary()
        elif session and session.error:
            detail = session.error
        else:
            detail = profile.target_text()
        self.header_detail.configure(text=detail)

    def _select(self, profile_id: str) -> None:
        self.active_id = profile_id
        try:
            self.store.update_last_active(profile_id)
        except Exception:
            pass
        self._refresh_cards()
        self._refresh_header()

    def _sync_buttons(self) -> None:
        busy = self.running or self._tx_busy
        self.execute_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if self.running else "disabled")
        state = "disabled" if busy else "normal"
        self.commit_button.configure(state=state)
        self.rollback_button.configure(state=state)

    def open_manager(self) -> None:
        if self._manager is not None and self._manager.winfo_exists():
            self._manager.focus()
            return
        self._manager = ConnectionManager(
            self,
            self.store,
            self._on_store_changed,
            self._is_busy,
            select_id=self.active_id,
        )

    def _is_busy(self, profile_id: str) -> bool:
        return self.running and profile_id == self._run_session_id

    def _on_store_changed(self) -> None:
        previous = {profile.id: connection_fingerprint(profile) for profile in self.profiles}
        active = self.active_id
        data = self._load_profiles()
        if active and self._profile(active) is not None:
            self.active_id = active
        elif data.last_active_id and self._profile(data.last_active_id) is not None:
            self.active_id = data.last_active_id
        elif self.profiles:
            self.active_id = self.profiles[0].id
        else:
            self.active_id = None
        current = {profile.id: connection_fingerprint(profile) for profile in self.profiles}
        removed = set(previous) - set(current)
        added = set(current) - set(previous)
        updated = {
            profile_id
            for profile_id in set(previous) & set(current)
            if previous[profile_id] != current[profile_id]
        }
        for profile_id in removed:
            self.pending.discard(profile_id)
            self.connect_gen[profile_id] = self.connect_gen.get(profile_id, 0) + 1
            session = self.sessions.pop(profile_id, None)
            if session is not None and not (self.running and profile_id == self._run_session_id):
                session.close()
        self._refresh_cards()
        self._refresh_header()
        for profile_id in added | updated:
            if self.running and profile_id == self._run_session_id:
                self._deferred_connect.add(profile_id)
                continue
            self._start_connect(profile_id)

    def _autoconnect_all(self) -> None:
        if not self.winfo_exists():
            return
        if not self.profiles:
            self._set_status("저장된 연결이 없습니다. 연결 관리에서 데이터베이스를 추가하세요.")
            self._refresh_cards()
            return
        self._set_status("저장된 연결에 접속하는 중...")
        for profile in self.profiles:
            self._start_connect(profile.id)

    def reconnect_active(self) -> None:
        if self.active_id is None:
            messagebox.showinfo("연결", "다시 연결할 항목을 선택하세요.")
            return
        if self._is_busy(self.active_id):
            messagebox.showwarning("실행 중", "스크립트 실행이 끝난 뒤에 다시 연결하세요.")
            return
        self._start_connect(self.active_id)

    def _start_connect(self, profile_id: str) -> None:
        profile = self._profile(profile_id)
        if profile is None:
            return
        self.connect_gen[profile_id] = self.connect_gen.get(profile_id, 0) + 1
        generation = self.connect_gen[profile_id]
        self.pending.add(profile_id)
        self._refresh_cards()
        self._refresh_header()
        snapshot = profile.snapshot()
        threading.Thread(
            target=self._connect_worker,
            args=(snapshot, generation),
            daemon=True,
            name=f"connect-{profile_id[:8]}",
        ).start()

    def _connect_worker(self, profile: ConnectionProfile, generation: int) -> None:
        session = OracleSession(profile)
        if profile.password_error or not profile.password:
            session.error = (
                "저장된 비밀번호를 열 수 없습니다. 연결 관리에서 비밀번호를 다시 저장하세요."
            )
        else:
            try:
                session.connect()
            except Exception as exc:
                session.error = format_db_error(exc)
                session.close()
        self.events.put(("session", profile.id, generation, session))

    def _apply_session(self, profile_id: str, generation: int, session: OracleSession) -> None:
        if generation != self.connect_gen.get(profile_id):
            session.close()
            return
        self.pending.discard(profile_id)
        if self.running and profile_id == self._run_session_id:
            session.close()
            self._refresh_cards()
            self._refresh_header()
            return
        if self._profile(profile_id) is None:
            session.close()
            self.sessions.pop(profile_id, None)
        else:
            old = self.sessions.get(profile_id)
            if old is not None and old is not session:
                old.close()
            self.sessions[profile_id] = session
        self._refresh_cards()
        self._refresh_header()
        if not self.running:
            self._update_count_status()

    def _update_count_status(self) -> None:
        if self.running or not self.winfo_exists():
            return
        if not self.profiles:
            self._set_status("저장된 연결이 없습니다. 연결 관리에서 데이터베이스를 추가하세요.")
            return
        ok = fail = pending = 0
        for profile in self.profiles:
            status, _message = self._connection_status(profile.id)
            if status == "connecting":
                pending += 1
            elif status == "connected":
                ok += 1
            elif status == "failed":
                fail += 1
        self._set_status(f"연결됨 {ok}  ·  실패 {fail}  ·  연결 중 {pending}")

    def _poll_queue(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._poll_queue)
        except tk.TclError:
            return

    def _handle_event(self, event) -> None:
        kind = event[0]
        if kind == "log":
            _tag_name, tag, message = event
            self._append_log(message, tag)
        elif kind == "result":
            _kind, title, columns, rows, truncated = event
            self._add_result(title, columns, rows, truncated)
        elif kind == "progress":
            _kind, index, total = event
            self._set_status(f"실행 중 ({index}/{total})")
        elif kind == "session":
            _kind, profile_id, generation, session = event
            self._apply_session(profile_id, generation, session)
        elif kind == "finished":
            _kind, stats = event
            self._on_finished(stats)
        elif kind == "tx-done":
            self._tx_busy = False
            self._sync_buttons()

    def _on_finished(self, stats: dict) -> None:
        self.running = False
        self._sync_buttons()
        elapsed = format_elapsed(float(stats.get("elapsed_ms") or 0))
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        skipped = stats.get("skip", 0)
        if stats.get("cancelled"):
            text = f"중지되었습니다. 성공 {ok}건, {elapsed}."
            tag = "warn"
        elif fail:
            text = f"실패가 있습니다. 성공 {ok}건, 실패 {fail}건, {elapsed}."
            tag = "warn"
        else:
            text = f"완료되었습니다. 성공 {ok}건, 건너뜀 {skipped}건, {elapsed}."
            tag = "ok"
        self._append_log(text, tag)
        self._set_status(text)
        deferred = list(self._deferred_connect)
        self._deferred_connect.clear()
        run_id = self._run_session_id
        if stats.get("reconnect") and run_id and run_id not in deferred:
            deferred.append(run_id)
        self._run_session_id = None
        for profile_id in deferred:
            self._start_connect(profile_id)
        self._refresh_header()
        self._refresh_cards()

    def inspect_script(self) -> None:
        script = self.editor.get("1.0", "end-1c")
        statements = split_oracle_script(script)
        self._clear_log()
        self.tabs.set("실행 로그")
        if not statements:
            self._append_log("실행할 문장이 없습니다.", "warn")
            return
        self._append_log("데이터베이스는 변경하지 않고 문장만 구분했습니다.", "muted")
        self._append_log(summarize_statements(statements), "info")
        for index, statement in enumerate(statements, 1):
            if statement.mode == "skip":
                self._append_log(
                    f"[{index}] 건너뜀 ({statement.note}) {statement.sql}",
                    "warn",
                )
                continue
            label = statement_label(statement.sql, statement.mode)
            self._append_log(f"[{index}] {label}", "ok")
            self._append_log("    " + preview_sql(statement.sql), "sql")
        risks = find_risks(statements)
        if risks:
            self._append_log("주의가 필요한 문장:", "warn")
            for risk in risks:
                self._append_log(risk, "warn")

    def execute(self) -> None:
        if self.running or self._tx_busy:
            return
        script = self.editor.get("1.0", "end-1c")
        statements = split_oracle_script(script)
        runnable = [item for item in statements if item.mode != "skip"]
        if not runnable:
            messagebox.showinfo("실행", "실행할 SQL 문장이 없습니다.")
            return
        profile = self._profile(self.active_id)
        session = self.sessions.get(self.active_id or "")
        if profile is None or session is None or not session.connected:
            messagebox.showwarning(
                "연결 없음",
                "연결된 데이터베이스를 선택한 뒤 실행하세요.\n연결이 없으면 연결 관리에서 추가하거나 모두 연결을 누르세요.",
            )
            return
        risks = find_risks(statements)
        if risks:
            shown = "\n".join(risks[:10])
            extra = f"\n... 외 {len(risks) - 10}건" if len(risks) > 10 else ""
            if not messagebox.askyesno(
                "실행 확인",
                "삭제이거나 조건 없는 변경이 있습니다.\n실행할까요?\n\n" + shown + extra,
            ):
                return
        commit = bool(self.commit_var.get())
        stop_on_error = bool(self.stop_var.get())
        self.running = True
        self._run_session_id = profile.id
        session = self.sessions.get(profile.id)
        if session is None or not session.connected:
            self.running = False
            self._run_session_id = None
            messagebox.showwarning(
                "연결 없음",
                "연결된 데이터베이스를 선택한 뒤 실행하세요.\n연결이 없으면 연결 관리에서 추가하거나 모두 연결을 누르세요.",
            )
            return
        self.cancel_event.clear()
        self._sync_buttons()
        self._clear_log()
        self._clear_results()
        self.tabs.set("실행 로그")
        self._append_log(
            f"{profile.name}에서 실행합니다. {summarize_statements(statements)}",
            "info",
        )
        self._set_status("실행 중...")

        def worker() -> None:
            sent = {"done": False}

            def report(*event) -> None:
                if event and event[0] == "finished":
                    sent["done"] = True
                self.events.put(event)

            try:
                run_script(
                    session,
                    statements,
                    commit=commit,
                    stop_on_error=stop_on_error,
                    cancel_event=self.cancel_event,
                    report=report,
                )
            except Exception as exc:
                self.events.put(("log", "err", "실행 오류: " + format_db_error(exc)))
            finally:
                if not sent["done"]:
                    self.events.put(
                        (
                            "finished",
                            {
                                "ok": 0,
                                "fail": 1,
                                "skip": 0,
                                "cancelled": False,
                                "reconnect": False,
                                "elapsed_ms": 0,
                            },
                        )
                    )

        threading.Thread(target=worker, daemon=True, name="run-script").start()

    def stop(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        session = self.sessions.get(self._run_session_id or "")
        if session is not None:
            request_cancel(session)
        self._set_status("중지 요청됨...")

    def commit_active(self) -> None:
        if not messagebox.askyesno(
            "커밋",
            "현재 연결의 데이터 변경을 확정할까요?\nDDL은 이미 반영되어 있고, 데이터 변경만 확정됩니다.",
        ):
            return
        self._session_call("커밋", lambda conn: conn.commit())

    def rollback_active(self) -> None:
        if not messagebox.askyesno("롤백", "아직 커밋하지 않은 데이터 변경을 취소할까요?"):
            return
        self._session_call("롤백", lambda conn: conn.rollback())

    def _session_call(self, verb: str, action) -> None:
        if self.running or self._tx_busy:
            return
        session = self.sessions.get(self.active_id or "")
        if session is None or not session.connected:
            messagebox.showwarning("연결 없음", "연결된 데이터베이스를 선택한 뒤 다시 시도하세요.")
            return
        self._tx_busy = True
        self._sync_buttons()

        def work() -> None:
            try:
                with session.lock:
                    if session.conn is None:
                        raise RuntimeError("연결이 없습니다.")
                    action(session.conn)
                self.events.put(("log", "ok", verb + "했습니다."))
            except Exception as exc:
                self.events.put(("log", "err", verb + "하지 못했습니다. " + format_db_error(exc)))
            finally:
                self.events.put(("tx-done",))

        threading.Thread(target=work, daemon=True).start()

    def open_file(self) -> None:
        if self.editor_dirty and self.editor.get("1.0", "end-1c").strip():
            if not messagebox.askyesno("열기", "편집 중인 스크립트를 바꿀까요?"):
                return
        selected = filedialog.askopenfilename(
            title="SQL 파일 열기",
            filetypes=[("SQL 파일", "*.sql"), ("모든 파일", "*.*")],
        )
        if not selected:
            return
        path = Path(selected)
        try:
            text = read_text_file(path)
        except OSError as exc:
            messagebox.showerror("열기 실패", str(exc))
            return
        self._set_editor(text)
        self.current_file = path
        self.file_label.configure(text=path.name)

    def save_file(self, save_as: bool = False) -> None:
        path = None if save_as else self.current_file
        if path is None:
            selected = filedialog.asksaveasfilename(
                title="SQL 파일 저장",
                defaultextension=".sql",
                filetypes=[("SQL 파일", "*.sql"), ("모든 파일", "*.*")],
            )
            if not selected:
                return
            path = Path(selected)
        try:
            path.write_text(self.editor.get("1.0", "end-1c"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("저장 실패", str(exc))
            return
        self.current_file = path
        self.editor_dirty = False
        _text_widget(self.editor).edit_modified(False)
        self.file_label.configure(text=path.name)
        self._set_status(f"저장했습니다. {path.name}")

    def insert_sample(self) -> None:
        if self.editor.get("1.0", "end-1c").strip():
            if not messagebox.askyesno("예제", "편집 중인 스크립트를 예제로 바꿀까요?"):
                return
        self._set_editor(SAMPLE_SCRIPT)
        self.current_file = None
        self.file_label.configure(text="예제 스크립트")

    def _set_editor(self, text: str) -> None:
        self.editor.delete("1.0", "end")
        if text:
            self.editor.insert("1.0", text)
        inner = _text_widget(self.editor)
        inner.edit_modified(False)
        self.editor_dirty = False

    def _append_log(self, message: str, tag: str) -> None:
        if tag == "sql":
            self._append_raw(message + "\n", "sql")
            return
        stamp = time.strftime("%H:%M:%S")
        self._append_raw(f"[{stamp}] {message}\n", tag)

    def _append_raw(self, text: str, tag: str) -> None:
        inner = _text_widget(self.log)
        start = inner.index("end-1c")
        inner.insert("end", text)
        end = inner.index("end-1c")
        if tag:
            inner.tag_add(tag, start, end)
        line_count = int(float(inner.index("end-1c")))
        if line_count > 8000:
            inner.delete("1.0", "2000.0")
        inner.see("end")

    def _clear_log(self) -> None:
        inner = _text_widget(self.log)
        inner.delete("1.0", "end")

    def _clear_results(self) -> None:
        self.result_sets.clear()
        self._result_labels = {}
        self.result_picker.configure(values=["조회 결과 없음"])
        self.result_picker.set("조회 결과 없음")
        self.result_note.configure(text="")
        self._clear_tree()

    def _clear_tree(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.tree["columns"] = ()

    def _add_result(self, title: str, columns: list[str], rows: list[list[str]], truncated: bool) -> None:
        self.result_sets.append((title, columns, rows, truncated))
        labels = [item[0] for item in self.result_sets]
        self._result_labels = {label: index for index, label in enumerate(labels)}
        self.result_picker.configure(values=labels)
        self.result_picker.set(title)
        self._render_result(len(self.result_sets) - 1)
        self.tabs.set("조회 결과")

    def _on_pick_result(self, label: str) -> None:
        index = self._result_labels.get(label)
        if index is None:
            return
        self._render_result(index)

    def _render_result(self, index: int) -> None:
        _title, columns, rows, truncated = self.result_sets[index]
        self._clear_tree()
        column_ids = [f"c{number}" for number in range(len(columns))]
        self.tree["columns"] = column_ids
        self.tree.heading("#0", text="#")
        self.tree.column("#0", width=56, stretch=False, anchor="e")
        for column_id, name in zip(column_ids, columns):
            self.tree.heading(column_id, text=name)
            width = min(260, max(90, len(name) * 14 + 28))
            self.tree.column(column_id, width=width, stretch=True, anchor="w")
        for row_number, row in enumerate(rows, 1):
            tag = "even" if row_number % 2 == 0 else "odd"
            self.tree.insert("", "end", text=str(row_number), values=row, tags=(tag,))
        note = f"{len(rows)}행"
        if truncated:
            note += " (최대 1000행까지만 표시)"
        self.result_note.configure(text=note)

    def _on_close(self) -> None:
        if self._manager is not None and self._manager.winfo_exists():
            if not self._manager.close():
                return
        if self.running:
            if not messagebox.askyesno("종료", "스크립트가 실행 중입니다. 중지하고 종료할까요?"):
                return
            self.cancel_event.set()
            session = self.sessions.get(self._run_session_id or "")
            if session is not None:
                request_cancel(session)
        self._closed = True
        for session in list(self.sessions.values()):
            session.close()
        self.destroy()


def _bind_click(widget, callback) -> None:
    widget.bind("<Button-1>", lambda _event: callback())
    for child in widget.winfo_children():
        _bind_click(child, callback)
