import cv2
import os
import numpy as np

# 视频素材路径
video_dir = "H:\\体癣"

# 输出视频路径
output_path = "output_video.mp4"

# 选择要剪辑的视频
product_video = os.path.join(video_dir, "产品", "10.mp4")
speech_video = os.path.join(video_dir, "口播", "4月9日.mp4")
symptom_video = os.path.join(video_dir, "病症", "C0008.mp4")

# 检查视频文件是否存在
if not os.path.exists(product_video):
    print(f"产品视频不存在: {product_video}")
    exit(1)
if not os.path.exists(speech_video):
    print(f"口播视频不存在: {speech_video}")
    exit(1)
if not os.path.exists(symptom_video):
    print(f"病症视频不存在: {symptom_video}")
    exit(1)

# 读取视频
cap1 = cv2.VideoCapture(product_video)
cap2 = cv2.VideoCapture(speech_video)
cap3 = cv2.VideoCapture(symptom_video)

# 获取视频参数
fps = int(cap1.get(cv2.CAP_PROP_FPS))
width = int(cap1.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap1.get(cv2.CAP_PROP_FRAME_HEIGHT))

# 创建输出视频写入器
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

# 读取并写入第一部分视频 (产品视频，取前10秒)
frames1 = int(fps * 10)
for i in range(frames1):
    ret, frame = cap1.read()
    if not ret:
        break
    # 添加标题
    if i < fps * 3:
        cv2.putText(frame, "体癣产品介绍", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    out.write(frame)

# 读取并写入第二部分视频 (口播视频，取前10秒)
frames2 = int(fps * 10)
for i in range(frames2):
    ret, frame = cap2.read()
    if not ret:
        break
    # 添加标题
    if i < fps * 3:
        cv2.putText(frame, "产品展示", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    # 调整尺寸以匹配输出视频
    frame = cv2.resize(frame, (width, height))
    out.write(frame)

# 读取并写入第三部分视频 (病症视频，取前10秒)
frames3 = int(fps * 10)
for i in range(frames3):
    ret, frame = cap3.read()
    if not ret:
        break
    # 添加标题
    if i < fps * 3:
        cv2.putText(frame, "病症表现", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    # 调整尺寸以匹配输出视频
    frame = cv2.resize(frame, (width, height))
    out.write(frame)

# 释放资源
cap1.release()
cap2.release()
cap3.release()
out.release()

print(f"视频剪辑完成，输出路径: {output_path}")
