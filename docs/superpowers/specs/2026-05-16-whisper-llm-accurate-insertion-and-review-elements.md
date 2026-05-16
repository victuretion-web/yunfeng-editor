# Whisper + LLM 精准插入管线 & 贴图广审始终加入

## 1. 背景与问题

### 问题 1：素材插入不准确

当前 LLM 剪辑规划（`generate_editing_plan_with_llm`）只接收视频总时长，完全不知道口播内容。Prompt 写死"前半段放置 symptom，后半段放置 product"，LLM 在盲猜。当 LLM 不可用时降级为纯时间驱动等分窗口，更粗糙。

### 问题 2：贴图和广审素材未加入

贴图/广审轨道代码已实现，但被 `is_review_version=True` 门控。批量工作流始终传 `False`，导致这些素材从未出现在最终视频中。

## 2. 目标

1. 基于口播真实语义在精准时间点插入正确类型的素材（提到症状→插入症状视频，提到药品名→插入产品展示视频）
2. 每次任务自动检查贴图/广审文件夹，有素材则加入，没有则跳过

## 3. 架构

### 3.1 新增模块：speech_transcriber.py

```
口播视频文件
  ↓ FFmpeg 提取音轨（16kHz Mono WAV，临时文件）
  ↓ openai-whisper base 模型转写
逐句段落 [{start_s, end_s, text}, ...]
  ↓ 缓存到 output/transcriptions/{video_name}.json
```

- **模型加载**：单例加载 `base` 模型，避免重复加载
- **音频提取**：使用已 bundled 的 FFmpeg，提取 16kHz mono WAV
- **转写**：`whisper.transcribe()`，language="zh"，输出 segments
- **缓存**：同视频不重复转写，直接读缓存 JSON
- **错误处理**：FFmpeg 失败 / Whisper 失败均返回 None，不影响后续降级

### 3.2 修改模块：llm_clip_matcher.py

新增函数 `generate_editing_plan_with_transcript(video_duration, transcript_segments, api_key, model, base_url, density_config)`：

- Prompt 包含完整逐句口播文本及对应时间戳
- LLM 基于真实内容为每段口播标记语义类型（symptom/product/generic）
- LLM 规划精准中插时间点，给出 `{start, end, type, reason}`
- 保留旧函数 `generate_editing_plan_with_llm` 不变（无转写时的兜底）

新增函数 `plan_insertions_with_keywords(transcript_segments)`：

- 基于 `SEMANTIC_KEYWORDS` 关键词字典直接判断每段口播的语义类型
- 在 symptom→product 或 product→symptom 切换点生成中插候选
- 无 LLM 依赖，作为降级方案

### 3.3 修改模块：otc_promo_workflow.py

**smart_material_matching 流程变更：**

```
1. Whisper 转写口播视频 → transcript_segments
2. 有 LLM API Key + 转写成功 → generate_editing_plan_with_transcript()
3. 有转写成功但无 LLM → plan_insertions_with_keywords()
4. 转写失败 → 现有降级链（generate_editing_plan_with_llm / 时间驱动）
```

**贴图/广审门控移除：**
- `create_otc_promo_video` 中贴图和广审轨道不再检查 `is_review_version`
- 始终检查对应文件夹是否存在且非空
- 文件夹有素材→加入，没有→跳过并记录日志
- 移除 `is_review_version` 参数（或保留但默认 True 且不影响贴图/广审逻辑）

### 3.4 修改模块：batch_otc_promo_workflow.py

- `create_otc_promo_video` 调用中传 `is_review_version=True`（或使用无此参数的新签名）

## 4. 降级链

```
LLM + 转写（最准）
  ↓ LLM API 不可用或失败
关键词规则 + 转写（次准）
  ↓ Whisper 转写失败
LLM 时长规划（旧逻辑，盲猜但不依赖转写）
  ↓ LLM 不可用
时间驱动等分窗口（兜底，确保不中断）
```

## 5. 错误处理

- FFmpeg 不存在：跳过转写，降级到旧逻辑，输出 warning
- Whisper 模型加载失败：跳过转写，输出 warning
- 转写返回空结果：跳过转写，输出 warning
- LLM 调用失败：降级到关键词规则或旧逻辑
- 贴图/广审文件夹不存在：跳过对应轨道，输出 info 日志
- 贴图/广审文件夹存在但无有效文件：跳过，输出 warning

## 6. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| speech_transcriber.py | 新增 | Whisper 转写模块 |
| llm_clip_matcher.py | 修改 | 新增 2 个函数 |
| otc_promo_workflow.py | 修改 | 接入转写管线；移除贴图/广审门控 |
| batch_otc_promo_workflow.py | 修改 | is_review_version=True |
| requirements.in | 确认 | openai-whisper 已在列表中 |
