# Three Kingdoms Multi-Agent Werewolf

一个基于 AgentScope 的三国狼人杀多智能体项目。  
项目把每个玩家实现为一个独立的 LLM Agent，用 Prompt 注入角色身份、目标和人物性格；同时用 Python 规则引擎负责游戏流程、技能结算和胜负判断；最后通过一个日志回放 Web UI 把整局对局可视化。

这个项目适合用于展示以下能力：

- 多智能体应用编排
- Prompt Engineering
- 结构化输出约束
- 确定性规则引擎设计
- 日志驱动的可视化回放
- 大模型应用 Demo 产品化

## Project Highlights

- 每个角色都是独立 Agent，具备不同身份与人设。
- 讨论、投票、查验、用药等关键环节都使用结构化输出，避免自由文本难以解析。
- 游戏状态和胜负逻辑完全由 Python 代码接管，保证流程可控。
- 使用日志回放 UI 复现整局对局，便于调试与展示。
- 项目结构清晰，适合作为大模型应用开发岗位的作品集项目。

## Tech Stack

- Python 3
- AgentScope
- DashScope / Qwen
- Pydantic
- asyncio
- Python stdlib HTTP server
- HTML / CSS / JavaScript

## Repository Structure

```text
.
├── assets/                  # GIF、截图等演示资源
├── main_cn.py               # 游戏主入口与多智能体编排
├── game_roles.py            # 角色定义、阵营配置、人物性格
├── prompt_cn.py             # 中文 Prompt 模板
├── structured_output_cn.py  # Pydantic 结构化输出模型
├── utils_cn.py              # 规则、胜负判断、主持人、工具函数
├── web_ui.py                # 日志回放 Web UI
├── test_env.py              # 本地环境变量检查脚本
├── requirements.txt         # 项目依赖
├── game_log.txt             # 示例日志
└── .env                     # 本地密钥配置，不应提交到 GitHub
```

## Architecture

项目可以分成 4 层：

### 1. Agent Layer

`main_cn.py` 中的 `ThreeKingdomsWerewolfGame` 会为每位玩家创建一个 `ReActAgent`。

- Agent 身份来自角色配置
- Agent 个性来自三国人物设定
- Agent 的行为边界来自系统 Prompt

核心点：

- 每个 Agent 都有自己的上下文和视角
- 每个 Agent 都会基于自己的身份做推理和决策

### 2. Structured Output Layer

`structured_output_cn.py` 中定义了讨论、投票、查验、女巫行动、猎人开枪等结构化模型。

作用：

- 约束模型输出字段
- 限制无效目标
- 让 Python 程序能稳定消费模型结果

这是整个项目里最关键的可靠性设计之一。

### 3. Rule Engine Layer

`utils_cn.py` 和 `game_roles.py` 管理确定性逻辑：

- 角色分配
- 胜负判断
- 多数票统计
- 回合上限
- 主持人播报
- 阵营与技能规则

这部分不依赖模型，避免把确定性逻辑交给 LLM。

### 4. Replay UI Layer

`web_ui.py` 会读取游戏日志并解析为事件流，再通过一个轻量 Web 页面进行回放。

支持的效果包括：

- 围桌式玩家站位
- 玩家发言气泡跟随
- 主持人/旁白固定气泡
- 攻击/查验/救人/毒杀动作连线
- 白天/夜晚场景切换
- 表情、睁眼/闭眼、死亡状态展示

## Game Flow

主流程在 `main_cn.py` 的 `run_game()` 中：

1. 初始化游戏并创建全部 Agent
2. 夜晚阶段
3. 狼人讨论并击杀目标
4. 预言家查验
5. 女巫决定是否救人/毒人
6. 结算夜晚死亡
7. 检查胜负
8. 白天讨论
9. 全员投票放逐
10. 猎人死亡时触发技能
11. 再次检查胜负
12. 进入下一轮

这是一个典型的有限状态机式编排。

## Why This Project Matters for LLM Application Roles

这个项目不是单纯“调用一次模型 API”，而是展示了完整的大模型应用思路：

- 把 LLM 当作决策模块，而不是把所有逻辑都交给 LLM
- 用 Schema 约束输出，提高系统稳定性
- 用多智能体模拟社会博弈场景
- 用日志和前端回放增强可观测性
- 用 Python 接管状态机与业务规则

如果你投递的是大模型应用开发、多智能体应用、AI 产品原型、Prompt/Agent 工程相关岗位，这类项目比单纯聊天机器人更有说服力。

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create `.env`

在项目根目录创建 `.env`：

```env
DASHSCOPE_API_KEY=your_api_key_here
```

### 3. Verify environment

```bash
python test_env.py
```

### 4. Run the game

```bash
python main_cn.py
```

### 5. Run the replay UI

```bash
python web_ui.py --host 127.0.0.1 --port 6006
```

然后打开：

```text
http://127.0.0.1:6006
```

## Key Files to Study First

如果你想系统学习这个项目，建议按这个顺序看代码：

1. `main_cn.py`
   先看整体编排，再看各阶段函数
2. `structured_output_cn.py`
   理解为什么要用结构化输出
3. `prompt_cn.py`
   理解角色行为约束怎么设计
4. `utils_cn.py`
   理解规则与胜负判断
5. `web_ui.py`
   理解日志解析与前端回放

## Interview Talking Points

你可以这样介绍这个项目：

> 这是一个基于 AgentScope 和 DashScope 的多智能体狼人杀模拟系统。  
> 我把每个角色实现成独立的 LLM Agent，用 Prompt 注入角色身份、目标和性格，再通过 Pydantic 结构化输出约束讨论、投票、查验和技能使用。  
> 游戏流程、技能结算和胜负判断由 Python 规则引擎管理，避免把确定性逻辑交给大模型。  
> 为了增强可观测性，我还实现了一个日志驱动的 Web 回放界面，方便调试 Agent 行为并做产品化展示。

## Possible Improvements

- 接入记忆机制，让 Agent 具备更强的长程推理能力
- 加入 RAG 或外部知识，扩展人物背景和行为风格
- 增加模型评测脚本，对不同 Prompt 和参数做离线对比
- 增加更完整的日志结构，便于数据分析
- 接入数据库保存对局记录
- 支持更多角色和更复杂的规则版本
- 进一步拆分前后端，做成完整 Web 应用

## Notes

- 本项目依赖 DashScope API Key，请勿将 `.env` 提交到 GitHub。
- 日志和回放界面主要用于演示与学习，不是生产级多人在线系统。
- 运行 `main_cn.py` 后，游戏过程会先输出在终端中；`web_ui.py` 会读取生成的日志文件，并将整局对局可视化回放。


