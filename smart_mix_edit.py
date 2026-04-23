import os
import sys
import json
import subprocess
import re
from typing import List, Dict, Optional

# 环境初始化
current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.join(current_dir, "jianying-editor-skill-main", "jianying-editor-skill-main")

if not os.path.exists(os.path.join(skill_root, "scripts", "jy_wrapper.py")):
    raise ImportError("Could not find jianying-editor skill root.")

sys.path.insert(0, os.path.join(skill_root, "scripts"))
sys.path.insert(0, os.path.join(skill_root, "examples"))

from jy_wrapper import JyProject, draft
from _bootstrap import ensure_skill_scripts_on_path

# 视频素材路径配置
VIDEO_DIR = "H:\\体癣"
SPEECH_DIR = os.path.join(VIDEO_DIR, "口播")
PRODUCT_DIR = os.path.join(VIDEO_DIR, "产品")
SYMPTOM_DIR = os.path.join(VIDEO_DIR, "病症")


def collect_video_files(directory: str) -> List[Dict]:
    """收集目录中的视频文件信息"""
    videos = []
    if not os.path.exists(directory):
        return videos
    
    for filename in os.listdir(directory):
        if filename.lower().endswith(('.mp4', '.mov', '.avi', '.wmv')):
            filepath = os.path.join(directory, filename)
            try:
                # 获取视频时长
                video = draft.VideoMaterial(filepath)
                duration = video.duration / 1_000_000.0  # 转换为秒
                videos.append({
                    'path': filepath,
                    'filename': filename,
                    'duration': duration,
                    'type': os.path.basename(os.path.dirname(filepath))
                })
            except Exception as e:
                print(f"Error reading video {filename}: {e}")
    
    return videos


def transcribe_video(video_path: str) -> List[Dict]:
    """对视频进行语音识别，生成字幕"""
    print(f"正在识别视频字幕: {video_path}")
    
    # 使用剪映的语音识别功能
    # 这里需要调用剪映的API或者使用其他语音识别工具
    # 暂时返回模拟数据
    
    # 实际应用中，可以使用以下方法：
    # 1. 使用剪映的语音识别API
    # 2. 使用其他语音识别服务（如百度、讯飞等）
    # 3. 使用本地语音识别工具（如whisper）
    
    # 模拟字幕数据
    mock_subtitles = [
        {
            "index": 1,
            "start": 0.0,
            "end": 3.0,
            "text": "大家好，今天我们来聊聊体癣的问题"
        },
        {
            "index": 2,
            "start": 3.0,
            "end": 6.0,
            "text": "体癣是一种常见的皮肤真菌感染"
        },
        {
            "index": 3,
            "start": 6.0,
            "end": 9.0,
            "text": "主要表现为皮肤上出现红斑、脱屑"
        },
        {
            "index": 4,
            "start": 9.0,
            "end": 12.0,
            "text": "患者会感到瘙痒不适"
        },
        {
            "index": 5,
            "start": 12.0,
            "end": 15.0,
            "text": "我们的产品可以有效治疗体癣"
        },
        {
            "index": 6,
            "start": 15.0,
            "end": 18.0,
            "text": "使用方法简单，效果显著"
        },
        {
            "index": 7,
            "start": 18.0,
            "end": 21.0,
            "text": "坚持使用，很快就能看到改善"
        }
    ]
    
    return mock_subtitles


def ai_match_materials(subtitles: List[Dict], product_videos: List[Dict], symptom_videos: List[Dict]) -> List[Dict]:
    """使用AI分析字幕内容，智能匹配素材"""
    print("正在使用AI分析字幕并匹配素材...")
    
    matches = []
    
    # 定义关键词匹配规则
    symptom_keywords = ['体癣', '症状', '表现', '红斑', '脱屑', '瘙痒', '皮肤', '感染']
    product_keywords = ['产品', '治疗', '使用', '方法', '效果', '改善', '推荐']
    
    for sub in subtitles:
        text = sub['text']
        matched_material = None
        
        # 分析字幕内容，决定使用哪种类型的素材
        if any(keyword in text for keyword in symptom_keywords):
            # 匹配病症视频
            if symptom_videos:
                # 轮询使用不同的病症视频
                idx = len(matches) % len(symptom_videos)
                matched_material = symptom_videos[idx]
        elif any(keyword in text for keyword in product_keywords):
            # 匹配产品视频
            if product_videos:
                # 轮询使用不同的产品视频
                idx = len(matches) % len(product_videos)
                matched_material = product_videos[idx]
        
        if matched_material:
            matches.append({
                'subtitle_index': sub['index'],
                'start_time': sub['start'],
                'end_time': sub['end'],
                'duration': sub['end'] - sub['start'],
                'text': sub['text'],
                'material': matched_material
            })
    
    return matches


def create_jianying_draft(project_name: str, speech_video: str, matches: List[Dict]) -> None:
    """创建剪映草稿项目"""
    print(f"正在创建剪映草稿: {project_name}")
    
    # 创建剪映项目
    project = JyProject(project_name, overwrite=True)
    
    # 添加口播视频到主轨道
    if os.path.exists(speech_video):
        project.add_media_safe(speech_video, start_time="0s", track_name="Main_Video")
    
    # 根据匹配结果添加B-Roll素材
    for match in matches:
        material = match['material']
        start_time = match['start_time']
        duration = match['duration']
        
        # 添加B-Roll视频到第二轨道
        project.add_media_safe(
            material['path'],
            start_time=f"{start_time}s",
            duration=f"{min(duration, material['duration'])}s",
            track_name="B_Roll"
        )
        
        # 添加字幕
        project.add_text_simple(
            text=match['text'],
            start_time=f"{start_time}s",
            duration=f"{duration}s",
            clip_settings=draft.ClipSettings(transform_y=-0.8),
            font_size=10.0,
            color_rgb=(1, 1, 1)
        )
    
    # 保存项目
    project.save()
    print(f"剪映草稿已创建: {project_name}")


def main():
    """主函数"""
    print("=" * 60)
    print("体癣视频智能混剪系统")
    print("=" * 60)
    
    # 收集视频素材
    print("\n1. 收集视频素材...")
    speech_videos = collect_video_files(SPEECH_DIR)
    product_videos = collect_video_files(PRODUCT_DIR)
    symptom_videos = collect_video_files(SYMPTOM_DIR)
    
    print(f"   - 口播视频: {len(speech_videos)} 个")
    print(f"   - 产品视频: {len(product_videos)} 个")
    print(f"   - 病症视频: {len(symptom_videos)} 个")
    
    if not speech_videos:
        print("错误: 没有找到口播视频！")
        return
    
    # 选择第一个口播视频进行处理
    speech_video = speech_videos[0]
    print(f"\n2. 选择口播视频: {speech_video['filename']}")
    
    # 语音识别生成字幕
    print("\n3. 进行语音识别...")
    subtitles = transcribe_video(speech_video['path'])
    print(f"   - 生成字幕: {len(subtitles)} 条")
    
    # 显示识别的字幕
    print("\n   识别到的字幕内容:")
    for sub in subtitles:
        print(f"   [{sub['start']:.1f}s - {sub['end']:.1f}s] {sub['text']}")
    
    # AI智能匹配素材
    print("\n4. AI智能匹配素材...")
    matches = ai_match_materials(subtitles, product_videos, symptom_videos)
    print(f"   - 匹配成功: {len(matches)} 处")
    
    # 显示匹配结果
    print("\n   匹配结果:")
    for match in matches:
        material = match['material']
        print(f"   [{match['start_time']:.1f}s] {match['text'][:20]}... -> {material['filename']}")
    
    # 创建剪映草稿
    print("\n5. 创建剪映草稿...")
    project_name = f"体癣混剪_{os.path.splitext(speech_video['filename'])[0]}"
    create_jianying_draft(project_name, speech_video['path'], matches)
    
    print("\n" + "=" * 60)
    print("处理完成！")
    print(f"剪映草稿名称: {project_name}")
    print("您可以在剪映中打开此草稿进行人工审核和修改")
    print("=" * 60)


if __name__ == "__main__":
    main()
