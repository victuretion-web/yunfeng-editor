import json
import re
import os

from batch_runtime_config import LLM_CONNECTION_POOL_LIMIT

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def test_llm_connectivity(api_key: str, model: str = "deepseek-v3.2", base_url: str = None) -> tuple[bool, str]:
    """测试大模型 API 连通性，返回(是否成功, 说明信息)。"""
    if not OpenAI:
        return False, "未安装 openai 库，请先安装依赖。"

    api_key = (api_key or "").strip()
    if not api_key:
        return False, "请先填写 API Key。"

    http_client = None
    try:
        import httpx

        http_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=LLM_CONNECTION_POOL_LIMIT,
                max_keepalive_connections=LLM_CONNECTION_POOL_LIMIT,
            ),
            timeout=10.0,
        )
    except Exception:
        http_client = None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=4,
            temperature=0.0,
        )
        content = ""
        if response and response.choices:
            content = (response.choices[0].message.content or "").strip()
        return True, f"联通成功（模型: {model}）{('，响应: ' + content[:60]) if content else ''}"
    except Exception as e:
        return False, f"联通失败: {e}"
    finally:
        if http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass


def generate_editing_plan_with_llm(
    video_duration: float,
    api_key: str,
    model: str = "deepseek-v3.2",
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
    if not OpenAI:
        raise ImportError("未安装 openai 库。请在终端运行: pip install openai")

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

    print(f"   [LLM] 正在使用大模型 ({model}) 基于总时长进行剪辑规划...")

    http_client = None
    try:
        import httpx

        http_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=LLM_CONNECTION_POOL_LIMIT,
                max_keepalive_connections=LLM_CONNECTION_POOL_LIMIT,
            )
        )
    except Exception:
        http_client = None

    client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )

        result_text = response.choices[0].message.content.strip()

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
    finally:
        if http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass
