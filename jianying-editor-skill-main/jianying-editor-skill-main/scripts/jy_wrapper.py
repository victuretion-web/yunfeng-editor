"""
JianYing Editor Skill - High Level Wrapper (Mixin Based)
旨在解决路径依赖、API 复杂度及严格校验问题。
"""

import os
import sys
import uuid
from typing import Union, Optional

# 环境初始化
from utils.env_setup import setup_env
setup_env()

# 导入工具函数
from utils.constants import SYNONYMS
from utils.formatters import (
    resolve_enum_with_synonyms, format_srt_time, safe_tim, 
    get_duration_ffprobe_cached, get_default_drafts_root, get_all_drafts
)

# 导入基类与 Mixins
from core.project_base import JyProjectBase
from core.media_ops import MediaOpsMixin
from core.text_ops import TextOpsMixin
from core.vfx_ops import VfxOpsMixin
from core.mocking_ops import MockingOpsMixin

try:
    import pyJianYingDraft as draft
    from pyJianYingDraft import VideoSceneEffectType, TransitionType
except ImportError:
    draft = None

class JyProject(JyProjectBase, MediaOpsMixin, TextOpsMixin, VfxOpsMixin, MockingOpsMixin):
    """
    高层封装工程类。通过多重继承 Mixins 实现功能解耦。
    """
    def _resolve_enum(self, enum_cls, name: str):
        return resolve_enum_with_synonyms(enum_cls, name, SYNONYMS)

    def add_clip(self, media_path: str, source_start: Union[str, int], duration: Union[str, int], 
                 target_start: Union[str, int] = None, track_name: str = "VideoTrack", **kwargs):
        """高层剪辑接口：从媒体指定位置裁剪指定长度，并放入轨道。"""
        if target_start is None:
            target_start = self.get_track_duration(track_name)
        return self.add_media_safe(media_path, target_start, duration, track_name, source_start=source_start, **kwargs)

    def save(self):
        """保存并执行质检报告。"""
        import json
        import time
        import uuid
        
        draft_path = os.path.join(self.root, self.name)
        draft_content_path = os.path.join(draft_path, "draft_content.json")
        draft_meta_path = os.path.join(draft_path, "draft_meta_info.json")
        
        # 1. 动态生成唯一的 UUID，解决多草稿 ID 碰撞导致剪映强制删除草稿的问题
        new_draft_id = str(uuid.uuid4()).upper()
        
        # 在执行 script.save() 之前，修改 script.content 的 ID
        if hasattr(self.script, "content") and isinstance(self.script.content, dict):
            self.script.content["id"] = new_draft_id
            
        self.script.save()
        self._patch_cloud_material_ids()
        self._force_activate_adjustments()
        
        if os.path.exists(draft_path):
            os.utime(draft_path, None)
            
        # 2. 更新 draft_meta_info.json 中的 ID, 时间, 路径等元数据
        try:
            if os.path.exists(draft_meta_path):
                with open(draft_meta_path, 'r', encoding='utf-8') as f:
                    draft_meta = json.load(f)
                    
                draft_meta["id"] = new_draft_id
                draft_meta["draft_id"] = new_draft_id
                draft_meta["draft_name"] = self.name
                draft_meta["draft_fold_path"] = draft_path.replace("\\", "/")
                draft_meta["draft_root_path"] = self.root.replace("\\", "/")
                
                # 更新 tm_duration 防止被清理脚本误删 (注意：单位必须是微秒！)
                if hasattr(self.script, "duration"):
                    draft_meta["tm_duration"] = int(self.script.duration)
                    if draft_meta["tm_duration"] < 1000000 and draft_meta["tm_duration"] > 0:
                        draft_meta["tm_duration"] *= 1000000
                
                # 更新修改时间
                current_timestamp = int(time.time() * 1000000)
                draft_meta["tm_draft_cloud_modified"] = current_timestamp
                draft_meta["tm_draft_modified"] = current_timestamp
                
                with open(draft_meta_path, 'w', encoding='utf-8') as f:
                    json.dump(draft_meta, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to update draft_meta_info.json: {e}")
            
        # 3. [修复] JianYing Pro v5.9+ 需要将草稿注册到 root_meta_info.json 才能在首页显示
        root_meta_path = os.path.join(self.root, "root_meta_info.json")
        try:
            if os.path.exists(root_meta_path):
                with open(root_meta_path, 'r', encoding='utf-8') as f:
                    root_meta = json.load(f)
            else:
                root_meta = {
                    "all_draft_store": [],
                    "draft_ids": 1,
                    "root_path": self.root.replace("\\", "/")
                }
            
            all_drafts = root_meta.get("all_draft_store", [])
            
            # 检查是否已存在
            draft_json_file = draft_content_path.replace("\\", "/")
            exists = False
            for d in all_drafts:
                if d.get("draft_json_file") == draft_json_file or d.get("draft_fold_path") == draft_path.replace("\\", "/"):
                    # 如果存在但 ID 旧了，更新一下
                    d["draft_id"] = new_draft_id
                    exists = True
                    break
                    
            if not exists:
                all_drafts.append({
                    "draft_fold_path": draft_path.replace("\\", "/"),
                    "draft_id": new_draft_id,
                    "draft_json_file": draft_json_file
                })
            
            root_meta["all_draft_store"] = all_drafts
            with open(root_meta_path, 'w', encoding='utf-8') as f:
                json.dump(root_meta, f, ensure_ascii=False, indent=4)
            print(f"✅ Registered draft {new_draft_id} in root_meta_info.json.")
        except Exception as e:
            print(f"⚠️ Failed to update root_meta_info.json: {e}")

        print(f"✅ Project '{self.name}' saved and patched.")
        return {"status": "SUCCESS", "draft_path": draft_path}

# 导出工具函数以便向下兼容
__all__ = ["JyProject", "get_default_drafts_root", "get_all_drafts", "safe_tim", "format_srt_time"]

if __name__ == "__main__":
    # 测试代码
    try:
        project = JyProject("Refactor_Test_Project", overwrite=True)
        print("🚀 Refactored JyProject initialized successfully.")
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
