import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import os
import sys
import json
import glob
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import queue
import re
import traceback
from PIL import Image, ImageTk
from app_paths import build_runtime_env, configure_current_process, get_worker_command, runtime_path
from batch_runtime_config import (
    get_batch_retry_limit,
    get_runtime_limits,
)
from draft_registry import (
    DRAFT_MODE_AUTO,
    DRAFT_MODE_PORTABLE,
    DRAFT_MODE_SYSTEM,
    delete_drafts_permanently,
    get_official_draft_root,
    get_draft_root,
    get_draft_root_info,
    reconcile_root_meta,
    sync_managed_drafts,
)
from media_file_rules import scan_video_file_paths
from subprocess_windows import popen_hidden, run_hidden
from text_output_utils import decode_process_output, repair_mojibake_text
from llm_clip_matcher import normalize_model_name
from secure_storage import load_secret, save_secret
from shot_understanding import analyze_directory as analyze_shot_directory
from shot_understanding import ensure_shot_output_root, load_latest_report

DEFAULT_LLM_BASE_URL = "https://api.siliconflow.cn"
DEFAULT_LLM_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_VLM_MODEL = "Qwen/Qwen3.6-35B-A3B"
STATUS_QUEUED = "排队中"
STATUS_RUNNING = "生成中"
STATUS_READY = "待发布"
STATUS_FAILED = "失败"
STATUS_ERROR = "异常"
TASK_UI_REFRESH_MS = 2500
UI_SETTINGS_PATH = runtime_path("output", "ui_settings.json")
DRAFT_MODE_LABELS = {
    DRAFT_MODE_AUTO: "自动优先系统目录",
    DRAFT_MODE_SYSTEM: "系统剪映目录",
    DRAFT_MODE_PORTABLE: "便携目录(排障)",
}
SHOT_SOURCE_LABELS = {
    "product": "产品素材",
    "symptom": "病症素材",
    "speech": "口播素材",
    "custom": "自定义目录",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class YunFengEditorUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("云锋剪辑 - 智能视频生成系统 (专业版)")
        self._configure_window()
        self.configure(padx=10, pady=10)

        self.output_dir = runtime_path("output")
        self.task_logs_dir = os.path.join(self.output_dir, "task_logs")
        self.internal_log_path = os.path.join(self.output_dir, "internal_maintenance.log")
        self.task_report_path = os.path.join(self.output_dir, "ui_task_report.json")
        self.ui_settings_path = UI_SETTINGS_PATH
        self._llm_key_secret_name = "llm_api_key"
        os.makedirs(self.task_logs_dir, exist_ok=True)

        # 提前初始化 uiautomation 缓存，防止并发生成草稿时 comtypes 缓存冲突导致 Permission Denied
        try:
            import uiautomation
        except ImportError:
            print("[WARN] 未安装 uiautomation，导出等依赖桌面自动化的能力可能受限。")

        # 任务并发池统一按批量并发配置运行
        runtime_limits = get_runtime_limits()
        self.batch_concurrency = runtime_limits["batch_concurrency"]
        self.batch_retry_limit = runtime_limits["batch_retry_limit"]
        self.task_queue_capacity = runtime_limits["task_queue_capacity"]
        self.executor = ThreadPoolExecutor(max_workers=self.batch_concurrency)
        self.task_queue = queue.Queue(maxsize=self.task_queue_capacity)
        self.subprocess_slots = threading.BoundedSemaphore(runtime_limits["subprocess_slot_limit"])
        self.task_count = 0
        self.running_tasks = {}
        self._tasks_lock = threading.Lock()
        self._last_queue_status_text = ""
        self._last_maintenance_status_text = ""
        self._last_button_states = {}
        self._shot_tree_rows = {}
        self._shot_preview_image = None
        self._latest_shot_report = None
        self._task_hit_rows = {}
        self._task_hit_all_rows = []
        self._task_preview_image = None
        self._last_task_runtime_log_text = None
        self._last_selected_task_id = ""
        self.var_task_hits_only_shot = tk.BooleanVar(value=False)
        self.var_task_hits_need_review = tk.BooleanVar(value=False)
        self._analysis_summary_cache = {}

        self.create_widgets()
        self._load_task_report_snapshot()
        self._load_ui_settings()
        self._refresh_runtime_target_labels()
        self._startup_draft_health_report = self.inspect_draft_registry()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(TASK_UI_REFRESH_MS, self.update_task_status_ui)
        self.update_idletasks()
        self.deiconify()

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
        self.tab_analysis = ttk.Frame(self.notebook, padding=(12, 8, 12, 8))

        self.notebook.add(self.tab_auto_gen, text="一键智能合成")
        self.notebook.add(self.tab_settings, text="成品模板设置")
        self.notebook.add(self.tab_tasks, text="任务队列看板")
        self.notebook.add(self.tab_analysis, text="业务分析面板")

        self.build_auto_gen_tab()
        self.build_settings_tab()
        self.build_tasks_tab()
        self.build_analysis_tab()

    def _collect_ui_settings(self):
        data = {
            "entries": {},
            "combo_values": {},
            "scale_values": {},
        }

        if hasattr(self, "entries"):
            for key, entry in self.entries.items():
                data["entries"][key] = entry.get().strip()

        if hasattr(self, "entry_llm_model"):
            data["entries"]["llm_model"] = self.entry_llm_model.get().strip()
        if hasattr(self, "entry_llm_base_url"):
            data["entries"]["llm_base_url"] = self.entry_llm_base_url.get().strip()
        if hasattr(self, "entry_shot_custom_dir"):
            data["entries"]["shot_custom_dir"] = self.entry_shot_custom_dir.get().strip()
        if hasattr(self, "entry_shot_vlm_model"):
            data["entries"]["shot_vlm_model"] = self.entry_shot_vlm_model.get().strip()
        if hasattr(self, "entry_shot_max_videos"):
            data["entries"]["shot_max_videos"] = self.entry_shot_max_videos.get().strip()
        if hasattr(self, "entry_shot_min_duration"):
            data["entries"]["shot_min_duration"] = self.entry_shot_min_duration.get().strip()
        if hasattr(self, "entry_shot_diff_threshold"):
            data["entries"]["shot_diff_threshold"] = self.entry_shot_diff_threshold.get().strip()
        if hasattr(self, "entry_main_shot_vlm_model"):
            data["entries"]["main_shot_vlm_model"] = self.entry_main_shot_vlm_model.get().strip()
        if hasattr(self, "entry_main_shot_max_videos"):
            data["entries"]["main_shot_max_videos"] = self.entry_main_shot_max_videos.get().strip()
        if hasattr(self, "entry_main_shot_top_n"):
            data["entries"]["main_shot_top_n"] = self.entry_main_shot_top_n.get().strip()

        if hasattr(self, "combo_sens"):
            data["combo_values"]["sensitivity"] = self.combo_sens.get().strip()
        if hasattr(self, "combo_res"):
            data["combo_values"]["resolution"] = self.combo_res.get().strip()
        if hasattr(self, "combo_template"):
            data["combo_values"]["template_mode"] = self.combo_template.get().strip()
        if hasattr(self, "combo_rhythm"):
            data["combo_values"]["rhythm_mode"] = self.combo_rhythm.get().strip()
        if hasattr(self, "combo_draft_mode"):
            data["combo_values"]["draft_mode"] = self.combo_draft_mode.get().strip()
        if hasattr(self, "combo_shot_source"):
            data["combo_values"]["shot_source_kind"] = self.combo_shot_source.get().strip()

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
        if hasattr(self, "var_shot_use_vlm"):
            data["checkbox_values"]["shot_use_vlm"] = self.var_shot_use_vlm.get()
        if hasattr(self, "var_main_shot_recall_enabled"):
            data["checkbox_values"]["main_shot_recall_enabled"] = self.var_main_shot_recall_enabled.get()
        if hasattr(self, "var_main_shot_use_vlm"):
            data["checkbox_values"]["main_shot_use_vlm"] = self.var_main_shot_use_vlm.get()
        if hasattr(self, "var_main_shot_force_refresh"):
            data["checkbox_values"]["main_shot_force_refresh"] = self.var_main_shot_force_refresh.get()
        if hasattr(self, "var_main_strict_semantic"):
            data["checkbox_values"]["main_strict_semantic"] = self.var_main_strict_semantic.get()

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

    def _load_saved_llm_api_key(self):
        try:
            return load_secret(self._llm_key_secret_name)
        except Exception as exc:
            self._log_nonfatal_issue("ui_settings", f"API Key 安全存储读取失败: {exc}", exc=exc)
            return ""

    def _save_saved_llm_api_key(self):
        if not hasattr(self, "entry_llm_key"):
            return
        try:
            save_secret(self._llm_key_secret_name, self.entry_llm_key.get().strip())
        except Exception as exc:
            self._log_nonfatal_issue("ui_settings", f"API Key 安全存储写入失败: {exc}", exc=exc)

    def _save_ui_settings(self):
        try:
            os.makedirs(os.path.dirname(self.ui_settings_path), exist_ok=True)
            with open(self.ui_settings_path, "w", encoding="utf-8") as f:
                json.dump(self._collect_ui_settings(), f, ensure_ascii=False, indent=2)
            self._save_saved_llm_api_key()
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

        legacy_llm_api_key = str(data.get("entries", {}).get("llm_api_key", "")).strip()
        llm_api_key = self._load_saved_llm_api_key()
        migrated_legacy_key = False
        if not llm_api_key and legacy_llm_api_key:
            llm_api_key = legacy_llm_api_key
            try:
                save_secret(self._llm_key_secret_name, legacy_llm_api_key)
                migrated_legacy_key = True
            except Exception as exc:
                self._log_nonfatal_issue("ui_settings", f"旧版 API Key 迁移到安全存储失败: {exc}", exc=exc)
        if llm_api_key:
            self.entry_llm_key.delete(0, tk.END)
            self.entry_llm_key.insert(0, llm_api_key)

        llm_model = str(data.get("entries", {}).get("llm_model", "")).strip()
        if llm_model:
            self.entry_llm_model.delete(0, tk.END)
            self.entry_llm_model.insert(0, llm_model)
        llm_base_url = str(data.get("entries", {}).get("llm_base_url", "")).strip()
        if hasattr(self, "entry_llm_base_url"):
            self.entry_llm_base_url.delete(0, tk.END)
            self.entry_llm_base_url.insert(0, self._normalize_base_url(llm_base_url))
        shot_custom_dir = str(data.get("entries", {}).get("shot_custom_dir", "")).strip()
        if shot_custom_dir and hasattr(self, "entry_shot_custom_dir"):
            self.entry_shot_custom_dir.delete(0, tk.END)
            self.entry_shot_custom_dir.insert(0, shot_custom_dir)
        shot_vlm_model = str(data.get("entries", {}).get("shot_vlm_model", "")).strip()
        if shot_vlm_model and hasattr(self, "entry_shot_vlm_model"):
            self.entry_shot_vlm_model.delete(0, tk.END)
            self.entry_shot_vlm_model.insert(0, shot_vlm_model)
        shot_max_videos = str(data.get("entries", {}).get("shot_max_videos", "")).strip()
        if shot_max_videos and hasattr(self, "entry_shot_max_videos"):
            self.entry_shot_max_videos.delete(0, tk.END)
            self.entry_shot_max_videos.insert(0, shot_max_videos)
        shot_min_duration = str(data.get("entries", {}).get("shot_min_duration", "")).strip()
        if shot_min_duration and hasattr(self, "entry_shot_min_duration"):
            self.entry_shot_min_duration.delete(0, tk.END)
            self.entry_shot_min_duration.insert(0, shot_min_duration)
        shot_diff_threshold = str(data.get("entries", {}).get("shot_diff_threshold", "")).strip()
        if shot_diff_threshold and hasattr(self, "entry_shot_diff_threshold"):
            self.entry_shot_diff_threshold.delete(0, tk.END)
            self.entry_shot_diff_threshold.insert(0, shot_diff_threshold)
        main_shot_vlm_model = str(data.get("entries", {}).get("main_shot_vlm_model", "")).strip()
        if main_shot_vlm_model and hasattr(self, "entry_main_shot_vlm_model"):
            self.entry_main_shot_vlm_model.delete(0, tk.END)
            self.entry_main_shot_vlm_model.insert(0, main_shot_vlm_model)
        main_shot_max_videos = str(data.get("entries", {}).get("main_shot_max_videos", "")).strip()
        if main_shot_max_videos and hasattr(self, "entry_main_shot_max_videos"):
            self.entry_main_shot_max_videos.delete(0, tk.END)
            self.entry_main_shot_max_videos.insert(0, main_shot_max_videos)
        main_shot_top_n = str(data.get("entries", {}).get("main_shot_top_n", "")).strip()
        if main_shot_top_n and hasattr(self, "entry_main_shot_top_n"):
            self.entry_main_shot_top_n.delete(0, tk.END)
            self.entry_main_shot_top_n.insert(0, main_shot_top_n)

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
        draft_mode = str(data.get("combo_values", {}).get("draft_mode", "")).strip()
        if hasattr(self, "combo_draft_mode") and draft_mode and draft_mode in self.combo_draft_mode.cget("values"):
            self.combo_draft_mode.set(draft_mode)
        shot_source_kind = str(data.get("combo_values", {}).get("shot_source_kind", "")).strip()
        if hasattr(self, "combo_shot_source") and shot_source_kind and shot_source_kind in self.combo_shot_source.cget("values"):
            self.combo_shot_source.set(shot_source_kind)
        if legacy_llm_api_key or migrated_legacy_key:
            self._save_ui_settings()

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
        if "shot_use_vlm" in checkbox_values and hasattr(self, "var_shot_use_vlm"):
            self.var_shot_use_vlm.set(bool(checkbox_values["shot_use_vlm"]))
        if "main_shot_recall_enabled" in checkbox_values and hasattr(self, "var_main_shot_recall_enabled"):
            self.var_main_shot_recall_enabled.set(bool(checkbox_values["main_shot_recall_enabled"]))
        if "main_shot_use_vlm" in checkbox_values and hasattr(self, "var_main_shot_use_vlm"):
            self.var_main_shot_use_vlm.set(bool(checkbox_values["main_shot_use_vlm"]))
        if "main_shot_force_refresh" in checkbox_values and hasattr(self, "var_main_shot_force_refresh"):
            self.var_main_shot_force_refresh.set(bool(checkbox_values["main_shot_force_refresh"]))
        if "main_strict_semantic" in checkbox_values and hasattr(self, "var_main_strict_semantic"):
            self.var_main_strict_semantic.set(bool(checkbox_values["main_strict_semantic"]))

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
        if hasattr(self, "_refresh_shot_source_state"):
            self._refresh_shot_source_state()
        if hasattr(self, "_refresh_shot_vlm_state"):
            self._refresh_shot_vlm_state()
        if hasattr(self, "_refresh_mainflow_semantic_state"):
            self._refresh_mainflow_semantic_state()
            



    def on_close(self):
        self._save_ui_settings()
        self.executor.shutdown(wait=False)
        self.destroy()

    def _normalize_base_url(self, value):
        normalized = str(value or "").strip().rstrip("/")
        return normalized or DEFAULT_LLM_BASE_URL

    def _get_llm_base_url(self):
        if not hasattr(self, "entry_llm_base_url"):
            return DEFAULT_LLM_BASE_URL
        normalized = self._normalize_base_url(self.entry_llm_base_url.get())
        current = self.entry_llm_base_url.get().strip()
        if current != normalized:
            self.entry_llm_base_url.delete(0, tk.END)
            self.entry_llm_base_url.insert(0, normalized)
        return normalized

    def _refresh_mainflow_semantic_state(self):
        enabled = bool(getattr(self, "var_main_shot_recall_enabled", None) and self.var_main_shot_recall_enabled.get())
        use_vlm = bool(getattr(self, "var_main_shot_use_vlm", None) and self.var_main_shot_use_vlm.get())
        if hasattr(self, "entry_main_shot_vlm_model"):
            self.entry_main_shot_vlm_model.configure(state="normal" if enabled and use_vlm else "disabled")
        if hasattr(self, "entry_main_shot_max_videos"):
            self.entry_main_shot_max_videos.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "entry_main_shot_top_n"):
            self.entry_main_shot_top_n.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "chk_main_shot_force_refresh"):
            self.chk_main_shot_force_refresh.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "chk_main_strict_semantic"):
            self.chk_main_strict_semantic.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "chk_main_shot_use_vlm"):
            self.chk_main_shot_use_vlm.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "lbl_main_shot_hint"):
            if not enabled:
                text = "当前已关闭主流程镜头召回，系统将回退到较弱的目录/标签/语义规则匹配。"
            elif getattr(self, "var_main_strict_semantic", None) and self.var_main_strict_semantic.get():
                text = "当前启用严格语义插入：识别不够有把握的片段将被放弃，不再为凑占比盲选乱插。"
            elif use_vlm:
                text = "当前已启用主流程 VLM 素材理解；建议保持“每次任务强制重建素材理解”为开启，以适配新上传素材。"
            else:
                text = "当前仅启用启发式镜头理解，不会调用视觉大模型；适合临时降级排障。"
            self.lbl_main_shot_hint.configure(text=text)

    def _validate_mainflow_semantic_requirements(self):
        enabled = bool(getattr(self, "var_main_shot_recall_enabled", None) and self.var_main_shot_recall_enabled.get())
        use_vlm = bool(getattr(self, "var_main_shot_use_vlm", None) and self.var_main_shot_use_vlm.get())
        if not enabled or not use_vlm:
            return True
        api_key = self.entry_llm_key.get().strip() if hasattr(self, "entry_llm_key") else ""
        llm_model = normalize_model_name(self.entry_llm_model.get().strip()) if hasattr(self, "entry_llm_model") else ""
        vlm_model = self.entry_main_shot_vlm_model.get().strip() if hasattr(self, "entry_main_shot_vlm_model") else ""
        missing = []
        if not api_key:
            missing.append("API Key")
        if not llm_model:
            missing.append("文本模型名称")
        if not vlm_model:
            missing.append("视觉模型名称")
        if missing:
            messagebox.showerror(
                "主流程素材理解未配置完整",
                "你已开启主流程 VLM 素材理解，但以下配置缺失：\n"
                + "\n".join(f"- {item}" for item in missing)
                + "\n\n请先补齐配置，再提交任务。",
            )
            return False
        return True

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

    def _short_path(self, path, max_len=62):
        text = str(path or "").strip()
        if len(text) <= max_len:
            return text or "-"
        return f"...{text[-(max_len - 3):]}"

    def _get_selected_draft_mode(self):
        label = getattr(self, "combo_draft_mode", None)
        if not label:
            return DRAFT_MODE_AUTO
        selected = self.combo_draft_mode.get().strip()
        for mode, mode_label in DRAFT_MODE_LABELS.items():
            if selected == mode_label:
                return mode
        return DRAFT_MODE_AUTO

    def _set_selected_draft_mode(self, mode):
        if hasattr(self, "combo_draft_mode"):
            self.combo_draft_mode.set(DRAFT_MODE_LABELS.get(mode, DRAFT_MODE_LABELS[DRAFT_MODE_AUTO]))

    def _resolve_selected_draft_root_info(self):
        return get_draft_root_info(self._get_selected_draft_mode())

    def _render_draft_visibility_text(self, draft_info):
        if not draft_info:
            return "草稿目录状态未知"
        resolved_mode = draft_info.get("resolved_mode")
        root = draft_info.get("resolved_root", "")
        fallback_reason = str(draft_info.get("fallback_reason", "")).strip()
        if resolved_mode == DRAFT_MODE_SYSTEM:
            return f"当前草稿会直接写入剪映首页目录: {self._short_path(root)}"
        if fallback_reason:
            return f"{fallback_reason}；本次草稿不会直接出现在剪映首页，请用“打开草稿复查”查看。"
        return f"当前草稿写入便携目录，仅用于稳定排障: {self._short_path(root)}"

    def _build_portable_draft_status_note(self, draft_info):
        draft_info = draft_info or {}
        fallback_reason = str(draft_info.get("fallback_reason", "")).strip()
        portable_root = str(draft_info.get("resolved_root", "")).strip()
        root_hint = self._short_path(portable_root, max_len=72) if portable_root else "当前运行目录下的便携草稿区"
        if fallback_reason:
            return f"{fallback_reason}，本次草稿不会直接出现在剪映首页，已保留在便携目录，请点“打开草稿复查”查看: {root_hint}"
        return f"本次草稿不会直接出现在剪映首页，已保留在便携目录，请点“打开草稿复查”查看: {root_hint}"

    def _is_directory_writable(self, path):
        path = str(path or "").strip()
        if not path:
            return False
        try:
            os.makedirs(path, exist_ok=True)
            probe_path = os.path.join(path, "write_probe.tmp")
            with open(probe_path, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe_path)
            return True
        except OSError:
            return False

    def _resolve_worker_draft_root(self):
        draft_info = self._resolve_selected_draft_root_info()
        requested_mode = str(draft_info.get("requested_mode", "")).strip()
        if requested_mode == DRAFT_MODE_PORTABLE:
            return str(draft_info.get("resolved_root", "")).strip()
        return ""

    def _refresh_runtime_target_labels(self):
        draft_info = self._resolve_selected_draft_root_info()
        draft_root = str(draft_info.get("resolved_root", "")).strip()
        draft_mode_label = DRAFT_MODE_LABELS.get(draft_info.get("requested_mode"), DRAFT_MODE_LABELS[DRAFT_MODE_AUTO])
        if hasattr(self, "lbl_draft_target_value"):
            self.lbl_draft_target_value.configure(text=draft_root or "-")
        if hasattr(self, "lbl_output_target_value"):
            self.lbl_output_target_value.configure(text=self.output_dir)
        if hasattr(self, "lbl_draft_visibility_hint"):
            self.lbl_draft_visibility_hint.configure(text=self._render_draft_visibility_text(draft_info))
        if hasattr(self, "lbl_runtime_target_status"):
            runtime_text = (
                f"草稿策略: {draft_mode_label} | 实际草稿目录: {self._short_path(draft_root)} | "
                f"结果目录: {self._short_path(self.output_dir)}"
            )
            self.lbl_runtime_target_status.configure(text=runtime_text)

    def _locate_task_artifacts(self, video_file, draft_root=""):
        base_name = os.path.splitext(os.path.basename(video_file))[0]
        project_prefix = f"OTC推广_{base_name}"
        review_pattern = os.path.join(self.output_dir, f"{project_prefix}_*_审查报告.json")
        review_candidates = [path for path in glob.glob(review_pattern) if os.path.isfile(path)]
        review_path = max(review_candidates, key=os.path.getmtime) if review_candidates else ""

        draft_candidates = []
        if draft_root and os.path.isdir(draft_root):
            draft_pattern = os.path.join(draft_root, f"{project_prefix}_*")
            draft_candidates = [path for path in glob.glob(draft_pattern) if os.path.isdir(path)]
        draft_path = max(draft_candidates, key=os.path.getmtime) if draft_candidates else ""
        return review_path, draft_path

    def _collect_current_task_draft_names(self, draft_root):
        draft_root = str(draft_root or "").strip()
        if not draft_root or not os.path.isdir(draft_root):
            return []
        names = []
        for _, info in sorted(self._snapshot_running_tasks().items()):
            draft_name = ""
            draft_dir = str(info.get("draft_dir", "")).strip()
            if draft_dir:
                candidate_name = os.path.basename(draft_dir)
                candidate_path = os.path.join(draft_root, candidate_name)
                if candidate_name.startswith("OTC推广_") and os.path.isdir(candidate_path):
                    draft_name = candidate_name
            if not draft_name:
                log_text = str(info.get("log", "")).strip()
                match = re.search(r"草稿:\s*([^|]+)", log_text)
                if match:
                    candidate_name = match.group(1).strip()
                    candidate_path = os.path.join(draft_root, candidate_name)
                    if candidate_name.startswith("OTC推广_") and os.path.isdir(candidate_path):
                        draft_name = candidate_name
            video_file = str(info.get("file", "")).strip()
            if not draft_name and video_file:
                _, located_draft_dir = self._locate_task_artifacts(video_file, draft_root=draft_root)
                if located_draft_dir:
                    draft_name = os.path.basename(located_draft_dir)
            if draft_name and draft_name not in names:
                names.append(draft_name)
        return names

    def _reconcile_draft_root_with_fallback(self, draft_root, include_names, report_path, lock_path):
        keep_names = tuple(str(name or "").strip() for name in (include_names or ()) if str(name or "").strip())
        try:
            return reconcile_root_meta(
                draft_root=draft_root,
                restore_project_drafts=False,
                project_prefixes=("OTC推广_",),
                include_names=keep_names,
                remove_stale_managed=False,
                remove_stale_from_recycle=False,
                report_path=report_path,
                lock_path=lock_path,
            )
        except Exception as exc:
            self._log_nonfatal_issue(
                "reconcile_draft_root",
                f"草稿索引修复失败，已回退为只重建索引: {self._short_path(draft_root, max_len=88)}",
                exc=exc,
            )
            return reconcile_root_meta(
                draft_root=draft_root,
                restore_project_drafts=False,
                project_prefixes=("OTC推广_",),
                include_names=keep_names,
                remove_stale_managed=False,
                remove_stale_from_recycle=False,
                report_path=report_path,
                lock_path=lock_path,
            )

    def _write_task_report_snapshot(self):
        payload = {
            "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "output_dir": self.output_dir,
            "draft_root": self._resolve_worker_draft_root() or self._resolve_selected_draft_root_info().get("resolved_root", ""),
            "tasks": [],
        }
        for task_id, info in sorted(self._snapshot_running_tasks().items()):
            payload["tasks"].append({
                "task_id": task_id,
                "file": info.get("file", ""),
                "status": info.get("status", ""),
                "result": info.get("result", ""),
                "log": info.get("log", ""),
                "log_path": info.get("log_path", ""),
                "review_report": info.get("review_report", ""),
                "draft_dir": info.get("draft_dir", ""),
            })
        with open(self.task_report_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_task_report_snapshot(self):
        if not os.path.exists(self.task_report_path):
            return
        try:
            with open(self.task_report_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return

        tasks = payload.get("tasks", []) or []
        with self._tasks_lock:
            self.running_tasks = {}
            for item in tasks:
                task_id = str(item.get("task_id", "")).strip()
                if not task_id:
                    continue
                self.running_tasks[task_id] = {
                    "status": item.get("status", STATUS_READY),
                    "log": item.get("log", ""),
                    "file": item.get("file", ""),
                    "result": item.get("result", ""),
                    "log_path": item.get("log_path", ""),
                    "review_report": item.get("review_report", ""),
                    "draft_dir": item.get("draft_dir", ""),
                }
                try:
                    task_num = int(task_id.split("-", 1)[1])
                    self.task_count = max(self.task_count, task_num)
                except Exception:
                    pass
        self._sync_task_counter_from_running_tasks()

        if hasattr(self, "tree"):
            for item in self.tree.get_children():
                self.tree.delete(item)
            for task_id, info in sorted(self._snapshot_running_tasks().items()):
                self.tree.insert(
                    "",
                    "end",
                    iid=task_id,
                    values=(
                        task_id,
                        "-",
                        info.get("status", ""),
                        info.get("log", ""),
                    ),
                )
                self._apply_tree_row_tag(task_id, info)
            children = self.tree.get_children()
            if children:
                self.tree.selection_set(children[0])
                self.tree.focus(children[0])
                self.on_task_tree_selected()

    def _sync_task_draft_to_official_root(self, draft_dir):
        draft_dir = str(draft_dir or "").strip()
        if not draft_dir or not os.path.isdir(draft_dir):
            return {"draft_dir": "", "status_note": ""}
        draft_info = self._resolve_selected_draft_root_info()
        requested_mode = str(draft_info.get("requested_mode", "")).strip()
        official_root = get_official_draft_root()
        if requested_mode == DRAFT_MODE_PORTABLE:
            return {
                "draft_dir": draft_dir,
                "status_note": self._build_portable_draft_status_note(draft_info),
            }

        source_root = os.path.dirname(draft_dir)
        if not official_root:
            return {
                "draft_dir": draft_dir,
                "status_note": "未检测到系统剪映草稿目录，草稿保留在便携目录。",
            }
        if os.path.abspath(source_root) == os.path.abspath(official_root):
            return {"draft_dir": draft_dir, "status_note": ""}

        sync_report_path = os.path.join(self.output_dir, "draft_registry_sync_report.json")
        try:
            sync_managed_drafts(
                source_root=source_root,
                target_root=official_root,
                project_prefixes=("OTC推广_",),
                include_names=(os.path.basename(draft_dir),),
                remove_stale_managed=False,
                report_path=sync_report_path,
                lock_path=os.path.join(self.output_dir, ".official_root_meta_info.lock"),
            )
        except Exception as exc:
            self._log_nonfatal_issue(
                "sync_task_draft_to_official_root",
                f"草稿已生成，但同步到系统剪映目录失败，已保留便携目录结果: {self._short_path(draft_dir, max_len=88)}",
                exc=exc,
            )
            return {
                "draft_dir": draft_dir,
                "status_note": "系统剪映目录同步失败，草稿已保留在便携目录，请点“打开草稿复查”。",
            }
        synced_path = os.path.join(official_root, os.path.basename(draft_dir))
        if os.path.isdir(synced_path):
            return {"draft_dir": synced_path, "status_note": ""}
        return {
            "draft_dir": draft_dir,
            "status_note": "系统剪映目录未看到同步结果，草稿已保留在便携目录，请点“打开草稿复查”。",
        }

    def _schedule_task_retry(self, task_id, env, sensitivity, video_file, retry_count, reason):
        next_retry = retry_count + 1
        delay_seconds = min(8, max(2, next_retry * 2))
        with self._tasks_lock:
            self.running_tasks[task_id] = {
                "status": STATUS_RUNNING,
                "log": f"{reason}，将在 {delay_seconds} 秒后重试 ({next_retry}/{self.batch_retry_limit})",
                "file": video_file,
                "result": "RETRYING",
            }
        self._write_task_report_snapshot()

        def _retry():
            self.executor.submit(self.execute_task, task_id, env, sensitivity, video_file, next_retry)

        timer = threading.Timer(delay_seconds, _retry)
        timer.daemon = True
        timer.start()

    def inspect_draft_registry(self):
        try:
            draft_info = self._resolve_selected_draft_root_info()
            active_root = draft_info.get("resolved_root", "") or get_draft_root()
            report_path = os.path.join(self.output_dir, "draft_registry_health.json")
            report = self._reconcile_draft_root_with_fallback(
                draft_root=active_root,
                include_names=(),
                report_path=report_path,
                lock_path=os.path.join(self.output_dir, ".root_meta_info.lock"),
            )
            restored = len(report.get("restored_from_recycle", []))
            invalid = len(report.get("invalid_drafts", []))
            if restored or invalid:
                print(f"草稿健康检查完成: 恢复 {restored} 个，发现无效目录 {invalid} 个。")
            return report
        except Exception as exc:
            print(f"草稿健康检查失败: {exc}")
            return {}

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
                try:
                    import json
                    output_dir = runtime_path("output")
                    os.makedirs(output_dir, exist_ok=True)

                    mapping_data = []
                    discovered = self._discover_material_dirs(base)
                    alias_map = self._material_dir_aliases()

                    for root, dirs, files in os.walk(base):
                        rel_path = os.path.relpath(root, base)
                        if rel_path == '.':
                            continue

                        depth = len(rel_path.split(os.sep))
                        mat_type = "未知"
                        for key, aliases in alias_map.items():
                            if any(alias in rel_path for alias in aliases):
                                mat_type = "必选" if key in ("speech", "product", "symptom") else "可选"
                                break

                        mapping_data.append({
                            "relative_path": rel_path,
                            "depth": depth,
                            "material_type": mat_type
                        })

                        target_dir = os.path.join(output_dir, rel_path)
                        os.makedirs(target_dir, exist_ok=True)

                    for key, aliases in alias_map.items():
                        p = discovered.get(key, "")
                        if not p and aliases:
                            p = os.path.join(base, aliases[0])
                        if os.path.exists(p):
                            self.entries[key].delete(0, tk.END)
                            self.entries[key].insert(0, p)
                            if key == "bgm" and hasattr(self, "entry_user_bgm_dir"):
                                self.entry_user_bgm_dir.delete(0, tk.END)
                                self.entry_user_bgm_dir.insert(0, p)

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

        delivery_frame = ttk.LabelFrame(content, text="交付与草稿输出", style='Card.TLabelframe')
        delivery_frame.pack(fill="x", pady=2)
        delivery_frame.columnconfigure(1, weight=1)

        ttk.Label(delivery_frame, text="草稿输出策略:").grid(row=0, column=0, sticky="e", pady=3, padx=5)
        self.combo_draft_mode = ttk.Combobox(
            delivery_frame,
            values=[DRAFT_MODE_LABELS[DRAFT_MODE_AUTO], DRAFT_MODE_LABELS[DRAFT_MODE_SYSTEM], DRAFT_MODE_LABELS[DRAFT_MODE_PORTABLE]],
            state="readonly",
            width=24,
        )
        self._set_selected_draft_mode(DRAFT_MODE_AUTO)
        self.combo_draft_mode.grid(row=0, column=1, sticky="w", pady=3, padx=5)
        self.combo_draft_mode.bind("<<ComboboxSelected>>", lambda _event: self._refresh_runtime_target_labels())

        ttk.Button(
            delivery_frame,
            text="打开当前草稿目录",
            style='Secondary.TButton',
            command=self.open_draft_folder,
        ).grid(row=0, column=2, sticky="w", padx=5, pady=3)
        ttk.Button(
            delivery_frame,
            text="打开结果目录",
            style='Secondary.TButton',
            command=self.open_output_folder,
        ).grid(row=0, column=3, sticky="w", padx=5, pady=3)

        ttk.Label(delivery_frame, text="当前草稿目录:").grid(row=1, column=0, sticky="ne", pady=3, padx=5)
        self.lbl_draft_target_value = ttk.Label(delivery_frame, text="-", style="Info.TLabel", justify="left")
        self.lbl_draft_target_value.grid(row=1, column=1, columnspan=3, sticky="w", pady=3, padx=5)

        ttk.Label(delivery_frame, text="结果输出目录:").grid(row=2, column=0, sticky="ne", pady=3, padx=5)
        self.lbl_output_target_value = ttk.Label(delivery_frame, text=self.output_dir, style="Info.TLabel", justify="left")
        self.lbl_output_target_value.grid(row=2, column=1, columnspan=3, sticky="w", pady=3, padx=5)

        self.lbl_draft_visibility_hint = ttk.Label(
            delivery_frame,
            text="",
            style="Info.TLabel",
            justify="left",
        )
        self.lbl_draft_visibility_hint.grid(row=3, column=0, columnspan=4, sticky="w", pady=(3, 0), padx=5)

        # 1.5 LLM 大模型配置区
        llm_frame = ttk.LabelFrame(content, text="AI 大模型配置 (用于深度语义理解)", style='Card.TLabelframe')
        llm_frame.pack(fill="x", pady=2)
        llm_frame.columnconfigure(1, weight=1)
        llm_frame.columnconfigure(3, weight=1)
        
        ttk.Label(llm_frame, text="API Key:").grid(row=0, column=0, sticky="e", pady=3, padx=5)
        self.entry_llm_key = ttk.Entry(llm_frame, width=40, show="*")
        self.entry_llm_key.grid(row=0, column=1, sticky="w", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="服务地址:").grid(row=0, column=2, sticky="e", pady=3, padx=5)
        self.entry_llm_base_url = ttk.Entry(llm_frame, width=40)
        self.entry_llm_base_url.insert(0, DEFAULT_LLM_BASE_URL)
        self.entry_llm_base_url.grid(row=0, column=3, sticky="ew", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="模型名称:").grid(row=1, column=0, sticky="e", pady=3, padx=5)
        self.entry_llm_model = ttk.Entry(llm_frame, width=40)
        self.entry_llm_model.insert(0, DEFAULT_LLM_MODEL)
        self.entry_llm_model.grid(row=1, column=1, sticky="w", pady=3, padx=5)

        self.btn_test_llm = ttk.Button(
            llm_frame,
            text="测试大模型联通",
            style='Secondary.TButton',
            command=self.test_llm_connectivity,
        )
        self.btn_test_llm.grid(row=1, column=2, sticky="e", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="*填写 API Key 后，系统将使用大模型基于视频总时长和节奏参数，\n精准输出病症/产品插入点以及音效、BGM的情绪节点。Key 将加密保存在当前 Windows 账户下；服务地址默认使用 SiliconFlow，也可改为兼容 OpenAI 的网关。",
                  style='Info.TLabel').grid(row=2, column=0, columnspan=4, sticky="w", pady=3, padx=5)

        semantic_frame = ttk.LabelFrame(content, text="主流程素材理解", style='Card.TLabelframe')
        semantic_frame.pack(fill="x", pady=2)
        semantic_frame.columnconfigure(1, weight=1)
        semantic_frame.columnconfigure(3, weight=1)

        self.var_main_shot_recall_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            semantic_frame,
            text="启用主流程镜头召回",
            variable=self.var_main_shot_recall_enabled,
            command=self._refresh_mainflow_semantic_state,
            style='Card.TCheckbutton',
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=4)

        self.var_main_shot_use_vlm = tk.BooleanVar(value=True)
        self.chk_main_shot_use_vlm = ttk.Checkbutton(
            semantic_frame,
            text="用视觉大模型理解新素材",
            variable=self.var_main_shot_use_vlm,
            command=self._refresh_mainflow_semantic_state,
            style='Card.TCheckbutton',
        )
        self.chk_main_shot_use_vlm.grid(row=0, column=2, columnspan=2, sticky="w", padx=5, pady=4)

        ttk.Label(semantic_frame, text="视觉模型名:").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        self.entry_main_shot_vlm_model = ttk.Entry(semantic_frame, width=28)
        self.entry_main_shot_vlm_model.insert(0, DEFAULT_VLM_MODEL)
        self.entry_main_shot_vlm_model.grid(row=1, column=1, sticky="we", padx=5, pady=4)
        self.btn_test_vision = ttk.Button(
            semantic_frame,
            text="测试视觉模型联通",
            style='Secondary.TButton',
            command=self.test_vision_connectivity,
        )
        self.btn_test_vision.grid(row=2, column=2, columnspan=2, sticky="w", padx=5, pady=4)

        ttk.Label(semantic_frame, text="每类最多分析视频数:").grid(row=1, column=2, sticky="e", padx=(180, 5), pady=4)
        self.entry_main_shot_max_videos = ttk.Entry(semantic_frame, width=10)
        self.entry_main_shot_max_videos.insert(0, "0")
        self.entry_main_shot_max_videos.grid(row=1, column=3, sticky="w", padx=5, pady=4)

        ttk.Label(semantic_frame, text="每段候选召回数:").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        self.entry_main_shot_top_n = ttk.Entry(semantic_frame, width=10)
        self.entry_main_shot_top_n.insert(0, "24")
        self.entry_main_shot_top_n.grid(row=2, column=1, sticky="w", padx=5, pady=4)

        self.var_main_shot_force_refresh = tk.BooleanVar(value=True)
        self.chk_main_shot_force_refresh = ttk.Checkbutton(
            semantic_frame,
            text="每次任务强制重建素材理解",
            variable=self.var_main_shot_force_refresh,
            command=self._refresh_mainflow_semantic_state,
            style='Card.TCheckbutton',
        )
        self.chk_main_shot_force_refresh.grid(row=3, column=2, columnspan=2, sticky="w", padx=5, pady=4)

        self.var_main_strict_semantic = tk.BooleanVar(value=True)
        self.chk_main_strict_semantic = ttk.Checkbutton(
            semantic_frame,
            text="严格语义插入（不乱插）",
            variable=self.var_main_strict_semantic,
            command=self._refresh_mainflow_semantic_state,
            style='Card.TCheckbutton',
        )
        self.chk_main_strict_semantic.grid(row=4, column=0, columnspan=2, sticky="w", padx=5, pady=4)

        self.lbl_main_shot_hint = ttk.Label(semantic_frame, text="", style="Info.TLabel", justify="left")
        self.lbl_main_shot_hint.grid(row=5, column=0, columnspan=4, sticky="w", padx=5, pady=(0, 4))
        self._refresh_mainflow_semantic_state()

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
            ("素材理解", [
                ("镜头召回",      lambda: "开" if self.var_main_shot_recall_enabled.get() else "关"),
                ("VLM识别",       lambda: "开" if self.var_main_shot_use_vlm.get() else "关"),
                ("视觉模型",      lambda: self.entry_main_shot_vlm_model.get().strip() or "-"),
                ("强制重建",      lambda: "是" if self.var_main_shot_force_refresh.get() else "否"),
                ("严格语义",      lambda: "是" if self.var_main_strict_semantic.get() else "否"),
                ("每类分析视频",  lambda: self.entry_main_shot_max_videos.get().strip() or "0"),
                ("召回候选数",    lambda: self.entry_main_shot_top_n.get().strip() or "24"),
            ]),
            ("交付路径", [
                ("草稿策略",    lambda: DRAFT_MODE_LABELS.get(self._get_selected_draft_mode(), DRAFT_MODE_LABELS[DRAFT_MODE_AUTO])),
                ("当前草稿目录", lambda: self._short_path(self._resolve_selected_draft_root_info().get("resolved_root", ""))),
                ("报告输出目录", lambda: self._short_path(self.output_dir)),
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
        self.after(5000, self._start_summary_refresh)

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

    def _is_task_board_visible(self):
        if not hasattr(self, "notebook") or not hasattr(self, "tab_tasks"):
            return True
        try:
            return self.notebook.select() == str(self.tab_tasks)
        except Exception:
            return True

    def _get_selected_task_ids(self):
        if not hasattr(self, "tree"):
            return []
        return [task_id for task_id in self.tree.selection() if self.tree.exists(task_id)]

    def _update_task_selection_actions(self):
        if not hasattr(self, "tree"):
            return
        has_rows = bool(self.tree.get_children())
        selected_ids = self._get_selected_task_ids()
        tasks = self._snapshot_running_tasks()
        has_draft = any(
            str(tasks.get(task_id, {}).get("draft_dir", "")).strip()
            for task_id in selected_ids
        )
        if hasattr(self, "btn_task_select_all"):
            self._set_button_state(self.btn_task_select_all, "normal" if has_rows else "disabled")
        if hasattr(self, "btn_task_select_inverse"):
            self._set_button_state(self.btn_task_select_inverse, "normal" if has_rows else "disabled")
        if hasattr(self, "btn_purge_task_drafts"):
            self._set_button_state(self.btn_purge_task_drafts, "normal" if has_draft else "disabled")

    def _update_maintenance_label(self):
        text = (f"维护告警: {self.maintenance_warning_count} | "
                f"{self.latest_maintenance_warning}")
        style_name = 'Warning.TLabel' if self.maintenance_warning_count > 0 else 'Info.TLabel'
        if (self.lbl_maintenance_status.cget('text') != text or
                self.lbl_maintenance_status.cget('style') != style_name):
            self.lbl_maintenance_status.configure(text=text, style=style_name)

    def _get_selected_shot_source_kind(self):
        if not hasattr(self, "combo_shot_source"):
            return "product"
        selected = self.combo_shot_source.get().strip()
        for kind, label in SHOT_SOURCE_LABELS.items():
            if selected == label:
                return kind
        return "product"

    def _set_selected_shot_source_kind(self, kind):
        if hasattr(self, "combo_shot_source"):
            self.combo_shot_source.set(SHOT_SOURCE_LABELS.get(kind, SHOT_SOURCE_LABELS["product"]))

    def _resolve_shot_source_dir(self):
        source_kind = self._get_selected_shot_source_kind()
        if source_kind == "custom":
            return self.entry_shot_custom_dir.get().strip(), "generic"
        if hasattr(self, "entries") and source_kind in self.entries:
            return self.entries[source_kind].get().strip(), source_kind
        return "", "generic"

    def _refresh_shot_source_state(self, _event=None):
        source_kind = self._get_selected_shot_source_kind()
        custom_enabled = source_kind == "custom"
        if hasattr(self, "entry_shot_custom_dir"):
            self.entry_shot_custom_dir.configure(state="normal" if custom_enabled else "disabled")
        if hasattr(self, "btn_shot_custom_dir"):
            self.btn_shot_custom_dir.configure(state="normal" if custom_enabled else "disabled")
        source_dir, role = self._resolve_shot_source_dir()
        hint = source_dir if source_dir else "请选择要分析的素材目录"
        role_label = {"product": "产品镜头", "symptom": "病症镜头", "speech": "口播镜头", "generic": "通用镜头"}.get(role, "通用镜头")
        if hasattr(self, "lbl_shot_source_hint"):
            self.lbl_shot_source_hint.configure(text=f"当前分析对象: {role_label} | 路径: {self._short_path(hint, max_len=88)}")

    def _refresh_shot_vlm_state(self):
        enabled = bool(getattr(self, "var_shot_use_vlm", None) and self.var_shot_use_vlm.get())
        if hasattr(self, "entry_shot_vlm_model"):
            self.entry_shot_vlm_model.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "lbl_shot_vlm_hint"):
            self.lbl_shot_vlm_hint.configure(
                text="已启用视觉模型描述，需要可用 API Key 和视觉模型名。"
                if enabled else
                "默认关闭。分享给同事做主流程剪辑时，不需要配置视觉模型。"
            )

    def build_shot_library_tab(self):
        frame = self.tab_shot_library
        ttk.Label(
            frame,
            text="镜头库预览：先把素材自动切成镜头，再预览关键帧、镜头标签和可选的大模型描述。",
            style="Info.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        control_frame = ttk.LabelFrame(frame, text="镜头分析控制台", style="Card.TLabelframe")
        control_frame.pack(fill="x", pady=(0, 8))
        control_frame.columnconfigure(3, weight=1)

        ttk.Label(control_frame, text="分析来源:").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        self.combo_shot_source = ttk.Combobox(
            control_frame,
            values=[SHOT_SOURCE_LABELS["product"], SHOT_SOURCE_LABELS["symptom"], SHOT_SOURCE_LABELS["speech"], SHOT_SOURCE_LABELS["custom"]],
            state="readonly",
            width=18,
        )
        self._set_selected_shot_source_kind("product")
        self.combo_shot_source.grid(row=0, column=1, sticky="w", padx=5, pady=4)
        self.combo_shot_source.bind("<<ComboboxSelected>>", self._refresh_shot_source_state)

        ttk.Label(control_frame, text="自定义目录:").grid(row=0, column=2, sticky="e", padx=5, pady=4)
        self.entry_shot_custom_dir = ttk.Entry(control_frame, width=42)
        self.entry_shot_custom_dir.grid(row=0, column=3, sticky="we", padx=5, pady=4)
        self.btn_shot_custom_dir = ttk.Button(
            control_frame,
            text="选择文件夹...",
            style="Ghost.TButton",
            command=lambda: self.browse_folder(self.entry_shot_custom_dir),
        )
        self.btn_shot_custom_dir.grid(row=0, column=4, sticky="w", padx=5, pady=4)

        ttk.Label(control_frame, text="最多分析视频数:").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        self.entry_shot_max_videos = ttk.Entry(control_frame, width=10)
        self.entry_shot_max_videos.insert(0, "8")
        self.entry_shot_max_videos.grid(row=1, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(control_frame, text="最短镜头时长(秒):").grid(row=1, column=2, sticky="e", padx=5, pady=4)
        self.entry_shot_min_duration = ttk.Entry(control_frame, width=10)
        self.entry_shot_min_duration.insert(0, "0.8")
        self.entry_shot_min_duration.grid(row=1, column=3, sticky="w", padx=5, pady=4)

        ttk.Label(control_frame, text="切分灵敏阈值:").grid(row=1, column=4, sticky="e", padx=5, pady=4)
        self.entry_shot_diff_threshold = ttk.Entry(control_frame, width=10)
        self.entry_shot_diff_threshold.insert(0, "0.42")
        self.entry_shot_diff_threshold.grid(row=1, column=5, sticky="w", padx=5, pady=4)

        self.var_shot_use_vlm = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            control_frame,
            text="启用大模型镜头描述",
            variable=self.var_shot_use_vlm,
            command=self._refresh_shot_vlm_state,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=4)

        ttk.Label(control_frame, text="视觉模型名:").grid(row=2, column=2, sticky="e", padx=5, pady=4)
        self.entry_shot_vlm_model = ttk.Entry(control_frame, width=28)
        self.entry_shot_vlm_model.insert(0, DEFAULT_VLM_MODEL)
        self.entry_shot_vlm_model.grid(row=2, column=3, sticky="w", padx=5, pady=4)

        self.btn_run_shot_analysis = ttk.Button(
            control_frame,
            text="开始分析镜头",
            style="Primary.TButton",
            command=self.start_shot_analysis,
        )
        self.btn_run_shot_analysis.grid(row=2, column=4, sticky="e", padx=5, pady=4)

        self.btn_refresh_shot_report = ttk.Button(
            control_frame,
            text="刷新最近结果",
            style="Secondary.TButton",
            command=self.refresh_latest_shot_report,
        )
        self.btn_refresh_shot_report.grid(row=2, column=5, sticky="w", padx=5, pady=4)

        self.btn_open_shot_cache = ttk.Button(
            control_frame,
            text="打开镜头缓存",
            style="Secondary.TButton",
            command=self.open_shot_cache_folder,
        )
        self.btn_open_shot_cache.grid(row=2, column=6, sticky="w", padx=5, pady=4)

        self.lbl_shot_source_hint = ttk.Label(control_frame, text="", style="Info.TLabel")
        self.lbl_shot_source_hint.grid(row=3, column=0, columnspan=7, sticky="w", padx=5, pady=(2, 2))
        self.lbl_shot_vlm_hint = ttk.Label(control_frame, text="", style="Info.TLabel")
        self.lbl_shot_vlm_hint.grid(row=4, column=0, columnspan=7, sticky="w", padx=5, pady=(0, 2))
        self.lbl_shot_summary = ttk.Label(control_frame, text="尚未生成镜头报告", style="Info.TLabel")
        self.lbl_shot_summary.grid(row=5, column=0, columnspan=7, sticky="w", padx=5, pady=(0, 6))

        content_frame = ttk.Frame(frame)
        content_frame.pack(fill="both", expand=True)
        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right_frame = ttk.LabelFrame(content_frame, text="镜头预览", style="Card.TLabelframe")
        right_frame.pack(side="right", fill="both", expand=True)

        tree_columns = ("video", "shot", "time", "subject", "scale", "purpose", "confidence", "source")
        self.tree_shots = ttk.Treeview(left_frame, columns=tree_columns, show="headings", height=16)
        self.tree_shots.heading("video", text="视频")
        self.tree_shots.heading("shot", text="镜头")
        self.tree_shots.heading("time", text="时间段")
        self.tree_shots.heading("subject", text="主体")
        self.tree_shots.heading("scale", text="景别")
        self.tree_shots.heading("purpose", text="用途")
        self.tree_shots.heading("confidence", text="置信度")
        self.tree_shots.heading("source", text="描述来源")
        self.tree_shots.column("video", width=140, anchor="w")
        self.tree_shots.column("shot", width=70, anchor="center")
        self.tree_shots.column("time", width=115, anchor="center")
        self.tree_shots.column("subject", width=105, anchor="center")
        self.tree_shots.column("scale", width=95, anchor="center")
        self.tree_shots.column("purpose", width=95, anchor="center")
        self.tree_shots.column("confidence", width=75, anchor="center")
        self.tree_shots.column("source", width=90, anchor="center")
        shot_scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree_shots.yview)
        self.tree_shots.configure(yscrollcommand=shot_scrollbar.set)
        self.tree_shots.pack(side="left", fill="both", expand=True)
        shot_scrollbar.pack(side="right", fill="y")
        self.tree_shots.bind("<<TreeviewSelect>>", self.on_shot_tree_selected)

        self.lbl_shot_preview_image = ttk.Label(right_frame, text="请选择左侧镜头查看预览", anchor="center")
        self.lbl_shot_preview_image.pack(fill="x", padx=8, pady=(8, 6))
        self.lbl_shot_preview_meta = ttk.Label(right_frame, text="-", style="Info.TLabel", justify="left")
        self.lbl_shot_preview_meta.pack(anchor="w", padx=8, pady=(0, 6))
        self.lbl_shot_preview_tags = ttk.Label(right_frame, text="-", style="Info.TLabel", justify="left", wraplength=420)
        self.lbl_shot_preview_tags.pack(anchor="w", padx=8, pady=(0, 6))
        self.lbl_shot_preview_desc = ttk.Label(right_frame, text="-", justify="left", wraplength=420)
        self.lbl_shot_preview_desc.pack(anchor="w", padx=8, pady=(0, 8))

        self._refresh_shot_source_state()
        self._refresh_shot_vlm_state()
        self.refresh_latest_shot_report(silent=True)

    def _parse_shot_analysis_params(self):
        try:
            max_videos = max(int(float(self.entry_shot_max_videos.get().strip() or "8")), 1)
        except ValueError:
            raise ValueError("最多分析视频数必须是正整数")
        try:
            min_duration = max(float(self.entry_shot_min_duration.get().strip() or "0.8"), 0.2)
        except ValueError:
            raise ValueError("最短镜头时长必须是数字")
        try:
            diff_threshold = float(self.entry_shot_diff_threshold.get().strip() or "0.42")
        except ValueError:
            raise ValueError("切分灵敏阈值必须是数字")
        if diff_threshold <= 0.05 or diff_threshold >= 0.95:
            raise ValueError("切分灵敏阈值建议在 0.05 到 0.95 之间")
        return max_videos, min_duration, diff_threshold

    def start_shot_analysis(self, force_refresh=False):
        source_dir, role = self._resolve_shot_source_dir()
        if not source_dir or not os.path.exists(source_dir):
            messagebox.showerror("路径错误", "当前镜头分析目录不存在，请先选择有效素材路径。")
            return
        try:
            max_videos, min_duration, diff_threshold = self._parse_shot_analysis_params()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        use_vlm = bool(self.var_shot_use_vlm.get())
        self._save_ui_settings()
        self.btn_run_shot_analysis.config(state="disabled")
        self.btn_refresh_shot_report.config(state="disabled")
        self.lbl_shot_summary.configure(text=f"分析中：{self._short_path(source_dir, max_len=80)}")
        self.update_idletasks()

        def worker():
            try:
                report = analyze_shot_directory(
                    source_dir=source_dir,
                    role=role,
                    recursive=True,
                    max_videos=max_videos,
                    use_vlm=use_vlm,
                    api_key=self.entry_llm_key.get().strip(),
                    vlm_model=self.entry_shot_vlm_model.get().strip(),
                    base_url=self._get_llm_base_url(),
                    min_shot_duration=min_duration,
                    diff_threshold=diff_threshold,
                    force_refresh=force_refresh,
                )
                self.after(0, lambda: self._on_shot_analysis_finished(report, None))
            except Exception as exc:
                self.after(0, lambda: self._on_shot_analysis_finished(None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_shot_analysis_finished(self, report, error):
        self.btn_run_shot_analysis.config(state="normal")
        self.btn_refresh_shot_report.config(state="normal")
        if error is not None:
            self.lbl_shot_summary.configure(text="镜头分析失败")
            self._log_nonfatal_issue("shot_understanding", f"镜头分析失败: {error}", exc=error)
            messagebox.showerror("镜头分析失败", str(error))
            return
        self._render_shot_report(report)
        warnings = report.get("warnings", []) or []
        if warnings:
            self._log_nonfatal_issue("shot_understanding", f"镜头分析完成，但有 {len(warnings)} 条提示信息")

    def _render_shot_report(self, report):
        self._latest_shot_report = report or {}
        self._shot_tree_rows = {}
        for item_id in self.tree_shots.get_children():
            self.tree_shots.delete(item_id)

        for video_report in report.get("videos", []) or []:
            source_name = video_report.get("source_name", "")
            for shot in video_report.get("shots", []) or []:
                item_id = self.tree_shots.insert(
                    "",
                    "end",
                    values=(
                        source_name,
                        f"#{shot.get('shot_index', '-')}",
                        f"{shot.get('start_seconds', 0):.1f}s - {shot.get('end_seconds', 0):.1f}s",
                        shot.get("subject_type", "-"),
                        shot.get("shot_scale", "-"),
                        shot.get("shot_purpose", "-"),
                        f"{float(shot.get('shot_purpose_confidence', 0.0)):.2f}",
                        shot.get("description_source", "heuristic"),
                    ),
                )
                self._shot_tree_rows[item_id] = {
                    "video_report": video_report,
                    "shot": shot,
                }

        summary = (
            f"最近一次分析：视频 {report.get('video_count', 0)} 条，镜头 {report.get('shot_count', 0)} 段，"
            f"错误 {len(report.get('errors', []) or [])} 条，提示 {len(report.get('warnings', []) or [])} 条。"
        )
        self.lbl_shot_summary.configure(text=summary)
        self._refresh_shot_source_state()
        children = self.tree_shots.get_children()
        if children:
            first_id = children[0]
            self.tree_shots.selection_set(first_id)
            self.tree_shots.focus(first_id)
            self.on_shot_tree_selected()
        else:
            self.lbl_shot_preview_image.configure(text="当前报告中没有镜头", image="")
            self.lbl_shot_preview_meta.configure(text="-")
            self.lbl_shot_preview_tags.configure(text="-")
            self.lbl_shot_preview_desc.configure(text="-")

    def on_shot_tree_selected(self, _event=None):
        selection = self.tree_shots.selection()
        if not selection:
            return
        payload = self._shot_tree_rows.get(selection[0]) or {}
        video_report = payload.get("video_report", {})
        shot = payload.get("shot", {})
        thumb_path = shot.get("thumbnail_path", "")
        self._shot_preview_image = None
        if thumb_path and os.path.exists(thumb_path):
            try:
                image = Image.open(thumb_path)
                image.thumbnail((440, 248))
                self._shot_preview_image = ImageTk.PhotoImage(image)
                self.lbl_shot_preview_image.configure(image=self._shot_preview_image, text="")
            except Exception:
                self.lbl_shot_preview_image.configure(text="缩略图加载失败", image="")
        else:
            self.lbl_shot_preview_image.configure(text="未找到镜头缩略图", image="")

        self.lbl_shot_preview_meta.configure(
            text=(
                f"视频: {video_report.get('source_name', '-')}\n"
                f"时间: {shot.get('start_seconds', 0):.2f}s - {shot.get('end_seconds', 0):.2f}s\n"
                f"主体: {shot.get('subject_type', '-')}\n"
                f"景别: {shot.get('shot_scale', '-')}\n"
                f"用途: {shot.get('shot_purpose', '-')} (置信度 {float(shot.get('shot_purpose_confidence', 0.0)):.2f})\n"
                f"时长: {shot.get('duration_seconds', 0):.2f}s | 运动强度: {shot.get('motion_score', 0):.3f}\n"
                f"质量分: {float(shot.get('aes_score', 0.0)):.2f}"
            )
        )
        candidates = shot.get("shot_purpose_candidates", {}) or {}
        candidate_text = " / ".join(
            f"{name}:{score:.2f}" for name, score in sorted(candidates.items(), key=lambda item: item[1], reverse=True)[:3]
        ) or "-"
        evidence_text = "；".join(shot.get("evidence", []) or ["无"])
        self.lbl_shot_preview_tags.configure(
            text=(
                f"标签: {' / '.join(shot.get('tags', []) or ['-'])}\n"
                f"显式线索: {' / '.join(shot.get('explicit_tags', []) or ['无'])}\n"
                f"用途候选: {candidate_text}\n"
                f"判断依据: {evidence_text}\n"
                f"描述来源: {shot.get('description_source', '-')}\n"
                f"镜头说明: {shot.get('caption', shot.get('description', '-'))}"
            )
        )
        self.lbl_shot_preview_desc.configure(text=f"镜头描述: {shot.get('description', '-')}")

    def refresh_latest_shot_report(self, silent=False):
        report = load_latest_report()
        if not report:
            if not silent:
                messagebox.showinfo("提示", "当前还没有镜头分析结果，请先点击“开始分析镜头”。")
            return
        self._render_shot_report(report)

    def open_shot_cache_folder(self):
        cache_root = ensure_shot_output_root()
        if os.path.exists(cache_root):
            os.startfile(cache_root)
        else:
            messagebox.showwarning("警告", f"未找到镜头缓存目录: {cache_root}")

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

        self.lbl_runtime_target_status = ttk.Label(
            frame,
            text="",
            style="Info.TLabel",
        )
        self.lbl_runtime_target_status.pack(anchor="w", pady=(0, 5))

        content_pane = ttk.Panedwindow(frame, orient=tk.VERTICAL)
        content_pane.pack(fill="both", expand=True)

        top_frame = ttk.Frame(content_pane)
        bottom_frame = ttk.Frame(content_pane)
        content_pane.add(top_frame, weight=3)
        content_pane.add(bottom_frame, weight=4)

        columns = ("id", "time", "status", "detail")
        self.tree = ttk.Treeview(top_frame, columns=columns, show="headings", height=10, selectmode="extended")
        self.tree.heading("id", text="任务 ID")
        self.tree.column("id", width=80, anchor="center")
        self.tree.heading("time", text="提交时间")
        self.tree.column("time", width=150, anchor="center")
        self.tree.heading("status", text="当前状态")
        self.tree.column("status", width=120, anchor="center")
        self.tree.heading("detail", text="日志/详情")
        self.tree.column("detail", width=450, anchor="w")
        task_scrollbar = ttk.Scrollbar(top_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=task_scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        task_scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_task_tree_selected)

        self.tree.tag_configure('running', background=self._colors['accent_bg'])
        self.tree.tag_configure('ready',   background=self._colors['green_bg'])
        self.tree.tag_configure('failed',  background=self._colors['red_bg'])
        self.tree.tag_configure('queued',  background=self._colors['fill_sec'])

        preview_header = ttk.Frame(bottom_frame)
        preview_header.pack(fill="x", pady=(6, 6))
        self.lbl_task_preview_summary = ttk.Label(preview_header, text="请选择上方任务查看命中片段预览", style="Info.TLabel")
        self.lbl_task_preview_summary.pack(side="left", anchor="w")
        ttk.Checkbutton(
            preview_header,
            text="只看镜头级命中",
            variable=self.var_task_hits_only_shot,
            command=self.on_task_hit_filter_changed,
            style='Card.TCheckbutton',
        ).pack(side="right", padx=4)
        ttk.Checkbutton(
            preview_header,
            text="只看需剪映复查",
            variable=self.var_task_hits_need_review,
            command=self.on_task_hit_filter_changed,
            style='Card.TCheckbutton',
        ).pack(side="right", padx=4)
        self.btn_task_hit_next = ttk.Button(preview_header, text="下一条", style='Secondary.TButton', command=self.select_next_task_hit, state="disabled")
        self.btn_task_hit_next.pack(side="right", padx=4)
        self.btn_task_hit_prev = ttk.Button(preview_header, text="上一条", style='Secondary.TButton', command=self.select_prev_task_hit, state="disabled")
        self.btn_task_hit_prev.pack(side="right", padx=4)
        self.btn_open_task_report = ttk.Button(preview_header, text="打开报告", style='Secondary.TButton', command=self.open_selected_task_report, state="disabled")
        self.btn_open_task_report.pack(side="right", padx=4)
        self.btn_open_task_log = ttk.Button(preview_header, text="打开日志", style='Secondary.TButton', command=self.open_selected_task_log, state="disabled")
        self.btn_open_task_log.pack(side="right", padx=4)
        self.btn_open_task_draft = ttk.Button(preview_header, text="打开草稿复查", style='Secondary.TButton', command=self.open_selected_task_draft, state="disabled")
        self.btn_open_task_draft.pack(side="right", padx=4)

        preview_pane = ttk.Panedwindow(bottom_frame, orient=tk.HORIZONTAL)
        preview_pane.pack(fill="both", expand=True)

        left_preview = ttk.Frame(preview_pane)
        right_preview = ttk.Frame(preview_pane)
        preview_pane.add(left_preview, weight=3)
        preview_pane.add(right_preview, weight=2)

        hit_columns = ("time", "type", "source", "review", "level", "score", "shot")
        self.tree_task_hits = ttk.Treeview(left_preview, columns=hit_columns, show="headings", height=10)
        self.tree_task_hits.heading("time", text="插入时间")
        self.tree_task_hits.column("time", width=110, anchor="center")
        self.tree_task_hits.heading("type", text="语义类型")
        self.tree_task_hits.column("type", width=80, anchor="center")
        self.tree_task_hits.heading("source", text="命中来源")
        self.tree_task_hits.column("source", width=90, anchor="center")
        self.tree_task_hits.heading("review", text="复查建议")
        self.tree_task_hits.column("review", width=110, anchor="center")
        self.tree_task_hits.heading("level", text="命中层级")
        self.tree_task_hits.column("level", width=180, anchor="center")
        self.tree_task_hits.heading("score", text="语义分")
        self.tree_task_hits.column("score", width=70, anchor="center")
        self.tree_task_hits.heading("shot", text="素材/片段")
        self.tree_task_hits.column("shot", width=360, anchor="w")
        hit_scrollbar = ttk.Scrollbar(left_preview, orient="vertical", command=self.tree_task_hits.yview)
        self.tree_task_hits.configure(yscrollcommand=hit_scrollbar.set)
        self.tree_task_hits.pack(side="left", fill="both", expand=True)
        hit_scrollbar.pack(side="right", fill="y")
        self.tree_task_hits.bind("<<TreeviewSelect>>", self.on_task_hit_selected)
        self.tree_task_hits.tag_configure('review_high', background=self._colors['red_bg'])
        self.tree_task_hits.tag_configure('review_medium', background=self._colors['accent_bg'])
        self.tree_task_hits.tag_configure('review_low', background=self._colors['green_bg'])

        self.lbl_task_preview_image = ttk.Label(right_preview, text="请选择左侧命中条目查看片段预览", anchor="center")
        self.lbl_task_preview_image.pack(fill="x", padx=8, pady=(8, 6))
        self.lbl_task_preview_meta = ttk.Label(right_preview, text="-", style="Info.TLabel", justify="left")
        self.lbl_task_preview_meta.pack(anchor="w", padx=8, pady=(0, 6))
        self.lbl_task_preview_tags = ttk.Label(right_preview, text="-", style="Info.TLabel", justify="left", wraplength=420)
        self.lbl_task_preview_tags.pack(anchor="w", padx=8, pady=(0, 6))
        self.lbl_task_preview_desc = ttk.Label(right_preview, text="-", justify="left", wraplength=420)
        self.lbl_task_preview_desc.pack(anchor="w", padx=8, pady=(0, 8))
        self.lbl_task_preview_breakdown = ttk.Label(right_preview, text="-", style="Info.TLabel", justify="left", wraplength=420)
        self.lbl_task_preview_breakdown.pack(anchor="w", padx=8, pady=(0, 8))

        runtime_log_card = ttk.LabelFrame(right_preview, text="实时运行日志", style="Card.TLabelframe")
        runtime_log_card.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.txt_task_runtime_log = ScrolledText(
            runtime_log_card,
            wrap="word",
            font=self._fonts['body'],
            height=12,
            relief="flat",
            borderwidth=0,
            background="#ffffff",
            foreground=self._colors['text'],
        )
        self.txt_task_runtime_log.pack(fill="both", expand=True, padx=8, pady=8)
        self.txt_task_runtime_log.configure(state="disabled")
        self._set_task_runtime_log_text("请选择上方任务查看当前任务的实时运行日志。")

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

        self.btn_clear_finished = ttk.Button(
            control_frame,
            text="清空已结束任务",
            style='Secondary.TButton',
            command=self.clear_finished_tasks,
            state="disabled",
        )
        self.btn_clear_finished.pack(side="left", padx=5)

        self.btn_task_select_all = ttk.Button(
            control_frame,
            text="全选任务",
            style='Secondary.TButton',
            command=self.select_all_tasks,
            state="disabled",
        )
        self.btn_task_select_all.pack(side="left", padx=5)

        self.btn_task_select_inverse = ttk.Button(
            control_frame,
            text="反选任务",
            style='Secondary.TButton',
            command=self.invert_task_selection,
            state="disabled",
        )
        self.btn_task_select_inverse.pack(side="left", padx=5)

        self.btn_purge_task_drafts = ttk.Button(
            control_frame,
            text="物理清除草稿",
            style='Secondary.TButton',
            command=self.purge_selected_task_drafts,
            state="disabled",
        )
        self.btn_purge_task_drafts.pack(side="left", padx=5)

    def build_analysis_tab(self):
        frame = self.tab_analysis
        ttk.Label(
            frame,
            text="业务分析面板：把核心业务逻辑、验证结果、运行环境和草稿治理统一展示给同事。",
            style="Info.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        top_actions = ttk.Frame(frame)
        top_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(
            top_actions,
            text="刷新业务摘要",
            style="Primary.TButton",
            command=self.refresh_analysis_panel,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            top_actions,
            text="运行环境自检",
            style="Secondary.TButton",
            command=self.run_analysis_preflight,
        ).pack(side="left", padx=6)
        ttk.Button(
            top_actions,
            text="打开批跑状态",
            style="Secondary.TButton",
            command=lambda: self._open_if_exists(os.path.join(self.output_dir, "batch_run_state.json"), "批跑状态文件"),
        ).pack(side="left", padx=6)
        ttk.Button(
            top_actions,
            text="打开镜头验证",
            style="Secondary.TButton",
            command=lambda: self._open_if_exists(os.path.join(self.output_dir, "shot_recall_validation_summary.json"), "镜头验证汇总"),
        ).pack(side="left", padx=6)
        ttk.Button(
            top_actions,
            text="打开素材池报告",
            style="Secondary.TButton",
            command=lambda: self._open_if_exists(os.path.join(self.output_dir, "material_pool_validation.json"), "素材池报告"),
        ).pack(side="left", padx=6)

        summary_card = ttk.LabelFrame(frame, text="业务摘要", style="Card.TLabelframe")
        summary_card.pack(fill="x", pady=(0, 8))
        self.lbl_analysis_summary = ttk.Label(summary_card, text="尚未加载业务摘要", style="Info.TLabel", justify="left")
        self.lbl_analysis_summary.pack(anchor="w", padx=8, pady=8)

        runtime_card = ttk.LabelFrame(frame, text="运行与草稿治理", style="Card.TLabelframe")
        runtime_card.pack(fill="x", pady=(0, 8))
        runtime_actions = ttk.Frame(runtime_card)
        runtime_actions.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(
            runtime_actions,
            text="修复草稿索引",
            style="Secondary.TButton",
            command=self.run_analysis_repair_registry,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            runtime_actions,
            text="打开草稿目录",
            style="Secondary.TButton",
            command=self.open_draft_folder,
        ).pack(side="left", padx=6)
        ttk.Button(
            runtime_actions,
            text="打开结果目录",
            style="Secondary.TButton",
            command=self.open_output_folder,
        ).pack(side="left", padx=6)
        self.lbl_analysis_runtime = ttk.Label(runtime_card, text="尚未加载运行状态", style="Info.TLabel", justify="left")
        self.lbl_analysis_runtime.pack(anchor="w", padx=8, pady=(0, 8))

        details_card = ttk.LabelFrame(frame, text="业务说明与结果明细", style="Card.TLabelframe")
        details_card.pack(fill="both", expand=True)
        self.txt_analysis = ScrolledText(
            details_card,
            wrap="word",
            font=self._fonts['body'],
            height=20,
            relief="flat",
            borderwidth=0,
            background="#ffffff",
            foreground=self._colors['text'],
        )
        self.txt_analysis.pack(fill="both", expand=True, padx=8, pady=8)
        self.txt_analysis.configure(state="disabled")
        self.refresh_analysis_panel()

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

    def _material_dir_aliases(self):
        return {
            "speech": ("口播",),
            "product": ("产品",),
            "symptom": ("病症",),
            "audio": ("音效",),
            "bgm": ("背景音乐", "my_bg_music"),
            "ad_review": ("广审素材", "广审"),
            "sticker": ("顶部贴图", "贴图"),
        }

    def _discover_material_dirs(self, base_dir):
        base_dir = str(base_dir or "").strip()
        discovered = {}
        if not base_dir or not os.path.isdir(base_dir):
            return discovered

        alias_map = self._material_dir_aliases()
        candidates = [(base_dir, 0)]
        try:
            for current_root, dirs, _files in os.walk(base_dir):
                rel_path = os.path.relpath(current_root, base_dir)
                depth = 0 if rel_path == "." else len(rel_path.split(os.sep))
                if depth > 3:
                    dirs[:] = []
                    continue
                candidates.append((current_root, depth))
        except OSError:
            return discovered

        for current_root, depth in sorted(candidates, key=lambda item: item[1]):
            name = os.path.basename(current_root.rstrip("\\/"))
            for key, aliases in alias_map.items():
                if key in discovered:
                    continue
                if any(alias in name or name in alias for alias in aliases):
                    discovered[key] = current_root
        return discovered

    def _build_llm_env(self, env):
        env["LLM_API_KEY"] = self.entry_llm_key.get().strip()
        env["LLM_BASE_URL"] = self._get_llm_base_url()
        env["LLM_MODEL"] = normalize_model_name(self.entry_llm_model.get().strip())
        return env

    def _build_material_path_env(self, env):
        env = dict(env or {})
        managed_keys = [
            "OTC_VIDEO_DIR",
            "OTC_SPEECH_DIR",
            "OTC_PRODUCT_DIR",
            "OTC_SYMPTOM_DIR",
            "OTC_AUDIO_DIR",
            "OTC_BGM_DIR",
            "OTC_USER_BGM_DIR",
            "OTC_AD_REVIEW_DIR",
            "OTC_STICKER_DIR",
        ]
        for key in managed_keys:
            env.pop(key, None)

        unified_root = ""
        if "unified" in getattr(self, "entries", {}):
            candidate_root = self.entries["unified"].get().strip()
            if candidate_root and os.path.isdir(candidate_root):
                unified_root = candidate_root

        discovered_dirs = self._discover_material_dirs(unified_root) if unified_root else {}
        path_map = {
            "speech": "OTC_SPEECH_DIR",
            "product": "OTC_PRODUCT_DIR",
            "symptom": "OTC_SYMPTOM_DIR",
            "audio": "OTC_AUDIO_DIR",
            "bgm": "OTC_USER_BGM_DIR",
            "ad_review": "OTC_AD_REVIEW_DIR",
            "sticker": "OTC_STICKER_DIR",
        }
        resolved_dirs = {}
        for entry_key, env_key in path_map.items():
            value = ""
            if entry_key in getattr(self, "entries", {}):
                value = self.entries[entry_key].get().strip()
            discovered_value = discovered_dirs.get(entry_key, "")
            if unified_root and discovered_value:
                if not value:
                    value = discovered_value
                else:
                    try:
                        same_tree = os.path.commonpath([
                            os.path.abspath(value),
                            os.path.abspath(unified_root),
                        ]) == os.path.abspath(unified_root)
                    except ValueError:
                        same_tree = False
                    if not same_tree:
                        value = discovered_value
            elif not value:
                value = discovered_value
            if value:
                env[env_key] = value
                resolved_dirs[entry_key] = value

        if unified_root:
            env["OTC_VIDEO_DIR"] = unified_root

        # BGM 主目录和用户 BGM 目录优先跟随当前主页面输入；设置页路径仅作为兜底。
        if env.get("OTC_USER_BGM_DIR"):
            env["OTC_BGM_DIR"] = env["OTC_USER_BGM_DIR"]
        elif hasattr(self, "entry_user_bgm_dir"):
            fallback_bgm_dir = self.entry_user_bgm_dir.get().strip()
            if fallback_bgm_dir:
                env["OTC_USER_BGM_DIR"] = fallback_bgm_dir
                env["OTC_BGM_DIR"] = fallback_bgm_dir

        existing_dirs = [
            os.path.abspath(path)
            for path in resolved_dirs.values()
            if path and os.path.isdir(path)
        ]
        if len(existing_dirs) >= 2:
            try:
                common_root = os.path.commonpath(existing_dirs)
            except ValueError:
                common_root = ""
            if common_root and os.path.isdir(common_root):
                drive, tail = os.path.splitdrive(os.path.abspath(common_root))
                if tail.strip("\\/"):
                    env["OTC_VIDEO_DIR"] = common_root

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
        draft_info = self._resolve_selected_draft_root_info()
        env["OTC_DRAFT_MODE"] = self._get_selected_draft_mode()
        resolved_draft_root = self._resolve_worker_draft_root() or str(draft_info.get("resolved_root", "")).strip()
        if self._get_selected_draft_mode() == DRAFT_MODE_PORTABLE and resolved_draft_root:
            env["OTC_DRAFT_ROOT"] = resolved_draft_root
        else:
            env.pop("OTC_DRAFT_ROOT", None)
        if hasattr(self, "scale_bgm_crossfade"):
            env["OTC_BGM_CROSSFADE_MS"] = str(int(self.scale_bgm_crossfade.get()))
        if hasattr(self, "scale_bgm_lufs"):
            env["OTC_BGM_TARGET_LUFS"] = str(int(self.scale_bgm_lufs.get()))
        if hasattr(self, "combo_bgm_pick_mode"):
            env["OTC_BGM_PICK_MODE"] = self.combo_bgm_pick_mode.get().strip()
        if hasattr(self, "entry_user_bgm_dir") and not env.get("OTC_USER_BGM_DIR"):
            env["OTC_USER_BGM_DIR"] = self.entry_user_bgm_dir.get().strip()
        if env.get("OTC_USER_BGM_DIR") and not env.get("OTC_BGM_DIR"):
            env["OTC_BGM_DIR"] = env["OTC_USER_BGM_DIR"]
        if hasattr(self, "var_bgm_normalize"):
            env["OTC_BGM_NORMALIZE"] = "1" if self.var_bgm_normalize.get() else "0"
        if hasattr(self, "var_bgm_phase_check"):
            env["OTC_BGM_PHASE_CHECK"] = "1" if self.var_bgm_phase_check.get() else "0"
        if hasattr(self, "var_main_shot_recall_enabled"):
            env["OTC_ENABLE_SHOT_RECALL"] = "1" if self.var_main_shot_recall_enabled.get() else "0"
        if hasattr(self, "var_main_shot_use_vlm"):
            env["OTC_SHOT_RECALL_USE_VLM"] = "1" if self.var_main_shot_use_vlm.get() else "0"
        if hasattr(self, "var_main_shot_force_refresh"):
            env["OTC_SHOT_RECALL_FORCE_REFRESH"] = "1" if self.var_main_shot_force_refresh.get() else "0"
        if hasattr(self, "var_main_strict_semantic"):
            env["OTC_STRICT_SEMANTIC_INSERTION"] = "1" if self.var_main_strict_semantic.get() else "0"
        if hasattr(self, "entry_main_shot_vlm_model"):
            env["OTC_SHOT_RECALL_VLM_MODEL"] = self.entry_main_shot_vlm_model.get().strip()
        if hasattr(self, "entry_main_shot_max_videos"):
            env["OTC_SHOT_RECALL_MAX_VIDEOS"] = self.entry_main_shot_max_videos.get().strip() or "0"
        if hasattr(self, "entry_main_shot_top_n"):
            env["OTC_SHOT_RECALL_TOP_N"] = self.entry_main_shot_top_n.get().strip() or "24"
        return env

    def _set_button_state(self, button, state):
        cache_key = str(button)
        if self._last_button_states.get(cache_key) != state:
            button.config(state=state)
            self._last_button_states[cache_key] = state

    def _run_worker_preflight(self):
        preflight_env = build_runtime_env(os.environ.copy())
        preflight_env = self._build_material_path_env(preflight_env)
        preflight_env = self._apply_advanced_settings_env(preflight_env)
        result = run_hidden(
            get_worker_command(["--preflight"]),
            env=preflight_env,
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

        report = json.loads(payload)
        try:
            snapshot_path = os.path.join(self.output_dir, "worker_preflight_latest.json")
            snapshot_payload = {
                "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "env": {
                    "OTC_DRAFT_MODE": preflight_env.get("OTC_DRAFT_MODE", ""),
                    "OTC_DRAFT_ROOT": preflight_env.get("OTC_DRAFT_ROOT", ""),
                    "LOCALAPPDATA": preflight_env.get("LOCALAPPDATA", ""),
                    "USERPROFILE": preflight_env.get("USERPROFILE", ""),
                },
                "report": report,
            }
            with open(snapshot_path, "w", encoding="utf-8") as f:
                json.dump(snapshot_payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return report

    def _ensure_runtime_ready(self):
        report = self._run_worker_preflight()
        self._latest_preflight_report = report
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

    def _warn_portable_draft_fallback_before_submit(self):
        report = getattr(self, "_latest_preflight_report", None)
        checks = report.get("checks", {}) if isinstance(report, dict) else {}
        using_portable = bool(checks.get("using_portable_draft_root"))
        if not using_portable:
            return True
        portable_root = str(checks.get("draft_root", "")).strip()
        detected_version = str(checks.get("detected_jianying_version", "") or "").strip()
        if detected_version:
            message = (
                f"已检测到剪映 {detected_version}，但系统草稿目录不可写。\n\n"
                f"将使用便携草稿目录继续生成：\n{portable_root}\n\n"
                "注意：生成的草稿不会自动出现在剪映首页。\n"
                "你可以稍后通过“草稿管理 -> 同步到剪映”手动同步。\n\n"
                "是否仍然继续生成？"
            )
        else:
            message = (
                "未检测到剪映安装，将使用便携草稿目录。\n\n"
                f"当前便携目录：\n{portable_root}\n\n"
                "注意：生成的草稿不会自动出现在剪映首页。\n"
                "如需在剪映中直接打开，请安装剪映专业版。\n\n"
                "是否仍然继续生成？"
            )
        return messagebox.askyesno("草稿目录提示", message)

    def test_llm_connectivity(self):
        api_key = self.entry_llm_key.get().strip()
        model = normalize_model_name(self.entry_llm_model.get().strip() or DEFAULT_LLM_MODEL)
        base_url = self._get_llm_base_url()
        self.entry_llm_model.delete(0, tk.END)
        self.entry_llm_model.insert(0, model)
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
                base_url=base_url,
            )
            if ok:
                messagebox.showinfo("联通成功", message)
            else:
                messagebox.showerror("联通失败", message)
        except Exception as exc:
            messagebox.showerror("联通失败", f"联通测试异常: {exc}")
        finally:
            self.btn_test_llm.config(state="normal")

    def test_vision_connectivity(self):
        api_key = self.entry_llm_key.get().strip()
        model = normalize_model_name(self.entry_main_shot_vlm_model.get().strip())
        base_url = self._get_llm_base_url()
        if hasattr(self, "entry_main_shot_vlm_model"):
            self.entry_main_shot_vlm_model.delete(0, tk.END)
            self.entry_main_shot_vlm_model.insert(0, model)
        if not api_key:
            messagebox.showwarning("提示", "请先填写 API Key。")
            return
        if not model:
            messagebox.showwarning("提示", "请先填写视觉模型名称。")
            return

        self._save_ui_settings()
        self.btn_test_vision.config(state="disabled")
        self.update_idletasks()
        try:
            import llm_clip_matcher

            ok, message = llm_clip_matcher.test_vision_connectivity(
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
            if ok:
                messagebox.showinfo("视觉联通成功", message)
            else:
                messagebox.showerror("视觉联通失败", message)
        except Exception as exc:
            messagebox.showerror("视觉联通失败", f"视觉联通测试异常: {exc}")
        finally:
            self.btn_test_vision.config(state="normal")

    def submit_batch_tasks(self):
        is_valid, msg = self.validate_paths()
        if not is_valid:
            messagebox.showerror("路径错误", msg)
            return
        self._save_ui_settings()
        if not self._validate_mainflow_semantic_requirements():
            return
        if not self._ensure_runtime_ready():
            return
        if not self._warn_portable_draft_fallback_before_submit():
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

        # 每次提交都基于当前界面输入重建素材路径环境，避免沿用上一轮旧素材目录。
        env = self._build_material_path_env(os.environ.copy())

        sensitivity = self.combo_sens.get().split()[0]
        env["OTC_AD_FREQ"] = str(self.scale_ad_freq.get())
        env["OTC_STICKER_FREQ"] = str(self.scale_sticker_freq.get())
        env["OTC_BROLL_FREQ"] = str(self.scale_broll_freq.get())
        
        env = self._apply_advanced_settings_env(env)
        env = self._build_llm_env(env)
        
        if len(speech_videos) > self.task_queue_capacity:
            messagebox.showerror("错误", f"当前批量任务数超过队列容量上限 {self.task_queue_capacity}，请分批提交。")
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

    def _sync_task_counter_from_running_tasks(self):
        max_task_num = 0
        with self._tasks_lock:
            for task_id in self.running_tasks.keys():
                try:
                    max_task_num = max(max_task_num, int(str(task_id).split("-", 1)[1]))
                except Exception:
                    continue
        self.task_count = max_task_num

    def clear_finished_tasks(self):
        removable_statuses = {STATUS_READY, STATUS_FAILED, STATUS_ERROR}
        with self._tasks_lock:
            removable_ids = [
                task_id for task_id, info in self.running_tasks.items()
                if str(info.get("status", "")).strip() in removable_statuses
            ]
            for task_id in removable_ids:
                self.running_tasks.pop(task_id, None)
        if not removable_ids:
            messagebox.showinfo("无需清理", "当前没有可清空的已结束任务。")
            return
        self._sync_task_counter_from_running_tasks()
        self._write_task_report_snapshot()
        for task_id in list(self.tree.get_children()):
            if task_id not in self.running_tasks:
                self.tree.delete(task_id)
        self._task_hit_all_rows = []
        self._task_hit_rows = {}
        self._clear_task_hit_preview("已清空已结束任务，请重新选择正在运行中的任务查看详情")
        self.on_task_tree_selected()
        self._update_task_selection_actions()
        messagebox.showinfo("已清空", f"已清空 {len(removable_ids)} 条已结束任务记录。")

    def select_all_tasks(self):
        if not hasattr(self, "tree"):
            return
        children = list(self.tree.get_children())
        if not children:
            return
        self.tree.selection_set(children)
        self.tree.focus(children[0])
        self.on_task_tree_selected()

    def invert_task_selection(self):
        if not hasattr(self, "tree"):
            return
        children = list(self.tree.get_children())
        if not children:
            return
        selected = set(self._get_selected_task_ids())
        inverted = [task_id for task_id in children if task_id not in selected]
        if inverted:
            self.tree.selection_set(inverted)
            self.tree.focus(inverted[0])
        else:
            self.tree.selection_remove(children)
        self.on_task_tree_selected()

    def purge_selected_task_drafts(self):
        selected_ids = self._get_selected_task_ids()
        if not selected_ids:
            messagebox.showinfo("提示", "请先在任务看板里选择要清理草稿的任务。")
            return

        tasks = self._snapshot_running_tasks()
        grouped_names = {}
        skipped = []
        for task_id in selected_ids:
            info = tasks.get(task_id, {})
            draft_dir = str(info.get("draft_dir", "")).strip()
            draft_name = os.path.basename(draft_dir) if draft_dir else ""
            draft_root = os.path.dirname(draft_dir) if draft_dir else ""
            if draft_name.startswith("OTC推广_") and draft_root:
                grouped_names.setdefault(draft_root, set()).add(draft_name)
            else:
                skipped.append(task_id)

        if not grouped_names:
            messagebox.showinfo("提示", "选中的任务里没有可物理清理的托管草稿。")
            return

        draft_count = sum(len(names) for names in grouped_names.values())
        tip = f"将物理删除 {draft_count} 个草稿目录，并同步更新剪映索引。删除后不会自动恢复。"
        if skipped:
            tip += f"\n有 {len(skipped)} 条任务当前没有可清理草稿，会自动跳过。"
        confirmed = messagebox.askyesno("确认物理清除", tip)
        if not confirmed:
            return

        report_root = os.path.join(self.output_dir, "draft_purge_reports")
        os.makedirs(report_root, exist_ok=True)
        removed_names = set()
        for draft_root, names in grouped_names.items():
            root_label = re.sub(r'[<>:"/\\|?*]+', "_", os.path.basename(draft_root) or "draft_root")
            report_path = os.path.join(
                report_root,
                f"purge_{root_label}_{int(time.time())}.json",
            )
            delete_drafts_permanently(
                draft_root=draft_root,
                draft_names=tuple(sorted(names)),
                project_prefixes=("OTC推广_",),
                remove_from_recycle=True,
                report_path=report_path,
                lock_path=os.path.join(self.output_dir, ".draft_delete.lock"),
            )
            removed_names.update(names)

        with self._tasks_lock:
            for task_id in selected_ids:
                info = self.running_tasks.get(task_id)
                if not info:
                    continue
                draft_dir = str(info.get("draft_dir", "")).strip()
                draft_name = os.path.basename(draft_dir) if draft_dir else ""
                if draft_name not in removed_names:
                    continue
                info["draft_dir"] = ""
                log_text = str(info.get("log", "")).strip()
                if "草稿已物理清理" not in log_text:
                    info["log"] = (log_text + " | 草稿已物理清理").strip(" |")
        self._write_task_report_snapshot()
        self.on_task_tree_selected()
        self._update_task_selection_actions()
        messagebox.showinfo("已清理", f"已物理清除 {len(removed_names)} 个草稿。后续打开剪映时不会再自动恢复这些草稿。")

    def execute_task(self, task_id, env, sensitivity, video_file, retry_count=0):
        max_retries = self.batch_retry_limit
        with self._tasks_lock:
            self.running_tasks[task_id] = {
                "status": STATUS_RUNNING,
                "log": f"正在处理: {video_file} (重试: {retry_count})",
                "file": video_file,
                "live_output": f"[任务] {video_file}\n[状态] 正在启动生成进程...\n",
            }

        try:
            task_env = build_runtime_env(env)
            draft_root = str(task_env.get("OTC_DRAFT_ROOT", "")).strip() or get_draft_root_info(task_env.get("OTC_DRAFT_MODE")).get("resolved_root", "")

            command = get_worker_command(["--sensitivity", sensitivity, "--video", video_file])
            stdout_chunks = []
            stderr_chunks = []

            def _append_live_output(text, prefix=""):
                text = repair_mojibake_text(str(text or "").replace("\r", "\n"))
                lines = [line.rstrip() for line in text.splitlines() if line.strip()]
                if not lines:
                    return
                rendered = "".join(f"{prefix}{line}\n" for line in lines)
                with self._tasks_lock:
                    info = self.running_tasks.get(task_id)
                    if not info:
                        return
                    merged = f"{info.get('live_output', '')}{rendered}"
                    info["live_output"] = merged[-12000:]
                    info["log"] = lines[-1][-220:]

            def _read_stream(stream, chunks, prefix=""):
                if stream is None:
                    return
                try:
                    for line in iter(stream.readline, ""):
                        if not line:
                            break
                        fixed = repair_mojibake_text(line)
                        chunks.append(fixed)
                        _append_live_output(fixed, prefix=prefix)
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass

            with self.subprocess_slots:
                process = popen_hidden(
                    command,
                    env=task_env,
                    cwd=runtime_path(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                stdout_thread = threading.Thread(target=_read_stream, args=(process.stdout, stdout_chunks), daemon=True)
                stderr_thread = threading.Thread(target=_read_stream, args=(process.stderr, stderr_chunks, "[STDERR] "), daemon=True)
                stdout_thread.start()
                stderr_thread.start()
                returncode = process.wait()
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)

            result = subprocess.CompletedProcess(
                command,
                returncode,
                stdout=repair_mojibake_text("".join(stdout_chunks)),
                stderr=repair_mojibake_text("".join(stderr_chunks)),
            )
            log_path = self._write_task_log(task_id, video_file, result, env=task_env)

            if result.returncode == 0:
                success_summary = self._summarize_process_output(result, success=True)
                review_report, draft_dir = self._locate_task_artifacts(video_file, draft_root=draft_root)
                draft_sync_result = self._sync_task_draft_to_official_root(draft_dir)
                draft_dir = str(draft_sync_result.get("draft_dir", "")).strip()
                draft_status_note = str(draft_sync_result.get("status_note", "")).strip()
                artifact_summary = []
                if draft_dir:
                    artifact_summary.append(f"草稿: {os.path.basename(draft_dir)}")
                if review_report:
                    artifact_summary.append(f"报告: {os.path.basename(review_report)}")
                if draft_status_note:
                    artifact_summary.append(draft_status_note)
                if not artifact_summary:
                    artifact_summary.append(f"结果目录: {self._short_path(self.output_dir, max_len=40)}")
                with self._tasks_lock:
                    self.running_tasks[task_id] = {
                        "status": STATUS_READY,
                        "log": f"{success_summary} | {' | '.join(artifact_summary)} | 日志: {log_path}",
                        "file": video_file,
                        "result": "SUCCESS",
                        "log_path": log_path,
                        "review_report": review_report,
                        "draft_dir": draft_dir,
                    }
                self._write_task_report_snapshot()
            else:
                if retry_count < max_retries:
                    err_msg = self._summarize_process_output(result, success=False)
                    self._schedule_task_retry(
                        task_id,
                        env,
                        sensitivity,
                        video_file,
                        retry_count,
                        f"任务失败: {err_msg}",
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
                    self._write_task_report_snapshot()
        except Exception as e:
            if retry_count < max_retries:
                self._schedule_task_retry(
                    task_id,
                    env,
                    sensitivity,
                    video_file,
                    retry_count,
                    f"任务异常: {e}",
                )
            else:
                with self._tasks_lock:
                    self.running_tasks[task_id] = {
                        "status": STATUS_ERROR,
                        "log": str(e),
                        "file": video_file,
                        "result": "ERROR",
                    }
                self._write_task_report_snapshot()

    def _read_recent_maintenance_entries(self, max_lines=12):
        if not os.path.exists(self.internal_log_path):
            return []
        try:
            with open(self.internal_log_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            return []
        return lines[-max(1, max_lines):]

    def _read_text_tail(self, path, max_chars=12000):
        path = str(path or "").strip()
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return ""
        text = repair_mojibake_text(decode_process_output(data)).strip()
        if len(text) > max_chars:
            return text[-max_chars:]
        return text

    def _set_task_runtime_log_text(self, text):
        if not hasattr(self, "txt_task_runtime_log"):
            return
        display_text = str(text or "").strip() or "当前还没有可显示的运行日志。"
        if self._last_task_runtime_log_text == display_text:
            return
        self.txt_task_runtime_log.configure(state="normal")
        previous_text = self._last_task_runtime_log_text
        if previous_text and display_text.startswith(previous_text):
            appended = display_text[len(previous_text):]
            if appended:
                self.txt_task_runtime_log.insert(tk.END, appended)
        else:
            self.txt_task_runtime_log.delete("1.0", tk.END)
            self.txt_task_runtime_log.insert("1.0", display_text)
        self.txt_task_runtime_log.see(tk.END)
        self.txt_task_runtime_log.configure(state="disabled")
        self._last_task_runtime_log_text = display_text

    def _refresh_selected_task_runtime_log(self):
        task_id, info = self._get_selected_task_info()
        if not task_id:
            self._set_task_runtime_log_text("请选择上方任务查看当前任务的实时运行日志。")
            return

        live_output = str(info.get("live_output", "")).strip()
        if live_output:
            self._set_task_runtime_log_text(live_output)
            return

        log_path = str(info.get("log_path", "")).strip()
        if log_path and os.path.exists(log_path):
            self._set_task_runtime_log_text(self._read_text_tail(log_path))
            return

        fallback = str(info.get("log", "")).strip()
        self._set_task_runtime_log_text(fallback or f"任务 {task_id} 当前还没有可显示的日志。")

    def _write_task_log(self, task_id, video_file, result, env=None):
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", os.path.splitext(video_file)[0])
        log_path = os.path.join(self.task_logs_dir, f"{task_id}_{safe_name}.log")
        stdout = result.stdout if isinstance(result.stdout, str) else str(result.stdout or "")
        stderr = result.stderr if isinstance(result.stderr, str) else str(result.stderr or "")
        stdout = repair_mojibake_text(stdout)
        stderr = repair_mojibake_text(stderr)
        env = dict(env or {})
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"task_id={task_id}\n")
            f.write(f"video_file={video_file}\n")
            f.write(f"returncode={result.returncode}\n")
            for key in (
                "OTC_DRAFT_MODE",
                "OTC_DRAFT_ROOT",
                "OTC_VIDEO_DIR",
                "OTC_SPEECH_DIR",
                "OTC_PRODUCT_DIR",
                "OTC_SYMPTOM_DIR",
                "OTC_AUDIO_DIR",
                "OTC_USER_BGM_DIR",
                "OTC_AD_REVIEW_DIR",
                "OTC_STICKER_DIR",
            ):
                f.write(f"{key}={env.get(key, '')}\n")
            f.write(f"batch_concurrency={self.batch_concurrency}\n")
            f.write(f"batch_retry_limit={self.batch_retry_limit}\n")
            f.write(f"task_queue_capacity={self.task_queue_capacity}\n")
            f.write("\n[STDOUT]\n")
            f.write(stdout)
            f.write("\n[STDERR]\n")
            f.write(stderr)
            maintenance_lines = self._read_recent_maintenance_entries()
            if maintenance_lines:
                f.write("\n[MAINTENANCE_LOG_TAIL]\n")
                f.write("\n".join(maintenance_lines))
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
        finished = sum(1 for info in tasks.values() if info["status"] in [STATUS_READY, STATUS_FAILED, STATUS_ERROR])

        mode_text = "【全自动批量模式】"
        queue_status_text = f"{mode_text} 正在处理: {active} | 等待中: {waiting} | 失败: {failed} | 总计提交: {self.task_count}"
        if queue_status_text != self._last_queue_status_text:
            self.lbl_queue_status.config(text=queue_status_text)
            self._last_queue_status_text = queue_status_text

        self._update_maintenance_label()
        task_board_visible = self._is_task_board_visible()

        if task_board_visible:
            # 仅在任务看板可见时刷新表格和日志，减少后台页签重绘导致的闪烁。
            for task_id, info in tasks.items():
                if self.tree.exists(task_id):
                    item = self.tree.item(task_id)
                    vals = list(item["values"])
                    if vals[2] != info["status"] or vals[3] != info["log"]:
                        vals[2] = info["status"]
                        vals[3] = info["log"]
                        self.tree.item(task_id, values=vals)
                    self._apply_tree_row_tag(task_id, info)
            selected_ids = self._get_selected_task_ids()
            if not selected_ids:
                children = self.tree.get_children()
                if children:
                    self.tree.selection_set(children[0])
                    self.tree.focus(children[0])
                    self.on_task_tree_selected()
            else:
                current_task_id, _ = self._get_selected_task_info()
                if current_task_id != self._last_selected_task_id:
                    self._last_selected_task_id = current_task_id
                    self.on_task_tree_selected()
                    
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
                self._set_button_state(self.btn_clear_finished, "normal" if finished > 0 else "disabled")
            else:
                self._cleanup_done = False
                self._set_button_state(self.btn_export_json, "disabled")
                self._set_button_state(self.btn_retry_failed, "disabled")
                self._set_button_state(self.btn_clear_finished, "disabled")
        else:
            self._cleanup_done = False
            self._set_button_state(self.btn_export_json, "disabled")
            self._set_button_state(self.btn_retry_failed, "disabled")
            self._set_button_state(self.btn_batch_add, "disabled")
            self._set_button_state(self.btn_clear_finished, "normal" if finished > 0 else "disabled")

        if task_board_visible:
            self._refresh_selected_task_runtime_log()
        self._update_task_selection_actions()
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
            draft_info = self._resolve_selected_draft_root_info()
            active_root = draft_info.get("resolved_root", "") or get_draft_root()
            current_draft_names = self._collect_current_task_draft_names(active_root)
            report_path = os.path.join(self.output_dir, "draft_registry_health.json")
            report = self._reconcile_draft_root_with_fallback(
                draft_root=active_root,
                include_names=current_draft_names,
                report_path=report_path,
                lock_path=os.path.join(self.output_dir, ".root_meta_info.lock"),
            )
            restored = len(report.get("restored_from_recycle", []))
            invalid = len(report.get("invalid_drafts", []))
            removed_stale = len(report.get("removed_stale_managed", []))
            removed_recycle = len(report.get("removed_stale_recycle", []))
            if restored or invalid:
                print(f"草稿索引修复完成: 恢复 {restored} 个，发现无效目录 {invalid} 个。")
            if removed_stale or removed_recycle:
                print(f"草稿修复提示: 主目录清理 {removed_stale} 个，回收目录清理 {removed_recycle} 个。")

            official_root = get_official_draft_root()
            portable_root = draft_info.get("portable_root", "")
            if official_root and portable_root and os.path.abspath(official_root) != os.path.abspath(active_root):
                current_draft_names = self._collect_current_task_draft_names(portable_root) or current_draft_names
                sync_report_path = os.path.join(self.output_dir, "draft_registry_sync_report.json")
                sync_managed_drafts(
                    source_root=portable_root,
                    target_root=official_root,
                    project_prefixes=("OTC推广_",),
                    include_names=tuple(current_draft_names),
                    remove_stale_managed=False,
                    report_path=sync_report_path,
                    lock_path=os.path.join(self.output_dir, ".official_root_meta_info.lock"),
                )
                official_report = self._reconcile_draft_root_with_fallback(
                    draft_root=official_root,
                    include_names=current_draft_names,
                    report_path=os.path.join(self.output_dir, "official_draft_registry_health.json"),
                    lock_path=os.path.join(self.output_dir, ".official_root_meta_info.lock"),
                )
                official_registered = len(official_report.get("registered_drafts", []))
                if official_registered:
                    print(f"系统剪映索引已同步修复: 当前可见草稿 {official_registered} 个。")
        except Exception as exc:
            print(f"草稿索引修复失败: {exc}")

    def retry_failed_tasks(self):
        self._save_ui_settings()
        if not self._ensure_runtime_ready():
            return

        env = self._build_material_path_env(os.environ.copy())

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
        draft_root = self._resolve_selected_draft_root_info().get("resolved_root", "")
        tasks = self._snapshot_running_tasks()
        for task_id, info in tasks.items():
            report.append({
                "draft_id": task_id,
                "file_path": info.get("file", ""),
                "status": info.get("result", "UNKNOWN"),
                "failure_reason": info.get("log", "") if info.get("result") != "SUCCESS" else "",
                "log_path": info.get("log_path", ""),
                "review_report": info.get("review_report", ""),
                "draft_dir": info.get("draft_dir", ""),
                "output_dir": self.output_dir,
                "draft_root": draft_root,
            })
        
        output_path = runtime_path("batch_generation_report.json")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("导出成功", f"执行报告已保存至:\n{output_path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _load_json_file(self, path):
        path = str(path or "").strip()
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _open_if_exists(self, path, label):
        path = str(path or "").strip()
        if path and os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo("提示", f"当前还没有可打开的{label}。")

    def _set_analysis_text(self, text):
        self.txt_analysis.configure(state="normal")
        self.txt_analysis.delete("1.0", "end")
        self.txt_analysis.insert("1.0", text)
        self.txt_analysis.configure(state="disabled")

    def _count_managed_drafts_in_root(self, draft_root):
        draft_root = str(draft_root or "").strip()
        if not draft_root or not os.path.isdir(draft_root):
            return 0
        total = 0
        try:
            for name in os.listdir(draft_root):
                if name.startswith("OTC推广_") and os.path.isdir(os.path.join(draft_root, name)):
                    total += 1
        except Exception:
            return 0
        return total

    def _run_worker_subprocess(self, *worker_args, timeout=180):
        command = get_worker_command(*worker_args)
        completed = run_hidden(
            command,
            cwd=os.getcwd(),
            env=build_runtime_env(os.environ.copy()),
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        stdout = repair_mojibake_text(decode_process_output(completed.stdout))
        stderr = repair_mojibake_text(decode_process_output(completed.stderr))
        return completed.returncode, stdout, stderr

    def _build_analysis_snapshot(self):
        batch_state = self._load_json_file(os.path.join(self.output_dir, "batch_run_state.json"))
        shot_summary = self._load_json_file(os.path.join(self.output_dir, "shot_recall_validation_summary.json"))
        material_report = self._load_json_file(os.path.join(self.output_dir, "material_pool_validation.json"))
        draft_info = self._resolve_selected_draft_root_info()
        draft_root = draft_info.get("resolved_root", "") or get_draft_root()
        root_meta = self._load_json_file(os.path.join(draft_root, "root_meta_info.json"))
        draft_ids = int(root_meta.get("draft_ids", 0) or 0)
        managed_draft_count = self._count_managed_drafts_in_root(draft_root)

        summary = batch_state.get("summary", {}) if isinstance(batch_state, dict) else {}
        meta = batch_state.get("meta", {}) if isinstance(batch_state, dict) else {}
        result_rows = batch_state.get("results", []) if isinstance(batch_state, dict) else []
        latest_success = [
            os.path.basename(str(item.get("draft_dir", "")).strip())
            for item in result_rows
            if isinstance(item, dict) and str(item.get("draft_dir", "")).strip()
        ][:12]

        snapshot = {
            "batch_updated_at": meta.get("updated_at", ""),
            "sensitivity": meta.get("sensitivity", ""),
            "selected_total": int(summary.get("selected_total", 0) or 0),
            "success_count": int(summary.get("success_count", 0) or 0),
            "failed_count": int(summary.get("failed_count", 0) or 0),
            "shot_selected_ratio": float(shot_summary.get("shot_selected_ratio", 0.0) or 0.0),
            "shot_selected_total": int(shot_summary.get("shot_selected_total", 0) or 0),
            "selection_total": int(shot_summary.get("selection_total", 0) or 0),
            "avg_shot_score": float(shot_summary.get("avg_shot_score", 0.0) or 0.0),
            "avg_full_video_score": float(shot_summary.get("avg_full_video_score", 0.0) or 0.0),
            "match_quality": dict(shot_summary.get("match_quality", {}) or {}),
            "product_after": int(material_report.get("product_after", 0) or 0),
            "symptom_after": int(material_report.get("symptom_after", 0) or 0),
            "draft_root": draft_root,
            "draft_mode": DRAFT_MODE_LABELS.get(draft_info.get("requested_mode"), DRAFT_MODE_LABELS[DRAFT_MODE_AUTO]),
            "draft_ids": draft_ids,
            "managed_draft_count": managed_draft_count,
            "latest_success_drafts": latest_success,
        }
        return snapshot

    def _format_analysis_snapshot(self, snapshot):
        headline = (
            f"当前 EXE 已经覆盖完整业务链路：读取口播 -> 字幕/语义理解 -> 镜头级召回 -> 中插规划 -> "
            f"广审/贴图/BGM -> 生成剪映草稿 -> 任务复查。"
        )
        lines = [
            headline,
            "",
            "一、业务逻辑总览",
            "1. 输入层：从口播目录、产品目录、病症目录收集素材，自动过滤非法文件与空素材。",
            "2. 理解层：用字幕文本、语义画像和镜头标签共同决定中插，不再只靠固定关键词。",
            "3. 召回层：优先尝试镜头级素材命中，命不中再回退整条素材，并记录 score_breakdown。",
            "4. 生成层：自动落广审、顶部贴图、BGM、音效，并输出剪映草稿和审查报告。",
            "5. 治理层：自动修复剪映索引、收敛最新草稿、任务看板支持复查与问题定位。",
            "",
            "二、当前结果概览",
            f"- 最近批跑时间：{snapshot.get('batch_updated_at') or '-'}",
            f"- 批跑成功/失败：{snapshot.get('success_count', 0)}/{snapshot.get('failed_count', 0)}",
            f"- 选择总命中数：{snapshot.get('selection_total', 0)}",
            f"- 镜头级命中占比：{snapshot.get('shot_selected_ratio', 0.0):.2%}",
            f"- 镜头级平均分：{snapshot.get('avg_shot_score', 0.0):.3f}",
            f"- 整条素材平均分：{snapshot.get('avg_full_video_score', 0.0):.3f}",
            f"- 强/放宽/降级命中：{snapshot.get('match_quality', {}).get('strong', 0)} / {snapshot.get('match_quality', {}).get('relaxed', 0)} / {snapshot.get('match_quality', {}).get('degraded', 0)}",
            f"- 产品/病症素材池：{snapshot.get('product_after', 0)} / {snapshot.get('symptom_after', 0)}",
            "",
            "三、剪映草稿状态",
            f"- 当前草稿模式：{snapshot.get('draft_mode') or '-'}",
            f"- 当前草稿目录：{snapshot.get('draft_root') or '-'}",
            f"- 索引登记数量：{snapshot.get('draft_ids', 0)}",
            f"- 主目录实际 OTC 草稿数：{snapshot.get('managed_draft_count', 0)}",
            "",
            "四、最近一轮成功草稿",
        ]
        latest = snapshot.get("latest_success_drafts") or []
        if latest:
            lines.extend([f"- {name}" for name in latest[:12]])
        else:
            lines.append("- 暂无可展示草稿")
        lines.extend([
            "",
            "五、给同事讲解时可以这样说",
            "- 这个 EXE 不是单纯打包壳，它已经把业务主流程、结果看板、草稿修复和验证入口都收进来了。",
            "- 同事只需要在程序里看 3 个地方：一键智能合成、任务队列看板、业务分析面板。",
            "- 如果需要排障，优先看任务队列里的草稿/日志/审查报告，再用这里的草稿修复和环境自检。",
        ])
        return "\n".join(lines)

    def refresh_analysis_panel(self):
        snapshot = self._build_analysis_snapshot()
        self._analysis_summary_cache = snapshot
        self.lbl_analysis_summary.configure(
            text=(
                f"最近批跑 {snapshot.get('success_count', 0)} 成功 / {snapshot.get('failed_count', 0)} 失败，"
                f"镜头级命中占比 {snapshot.get('shot_selected_ratio', 0.0):.2%}，"
                f"当前剪映索引 {snapshot.get('draft_ids', 0)} 条。"
            )
        )
        self.lbl_analysis_runtime.configure(
            text=(
                f"草稿模式: {snapshot.get('draft_mode') or '-'}\n"
                f"草稿目录: {self._short_path(snapshot.get('draft_root', ''), max_len=100)}\n"
                f"目录 OTC 草稿: {snapshot.get('managed_draft_count', 0)} | 索引登记: {snapshot.get('draft_ids', 0)}"
            )
        )
        self._set_analysis_text(self._format_analysis_snapshot(snapshot))

    def run_analysis_preflight(self):
        self.lbl_analysis_runtime.configure(text="正在执行 EXE/worker 环境自检，请稍候...")
        self.update_idletasks()
        returncode, stdout, stderr = self._run_worker_subprocess("--preflight", timeout=240)
        payload = {}
        try:
            payload = json.loads(stdout.strip()) if stdout.strip().startswith("{") else {}
        except Exception:
            payload = {}
        fatal_errors = payload.get("fatal_errors", []) if isinstance(payload, dict) else []
        warnings = payload.get("warnings", []) if isinstance(payload, dict) else []
        result_text = [
            f"worker 自检返回码: {returncode}",
            f"fatal_errors: {len(fatal_errors)}",
            f"warnings: {len(warnings)}",
        ]
        if fatal_errors:
            result_text.append("关键错误: " + " | ".join(str(item) for item in fatal_errors[:5]))
        elif warnings:
            result_text.append("注意项: " + " | ".join(str(item) for item in warnings[:5]))
        else:
            result_text.append("当前运行环境自检通过。")
        self.lbl_analysis_runtime.configure(text="\n".join(result_text))
        if stderr.strip():
            self._set_analysis_text(self.txt_analysis.get("1.0", "end").strip() + "\n\n[stderr]\n" + stderr.strip())

    def run_analysis_repair_registry(self):
        self.repair_draft_registry()
        self.refresh_analysis_panel()
        messagebox.showinfo("完成", "已执行草稿索引修复，并刷新业务分析面板。")

    def _get_selected_task_info(self):
        selection = self._get_selected_task_ids()
        if not selection:
            return "", {}
        focused = self.tree.focus()
        task_id = focused if focused in selection else selection[0]
        return task_id, self._snapshot_running_tasks().get(task_id, {})

    def _refresh_task_artifacts_if_needed(self, task_id, info):
        video_file = str(info.get("file", "")).strip()
        if not video_file:
            return info
        draft_root = self._resolve_selected_draft_root_info().get("resolved_root", "")
        latest_report, latest_draft = self._locate_task_artifacts(video_file, draft_root=draft_root)
        updated = dict(info)
        changed = False

        current_report = str(info.get("review_report", "")).strip()
        if latest_report and latest_report != current_report:
            updated["review_report"] = latest_report
            changed = True

        current_draft = str(info.get("draft_dir", "")).strip()
        if latest_draft and latest_draft != current_draft:
            updated["draft_dir"] = latest_draft
            changed = True

        if changed:
            with self._tasks_lock:
                if task_id in self.running_tasks:
                    self.running_tasks[task_id].update({
                        "review_report": updated.get("review_report", ""),
                        "draft_dir": updated.get("draft_dir", ""),
                    })
            self._write_task_report_snapshot()
        return updated

    def _task_hit_lookup_key(self, hit):
        return (
            round(float(hit.get("start_time", 0.0) or 0.0), 3),
            round(float(hit.get("end_time", 0.0) or 0.0), 3),
            str(hit.get("material_name", "")).strip(),
            str(hit.get("fallback_level", "")).strip(),
        )

    def _load_task_decision_hits(self, decision_log_path):
        rows = []
        path = str(decision_log_path or "").strip()
        if not path or not os.path.exists(path):
            return rows
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if payload.get("event") == "broll_material_selected":
                        rows.append(payload)
        except Exception:
            return []
        return rows

    def _merge_task_hit_details(self, insertions, decision_hits):
        decision_map = {self._task_hit_lookup_key(item): item for item in decision_hits}
        merged = []
        for item in insertions:
            current = dict(item or {})
            detail = decision_map.get(self._task_hit_lookup_key(current), {})
            if detail:
                if not current.get("score_breakdown"):
                    current["score_breakdown"] = dict(detail.get("score_breakdown") or {})
                for key in ("candidate_intent", "candidate_action_type", "candidate_scene_hint", "candidate_entities", "material_tags"):
                    if key not in current or not current.get(key):
                        current[key] = detail.get(key)
            merged.append(current)
        return merged

    def _format_hit_breakdown(self, hit):
        breakdown = dict(hit.get("score_breakdown") or {})
        if not breakdown:
            return "命中理由: 当前结果未记录 score_breakdown。"
        preferred_keys = [
            ("final_score", "最终分"),
            ("semantic_only", "语义主分"),
            ("shot_recall", "镜头召回"),
            ("aes_score", "质量分"),
            ("intent", "意图"),
            ("action", "动作"),
            ("scene", "场景"),
            ("entity", "实体"),
            ("context", "上下文"),
            ("visual_quality", "画质"),
            ("type_alignment", "类型一致"),
            ("anchor", "锚点"),
            ("token_overlap", "词重合"),
        ]
        parts = []
        for key, label in preferred_keys:
            if key in breakdown:
                try:
                    parts.append(f"{label}:{float(breakdown[key]):.3f}")
                except Exception:
                    parts.append(f"{label}:{breakdown[key]}")
        if not parts:
            for key, value in breakdown.items():
                parts.append(f"{key}:{value}")
        return "命中理由: " + " | ".join(parts)

    def _get_hit_level_label(self, hit):
        explicit = str(hit.get("match_quality_label", "")).strip()
        if explicit:
            return explicit
        fallback_level = str(hit.get("fallback_level", "")).strip().lower()
        if fallback_level == "blind_fallback":
            return "盲选兜底"
        if "emergency" in fallback_level:
            return "泛素材降级"
        if "previous_video" in fallback_level:
            return "历史复用降级"
        if "relaxed" in fallback_level:
            return "放宽阈值命中"
        if hit.get("is_shot_material"):
            return "镜头强命中"
        return "标准语义命中"

    def _classify_hit_review(self, hit):
        fallback_level = str(hit.get("fallback_level", "")).strip().lower()
        semantic_score = float(hit.get("semantic_score", 0.0) or 0.0)
        is_shot = bool(hit.get("is_shot_material"))

        if "emergency" in fallback_level or "previous_video" in fallback_level:
            return "需剪映复查", "review_high"
        if "relaxed" in fallback_level and semantic_score < 0.68:
            return "需剪映复查", "review_high"
        if (not is_shot and semantic_score < 0.75) or semantic_score < 0.60:
            return "需剪映复查", "review_high"
        if "relaxed" in fallback_level or (not is_shot and semantic_score < 0.85) or (is_shot and semantic_score < 0.72):
            return "建议剪映复查", "review_medium"
        return "高把握", "review_low"

    def _is_hit_need_review(self, hit):
        review_label, _ = self._classify_hit_review(hit)
        return review_label != "高把握"

    def _load_task_hit_entries(self, review_report):
        report = self._load_json_file(review_report)
        traceability = report.get("traceability") or {}
        insertions = list(traceability.get("broll_insertions") or [])
        decision_log_path = (
            str(traceability.get("decision_log_path", "")).strip()
            or str((report.get("acceptance_check") or {}).get("decision_log_path", "")).strip()
        )
        decision_hits = self._load_task_decision_hits(decision_log_path)
        return self._merge_task_hit_details(insertions, decision_hits)

    def _clear_task_hit_preview(self, summary_text):
        self.lbl_task_preview_summary.configure(text=summary_text)
        self.lbl_task_preview_image.configure(text="当前任务暂无命中片段", image="")
        self.lbl_task_preview_meta.configure(text="-")
        self.lbl_task_preview_tags.configure(text="-")
        self.lbl_task_preview_desc.configure(text="-")
        self.lbl_task_preview_breakdown.configure(text="-")
        self._set_button_state(self.btn_task_hit_prev, "disabled")
        self._set_button_state(self.btn_task_hit_next, "disabled")

    def _render_task_hit_list(self):
        self._task_hit_rows = {}
        for item_id in self.tree_task_hits.get_children():
            self.tree_task_hits.delete(item_id)

        insertions = list(self._task_hit_all_rows)
        if self.var_task_hits_only_shot.get():
            insertions = [item for item in insertions if item.get("is_shot_material")]
        if self.var_task_hits_need_review.get():
            insertions = [item for item in insertions if self._is_hit_need_review(item)]

        for idx, hit in enumerate(insertions, start=1):
            source_label = "镜头片段" if hit.get("is_shot_material") else "整条素材"
            review_label, review_tag = self._classify_hit_review(hit)
            item_id = self.tree_task_hits.insert(
                "",
                "end",
                values=(
                    f"{float(hit.get('start_time', 0.0)):.1f}s - {float(hit.get('end_time', 0.0)):.1f}s",
                    hit.get("semantic_type", "-"),
                    source_label,
                    review_label,
                    self._get_hit_level_label(hit),
                    f"{float(hit.get('semantic_score', 0.0)):.2f}",
                    hit.get("material_name", f"命中片段 {idx}"),
                ),
                tags=(review_tag,),
            )
            self._task_hit_rows[item_id] = hit

        children = self.tree_task_hits.get_children()
        if children:
            self.tree_task_hits.selection_set(children[0])
            self.tree_task_hits.focus(children[0])
            self.on_task_hit_selected()
        else:
            if self.var_task_hits_need_review.get():
                filter_text = "当前筛选条件下没有需剪映复查的命中"
            elif self.var_task_hits_only_shot.get():
                filter_text = "当前筛选条件下没有镜头级命中"
            else:
                filter_text = "当前任务暂无命中片段"
            self._clear_task_hit_preview(filter_text)

    def on_task_hit_filter_changed(self):
        task_id, info = self._get_selected_task_info()
        shown_count = len([
            item for item in self._task_hit_all_rows
            if (item.get("is_shot_material") or not self.var_task_hits_only_shot.get())
            and (self._is_hit_need_review(item) or not self.var_task_hits_need_review.get())
        ])
        shot_count = sum(1 for item in self._task_hit_all_rows if item.get("is_shot_material"))
        review_count = sum(1 for item in self._task_hit_all_rows if self._is_hit_need_review(item))
        if task_id:
            self.lbl_task_preview_summary.configure(
                text=(
                    f"任务: {task_id} | 文件: {info.get('file', '-')} | "
                    f"命中 {shown_count}/{len(self._task_hit_all_rows)} 条 | 镜头片段 {shot_count} 条 | "
                    f"需剪映复查 {review_count} 条 | "
                    f"状态: {info.get('status', '-')}"
                )
            )
        self._render_task_hit_list()

    def on_task_tree_selected(self, _event=None):
        task_id, info = self._get_selected_task_info()
        self._last_selected_task_id = task_id
        self._update_task_selection_actions()
        info = self._refresh_task_artifacts_if_needed(task_id, info)
        self._task_hit_rows = {}
        self._task_hit_all_rows = []
        for item_id in getattr(self, "tree_task_hits", ttk.Treeview()).get_children():
            self.tree_task_hits.delete(item_id)

        if not task_id:
            self._clear_task_hit_preview("请选择上方任务查看命中片段预览")
            self._set_button_state(self.btn_open_task_report, "disabled")
            self._set_button_state(self.btn_open_task_log, "disabled")
            self._set_button_state(self.btn_open_task_draft, "disabled")
            self._refresh_selected_task_runtime_log()
            return

        review_report = str(info.get("review_report", "")).strip()
        log_path = str(info.get("log_path", "")).strip()
        draft_dir = str(info.get("draft_dir", "")).strip()
        self._set_button_state(self.btn_open_task_report, "normal" if review_report and os.path.exists(review_report) else "disabled")
        self._set_button_state(self.btn_open_task_log, "normal" if log_path and os.path.exists(log_path) else "disabled")
        self._set_button_state(self.btn_open_task_draft, "normal" if draft_dir and os.path.exists(draft_dir) else "disabled")

        insertions = self._load_task_hit_entries(review_report)
        self._task_hit_all_rows = insertions
        shot_count = sum(1 for item in insertions if item.get("is_shot_material"))
        review_count = sum(1 for item in insertions if self._is_hit_need_review(item))
        shown_count = len([
            item for item in insertions
            if (item.get("is_shot_material") or not self.var_task_hits_only_shot.get())
            and (self._is_hit_need_review(item) or not self.var_task_hits_need_review.get())
        ])
        self.lbl_task_preview_summary.configure(
            text=(
                f"任务: {task_id} | 文件: {info.get('file', '-')} | "
                f"命中 {shown_count}/{len(insertions)} 条 | 镜头片段 {shot_count} 条 | "
                f"需剪映复查 {review_count} 条 | "
                f"状态: {info.get('status', '-')}"
            )
        )
        self._render_task_hit_list()
        self._refresh_selected_task_runtime_log()

    def on_task_hit_selected(self, _event=None):
        selection = self.tree_task_hits.selection()
        if not selection:
            return
        hit = self._task_hit_rows.get(selection[0]) or {}
        children = list(self.tree_task_hits.get_children())
        selected_index = children.index(selection[0]) if selection[0] in children else -1
        thumb_path = str(hit.get("shot_thumbnail_path", "")).strip()
        self._task_preview_image = None
        if thumb_path and os.path.exists(thumb_path):
            try:
                image = Image.open(thumb_path)
                image.thumbnail((440, 248))
                self._task_preview_image = ImageTk.PhotoImage(image)
                self.lbl_task_preview_image.configure(image=self._task_preview_image, text="")
            except Exception:
                self.lbl_task_preview_image.configure(text="缩略图加载失败", image="")
        else:
            self.lbl_task_preview_image.configure(
                text="当前命中为整条素材，暂无镜头缩略图" if not hit.get("is_shot_material") else "未找到镜头缩略图",
                image="",
            )

        self._set_button_state(self.btn_task_hit_prev, "normal" if selected_index > 0 else "disabled")
        self._set_button_state(self.btn_task_hit_next, "normal" if 0 <= selected_index < len(children) - 1 else "disabled")
        review_label, _ = self._classify_hit_review(hit)
        self.lbl_task_preview_meta.configure(
            text=(
                f"素材: {hit.get('material_name', '-')}\n"
                f"插入时间: {float(hit.get('start_time', 0.0)):.2f}s - {float(hit.get('end_time', 0.0)):.2f}s\n"
                f"命中类型: {hit.get('semantic_type', '-')} | 来源: {'镜头片段' if hit.get('is_shot_material') else '整条素材'}\n"
                f"命中层级: {self._get_hit_level_label(hit)} | 语义分: {float(hit.get('semantic_score', 0.0)):.2f}\n"
                f"复查建议: {review_label}\n"
                f"片段时间: {hit.get('shot_start_seconds', '-')} - {hit.get('shot_end_seconds', '-')}\n"
                f"当前位置: {selected_index + 1}/{len(children) if children else 0}"
            )
        )
        self.lbl_task_preview_tags.configure(
            text=(
                f"素材路径: {self._short_path(hit.get('material_path', '-'), max_len=64)}\n"
                f"镜头索引: {hit.get('shot_index', '-')}\n"
                f"候选意图: {hit.get('candidate_intent', '-')}\n"
                f"候选动作: {hit.get('candidate_action_type', '-')}\n"
                f"候选实体: {' / '.join(hit.get('candidate_entities', []) or ['-'])}\n"
                f"素材标签: {' / '.join(hit.get('material_tags', []) or ['-'])}\n"
                f"缩略图: {self._short_path(thumb_path, max_len=64) if thumb_path else '-'}"
            )
        )
        self.lbl_task_preview_desc.configure(text=f"片段说明: {hit.get('shot_caption', '当前命中为整条素材，暂无镜头级说明。')}")
        self.lbl_task_preview_breakdown.configure(
            text=self._format_hit_breakdown(hit) + f"\n复查动作: {'请打开当前剪映草稿复查这一条中插。' if self._is_hit_need_review(hit) else '当前命中把握较高，可在剪映中快速略看确认。'}"
        )

    def _select_task_hit_by_offset(self, offset):
        children = list(self.tree_task_hits.get_children())
        if not children:
            return
        selection = self.tree_task_hits.selection()
        if selection and selection[0] in children:
            current_index = children.index(selection[0])
        else:
            current_index = 0
        target_index = max(0, min(len(children) - 1, current_index + offset))
        target_id = children[target_index]
        self.tree_task_hits.selection_set(target_id)
        self.tree_task_hits.focus(target_id)
        self.tree_task_hits.see(target_id)
        self.on_task_hit_selected()

    def select_prev_task_hit(self):
        self._select_task_hit_by_offset(-1)

    def select_next_task_hit(self):
        self._select_task_hit_by_offset(1)

    def open_selected_task_report(self):
        _, info = self._get_selected_task_info()
        report_path = str(info.get("review_report", "")).strip()
        if report_path and os.path.exists(report_path):
            os.startfile(report_path)
        else:
            messagebox.showinfo("提示", "当前任务还没有可打开的审查报告。")

    def open_selected_task_log(self):
        _, info = self._get_selected_task_info()
        log_path = str(info.get("log_path", "")).strip()
        if log_path and os.path.exists(log_path):
            os.startfile(log_path)
        else:
            messagebox.showinfo("提示", "当前任务还没有可打开的日志文件。")

    def open_selected_task_draft(self):
        _, info = self._get_selected_task_info()
        draft_dir = str(info.get("draft_dir", "")).strip()
        if draft_dir and os.path.exists(draft_dir):
            os.startfile(draft_dir)
        else:
            messagebox.showinfo("提示", "当前任务还没有可打开的草稿目录。")

    def open_draft_folder(self):
        draft_path = self._resolve_selected_draft_root_info().get("resolved_root", "") or get_draft_root()
        if os.path.exists(draft_path):
            os.startfile(draft_path)
        else:
            messagebox.showwarning("警告", f"未找到剪映草稿目录: {draft_path}")

    def open_output_folder(self):
        if os.path.exists(self.output_dir):
            os.startfile(self.output_dir)
        else:
            messagebox.showwarning("警告", f"未找到结果输出目录: {self.output_dir}")

    def open_internal_log(self):
        if os.path.exists(self.internal_log_path):
            os.startfile(self.internal_log_path)
        else:
            messagebox.showinfo("提示", f"当前还没有维护日志文件：\n{self.internal_log_path}")

if __name__ == "__main__":
    configure_current_process()
    app = YunFengEditorUI()
    app.mainloop()
                                                                        
