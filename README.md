# session-to-obsidian

[中文](#中文) | [English](#english)

---

## 中文

**把你和 Claude Code 的每一次对话，自动保存为可搜索的 Markdown 笔记到 Obsidian。**

不仅是人类可读的归档，也是 Claude 新窗口的记忆来源 —— 通过 Obsidian MCP，新会话的 Claude 可以搜索和读取过往会话记录，恢复上下文。

### 它解决什么问题

Claude Code 每次开新窗口都是全新的上下文，上一个窗口做了什么、讨论了什么，新窗口全不知道。原始会话记录保存在 `.jsonl` 文件里，格式难读、无法搜索。

这个脚本把它们变成结构化的 Markdown，写入 Obsidian vault，同时服务两个用户：

| 用户 | 怎么用 | 场景 |
|------|--------|------|
| **你（人类）** | 在 Obsidian 里搜索、浏览 | "上周那个 bug 怎么修的"、"之前讨论的方案是什么" |
| **Claude（AI）** | 通过 Obsidian MCP 搜索和读取 | "继续上个窗口的工作"、"上次我们讨论到哪里了" |

### 主要功能

#### 跨窗口记忆承接

新开的 Claude Code 窗口可以通过 Obsidian MCP 调取过往会话记录：
- 用户说"继续上个窗口的工作"，Claude 搜索最近的会话记录，知道上次做了什么
- 用户问"上次那个配置怎么改的"，Claude 能找到对应的历史对话
- 配合 Qdrant/Graphiti 等向量记忆系统，形成多层记忆架构

#### 自动归档

- 每次 Claude Code 会话结束时，自动解析 JSONL 转录文件
- 提取用户问题、Claude 回复、工具调用记录
- 生成结构化 Markdown，含 YAML frontmatter（`created` / `updated` datetime）
- 按 Round 编号、带时间戳

#### 多项目目录映射

`~/.claude/projects/` 下每个项目子目录 → Obsidian 下同名子目录，一一对应组织历史对话。

#### 智能过滤

自动过滤系统噪音，只保留实质对话：
- 过滤 `system-reminder`、hook 输出、skill 指令、`task-notification`
- 跳过 context continuation、tool loaded 等系统消息
- 工具调用详情折叠在 `<details>` 标签内，不干扰阅读

#### 双保险触发

| Hook | 触发时机 | 行为 |
|------|----------|------|
| `SessionEnd` | 会话结束时（`/exit`、`Ctrl+C`、`/clear`） | 归档当前会话 |
| `SessionStart` | 新会话启动时 | `--scan-all` 补归档所有漏掉的 |

> 为什么需要两个？SessionEnd 在直接关终端/崩溃时不触发，SessionStart 作为兜底。

#### 智能标题

- 优先使用 VS Code Claude Code 扩展中的对话"重命名"标题
- 回退到首条用户消息前 20 字，自动清洗 Markdown 格式符

#### 增量更新

- mtime 检测变化，只重新处理有更新的会话
- mapping 文件跟踪 JSONL → Markdown 对应关系，防重复
- 检测到标题变化（VS Code 重命名）自动重新生成，同步更新文件名

#### 零依赖

- 纯 Python 标准库，无第三方依赖
- 直接写文件到 vault 目录，Obsidian 自动识别

### 输出效果

每个会话生成一个 Markdown 文件，文件名：`会话主题.md`

内容包含：
- YAML frontmatter（`created` 和 `updated`，datetime 类型）
- 会话元信息（时间范围、对话轮数、来源文件、Session ID）
- 按 Round 编号的完整问答记录
- 每轮带时间戳（北京时间）
- 工具调用详情（折叠在 `<details>` 标签内）

<details><summary>示例</summary>

```markdown
---
created: 2026-03-01T14:30:25
updated: 2026-03-01T15:45:12
---

# 如何配置项目的自动化测试

> 时间：2026-03-01 14:30:25 ~ 2026-03-01 15:45:12
> 轮数：用户 8 轮，Claude 8 轮
> 来源：`abc12345-xxxx.jsonl`
> Session：`abc12345xxxx...`

---

## Round 1

**用户** `14:30:25`

帮我配置项目的自动化测试

**Claude** `14:30:42`

<details><summary>工具调用</summary>

- `Read`: /path/to/package.json
- `Bash`: `npm test`

</details>

已经帮你配置好了...

---
```

</details>

### 前置条件

- Python 3.10+（使用 `X | Y` 类型语法）
- Claude Code CLI
- Obsidian（任意版本，只需有一个 vault 目录）
- **Obsidian MCP**（可选，但强烈推荐）—— 让 Claude 能搜索和读取历史会话记录

### 安装

#### 第一步：下载脚本

```bash
# 克隆仓库
git clone https://github.com/hailanlan0577/session-to-obsidian.git

# 或者直接下载脚本到 Claude Code hooks 目录
mkdir -p ~/.claude/scripts/hooks
curl -o ~/.claude/scripts/hooks/session-to-obsidian.py \
  https://raw.githubusercontent.com/hailanlan0577/session-to-obsidian/main/session-to-obsidian.py
chmod +x ~/.claude/scripts/hooks/session-to-obsidian.py
```

#### 第二步：修改配置

打开 `session-to-obsidian.py`，修改顶部配置项：

```python
# ===== 配置 =====
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"   # 改成你的 Obsidian vault 子目录
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"  # Claude Code 转录文件父目录
CHINA_TZ = timezone(timedelta(hours=8))  # 改成你的时区
```

> `TRANSCRIPTS_DIR` 设为 `~/.claude/projects/`（父目录），脚本会自动遍历其下所有项目子目录，在 Obsidian 中创建同名子目录一一对应。

| 变量 | 说明 | 如何确定你的值 |
|------|------|---------------|
| `OBSIDIAN_DIR` | Markdown 输出根目录，脚本会自动在其下创建项目子目录 | 打开 Obsidian → 设置 → 查看 vault 路径，然后加一个子文件夹 |
| `TRANSCRIPTS_DIR` | Claude Code 转录文件父目录，包含所有项目子目录 | 运行 `ls ~/.claude/projects/` 确认该目录存在 |
| `CHINA_TZ` | 时间戳转换时区 | 改成你当地时区的 UTC 偏移 |

#### 第三步：配置 Claude Code Hook

编辑 `~/.claude/settings.json`，在 `hooks` 字段中添加：

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/scripts/hooks/session-to-obsidian.py",
            "timeout": 30,
            "async": true
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all",
            "timeout": 60,
            "async": true
          }
        ]
      }
    ]
  }
}
```

#### 第四步：配置 Obsidian MCP（可选但强烈推荐）

要让 Claude 能搜索和读取历史会话，需要配置 Obsidian MCP。

在 `~/.claude/mcp.json`（或项目级 `.mcp.json`）中添加：

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "npx",
      "args": ["-y", "obsidian-mcp"],
      "env": {
        "OBSIDIAN_API_KEY": "<你的 Obsidian Local REST API 密钥>"
      }
    }
  }
}
```

前置条件：
1. 安装 Obsidian 插件 [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)
2. 在插件设置中获取 API Key
3. 确保 Obsidian 客户端开着（REST API 需要它运行）

配置完成后，Claude 就能用 `obsidian_simple_search` 搜索历史会话，用 `obsidian_get_file_contents` 读取具体对话内容。

#### 第五步：验证安装

```bash
# 手动运行一次全量扫描，归档所有历史会话
python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all

# 检查输出目录（按项目子目录组织）
ls ~/Obsidian/Project/claude/session/
```

### 使用方式

**自动模式（推荐）**：配置好 Hook 后无需任何操作，每次会话自动归档。

**手动模式**：

```bash
# 归档最近一个会话
python3 ~/.claude/scripts/hooks/session-to-obsidian.py

# 扫描并补归档所有未处理的会话
python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all
```

### 工作原理

```
Claude Code 会话（各项目目录）
    │
    │  每轮对话写入 JSONL 转录
    ▼
~/.claude/projects/
  ├─ -Users-admin/*.jsonl
  ├─ -Users-admin-Documents-Tech-project-fns/*.jsonl
  └─ ...
    │
    │  SessionEnd / SessionStart hook 触发
    ▼
session-to-obsidian.py
    ├─ 遍历所有项目子目录
    ├─ 解析 JSONL，提取用户/Claude 消息
    ├─ 过滤系统噪音
    ├─ 优先 VS Code 重命名标题 → 回退首条消息
    ├─ 生成结构化 Markdown（含 frontmatter）
    ├─ mtime + mapping + topic 变化检测，防重复
    ▼
~/Obsidian/Project/claude/session/
  ├─ -Users-admin/*.md
  ├─ -Users-admin-Documents-Tech-project-fns/*.md
  └─ ...
    │
    ├─ 人类：Obsidian 里搜索、浏览、回顾
    └─ AI：Claude 通过 grep/Read 搜索历史
```

### 在记忆体系中的位置

| 记忆层 | 工具 | 用途 |
|--------|------|------|
| **对话历史**（本工具） | Obsidian | 完整会话记录，人机双用 |
| 向量记忆 | Qdrant V3 | 语义搜索关键信息 |
| 知识图谱 | Graphiti | 实体关系和时序推理 |
| 上下文注入 | SessionStart hook | 上一次会话摘要 |

### 常见问题

**需要 Obsidian MCP 吗？**
归档功能不需要。但如果你想让 Claude 在新窗口能搜索历史对话，则需要配置 Obsidian MCP。

**关闭终端窗口会漏掉归档吗？**
不会。SessionStart hook 会在下次打开 Claude Code 时自动扫描补归档。

**已归档的会话更新了怎么办？**
脚本通过比较 JSONL 和输出文件的 mtime 检测更新。如果 JSONL 比输出文件新，会重新生成。如果在 VS Code 中重命名了对话标题，脚本也会在下一次扫描时检测到变化并自动更新文件名。

**能不能改输出目录？**
可以，修改脚本顶部的 `OBSIDIAN_DIR` 变量即可。脚本会在该目录下自动创建项目子目录。

**转录目录怎么找？**
```bash
ls ~/.claude/projects/
```
会看到类似 `-Users-yourname`、`-Users-yourname-Documents-xxx` 的目录，每个对应一个项目，里面有 `.jsonl` 文件。脚本的 `TRANSCRIPTS_DIR` 设为父目录 `~/.claude/projects/` 即可自动遍历所有子目录。

---

## English

**Automatically save every Claude Code conversation as a searchable Markdown note in Obsidian.**

Not just a human-readable archive — it's also a memory source for new Claude windows. Via Obsidian MCP, a new Claude session can search and read past conversation records to restore context.

### Problem It Solves

Every new Claude Code window starts with a blank context — it has no idea what the previous window did or discussed. Raw session transcripts are stored as `.jsonl` files, which are unreadable and unsearchable.

This script converts them into structured Markdown in your Obsidian vault, serving two users:

| User | How | Scenario |
|------|-----|----------|
| **You (human)** | Search and browse in Obsidian | "How did I fix that bug last week?" |
| **Claude (AI)** | Search and read via Obsidian MCP | "Continue where we left off", "What did we discuss last time?" |

### Key Features

#### Cross-Window Memory

New Claude Code windows can retrieve past session records via Obsidian MCP:
- User says "continue the previous session's work" → Claude searches recent session records
- User asks "how did we change that config last time?" → Claude finds the relevant conversation
- Works alongside Qdrant/Graphiti vector memory for a multi-layer memory architecture

#### Auto-Archival

- Automatically parses JSONL transcript files when a Claude Code session ends
- Extracts user questions, Claude responses, and tool usage
- Generates structured Markdown with YAML frontmatter (`created` / `updated` datetime)
- Organized by Round numbers with timestamps

#### Multi-Project Directory Mapping

Each subdirectory under `~/.claude/projects/` maps to a same-named subdirectory in Obsidian, organizing conversations by project.

#### Smart Filtering

Filters out system noise, keeping only substantive dialogue:
- Removes `system-reminder`, hook output, skill directives, `task-notification`
- Skips context continuation, "tool loaded" and other system messages
- Tool call details are collapsed in `<details>` tags

#### Dual-Hook Safety Net

| Hook | When | Action |
|------|------|--------|
| `SessionEnd` | Session ends (`/exit`, `Ctrl+C`, `/clear`) | Archive current session |
| `SessionStart` | New session starts | `--scan-all` catches any missed sessions |

> Why two? SessionEnd doesn't fire when the terminal is closed or the process crashes. SessionStart catches those.

#### Smart Titles

- Prioritizes conversation "Rename" titles from VS Code Claude Code extension
- Falls back to first user message (first 20 chars), auto-cleans Markdown formatting

#### Incremental Updates

- Uses mtime comparison to only reprocess changed sessions
- Mapping file tracks JSONL → Markdown relationships, preventing duplicates
- Detects title changes (VS Code renames) and auto-regenerates with updated filenames

#### Zero Dependencies

- Pure Python standard library, no third-party packages
- Writes directly to the vault directory; Obsidian auto-detects new files

### Output Format

Each session generates one Markdown file: `topic.md`

Contents:
- YAML frontmatter (`created` and `updated`, datetime type)
- Session metadata (time range, round count, source file, Session ID)
- Complete Q&A records organized by Round
- Timestamps per round (configurable timezone)
- Tool call details (collapsed in `<details>` tags)

<details><summary>Example</summary>

```markdown
---
created: 2026-03-01T14:30:25
updated: 2026-03-01T15:45:12
---

# How to configure automated testing

> Time: 2026-03-01 14:30:25 ~ 2026-03-01 15:45:12
> Rounds: User 8, Claude 8
> Source: `abc12345-xxxx.jsonl`
> Session: `abc12345xxxx...`

---

## Round 1

**User** `14:30:25`

Help me configure automated testing for the project

**Claude** `14:30:42`

<details><summary>Tool calls</summary>

- `Read`: /path/to/package.json
- `Bash`: `npm test`

</details>

I've configured it for you...

---
```

</details>

### Prerequisites

- Python 3.10+ (uses `X | Y` type syntax)
- Claude Code CLI
- Obsidian (any version, just need a vault directory)
- **Obsidian MCP** (optional but strongly recommended) — enables Claude to search/read past sessions

### Installation

#### Step 1: Download the Script

```bash
# Clone the repo
git clone https://github.com/hailanlan0577/session-to-obsidian.git

# Or download directly to Claude Code hooks directory
mkdir -p ~/.claude/scripts/hooks
curl -o ~/.claude/scripts/hooks/session-to-obsidian.py \
  https://raw.githubusercontent.com/hailanlan0577/session-to-obsidian/main/session-to-obsidian.py
chmod +x ~/.claude/scripts/hooks/session-to-obsidian.py
```

#### Step 2: Configure

Edit the config section at the top of `session-to-obsidian.py`:

```python
# ===== Config =====
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"   # Your Obsidian vault subdirectory
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"  # Claude Code transcript parent directory
CHINA_TZ = timezone(timedelta(hours=8))  # Your timezone
```

> Set `TRANSCRIPTS_DIR` to `~/.claude/projects/` (parent). The script auto-traverses all project subdirectories and creates matching subdirectories in Obsidian.

| Variable | Description | How to Find Your Value |
|----------|-------------|------------------------|
| `OBSIDIAN_DIR` | Output root directory; project subdirectories are auto-created under it | Obsidian → Settings → vault path, then add a subfolder |
| `TRANSCRIPTS_DIR` | Claude Code transcript parent directory, containing all project subdirectories | Run `ls ~/.claude/projects/` to confirm it exists |
| `CHINA_TZ` | Timezone for timestamps | Change to your local UTC offset |

#### Step 3: Configure Claude Code Hooks

Edit `~/.claude/settings.json`, add to `hooks`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/scripts/hooks/session-to-obsidian.py",
            "timeout": 30,
            "async": true
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all",
            "timeout": 60,
            "async": true
          }
        ]
      }
    ]
  }
}
```

#### Step 4: Configure Obsidian MCP (Optional but Recommended)

To let Claude search and read past sessions, configure Obsidian MCP.

Add to `~/.claude/mcp.json` (or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "npx",
      "args": ["-y", "obsidian-mcp"],
      "env": {
        "OBSIDIAN_API_KEY": "<your Obsidian Local REST API key>"
      }
    }
  }
}
```

Prerequisites:
1. Install the Obsidian plugin [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)
2. Get the API Key from plugin settings
3. Keep the Obsidian app running (the REST API requires it)

#### Step 5: Verify

```bash
# Run a full scan to archive all historical sessions
python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all

# Check the output directory (organized by project subdirectory)
ls ~/Obsidian/Project/claude/session/
```

### Usage

**Automatic (recommended)**: Once hooks are configured, every session is archived automatically.

**Manual**:

```bash
# Archive the most recent session
python3 ~/.claude/scripts/hooks/session-to-obsidian.py

# Scan and archive all unprocessed sessions
python3 ~/.claude/scripts/hooks/session-to-obsidian.py --scan-all
```

### How It Works

```
Claude Code sessions (per project directory)
    │
    │  Each round writes to JSONL transcript
    ▼
~/.claude/projects/
  ├─ -Users-admin/*.jsonl
  ├─ -Users-admin-Documents-Tech-project-fns/*.jsonl
  └─ ...
    │
    │  SessionEnd / SessionStart hook triggers
    ▼
session-to-obsidian.py
    ├─ Traverse all project subdirectories
    ├─ Parse JSONL, extract user/Claude messages
    ├─ Filter system noise
    ├─ Prioritize VS Code rename → fallback to first message
    ├─ Generate structured Markdown (with frontmatter)
    ├─ mtime + mapping + topic change detection, deduplication
    ▼
~/Obsidian/Project/claude/session/
  ├─ -Users-admin/*.md
  ├─ -Users-admin-Documents-Tech-project-fns/*.md
  └─ ...
    │
    ├─ Human: search, browse, review in Obsidian
    └─ AI: Claude searches history via grep/Read
```

### Role in the Memory Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| **Conversation history** (this tool) | Obsidian | Complete session records, human + AI accessible |
| Vector memory | Qdrant V3 | Semantic search of key information |
| Knowledge graph | Graphiti | Entity relationships and temporal reasoning |
| Context injection | SessionStart hook | Previous session summary |

### FAQ

**Do I need Obsidian MCP?**
Not for archiving. But if you want Claude to search past conversations in new windows, you need it.

**Will closing the terminal miss an archive?**
No. SessionStart hook auto-scans on next Claude Code launch.

**What if an archived session gets updated?**
The script compares JSONL and output file mtime. If the JSONL is newer, it regenerates. If you renamed the conversation in VS Code, the script detects the title change on the next scan and auto-updates the filename.

**Can I change the output directory?**
Yes, modify `OBSIDIAN_DIR` at the top of the script. Project subdirectories are auto-created under it.

**How do I find my transcripts directory?**
```bash
ls ~/.claude/projects/
```
Look for directories like `-Users-yourname`, `-Users-yourname-Documents-xxx` — each corresponds to a project. Set `TRANSCRIPTS_DIR` to the parent `~/.claude/projects/` and the script auto-traverses all subdirectories.

## License

MIT
