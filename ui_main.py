import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import sys
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
from text_output_utils import decode_process_output, repair_mojibake_text

LOCKED_LLM_BASE_URL = "https://api.kuai.host/v1"

os.environ.update(build_runtime_env(os.environ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class YunFengEditorUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("云锋剪辑 - 智能视频生成系统 (专业版)")
        self.geometry("950x850")
        self.configure(padx=10, pady=10)

        self.output_dir = runtime_path("output")
        self.task_logs_dir = os.path.join(self.output_dir, "task_logs")
        self.internal_log_path = os.path.join(self.output_dir, "internal_maintenance.log")
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

        # 样式设置
        style = ttk.Style()
        style.configure("TButton", padding=6, font=('Microsoft YaHei', 10))
        style.configure("Header.TLabel", font=('Microsoft YaHei', 14, 'bold'))
        style.configure("Info.TLabel", font=('Microsoft YaHei', 9), foreground="gray")
        style.configure("Status.TLabel", font=('Microsoft YaHei', 10, 'bold'))

        self.create_widgets()
        self.repair_draft_registry()
        # 启动时先清理一次历史残留的空草稿
        self.cleanup_empty_drafts()
        self.after(1000, self.update_task_status_ui)

    def create_widgets(self):
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

        self.tab_auto_gen = ttk.Frame(self.notebook, padding=10)
        self.tab_tasks = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_auto_gen, text="🎬 一键智能合成")
        self.notebook.add(self.tab_tasks, text="📊 任务队列看板")

        self.build_auto_gen_tab()
        self.build_tasks_tab()

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

        # 1. 路径输入区
        path_frame = ttk.LabelFrame(frame, text="素材路径配置 (必填)", padding=5)
        path_frame.pack(fill="x", pady=2)

        self.entries = {}
        
        def add_path_row(parent, row, label_text, key):
            ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="e", pady=3, padx=5)
            entry = ttk.Entry(parent, width=55)
            entry.grid(row=row, column=1, sticky="we", pady=3, padx=5)
            ttk.Button(parent, text="选择文件夹...", command=lambda: self.browse_folder(entry)).grid(row=row, column=2, padx=5, pady=3)
            self.entries[key] = entry

        add_path_row(path_frame, 0, "🗣️ 口播素材路径 (仅视频):", "speech")
        add_path_row(path_frame, 1, "📦 产品素材路径 (展示片段):", "product")
        add_path_row(path_frame, 2, "🤒 病症素材路径 (痛点片段):", "symptom")
        add_path_row(path_frame, 3, "🎵 音效素材路径 (可选):", "audio")
        add_path_row(path_frame, 4, "🎼 背景音乐路径 (可选):", "bgm")
        add_path_row(path_frame, 5, "📺 广审素材路径 (可选):", "ad_review")
        add_path_row(path_frame, 6, "✨ 贴图素材路径 (可选):", "sticker")
        add_path_row(path_frame, 7, "📁 统一总目录 (可选快速填入):", "unified")

        # 快速填充按钮
        def auto_fill_from_unified():
            base = self.entries["unified"].get().strip()
            if base and os.path.exists(base):
                mapping = [
                    ("speech", "口播", "必选"), ("product", "产品", "必选"), ("symptom", "病症", "必选"), 
                    ("audio", "音效", "可选"), ("bgm", "背景音乐", "可选"), ("ad_review", "广审", "可选"), ("sticker", "贴图", "可选")
                ]
                
                # 开发递归扫描程序，生成结构映射表并复制树状结构
                try:
                    import json
                    output_dir = runtime_path("output")
                    os.makedirs(output_dir, exist_ok=True)
                    
                    mapping_data = []
                    found_paths = {k: None for k, _, _ in mapping}
                    
                    # 遍历所有的子目录
                    for root, dirs, files in os.walk(base):
                        rel_path = os.path.relpath(root, base)
                        if rel_path == '.':
                            continue
                            
                        depth = len(rel_path.split(os.sep))
                        
                        # 判断素材类型
                        mat_type = "未知"
                        
                        # 查找当前目录路径中是否包含映射表中的关键字
                        path_parts = rel_path.split(os.sep)
                        matched = False
                        for part in path_parts:
                            for k, folder_name, m_type in mapping:
                                # 使用关键字模糊匹配，因为可选文件夹可能叫 "可选-贴图" 或者 "补充音效"
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
                        
                        # 在最终输出中保持与源目录100%一致的树状结构
                        target_dir = os.path.join(output_dir, rel_path)
                        os.makedirs(target_dir, exist_ok=True)
                        
                    # 填充UI
                    for k, folder_name, _ in mapping:
                        # 优先使用扫描到的路径，如果没有扫描到则尝试直接拼接默认名称
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

        ttk.Button(path_frame, text="⬇️ 自动从总目录推导子路径", command=auto_fill_from_unified).grid(row=7, column=3, padx=5)

        # 1.5 LLM 大模型配置区
        llm_frame = ttk.LabelFrame(frame, text="AI 大模型配置 (用于深度语义理解)", padding=5)
        llm_frame.pack(fill="x", pady=2)
        
        ttk.Label(llm_frame, text="API Key:").grid(row=0, column=0, sticky="e", pady=3, padx=5)
        self.entry_llm_key = ttk.Entry(llm_frame, width=40, show="*")
        self.entry_llm_key.insert(0, "sk-6qR79NVj7d15Yq9HejDdudvDeaMZ9O6xfV1rOhqwqtqQSaGZ")
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
            command=self.test_llm_connectivity,
        )
        self.btn_test_llm.grid(row=1, column=2, sticky="e", pady=3, padx=5)
        
        ttk.Label(llm_frame, text="*填写 API Key 后，系统将放弃死板的关键词规则，转为使用真正的大模型通读口播字幕，\n精准输出病症/产品插入点以及音效、BGM的情绪节点。", foreground="gray").grid(row=2, column=0, columnspan=4, sticky="w", pady=3, padx=5)

        # 2. 生成设置区
        settings_frame = ttk.LabelFrame(frame, text="生成参数与频率控制", padding=5)
        settings_frame.pack(fill="x", pady=2)

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
        self.scale_ad_freq = tk.Scale(settings_frame, from_=0, to=10, orient="horizontal", length=150)
        self.scale_ad_freq.set(1) # 默认每任务1次
        self.scale_ad_freq.grid(row=1, column=1, padx=5, pady=3, sticky="w")

        ttk.Label(settings_frame, text="贴图素材频率 (次/任务, 0为无限制):").grid(row=1, column=2, padx=5, pady=3, sticky="e")
        self.scale_sticker_freq = tk.Scale(settings_frame, from_=0, to=10, orient="horizontal", length=150)
        self.scale_sticker_freq.set(0) # 默认无限制
        self.scale_sticker_freq.grid(row=1, column=3, padx=5, pady=3, sticky="w")

        ttk.Label(settings_frame, text="中插素材频率 (次/任务, 0为无限制):").grid(row=2, column=0, padx=5, pady=3, sticky="e")
        self.scale_broll_freq = tk.Scale(settings_frame, from_=0, to=10, orient="horizontal", length=150)
        self.scale_broll_freq.set(1) # 默认每任务只使用1次，严控重复
        self.scale_broll_freq.grid(row=2, column=1, padx=5, pady=3, sticky="w")

        # 3. 动作区
        action_frame = ttk.Frame(frame)
        action_frame.pack(fill="x", pady=5)
        
        self.btn_batch_add = ttk.Button(action_frame, text="🚀 批量生成全自动草稿 (零人工干预)", command=self.submit_batch_tasks)
        self.btn_batch_add.pack(side="left", padx=5)
        
        ttk.Button(action_frame, text="📂 打开草稿目录预览", command=self.open_draft_folder).pack(side="right", padx=5)

    def build_tasks_tab(self):
        frame = self.tab_tasks
        ttk.Label(frame, text="任务队列看板：全自动批处理 -> 匹配素材 -> 轨道添加 -> 输出草稿 -> 异常重试", style="Info.TLabel").pack(anchor="w", pady=(0, 5))

        self.lbl_queue_status = ttk.Label(frame, text="当前无任务", style="Status.TLabel")
        self.lbl_queue_status.pack(anchor="w", pady=5)

        self.lbl_maintenance_status = ttk.Label(
            frame,
            text="维护告警: 0 | 当前无维护告警",
            style="Info.TLabel",
            foreground="#b45309",
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
        
        # 底部控制区
        control_frame = ttk.Frame(frame)
        control_frame.pack(fill="x", pady=10)
        
        self.btn_retry_failed = ttk.Button(control_frame, text="🔄 重试失败任务", command=self.retry_failed_tasks, state="disabled")
        self.btn_retry_failed.pack(side="left", padx=5)
        
        self.btn_export_json = ttk.Button(control_frame, text="📄 导出执行报告", command=self.export_report_json, state="disabled")
        self.btn_export_json.pack(side="left", padx=5)

        self.btn_open_internal_log = ttk.Button(control_frame, text="🧾 打开维护日志", command=self.open_internal_log)
        self.btn_open_internal_log.pack(side="left", padx=5)

    def browse_file(self, entry_widget):
        filename = filedialog.askopenfilename(filetypes=[("文本或字幕", "*.txt *.srt"), ("所有文件", "*.*")])
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

    def test_llm_connectivity(self):
        api_key = self.entry_llm_key.get().strip()
        model = self.entry_llm_model.get().strip() or "deepseek-v3.2"
        if not api_key:
            messagebox.showwarning("提示", "请先填写 API Key。")
            return

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
            env["OTC_BGM_DIR"] = bgm_dir

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
        
        env = self._build_llm_env(env)
        
        if len(speech_videos) > TASK_QUEUE_CAPACITY:
            messagebox.showerror("错误", f"当前批量任务数超过队列容量上限 {TASK_QUEUE_CAPACITY}，请分批提交。")
            return

        self.executor._max_workers = BATCH_CONCURRENCY
        self.btn_batch_add.config(state="disabled")

        for video_file in speech_videos:
            self.task_count += 1
            task_id = f"Task-{self.task_count:03d}"
            submit_time = time.strftime("%H:%M:%S")
            
            self.tree.insert("", "end", iid=task_id, values=(task_id, submit_time, "⌛ 排队中", f"准备处理: {video_file}"))
            self.executor.submit(self.execute_task, task_id, env, sensitivity, video_file)
            
        self.notebook.select(self.tab_tasks)
        messagebox.showinfo("已提交", f"已批量提交 {len(speech_videos)} 个视频任务到全自动队列！")

    def execute_task(self, task_id, env, sensitivity, video_file, retry_count=0):
        max_retries = BATCH_RETRY_LIMIT
        self.running_tasks[task_id] = {"status": "🔄 生成中", "log": f"正在处理: {video_file} (重试: {retry_count})", "file": video_file}
        
        try:
            task_env = build_runtime_env(env)
            with self.subprocess_slots:
                result = subprocess.run(
                    get_worker_command(["--sensitivity", sensitivity, "--video", video_file]),
                    env=task_env,
                    cwd=runtime_path(),
                    capture_output=True,
                    text=False
                )
            result.stdout = decode_process_output(result.stdout)
            result.stderr = decode_process_output(result.stderr)
            log_path = self._write_task_log(task_id, video_file, result)
            
            if result.returncode == 0:
                success_summary = self._summarize_process_output(result, success=True)
                self.running_tasks[task_id] = {
                    "status": "✅ 待发布",
                    "log": f"{success_summary} | 日志: {log_path}",
                    "file": video_file,
                    "result": "SUCCESS",
                    "log_path": log_path
                }
            else:
                if retry_count < max_retries:
                    self.execute_task(task_id, env, sensitivity, video_file, retry_count + 1)
                else:
                    err_msg = self._summarize_process_output(result, success=False)
                    self.running_tasks[task_id] = {
                        "status": "❌ 失败",
                        "log": f"{err_msg} | 日志: {log_path}",
                        "file": video_file,
                        "result": "FAILED",
                        "log_path": log_path
                    }
        except Exception as e:
            if retry_count < max_retries:
                self.execute_task(task_id, env, sensitivity, video_file, retry_count + 1)
            else:
                self.running_tasks[task_id] = {"status": "❌ 异常", "log": str(e), "file": video_file, "result": "ERROR"}

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

        keywords = ("[失败]", "Traceback", "RuntimeError", "Error", "错误", "当前版本不受支持", "核心文件未落盘")
        for line in reversed(lines):
            if any(keyword in line for keyword in keywords):
                return line[-240:]
        return lines[-1][-240:]

    def retry_failed_tasks(self):
        env = os.environ.copy() # Need a way to recover env, or store it. For now, grab from entries
        # Better to store env and sensitivity in the class or task info.
        pass # Will implement properly next


    def update_task_status_ui(self):
        # 更新队列统计文本
        active = sum(1 for info in self.running_tasks.values() if info["status"] in ["🔄 生成中", "🔍 待人工确认"])
        waiting = sum(1 for info in self.running_tasks.values() if info["status"] in ["⏳ 等待执行", "⌛ 排队中"])
        failed = sum(1 for info in self.running_tasks.values() if info["status"] in ["❌ 失败", "❌ 异常"])
        
        mode_text = "【全自动批量模式】"
        self.lbl_queue_status.config(text=f"{mode_text} 正在处理: {active} | 等待中: {waiting} | 失败: {failed} | 总计提交: {self.task_count}")
        self.lbl_maintenance_status.config(
            text=f"维护告警: {self.maintenance_warning_count} | {self.latest_maintenance_warning}"
        )

        # 定时刷新任务列表的 UI
        for task_id, info in list(self.running_tasks.items()):
            if self.tree.exists(task_id):
                item = self.tree.item(task_id)
                vals = list(item["values"])
                if vals[2] != info["status"] or vals[3] != info["log"]:
                    vals[2] = info["status"]
                    vals[3] = info["log"]
                    self.tree.item(task_id, values=vals)
                    
        # 激活/禁用按钮
        if active == 0 and waiting == 0 and self.task_count > 0:
            if not getattr(self, '_cleanup_done', False):
                self.cleanup_empty_drafts()
                self._cleanup_done = True
                
            self.btn_export_json.config(state="normal")
            if failed > 0:
                self.btn_retry_failed.config(state="normal")
            self.btn_batch_add.config(state="normal")
        else:
            self._cleanup_done = False
            self.btn_export_json.config(state="disabled")
            self.btn_retry_failed.config(state="disabled")
            
        self.after(1000, self.update_task_status_ui)

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
        env = os.environ.copy()
        env["OTC_SPEECH_DIR"] = self.entries["speech"].get().strip()
        env["OTC_PRODUCT_DIR"] = self.entries["product"].get().strip()
        env["OTC_SYMPTOM_DIR"] = self.entries["symptom"].get().strip()
        audio_dir = self.entries["audio"].get().strip()
        if audio_dir and os.path.exists(audio_dir): env["OTC_AUDIO_DIR"] = audio_dir
        bgm_dir = self.entries["bgm"].get().strip()
        if bgm_dir and os.path.exists(bgm_dir): env["OTC_BGM_DIR"] = bgm_dir
        ad_review_dir = self.entries["ad_review"].get().strip()
        if ad_review_dir and os.path.exists(ad_review_dir): env["OTC_AD_REVIEW_DIR"] = ad_review_dir
        sticker_dir = self.entries["sticker"].get().strip()
        if sticker_dir and os.path.exists(sticker_dir): env["OTC_STICKER_DIR"] = sticker_dir

        env = self._build_llm_env(env)

        sensitivity = self.combo_sens.get().split()[0]
        
        retry_count = 0
        for task_id, info in self.running_tasks.items():
            if info["status"] in ["❌ 失败", "❌ 异常"]:
                self.executor.submit(self.execute_task, task_id, env, sensitivity, info["file"], 0)
                retry_count += 1
                
        if retry_count > 0:
            messagebox.showinfo("重试", f"已重新提交 {retry_count} 个失败任务！")
            self.btn_retry_failed.config(state="disabled")
            
    def export_report_json(self):
        import json
        report = []
        for task_id, info in self.running_tasks.items():
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
