import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.join(current_dir, "jianying-editor-skill-main", "jianying-editor-skill-main")

if not os.path.exists(os.path.join(skill_root, "scripts", "jy_wrapper.py")):
    raise ImportError("Could not find jianying-editor skill root.")
sys.path.insert(0, os.path.join(skill_root, "scripts"))
sys.path.insert(0, os.path.join(skill_root, "scripts", "utils"))
from jy_wrapper import JyProject, draft

if __name__ == "__main__":
    project = JyProject("体癣视频剪辑示例", overwrite=True, width=1080, height=1920)

    video_dir = "H:\\体癣"

    product_video = os.path.join(video_dir, "产品", "10.mp4")
    if os.path.exists(product_video):
        project.add_media_safe(product_video, "0s")

    speech_video = os.path.join(video_dir, "口播", "4月9日.mp4")
    if os.path.exists(speech_video):
        project.add_media_safe(speech_video, "5s")

    symptom_video = os.path.join(video_dir, "病症", "C0008.mp4")
    if os.path.exists(symptom_video):
        seg = project.add_media_safe(symptom_video, "10s")
        if seg and hasattr(seg, 'volume'):
            seg.volume = 0.0

    project.add_text_simple("体癣产品介绍", start_time="0s", duration="3s", anim_in="复古打字机")
    project.add_text_simple("产品展示", start_time="5s", duration="3s", anim_in="淡入")
    project.add_text_simple("病症表现", start_time="10s", duration="3s", anim_in="淡入")

    project.save()

    print(f"草稿已创建: 体癣视频剪辑示例")
    print("请在剪映中打开草稿进行审核和手动导出")
