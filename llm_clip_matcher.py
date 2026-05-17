import base64
import json
import re
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

from batch_runtime_config import get_llm_connection_pool_limit

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

MODEL_ALIASES = {
    "deepseek-v3": "deepseek-v3",
    "deepseek_v3": "deepseek-v3",
    "deepseek v3": "deepseek-v3",
    "DeepSeek-V3": "deepseek-v3",
    "Deepseek-V3": "deepseek-v3",
    "deepseek-chat": "deepseek-chat",
    "DeepSeek-Chat": "deepseek-chat",
    "deepseek-v3.2": "deepseek-v3.2",
    "DeepSeek-V3.2": "deepseek-v3.2",
}

DEFAULT_LLM_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def _normalize_base_url(base_url: Optional[str]) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    return normalized or "https://api.openai.com"


def normalize_model_name(model: Optional[str]) -> str:
    raw = str(model or "").strip()
    if not raw:
        return DEFAULT_LLM_MODEL
    compact = raw.replace("_", "-").strip()
    lowered = compact.lower()
    return MODEL_ALIASES.get(raw, MODEL_ALIASES.get(compact, MODEL_ALIASES.get(lowered, compact)))


def _build_chat_completion_endpoints(base_url: Optional[str]) -> List[str]:
    normalized = _normalize_base_url(base_url)
    if normalized.endswith("/chat/completions"):
        return [normalized]

    candidates: List[str] = []
    if normalized.endswith("/v1"):
        candidates.append(normalized + "/chat/completions")
        candidates.append(normalized.rsplit("/v1", 1)[0] + "/chat/completions")
    else:
        candidates.append(normalized + "/v1/chat/completions")
        candidates.append(normalized + "/chat/completions")

    deduped: List[str] = []
    for endpoint in candidates:
        endpoint = endpoint.rstrip("/")
        if endpoint and endpoint not in deduped:
            deduped.append(endpoint)
    return deduped


def _extract_response_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    text = str(content or "").strip()
    if text:
        return text
    return str(message.get("reasoning_content", "") or "").strip()


def _image_path_to_data_url(image_path: str) -> str:
    ext = os.path.splitext(str(image_path or ""))[1].lower()
    mime_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _call_chat_completions_http(
    *,
    api_key: str,
    model: str,
    base_url: Optional[str],
    messages: list[dict[str, Any]],
    timeout_seconds: float = 20.0,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": normalize_model_name(model),
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature

    retryable_statuses = {429, 500, 502, 503, 504}
    last_error: Optional[Exception] = None
    endpoints = _build_chat_completion_endpoints(base_url)

    for endpoint in endpoints:
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout_seconds,
                )
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if response.status_code in retryable_statuses:
                    detail = response.text[:300].strip()
                    if attempt < 3:
                        time.sleep(min(2 ** (attempt - 1), 3))
                        continue
                    raise RuntimeError(
                        f"服务通道暂时不可用（HTTP {response.status_code}）"
                        + (f": {detail}" if detail else "")
                    )
                response.raise_for_status()
                if "application/json" not in content_type:
                    detail = response.text[:120].strip().replace("\n", " ")
                    raise RuntimeError(
                        f"接口返回了非 JSON 响应，当前路径可能不是模型接口: {endpoint}"
                        + (f" | 响应片段: {detail}" if detail else "")
                    )
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(min(2 ** (attempt - 1), 3))
                    continue
                break
            except RuntimeError as exc:
                last_error = exc
                # 如果只是命中了站点页面而不是 API，结束当前 endpoint 的重试，继续探测下一个兼容路径。
                if "当前路径可能不是模型接口" in str(exc):
                    break
                raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("未知接口错误")


def _call_chat_completions(
    *,
    api_key: str,
    model: str,
    base_url: Optional[str],
    messages: list[dict[str, Any]],
    timeout_seconds: float = 20.0,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    # 优先直连 OpenAI-compatible HTTP 接口，避免 GUI 环境因为缺少 openai SDK 无法联通。
    try:
        return _call_chat_completions_http(
            api_key=api_key,
            model=model,
            base_url=base_url,
            messages=messages,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as http_exc:
        if OpenAI is None:
            raise RuntimeError(f"接口请求失败: {http_exc}") from http_exc

        last_sdk_error: Optional[Exception] = None
        for attempt in range(1, 4):
            http_client = None
            try:
                import httpx

                http_client = httpx.Client(
                    limits=httpx.Limits(
                        max_connections=get_llm_connection_pool_limit(),
                        max_keepalive_connections=get_llm_connection_pool_limit(),
                    ),
                    timeout=timeout_seconds,
                )
                client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    http_client=http_client,
                )
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature if temperature is not None else 0.3,
                )
                return response.model_dump() if hasattr(response, "model_dump") else dict(response)
            except Exception as sdk_exc:
                last_sdk_error = sdk_exc
                if "当前路径可能不是模型接口" in str(http_exc) or "非 JSON" in str(http_exc):
                    raise RuntimeError(
                        "接口返回的是网页而不是模型 JSON。当前内部通道大概率没有命中正确的 OpenAI-compatible 接口，"
                        f"请优先检查网关是否仍支持 `{_normalize_base_url(base_url)}/v1/chat/completions`。"
                    ) from sdk_exc
                if attempt < 3:
                    time.sleep(min(2 ** (attempt - 1), 3))
                    continue
            finally:
                if http_client is not None:
                    try:
                        http_client.close()
                    except OSError:
                        pass

        raise RuntimeError(f"HTTP直连接口失败: {http_exc}; SDK回退重试3次后仍失败: {last_sdk_error}") from last_sdk_error


def test_llm_connectivity(api_key: str, model: str = DEFAULT_LLM_MODEL, base_url: str = None) -> tuple[bool, str]:
    """测试大模型 API 连通性，返回(是否成功, 说明信息)。"""
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "请先填写 API Key。"
    normalized_model = normalize_model_name(model)

    try:
        response_payload = _call_chat_completions(
            api_key=api_key,
            model=normalized_model,
            base_url=base_url,
            messages=[{"role": "user", "content": "ping"}],
            timeout_seconds=10.0,
            max_tokens=4,
            temperature=0.0,
        )
        content = _extract_response_text(response_payload)
        alias_note = f"，已自动映射为 {normalized_model}" if normalized_model != str(model).strip() else ""
        return True, f"联通成功（模型: {normalized_model}{alias_note}）{('，响应: ' + content[:60]) if content else ''}"
    except Exception as e:
        message = str(e)
        if "网页而不是模型 JSON" in message or "非 JSON" in message or "<!DOCTYPE html>" in message:
            return False, (
                "联通失败：当前内部通道返回的是网页，不是模型接口 JSON。"
                " 这通常说明网关路由没有命中 OpenAI-compatible 接口。"
                " 当前程序已优先按 `/v1/chat/completions` 测试；如果仍失败，请检查这条内部通道是否仍可用于大模型接口。"
            )
        if "HTTP 401" in message or "Unauthorized" in message or "无效的令牌" in message:
            return False, "联通失败：API Key 无效或已过期，请检查后重试。"
        if "HTTP 404" in message or "model_not_found" in message:
            return False, f"联通失败：模型 `{normalized_model}` 不存在或当前通道不支持。"
        if "HTTP 429" in message:
            return False, "联通失败：请求过于频繁或额度不足，请稍后重试。"
        if "无可用渠道" in message or "no available distributor" in message.lower():
            return False, f"联通失败：当前通道下模型 `{normalized_model}` 暂无可用渠道，请改用其他可用模型后重试。"
        if "服务通道暂时不可用（HTTP 503）" in message or "503 Server Error" in message:
            return False, "联通失败：内部服务通道当前繁忙或临时不可用，请稍后重试。"
        if "HTTP 502" in message or "HTTP 504" in message:
            return False, "联通失败：服务网关异常，请稍后重试。"
        return False, f"联通失败: {message}"


def describe_image_with_llm(
    *,
    api_key: str,
    model: str,
    base_url: Optional[str],
    prompt: str,
    image_path: str,
    max_tokens: int = 160,
) -> str:
    api_key = (api_key or "").strip()
    image_path = str(image_path or "").strip()
    if not api_key:
        raise RuntimeError("缺少 API Key")
    if not image_path or not os.path.exists(image_path):
        raise RuntimeError(f"图片不存在: {image_path}")

    response_payload = _call_chat_completions(
        api_key=api_key,
        model=normalize_model_name(model),
        base_url=base_url,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _image_path_to_data_url(image_path)}},
                ],
            }
        ],
        timeout_seconds=45.0,
        max_tokens=max_tokens,
        temperature=0.2,
    )
    content = _extract_response_text(response_payload)
    if not content:
        raise RuntimeError("视觉模型未返回有效文本")
    return content


def _create_vision_test_image() -> str:
    png_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR4nGP8z8Dwn4GBgYGJAQoAHxcC"
        "Ar7cO2QAAAAASUVORK5CYII="
    )
    fd, path = tempfile.mkstemp(prefix="vf_vlm_ping_", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(png_base64.encode("ascii")))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def test_vision_connectivity(api_key: str, model: str, base_url: str = None) -> tuple[bool, str]:
    api_key = (api_key or "").strip()
    normalized_model = normalize_model_name(model)
    if not api_key:
        return False, "请先填写 API Key。"
    if not normalized_model:
        return False, "请先填写视觉模型名称。"

    temp_image_path = _create_vision_test_image()
    try:
        content = describe_image_with_llm(
            api_key=api_key,
            model=normalized_model,
            base_url=base_url,
            prompt="请用一句中文短句说明这是一张测试图片，并回答“联通正常”。",
            image_path=temp_image_path,
            max_tokens=40,
        )
        return True, (
            f"视觉模型联通成功（模型: {normalized_model}）"
            + (f"，响应: {content[:80]}" if content else "")
        )
    except Exception as e:
        message = str(e)
        if "网页而不是模型 JSON" in message or "非 JSON" in message or "<!DOCTYPE html>" in message:
            return False, (
                "视觉联通失败：当前内部通道返回的是网页，不是模型接口 JSON。"
                " 请检查该网关是否支持 OpenAI-compatible 的图文输入。"
            )
        if "HTTP 401" in message or "Unauthorized" in message or "无效的令牌" in message:
            return False, "视觉联通失败：API Key 无效或已过期，请检查后重试。"
        if "HTTP 404" in message or "model_not_found" in message:
            return False, f"视觉联通失败：视觉模型 `{normalized_model}` 不存在或当前通道不支持。"
        if "HTTP 429" in message:
            return False, "视觉联通失败：请求过于频繁或额度不足，请稍后重试。"
        if "视觉模型未返回有效文本" in message:
            return False, f"视觉联通失败：模型 `{normalized_model}` 没有返回可解析文本。"
        return False, f"视觉联通失败: {message}"
    finally:
        try:
            os.remove(temp_image_path)
        except OSError:
            pass


def generate_editing_plan_with_llm(
    video_duration: float,
    api_key: str,
    model: str = DEFAULT_LLM_MODEL,
    base_url: str = None,
    density_config: dict = None
) -> dict:
    """
    使用大模型基于总时长和节奏参数生成结构化的剪辑剧本（中插、音效、BGM情感）。

    参数:
        video_duration: 口播视频总时长（秒）
        api_key: 大模型 API Key
        model: 模型名称
        base_url: 自定义 API 地址
        density_config: 节奏模板配置

    返回:
        解析后的 JSON 字典，包含 b_rolls, sfx, bgm_emotion
    """
    density_info = ""
    if density_config:
        density_info = (
            f"节奏要求：每 {density_config.get('window_seconds', 30)} 秒至少 "
            f"{density_config.get('min_segments_per_window', 2)} 段中插，"
            f"每段 {density_config.get('insert_min_duration', 1.0)}-{density_config.get('insert_max_duration', 3.0)} 秒。\n"
        )

    example_output = {
        "b_rolls": [
            {"start": 4.0, "end": 7.0, "type": "symptom", "reason": "病症痛点段落"},
            {"start": 16.0, "end": 18.5, "type": "product", "reason": "产品介绍段落"}
        ],
        "sfx": [
            {"time": 4.0, "type": "whoosh", "reason": "引入病症切入点加转场音效"},
            {"time": 16.0, "type": "ding", "reason": "引出产品加提示音效"}
        ],
        "bgm_emotion": "positive"
    }

    prompt = (
        "你是一个专业的视频剪辑大师。现在有一段口播视频，总时长为 "
        f"{video_duration:.1f} 秒。\n"
        f"{density_info}"
        "请根据总时长合理规划中插视频（B-Rolls）的时间分布，遵循以下原则：\n"
        "1. 中插视频 (B-Rolls)：\n"
        "   - 前半段优先放置病症/痛点素材（symptom），后半段优先放置产品/解决素材（product）\n"
        "   - 每段中插建议 1.5 到 3 秒之间，两段中插之间保留口播主体画面\n"
        "   - 中插总时长尽量占到口播总时长的 60% 左右\n"
        "   - 时间分布均匀，避免某一段过于密集\n\n"
        "2. 音效 (SFX)：在关键转折点插入音效，提供精准时间戳。\n\n"
        "3. 背景音乐情感 (BGM Emotion)：positive, negative 或 neutral。\n\n"
        "请严格只输出一个 JSON 对象，不要包含 markdown 标记或其他多余文本。格式示例：\n"
        f"{json.dumps(example_output, ensure_ascii=False, indent=2)}"
    )

    normalized_model = normalize_model_name(model)
    print(f"   [LLM] 正在使用大模型 ({normalized_model}) 基于总时长进行剪辑规划...")

    try:
        response_payload = _call_chat_completions(
            api_key=api_key,
            model=normalized_model,
            base_url=base_url,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        result_text = _extract_response_text(response_payload)

        # 清理 markdown 标记
        result_text = re.sub(r'^```json\s*', '', result_text)
        result_text = re.sub(r'^```\s*', '', result_text)
        result_text = re.sub(r'\s*```$', '', result_text)

        plan = json.loads(result_text)

        # 简单校验
        if "b_rolls" not in plan: plan["b_rolls"] = []
        if "sfx" not in plan: plan["sfx"] = []
        if "bgm_emotion" not in plan: plan["bgm_emotion"] = "neutral"

        return plan
    except Exception as e:
        print(f"   [LLM Error] API 调用或解析失败: {str(e)}")
        if 'result_text' in locals():
            print(f"   [LLM Raw Output] {result_text}")
        return None


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

        valid_b_rolls = []
        for b in plan["b_rolls"]:
            if float(b.get("end", 0)) > float(b.get("start", 0)):
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
    last_end = -999.0
    symptom_keywords = [
        "发痒", "痒", "抓挠", "红肿", "皮损", "脱皮", "起皮",
    ] + list(SEMANTIC_KEYWORDS["symptom"])
    product_keywords = [
        "药膏", "乳膏", "软膏", "凝胶", "喷剂", "喷雾", "搽剂", "药盒", "外盒",
        "药品", "这款药", "这个药", "本品", "药",
    ] + list(SEMANTIC_KEYWORDS["product"])

    for seg in transcript_segments:
        text = str(seg.get("text", "")).lower()
        if not text:
            continue

        symptom_hit_words = [kw for kw in symptom_keywords if kw in text]
        product_hit_words = [kw for kw in product_keywords if kw in text]
        symptom_hits = len(symptom_hit_words)
        product_hits = len(product_hit_words)

        seg_type = None
        trigger_keyword = None
        if symptom_hits > product_hits and symptom_hits > 0:
            seg_type = "symptom"
            trigger_keyword = symptom_hit_words[0]
        elif product_hits > symptom_hits and product_hits > 0:
            seg_type = "product"
            trigger_keyword = product_hit_words[0]

        if seg_type:
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", 0))
            seg_dur = seg_end - seg_start
            if seg_type == last_type and (seg_start - last_end) < 1.2:
                continue

            if seg_dur >= 0.8:
                insert_dur = min(seg_dur, insert_max)
                insert_start = seg_start + (seg_dur - insert_dur) / 2
                b_rolls.append({
                    "start": round(insert_start, 1),
                    "end": round(insert_start + insert_dur, 1),
                    "type": seg_type,
                    "reason": f"关键词命中: {trigger_keyword or text[:20]}",
                    "keyword": trigger_keyword or "",
                })
                last_type = seg_type
                last_end = float(insert_start + insert_dur)

    b_rolls.sort(key=lambda b: b["start"])

    for b in b_rolls:
        b["start"] = max(0.0, min(video_duration - 0.5, float(b.get("start", 0))))
        b["end"] = max(float(b["start"]) + 0.5, min(video_duration, float(b.get("end", 0))))

    return {"b_rolls": b_rolls, "sfx": [], "bgm_emotion": "neutral"}
