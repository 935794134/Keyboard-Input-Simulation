"""把文字用「模拟按键」的方式敲进当前焦点窗口，绕过剪贴板与粘贴限制。

原理：调用 Windows 的 SendInput，以 KEYEVENTF_UNICODE 逐字发送字符码点，
目标程序收到的是真实按键消息（而不是粘贴），因此禁用了 Ctrl+V 的输入框也能输入。

用法：把文字粘进上面的输入框 → 点「开始输入」或按 F9 → 倒计时内把光标点进目标输入框。
中途想停就按 F10。勾着「去掉回车」时，多行文字会压成一行再输入，免得回车把消息提前发出去。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

APP_TITLE = "模拟键盘输入（用于无法粘贴的地方）"
DEFAULT_COUNTDOWN = 5       # 倒计时秒数
DEFAULT_INTERVAL_MS = 10    # 每个字之间的间隔毫秒
COMPAT_INTERVAL_MS = 30     # 兼容模式下建议的最慢间隔（虚拟机吃太快容易丢字）
MAX_COUNTDOWN = 60
MAX_INTERVAL_MS = 500
WINDOW_WIDTH = 920
WINDOW_HEIGHT = 720
MIN_WINDOW_WIDTH = 700
MIN_WINDOW_HEIGHT = 560

# ---- Win32 常量 ----
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAPVK_VK_TO_VSC = 0

VK_TAB = 0x09
VK_RETURN = 0x0D
VK_LSHIFT = 0xA0
VK_F9 = 0x78
VK_F10 = 0x79

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID_START = 1
HOTKEY_ID_STOP = 2

# 左/右 Shift、Ctrl、Alt、Win：开始输入前先松开，避免残留在按住状态把输出弄乱
MODIFIER_KEYS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    )


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.VkKeyScanExW.argtypes = (wintypes.WCHAR, wintypes.HKL)
user32.VkKeyScanExW.restype = ctypes.c_short
user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetMessageW.argtypes = (
    ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
)
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


# ---- 底层：发送按键 ----

def send_key_events(events: list[tuple[int, int, int]]) -> None:
    """把一批按键事件交给系统。每项是 (虚拟键码, 扫描码, 标志位)。"""
    if not events:
        return
    inputs = (INPUT * len(events))()
    for index, (virtual_key, scan_code, flags) in enumerate(events):
        input_item = inputs[index]
        input_item.type = INPUT_KEYBOARD
        input_item.ki.wVk = virtual_key
        input_item.ki.wScan = scan_code
        input_item.ki.dwFlags = flags
        input_item.ki.time = 0
        input_item.ki.dwExtraInfo = 0
    sent = user32.SendInput(len(events), inputs, ctypes.sizeof(INPUT))
    if sent != len(events):
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "系统拒绝了模拟按键（目标程序可能以管理员身份运行，需要同样用管理员权限打开本工具）",
        )


def tap_virtual_key(virtual_key: int) -> None:
    """按下并松开一个功能键（回车、Tab 这类）。"""
    scan_code = user32.MapVirtualKeyW(virtual_key, MAPVK_VK_TO_VSC)
    send_key_events([(virtual_key, scan_code, 0), (virtual_key, scan_code, KEYEVENTF_KEYUP)])


def utf16_units(character: str) -> list[int]:
    """一个字符拆成 UTF-16 码元；表情等增补平面字符会拆成两个。"""
    raw = character.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def type_character(character: str) -> None:
    """以 Unicode 按键的方式输入单个字符，不依赖输入法和键盘布局。"""
    if character == "\n":
        tap_virtual_key(VK_RETURN)
    elif character == "\t":
        tap_virtual_key(VK_TAB)
    else:
        for code_unit in utf16_units(character):
            send_key_events(
                [
                    (0, code_unit, KEYEVENTF_UNICODE),
                    (0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                ]
            )


def release_modifiers() -> None:
    """松开所有修饰键，避免 Shift/Ctrl 卡住导致字符错乱。"""
    for virtual_key in MODIFIER_KEYS:
        scan_code = user32.MapVirtualKeyW(virtual_key, MAPVK_VK_TO_VSC)
        send_key_events([(virtual_key, scan_code, KEYEVENTF_KEYUP)])


def flatten_lines(text: str) -> str:
    """把多行文字压成一行：逐行去掉首尾空白，空行丢弃，行间用一个空格连接。

    勾了「去掉回车」时用它，避免回车在聊天框里把消息提前发出去；
    顺带把 Tab 换成空格，免得 Tab 把焦点跳出输入框。
    """
    lines = (line.strip().replace("\t", " ") for line in text.splitlines())
    return " ".join(line for line in lines if line)


def current_keyboard_layout() -> int:
    """取目标窗口所在线程的键盘布局；取不到就退回当前线程的布局。"""
    foreground = user32.GetForegroundWindow()
    thread_id = user32.GetWindowThreadProcessId(foreground, None)
    layout = user32.GetKeyboardLayout(thread_id)
    return layout or user32.GetKeyboardLayout(0)


def type_character_by_layout(character: str, layout: int) -> bool:
    """按键盘布局用真实扫描码输入一个字符（虚拟机、远程桌面认这种）。

    字符在当前布局里没有对应按键时返回 False，由调用方跳过。
    """
    if character == "\n":
        tap_virtual_key(VK_RETURN)
        return True
    if character == "\t":
        tap_virtual_key(VK_TAB)
        return True

    key_scan = user32.VkKeyScanExW(character, layout)
    if key_scan == -1:
        return False
    virtual_key = key_scan & 0xFF
    modifier_bits = (key_scan >> 8) & 0xFF
    if modifier_bits & 0x06:  # 需要 Ctrl 或 Alt 才打得出来，本工具不处理
        return False

    scan_code = user32.MapVirtualKeyW(virtual_key, MAPVK_VK_TO_VSC)
    needs_shift = bool(modifier_bits & 0x01)
    events = []
    if needs_shift:
        events.append((VK_LSHIFT, 0x2A, 0))
    events.append((virtual_key, scan_code, 0))
    events.append((virtual_key, scan_code, KEYEVENTF_KEYUP))
    if needs_shift:
        events.append((VK_LSHIFT, 0x2A, KEYEVENTF_KEYUP))
    send_key_events(events)
    return True


def count_untypeable(text: str) -> int:
    """数一下有多少字符在当前键盘布局下打不出来（中文一般都属于这种）。"""
    layout = current_keyboard_layout()
    return sum(
        1
        for character in text.replace("\r", "")
        if character not in "\n\t" and user32.VkKeyScanExW(character, layout) == -1
    )


# ---- 主流程：逐字输入 ----

def type_text(
    text: str,
    interval_ms: int,
    stop_event: threading.Event,
    on_progress=None,
    use_scancodes: bool = False,
) -> tuple[bool, int]:
    """逐字输入整段文字。

    返回 (是否完整输完, 因键盘布局打不出来而跳过的字符数)。
    use_scancodes=True 时走真实扫描码（兼容虚拟机），False 时走 Unicode 注入。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    release_modifiers()
    layout = current_keyboard_layout() if use_scancodes else 0
    delay_seconds = max(0, interval_ms) / 1000
    total = len(normalized)
    skipped = 0
    try:
        for index, character in enumerate(normalized, start=1):
            if stop_event.is_set():
                return False, skipped
            if use_scancodes:
                if not type_character_by_layout(character, layout):
                    skipped += 1
            else:
                type_character(character)
            if on_progress is not None:
                on_progress(index, total)
            if delay_seconds and index < total:
                time.sleep(delay_seconds)
        return True, skipped
    finally:
        release_modifiers()


# ---- 界面 ----

class KeyboardTyperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.hotkey_thread_id = 0
        self.pump_job: str | None = None
        self._build_ui()
        self.hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        self.hotkey_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.pump_job = self.root.after(80, self._drain_messages)

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        window_width, _ = self._center_window()

        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        self.clear_button = ttk.Button(header, text="清空", width=6, command=self.clear_text)
        self.clear_button.pack(side="left")
        ttk.Label(
            header,
            text="把要输入的文字粘到下面（Ctrl+V 直接粘）：",
        ).pack(side="left", padx=(10, 0))

        text_frame = ttk.Frame(container)
        text_frame.pack(fill="both", expand=True, pady=(6, 10))
        self.text_widget = tk.Text(text_frame, wrap="word", height=12, undo=True)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.text_widget.yview)
        self.text_widget.configure(yscrollcommand=scrollbar.set)
        self.text_widget.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        options = ttk.Frame(container)
        options.pack(fill="x")
        self.countdown_var = tk.StringVar(value=str(DEFAULT_COUNTDOWN))
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_MS))

        ttk.Label(options, text="倒计时").pack(side="left")
        ttk.Spinbox(
            options, from_=0, to=MAX_COUNTDOWN, width=5, textvariable=self.countdown_var
        ).pack(side="left", padx=(4, 4))
        ttk.Label(options, text="秒").pack(side="left", padx=(0, 18))

        ttk.Label(options, text="每个字间隔").pack(side="left")
        ttk.Spinbox(
            options, from_=0, to=MAX_INTERVAL_MS, width=6, textvariable=self.interval_var
        ).pack(side="left", padx=(4, 4))
        ttk.Label(options, text="毫秒").pack(side="left")

        toggles = ttk.Frame(container)
        toggles.pack(fill="x", pady=(8, 0))
        self.flatten_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toggles,
            text="去掉回车（防止消息提前发出）",
            variable=self.flatten_var,
        ).pack(side="left")
        self.compat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toggles,
            text="兼容模式（虚拟机 / 远程桌面）",
            variable=self.compat_var,
            command=self._on_compat_toggle,
        ).pack(side="left", padx=(18, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(10, 6))
        self.start_button = ttk.Button(buttons, text="开始输入 (F9)", command=self.start_typing)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            buttons, text="停止 (F10)", command=self.stop_typing, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        self.release_button = ttk.Button(
            buttons, text="释放按键", command=self.release_stuck_keys
        )
        self.release_button.pack(side="left")

        self.status_var = tk.StringVar(value="就绪：粘好文字后点「开始输入」，或直接按 F9。")
        ttk.Label(container, textvariable=self.status_var, foreground="#1f6feb").pack(anchor="w")

        hint = (
            "提示：倒计时期间把光标点进目标输入框；往虚拟机 / 远程桌面里输入请勾「兼容模式」；"
            "目标程序以管理员身份运行时，本工具也要用管理员身份打开。"
        )
        self.hint_label = ttk.Label(
            container,
            text=hint,
            foreground="#808080",
            wraplength=max(420, window_width - 60),
        )
        self.hint_label.pack(anchor="w", pady=(8, 0))
        self._hint_width = window_width
        self.root.bind("<Configure>", self._on_root_resize)

    # ---- 开始 / 停止 ----

    def _on_root_resize(self, event: tk.Event) -> None:
        """窗口变窄时让底部提示重新折行，免得被切掉。"""
        if event.widget is not self.root:
            return
        if abs(event.width - self._hint_width) < 8:
            return
        self._hint_width = event.width
        self.hint_label.configure(wraplength=max(420, event.width - 40))

    def _center_window(self) -> tuple[int, int]:
        """按屏幕尺寸和高分屏缩放把窗口放到中间，别一开就缩在左上角。"""
        scale = self.root.winfo_fpixels("1i") / 96.0  # 100% 缩放时约等于 1
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(int(WINDOW_WIDTH * scale), screen_width - 80)
        height = min(int(WINDOW_HEIGHT * scale), screen_height - 100)
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.root.minsize(int(MIN_WINDOW_WIDTH * scale), int(MIN_WINDOW_HEIGHT * scale))
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        return width, height

    def clear_text(self) -> None:
        self.text_widget.delete("1.0", "end")
        self.text_widget.focus_set()
        self.status_var.set("输入框已清空。")

    def _on_compat_toggle(self) -> None:
        """勾上兼容模式时，把太快的间隔抬到 30 毫秒，虚拟机吃太快容易丢字。"""
        if not self.compat_var.get():
            return
        interval_ms = self._read_number(
            self.interval_var, DEFAULT_INTERVAL_MS, 0, MAX_INTERVAL_MS
        )
        if interval_ms < COMPAT_INTERVAL_MS:
            self.interval_var.set(str(COMPAT_INTERVAL_MS))
            self.status_var.set("兼容模式：每个字间隔已调到 30 毫秒。")

    def start_typing(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        text = self.text_widget.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo(APP_TITLE, "输入框是空的，先把要输入的文字粘进去。")
            return

        if self.flatten_var.get():
            text = flatten_lines(text)
            if not text:
                messagebox.showinfo(APP_TITLE, "去掉回车以后什么都不剩了，检查一下内容。")
                return

        use_scancodes = self.compat_var.get()
        if use_scancodes:
            missing = count_untypeable(text)
            if missing and not messagebox.askokcancel(
                APP_TITLE,
                f"有 {missing} 个字符在当前键盘布局里打不出来（中文、全角符号一般都属于这种）。\n"
                "兼容模式会跳过它们；虚拟机里要输中文，得在虚拟机内自己开输入法。\n\n继续吗？",
            ):
                return

        countdown = self._read_number(self.countdown_var, DEFAULT_COUNTDOWN, 0, MAX_COUNTDOWN)
        interval_ms = self._read_number(
            self.interval_var, DEFAULT_INTERVAL_MS, 0, MAX_INTERVAL_MS
        )
        self.countdown_var.set(str(countdown))
        self.interval_var.set(str(interval_ms))

        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(
            target=self._typing_job,
            args=(text, countdown, interval_ms, use_scancodes),
            daemon=True,
        )
        self.worker.start()

    def stop_typing(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("正在停止…")

    def release_stuck_keys(self) -> None:
        """虚拟机里如果卡住（比如 Shift 一直按着），点这个补发松开信号。"""
        try:
            release_modifiers()
            self.status_var.set("已发送松开按键的信号；虚拟机里卡住的修饰键一般能解开。")
        except OSError as error:
            self.status_var.set(f"松开按键失败：{error}")

    def _typing_job(
        self, text: str, countdown: int, interval_ms: int, use_scancodes: bool
    ) -> None:
        # 不管中间怎么退出（取消、出错、按键被拒），最后一定发一条 done，
        # 否则按钮会一直卡在禁用状态，看起来就像程序死了。
        message = "输入结束。"
        try:
            for remaining in range(countdown, 0, -1):
                if self.stop_event.is_set():
                    message = "已取消，什么都没输入。"
                    return
                self.messages.put(
                    ("status", f"{remaining} 秒后开始输入，请把光标点进目标输入框…")
                )
                if self.stop_event.wait(1.0):
                    message = "已取消，什么都没输入。"
                    return

            self.messages.put(
                ("status", "开始输入…（兼容模式：真实扫描码）" if use_scancodes else "开始输入…")
            )
            progress_state = {"typed": 0}

            def report(done: int, total: int) -> None:
                progress_state["typed"] = done
                if done % 10 == 0 or done == total:
                    self.messages.put(("progress", (done, total)))

            finished, skipped = type_text(
                text,
                interval_ms,
                self.stop_event,
                report,
                use_scancodes=use_scancodes,
            )
            typed = progress_state["typed"] - skipped
            if finished:
                message = f"输入完成，共 {typed} 个字符。"
            else:
                message = f"已停止，已输入 {typed} 个字符。"
            if skipped:
                message += f"（另有 {skipped} 个字符当前布局打不出来，已跳过）"
        except OSError as error:
            message = f"模拟按键被系统拒绝：{error}"
        except Exception as error:  # noqa: BLE001 - 界面线程兜底，避免静默失败
            message = f"出错了：{error}"
        finally:
            self.messages.put(("done", message))

    # ---- 消息泵：子线程不直接碰界面 ----

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    done, total = payload  # type: ignore[misc]
                    self.status_var.set(f"正在输入… {done}/{total}")
                elif kind == "done":
                    self.status_var.set(str(payload))
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                elif kind == "hotkey":
                    if payload == "start":
                        self.start_typing()
                    else:
                        self.stop_typing()
        except queue.Empty:
            pass
        if not self.shutdown_event.is_set():
            self.pump_job = self.root.after(80, self._drain_messages)

    # ---- 全局快捷键：优先向系统注册 F9 / F10，注册不到再退回轮询 ----

    def _hotkey_loop(self) -> None:
        self.hotkey_thread_id = kernel32.GetCurrentThreadId()
        registered = user32.RegisterHotKey(
            None, HOTKEY_ID_START, 0, VK_F9
        ) and user32.RegisterHotKey(None, HOTKEY_ID_STOP, 0, VK_F10)
        if registered:
            self._hotkey_message_loop()
            return
        user32.UnregisterHotKey(None, HOTKEY_ID_START)
        user32.UnregisterHotKey(None, HOTKEY_ID_STOP)
        self._hotkey_polling_loop()

    def _hotkey_message_loop(self) -> None:
        """注册成功时，系统把 WM_HOTKEY 直接投递到这个线程的消息队列。"""
        message = MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message != WM_HOTKEY:
                continue
            if message.wParam == HOTKEY_ID_START:
                self.messages.put(("hotkey", "start"))
            elif message.wParam == HOTKEY_ID_STOP:
                self.messages.put(("hotkey", "stop"))
        user32.UnregisterHotKey(None, HOTKEY_ID_START)
        user32.UnregisterHotKey(None, HOTKEY_ID_STOP)

    def _hotkey_polling_loop(self) -> None:
        """注册失败的手动轮询方案。"""
        previous = {VK_F9: False, VK_F10: False}
        while not self.shutdown_event.is_set():
            for virtual_key, action in ((VK_F9, "start"), (VK_F10, "stop")):
                is_down = bool(user32.GetAsyncKeyState(virtual_key) & 0x8000)
                if is_down and not previous[virtual_key]:
                    self.messages.put(("hotkey", action))
                previous[virtual_key] = is_down
            time.sleep(0.03)

    # ---- 收尾 ----

    def _on_close(self) -> None:
        self.shutdown_event.set()
        self.stop_event.set()
        if self.pump_job is not None:
            try:
                self.root.after_cancel(self.pump_job)
            except tk.TclError:
                pass
            self.pump_job = None
        thread_id = getattr(self, "hotkey_thread_id", 0)
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        self.root.destroy()

    @staticmethod
    def _read_number(variable: tk.StringVar, fallback: int, minimum: int, maximum: int) -> int:
        try:
            value = int(float(variable.get()))
        except (TypeError, ValueError):
            value = fallback
        return max(minimum, min(maximum, value))


def enable_dpi_awareness() -> None:
    """高分屏下文字不糊。"""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    application = KeyboardTyperApp(root)
    root.app = application  # 保住引用，防止被回收
    root.mainloop()


if __name__ == "__main__":
    main()
