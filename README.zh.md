# Themiz

为俄罗斯诉讼准备的一套工作系统：原始材料、案件地图、可核对的引用、文书草稿和独立复核。

[English](README.md) · [Русский](README.ru.md)

[![License](https://img.shields.io/badge/license-community%201.0-blue.svg)](LICENSE) [![Stars](https://img.shields.io/github/stars/zarubinvibe/themiz?style=flat&color=C9A87A)](https://github.com/zarubinvibe/themiz/stargazers) [![Status](https://img.shields.io/badge/status-in%20development-brightgreen.svg)](https://github.com/zarubinvibe/themiz) [![Olympuz](https://img.shields.io/badge/olympuz-family-B8D6EA.svg)](https://github.com/zarubinvibe/athena#olympuz-family)

<p align="center"><img src="docs/assets/pantheon/hero.png" alt="Pantheon 插画：白色大理石的 Themiz 站在古典石柱旁，身边是案卷和复核卡片" width="100%"></p>

<!-- owner-welcome:start -->

> 你好，我是 Fil。
>
> 我做 Themiz，是因为夜晚总被案件的机械活吃掉：扫描件、日期、引文，还有对同一目录的又一次翻查。我想要一个工作空间，记得每个事实从哪里来，也会在检查没有完成时直说。
>
> 先用一份不含敏感信息的文件副本试运行。遇到故障，请开 issue，附上你执行的命令和看到的结果。如果 Themiz 最终留在你的工作中，给仓库点亮星标，也可以看看其他 Olympuz 项目：https://zarubinvibe.com
>
> — Filipp Zarubin

<!-- owner-welcome:end -->

## 目录

- [这是什么](#这是什么)
- [它解决什么问题](#它解决什么问题)
- [最大的优势](#最大的优势)
- [工作流程](#工作流程)
- [快速开始](#快速开始)
- [简单对比](#简单对比)
- [简单词汇](#简单词汇)
- [安全与隐私](#安全与隐私)
- [局限](#局限)
- [点亮星标与参与](#点亮星标与参与)

<!-- beginner-readme:start -->

## 这是什么

Themiz 面向办理俄罗斯法诉讼的律师。每个案件都有独立目录：系统读取本地文件、整理事实、检索获准来源，并让草稿经过另一个角色复核。你可以用 Codex CLI，也可以用 Claude Code；两条路径地位相同。

公开仓库提供引擎、智能体、测试和安全模板，不附带现成的法律语料，也没有任何人的案件材料。处理当事人资料前，由你选择来源、权限和模型服务商。

## 它解决什么问题

案件工作常被细小却昂贵的机械活拖住：再读一份扫描件、核对日期、找到引文出处、确认草稿仍与证据一致。Themiz 把这些动作留在同一个案件里，并写清哪些检查通过、哪里失败、还有什么必须由律师处理。

它针对俄罗斯诉讼设计。文书契约、法定计算、法院术语和来源路线遵循俄罗斯程序法，不是覆盖所有法域的通用法律系统。

## 最大的优势

**最大的优势：** 每个事实和引文都能追到文件、来源和复核状态。

**为什么这样更好：** 代码处理已支持的计算，包括法定利息、诉讼期限、国家规费和金额大写。引用工具从你硬盘上已有的语料取法条，核对校验和；来源时效未证实或需要复核时，它会警告。模型的转述不会被当成已核验的条文。

## 工作流程

下面七步表示工作顺序，不等于每个案件都要启动七组智能体。问题窄、材料不超过六份且已有文本或 OCR 时，可以走 FAST；复杂争议、阅读存在分歧或扫描件很多时，走 FULL，并启用分工阅读与交叉核对。下方 Pantheon 场景只是流程插画，不是界面截图。

<!-- workflow-diagram:start -->

<p align="center"><img src="docs/assets/pantheon/takt-zh.png" alt="Themiz 工作流程插图：Pantheon 大理石场景中从接收到开庭的七个带标注阶段，并非应用截图" width="100%"></p>

<!-- workflow-diagram:end -->

| 阶段 | 会发生什么 |
|---|---|
| 1. 接收 | 一个本地目录保存案件原始材料 |
| 2. 读取 | 本地路由读取文本、扫描件、表格和音频 |
| 3. 地图 | 案件地图连接事实、诉求、证据和冲突 |
| 4. 判例 | 检索同时记录支持材料、程序方案和不利依据 |
| 5. 合议 | 评议会审查立场；L3 必须进行评议 |
| 6. 文书 | 一个角色起草，另一个角色复核同一版本 |
| 7. 开庭 | 庭审材料包把清单、论点和期限放在一起 |

### 第 1 步：把案件材料交过来

把选定文件复制到案件的 intake 目录。工作流把它们视为原始材料，生成的笔记另行保存。仓库发布规则排除当事人目录，但哪些文字交给模型服务商仍由你决定。

<p align="center"><img src="docs/assets/pantheon/workflow/01-intake.png" alt="Pantheon 工作流插画，并非界面截图：第 1 步，接收案件材料" width="100%"></p>

**你会得到：** 一个范围清楚的案件工作区，原始材料与生成内容分开保存。

### 第 2 步：扫描件在你的 Mac 上被读出来

文本 PDF、DOCX、PPTX 和 XLSX 由本地提取器处理。macOS 上的扫描件与图片交给 Apple Vision；本地运行环境和模型可用时，Whisper 可以转写音频。路由器按内容哈希保存文本和提取要素。案号、金额和关键细节仍要与原件核对。

<p align="center"><img src="docs/assets/pantheon/workflow/02-extract.png" alt="Pantheon 工作流插画，并非界面截图：第 2 步，本地文档提取" width="100%"></p>

**你会得到：** 可检索的本地文本、明确的处理路线，以及仍需对照原件的提取要素。

### 第 3 步：搭起案件地图

FAST 由一个制图角色读取不超过六份文本或已识别文件。材料很多、扫描件复杂或阅读有争议时，FULL 按格式分给多个阅读角色，再核对各自报告。缺失和冲突会保留下来，不会被自动补写。

<p align="center"><img src="docs/assets/pantheon/workflow/03-case-map.png" alt="Pantheon 工作流插画，并非界面截图：第 3 步，案件地图" width="100%"></p>

**你会得到：** 一份带来源、未解决冲突和清楚阅读边界的案件地图。

### 第 4 步：正反两面的判例

检索先检查本地材料和渠道可用性。FAST 通常只启用一个程序策略角色；FULL 可以分成支持、质疑和程序三个方向。外部查询只走获准来源并去除身份信息；来源不可用时，结果会明确写出。

<p align="center"><img src="docs/assets/pantheon/workflow/04-research.png" alt="Pantheon 工作流插画，并非界面截图：第 4 步，正反两面的法律检索" width="100%"></p>

**你会得到：** 一份带来源的研究记录，其中包括不利材料和已指出的缺口。

### 第 5 步：五位法学家争论

对于 L2 和 L3 事项，多个法律角色从不同角度审查立场，并记录分歧。L3 必须进行评议。L2 FAST 只有在律师已记录自己的立场时才可跳过评议。评议会整理论证，但不会凭空增加独立服务商，也不能代替来源核验。

<p align="center"><img src="docs/assets/pantheon/workflow/05-council.png" alt="Pantheon 工作流插画，并非界面截图：第 5 步，法律评议会" width="100%"></p>

**你会得到：** 一份把假设、反对意见和薄弱处写在纸面上的立场。

### 第 6 步：一个写，另一个查

起草角色依照案件契约和已记录来源写作。独立复核角色检查事实、引文、立场、格式和完整性，再写入受控结论。证据、复核、格式或个人数据检查失败时，机械闸门可以阻止生成成品，但不会认证法律结论。

<p align="center"><img src="docs/assets/pantheon/workflow/06-draft.png" alt="Pantheon 工作流插画，并非界面截图：第 6 步，起草与独立复核" width="100%"></p>

**你会得到：** 一份经过复核的草稿，以及留给律师处理的明确问题清单。

### 第 7 步：开庭准备与提醒

准备角色依据案件地图和已有立场生成庭审清单。状态工具显示缺失步骤，并在支持的场景中给出期限及所用规则。Telegram 提醒是可选项。你要检查材料、决定提交什么、亲自签署并出庭。

<p align="center"><img src="docs/assets/pantheon/workflow/07-hearing.png" alt="Pantheon 工作流插画，并非界面截图：第 7 步，庭审准备" width="100%"></p>

**你会得到：** 一份准备材料包和清楚的待办事项，不是自动提交决定。

## 快速开始

已验证的 macOS 路径需要 Python 3.11 或更高版本以及 Xcode Command Line Tools。Apple Vision 扫描识别只在 macOS 上运行。Codex CLI 和 Claude Code 是并列选项，请使用你已配置的那一个。

```bash
git clone https://github.com/zarubinvibe/themiz.git
cd themiz
bash install.sh
. .venv/bin/activate
```

公共代码块只负责安装项目并激活 Python 环境。接着从两个智能体客户端中选一个，不要把两条命令当作连续安装步骤。

**选项 A：Codex CLI**

```bash
codex
```

让 Codex 按你的执业方式配置 Themiz。

**选项 B：Claude Code**

```bash
claude
```

在 Claude Code 中运行 `/themiz-setup`。

用编辑器打开目录：

```bash
code .
```

先让智能体创建案件。将 `cases/client/case` 替换为现有案件文件夹的实际路径。这条命令只汇总该具体案件，不是通用运行环境概览：

```bash
python3 scripts/themiz_status.py cases/client/case --brief
```

可选的浏览器面板：

```bash
.venv/bin/python cockpit/app.py
```

面板会在 `http://127.0.0.1:8800` 本地打开。它的智能体操作目前调用 Claude Code；使用 Codex 时，请从 Codex CLI 或编辑器启动。没有 Git？下载 [ZIP](https://github.com/zarubinvibe/themiz/archive/refs/heads/main.zip)，安装步骤相同。

第一次做这件事？[上手引导](docs/ONBOARDING.zh.md) 会一步一步带你走完第一次运行，并写清楚每条命令之后你会看到什么。

**你会得到：** 一套已安装的工作目录，内含两个 CLI 的配置、安全起始模板和状态命令。打开真实案件前，请配置模型访问权限和获准法律来源。

## 简单对比

| 产品 | 用途 | 法律来源 | 案件材料 | 起草 | 复核 |
|---|---|---|---|---|---|
| **Themiz** | 俄罗斯诉讼案件工作流 | 本地语料和获准外部来源，并显示来源状态 | 本地提取；由你决定模型服务商收到什么 | 按案件契约起草诉讼文书 | 独立复核角色和机械闸门；是否提交由律师决定 |
| 手工处理 | 律师直接使用案件目录 | 律师自行核对第一手来源 | 律师阅读原件 | 律师自行写作 | 控制和责任都在律师 |
| [ConsultantPlus](https://www.consultant.ru/about/software/cons/) | 俄罗斯法律信息系统 | 法律、判例和评论资料 | 系统提供的文档；任意案件上传未获本次核实 | 合同和本地文档工具取决于所购组件 | 系统提示风险，选择由用户作出 |
| [Garant](https://udalenka.garant.ru/) | 法律信息与支持 | 法律数据库、百科和专家材料 | 文档与审批功能取决于所接服务 | 本次核对的产品页面未确认 | 工作组织和专家帮助，决定由用户作出 |
| [ChatGPT](https://help.openai.com/en/articles/9260256) | 通用 AI 助手 | 搜索和深度研究取决于设置与套餐；此处未声称有俄罗斯法专用语料 | 支持上传 PDF、演示文稿和文本文件 | 起草、改写和摘要 | 未声称提供法律验收，结果由用户复核 |
| [Claude](https://claude.com/product/overview) | 通用 AI 助手 | 网页搜索和连接功能取决于配置；此处未声称有俄罗斯法专用语料 | 处理 PDF、Word、Excel 和图片 | 起草、编辑和润色 | 可以评估草稿，但不作法律认证；控制权仍在用户 |

产品名称归各自所有者。此表只说明适用范围，不作排名。功能和访问权限可能因服务、套餐、地区和配置而异；当前信息以链接中的产品页面为准。

## 简单词汇

| 词 | 简单解释 |
|---|---|
| Repository | 仓库：Git 保存并记录版本的项目文件夹 |
| Terminal | 终端：你输入命令的窗口 |
| Command | 命令：给电脑的一条指令 |
| Branch | 分支：不影响 `main` 的另一条修改线 |
| Pull Request | 合并请求：请别人审阅并接受你的修改 |
| 案件地图 | 把当事人、日期、诉求、证据和未解决冲突连在一起的文件 |
| 智能体 | 只承担一项有限任务的助手角色，例如阅读文档或复核草稿 |
| LKG | 最近一次可用的本地副本；来源更新失败时仍可读取 |

## 安全与隐私

- 文件、OCR、提取缓存和案件目录保存在你的电脑上。你交给智能体的文字会按所选模型服务商的条款处理；使用当事人资料前，请先划定这条边界。
- 公开版本排除案件目录和私人法律语料。个人数据守卫会在提交中检查已知模式，降低误发布风险，但不能证明任何形式的泄露都绝不可能。
- 外部判例检索只走获准渠道，发送去标识化查询，不发送案卷。难读页面转交云端检查必须明确触发，本地 OCR 不会静默切换到云端。
- Telegram 是可选功能，配置你自己的机器人前保持关闭。启用后，获准的提醒数据会离开电脑并发送给 Telegram。
- 机械检查覆盖已支持的格式、校验和、计算和工作流状态，不为法律、证据或最终诉讼决定作认证。

在共用电脑上处理资料，或把当事人文字交给模型前，请阅读 [SECURITY.zh.md](SECURITY.zh.md)，并为你的执业设定权限。

## 局限

Themiz 仍在开发，由律师掌握最终控制。Codex CLI 与 Claude Code 是平等的启动路径。技术契约和 macOS 全新安装已有测试；完整真实案件流程以及 Windows、Linux 全新安装尚未认证。

- Apple Vision 只在 macOS 上识别扫描件和图片。文本格式由 Python 工具处理，但 Windows 和 Linux 的完整安装尚未验证。
- DOCX、文本 PDF、PPTX 和 XLSX 已在全新安装后分别通过转换检查。旧式 XLS 已配置 reader，但没有接受同样的合成转换测试。
- 判例检索和语料更新依赖外部来源。来源不可用时就保持不可用，不会用模型猜测补位。
- 公开克隆包含引擎和模板，不含已经填充的法律语料。本地数据就绪后，Themiz 可以建立独立法律图谱，保留来源和时效标记。仓库附带的 Graphify 图描述代码，而不是你的法律语料。
- 后台下载器独立于模型订阅额度检查获准公共来源；刷新失败时保留最近可用副本。每周维护只用 Codex 的实际重置事件安排自己的周期，不设置美元上限，也不管理所有服务商的套餐。
- 本地 OCR、提取、计算和后台下载不需要单独付费调用模型 API。智能体分析仍使用你选择的模型服务商账号或订阅。
- 独立复核和绿色工作流状态不会自动把文书变成可提交成品。你要核对事实与法律，决定是否提交，亲自签署，并对案件负责。

详见[工作流说明](docs/HOW-IT-WORKS.ru.md)和[智能体参考](docs/DETAILS.md)。工作流说明目前为俄语。

## 点亮星标与参与

觉得有用？给 Themiz 点亮星标：[https://github.com/zarubinvibe/themiz](https://github.com/zarubinvibe/themiz)。这只要一秒，却决定别人能不能找到这个项目。

想改点什么？流程很短：先 fork 仓库，建一个分支 branch，提交 commit，推送 push，然后开一个 Pull Request。请不要直接向 `main` 推送变更。

发现问题？到 [https://github.com/zarubinvibe/themiz/issues](https://github.com/zarubinvibe/themiz/issues) 开一个 issue，写清楚你运行了什么、发生了什么。

<!-- beginner-readme:end -->

<!-- pantheon-family:start -->
## Olympuz 家族

这是 [Olympuz 家族](https://github.com/zarubinvibe/athena#olympuz-family) 的公开项目之一。表格里的每一行都可以打开仓库，或者直接下载源码压缩包。

| 类型 | 名称 | 做什么 | 如何帮到这个项目 | 获取 |
|---|---|---|---|---|
| 项目 | Athena | 可携带的智能体操作系统：在新的 Mac 上重建 Claude 与 Codex 的工作环境。 | 一次运行就在新机器上铺好智能体的工作环境：规则、技能、钩子。 | [仓库](https://github.com/zarubinvibe/athena) · [ZIP](https://github.com/zarubinvibe/athena/archive/refs/heads/main.zip) |
| 项目 | Helioz | 全天候的智能体工作传送带，带可验证的完成标记和按目标做出的夜间决策。 | 让智能体的工作全天候运转，每个任务都以可验证的标记收尾。 | [仓库](https://github.com/zarubinvibe/helioz) · [ZIP](https://github.com/zarubinvibe/helioz/archive/refs/heads/main.zip) |
| 项目 | Mnemazine | 本地优先的记忆系统：把原始材料变成可复用的、已核验的知识。 | 把截图、PDF、链接这些素材，变成经过核实的知识笔记。 | [仓库](https://github.com/zarubinvibe/mnemazine) · [ZIP](https://github.com/zarubinvibe/mnemazine/archive/refs/heads/main.zip) |
| 项目 | Themiz | 面向俄罗斯诉讼的多智能体助手，本地识别扫描件，五位法学家组成合议审阅。 | 以多智能体处理俄罗斯法院案件，材料保存在本地。 | [仓库](https://github.com/zarubinvibe/themiz) · [ZIP](https://github.com/zarubinvibe/themiz/archive/refs/heads/main.zip) |
| 项目 | Zeuz | 工作流工厂：把一个想法变成带规则、闸门、可观测性和回放的多智能体系统。 | 搭建带规则、闸门、可观测性和重放的多智能体工作流。 | [仓库](https://github.com/zarubinvibe/zeuz) · [ZIP](https://github.com/zarubinvibe/zeuz/archive/refs/heads/main.zip) |
| 项目 | Lynceuz | 以零成本收集公开网页证据；安全路径走完时，它会给出诚实的理由并停下。 | 零成本从公开网络收集证据，走到边界就诚实地停下。 | [仓库](https://github.com/zarubinvibe/lynceuz) · [ZIP](https://github.com/zarubinvibe/lynceuz/archive/refs/heads/main.zip) |
| 项目 | Iriz | macOS 菜单栏听写：语音在你自己的 Mac 上解码，键盘布局自动纠正，口述可以直接变成给智能体的任务。 | macOS 菜单栏听写：语音在你自己的 Mac 上解码，键盘布局自动纠正。 | [仓库](https://github.com/zarubinvibe/iriz) · [ZIP](https://github.com/zarubinvibe/iriz/archive/refs/heads/main.zip) |
| 项目 | Mantoz | 把一个想法摆到五百个并不存在的人面前，然后告诉你每个群体是怎么答的。 | 在真人看到之前，先把想法摆在几百个生成出来的人面前。 | [仓库](https://github.com/zarubinvibe/mantoz) · [ZIP](https://github.com/zarubinvibe/mantoz/archive/refs/heads/main.zip) |
| 项目 | Koiz | 所有项目共用一份教训库。每次失败都追到原因，原因不被钩子、闸门或测试关掉，就一直挂在那里。 | 为所有项目保存同一份教训库，并且要求机制，而不是承诺。 | [仓库](https://github.com/zarubinvibe/koiz) · [ZIP](https://github.com/zarubinvibe/koiz/archive/refs/heads/main.zip) |
<!-- pantheon-family:end -->

## 许可证

Themiz Community Licence 1.0 允许个人律师免费使用，包括个人执业；组织需要商业许可。模型订阅和第三方服务另行计算。见 [LICENSE](LICENSE) 与 [LICENSE.ru.md](LICENSE.ru.md)。
