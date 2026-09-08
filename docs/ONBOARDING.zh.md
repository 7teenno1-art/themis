# 首次运行 Themiz

<p align="center"><img src="assets/pantheon/workflow/01-intake.png" alt="Themiz 收案流程插图，并非应用截图" width="100%"></p>

这条路径使用 macOS、Python 3.11 或更新版本，以及 Xcode 命令行工具。Apple Vision 只能在 Mac 上识别扫描件。Windows 和 Linux 上的完整安装尚未验证。你所选智能体的访问权限和订阅额度需要另外配置。

如果你更喜欢对话，可以在 Codex CLI 或 Claude Code 中打开[分步引导](ONBOARDING-CHAT.zh.md)。两个客户端是平等的入口。安装前，先让智能体说明磁盘上会发生什么变化。

## 第 1 步：检查运行环境

```bash
sw_vers -productVersion
python3 --version
xcode-select -p
```

第一条命令显示已安装的 macOS 版本，第二条应显示 Python 3.11 或更新版本，第三条应显示开发者工具路径。音频转写还需要 ffmpeg 和本地 Whisper 模型。缺少组件时，安装程序会在最终报告中明确列出。检查失败或超时不等于通过。

## 第 2 步：获取项目

```bash
git clone https://github.com/zarubinvibe/themiz.git
cd themiz
```

第一条命令会创建 `themiz` 文件夹，第二条命令会让终端进入该文件夹。没有 Git 时，可以下载 [ZIP](https://github.com/zarubinvibe/themiz/archive/refs/heads/main.zip)，解压后在终端中打开该目录。

## 第 3 步：安装并启用环境

```bash
bash install.sh
. .venv/bin/activate
```

安装程序会把 Python 包放进 `.venv`，并准备字体、本地识别工具和工作目录。完成后会打印一份报告。运行第二条命令后，终端提示符通常会以 `(.venv)` 开头。

## 第 4 步：选择客户端

Codex CLI：

```bash
codex
```

让它使用 `themiz-setup` 技能配置 Themiz。

Claude Code：

```bash
claude
```

然后运行 `/themiz-setup`。无论使用哪个客户端，智能体都应先读取项目规则、了解你的执业情况，并在处理当事人材料前说明所需权限。两个入口配置的是同一个项目。

## 第 5 步：用安全样例检查边界

先使用不含敏感数据的文件副本。文本提取和 OCR 在本地运行，发送给智能体的片段由你选择的模型供应商处理。使用真实案卷前，让智能体用直白的话复述这条边界。材料接收后，不要修改 `00_intake/` 中的原始文件。

公开克隆不包含填充好的法律语料库。允许使用的来源和所需文件需要另外配置。下载失败或版本未经确认，仍属于待解决的问题。磁盘上有文本，不代表法律条文就是现行版本。

## 第 6 步：查看案件状态

先让智能体创建案件。将 `cases/client/case` 替换为现有案件文件夹的实际路径。这条命令只汇总该具体案件，不是通用运行环境概览。

```bash
python3 scripts/themiz_status.py cases/client/case --brief
```

对照原件检查案件地图，金额、案号和引文都需要核实。范围小的任务使用 FAST，复杂任务使用 FULL，并安排独立读者和审阅。草稿由没有参与撰写的角色检查。是否签署和提交，由你决定。

## 第 7 步：按需打开浏览器面板

```bash
.venv/bin/python cockpit/app.py
```

打开 `http://127.0.0.1:8800`，你会看到本地状态面板。这个面板并非必需，其智能体按钮目前只会调用 Claude Code。通过 Codex 执行任务时，请使用 Codex CLI。

## 第 8 步：更新现有 Git 克隆

在 Codex 中，让它使用 `themiz-update` 技能更新 Themiz。在 Claude Code 中运行 `/themiz-update`。这种更新需要 Git 克隆。若通过 ZIP 安装，请新建 Git 克隆；核对后让智能体迁移本地数据，且不要覆盖原始文件。先检查拟议变更和工作目录状态。更新程序与核对法律版本仍是两项不同的操作。

## 反馈与贡献

有用的话，可以[点亮星标](https://github.com/zarubinvibe/themiz)。发现问题时，请[提交 issue](https://github.com/zarubinvibe/themiz/issues)，附上命令、错误信息和虚构样例。不要公开当事人的材料。

如果你想提交修复：先 fork 仓库，创建分支 branch，提交 commit，推送 push，然后开一个 Pull Request。不要直接向 `main` 推送变更。
