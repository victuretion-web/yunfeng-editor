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


def generate_editing_plan_with_llm(subtitles: list, api_key: str, model: str = "deepseek-v3.2", base_url: str = None) -> dict:
    """
    使用大模型通读字幕，生成结构化的剪辑剧本（中插、音效、BGM情感）。
    
    参数:
        subtitles: 带有时间戳的口播字幕列表，例如 [{"start": 0.5, "end": 3.5, "text": "大家好"}]
        api_key: 大模型 API Key
        model: 模型名称
        base_url: 自定义 API 地址
        
    返回:
        解析后的 JSON 字典，包含 b_rolls, sfx, bgm_emotion
    """
    if not OpenAI:
        raise ImportError("未安装 openai 库。请在终端运行: pip install openai")
        
    # 构建输入文本
    transcript = ""
    for sub in subtitles:
        transcript += f"[{sub['start']:.1f}s - {sub['end']:.1f}s] {sub['text']}\n"
        
    example_output = {
        "b_rolls": [
            {"start": 4.0, "end": 7.0, "type": "symptom", "reason": "描述症状第一段"},
            {"start": 7.0, "end": 9.5, "type": "symptom", "reason": "描述症状第二段"},
            {"start": 16.0, "end": 18.5, "type": "product", "reason": "产品介绍第一段"}
        ],
        "sfx": [
            {"time": 4.0, "type": "whoosh", "reason": "从引入切入病症，加转场音效"},
            {"time": 16.0, "type": "ding", "reason": "引出产品，加提示音效"}
        ],
        "bgm_emotion": "positive"
    }

    prompt = (
        "你是一个专业的视频剪辑大师和语义分析专家。现在有一段完整的口播视频字幕，带有精确到秒的时间戳。\n"
        "请你通读全文，理解其中的语义逻辑、情绪起伏和转折点，并为我输出一份精确的“自动化剪辑剧本”。\n\n"
        f"口播字幕如下：\n{transcript}\n"
        "请完成以下三个维度的规划：\n"
        "1. 中插视频 (B-Rolls)：\n"
        "   - 识别哪些段落是在描述“病症/痛点”（symptom），哪些段落是在介绍“产品/解决”（product）。\n"
        "   - 优先把病症素材放在描述症状、困扰、痛点的句段，把产品素材放在介绍功效、认证、卖点、解决方案的句段。\n"
        "   - 每段中插视频的长度建议控制在 1.5 到 3 秒之间，尽量不要连续紧贴出现，两段中插之间尽量保留口播主体画面。\n"
        "   - 时间点必须严格与口播句子的起止时间对齐，确保中插前后口播内容连贯，不要把产品素材放到病症语义上，也不要把病症素材放到产品语义上。\n"
        "   - 如果某一段语义已经被中插覆盖，下一段请优先等待新的语义重点或明显转折点，而不是机械地连续插入。\n\n"
        "2. 音效 (SFX)：\n"
        "   - 在语义转折点、情绪强调点插入音效。\n"
        "   - 提供精准的时间戳 time 和推荐的音效类型。\n\n"
        "3. 背景音乐情感 (BGM Emotion)：\n"
        "   - 判断整段视频应使用 positive、negative 或 neutral。\n\n"
        "请严格只输出一个 JSON 对象，不要包含 markdown 标记或其他多余文本。格式示例：\n"
        f"{json.dumps(example_output, ensure_ascii=False, indent=2)}"
    )

    print(f"   [LLM] 正在使用大模型 ({model}) 进行全局语义分析...")
    
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
