import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import sys
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import queue
import re
import traceback
from app_paths import build_runtime_env, get_worker_command, runtime_path
from batch_runtime_config import (
    BATCH_CONCURRENCY,
    BATCH_RETRY_LIMIT,
    SUBPROCESS_SLOT_LIMIT,
    TASK_QUEUE_CAPACITY,
)
from draft_registry import get_draft_root, reconcile_root_meta
from media_file_rules import scan_video_file_paths
from subprocess_windows import run_hidden
from text_output_utils import decode_process_output, repair_mojibake_text

LOCKED_LLM_BASE_URL = "https://api.kuai.host/v1"
STATUS_QUEUED = "排队中"
STATUS_RUNNING = "生成中"
STATUS_READY = "待发布"
STATUS_FAILED = "失败"
STATUS_ERROR = "异常"
TASK_UI_REFRESH_MS = 1500
UI_SETTINGS_PATH = runtime_path("output", "ui_settings.json")

os.environ.update(build_runtime_env(os.environ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class YunFengEditorUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("云锋剪辑 - 智能视频生成系统 (专业版)")
        self._configure_window()
        self.configure(padx=10, pady=10)

        self.output_dir = runtime_path("output")
        self.task_logs_dir = os.path.join(self.output_dir, "task_logs")
        self.internal_log_path = os.path.join(self.output_dir, "internal_maintenance.log")
        self.ui_settings_path = UI_SETTINGS_PATH
        os.makedirs(self.task_logs_dir, exist_ok=True)

        # 提前初始化 uiautomation 缓存，防止并发生成草稿时 comtypes 缓存冲突导致 Permission Denied
        try:
            import uiautomation
        except ImportError:
            print("[WARN] 未安装 uiautomation，导出等依赖桌面自动化的能力可能受限。")

        # 任务并发池统一按批量并发配置运行
        self.executor = ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY)
        self.task_queue = queue.Queue(maxsize=TASK_QUEUE_CAPACITY)
        self.subprocess_slots = threading.BoundedSemaphore(SUBPROCESS_SLOT_LIMIT)
        self.task_count = 0
        self.running_tasks = {}
        self._tasks_lock = threading.Lock()
        self._last_queue_status_text = ""
        self._last_maintenance_status_text = ""
        self._last_button_states = {}

        self.create_widgets()
        self._load_ui_settings()
        self.repair_draft_registry()
        # 启动时先清理一次历史残留的空草稿
        self.cleanup_empty_drafts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(TASK_UI_REFRESH_MS, self.update_task_status_ui)

    def _configure_window(self):
        screen_width = max(self.winfo_screenwidth(), 1280)
        screen_height = max(self.winfo_screenheight(), 900)
        window_width = min(max(1120, int(screen_width * 0.86)), screen_width - 40)
        window_height = min(max(860, int(screen_height * 0.9)), screen_height - 60)
        pos_x = max((screen_width - window_width) // 2, 0)
        pos_y = max((screen_height - window_height) // 2, 0)

        self.geometry(f"{window_width}x{window_height}+{pos_x}+{pos_y}")
        self.minsize(min(window_width, 1080), min(window_height, 820))

    def _setup_apple_theme(self):
        """Apple-inspired visual theme using ttk.Style with 'clam' theme."""
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass

        BG          = '#f5f5f7'
        SURFACE     = '#ffffff'
        ACCENT      = '#007aff'
        ACCENT_DARK = '#0056cc'
        ACCENT_BG   = '#e8f2ff'
        TEXT        = '#1d1d1f'
        TEXT_SEC    = '#86868b'
        TEXT_TER    = '#aeaeb2'
        SEPARATOR   = '#d2d2d7'
        FILL        = '#e5e5ea'
        FILL_SEC    = '#f2f2f7'
        SUCCESS     = '#34c759'
        WARNING     = '#ff9500'
        ERROR       = '#ff3b30'
        GREEN_BG    = '#e6f9ed'
        ORANGE_BG   = '#fff3e5'
        RED_BG      = '#ffe8e6'

        FONT_FAMILY = 'Segoe UI'

        self.configure(bg=BG)
        self.option_add('*Background', BG)

        style.configure('TFrame', background=BG)
        style.configure('Card.TFrame', background=SURFACE)

        style.configure('TLabel',
            background=BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.configure('Header.TLabel',
            background=BG, foreground=TEXT,
            font=(FONT_FAMILY, 18, 'bold'), padding=(0, 4, 0, 0))
        style.configure('Subheader.TLabel',
            background=BG, foreground=TEXT,
            font=(FONT_FAMILY, 13, 'bold'), padding=(0, 2, 0, 2))
        style.configure('Info.TLabel',
            background=BG, foreground=TEXT_SEC, font=(FONT_FAMILY, 9))
        style.configure('Status.TLabel',
            background=BG, foreground=TEXT,
            font=(FONT_FAMILY, 10, 'bold'))
        style.configure('CardLabel.TLabel',
            background=SURFACE, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.configure('SummaryKey.TLabel',
            background=SURFACE, foreground=TEXT_SEC, font=(FONT_FAMILY, 9))
        style.configure('SummaryVal.TLabel',
            background=SURFACE, foreground=TEXT,
            font=(FONT_FAMILY, 10, 'bold'))
        style.configure('Success.TLabel',
            background=BG, foreground=SUCCESS,
            font=(FONT_FAMILY, 10, 'bold'))
        style.configure('Warning.TLabel',
            background=BG, foreground=WARNING,
            font=(FONT_FAMILY, 10, 'bold'))
        style.configure('Error.TLabel',
            background=BG, foreground=ERROR,
            font=(FONT_FAMILY, 10, 'bold'))

        style.configure('TNotebook',
            background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('TNotebook.Tab',
            background=FILL_SEC, foreground=TEXT_SEC,
            font=(FONT_FAMILY, 10), padding=(18, 8), borderwidth=0)
        style.map('TNotebook.Tab',
            background=[('selected', SURFACE), ('active', SURFACE)],
            foreground=[('selected', ACCENT), ('active', TEXT)],
            expand=[('selected', (0, 0, 0, 0))])

        style.configure('Card.TLabelframe',
            background=SURFACE, borderwidth=1, relief='solid',
            bordercolor=SEPARATOR, padding=(16, 12, 16, 12))
        style.configure('Card.TLabelframe.Label',
            background=SURFACE, foreground=TEXT,
            font=(FONT_FAMILY, 11, 'bold'), padding=(8, 0))

        style.configure('Primary.TButton',
            background=ACCENT, foreground='#ffffff',
            font=(FONT_FAMILY, 10, 'bold'), borderwidth=0,
            relief='flat', padding=(18, 8), anchor='center')
        style.map('Primary.TButton',
            background=[('active', ACCENT_DARK), ('disabled', FILL)],
            foreground=[('disabled', TEXT_TER)],
            relief=[('pressed', 'flat')])

        style.configure('Secondary.TButton',
            background=FILL_SEC, foreground=TEXT,
            font=(FONT_FAMILY, 10), borderwidth=1, relief='solid',
            bordercolor=SEPARATOR, padding=(14, 6), anchor='center')
        style.map('Secondary.TButton',
            background=[('active', FILL), ('disabled', FILL_SEC)],
            foreground=[('disabled', TEXT_TER)])

        style.configure('Ghost.TButton',
            background=SURFACE, foreground=ACCENT,
            font=(FONT_FAMILY, 9), borderwidth=0, relief='flat',
            padding=(10, 4), anchor='center')
        style.map('Ghost.TButton',
            foreground=[('active', ACCENT_DARK), ('disabled', TEXT_TER)])

        style.configure('TButton',
            background=FILL_SEC, foreground=TEXT,
            font=(FONT_FAMILY, 10), borderwidth=1, relief='solid',
            bordercolor=SEPARATOR, padding=(14, 6), anchor='center')
        style.map('TButton',
            background=[('active', FILL), ('disabled', FILL_SEC)],
            foreground=[('disabled', TEXT_TER)])

        style.configure('TEntry',
            fieldbackground='#ffffff', foreground=TEXT,
            font=(FONT_FAMILY, 10), borderwidth=1, relief='solid',
            bordercolor=SEPARATOR, padding=(8, 6))
        style.map('TEntry',
            bordercolor=[('focus', ACCENT)],
            fieldbackground=[('readonly', FILL_SEC)])

        style.configure('TCombobox',
            fieldbackground='#ffffff', background=SURFACE,
            foreground=TEXT, font=(FONT_FAMILY, 10),
            borderwidth=1, relief='solid', bordercolor=SEPARATOR,
            padding=(8, 6), arrowcolor=TEXT, arrowsize=14)
        style.map('TCombobox',
            fieldbackground=[('readonly', SURFACE)],
            bordercolor=[('focus', ACCENT), ('readonly', SEPARATOR)])
        self.option_add('*TCombobox*Listbox.font', (FONT_FAMILY, 10))
        self.option_add('*TCombobox*Listbox.background', SURFACE)
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        self.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')

        style.configure('Treeview',
            background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, font=(FONT_FAMILY, 9),
            rowheight=32, borderwidth=0)
        style.configure('Treeview.Heading',
            background=FILL_SEC, foreground=TEXT_SEC,
            font=(FONT_FAMILY, 9, 'bold'), borderwidth=0,
            padding=(8, 6), relief='flat')
        style.map('Treeview.Heading', background=[('active', FILL)])
        style.map('Treeview',
            background=[('selected', ACCENT_BG)],
            foreground=[('selected', TEXT)])

        style.configure('Vertical.TScrollbar',
            background=FILL_SEC, troughcolor=FILL_SEC,
            borderwidth=0, arrowsize=0, width=8)
        style.map('Vertical.TScrollbar', background=[('active', FILL)])
        style.configure('Horizontal.TScrollbar',
            background=FILL_SEC, troughcolor=FILL_SEC,
            borderwidth=0, arrowsize=0, width=8)
        style.map('Horizontal.TScrollbar', background=[('active', FILL)])

        style.configure('TSeparator', background=SEPARATOR)

        style.configure('TCheckbutton',
            background=BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.map('TCheckbutton', background=[('active', BG)])
        style.configure('Card.TCheckbutton',
            background=SURFACE, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.map('Card.TCheckbutton', background=[('active', SURFACE)])

        style.configure('TProgressbar',
            troughcolor=FILL, background=ACCENT,
            borderwidth=0, thickness=6)

        self._colors = {
            'bg': BG, 'surface': SURFACE, 'accent': ACCENT,
            'accent_dark': ACCENT_DARK, 'accent_bg': ACCENT_BG,
            'text': TEXT, 'text_sec': TEXT_SEC, 'text_ter': TEXT_TER,
            'separator': SEPARATOR, 'fill': FILL, 'fill_sec': FILL_SEC,
            'success': SUCCESS, 'warning': WARNING, 'error': ERROR,
            'green_bg': GREEN_BG, 'orange_bg': ORANGE_BG, 'red_bg': RED_BG,
        }
        self._fonts = {
            'body':       (FONT_FAMILY, 10),
            'body_bold':  (FONT_FAMILY, 10, 'bold'),
            'caption':    (FONT_FAMILY, 9),
            'caption_bold': (FONT_FAMILY, 9, 'bold'),
            'heading':    (FONT_FAMILY, 13, 'bold'),
            'title':      (FONT_FAMILY, 18, 'bold'),
            'mono':       ('Cascadia Code', 9),
        }

    def _create_styled_scale(self, parent, from_, to, orient='horizontal',
                             resolution=None, length=None, **extra_opts):
        opts = {
            'orient': orient,
            'from_': from_,
            'to': to,
            'bg': self._colors['surface'],
            'fg': self._colors['text'],
            'troughcolor': self._colors['fill'],
            'activebackground': self._colors['accent'],
            'highlightbackground': self._colors['surface'],
            'highlightcolor': self._colors['accent'],
            'highlightthickness': 1,
            'borderwidth': 0,
            'font': self._fonts['caption'],
            'sliderlength': 20,
            'sliderrelief': 'flat',
        }
        if resolution is not None:
            opts['resolution'] = resolution
        if length is not None:
            opts['length'] = length
        opts.update(extra_opts)
        return tk.Scale(parent, **opts)

    def create_widgets(self):
        self._setup_apple_theme()

        # 1. 顶部标题
        header_frame = ttk.Frame(self)
        header_frame.pack(fill="x", pady=(0, 5))
        ttk.Label(header_frame, text="智能视频生成系统", style="Header.TLabel").pack(anchor="w")
        ttk.Label(header_frame, text="集成 AI 语音识别、语义打点、智能匹配与并发生成的全自动工作流。", style="Info.TLabel").pack(anchor="w")

        # 状态栏变量
        self.current_task_idx = 0
        self.total_tasks_submitted = 0
        self.maintenance_warning_count = 0
        self.latest_maintenance_warning = "当前无维护告警"
        
        # 2. 主选项卡区
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, pady=2)

        self.tab_auto_gen = ttk.Frame(self.notebook, padding=(12, 8, 12, 8))
        self.tab_settings = ttk.Frame(self.notebook, padding=(12, 8, 12, 8))
        self.tab_tasks   = ttk.Frame(self.notebook, padding=(12, 8, 12, 8))

        self.notebook.add(self.tab_auto_gen, text="一键智能合成")
        self.notebook.add(self.tab_settings, text="成品模板设置")
        self.notebook.add(self.tab_tasks, text="任务队列看板")

        self.build_auto_gen_tab()
        self.build_settings_tab()
        self.build_tasks_tab()

    def _collect_ui_settings(self):
        data = {
            "entries": {},
            "combo_values": {},
            "scale_values": {},
        }

        if hasattr(self, "entries"):
            for key, entry in self.entries.items():
                data["entries"][key] = entry.get().strip()

        if hasattr(self, "entry_llm_key"):
            data["entries"]["llm_api_key"] = self.entry_llm_key.get().strip()
        if hasattr(self, "entry_llm_model"):
            data["entries"]["llm_model"] = self.entry_llm_model.get().strip()

        if hasattr(self, "combo_sens"):
            data["combo_values"]["sensitivity"] = self.combo_sens.get().strip()
        if hasattr(self, "combo_res"):
            data["combo_values"]["resolution"] = self.combo_res.get().strip()
        if hasattr(self, "combo_template"):
            data["combo_values"]["template_mode"] = self.combo_template.get().strip()
        if hasattr(self, "combo_rhythm"):
            data["combo_values"]["rhythm_mode"] = self.combo_rhythm.get().strip()

        if hasattr(self, "scale_ad_freq"):
            data["scale_values"]["ad_freq"] = int(self.scale_ad_freq.get())
        if hasattr(self, "scale_sticker_freq"):
            data["scale_values"]["sticker_freq"] = int(self.scale_sticker_freq.get())
        if hasattr(self, "scale_broll_freq"):
            data["scale_values"]["broll_freq"] = int(self.scale_broll_freq.get())
        if hasattr(self, "scale_min_host"):
            data["scale_values"]["min_host_duration"] = float(self.scale_min_host.get())

        if hasattr(self, "entry_user_bgm_dir"):
            data["entries"]["user_bgm_dir"] = self.entry_user_bgm_dir.get().strip()
            
        data["checkbox_values"] = {}
        if hasattr(self, "var_auto_fill_density"):
            data["checkbox_values"]["auto_fill_density"] = self.var_auto_fill_density.get()
        if hasattr(self, "var_allow_prev_video_reuse"):
            data["checkbox_values"]["allow_prev_video_reuse"] = self.var_allow_prev_video_reuse.get()
        if hasattr(self, "var_bgm_normalize"):
            data["checkbox_values"]["bgm_normalize"] = self.var_bgm_normalize.get()
        if hasattr(self, "var_bgm_phase_check"):
            data["checkbox_values"]["bgm_phase_check"] = self.var_bgm_phase_check.get()

        if hasattr(self, "combo_density_template"):
            data["combo_values"]["density_template"] = self.combo_density_template.get().strip()
        if hasattr(self, "combo_broll_ratio"):
            data["combo_values"]["broll_ratio"] = self.combo_broll_ratio.get().strip()
        if hasattr(self, "combo_bgm_pick_mode"):
            data["combo_values"]["bgm_pick_mode"] = self.combo_bgm_pick_mode.get().strip()

        if hasattr(self, "scale_density_window"):
            data["scale_values"]["density_window"] = int(self.scale_density_window.get())
        if hasattr(self, "scale_min_inserts"):
            data["scale_values"]["min_inserts_per_window"] = int(self.scale_min_inserts.get())
        if hasattr(self, "scale_insert_min"):
            data["scale_values"]["insert_min_duration"] = float(self.scale_insert_min.get())
        if hasattr(self, "scale_insert_max"):
            data["scale_values"]["insert_max_duration"] = float(self.scale_insert_max.get())
        if hasattr(self, "scale_bgm_crossfade"):
            data["scale_values"]["bgm_crossfade_ms"] = int(self.scale_bgm_crossfade.get())
        if hasattr(self, "scale_bgm_lufs"):
            data["scale_values"]["bgm_target_lufs"] = int(self.scale_bgm_lufs.get())
        if hasattr(self, "scale_trigger_delay"):
            data["scale_values"]["keyword_trigger_delay"] = float(self.scale_trigger_delay.get())
        if hasattr(self, "scale_idle_gap"):
            data["scale_values"]["keyword_idle_gap"] = float(self.scale_idle_gap.get())
        if hasattr(self, "scale_semantic_threshold"):
            data["scale_values"]["semantic_match_threshold"] = float(self.scale_semantic_threshold.get())
        if hasattr(self, "scale_target_broll_ratio"):
            data["scale_values"]["target_broll_ratio"] = float(self.scale_target_broll_ratio.get())

        return data

    def _save_ui_settings(self):
        try:
            os.makedirs(os.path.dirname(self.ui_settings_path), exist_ok=True)
            with open(self.ui_settings_path, "w", encoding="utf-8") as f:
                json.dump(self._collect_ui_settings(), f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self._log_nonfatal_issue("ui_settings", f"界面设置保存失败: {exc}", exc=exc)

    def _load_ui_settings(self):
        if not os.path.exists(self.ui_settings_path):
            return

        try:
            with open(self.ui_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self._log_nonfatal_issue("ui_settings", f"界面设置读取失败: {exc}", exc=exc)
            return

        for key, entry in getattr(self, "entries", {}).items():
            value = str(data.get("entries", {}).get(key, "")).strip()
            if value:
                entry.delete(0, tk.END)
                entry.insert(0, value)

        llm_api_key = str(data.get("entries", {}).get("llm_api_key", "")).strip()
        if llm_api_key:
            self.entry_llm_key.delete(0, tk.END)
            self.entry_llm_key.insert(0, llm_api_key)

        llm_model = str(data.get("entries", {}).get("llm_model", "")).strip()
        if llm_model:
            self.entry_llm_model.delete(0, tk.END)
            self.entry_llm_model.insert(0, llm_model)

        sensitivity = str(data.get("combo_values", {}).get("sensitivity", "")).strip()
        if sensitivity and sensitivity in self.combo_sens.cget("values"):
            self.combo_sens.set(sensitivity)

        resolution = str(data.get("combo_values", {}).get("resolution", "")).strip()
        if resolution and resolution in self.combo_res.cget("values"):
            self.combo_res.set(resolution)
            
        template_mode = str(data.get("combo_values", {}).get("template_mode", "")).strip()
        if hasattr(self, "combo_template") and template_mode and template_mode in self.combo_template.cget("values"):
            self.combo_template.set(template_mode)
            
        rhythm_mode = str(data.get("combo_values", {}).get("rhythm_mode", "")).strip()
        if hasattr(self, "combo_rhythm") and rhythm_mode and rhythm_mode in self.combo_rhythm.cget("values"):
            self.combo_rhythm.set(rhythm_mode)

        scale_values = data.get("scale_values", {})
        if "ad_freq" in scale_values:
            self.scale_ad_freq.set(scale_values["ad_freq"])
        if "sticker_freq" in scale_values:
            self.scale_sticker_freq.set(scale_values["sticker_freq"])
        if "broll_freq" in scale_values:
            self.scale_broll_freq.set(scale_values["broll_freq"])
        if "min_host_duration" in scale_values and hasattr(self, "scale_min_host"):
            self.scale_min_host.set(scale_values["min_host_duration"])
            

        user_bgm_dir = str(data.get("entries", {}).get("user_bgm_dir", "")).strip()
        if user_bgm_dir and hasattr(self, "entry_user_bgm_dir"):
            self.entry_user_bgm_dir.delete(0, tk.END)
            self.entry_user_bgm_dir.insert(0, user_bgm_dir)
            
        checkbox_values = data.get("checkbox_values", {})
        if "auto_fill_density" in checkbox_values and hasattr(self, "var_auto_fill_density"):
            self.var_auto_fill_density.set(bool(checkbox_values["auto_fill_density"]))
        if "allow_prev_video_reuse" in checkbox_values and hasattr(self, "var_allow_prev_video_reuse"):
            self.var_allow_prev_video_reuse.set(bool(checkbox_values["allow_prev_video_reuse"]))
        if "bgm_normalize" in checkbox_values and hasattr(self, "var_bgm_normalize"):
            self.var_bgm_normalize.set(bool(checkbox_values["bgm_normalize"]))
        if "bgm_phase_check" in checkbox_values and hasattr(self, "var_bgm_phase_check"):
            self.var_bgm_phase_check.set(bool(checkbox_values["bgm_phase_check"]))

        density_template = str(data.get("combo_values", {}).get("density_template", "")).strip()
        if hasattr(self, "combo_density_template") and density_template and density_template in self.combo_density_template.cget("values"):
            self.combo_density_template.set(density_template)
        broll_ratio = str(data.get("combo_values", {}).get("broll_ratio", "")).strip()
        if hasattr(self, "combo_broll_ratio") and broll_ratio and broll_ratio in self.combo_broll_ratio.cget("values"):
            self.combo_broll_ratio.set(broll_ratio)
        bgm_pick_mode = str(data.get("combo_values", {}).get("bgm_pick_mode", "")).strip()
        if hasattr(self, "combo_bgm_pick_mode") and bgm_pick_mode and bgm_pick_mode in self.combo_bgm_pick_mode.cget("values"):
            self.combo_bgm_pick_mode.set(bgm_pick_mode)

        if "density_window" in scale_values and hasattr(self, "scale_density_window"):
            self.scale_density_window.set(scale_values["density_window"])
        if "min_inserts_per_window" in scale_values and hasattr(self, "scale_min_inserts"):
            self.scale_min_inserts.set(scale_values["min_inserts_per_window"])
        if "insert_min_duration" in scale_values and hasattr(self, "scale_insert_min"):
            self.scale_insert_min.set(scale_values["insert_min_duration"])
        if "insert_max_duration" in scale_values and hasattr(self, "scale_insert_max"):
            self.scale_insert_max.set(scale_values["insert_max_duration"])
        if "bgm_crossfade_ms" in scale_values and hasattr(self, "scale_bgm_crossfade"):
            self.scale_bgm_crossfade.set(scale_values["bgm_crossfade_ms"])
        if "bgm_target_lufs" in scale_values and hasattr(self, "scale_bgm_lufs"):
            self.scale_bgm_lufs.set(scale_values["bgm_target_lufs"])
        if "keyword_trigger_delay" in scale_values and hasattr(self, "scale_trigger_delay"):
            self.scale_trigger_delay.set(scale_values["keyword_trigger_delay"])
        if "keyword_idle_gap" in scale_values and hasattr(self, "scale_idle_gap"):
            self.scale_idle_gap.set(scale_values["keyword_idle_gap"])
        if "semantic_match_threshold" in scale_values and hasattr(self, "scale_semantic_threshold"):
            self.scale_semantic_threshold.set(scale_values["semantic_match_threshold"])
        if "target_broll_ratio" in scale_values and hasattr(self, "scale_target_broll_ratio"):
            self.scale_target_broll_ratio.set(scale_values["target_broll_ratio"])
            



    def on_close(self):
        self._save_ui_settings()
        self.executor.shutdown(wait=False)
        self.destroy()

    def _append_internal_log(self, stage, message, exc=None):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{timestamp}] [{stage}] {message}"]
        if exc is not None:
            lines.append(f"{type(exc).__name__}: {exc}")
            lines.append(traceback.format_exc().strip())
        lines.append("")
        with open(self.internal_log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _log_nonfatal_issue(self, stage, message, exc=None):
        print(f"[WARN] {message}")
        self.maintenance_warning_count += 1
        self.latest_maintenance_warning = f"[{stage}] {message}"
        try:
            self._append_internal_log(stage, message, exc=exc)
        except Exception:
            print(f"[WARN] 维护日志写入失败: {stage}")

    def build_auto_gen_tab(self):
        frame = self.tab_auto_gen
        scroll_host = ttk.Frame(frame)
        scroll_host.pack(fill="both", expand=True)

        self.auto_gen_canvas = tk.Canvas(scroll_host, highlightthickness=0, borderwidth=0,
                                           bg=self._colors['bg'])
        auto_scrollbar = ttk.Scrollbar(
            scroll_host,
            orient="vertical",
            command=self.auto_gen_canvas.yview,
        )
        self.auto_gen_canvas.configure(yscrollcommand=auto_scrollbar.set)

        auto_scrollbar.pack(side="right", fill="y")
        self.auto_gen_canvas.pack(side="left", fill="both", expand=True)

        content = ttk.Frame(self.auto_gen_canvas)
        canvas_window = self.auto_gen_canvas.create_window((0, 0), window=content, anchor="nw")
        self.auto_gen_content = content

        def sync_scroll_region(_event=None):
            self.auto_gen_canvas.configure(scrollregion=self.auto_gen_canvas.bbox("all"))

        def sync_content_width(event):
            self.auto_gen_canvas.itemconfigure(canvas_window, width=event.width)

        content.bind("<Configure>", sync_scroll_region)
        self.auto_gen_canvas.bind("<Configure>", sync_content_width)

        def on_mousewheel(event):
            if self.auto_gen_canvas.winfo_exists():
                self.auto_gen_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._auto_gen_mousewheel_handler = on_mousewheel

        def bind_mousewheel_recursively(widget):
            widget.bind("<MouseWheel>", self._auto_gen_mousewheel_handler)
            for child in widget.winfo_children():
                bind_mousewheel_recursively(child)
        self._bind_auto_gen_mousewheel = bind_mousewheel_recursively

        # 1. 路径输入区
        path_frame = ttk.LabelFrame(content, text="素材路径配置 (必填)", style='Card.TLabelframe')
        path_frame.pack(fill="x", pady=2)
        path_frame.columnconfigure(1, weight=1)

        self.entries = {}
        
        def add_path_row(parent, row, label_text, key):
            ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="e", pady=3, padx=5)
            entry = ttk.Entry(parent, width=55)
            entry.grid(row=row, column=1, sticky="we", pady=3, padx=5)
            ttk.Button(parent, text="选择文件夹...", style='Ghost.TButton',
                        command=lambda: self.browse_folder(entry)).grid(row=row, column=2, padx=5, pady=3)
            self.entries[key] = entry

        add_path_row(path_frame, 0, "🗣️ 口播素材路径 (仅视频):", "speech")
        add_path_row(path_frame, 1, "📦 产品素材路径 (展示片段):", "product")
        add_path_row(path_frame, 2, "🤒 病症素材路径 (痛点片段):", "symptom")
        add_path_row(path_frame, 3, "🎵 音效素材路径 (可选):", "audio")
        add_path_row(path_frame, 4, "🎼 用户背景音乐文件夹 (my_bg_music):", "bgm")
        add_path_row(path_frame, 5, "📺 广审素材路径 (可选):", "ad_review")
        add_path_row(path_frame, 6, "✨ 贴图素材路径 (可选):", "sticker")
        add_path_row(path_frame, 7, "📁 统一总目录 (可选快速填入):", "unified")

        # 快速填充按钮
        def auto_fill_from_unified():
            base = self.entries["unified"].get().strip()
            if base and os.path.exists(base):
                mapping = [
                    ("speech", "口播", "必选"), ("product", "产品", "必选"), ("symptom", "病症", "必选"),
                    ("audio", "音效", "可选"), ("bgm", "my_bg_music", "可选"), ("ad_review", "广审", "可选"), ("sticker", "贴图", "可选")
                ]

                try:
                    import json
                    output_dir = runtime_path("output")
                    os.makedirs(output_dir, exist_ok=True)

                    mapping_data = []
                    found_paths = {k: None for k, _, _ in mapping}

                    for root, dirs, files in os.walk(base):
                        rel_path = os.path.relpath(root, base)
                        if rel_path == '.':
                            continue

                        depth = len(rel_path.split(os.sep))

                        mat_type = "未知"

                        path_parts = rel_path.split(os.sep)
                        matched = False
                        for part in path_parts:
                            for k, folder_name, m_type in mapping:
                                if folder_name in part or part in folder_name:
                                    mat_type = m_type
                                    matched = True
                                    if found_paths[k] is None:
                                        found_paths[k] = root
                                    break
                            if matched:
                                break

                        mapping_data.append({
                            "relative_path": rel_path,
                            "depth": depth,
                            "material_type": mat_type
                        })

                        target_dir = os.path.join(output_dir, rel_path)
                        os.makedirs(target_dir, exist_ok=True)

                    for k, folder_name, _ in mapping:
                        p = found_paths[k] if found_paths[k] else os.path.join(base, folder_name)
                        if os.path.exists(p):
                            self.entries[k].delete(0, tk.END)
                            self.entries[k].insert(0, p)

                    mapping_file = os.path.join(output_dir, "directory_mapping.json")
                    with open(mapping_file, 'w', encoding='utf-8') as f:
                        json.dump(mapping_data, f, ensure_ascii=False, indent=4)

                    messagebox.showinfo("推导成功", f"路径已自动填入，并已生成结构映射表及镜像目录树：\n{mapping_file}")
                except Exception as e:
                    messagebox.showerror("扫描错误", f"目录递归扫描失败: {e}")
            else:
                messagebox.showwarning("提示", "统一总目录不存在！")

        ttk.Button(path_frame, text="自动从总目录推导子路径", command=auto_fill_from_unified,
                   style='Secondary.TButton').grid(row=7, column=3, padx=5)

        # 1.5 LLM 大模型配置区
        llm_frame = ttk.LabelFrame(content, text="AI 大模型配置 (用于深度语义理解)", style='Card.TLabelframe')
        llm_frame.pack(fill="x", pady=2)
        llm_frame.columnconfigure(1, weight=1)
        llm_frame.columnconfigure(3, weight=1)
        
        ttk.Label(llm_frame, text="API Key:").grid(row=0, column=0, sticky="e", pady=3, padx=5)
        self.entry_llm_key = ttk.Entry(llm_frame, width=40, show="*")
        self.entry_llm_key.grid(row=0, column=1, sticky="w", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="服务通道:").grid(row=0, column=2, sticky="e", pady=3, padx=5)
        ttk.Label(
            llm_frame,
            text="已锁定内部通道",
            style="Info.TLabel",
        ).grid(row=0, column=3, sticky="w", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="模型名称:").grid(row=1, column=0, sticky="e", pady=3, padx=5)
        self.entry_llm_model = ttk.Entry(llm_frame, width=40)
        self.entry_llm_model.insert(0, "deepseek-v3.2")
        self.entry_llm_model.grid(row=1, column=1, sticky="w", pady=3, padx=5)

        self.btn_test_llm = ttk.Button(
            llm_frame,
            text="测试大模型联通",
            style='Secondary.TButton',
            command=self.test_llm_connectivity,
        )
        self.btn_test_llm.grid(row=1, column=2, sticky="e", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="*填写 API Key 后，系统将使用大模型基于视频总时长和节奏参数，\n精准输出病症/产品插入点以及音效、BGM的情绪节点。",
                  style='Info.TLabel').grid(row=2, column=0, columnspan=4, sticky="w", pady=3, padx=5)

        # 2. 生成设置区
        settings_frame = ttk.LabelFrame(content, text="生成参数与频率控制", style='Card.TLabelframe')
        settings_frame.pack(fill="x", pady=2)
        settings_frame.columnconfigure(1, weight=1)
        settings_frame.columnconfigure(3, weight=1)

        ttk.Label(settings_frame, text="素材插入灵敏度:").grid(row=0, column=0, padx=5, pady=3)
        self.combo_sens = ttk.Combobox(settings_frame, values=["medium (推荐/中密)", "high (快节奏/高密)"], state="readonly", width=20)
        self.combo_sens.current(0)
        self.combo_sens.grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(settings_frame, text="输出画质:").grid(row=0, column=2, padx=5, pady=3)
        self.combo_res = ttk.Combobox(settings_frame, values=["1080p", "4k"], state="readonly", width=10)
        self.combo_res.current(0)
        self.combo_res.grid(row=0, column=3, padx=5, pady=3)
        
        # 频率控制滑块
        ttk.Label(settings_frame, text="广审素材频率 (次/任务, 0为无限制):").grid(row=1, column=0, padx=5, pady=3, sticky="e")
        self.scale_ad_freq = self._create_styled_scale(settings_frame, from_=0, to=10, length=150)
        self.scale_ad_freq.set(1)
        self.scale_ad_freq.grid(row=1, column=1, padx=5, pady=3, sticky="w")

        ttk.Label(settings_frame, text="贴图素材频率 (次/任务, 0为无限制):").grid(row=1, column=2, padx=5, pady=3, sticky="e")
        self.scale_sticker_freq = self._create_styled_scale(settings_frame, from_=0, to=10, length=150)
        self.scale_sticker_freq.set(0)
        self.scale_sticker_freq.grid(row=1, column=3, padx=5, pady=3, sticky="w")

        ttk.Label(settings_frame, text="中插素材频率 (次/任务, 0为无限制):").grid(row=2, column=0, padx=5, pady=3, sticky="e")
        self.scale_broll_freq = self._create_styled_scale(settings_frame, from_=0, to=10, length=150)
        self.scale_broll_freq.set(3)
        self.scale_broll_freq.grid(row=2, column=1, padx=5, pady=3, sticky="w")

        action_frame = ttk.Frame(frame)
        action_frame.pack(fill="x", pady=(8, 0))

        ttk.Separator(action_frame).pack(fill="x", pady=(0, 8))
        action_bar = ttk.Frame(action_frame)
        action_bar.pack(fill="x")

        ttk.Label(
            action_bar,
            text="底部操作区固定显示，若上方内容较多可直接滚动查看。",
            style="Info.TLabel",
        ).pack(side="left", padx=(0, 10))

        ttk.Button(
            action_bar,
            text="切换到任务看板",
            style='Secondary.TButton',
            command=lambda: self.notebook.select(self.tab_tasks),
        ).pack(side="right", padx=5)
        ttk.Button(
            action_bar,
            text="打开草稿目录预览",
            style='Secondary.TButton',
            command=self.open_draft_folder,
        ).pack(side="right", padx=5)

        self.btn_batch_add = ttk.Button(
            action_bar,
            text="批量生成全自动草稿",
            style='Primary.TButton',
            command=self.submit_batch_tasks,
        )
        self.btn_batch_add.pack(side="right", padx=5)

        self._bind_auto_gen_mousewheel(self.auto_gen_canvas)

    def build_settings_tab(self):
        frame = self.tab_settings
        
        # 左右两列布局
        left_col = ttk.Frame(frame)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        right_col = ttk.Frame(frame)
        right_col.pack(side="right", fill="both", expand=True, padx=(5, 0))

        # --- 左侧：配置面板 ---
        # 1. 模板与节奏控制
        rhythm_frame = ttk.LabelFrame(left_col, text="模板与节奏控制", style='Card.TLabelframe', padding=10)
        rhythm_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(rhythm_frame, text="全局成品模板:").grid(row=0, column=0, sticky="w", pady=5)
        self.combo_template = ttk.Combobox(rhythm_frame, values=["标准口播版", "强转化版", "审查稳妥版"], state="readonly", width=15)
        self.combo_template.grid(row=0, column=1, sticky="we", pady=5)
        self.combo_template.set("标准口播版")
        
        ttk.Label(rhythm_frame, text="镜头插入紧凑度:").grid(row=1, column=0, sticky="w", pady=5)
        self.combo_rhythm = ttk.Combobox(rhythm_frame, values=["常规呼吸感", "紧凑高频", "舒缓留白"], state="readonly", width=15)
        self.combo_rhythm.grid(row=1, column=1, sticky="we", pady=5)
        self.combo_rhythm.set("常规呼吸感")
        
        ttk.Label(rhythm_frame, text="最短口播保留时长 (秒):").grid(row=2, column=0, sticky="w", pady=5)
        self.scale_min_host = self._create_styled_scale(rhythm_frame, from_=0.5, to=3.0, resolution=0.1)
        self.scale_min_host.grid(row=2, column=1, sticky="we", pady=5)
        self.scale_min_host.set(1.2)

        density_frame = ttk.LabelFrame(left_col, text="中插密度与节奏模板", style='Card.TLabelframe', padding=10)
        density_frame.pack(fill="x", pady=10)
        density_frame.columnconfigure(1, weight=1)

        ttk.Label(density_frame, text="节奏模板:").grid(row=0, column=0, sticky="w", pady=5)
        self.combo_density_template = ttk.Combobox(
            density_frame,
            values=["快节奏", "中节奏", "慢节奏"],
            state="readonly",
            width=12,
        )
        self.combo_density_template.grid(row=0, column=1, sticky="we", pady=5)
        self.combo_density_template.set("中节奏")

        ttk.Label(density_frame, text="病症/产品比例:").grid(row=1, column=0, sticky="w", pady=5)
        self.combo_broll_ratio = ttk.Combobox(
            density_frame,
            values=["1:1", "2:1", "1:2"],
            state="readonly",
            width=12,
        )
        self.combo_broll_ratio.grid(row=1, column=1, sticky="we", pady=5)
        self.combo_broll_ratio.set("1:1")

        ttk.Label(density_frame, text="关键词触发延时(秒):").grid(row=2, column=0, sticky="w", pady=5)
        self.scale_trigger_delay = self._create_styled_scale(density_frame, from_=0.1, to=1.0, resolution=0.1)
        self.scale_trigger_delay.grid(row=2, column=1, sticky="we", pady=5)
        self.scale_trigger_delay.set(0.5)

        ttk.Label(density_frame, text="无关键词兜底秒数:").grid(row=3, column=0, sticky="w", pady=5)
        self.scale_idle_gap = self._create_styled_scale(density_frame, from_=3.0, to=8.0, resolution=0.5)
        self.scale_idle_gap.grid(row=3, column=1, sticky="we", pady=5)
        self.scale_idle_gap.set(5.0)

        ttk.Label(density_frame, text="检测窗口(秒):").grid(row=4, column=0, sticky="w", pady=5)
        self.scale_density_window = self._create_styled_scale(density_frame, from_=30, to=45, resolution=5)
        self.scale_density_window.grid(row=4, column=1, sticky="we", pady=5)
        self.scale_density_window.set(30)

        ttk.Label(density_frame, text="每窗口最少中插数:").grid(row=5, column=0, sticky="w", pady=5)
        self.scale_min_inserts = self._create_styled_scale(density_frame, from_=1, to=3, resolution=1)
        self.scale_min_inserts.grid(row=5, column=1, sticky="we", pady=5)
        self.scale_min_inserts.set(1)

        ttk.Label(density_frame, text="单段最短时长(秒):").grid(row=6, column=0, sticky="w", pady=5)
        self.scale_insert_min = self._create_styled_scale(density_frame, from_=3.0, to=8.0, resolution=0.5)
        self.scale_insert_min.grid(row=6, column=1, sticky="we", pady=5)
        self.scale_insert_min.set(3.0)

        ttk.Label(density_frame, text="单段最长时长(秒):").grid(row=7, column=0, sticky="w", pady=5)
        self.scale_insert_max = self._create_styled_scale(density_frame, from_=3.0, to=8.0, resolution=0.5)
        self.scale_insert_max.grid(row=7, column=1, sticky="we", pady=5)
        self.scale_insert_max.set(8.0)

        self.var_auto_fill_density = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            density_frame,
            text="低于指标时自动补齐中插建议并批量填充",
            variable=self.var_auto_fill_density,
            style='Card.TCheckbutton',
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(density_frame, text="语义匹配阈值:").grid(row=9, column=0, sticky="w", pady=5)
        self.scale_semantic_threshold = self._create_styled_scale(density_frame, from_=0.50, to=0.95, resolution=0.01)
        self.scale_semantic_threshold.grid(row=9, column=1, sticky="we", pady=5)
        self.scale_semantic_threshold.set(0.75)

        ttk.Label(density_frame, text="目标中插占比(%):").grid(row=10, column=0, sticky="w", pady=5)
        self.scale_target_broll_ratio = self._create_styled_scale(density_frame, from_=50, to=90, resolution=1)
        self.scale_target_broll_ratio.grid(row=10, column=1, sticky="we", pady=5)
        self.scale_target_broll_ratio.set(65)

        self.var_allow_prev_video_reuse = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            density_frame,
            text="允许24小时内复用上一支视频素材",
            variable=self.var_allow_prev_video_reuse,
            style='Card.TCheckbutton',
        ).grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))

        bgm_frame = ttk.LabelFrame(left_col, text="背景音乐对齐规则", style='Card.TLabelframe', padding=10)
        bgm_frame.pack(fill="x", pady=10)
        bgm_frame.columnconfigure(1, weight=1)

        ttk.Label(bgm_frame, text="交叉淡化(毫秒):").grid(row=0, column=0, sticky="w", pady=5)
        self.scale_bgm_crossfade = self._create_styled_scale(bgm_frame, from_=50, to=1000, resolution=50)
        self.scale_bgm_crossfade.grid(row=0, column=1, sticky="we", pady=5)
        self.scale_bgm_crossfade.set(200)

        ttk.Label(bgm_frame, text="目标响度(LUFS):").grid(row=1, column=0, sticky="w", pady=5)
        self.scale_bgm_lufs = self._create_styled_scale(bgm_frame, from_=-24, to=-12, resolution=1)
        self.scale_bgm_lufs.grid(row=1, column=1, sticky="we", pady=5)
        self.scale_bgm_lufs.set(-18)

        ttk.Label(bgm_frame, text="BGM 选取方式:").grid(row=2, column=0, sticky="w", pady=5)
        self.combo_bgm_pick_mode = ttk.Combobox(
            bgm_frame,
            values=["按文件名顺序", "随机洗牌"],
            state="readonly",
            width=14,
        )
        self.combo_bgm_pick_mode.grid(row=2, column=1, sticky="we", pady=5)
        self.combo_bgm_pick_mode.set("按文件名顺序")

        ttk.Label(bgm_frame, text="用户BGM文件夹:").grid(row=3, column=0, sticky="w", pady=5)
        bgm_dir_box = ttk.Frame(bgm_frame)
        bgm_dir_box.grid(row=3, column=1, sticky="we", pady=5)
        self.entry_user_bgm_dir = ttk.Entry(bgm_dir_box)
        self.entry_user_bgm_dir.pack(side="left", fill="x", expand=True)
        self.entry_user_bgm_dir.insert(0, runtime_path("my_bg_music"))
        ttk.Button(bgm_dir_box, text="选择文件夹...", style='Ghost.TButton',
                   command=lambda: self.browse_folder(self.entry_user_bgm_dir)).pack(side="left", padx=(6, 0))

        self.var_bgm_normalize = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bgm_frame,
            text="启用响度归一化",
            variable=self.var_bgm_normalize,
            style='Card.TCheckbutton',
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.var_bgm_phase_check = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bgm_frame,
            text="启用立体声相位检查",
            variable=self.var_bgm_phase_check,
            style='Card.TCheckbutton',
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- 右侧：配置摘要面板 ---
        self._build_summary_panel(right_col)
        self._start_summary_refresh()

    def _build_summary_panel(self, parent):
        card = ttk.LabelFrame(parent, text="配置摘要", style='Card.TLabelframe')
        card.pack(fill='both', expand=True)
        inner = ttk.Frame(card)
        inner.pack(fill='both', expand=True, padx=4, pady=4)
        self._summary_labels = {}
        sections = [
            ("模板设置", [
                ("成品模板",   lambda: self.combo_template.get()),
                ("插入节奏",   lambda: self.combo_rhythm.get()),
                ("最短口播",   lambda: f"{self.scale_min_host.get():.1f}s"),
                ("输出画质",   lambda: self.combo_res.get()),
                ("素材灵敏度", lambda: self.combo_sens.get().split()[0]),
            ]),
            ("中插密度", [
                ("节奏模板",       lambda: self.combo_density_template.get()),
                ("病症/产品比例",  lambda: self.combo_broll_ratio.get()),
                ("触发延时",       lambda: f"{self.scale_trigger_delay.get():.1f}s"),
                ("兜底间隔",       lambda: f"{self.scale_idle_gap.get():.1f}s"),
                ("检测窗口",       lambda: f"{int(self.scale_density_window.get())}s"),
                ("最少中插/窗口",  lambda: f"{int(self.scale_min_inserts.get())}个"),
                ("单段时长范围",   lambda: f"{self.scale_insert_min.get():.1f}-{self.scale_insert_max.get():.1f}s"),
                ("语义匹配阈值",   lambda: f"{self.scale_semantic_threshold.get():.2f}"),
                ("目标中插占比",   lambda: f"{int(self.scale_target_broll_ratio.get())}%"),
                ("自动补齐",       lambda: "是" if self.var_auto_fill_density.get() else "否"),
                ("素材复用(24h)",  lambda: "是" if self.var_allow_prev_video_reuse.get() else "否"),
            ]),
            ("BGM设置", [
                ("交叉淡化",   lambda: f"{int(self.scale_bgm_crossfade.get())}ms"),
                ("目标响度",   lambda: f"{int(self.scale_bgm_lufs.get())} LUFS"),
                ("选取方式",   lambda: self.combo_bgm_pick_mode.get()),
                ("响度归一化", lambda: "是" if self.var_bgm_normalize.get() else "否"),
                ("相位检查",   lambda: "是" if self.var_bgm_phase_check.get() else "否"),
            ]),
        ]
        row_idx = 0
        for section_title, entries in sections:
            ttk.Label(inner, text=section_title.upper(),
                      style='SummaryKey.TLabel').grid(
                row=row_idx, column=0, columnspan=2, sticky='w',
                pady=(10, 4), padx=4)
            row_idx += 1
            for key, getter in entries:
                ttk.Label(inner, text=key, style='SummaryKey.TLabel').grid(
                    row=row_idx, column=0, sticky='w', padx=(12, 8), pady=1)
                val = getter()
                lbl = ttk.Label(inner, text=val, style='SummaryVal.TLabel')
                lbl.grid(row=row_idx, column=1, sticky='w', pady=1)
                self._summary_labels[key] = (lbl, getter)
                row_idx += 1
            if section_title != sections[-1][0]:
                ttk.Separator(inner, orient='horizontal').grid(
                    row=row_idx, column=0, columnspan=2, sticky='ew',
                    pady=(2, 0), padx=4)
                row_idx += 1

    def _start_summary_refresh(self):
        self._refresh_summary_panel()
        self.after(800, self._start_summary_refresh)

    def _refresh_summary_panel(self):
        if not hasattr(self, '_summary_labels'):
            return
        for key, (lbl, getter) in self._summary_labels.items():
            try:
                new_val = getter()
                if lbl.cget('text') != new_val:
                    lbl.configure(text=new_val)
            except Exception:
                pass

    def _apply_tree_row_tag(self, task_id, info):
        status = info.get('status', '')
        tag_map = {
            STATUS_RUNNING: 'running',
            STATUS_READY:   'ready',
            STATUS_FAILED:  'failed',
            STATUS_ERROR:   'failed',
            STATUS_QUEUED:  'queued',
        }
        tag_name = tag_map.get(status)
        if tag_name and self.tree.exists(task_id):
            expected = (tag_name,)
            if self.tree.item(task_id, 'tags') != expected:
                self.tree.item(task_id, tags=expected)

    def _update_maintenance_label(self):
        text = (f"维护告警: {self.maintenance_warning_count} | "
                f"{self.latest_maintenance_warning}")
        style_name = 'Warning.TLabel' if self.maintenance_warning_count > 0 else 'Info.TLabel'
        if (self.lbl_maintenance_status.cget('text') != text or
                self.lbl_maintenance_status.cget('style') != style_name):
            self.lbl_maintenance_status.configure(text=text, style=style_name)

    def build_tasks_tab(self):
        frame = self.tab_tasks
        ttk.Label(frame, text="任务队列看板：全自动批处理 -> 匹配素材 -> 轨道添加 -> 输出草稿 -> 异常重试", style="Info.TLabel").pack(anchor="w", pady=(0, 5))

        self.lbl_queue_status = ttk.Label(frame, text="当前无任务", style="Status.TLabel")
        self.lbl_queue_status.pack(anchor="w", pady=5)

        self.lbl_maintenance_status = ttk.Label(
            frame,
            text="维护告警: 0 | 当前无维护告警",
            style="Info.TLabel",
        )
        self.lbl_maintenance_status.pack(anchor="w", pady=(0, 5))

        # 任务列表表格
        columns = ("id", "time", "status", "detail")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        self.tree.heading("id", text="任务 ID")
        self.tree.column("id", width=80, anchor="center")
        self.tree.heading("time", text="提交时间")
        self.tree.column("time", width=150, anchor="center")
        self.tree.heading("status", text="当前状态")
        self.tree.column("status", width=120, anchor="center")
        self.tree.heading("detail", text="日志/详情")
        self.tree.column("detail", width=450, anchor="w")
        self.tree.pack(fill="both", expand=True)

        self.tree.tag_configure('running', background=self._colors['accent_bg'])
        self.tree.tag_configure('ready',   background=self._colors['green_bg'])
        self.tree.tag_configure('failed',  background=self._colors['red_bg'])
        self.tree.tag_configure('queued',  background=self._colors['fill_sec'])

        # 底部控制区
        control_frame = ttk.Frame(frame)
        control_frame.pack(fill="x", pady=10)

        self.btn_retry_failed = ttk.Button(control_frame, text="重试失败任务",
            style='Primary.TButton', command=self.retry_failed_tasks, state="disabled")
        self.btn_retry_failed.pack(side="left", padx=5)

        self.btn_export_json = ttk.Button(control_frame, text="导出执行报告",
            style='Secondary.TButton', command=self.export_report_json, state="disabled")
        self.btn_export_json.pack(side="left", padx=5)

        self.btn_open_internal_log = ttk.Button(control_frame, text="打开维护日志",
            style='Secondary.TButton', command=self.open_internal_log)
        self.btn_open_internal_log.pack(side="left", padx=5)

    def browse_file(self, entry_widget):
        filename = filedialog.askopenfilename(
            filetypes=[
                ("文本/词库", "*.txt *.csv *.json"),
                ("所有文件", "*.*"),
            ]
        )
        if filename:
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, filename)

    def browse_folder(self, entry_widget):
        folder = filedialog.askdirectory()
        if folder:
            entry_widget.delete(0, tk.END)
            entry_widget.insert(0, folder)

    def validate_paths(self):
        req_keys = ["speech", "product", "symptom"]
        for k in req_keys:
            p = self.entries[k].get().strip()
            if not p or not os.path.exists(p):
                return False, f"核心素材目录不存在: {k} (请检查口播、产品、病症路径)"
        return True, "验证通过"

    def _build_llm_env(self, env):
        env["LLM_API_KEY"] = self.entry_llm_key.get().strip()
        env["LLM_BASE_URL"] = LOCKED_LLM_BASE_URL
        env["LLM_MODEL"] = self.entry_llm_model.get().strip()
        return env

    def _apply_advanced_settings_env(self, env):
        if hasattr(self, "combo_template"):
            env["OTC_TEMPLATE_MODE"] = self.combo_template.get().strip()
        if hasattr(self, "combo_rhythm"):
            env["OTC_RHYTHM_MODE"] = self.combo_rhythm.get().strip()
        if hasattr(self, "scale_min_host"):
            env["OTC_MIN_HOST_DURATION"] = str(self.scale_min_host.get())
        if hasattr(self, "combo_density_template"):
            env["OTC_DENSITY_TEMPLATE"] = self.combo_density_template.get().strip()
        if hasattr(self, "combo_broll_ratio"):
            env["OTC_BROLL_RATIO"] = self.combo_broll_ratio.get().strip()
        if hasattr(self, "scale_trigger_delay"):
            env["OTC_KEYWORD_TRIGGER_DELAY"] = str(self.scale_trigger_delay.get())
        if hasattr(self, "scale_idle_gap"):
            env["OTC_KEYWORD_IDLE_GAP_SECONDS"] = str(self.scale_idle_gap.get())
        if hasattr(self, "scale_density_window"):
            env["OTC_DENSITY_WINDOW_SECONDS"] = str(int(self.scale_density_window.get()))
        if hasattr(self, "scale_min_inserts"):
            env["OTC_MIN_INSERTS_PER_WINDOW"] = str(int(self.scale_min_inserts.get()))
        if hasattr(self, "scale_insert_min"):
            env["OTC_INSERT_MIN_DURATION"] = str(self.scale_insert_min.get())
        if hasattr(self, "scale_insert_max"):
            env["OTC_INSERT_MAX_DURATION"] = str(self.scale_insert_max.get())
        if hasattr(self, "var_auto_fill_density"):
            env["OTC_AUTO_FILL_DENSITY"] = "1" if self.var_auto_fill_density.get() else "0"
        if hasattr(self, "scale_semantic_threshold"):
            env["OTC_SEMANTIC_MATCH_THRESHOLD"] = f"{float(self.scale_semantic_threshold.get()):.2f}"
        if hasattr(self, "scale_target_broll_ratio"):
            env["OTC_TARGET_BROLL_RATIO"] = f"{float(self.scale_target_broll_ratio.get()) / 100.0:.2f}"
        if hasattr(self, "var_allow_prev_video_reuse"):
            env["OTC_ALLOW_PREVIOUS_VIDEO_REUSE_WITHIN_24H"] = "1" if self.var_allow_prev_video_reuse.get() else "0"
        if hasattr(self, "scale_bgm_crossfade"):
            env["OTC_BGM_CROSSFADE_MS"] = str(int(self.scale_bgm_crossfade.get()))
        if hasattr(self, "scale_bgm_lufs"):
            env["OTC_BGM_TARGET_LUFS"] = str(int(self.scale_bgm_lufs.get()))
        if hasattr(self, "combo_bgm_pick_mode"):
            env["OTC_BGM_PICK_MODE"] = self.combo_bgm_pick_mode.get().strip()
        if hasattr(self, "entry_user_bgm_dir"):
            env["OTC_USER_BGM_DIR"] = self.entry_user_bgm_dir.get().strip()
        if hasattr(self, "var_bgm_normalize"):
            env["OTC_BGM_NORMALIZE"] = "1" if self.var_bgm_normalize.get() else "0"
        if hasattr(self, "var_bgm_phase_check"):
            env["OTC_BGM_PHASE_CHECK"] = "1" if self.var_bgm_phase_check.get() else "0"
        return env

    def _set_button_state(self, button, state):
        cache_key = str(button)
        if self._last_button_states.get(cache_key) != state:
            button.config(state=state)
            self._last_button_states[cache_key] = state

    def _run_worker_preflight(self):
        result = run_hidden(
            get_worker_command(["--preflight"]),
            env=build_runtime_env(os.environ.copy()),
            cwd=runtime_path(),
            capture_output=True,
            text=False,
        )
        stdout = repair_mojibake_text(decode_process_output(result.stdout))
        stderr = repair_mojibake_text(decode_process_output(result.stderr))
        if result.returncode not in (0, 1):
            raise RuntimeError(stderr or stdout or "环境自检命令执行失败")

        payload = stdout.strip()
        if not payload:
            raise RuntimeError(stderr or "环境自检没有返回结果")

        return json.loads(payload)

    def _ensure_runtime_ready(self):
        report = self._run_worker_preflight()
        fatal_errors = report.get("fatal_errors", [])
        warnings = report.get("warnings", [])

        if fatal_errors:
            detail = "\n".join(f"- {item}" for item in fatal_errors)
            messagebox.showerror("运行环境异常", f"当前环境无法正常生成草稿：\n{detail}")
            return False

        if warnings:
            detail = "\n".join(f"- {item}" for item in warnings)
            continue_run = messagebox.askyesno(
                "兼容性提示",
                f"检测到以下风险：\n{detail}\n\n是否仍然继续生成？",
            )
            if not continue_run:
                return False

        return True

    def test_llm_connectivity(self):
        api_key = self.entry_llm_key.get().strip()
        model = self.entry_llm_model.get().strip() or "deepseek-v3.2"
        if not api_key:
            messagebox.showwarning("提示", "请先填写 API Key。")
            return

        self._save_ui_settings()
        self.btn_test_llm.config(state="disabled")
        self.update_idletasks()
        try:
            import llm_clip_matcher

            ok, message = llm_clip_matcher.test_llm_connectivity(
                api_key=api_key,
                model=model,
                base_url=LOCKED_LLM_BASE_URL,
            )
            if ok:
                messagebox.showinfo("联通成功", message)
            else:
                messagebox.showerror("联通失败", message)
        except Exception as exc:
            messagebox.showerror("联通失败", f"联通测试异常: {exc}")
        finally:
            self.btn_test_llm.config(state="normal")

    def submit_batch_tasks(self):
        is_valid, msg = self.validate_paths()
        if not is_valid:
            messagebox.showerror("路径错误", msg)
            return
        self._save_ui_settings()
        if not self._ensure_runtime_ready():
            return

        speech_dir = self.entries["speech"].get().strip()
        speech_video_paths, skipped_audio_paths = scan_video_file_paths(
            speech_dir,
            recursive=False,
            skip_generated_artifacts=True,
        )
        speech_videos = [os.path.basename(path) for path in speech_video_paths]

        if not speech_videos:
            if skipped_audio_paths:
                skipped_names = "、".join(os.path.basename(path) for path in skipped_audio_paths[:5])
                messagebox.showerror("错误", f"口播目录下仅检测到音频文件，已全部跳过：{skipped_names}")
            else:
                messagebox.showerror("错误", "口播目录下没有找到支持的视频文件！")
            return

        for skipped_path in skipped_audio_paths:
            print(f"[SKIP] 口播音频文件已跳过，不生成草稿: {os.path.basename(skipped_path)}")

        # 提取参数
        env = os.environ.copy()
        env["OTC_SPEECH_DIR"] = speech_dir
        env["OTC_PRODUCT_DIR"] = self.entries["product"].get().strip()
        env["OTC_SYMPTOM_DIR"] = self.entries["symptom"].get().strip()
        
        audio_dir = self.entries["audio"].get().strip()
        if audio_dir and os.path.exists(audio_dir):
            env["OTC_AUDIO_DIR"] = audio_dir
            
        bgm_dir = self.entries["bgm"].get().strip()
        if bgm_dir and os.path.exists(bgm_dir):
            env["OTC_USER_BGM_DIR"] = bgm_dir

        ad_review_dir = self.entries["ad_review"].get().strip()
        if ad_review_dir and os.path.exists(ad_review_dir):
            env["OTC_AD_REVIEW_DIR"] = ad_review_dir
            
        sticker_dir = self.entries["sticker"].get().strip()
        if sticker_dir and os.path.exists(sticker_dir):
            env["OTC_STICKER_DIR"] = sticker_dir

        sensitivity = self.combo_sens.get().split()[0]
        env["OTC_AD_FREQ"] = str(self.scale_ad_freq.get())
        env["OTC_STICKER_FREQ"] = str(self.scale_sticker_freq.get())
        env["OTC_BROLL_FREQ"] = str(self.scale_broll_freq.get())
        
        env = self._apply_advanced_settings_env(env)
        env = self._build_llm_env(env)
        
        if len(speech_videos) > TASK_QUEUE_CAPACITY:
            messagebox.showerror("错误", f"当前批量任务数超过队列容量上限 {TASK_QUEUE_CAPACITY}，请分批提交。")
            return

        self._set_button_state(self.btn_batch_add, "disabled")

        for video_file in speech_videos:
            self.task_count += 1
            task_id = f"Task-{self.task_count:03d}"
            submit_time = time.strftime("%H:%M:%S")
            
            self.tree.insert("", "end", iid=task_id, values=(task_id, submit_time, STATUS_QUEUED, f"准备处理: {video_file}"), tags=('queued',))
            self.executor.submit(self.execute_task, task_id, env, sensitivity, video_file)
            
        self.notebook.select(self.tab_tasks)
        messagebox.showinfo("已提交", f"已批量提交 {len(speech_videos)} 个视频任务到全自动队列！")

    def _snapshot_running_tasks(self):
        with self._tasks_lock:
            return dict(self.running_tasks)

    def execute_task(self, task_id, env, sensitivity, video_file, retry_count=0):
        max_retries = BATCH_RETRY_LIMIT
        with self._tasks_lock:
            self.running_tasks[task_id] = {
                "status": STATUS_RUNNING,
                "log": f"正在处理: {video_file} (重试: {retry_count})",
                "file": video_file,
            }

        try:
            task_env = build_runtime_env(env)
            with self.subprocess_slots:
                result = run_hidden(
                    get_worker_command(["--sensitivity", sensitivity, "--video", video_file]),
                    env=task_env,
                    cwd=runtime_path(),
                    capture_output=True,
                    text=False,
                )
            result.stdout = decode_process_output(result.stdout)
            result.stderr = decode_process_output(result.stderr)
            log_path = self._write_task_log(task_id, video_file, result)

            if result.returncode == 0:
                success_summary = self._summarize_process_output(result, success=True)
                with self._tasks_lock:
                    self.running_tasks[task_id] = {
                        "status": STATUS_READY,
                        "log": f"{success_summary} | 日志: {log_path}",
                        "file": video_file,
                        "result": "SUCCESS",
                        "log_path": log_path,
                    }
            else:
                if retry_count < max_retries:
                    self.executor.submit(
                        self.execute_task, task_id, env, sensitivity,
                        video_file, retry_count + 1,
                    )
                else:
                    err_msg = self._summarize_process_output(result, success=False)
                    with self._tasks_lock:
                        self.running_tasks[task_id] = {
                            "status": STATUS_FAILED,
                            "log": f"{err_msg} | 日志: {log_path}",
                            "file": video_file,
                            "result": "FAILED",
                            "log_path": log_path,
                        }
        except Exception as e:
            if retry_count < max_retries:
                self.executor.submit(
                    self.execute_task, task_id, env, sensitivity,
                    video_file, retry_count + 1,
                )
            else:
                with self._tasks_lock:
                    self.running_tasks[task_id] = {
                        "status": STATUS_ERROR,
                        "log": str(e),
                        "file": video_file,
                        "result": "ERROR",
                    }

    def _write_task_log(self, task_id, video_file, result):
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", os.path.splitext(video_file)[0])
        log_path = os.path.join(self.task_logs_dir, f"{task_id}_{safe_name}.log")
        stdout = result.stdout if isinstance(result.stdout, str) else str(result.stdout or "")
        stderr = result.stderr if isinstance(result.stderr, str) else str(result.stderr or "")
        stdout = repair_mojibake_text(stdout)
        stderr = repair_mojibake_text(stderr)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"task_id={task_id}\n")
            f.write(f"video_file={video_file}\n")
            f.write(f"returncode={result.returncode}\n")
            f.write("\n[STDOUT]\n")
            f.write(stdout)
            f.write("\n[STDERR]\n")
            f.write(stderr)
        return log_path

    def _summarize_process_output(self, result, success=False):
        combined_parts = []
        stdout = result.stdout if isinstance(result.stdout, str) else str(result.stdout or "")
        stderr = result.stderr if isinstance(result.stderr, str) else str(result.stderr or "")
        stdout = repair_mojibake_text(stdout)
        stderr = repair_mojibake_text(stderr)
        if stdout:
            combined_parts.append(stdout)
        if stderr:
            combined_parts.append(stderr)
        combined = "\n".join(combined_parts).strip()
        if not combined:
            return "成功" if success else "未知错误（无输出）"

        lines = [line.strip() for line in combined.splitlines() if line.strip()]
        if success:
            for line in reversed(lines):
                if "[成功]" in line or "成功" in line:
                    return line[-200:]
            return lines[-1][-200:]

        preferred_keywords = (
            "草稿目录不可写",
            "当前未使用系统剪映草稿目录",
            "未找到 ffmpeg",
            "未找到 ffprobe",
            "检测到剪映版本",
            "核心文件未落盘",
            "ImportError",
            "ModuleNotFoundError",
            "PermissionError",
            "Permission denied",
            "Traceback",
            "RuntimeError",
            "Error",
            "错误",
        )
        skip_lines = {
            "[失败] 视频创建失败，请检查错误信息",
        }
        for line in reversed(lines):
            if line in skip_lines:
                continue
            if any(keyword in line for keyword in preferred_keywords):
                return line[-240:]

        keywords = ("[失败]", "Traceback", "RuntimeError", "Error", "错误")
        for line in reversed(lines):
            if any(keyword in line for keyword in keywords):
                return line[-240:]
        return lines[-1][-240:]


    def update_task_status_ui(self):
        # 更新队列统计文本（仅在文本变更时刷新，避免频繁重绘导致闪烁）
        tasks = self._snapshot_running_tasks()
        active = sum(1 for info in tasks.values() if info["status"] == STATUS_RUNNING)
        waiting = sum(1 for info in tasks.values() if info["status"] == STATUS_QUEUED)
        failed = sum(1 for info in tasks.values() if info["status"] in [STATUS_FAILED, STATUS_ERROR])

        mode_text = "【全自动批量模式】"
        queue_status_text = f"{mode_text} 正在处理: {active} | 等待中: {waiting} | 失败: {failed} | 总计提交: {self.task_count}"
        if queue_status_text != self._last_queue_status_text:
            self.lbl_queue_status.config(text=queue_status_text)
            self._last_queue_status_text = queue_status_text

        self._update_maintenance_label()

        # 定时刷新任务列表的 UI
        for task_id, info in tasks.items():
            if self.tree.exists(task_id):
                item = self.tree.item(task_id)
                vals = list(item["values"])
                if vals[2] != info["status"] or vals[3] != info["log"]:
                    vals[2] = info["status"]
                    vals[3] = info["log"]
                    self.tree.item(task_id, values=vals)
                self._apply_tree_row_tag(task_id, info)
                    
        # 空闲时应允许继续提交新任务；只有执行中或排队中才禁用提交按钮。
        if active == 0 and waiting == 0:
            self._set_button_state(self.btn_batch_add, "normal")
            if self.task_count > 0:
                if not getattr(self, '_cleanup_done', False):
                    self.cleanup_empty_drafts()
                    self._cleanup_done = True

                self._set_button_state(self.btn_export_json, "normal")
                if failed > 0:
                    self._set_button_state(self.btn_retry_failed, "normal")
                else:
                    self._set_button_state(self.btn_retry_failed, "disabled")
            else:
                self._cleanup_done = False
                self._set_button_state(self.btn_export_json, "disabled")
                self._set_button_state(self.btn_retry_failed, "disabled")
        else:
            self._cleanup_done = False
            self._set_button_state(self.btn_export_json, "disabled")
            self._set_button_state(self.btn_retry_failed, "disabled")
            self._set_button_state(self.btn_batch_add, "disabled")

        self.after(TASK_UI_REFRESH_MS, self.update_task_status_ui)

    def cleanup_empty_drafts(self):
        """清理因为中途失败或中止残留的空草稿"""
        import json
        import shutil
        try:
            draft_path = get_draft_root()
            if not os.path.exists(draft_path):
                return
                
            cleanup_count = 0
            for folder_name in os.listdir(draft_path):
                if folder_name.startswith("OTC推广_"):
                    folder_path = os.path.join(draft_path, folder_name)
                    if os.path.isdir(folder_path):
                        meta_file = os.path.join(folder_path, "draft_meta_info.json")
                        content_file = os.path.join(folder_path, "draft_content.json")
                        
                        is_empty = False
                        if not os.path.exists(content_file):
                            is_empty = True
                        elif os.path.exists(meta_file):
                            try:
                                with open(meta_file, 'r', encoding='utf-8') as f:
                                    meta = json.load(f)
                                    # 注意这里放宽条件：如果 tm_duration 是 0，且不包含素材，才算是空草稿。
                                    # 否则可能会误删刚建好还没刷新缓存的草稿
                                    if meta.get("tm_duration", 0) == 0:
                                        if not os.path.exists(content_file) or os.path.getsize(content_file) < 1000:
                                            # 再给一个宽限期：如果是最近 2 分钟内新建的文件夹，绝对不删，保护正在生成的草稿
                                            import time
                                            folder_mtime = os.path.getmtime(folder_path)
                                            if time.time() - folder_mtime > 120:
                                                is_empty = True
                            except Exception as exc:
                                self._log_nonfatal_issue(
                                    "cleanup_empty_drafts.read_meta",
                                    f"读取草稿元数据失败，已跳过该目录: {folder_name}",
                                    exc=exc,
                                )
                                
                        if is_empty:
                            try:
                                shutil.rmtree(folder_path)
                                cleanup_count += 1
                            except Exception as exc:
                                self._log_nonfatal_issue(
                                    "cleanup_empty_drafts.delete",
                                    f"删除空草稿目录失败: {folder_name}",
                                    exc=exc,
                                )
            if cleanup_count > 0:
                print(f"自动清理了 {cleanup_count} 个空草稿残留。")
            self.repair_draft_registry()
        except Exception as exc:
            self._log_nonfatal_issue(
                "cleanup_empty_drafts",
                "启动时清理空草稿流程执行失败，已跳过本轮清理。",
                exc=exc,
            )

    def repair_draft_registry(self):
        try:
            report_path = os.path.join(self.output_dir, "draft_registry_health.json")
            report = reconcile_root_meta(
                draft_root=get_draft_root(),
                restore_project_drafts=False,
                project_prefixes=("OTC推广_",),
                report_path=report_path,
                lock_path=os.path.join(self.output_dir, ".root_meta_info.lock"),
            )
            restored = len(report.get("restored_from_recycle", []))
            invalid = len(report.get("invalid_drafts", []))
            if restored or invalid:
                print(f"草稿索引修复完成: 恢复 {restored} 个，发现无效目录 {invalid} 个。")
        except Exception as exc:
            print(f"草稿索引修复失败: {exc}")

    def retry_failed_tasks(self):
        self._save_ui_settings()
        if not self._ensure_runtime_ready():
            return

        env = os.environ.copy()
        env["OTC_SPEECH_DIR"] = self.entries["speech"].get().strip()
        env["OTC_PRODUCT_DIR"] = self.entries["product"].get().strip()
        env["OTC_SYMPTOM_DIR"] = self.entries["symptom"].get().strip()
        audio_dir = self.entries["audio"].get().strip()
        if audio_dir and os.path.exists(audio_dir): env["OTC_AUDIO_DIR"] = audio_dir
        bgm_dir = self.entries["bgm"].get().strip()
        if bgm_dir and os.path.exists(bgm_dir): env["OTC_USER_BGM_DIR"] = bgm_dir
        ad_review_dir = self.entries["ad_review"].get().strip()
        if ad_review_dir and os.path.exists(ad_review_dir): env["OTC_AD_REVIEW_DIR"] = ad_review_dir
        sticker_dir = self.entries["sticker"].get().strip()
        if sticker_dir and os.path.exists(sticker_dir): env["OTC_STICKER_DIR"] = sticker_dir

        env = self._apply_advanced_settings_env(env)
        env = self._build_llm_env(env)

        sensitivity = self.combo_sens.get().split()[0]
        
        retry_count = 0
        tasks = self._snapshot_running_tasks()
        for task_id, info in tasks.items():
            if info["status"] in [STATUS_FAILED, STATUS_ERROR]:
                self.executor.submit(self.execute_task, task_id, env, sensitivity, info["file"], 0)
                retry_count += 1
                
        if retry_count > 0:
            messagebox.showinfo("重试", f"已重新提交 {retry_count} 个失败任务！")
            self._set_button_state(self.btn_retry_failed, "disabled")
            
    def export_report_json(self):
        import json
        report = []
        tasks = self._snapshot_running_tasks()
        for task_id, info in tasks.items():
            report.append({
                "draft_id": task_id,
                "file_path": info.get("file", ""),
                "status": info.get("result", "UNKNOWN"),
                "failure_reason": info.get("log", "") if info.get("result") != "SUCCESS" else ""
            })
        
        output_path = runtime_path("batch_generation_report.json")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("导出成功", f"执行报告已保存至:\n{output_path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def open_draft_folder(self):
        draft_path = get_draft_root()
        if os.path.exists(draft_path):
            os.startfile(draft_path)
        else:
            messagebox.showwarning("警告", f"未找到剪映草稿目录: {draft_path}")

    def open_internal_log(self):
        if os.path.exists(self.internal_log_path):
            os.startfile(self.internal_log_path)
        else:
            messagebox.showinfo("提示", f"当前还没有维护日志文件：\n{self.internal_log_path}")

if __name__ == "__main__":
    app = YunFengEditorUI()
    app.mainloop()
