"""Add, update, test, and delete saved Oracle connections."""

from __future__ import annotations

import re
import threading
import tkinter as tk
import uuid
from collections.abc import Callable
from tkinter import messagebox

import customtkinter as ctk

from app.connection_store import ConnectionStore
from app.models import ConnectionProfile
from app.oracle_runner import format_db_error, test_connection
from app.theme import (
    ACCENT,
    ACCENT_HOVER,
    BG,
    DANGER,
    DANGER_HOVER,
    EDITOR,
    ERR,
    MUTED,
    OK,
    PANEL,
    SECONDARY,
    SECONDARY_HOVER,
    TEXT,
    WARN,
)

_SCHEMA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")


class ConnectionManager(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        store: ConnectionStore,
        on_changed: Callable[[], None],
        is_busy: Callable[[str], bool],
        select_id: str | None = None,
    ) -> None:
        super().__init__(master, fg_color=BG)
        self.store = store
        self.on_changed = on_changed
        self.is_busy = is_busy
        self.profiles: list[ConnectionProfile] = []
        self._editing_id: str | None = None
        self._form_index: int | None = None
        self._loaded_password = ""
        self._password_error = False
        self._filling = True
        self._loading_list = False
        self.dirty = False

        self.title("연결 관리")
        self.geometry("980x700")
        self.minsize(860, 620)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())

        self.font_ui = ctk.CTkFont(family="Malgun Gothic", size=13)
        self.font_bold = ctk.CTkFont(family="Malgun Gothic", size=16, weight="bold")
        self.font_small = ctk.CTkFont(family="Malgun Gothic", size=12)

        self.name_var = ctk.StringVar()
        self.host_var = ctk.StringVar()
        self.port_var = ctk.StringVar(value="1521")
        self.id_type = ctk.StringVar(value="service_name")
        self.identifier_var = ctk.StringVar()
        self.username_var = ctk.StringVar()
        self.password_var = ctk.StringVar()
        self.schema_var = ctk.StringVar()
        self.dsn_var = ctk.StringVar()
        self.show_password_var = ctk.BooleanVar(value=False)
        for variable in (
            self.name_var,
            self.host_var,
            self.port_var,
            self.id_type,
            self.identifier_var,
            self.username_var,
            self.password_var,
            self.schema_var,
            self.dsn_var,
        ):
            variable.trace_add("write", self._mark_dirty)

        self._build()
        self._reload_list(select_id)
        self.after(50, self._focus)

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="연결 관리", font=self.font_bold, text_color=TEXT).pack(side="left")
        ctk.CTkLabel(
            header,
            text="저장한 연결은 프로그램을 열 때마다 자동으로 접속합니다.",
            font=self.font_small,
            text_color=MUTED,
        ).pack(side="left", padx=(12, 0))

        left = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        left.grid(row=1, column=0, sticky="nsew", padx=(16, 8), pady=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(left, text="저장된 연결", font=self.font_ui, text_color=TEXT).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 6)
        )
        list_holder = tk.Frame(left, bg=EDITOR, highlightthickness=0)
        list_holder.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        list_holder.grid_rowconfigure(0, weight=1)
        list_holder.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            list_holder,
            bg=EDITOR,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            activestyle="none",
            relief="flat",
            font=("Malgun Gothic", 11),
            highlightthickness=0,
            bd=0,
            exportselection=False,
        )
        scroll = tk.Scrollbar(list_holder, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        right = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 16), pady=(0, 8))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        form = ctk.CTkScrollableFrame(right, fg_color="transparent")
        form.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))
        form.grid_columnconfigure(1, weight=1)

        self._add_field(form, 0, "연결 이름", "name_entry", self.name_var, "예: 개발 DB")
        self._add_field(form, 1, "호스트", "host_entry", self.host_var, "예: 10.0.0.10")
        self._add_field(form, 2, "포트", "port_entry", self.port_var, "1521")

        ctk.CTkLabel(form, text="구분", font=self.font_ui, text_color=MUTED).grid(
            row=3, column=0, sticky="w", padx=8, pady=6
        )
        radios = ctk.CTkFrame(form, fg_color="transparent")
        radios.grid(row=3, column=1, sticky="w", padx=8, pady=6)
        ctk.CTkRadioButton(
            radios,
            text="서비스명",
            variable=self.id_type,
            value="service_name",
            font=self.font_ui,
        ).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(
            radios, text="SID", variable=self.id_type, value="sid", font=self.font_ui
        ).pack(side="left")

        self._add_field(form, 4, "서비스명 / SID", "identifier_entry", self.identifier_var, "예: ORCL")
        self._add_field(form, 5, "사용자", "username_entry", self.username_var, "예: SCOTT")

        ctk.CTkLabel(form, text="비밀번호", font=self.font_ui, text_color=MUTED).grid(
            row=6, column=0, sticky="w", padx=8, pady=6
        )
        password_row = ctk.CTkFrame(form, fg_color="transparent")
        password_row.grid(row=6, column=1, sticky="ew", padx=8, pady=6)
        password_row.grid_columnconfigure(0, weight=1)
        self.password_entry = ctk.CTkEntry(
            password_row,
            textvariable=self.password_var,
            show="*",
            font=self.font_ui,
            placeholder_text="비밀번호",
        )
        self.password_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkCheckBox(
            password_row,
            text="표시",
            variable=self.show_password_var,
            command=self._toggle_password,
            font=self.font_small,
            width=60,
        ).grid(row=0, column=1, padx=(8, 0))

        self._add_field(form, 7, "현재 스키마", "schema_entry", self.schema_var, "선택 사항")
        self._add_field(
            form,
            8,
            "직접 입력 DSN",
            "dsn_entry",
            self.dsn_var,
            "선택 사항. 입력하면 호스트 대신 사용",
        )
        ctk.CTkLabel(
            form,
            text="DSN을 입력하면 호스트, 포트, 서비스명 대신 그 값으로 접속합니다.",
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
        ).grid(row=9, column=1, sticky="w", padx=8, pady=(0, 8))

        self.test_result = ctk.CTkLabel(
            right,
            text="",
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.test_result.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 0))
        self.form_status = ctk.CTkLabel(
            right,
            text="",
            font=self.font_small,
            text_color=OK,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.form_status.grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 4))

        actions = ctk.CTkFrame(right, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        ctk.CTkButton(
            actions,
            text="새 연결",
            width=90,
            font=self.font_ui,
            fg_color=SECONDARY,
            hover_color=SECONDARY_HOVER,
            command=self.new_profile,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="저장",
            width=80,
            font=self.font_ui,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.save_profile,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="삭제",
            width=80,
            font=self.font_ui,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
            command=self.delete_profile,
        ).pack(side="left", padx=(0, 6))
        self.test_button = ctk.CTkButton(
            actions,
            text="연결 테스트",
            width=110,
            font=self.font_ui,
            fg_color=SECONDARY,
            hover_color=SECONDARY_HOVER,
            command=self.test_profile,
        )
        self.test_button.pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="닫기",
            width=80,
            font=self.font_ui,
            fg_color=SECONDARY,
            hover_color=SECONDARY_HOVER,
            command=self.close,
        ).pack(side="right")

        hint = ctk.CTkLabel(
            self,
            text=(
                "비밀번호는 이 Windows 사용자 계정으로 암호화되어 저장됩니다. "
                "연결을 삭제하면 다음 실행부터 자동으로 접속하지 않습니다."
            ),
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
            wraplength=920,
        )
        hint.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 12))

    def _add_field(self, parent, row: int, label: str, attr: str, variable, placeholder: str) -> None:
        ctk.CTkLabel(parent, text=label, font=self.font_ui, text_color=MUTED).grid(
            row=row, column=0, sticky="w", padx=8, pady=6
        )
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            font=self.font_ui,
            placeholder_text=placeholder,
        )
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        setattr(self, attr, entry)

    def _focus(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def _mark_dirty(self, *_args) -> None:
        if not self._filling:
            self.dirty = True

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def _reload_list(self, select_id: str | None) -> None:
        data = self.store.load()
        self.profiles = data.connections
        self._loading_list = True
        self.listbox.delete(0, "end")
        chosen = 0
        for index, profile in enumerate(self.profiles):
            self.listbox.insert("end", f"{profile.name}    {profile.target_text()}")
            if profile.id == select_id:
                chosen = index
        if self.profiles:
            self.listbox.selection_set(chosen)
            self.listbox.activate(chosen)
            self.listbox.see(chosen)
            self._load_form(self.profiles[chosen])
        else:
            self._load_form(None)
        self._loading_list = False

    def _load_form(self, profile: ConnectionProfile | None) -> None:
        self._filling = True
        try:
            self.show_password_var.set(False)
            self.password_entry.configure(show="*")
            if profile is None:
                self._editing_id = None
                self._form_index = None
                self._loaded_password = ""
                self._password_error = False
                self.name_var.set("")
                self.host_var.set("")
                self.port_var.set("1521")
                self.id_type.set("service_name")
                self.identifier_var.set("")
                self.username_var.set("")
                self.password_var.set("")
                self.schema_var.set("")
                self.dsn_var.set("")
                self.password_entry.configure(placeholder_text="비밀번호")
            else:
                self._editing_id = profile.id
                self._form_index = self._index_of(profile.id)
                self._loaded_password = profile.password
                self._password_error = profile.password_error
                self.name_var.set(profile.name)
                self.host_var.set(profile.host)
                self.port_var.set(str(profile.port))
                self.id_type.set(profile.identifier_type)
                self.identifier_var.set(profile.identifier)
                self.username_var.set(profile.username)
                self.password_var.set("")
                self.schema_var.set(profile.current_schema)
                self.dsn_var.set(profile.dsn)
                self.password_entry.configure(
                    placeholder_text="비워 두면 저장된 비밀번호를 사용합니다."
                )
            self.test_result.configure(text="")
            self.form_status.configure(text="")
        finally:
            self._filling = False
            self.dirty = False

    def _index_of(self, profile_id: str) -> int | None:
        for index, profile in enumerate(self.profiles):
            if profile.id == profile_id:
                return index
        return None

    def _on_list_select(self, _event=None) -> None:
        if self._loading_list:
            return
        selection = self.listbox.curselection()
        if not selection:
            return
        index = selection[0]
        if index == self._form_index:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel("저장하지 않은 변경", "변경 내용을 저장할까요?")
            if answer is None:
                self._restore_selection()
                return
            if answer:
                if not self.save_profile():
                    self._restore_selection()
                    return
                self._select_index(index)
                return
        if 0 <= index < len(self.profiles):
            self._load_form(self.profiles[index])

    def _restore_selection(self) -> None:
        self._loading_list = True
        self.listbox.selection_clear(0, "end")
        if self._form_index is not None:
            self.listbox.selection_set(self._form_index)
            self.listbox.activate(self._form_index)
        self._loading_list = False

    def _select_index(self, index: int) -> None:
        if index < 0 or index >= len(self.profiles):
            return
        self._loading_list = True
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.activate(index)
        self._loading_list = False
        self._load_form(self.profiles[index])

    def new_profile(self) -> None:
        if not self._discard_or_save():
            return
        self._loading_list = True
        self.listbox.selection_clear(0, "end")
        self._loading_list = False
        self._load_form(None)
        self.name_entry.focus()

    def save_profile(self) -> bool:
        profile = self._collect_profile(for_test=False)
        if profile is None:
            return False
        if profile.id and self.is_busy(profile.id):
            messagebox.showwarning(
                "실행 중",
                "이 연결로 스크립트를 실행하는 중입니다. 끝난 뒤에 저장하세요.",
            )
            return False
        try:
            self.store.upsert(profile)
        except Exception as exc:
            messagebox.showerror("저장 실패", str(exc))
            return False
        self.dirty = False
        self._reload_list(profile.id)
        self.on_changed()
        self.form_status.configure(
            text="저장했습니다. 프로그램을 열 때마다 이 연결로 자동 접속합니다.",
            text_color=OK,
        )
        return True

    def delete_profile(self) -> None:
        if self._editing_id is None:
            messagebox.showinfo("삭제", "삭제할 연결을 선택하세요.")
            return
        profile = self._profile_by_id(self._editing_id)
        if profile is None:
            return
        if self.is_busy(profile.id):
            messagebox.showwarning(
                "실행 중",
                "이 연결로 스크립트를 실행하는 중입니다. 끝난 뒤에 삭제하세요.",
            )
            return
        if not messagebox.askyesno(
            "연결 삭제",
            f"'{profile.name}' 연결을 삭제할까요?\n다음 실행부터 자동으로 접속하지 않습니다.",
        ):
            return
        try:
            self.store.delete(profile.id)
        except Exception as exc:
            messagebox.showerror("삭제 실패", str(exc))
            return
        self.dirty = False
        self._reload_list(None)
        self.on_changed()
        self.form_status.configure(text="삭제했습니다.", text_color=WARN)

    def test_profile(self) -> None:
        profile = self._collect_profile(for_test=True)
        if profile is None:
            return
        self.test_button.configure(state="disabled")
        self.test_result.configure(text="연결 테스트 중...", text_color=WARN)

        def work() -> None:
            try:
                info = test_connection(profile)
                text = "연결되었습니다. " + info.summary()
                color = OK
            except Exception as exc:
                text = format_db_error(exc)
                color = ERR
            self.after(0, lambda message=text, tone=color: self._show_test(message, tone))

        threading.Thread(target=work, daemon=True).start()

    def _show_test(self, text: str, color: str) -> None:
        if not self.winfo_exists():
            return
        self.test_button.configure(state="normal")
        self.test_result.configure(text=text, text_color=color)

    def close(self) -> bool:
        if not self._discard_or_save():
            return False
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        return True

    def _discard_or_save(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("저장하지 않은 변경", "변경 내용을 저장할까요?")
        if answer is None:
            return False
        if answer:
            return self.save_profile()
        self.dirty = False
        return True

    def _profile_by_id(self, profile_id: str) -> ConnectionProfile | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def _collect_profile(self, *, for_test: bool) -> ConnectionProfile | None:
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        identifier = self.identifier_var.get().strip()
        username = self.username_var.get().strip()
        schema = self.schema_var.get().strip()
        dsn = self.dsn_var.get().strip()
        port_text = self.port_var.get().strip() or "1521"
        if not name:
            messagebox.showwarning("확인", "연결 이름을 입력하세요.")
            return None
        if not port_text.isdigit():
            messagebox.showwarning("확인", "포트는 숫자여야 합니다.")
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            messagebox.showwarning("확인", "포트는 1부터 65535 사이여야 합니다.")
            return None
        if not dsn:
            if not host:
                messagebox.showwarning("확인", "호스트를 입력하세요. DSN을 쓰는 경우에는 DSN만 입력해도 됩니다.")
                return None
            if not identifier:
                messagebox.showwarning("확인", "서비스명 또는 SID를 입력하세요.")
                return None
        if not username:
            messagebox.showwarning("확인", "사용자 이름을 입력하세요.")
            return None
        if schema and not _SCHEMA_RE.fullmatch(schema):
            messagebox.showwarning(
                "확인",
                "현재 스키마는 영문으로 시작하고 영문, 숫자, _, $, # 만 사용할 수 있습니다.",
            )
            return None
        typed_password = self.password_var.get()
        if typed_password:
            password = typed_password
        elif self._editing_id and self._loaded_password and not self._password_error:
            password = self._loaded_password
        else:
            messagebox.showwarning("확인", "비밀번호를 입력하세요.")
            return None
        profile_id = self._editing_id or str(uuid.uuid4())
        if not for_test:
            folded = name.casefold()
            for profile in self.profiles:
                if profile.id != profile_id and profile.name.casefold() == folded:
                    messagebox.showwarning("확인", "같은 이름의 연결이 이미 있습니다.")
                    return None
        identifier_type = self.id_type.get()
        if identifier_type not in {"service_name", "sid"}:
            identifier_type = "service_name"
        return ConnectionProfile(
            id=profile_id,
            name=name,
            host=host,
            port=port,
            identifier_type=identifier_type,
            identifier=identifier,
            username=username,
            password=password,
            current_schema=schema,
            dsn=dsn,
        )
