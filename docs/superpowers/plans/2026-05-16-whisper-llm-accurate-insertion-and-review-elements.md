# Whisper + LLM 精准插入管线 & 贴图广审始终加入 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过 Whisper 转写 + LLM 内容感知规划实现精准素材插入，同时解除贴图/广审门控使其始终生效。

**Architecture:** 新增 speech_transcriber.py（Whisper 转写+缓存），扩展 llm_clip_matcher.py（基于转写的 LLM 规划和关键词规则），修改 smart_material_matching 接入新管线，移除 create_otc_promo_video 中贴图/广审的 is_review_version 门控。

**Tech Stack:** openai-whisper (base model), FFmpeg (bundled), Python 3.x

---

### Task 1: 创建 speech_transcriber.py — Whisper 转写模块

**Files:**
- Create: `speech_transcriber.py`

- [ ] **Step 1: 创建模块骨架并写导入和文件头**

```python
"""Whisper 语音转写模块 — 提取口播视频音频并逐句转写。"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, List, Optional

from app_paths import resource_path

_WHISPER_MODEL = None
_WHISPER_MODEL_LOCK = threading.Lock()


def _resolve_ffmpeg() -> str:
    """查找 FFmpeg 路径（优先系统 PATH，其次 bundled）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    bundled = resource_path("ffmpeg-8.1-essentials_build", "bin", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    return "ffmpeg"
```

- [ ] **Step 2: 添加 Whisper 模型单例加载**

```python
def _load_whisper_model() -> Optional[object]:
    """懒加载 Whisper base 模型（单例，线程安全）。"""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_MODEL_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        try:
            import whisper
            _WHISPER_MODEL = whisper.load_model("base")
            print("   [Whisper] base 模型加载完成")
        except ImportError:
            print("   [Whisper] openai-whisper 未安装，转写功能不可用")
            _WHISPER_MODEL = False
        except Exception as exc:
            print(f"   [Whisper] 模型加载失败: {exc}")
            _WHISPER_MODEL = False
    return _WHISPER_MODEL if _WHISPER_MODEL is not False else None
```

- [ ] **Step 3: 添加音频提取函数**

```python
def extract_audio_from_video(video_path: str) -> Optional[str]:
    """从视频提取 16kHz mono WAV 音频，返回临时文件路径。"""
    ffmpeg = _resolve_ffmpeg()
    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="whisper_audio_")
    os.close(fd)
    try:
        subprocess.run(
            [
                ffmpeg, "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
                "-y", tmp_path,
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        if os.path.getsize(tmp_path) > 1024:
            return tmp_path
        print("   [Whisper] 提取的音频文件过小，可能视频无音轨")
        return None
    except subprocess.CalledProcessError as exc:
        print(f"   [Whisper] FFmpeg 提取音频失败: {exc.stderr.decode('utf-8', errors='replace')[:200]}")
        return None
    except Exception as exc:
        print(f"   [Whisper] 音频提取异常: {exc}")
        return None
```

- [ ] **Step 4: 添加缓存读写函数**

```python
def _transcript_cache_dir(output_dir: str) -> str:
    d = os.path.join(output_dir, "transcriptions")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(video_name: str, cache_dir: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in video_name)
    return os.path.join(cache_dir, f"{safe_name}.json")


def read_cached_transcript(video_name: str, cache_dir: str) -> Optional[List[Dict]]:
    path = _cache_path(video_name, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        segments = data.get("segments", [])
        if segments and all(isinstance(s, dict) and "start" in s and "end" in s and "text" in s for s in segments):
            return segments
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def write_transcript(video_name: str, segments: List[Dict], cache_dir: str) -> None:
    path = _cache_path(video_name, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False, indent=2)
```

- [ ] **Step 5: 添加主转写函数**

```python
def transcribe_speech(video_path: str, video_name: str, cache_dir: str) -> Optional[List[Dict]]:
    """转写口播视频为逐句时间戳，带缓存。
    
    返回: [{start: float, end: float, text: str}, ...] 或 None
    """
    # 检查缓存
    cached = read_cached_transcript(video_name, cache_dir)
    if cached is not None:
        print(f"   [Whisper] 命中缓存 ({len(cached)} 句)")
        return cached

    model = _load_whisper_model()
    if model is None:
        return None

    audio_path = extract_audio_from_video(video_path)
    if audio_path is None:
        return None

    try:
        print("   [Whisper] 正在转写口播内容...")
        import whisper
        result = model.transcribe(audio_path, language="zh", verbose=False)
        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": round(float(seg.get("start", 0)), 3),
                "end": round(float(seg.get("end", 0)), 3),
                "text": str(seg.get("text", "")).strip(),
            })
        if segments:
            write_transcript(video_name, segments, cache_dir)
            print(f"   [Whisper] 转写完成 ({len(segments)} 句)")
            return segments
        print("   [Whisper] 转写结果为空")
        return None
    except Exception as exc:
        print(f"   [Whisper] 转写异常: {exc}")
        return None
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass
```

- [ ] **Step 6: 验证语法**

```bash
python -c "import ast; ast.parse(open('speech_transcriber.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add speech_transcriber.py
git commit -m "feat: add Whisper speech transcription module

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: 扩展 llm_clip_matcher.py — 基于转写的 LLM 规划和关键词规则

**Files:**
- Modify: `llm_clip_matcher.py`

- [ ] **Step 1: 在 llm_clip_matcher.py 末尾添加 `generate_editing_plan_with_transcript`**

```python
def generate_editing_plan_with_transcript(
    video_duration: float,
    transcript_segments: List[Dict],
    api_key: str,
    model: str = DEFAULT_LLM_MODEL,
    base_url: str = None,
    density_config: dict = None
) -> dict:
    """
    基于真实口播转写内容，使用 LLM 生成精准剪辑剧本。
    
    transcript_segments: [{start: float, end: float, text: str}, ...]
    """
    density_info = ""
    if density_config:
        density_info = (
            f"节奏要求：每 {density_config.get('window_seconds', 30)} 秒至少 "
            f"{density_config.get('min_segments_per_window', 2)} 段中插，"
            f"每段 {density_config.get('insert_min_duration', 1.0)}-{density_config.get('insert_max_duration', 3.0)} 秒。\n"
        )

    # 构建带时间戳的口播文本
    transcript_text = ""
    for idx, seg in enumerate(transcript_segments):
        transcript_text += (
            f"[{idx}] {seg['start']:.1f}s-{seg['end']:.1f}s: {seg['text']}\n"
        )

    example_output = {
        "b_rolls": [
            {"start": 4.0, "end": 7.0, "type": "symptom",
             "reason": "口播第2句描述了皮肤瘙痒症状，插入对应的病症素材"},
            {"start": 16.0, "end": 18.5, "type": "product",
             "reason": "口播第5句介绍了产品成分和使用方法，插入产品展示素材"}
        ],
        "sfx": [
            {"time": 4.0, "type": "whoosh", "reason": "引入病症切入点的转场音效"},
            {"time": 16.0, "type": "ding", "reason": "引出产品的提示音效"}
        ],
        "bgm_emotion": "positive"
    }

    prompt = (
        "你是一个专业的药品推广视频剪辑大师。现在有一段口播视频的逐句转写（含时间戳）：\n\n"
        f"{transcript_text}\n"
        f"总时长为 {video_duration:.1f} 秒。\n"
        f"{density_info}"
        "请根据以上口播内容，在正确的时间点精准规划中插视频（B-Rolls）。原则：\n\n"
        "1. 中插视频 (B-Rolls)：\n"
        "   - 口播中提到**症状、不适、病症**的句子 → type 设为 \"symptom\"，在该句时间范围内安排病症素材\n"
        "   - 口播中提到**产品名、成分、功效、用法、治疗**的句子 → type 设为 \"product\"，在该句时间范围内安排产品展示素材\n"
        "   - 不要机械地前半段 symptom 后半段 product，必须根据每句话的真实内容判断\n"
        "   - 每段中插 1.5 到 4 秒，两段之间保留口播主体画面\n"
        "   - 中插总时长尽量占口播总时长的 60% 左右\n"
        "   - 中插时间点应落在对应口播句子的时间范围内\n\n"
        "2. 音效 (SFX)：在关键转折点插入音效。\n\n"
        "3. 背景音乐情感 (BGM Emotion)：positive, negative 或 neutral。\n\n"
        "请严格只输出一个 JSON 对象，不要包含 markdown 标记或其他多余文本。格式示例：\n"
        f"{json.dumps(example_output, ensure_ascii=False, indent=2)}"
    )

    normalized_model = normalize_model_name(model)
    print(f"   [LLM] 基于口播转写 ({len(transcript_segments)} 句) 进行精准剪辑规划 ({normalized_model})...")

    try:
        response_payload = _call_chat_completions(
            api_key=api_key,
            model=normalized_model,
            base_url=base_url,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        result_text = _extract_response_text(response_payload)

        result_text = re.sub(r'^```json\s*', '', result_text)
        result_text = re.sub(r'^```\s*', '', result_text)
        result_text = re.sub(r'\s*```$', '', result_text)

        plan = json.loads(result_text)

        if "b_rolls" not in plan: plan["b_rolls"] = []
        if "sfx" not in plan: plan["sfx"] = []
        if "bgm_emotion" not in plan: plan["bgm_emotion"] = "neutral"

        # 校验时间范围
        valid_b_rolls = []
        for b in plan["b_rolls"]:
            if float(b.get("end", 0)) > float(b.get("start", 0)):
                # 修正超出视频时长的插入
                b["start"] = max(0.0, min(video_duration - 0.5, float(b.get("start", 0))))
                b["end"] = max(float(b["start"]) + 0.5, min(video_duration, float(b.get("end", 0))))
                b.setdefault("type", "symptom")
                b.setdefault("reason", "LLM精准规划")
                valid_b_rolls.append(b)
        plan["b_rolls"] = valid_b_rolls

        return plan
    except Exception as e:
        print(f"   [LLM Error] 精准规划失败: {str(e)}")
        if 'result_text' in locals():
            print(f"   [LLM Raw Output] {result_text}")
        return None
```

- [ ] **Step 2: 添加关键词规则规划函数 `plan_insertions_with_keywords`**

```python
def plan_insertions_with_keywords(
    transcript_segments: List[Dict],
    video_duration: float,
    density_config: dict = None
) -> dict:
    """
    基于关键词字典从转写文本中提取语义类型并生成中插计划。
    作为无 LLM 时的降级方案。
    """
    from semantic_matcher import SEMANTIC_KEYWORDS

    if density_config:
        insert_min = density_config.get("insert_min_duration", 1.5)
        insert_max = density_config.get("insert_max_duration", 3.0)
    else:
        insert_min = 1.5
        insert_max = 3.0

    b_rolls = []
    last_type = None

    for seg in transcript_segments:
        text = str(seg.get("text", "")).lower()
        if not text:
            continue

        # 统计关键词命中
        symptom_hits = sum(1 for kw in SEMANTIC_KEYWORDS["symptom"] if kw in text)
        product_hits = sum(1 for kw in SEMANTIC_KEYWORDS["product"] if kw in text)

        seg_type = None
        if symptom_hits > product_hits and symptom_hits > 0:
            seg_type = "symptom"
        elif product_hits > symptom_hits and product_hits > 0:
            seg_type = "product"

        if seg_type and seg_type != last_type:
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", 0))
            seg_dur = seg_end - seg_start

            if seg_dur >= insert_min:
                insert_dur = min(seg_dur, insert_max)
                insert_start = seg_start + (seg_dur - insert_dur) / 2
                b_rolls.append({
                    "start": round(insert_start, 1),
                    "end": round(insert_start + insert_dur, 1),
                    "type": seg_type,
                    "reason": f"关键词命中: {text[:40]}"
                })
            last_type = seg_type

    # 如果结果太少，按比例补充
    target_count = max(4, int(video_duration / 15))
    if len(b_rolls) < target_count:
        existing_starts = {float(b["end"]) for b in b_rolls}
        step = video_duration / (target_count + 1)
        for i in range(target_count):
            start = step * (i + 1)
            # 避免与已有插入重叠
            too_close = any(abs(start - es) < 3.0 for es in existing_starts)
            if too_close:
                continue
            b_rolls.append({
                "start": round(start, 1),
                "end": round(start + insert_min, 1),
                "type": "symptom" if i % 2 == 0 else "product",
                "reason": "密度补充"
            })
            existing_starts.add(start)

    b_rolls.sort(key=lambda b: b["start"])
    # 截取到合理数量
    if len(b_rolls) > target_count * 2:
        b_rolls = b_rolls[:target_count * 2]

    # 修正时间范围
    for b in b_rolls:
        b["start"] = max(0.0, min(video_duration - 0.5, float(b.get("start", 0))))
        b["end"] = max(float(b["start"]) + 0.5, min(video_duration, float(b.get("end", 0))))

    return {"b_rolls": b_rolls, "sfx": [], "bgm_emotion": "neutral"}
```

- [ ] **Step 3: 验证语法**

```bash
python -c "import ast; ast.parse(open('llm_clip_matcher.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add llm_clip_matcher.py
git commit -m "feat: add transcript-based LLM planning and keyword fallback

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: 修改 smart_material_matching — 接入转写新管线

**Files:**
- Modify: `otc_promo_workflow.py`

- [ ] **Step 1: 添加 speech_video_path 参数到 smart_material_matching 签名**

在 `otc_promo_workflow.py` 中找到 `smart_material_matching` 函数签名（约 L1500），在 `batch_tracker` 参数后添加 `speech_video_path`:

```python
def smart_material_matching(
    video_duration: float,
    product_videos: List[Dict],
    symptom_videos: List[Dict],
    sensitivity: str = 'medium',
    video_id: str = "default_video",
    tracker: UsageTracker = None,
    batch_tracker: Optional[UsageTracker] = None,
    speech_video_path: str = "",
) -> Tuple[List[Dict], List[Dict], str]:
```

- [ ] **Step 2: 在 LLM 规划之前插入 Whisper 转写 + 新规划逻辑**

找到 `smart_material_matching` 中 LLM 规划调用处（约 L1541），替换该段逻辑：

找到这段代码：
```python
    # LLM 剧本生成（基于时长，无需字幕）
    if llm_api_key:
        try:
            import llm_clip_matcher
            plan = llm_clip_matcher.generate_editing_plan_with_llm(
                video_duration=video_duration,
                ...
            )
```

替换为：
```python
    # LLM 剧本生成 — 优先基于真实口播转写
    transcript_segments = None
    if speech_video_path:
        try:
            from speech_transcriber import transcribe_speech
            transcript_segments = transcribe_speech(
                video_path=speech_video_path,
                video_name=video_id,
                cache_dir=os.path.dirname(decision_log_path) if decision_log_path else OUTPUT_DIR,
            )
        except Exception as e:
            print(f"   [Whisper] 转写阶段异常，降级为关键词/时长规划: {e}")

    if llm_api_key:
        try:
            import llm_clip_matcher
            if transcript_segments:
                plan = llm_clip_matcher.generate_editing_plan_with_transcript(
                    video_duration=video_duration,
                    transcript_segments=transcript_segments,
                    api_key=llm_api_key,
                    model=llm_model,
                    base_url=llm_base_url if llm_base_url else None,
                    density_config=density_template,
                )
            else:
                plan = llm_clip_matcher.generate_editing_plan_with_llm(
                    video_duration=video_duration,
                    api_key=llm_api_key,
                    model=llm_model,
                    base_url=llm_base_url if llm_base_url else None,
                    density_config=density_template,
                )
```

然后在这段代码后面，LLM 不可用时的降级逻辑（约 L1597 附近），LLM 不可用时先尝试关键词规则：

找到设置 `candidate_matches = time_based_candidates` 的地方，在上方添加：

```python
    if not candidate_matches and transcript_segments:
        try:
            import llm_clip_matcher
            keyword_plan = llm_clip_matcher.plan_insertions_with_keywords(
                transcript_segments=transcript_segments,
                video_duration=video_duration,
                density_config=density_template,
            )
            if keyword_plan and keyword_plan.get("b_rolls"):
                print("   [INFO] 使用关键词规则基于口播转写规划中插...")
                for b in keyword_plan["b_rolls"]:
                    start_time = float(b.get("start", 0))
                    end_time = float(b.get("end", 0))
                    semantic_type = b.get("type", "symptom")
                    if end_time - start_time < 0.5:
                        continue
                    candidate_matches.append({
                        "start_time": start_time,
                        "end_time": end_time,
                        "duration": round(end_time - start_time, 3),
                        "semantic_type": semantic_type,
                        "text": b.get("reason", "关键词中插"),
                        "is_transition": False,
                        "trigger_reason": "keyword_plan",
                        "emotion_strength": "medium",
                        "intent": "",
                        "action_type": "",
                        "scene_hint": "",
                        "entities": [],
                    })
        except Exception as e:
            print(f"   [WARN] 关键词规则规划失败: {e}")
```

- [ ] **Step 3: 修复重复调用时的局部变量问题**

确认 `generate_editing_plan_with_llm` 调用处后面没有使用 `plan` 变量前未定义的 bug。确保原始 `plan = llm_clip_matcher.generate_editing_plan_with_llm(...)` 调用在 `if llm_api_key:` 内且赋值给 `plan`。

- [ ] **Step 4: 更新两个调用方传递 speech_video_path**

在 `batch_otc_promo_workflow.py` L126 行：
```python
        matches, sfx_list, bgm_emotion = smart_material_matching(
            video_duration,
            product_videos,
            symptom_videos,
            sensitivity=sensitivity,
            video_id=os.path.splitext(speech_video['filename'])[0],
            tracker=tracker,
            batch_tracker=batch_tracker,
            speech_video_path=speech_video['path'],
        )
```

在 `otc_promo_workflow.py` L2462 行：
```python
    matches, sfx_list, bgm_emotion = smart_material_matching(
        video_duration=selected_video['duration'],
        product_videos=product_videos,
        symptom_videos=symptom_videos,
        sensitivity=sensitivity,
        video_id=video_id,
        tracker=tracker,
        speech_video_path=selected_video['path'],
    )
```

- [ ] **Step 5: 验证语法**

```bash
python -c "import ast; ast.parse(open('otc_promo_workflow.py', encoding='utf-8').read()); print('OK')"
python -c "import ast; ast.parse(open('batch_otc_promo_workflow.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add otc_promo_workflow.py batch_otc_promo_workflow.py
git commit -m "feat: wire Whisper transcription into smart material matching pipeline

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: 解除贴图/广审的 is_review_version 门控

**Files:**
- Modify: `otc_promo_workflow.py`

- [ ] **Step 1: 移除广审和贴图轨道的 is_review_version 条件**

找到 L1939 行 `if is_review_version:` 和其下直到 L2024 的广审+贴图代码块。将其从 `if is_review_version:` 块中移出，使其始终执行。

找到：
```python
            if is_review_version:
                # 3. 添加广审素材轨道 (Ad Review)
                print("   添加广审素材轨道...")
                ...（广审和贴图代码）
```

将广审代码块和贴图代码块从 `if is_review_version:` 内部移出，改为无条件执行（内部的文件夹存在检查已足够）。

具体：删除 `if is_review_version:` 行，减少一级缩进，保留广审/贴图代码块的完整逻辑。`is_review_version` 仍控制上面的 watermark 轨道（L1873），不修改那部分。

- [ ] **Step 2: 确保 track hierarchy 报告包含广审/贴图**

找到 L2204 行 `if is_review_version:` 控制的 track_hierarchy 扩展代码：
```python
        if is_review_version:
            track_hierarchy.extend(
                [
                    {"track_id": 3, "name": "05_Ad_Review", ...},
                    {"track_id": 4, "name": "06_Top_Sticker", ...},
                ]
            )
```

改为根据实际是否成功添加来动态扩展：
- 用布尔变量 `ad_review_added` 和 `sticker_added` 控制是否加入 track_hierarchy
- 或者简单地将此条件也移除（始终在 track_hierarchy 中列出，即使未添加也不影响功能）

改为：
```python
        if ad_review_added:
            track_hierarchy.append(
                {"track_id": 3, "name": "05_Ad_Review", "content": "广审文件", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "bottom_10%"}
            )
        if sticker_added:
            track_hierarchy.append(
                {"track_id": 4, "name": "06_Top_Sticker", "content": "顶部贴图", "duration": speech_duration, "is_full_duration": True, "opacity": "100%", "position": "top_10%"}
            )
```

- [ ] **Step 3: 验证语法**

```bash
python -c "import ast; ast.parse(open('otc_promo_workflow.py', encoding='utf-8').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add otc_promo_workflow.py
git commit -m "feat: always add ad review and sticker tracks when directories have files

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: 端到端验证

**Files:**
- Test: `tests/test_speech_transcriber.py` (新建，可选)

- [ ] **Step 1: 核心模块导入验证**

```bash
cd d:/trae/云锋剪辑
python -c "
from speech_transcriber import transcribe_speech, _resolve_ffmpeg, _load_whisper_model
from llm_clip_matcher import generate_editing_plan_with_transcript, plan_insertions_with_keywords
from otc_promo_workflow import smart_material_matching, create_otc_promo_video
print('All imports OK')
"
```

- [ ] **Step 2: 转写接口单元测试（不依赖 Whisper）**

```python
# tests/test_speech_transcriber.py
import os
import json
import tempfile
from speech_transcriber import (
    read_cached_transcript,
    write_transcript,
    _resolve_ffmpeg,
)

def test_read_write_cache():
    d = tempfile.mkdtemp()
    try:
        cached = read_cached_transcript("test_video", d)
        assert cached is None, "cache should be empty initially"

        segments = [
            {"start": 0.0, "end": 2.5, "text": "大家好"},
            {"start": 2.5, "end": 5.0, "text": "今天介绍这款药品"},
        ]
        write_transcript("test_video", segments, d)

        loaded = read_cached_transcript("test_video", d)
        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0]["text"] == "大家好"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

def test_ffmpeg_resolve():
    ffmpeg = _resolve_ffmpeg()
    assert ffmpeg is not None
    assert "ffmpeg" in ffmpeg
```

- [ ] **Step 3: 关键词规则单元测试**

```python
# 在 tests/ 中追加
def test_keyword_planning():
    from llm_clip_matcher import plan_insertions_with_keywords

    segments = [
        {"start": 0.0, "end": 3.0, "text": "大家好欢迎收看"},
        {"start": 3.0, "end": 8.0, "text": "皮肤瘙痒红肿非常难受"},
        {"start": 8.0, "end": 15.0, "text": "这款产品能够有效抑菌止痒"},
        {"start": 15.0, "end": 20.0, "text": "使用方法是每天涂抹两次"},
    ]
    plan = plan_insertions_with_keywords(segments, video_duration=20.0)
    assert "b_rolls" in plan
    # 至少应在 symptom 句和 product 句处各产生一个插入
    types = [b["type"] for b in plan["b_rolls"]]
    assert "symptom" in types, f"expected symptom insert, got: {types}"
    assert "product" in types, f"expected product insert, got: {types}"
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest tests/test_speech_transcriber.py -v
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_speech_transcriber.py
git commit -m "test: add unit tests for speech transcriber and keyword planning

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
