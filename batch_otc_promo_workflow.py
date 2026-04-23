"""
OTC药品推广视频批量智能剪辑工作流
批量处理所有口播视频，生成符合OTC药品推广规范的剪映草稿
"""

import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from app_paths import build_runtime_env
from batch_runtime_config import BATCH_CONCURRENCY
from material_pool_rules import validate_material_pools, write_material_pool_report

os.environ.update(build_runtime_env(os.environ))

# 导入主工作流模块
from otc_promo_workflow import (
    collect_video_files,
    transcribe_with_ai,
    smart_material_matching,
    create_otc_promo_video,
    SPEECH_DIR,
    PRODUCT_DIR,
    SYMPTOM_DIR,
    UsageTracker,
    AD_FREQ_LIMIT,
    STICKER_FREQ_LIMIT,
    BROLL_FREQ_LIMIT,
    MATERIAL_POOL_REPORT_PATH,
)


def _process_single_speech_video(speech_video, product_videos, symptom_videos, sensitivity):
    result = {
        'video': speech_video['filename'],
        'project': f"OTC推广_{os.path.splitext(speech_video['filename'])[0]}",
        'status': '失败',
        'subtitles': 0,
        'matches': 0,
        'product_matches': 0,
        'symptom_matches': 0,
        'error': '',
    }

    try:
        subtitles = transcribe_with_ai(speech_video['path'])
        limits = {
            "ad_review": AD_FREQ_LIMIT,
            "sticker": STICKER_FREQ_LIMIT,
            "broll": BROLL_FREQ_LIMIT
        }
        tracker = UsageTracker(limits)
        matches, sfx_list, bgm_emotion = smart_material_matching(
            subtitles,
            product_videos,
            symptom_videos,
            sensitivity=sensitivity,
            video_duration=speech_video['duration'],
            video_id=os.path.splitext(speech_video['filename'])[0],
            tracker=tracker
        )

        result['subtitles'] = len(subtitles)
        result['matches'] = len(matches)
        result['symptom_matches'] = sum(1 for m in matches if m['material_type'] == "病症困扰")
        result['product_matches'] = sum(1 for m in matches if m['material_type'] == "产品展示")

        success = create_otc_promo_video(
            result['project'],
            speech_video['path'],
            matches,
            subtitles,
            sfx_list=sfx_list,
            bgm_emotion=bgm_emotion,
            tracker=tracker,
            is_review_version=False
        )
        result['status'] = '成功' if success else '失败'
        if not success:
            result['error'] = 'create_otc_promo_video returned False'
        return result
    except Exception as exc:
        result['error'] = str(exc)
        return result


def batch_process_otc_videos(sensitivity='high', limit=0):
    """批量处理所有口播视频"""
    print("=" * 80)
    print("OTC药品推广视频批量智能剪辑工作流")
    print("=" * 80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"灵敏度设置: {sensitivity}\n")

    # 1. 收集素材
    print("步骤1: 收集视频素材...")
    speech_videos = collect_video_files(SPEECH_DIR)
    product_videos = collect_video_files(PRODUCT_DIR)
    symptom_videos = collect_video_files(SYMPTOM_DIR)

    print(f"   - 口播视频: {len(speech_videos)} 个")
    print(f"   - 产品视频: {len(product_videos)} 个")
    print(f"   - 病症视频: {len(symptom_videos)} 个\n")

    if not speech_videos:
        print("错误: 没有找到口播视频！")
        return

    product_videos, symptom_videos, report = validate_material_pools(
        product_videos=product_videos,
        symptom_videos=symptom_videos,
        sensitivity=sensitivity,
    )
    write_material_pool_report(report, MATERIAL_POOL_REPORT_PATH)
    print("步骤1.1: 素材池校验通过")
    print(f"   - 产品独立素材: {report['product_after']} 个")
    print(f"   - 病症独立素材: {report['symptom_after']} 个\n")

    # 2. 批量处理
    print("步骤2: 批量处理口播视频...\n")
    success_count = 0
    failed_count = 0
    results = []
    selected_videos = speech_videos[:limit] if limit > 0 else speech_videos
    print(f"   使用真实批量并发: {BATCH_CONCURRENCY}")

    with ThreadPoolExecutor(max_workers=BATCH_CONCURRENCY) as executor:
        future_map = {
            executor.submit(_process_single_speech_video, speech_video, product_videos, symptom_videos, sensitivity): speech_video
            for speech_video in selected_videos
        }

        for index, future in enumerate(as_completed(future_map), 1):
            speech_video = future_map[future]
            print("=" * 80)
            print(f"处理进度: [{index}/{len(selected_videos)}]")
            print(f"当前视频: {speech_video['filename']}")
            print(f"视频时长: {speech_video['duration']:.1f}秒")
            print("=" * 80)

            task_result = future.result()
            results.append(task_result)

            print(f"   - 病症素材: {task_result['symptom_matches']} 处")
            print(f"   - 产品素材: {task_result['product_matches']} 处")
            print(f"   - 素材灵敏度: {sensitivity}")

            if task_result['status'] == '成功':
                success_count += 1
            else:
                failed_count += 1
                if task_result['error']:
                    print(f"\n[错误] 处理失败: {task_result['error']}")

            print()

    # 4. 交付前清理空草稿与无效草稿
    # 不再在此处调用，防止跨进程竞态条件误删
    # print("步骤3: 交付前清理无效草稿...")
    # cleanup_count = 0
    # try:
    #     from otc_promo_workflow import JyProject
    #     import json
    #     import shutil
    #     
    #     # 尝试获取剪映草稿目录
    #     draft_path = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'JianyingPro', 'User Data', 'Projects', 'com.lveditor.draft')
    #     if os.path.exists(draft_path):
    #         for folder_name in os.listdir(draft_path):
    #             # 只清理本次任务可能生成的 "OTC推广_" 开头的草稿，防止误删用户的其他草稿
    #             if folder_name.startswith("OTC推广_"):
    #                 folder_path = os.path.join(draft_path, folder_name)
    #                 if os.path.isdir(folder_path):
    #                     meta_file = os.path.join(folder_path, "draft_meta_info.json")
    #                     content_file = os.path.join(folder_path, "draft_content.json")
    #                     
    #                     is_empty = False
    #                     # 如果连内容文件都没有，绝对是生成中途崩溃的空草稿
    #                     if not os.path.exists(content_file):
    #                         is_empty = True
    #                     elif os.path.exists(meta_file):
    #                         try:
    #                             with open(meta_file, 'r', encoding='utf-8') as f:
    #                                 meta = json.load(f)
    #                                 # 如果草稿时间为 0，也是无效的空草稿
    #                                 if meta.get("tm_duration", 0) == 0:
    #                                     is_empty = True
    #                         except Exception:
    #                             pass
    #                             
    #                     if is_empty:
    #                         try:
    #                             shutil.rmtree(folder_path)
    #                             cleanup_count += 1
    #                             print(f"   [清理] 已删除空草稿/无效草稿: {folder_name}")
    #                         except Exception as e:
    #                             print(f"   [警告] 清理空草稿失败 {folder_name}: {e}")
    # except Exception as e:
    #     print(f"   [警告] 执行清理过程出错: {e}")

    # 5. 输出总结
    print("=" * 80)
    print("批量处理完成！")
    print("=" * 80)
    print(f"总视频数: {len(selected_videos)}")
    print(f"成功: {success_count} 个")
    print(f"失败: {failed_count} 个")
    print(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    print("处理详情:")
    print("-" * 80)
    for result in results:
        status_icon = "[成功]" if result['status'] == '成功' else "[失败]"
        print(f"{status_icon} {result['video']:<40} -> {result['project']}")
        if result['status'] == '成功':
            print(f"      字幕: {result['subtitles']} 条, 素材: {result['matches']} 处")

    print("\n您可以在剪映中打开这些草稿进行人工审核和修改")
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='OTC药品推广视频批量智能剪辑工作流')
    parser.add_argument('--sensitivity', '-s', choices=['medium', 'high'], default='high',
                        help='素材插入灵敏度: medium=中密度, high=高密度')
    parser.add_argument('--limit', '-n', type=int, default=0,
                        help='处理视频数量上限（0=处理全部）')
    args = parser.parse_args()
    batch_process_otc_videos(sensitivity=args.sensitivity, limit=args.limit)
