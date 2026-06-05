# Claude-chats_plans-to-obsidian

[中文](#中文) | [English](#english)

---

## 中文

**把 Claude Code 的每一次对话自动保存为可搜索的 Markdown 笔记，同时将 Plan 文件提取为独立版本化笔记到 Obsidian。**

### 功能

#### 会话归档

- SessionEnd/SessionStart hooks 自动触发，解析 JSONL 转录文件
- 提取用户问题、Claude 回复、工具调用记录
- 生成结构化 Markdown，含 YAML frontmatter（`created`/`modified`/`cwd`/`label`/`final_plans`）
- 按 Round 编号组织，带北京时间戳
- 智能过滤系统噪音（system-reminder、hook 输出、skill 指令等）
- 工具调用折叠在 `> [!note]-` Obsidian callout 中，连续纯工具调用自动合并

#### Plan 文件版本管理

- 自动检测对话中对 `~/.claude/plans/` 的 Write/Edit 操作
- 按用户消息边界分组编辑周期，为每次修改创建独立版本
- 版本文件命名：`{H1标题} {YYYYMMDD-HHMMSS}.md`，含 frontmatter（`created`/`modified`/`cwd`/`ref_plan_file`）
- 同标题（H1）的旧版本自动标记为 draft，移入 `draft_plans/` 子目录，仅最新版保留在 `plans/` 根目录
- Session frontmatter 中 `final_plans` 字段列出当前活跃版本（非 draft）
- 会话笔记中以 `## 📋 [[文件名]]` wikilink 精准引用对应版本

#### 多项目目录映射

`~/.claude/projects/` 下每个项目子目录 → Obsidian 下对应子目录，`plans/` 子目录存放该项目的 plan 版本。

#### 增量更新

mtime 检测变化，只重新处理有更新的会话。content hash 去重避免重复创建 plan 版本。

#### 零依赖

纯 Python 标准库，无第三方依赖。

### 输出示例

```markdown
---
created: 2026-05-21T11:50:01
modified: 2026-05-21T15:10:09
cwd: '/Users/admin/Documents/project/Claude-chats_plans-to-obsidian'
label: Claude-chats_plans-to-obsidian
final_plans:
  - '[[修复登录页 token 验证逻辑 20260521-115130.md]]'
---
> 时间：2026-05-21 11:50:01 ~ 2026-05-21 15:10:09
> 轮数：用户 13 轮，Claude 125 轮
> 来源：`904e69a0.jsonl`
> Session：`904e69a0-6f85-44a5-a2b8-8cc47ce0f103`

# Round 1
**用户** `11:50:01`
帮我修复登录页的 bug

**Claude** `11:50:13`
让我先查看相关代码。

**Claude** `11:50:20`
> [!note]- 工具调用
> - `Read`: /path/to/login.ts
> - `Grep`: authMiddleware

**Claude** `11:51:05`
找到问题了，在 auth middleware 里 token 验证逻辑有误。

**Claude** `11:51:30`
> [!note]- 工具调用
> - `Write`: /Users/admin/.claude/plans/elegant-hatching-meerkat.md

## 📋 [[修复登录页 token 验证逻辑 20260521-115130.md]]

# Round 2
**用户** `11:55:00`
好的，按计划执行
```

### 前置条件

- Python 3.10+
- Claude Code CLI
- Obsidian

### 安装

#### 第一步：下载脚本

```bash
git clone https://github.com/chenxiccc/Claude-chats_plans-to-obsidian.git
mkdir -p ~/.claude/scripts/hooks
cp Claude-chats_plans-to-obsidian.py ~/.claude/scripts/hooks/
```

#### 第二步：修改配置

编辑脚本顶部 `# ===== 配置 =====` 区域：

```python
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"
DISPLAY_TZ = timezone(timedelta(hours=8))   # 改成你的时区
```

| 变量 | 说明 |
|------|------|
| `OBSIDIAN_DIR` | Markdown 输出根目录，支持 `OBSIDIAN_DIR` 环境变量覆盖 |
| `TRANSCRIPTS_DIR` | Claude Code 转录文件父目录，支持 `TRANSCRIPTS_DIR` 环境变量覆盖 |
| `DISPLAY_TZ` | 时间戳转换时区 |

#### 第三步：配置 Claude Code Hook

编辑 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "SessionEnd": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py",
        "timeout": 30,
        "async": true
      }]
    }],
    "SessionStart": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py --scan-all",
        "timeout": 60,
        "async": true
      }]
    }]
  }
}
```

#### 第四步：验证

```bash
python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py --scan-all
ls ~/Obsidian/Project/claude/session/
```

### 使用

配置好 Hook 后无需操作，每次会话自动归档。也可手动运行：

```bash
# 归档最近一个会话
python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py

# 全量扫描
python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py --scan-all
```

### 目录结构

```
~/Obsidian/Project/claude/session/
├── Claude-chats_plans-to-obsidian/
│   ├── session_mapping.json
│   ├── plans/
│   │   ├── plan_mapping.json
│   │   ├── draft_plans/
│   │   │   └── 修复登录页bug 20260521-120000.md
│   │   ├── 修复登录页bug 20260521-143000.md
│   │   └── 新增用户权限管理 20260521-153000.md
│   ├── 探讨plans文件如何同步到Obsidian.md
│   └── 修复会话标题提取逻辑.md
└── fns/
    ├── session_mapping.json
    ├── plans/
    │   └── ...
    └── ...
```

> `cwd_mapping.json` 已迁移至 `~/.claude/claude_to_obsidian/`（缓存目录集中管理）

### 工作原理

```
Claude Code 会话 → JSONL 转录文件
    │
    │  SessionEnd / SessionStart hook 触发
    ▼
Claude-chats_plans-to-obsidian.py
    ├─ 解析 JSONL，提取消息和 plan 写入
    ├─ 按编辑周期创建 plan 版本文件
    ├─ H1 分组判定 draft，旧版移入 draft_plans/
    ├─ 按时间戳匹配 plan 引用
    └─ 生成结构化 Markdown（含 final_plans）
    ▼
Obsidian vault
    ├─ {project}/会话笔记.md
    └─ {project}/plans/
        ├─ 最新版本.md
        └─ draft_plans/旧版本.md
```

---

## English

**Automatically save every Claude Code conversation as searchable Markdown notes, and extract Plan files as versioned notes into Obsidian.**

### Features

#### Session Archival

- Auto-triggered by SessionEnd/SessionStart hooks; parses JSONL transcript files
- Extracts user questions, Claude responses, and tool usage
- Generates structured Markdown with YAML frontmatter
- Organized by Round with Beijing time timestamps
- Smart filtering of system noise
- Tool calls collapsed in `> [!note]-` Obsidian callouts; consecutive calls auto-merged

#### Plan File Versioning

- Detects Write/Edit operations to `~/.claude/plans/` during conversations
- Groups edits into cycles by user message boundaries; creates independent versions
- Version filenames: `{H1 title} {YYYYMMDD-HHMMSS}.md` with frontmatter
- Old versions of the same title (H1) auto-marked as draft and moved to `draft_plans/`; only latest stays in `plans/`
- `final_plans` field in session frontmatter lists currently active (non-draft) plan versions
- Session notes reference the correct version via `## 📋 [[filename]]` wikilinks

#### Zero Dependencies

Pure Python standard library.

### Installation

See Chinese section above for detailed steps. Quick start:

```bash
cp Claude-chats_plans-to-obsidian.py ~/.claude/scripts/hooks/
# Configure hooks in ~/.claude/settings.json
python3 ~/.claude/scripts/hooks/Claude-chats_plans-to-obsidian.py --scan-all
```

## License

MIT
