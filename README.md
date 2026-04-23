# YunFengEditor

Windows 本地运行的剪映草稿自动生成工具，用于 OTC 口播视频的批量草稿生成、素材匹配、字幕生成与剪映工程输出。

## 项目简介

`YunFengEditor` 是一个面向实际交付场景的桌面工具，核心目标是：

- 读取本地口播视频、产品素材、病症素材
- 自动完成字幕识别与时间线整理
- 按语义插入产品/病症中插素材
- 生成可在剪映中继续编辑的草稿工程
- 支持 Windows 绿色版分发，适合同事直接使用

当前项目重点已经从“功能能跑”推进到“可打包、可发布、界面完整可见、同事可直接使用”。

## 主要功能

- `tkinter` 图形界面，支持本地配置保存
- 批量生成剪映草稿
- 只处理口播视频，自动跳过音频文件
- 本地 Whisper 字幕识别
- 产品/病症素材语义匹配
- 中插素材频率控制
- 任务日志与维护日志落盘
- 剪映草稿目录自动修复与回退
- 打包版 worker 环境自检
- Windows 下隐藏子进程黑框，减少频闪

## 运行环境

- Windows 10 / 11 64 位
- 剪映专业版 `5.9.x`
- 不要求额外安装 Python

## 目录说明

- `ui_main.py`: 主界面入口
- `app_launcher.py`: 打包入口与 worker 分发入口
- `otc_promo_workflow.py`: 核心草稿生成工作流
- `draft_registry.py`: 草稿目录选择与索引修复
- `app_paths.py`: 打包资源路径与运行路径解析
- `subprocess_windows.py`: Windows 子进程隐藏控制台封装
- `build_release.ps1`: 一键打包脚本
- `verify_release.py`: 发布产物校验脚本
- `jianying-editor-skill-main/`: 剪映草稿 skill 运行资源

## 本地开发

安装依赖：

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

启动界面：

```powershell
python ui_main.py
```

## 打包发布

执行：

```powershell
.\build_release.ps1
```

默认输出：

- `dist/YunFengEditor/`
- `dist/YunFengEditor-portable-win64.zip`

## 发布包使用

1. 解压 `YunFengEditor-portable-win64.zip`
2. 运行 `YunFengEditor.exe`
3. 填写 `口播素材路径`、`产品素材路径`、`病症素材路径`
4. 按需填写 `API Key`
5. 点击 `批量生成全自动草稿`

详细说明见：

- `发布使用说明.md`
- `RELEASE_GUIDE.md`

## 当前状态

- 已支持打包版自检
- 已修复底部按钮不可点击问题
- 已修复同事环境中批量提交按钮被误禁用问题
- 已收敛 Windows 下 `ffmpeg/ffprobe/worker` 触发的黑框频闪
- 已清理大部分无用测试、示例和开发残留文件

## 版本

当前仓库已建立 Git 版本管理，并准备从 `v1.0.0` 开始做正式版本标记。
