from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import ConversionError, convert
from .i18n import (
    DEFAULT_LANGUAGE,
    language_label,
    normalize_language,
    supported_languages,
    translate,
)
from .model import ConversionOptions, ConversionResult


@dataclass(frozen=True)
class Choice:
    value: str
    label_key: str


@dataclass
class LocalizedChoiceControl:
    value: tk.StringVar
    display: tk.StringVar
    widget: ttk.Combobox
    choices: tuple[Choice, ...]


AUTO_CHOICE = Choice("auto", "choice_auto")
GUEST_OS_CHOICES = (
    AUTO_CHOICE,
    Choice("windows", "choice_windows"),
    Choice("linux", "choice_linux"),
)
WINDOWS_BACKEND_CHOICES = (
    AUTO_CHOICE,
    Choice("qemu-img", "choice_qemu_img"),
    Choice("virt-v2v", "choice_virt_v2v"),
    Choice("winpe-dism", "choice_winpe_dism"),
)
LINUX_BACKEND_CHOICES = (
    AUTO_CHOICE,
    Choice("qemu-img", "choice_qemu_img"),
    Choice("virt-v2v", "choice_virt_v2v"),
)
NETWORK_MODEL_CHOICES = (
    AUTO_CHOICE,
    Choice("e1000", "choice_e1000"),
    Choice("virtio-net-pci", "choice_virtio_net_pci"),
)
QGA_INSTALL_MODE_CHOICES = (
    Choice("bake", "choice_bake"),
    Choice("firstboot", "choice_firstboot"),
)
WINPE_ARCH_CHOICES = (
    AUTO_CHOICE,
    Choice("amd64", "choice_amd64"),
    Choice("arm64", "choice_arm64"),
)


class ConverterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.geometry("940x760")
        self.minsize(820, 620)
        self._language = tk.StringVar(value=DEFAULT_LANGUAGE)
        self._language_display = tk.StringVar()
        self._input = tk.StringVar()
        self._output = tk.StringVar()
        self._runtime_dir = tk.StringVar()
        self._driver_iso = tk.StringVar()
        self._driver_old = tk.StringVar()
        self._driver_new = tk.StringVar()
        self._winpe_iso = tk.StringVar()
        self._windows_bake_username = tk.StringVar()
        self._windows_bake_password = tk.StringVar()
        self._winpe_arch = tk.StringVar(value="auto")
        self._qemu_system = tk.StringVar(value="qemu-system-x86_64")
        self._guest_os = tk.StringVar(value="auto")
        self._windows_backend = tk.StringVar(value="auto")
        self._linux_backend = tk.StringVar(value="auto")
        self._network_model = tk.StringVar(value="auto")
        self._require_virtio = tk.BooleanVar(value=False)
        self._require_qga = tk.BooleanVar(value=True)
        self._qga_install_mode = tk.StringVar(value="bake")
        self._status = tk.StringVar()
        self._status_key = "status_ready"
        self._text_widgets: list[tuple[tk.Widget, str]] = []
        self._choice_controls: dict[str, LocalizedChoiceControl] = {}
        self._build()
        self._apply_language()

    def _tr(self, key: str, **values: object) -> str:
        return translate(self._language.get(), key, **values)

    def _register_text(self, widget: tk.Widget, key: str) -> None:
        self._text_widgets.append((widget, key))

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        row = 0
        language_label_widget = ttk.Label(self)
        language_label_widget.grid(
            row=row, column=0, padx=10, pady=(12, 6), sticky="w"
        )
        self._register_text(language_label_widget, "language")
        self._language_combo = ttk.Combobox(
            self,
            textvariable=self._language_display,
            state="readonly",
            width=18,
        )
        self._language_combo.grid(
            row=row, column=1, padx=10, pady=(12, 6), sticky="w"
        )
        self._language_combo.bind(
            "<<ComboboxSelected>>",
            self._on_language_changed,
        )
        row += 1

        rows = (
            ("input_path", self._input, False),
            ("output_path", self._output, False),
            ("runtime_dir", self._runtime_dir, False),
            ("driver_iso", self._driver_iso, False),
            ("driver_old", self._driver_old, False),
            ("driver_new", self._driver_new, False),
            ("winpe_iso", self._winpe_iso, False),
            ("windows_bake_username", self._windows_bake_username, False),
            ("windows_bake_password", self._windows_bake_password, True),
        )
        for index, (label_key, variable, is_password) in enumerate(rows):
            label = ttk.Label(self)
            label.grid(row=row, column=0, padx=10, pady=6, sticky="w")
            self._register_text(label, label_key)
            entry = ttk.Entry(
                self,
                textvariable=variable,
                show="*" if is_password else "",
            )
            entry.grid(row=row, column=1, padx=10, pady=6, sticky="ew")
            if index == 0:
                button_frame = ttk.Frame(self)
                button_frame.grid(row=row, column=2, padx=10, pady=6)
                directory_button = ttk.Button(
                    button_frame,
                    command=self._choose_input_dir,
                )
                directory_button.pack(side="left", padx=(0, 4))
                self._register_text(directory_button, "choose_directory")
                zip_button = ttk.Button(
                    button_frame,
                    command=self._choose_input_zip,
                )
                zip_button.pack(side="left")
                self._register_text(zip_button, "choose_zip")
            elif index == 1:
                self._add_browse_button(row, self._choose_output)
            elif index == 2:
                self._add_browse_button(row, self._choose_runtime_dir)
            elif index == 3:
                self._add_browse_button(row, self._choose_iso)
            elif index == 4:
                self._add_browse_button(row, self._choose_old_iso)
            elif index == 5:
                self._add_browse_button(row, self._choose_new_iso)
            elif index == 6:
                self._add_browse_button(row, self._choose_winpe_iso)
            row += 1

        self._add_choice_combo(
            row,
            "guest_os",
            "guest_os",
            self._guest_os,
            GUEST_OS_CHOICES,
        )
        row += 1
        self._add_choice_combo(
            row,
            "windows_backend",
            "windows_backend",
            self._windows_backend,
            WINDOWS_BACKEND_CHOICES,
        )
        row += 1
        self._add_choice_combo(
            row,
            "linux_backend",
            "linux_backend",
            self._linux_backend,
            LINUX_BACKEND_CHOICES,
        )
        row += 1
        self._add_choice_combo(
            row,
            "network_model",
            "network_model",
            self._network_model,
            NETWORK_MODEL_CHOICES,
        )
        row += 1

        self._require_virtio_checkbox = ttk.Checkbutton(
            self,
            variable=self._require_virtio,
        )
        self._require_virtio_checkbox.grid(
            row=row,
            column=0,
            columnspan=3,
            padx=10,
            pady=5,
            sticky="w",
        )
        self._register_text(self._require_virtio_checkbox, "require_virtio")
        row += 1
        self._require_qga_checkbox = ttk.Checkbutton(
            self,
            variable=self._require_qga,
        )
        self._require_qga_checkbox.grid(
            row=row,
            column=0,
            columnspan=3,
            padx=10,
            pady=5,
            sticky="w",
        )
        self._register_text(self._require_qga_checkbox, "require_qga")
        row += 1
        self._add_choice_combo(
            row,
            "qga_install_mode",
            "qga_install_mode",
            self._qga_install_mode,
            QGA_INSTALL_MODE_CHOICES,
        )
        row += 1
        self._add_choice_combo(
            row,
            "winpe_arch",
            "winpe_arch",
            self._winpe_arch,
            WINPE_ARCH_CHOICES,
        )
        row += 1

        qemu_label = ttk.Label(self)
        qemu_label.grid(row=row, column=0, padx=10, pady=6, sticky="w")
        self._register_text(qemu_label, "qemu_system")
        ttk.Entry(self, textvariable=self._qemu_system).grid(
            row=row,
            column=1,
            padx=10,
            pady=6,
            sticky="ew",
        )
        row += 1

        self._run_button = ttk.Button(self, command=self._start)
        self._run_button.grid(row=row, column=0, columnspan=3, pady=12)
        self._register_text(self._run_button, "start_conversion")
        row += 1
        ttk.Label(self, textvariable=self._status).grid(
            row=row,
            column=0,
            columnspan=3,
            padx=10,
            sticky="w",
        )
        row += 1
        self._log = tk.Text(self, height=14, state="disabled", wrap="word")
        self._log.grid(
            row=row,
            column=0,
            columnspan=3,
            padx=10,
            pady=(8, 10),
            sticky="nsew",
        )
        self.rowconfigure(row, weight=1)

    def _add_browse_button(self, row: int, command: object) -> None:
        button = ttk.Button(self, command=command)
        button.grid(row=row, column=2, padx=10, pady=6)
        self._register_text(button, "choose")

    def _add_choice_combo(
        self,
        row: int,
        name: str,
        label_key: str,
        variable: tk.StringVar,
        choices: tuple[Choice, ...],
    ) -> None:
        label = ttk.Label(self)
        label.grid(row=row, column=0, padx=10, pady=6, sticky="w")
        self._register_text(label, label_key)
        display = tk.StringVar()
        widget = ttk.Combobox(
            self,
            textvariable=display,
            state="readonly",
        )
        widget.grid(row=row, column=1, padx=10, pady=6, sticky="ew")
        control = LocalizedChoiceControl(
            value=variable,
            display=display,
            widget=widget,
            choices=choices,
        )
        self._choice_controls[name] = control
        widget.bind(
            "<<ComboboxSelected>>",
            lambda _event, control_name=name: self._on_choice_selected(
                control_name
            ),
        )

    def _on_language_changed(self, _event: object = None) -> None:
        selected = self._language_display.get()
        for language in supported_languages():
            if selected == language_label(language):
                self._language.set(language)
                break
        self._apply_language()

    def _on_choice_selected(self, name: str) -> None:
        control = self._choice_controls[name]
        for choice in control.choices:
            if control.display.get() == self._tr(choice.label_key):
                control.value.set(choice.value)
                return

    def _apply_language(self) -> None:
        self._language.set(normalize_language(self._language.get()))
        self.title(self._tr("app_title"))
        for widget, key in self._text_widgets:
            widget.configure(text=self._tr(key))
        languages = supported_languages()
        self._language_combo.configure(
            values=tuple(language_label(language) for language in languages)
        )
        self._language_display.set(language_label(self._language.get()))
        for control in self._choice_controls.values():
            control.widget.configure(
                values=tuple(
                    self._tr(choice.label_key) for choice in control.choices
                )
            )
            selected = next(
                (
                    choice
                    for choice in control.choices
                    if choice.value == control.value.get()
                ),
                None,
            )
            control.display.set(
                self._tr(selected.label_key) if selected else control.value.get()
            )
        self._status.set(self._tr(self._status_key))

    def _set_status(self, key: str) -> None:
        self._status_key = key
        self._status.set(self._tr(key))

    def _filetypes(self, kind: str) -> tuple[tuple[str, str], ...]:
        if kind == "zip":
            return (
                (self._tr("filetype_zip"), "*.zip"),
                (self._tr("filetype_all"), "*.*"),
            )
        if kind == "iso":
            return (
                (self._tr("filetype_iso"), "*.iso"),
                (self._tr("filetype_all"), "*.*"),
            )
        return (
            (self._tr("filetype_qcow2"), "*.qcow2"),
            (self._tr("filetype_all"), "*.*"),
        )

    def _choose_input_dir(self) -> None:
        path = filedialog.askdirectory(title=self._tr("select_vmware_directory"))
        if path:
            self._input.set(path)

    def _choose_input_zip(self) -> None:
        path = filedialog.askopenfilename(
            title=self._tr("select_vmware_zip"),
            filetypes=self._filetypes("zip"),
        )
        if path:
            self._input.set(path)

    def _choose_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title=self._tr("select_output"),
            defaultextension=".qcow2",
            filetypes=self._filetypes("qcow2"),
        )
        if path:
            self._output.set(path)

    def _choose_iso(self) -> None:
        path = filedialog.askopenfilename(
            title=self._tr("select_virtio_iso"),
            filetypes=self._filetypes("iso"),
        )
        if path:
            self._driver_iso.set(path)

    def _choose_runtime_dir(self) -> None:
        path = filedialog.askdirectory(
            title=self._tr("select_runtime_directory")
        )
        if path:
            self._runtime_dir.set(path)

    def _choose_old_iso(self) -> None:
        path = filedialog.askopenfilename(
            title=self._tr("select_old_virtio_iso"),
            filetypes=self._filetypes("iso"),
        )
        if path:
            self._driver_old.set(path)

    def _choose_new_iso(self) -> None:
        path = filedialog.askopenfilename(
            title=self._tr("select_new_virtio_iso"),
            filetypes=self._filetypes("iso"),
        )
        if path:
            self._driver_new.set(path)

    def _choose_winpe_iso(self) -> None:
        path = filedialog.askopenfilename(
            title=self._tr("select_winpe_iso"),
            filetypes=self._filetypes("iso"),
        )
        if path:
            self._winpe_iso.set(path)

    def _append(self, message: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", message + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _conversion_options(self) -> ConversionOptions:
        return ConversionOptions(
            input_path=Path(self._input.get()),
            output_path=Path(self._output.get()),
            runtime_dir=Path(self._runtime_dir.get())
            if self._runtime_dir.get()
            else None,
            driver_iso=Path(self._driver_iso.get())
            if self._driver_iso.get()
            else None,
            guest_os=self._guest_os.get(),  # type: ignore[arg-type]
            windows_backend=self._windows_backend.get(),  # type: ignore[arg-type]
            linux_backend=self._linux_backend.get(),  # type: ignore[arg-type]
            network_model=self._network_model.get(),  # type: ignore[arg-type]
            require_virtio=self._require_virtio.get(),
            require_qga=self._require_qga.get(),
            qga_install_mode=self._qga_install_mode.get(),  # type: ignore[arg-type]
            windows_bake_username=self._windows_bake_username.get() or None,
            windows_bake_password=self._windows_bake_password.get() or None,
            virtio_win_old=Path(self._driver_old.get())
            if self._driver_old.get()
            else None,
            virtio_win_new=Path(self._driver_new.get())
            if self._driver_new.get()
            else None,
            winpe_iso=Path(self._winpe_iso.get())
            if self._winpe_iso.get()
            else None,
            winpe_arch=self._winpe_arch.get(),  # type: ignore[arg-type]
            qemu_system=self._qemu_system.get(),
        )

    def _start(self) -> None:
        if not self._input.get() or not self._output.get():
            messagebox.showerror(
                self._tr("missing_input_title"),
                self._tr("missing_input_message"),
            )
            return
        options = self._conversion_options()
        self._run_button.configure(state="disabled")
        self._set_status("status_running")
        thread = threading.Thread(
            target=self._worker,
            args=(options,),
            daemon=True,
        )
        thread.start()

    def _worker(self, options: ConversionOptions) -> None:
        try:
            result = convert(
                options,
                progress=lambda message: self.after(0, self._append, message),
            )
        except (ConversionError, OSError) as exc:
            self.after(0, self._show_failure, str(exc))
        else:
            self.after(0, self._show_success, result)
        finally:
            self.after(
                0,
                lambda: self._run_button.configure(state="normal"),
            )

    def _show_failure(self, detail: str) -> None:
        self._set_status("status_failed")
        messagebox.showerror(self._tr("conversion_failed_title"), detail)

    def _show_success(self, result: ConversionResult) -> None:
        self._set_status("status_completed")
        self._append(
            self._tr("log_output", output_path=result.output_path)
        )


def main() -> None:
    ConverterApp().mainloop()


if __name__ == "__main__":
    main()
