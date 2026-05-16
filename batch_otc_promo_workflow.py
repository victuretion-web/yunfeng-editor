"""
OTC药品推广视频批量智能剪辑工作流
批量处理所有口播视频，生成符合OTC药品推广规范的剪映草稿
"""

import os
import json
import glob
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from app_paths import configure_current_process
from batch_runtime_config import get_batch_concurrency
from draft_registry import get_draft_root
from material_pool_rules import validate_material_pools, write_material_pool_report

# 导入主工作流模块
from otc_promo_workflow import (
    collect_video_files,
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
    OUTPUT_DIR,
)


def _default_batch_state():
    return {"results": [], "meta": {}}


def _build_state_backup_path(state_path):
    base, ext = os.path.splitext(state_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ext or ".json"
    return f"{base}.corrupt_{timestamp}{suffix}"


def _load_batch_state(state_path):
    if not os.path.exists(state_path):
        return _default_batch_state()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("批处理状态文件不是 JSON 对象")
        data.setdefault("results", [])
        data.setdefault("meta", {})
        return data
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        backup_path = _build_state_backup_path(state_path)
        try:
            os.replace(state_path, backup_path)
            print(f"[WARN] 批处理状态文件损坏，已备份到: {backup_path}")
        except OSError as backup_exc:
            backup_path = ""
            print(f"[WARN] 批处理状态文件损坏，且备份失败: {backup_exc}")
        recovered = _default_batch_state()
        recovered["meta"] = {
            "recovered_from_corrupt_state": True,
            "corrupt_state_path": state_path,
            "backup_path": backup_path,
            "recovery_error": str(exc),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        return recovered


def _write_batch_state(state_path, payload):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _find_latest_artifacts(project_prefix, output_dir, draft_root):
    review_pattern = os.path.join(output_dir, f"{project_prefix}_*_审查报告.json")
    review_candidates = [path for path in glob.glob(review_pattern) if os.path.isfile(path)]
    review_path = max(review_candidates, key=os.path.getmtime) if review_candidates else ""

    draft_pattern = os.path.join(draft_root, f"{project_prefix}_*")
    draft_candidates = [path for path in glob.glob(draft_pattern) if os.path.isdir(path)]
    draft_dir = max(draft_candidates, key=os.path.getmtime) if draft_candidates else ""
    return review_path, draft_dir


def _build_batch_summary(results_by_video, selected_total, skipped_success):
    results = list(results_by_video.values())
    success_count = sum(1 for item in results if item.get("status") == "成功")
    failed_count = sum(1 for item in results if item.get("status") != "成功")
    return {
        "selected_total": selected_total,
        "skipped_success": skipped_success,
        "recorded_total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
    }


def _process_single_speech_video(speech_video, product_videos, symptom_videos, sensitivity, output_dir, draft_root, batch_tracker=None):
    result = {
        'video': speech_video['filename'],
        'project': f"OTC推广_{os.path.splitext(speech_video['filename'])[0]}",
        'status': '失败',
        'matches': 0,
        'product_matches': 0,
        'symptom_matches': 0,
        'error': '',
        'review_report': '',
        'draft_dir': '',
    }

    try:
        video_duration = float(speech_video.get('duration', 0))
        limits = {
            "ad_review": AD_FREQ_LIMIT,
            "sticker": STICKER_FREQ_LIMIT,
            "broll": BROLL_FREQ_LIMIT
        }
        tracker = UsageTracker(limits)
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

        result['matches'] = len(matches)
        result['symptom_matches'] = sum(1 for m in matches if m['material_type'] == "病症困扰")
        result['product_matches'] = sum(1 for m in matches if m['material_type'] == "产品展示")

        success = create_otc_promo_video(
            result['project'],
            speech_video['path'],
            matches,
            sfx_list=sfx_list,
            bgm_emotion=bgm_emotion,
            tracker=tracker,
            is_review_version=True
        )
        result['status'] = '成功' if success else '失败'
        if not success:
            result['error'] = 'create_otc_promo_video returned False'
        review_report, draft_dir = _find_latest_artifacts(result['project'], output_dir, draft_root)
        result['review_report'] = review_report
        result['draft_dir'] = draft_dir
        return result
    except Exception as exc:
        result['error'] = str(exc)
        return result


def batch_process_otc_videos(sensitivity='high', limit=0, resume=False, retry_failed_only=False):
    """批量处理所有口播视频"""
    configure_current_process()
    batch_concurrency = get_batch_concurrency()
    print("=" * 80)
    print("OTC药品推广视频批量智能剪辑工作流")
    print("=" * 80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"灵敏度设置: {sensitivity}\n")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"草稿目录: {get_draft_root()}\n")

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
    draft_root = get_draft_root()
    state_path = os.path.join(OUTPUT_DIR, "batch_run_state.json")
    state = _load_batch_state(state_path)
    results_by_video = {
        item.get("video"): item
        for item in state.get("results", [])
        if isinstance(item, dict) and item.get("video")
    }
    selected_videos = speech_videos[:limit] if limit > 0 else speech_videos
    skipped_success = 0

    if retry_failed_only:
        selected_videos = [
            item for item in selected_videos
            if results_by_video.get(item["filename"], {}).get("status") != "成功"
        ]
        print("   运行模式: 仅重跑未成功/失败项")
    elif resume:
        filtered_videos = []
        for item in selected_videos:
            if results_by_video.get(item["filename"], {}).get("status") == "成功":
                skipped_success += 1
                continue
            filtered_videos.append(item)
        selected_videos = filtered_videos
        print(f"   运行模式: 断点续跑，已跳过成功项 {skipped_success} 个")

    previous_meta = dict(state.get("meta") or {})
    previous_meta.update({
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": OUTPUT_DIR,
        "draft_root": draft_root,
        "sensitivity": sensitivity,
        "resume": bool(resume),
        "retry_failed_only": bool(retry_failed_only),
    })
    state["meta"] = previous_meta
    _write_batch_state(state_path, {
        "meta": state["meta"],
        "results": list(results_by_video.values()),
        "summary": _build_batch_summary(results_by_video, len(selected_videos), skipped_success),
    })

    if not selected_videos:
        print("   没有需要处理的视频，本轮直接结束。")
        print(f"   运行清单: {state_path}")
        return

    print(f"   使用真实批量并发: {batch_concurrency}")

    batch_tracker = UsageTracker({"broll": 2})
    print(f"   跨视频素材去重: 每素材每任务最多使用2次\n")

    with ThreadPoolExecutor(max_workers=batch_concurrency) as executor:
        future_map = {
            executor.submit(
                _process_single_speech_video,
                speech_video,
                product_videos,
                symptom_videos,
                sensitivity,
                OUTPUT_DIR,
                draft_root,
                batch_tracker,
            ): speech_video
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
            results_by_video[task_result['video']] = task_result

            print(f"   - 病症素材: {task_result['symptom_matches']} 处")
            print(f"   - 产品素材: {task_result['product_matches']} 处")
            print(f"   - 素材灵敏度: {sensitivity}")
            if task_result.get('draft_dir'):
                print(f"   - 草稿目录: {task_result['draft_dir']}")
            if task_result.get('review_report'):
                print(f"   - 审查报告: {task_result['review_report']}")

            if task_result['status'] == '成功':
                success_count += 1
            else:
                failed_count += 1
                if task_result['error']:
                    print(f"\n[错误] 处理失败: {task_result['error']}")

            print()
            _write_batch_state(state_path, {
                "meta": state["meta"],
                "results": list(results_by_video.values()),
                "summary": _build_batch_summary(results_by_video, len(selected_videos), skipped_success),
            })

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
    for result in results_by_video.values():
        status_icon = "[成功]" if result['status'] == '成功' else "[失败]"
        print(f"{status_icon} {result['video']:<40} -> {result['project']}")
        if result['status'] == '成功':
            print(f"      素材: {result['matches']} 处")
            if result.get('review_report'):
                print(f"      报告: {result['review_report']}")
            if result.get('draft_dir'):
                print(f"      草稿: {result['draft_dir']}")
        elif result.get('error'):
            print(f"      错误: {result['error']}")

    print("\n您可以在剪映中打开这些草稿进行人工审核和修改")
    print(f"批处理清单: {state_path}")
    print("=" * 80)
    _write_batch_state(state_path, {
        "meta": state["meta"],
        "results": list(results_by_video.values()),
        "summary": _build_batch_summary(results_by_video, len(selected_videos), skipped_success),
    })


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='OTC药品推广视频批量智能剪辑工作流')
    parser.add_argument('--sensitivity', '-s', choices=['medium', 'high'], default='high',
                        help='素材插入灵敏度: medium=中密度, high=高密度')
    parser.add_argument('--limit', '-n', type=int, default=0,
                        help='处理视频数量上限（0=处理全部）')
    parser.add_argument('--resume', action='store_true', help='跳过已经成功的视频，仅续跑剩余项')
    parser.add_argument('--retry-failed-only', action='store_true', help='只重跑历史失败项')
    args = parser.parse_args()
    batch_process_otc_videos(
        sensitivity=args.sensitivity,
        limit=args.limit,
        resume=args.resume,
        retry_failed_only=args.retry_failed_only,
    )
