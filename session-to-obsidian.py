#!/usr/bin/env python3
"""
Claude Code 会话记录 → Obsidian 自动归档
SessionEnd hook：每次会话结束时，将完整对话转为 Markdown 写入 Obsidian vault。

TRANSCRIPTS_DIR 下的每个项目子目录映射到 OBSIDIAN_DIR 下的子目录，
文件夹名由 JSONL 中的 cwd 字段派生：取最后一级项目名，冲突时逐级追加父目录。
"""

import json
import sqlite3
import sys
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===== 配置 =====
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"

# 是否在会话记录中引用 Obsidian 中的计划文件 / Reference plan files in Obsidian via wikilinks
# 0 = 不引用，工具调用中对计划文件的 Write/Edit/Read 操作保持折叠在 <details> 中
# 1 = 生成 [[filename]] wikilink 引用 Obsidian 中同名的计划文件（不折叠，直接可见）
# 引用的前提：需先将 ~/.claude/plans 软链接到 Obsidian vault 下，操作方式：
#   mkdir -p ~/Obsidian/Project/claude
#   ln -s ~/.claude/plans ~/Obsidian/Project/claude/plans

REFERENCE_PLANS_IN_OBSIDIAN = 1

CHINA_TZ = timezone(timedelta(hours=8))


def find_latest_transcript():
    """跨所有项目子目录找最近修改的 .jsonl 文件"""
    latest = None
    latest_mtime = 0
    for subdir in TRANSCRIPTS_DIR.iterdir():
        if not subdir.is_dir():
            continue
        for f in subdir.glob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = f
    return latest


def get_cwd_from_jsonl(jsonl_path: Path) -> str | None:
    """从 JSONL 文件中提取工作目录（cwd） / Extract working directory from JSONL file"""
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i >= 100:  # 前 100 行内必定有 user 消息 / user message always within first 100 lines
                    break
                try:
                    d = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "user" and d.get("userType") == "external":
                    cwd = d.get("cwd")
                    if cwd:
                        return cwd
                    return None
    except OSError:
        return None
    return None


def resolve_folder_name(cwd: str, used_names: set[str]) -> str:
    """根据 cwd 生成唯一文件夹名，冲突时逐级追加父目录 / Generate unique folder name from cwd, append parent dirs on conflict"""
    parts = [p for p in cwd.split("/") if p]
    parts.reverse()  # [项目名, 父级, 祖父级, ...] / [project, parent, grandparent, ...]

    name = parts[0]
    idx = 1
    while name in used_names and idx < len(parts):
        name = f"{name}-{parts[idx]}"
        idx += 1

    used_names.add(name)
    return name


def load_cwd_mapping() -> dict[str, str]:
    """读取 cwd → folder_name 映射 / Load cwd → folder_name mapping"""
    mapping_file = OBSIDIAN_DIR / ".cwd_mapping.json"
    if not mapping_file.exists():
        return {}
    try:
        # 文件存储为 {folder_name: cwd}，反转为 {cwd: folder_name} 便于查询 / File stores {folder_name: cwd}, invert for O(1) lookup
        raw = json.loads(mapping_file.read_text())
        return {cwd: name for name, cwd in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cwd_mapping(name_to_cwd: dict[str, str]) -> None:
    """全量写入 {folder_name: cwd} 映射 / Full rewrite of folder_name → cwd mapping"""
    mapping_file = OBSIDIAN_DIR / ".cwd_mapping.json"
    mapping_file.write_text(json.dumps(name_to_cwd, ensure_ascii=False, indent=2))


# 匹配 /.claude/plans/*.md 路径，兼容 Unix / Windows 分隔符 / Match /.claude/plans/*.md paths, cross-platform separators
_PLAN_PATH_RE = re.compile(r'[/\\]\.claude[/\\]plans[/\\]([^\s)\]\*]+?)\.md')


def extract_plan_refs(text: str) -> list[str]:
    """从文本中提取 .claude/plans/*.md 引用，返回去重 wikilink 列表 / Extract plan refs from text, return deduplicated wikilinks"""
    if not REFERENCE_PLANS_IN_OBSIDIAN:
        return []
    names = _PLAN_PATH_RE.findall(text)
    seen: set[str] = set()
    refs: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            refs.append(f"[[{name}]]")
    return refs


# VS Code 会话标签缓存（首次调用时构建）
_vscode_labels: dict[str, str] | None = None


def build_vscode_label_index() -> dict[str, str]:
    """扫描所有 VS Code workspace state.vscdb，构建 sessionId → label 索引"""
    index: dict[str, str] = {}
    ws_base = Path.home() / "Library/Application Support/Code/User/workspaceStorage"
    if not ws_base.exists():
        return index
    for wsdir in ws_base.iterdir():
        db = wsdir / "state.vscdb"
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(str(db))
            rows = conn.execute(
                "SELECT value FROM ItemTable WHERE value LIKE '%claude-code%'"
            ).fetchall()
            conn.close()
            for (value,) in rows:
                try:
                    entries = json.loads(value)
                    if isinstance(entries, list):
                        for entry in entries:
                            if isinstance(entry, dict) and entry.get("providerType") == "claude-code":
                                resource = entry.get("resource", "")
                                label = entry.get("label", "")
                                if resource.startswith("claude-code:/"):
                                    sid = resource.split("claude-code:/")[-1]
                                    if sid and sid not in index:
                                        index[sid] = label
                except json.JSONDecodeError:
                    pass
        except Exception:
            continue
    return index


def find_session_name(session_id: str) -> str | None:
    """从 VS Code workspace state.vscdb 查找会话标签（持久化存储）"""
    global _vscode_labels
    if not session_id:
        return None
    if _vscode_labels is None:
        _vscode_labels = build_vscode_label_index()
    return _vscode_labels.get(session_id)


def format_frontmatter_datetime(ts_str):
    """ISO 时间戳 → 北京时间 YYYY-MM-DDTHH:MM:SS（Obsidian datetime 格式）"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt = dt.astimezone(CHINA_TZ)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ts_str[:19]


def parse_transcript(filepath):
    """解析 JSONL 转录文件，提取对话内容"""
    messages = []
    session_id = None
    first_ts = None
    last_ts = None

    with open(filepath) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            msg_type = d.get("type")
            timestamp = d.get("timestamp", "")

            if msg_type == "user" and d.get("userType") == "external":
                if not session_id:
                    session_id = d.get("sessionId", "")
                if not first_ts and timestamp:
                    first_ts = timestamp

                content = d["message"].get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text", ""))
                        elif isinstance(c, dict) and c.get("type") == "image":
                            parts.append("[图片]")
                    text = "\n".join(parts)
                else:
                    text = str(content)

                # 过滤 Claude Code 系统注入的 XML 标签
                text = re.sub(
                    r'<(system-reminder|local-command-caveat|command-name|command-message'
                    r'|command-args|local-command-stdout|task-notification'
                    r'|ide_opened_file|ide_selection|ide_diagnostics)>[\s\S]*?</\1>',
                    '', text)
                text = text.strip()

                if not text:
                    continue

                # 跳过 skill 激活指令和 context continuation 等非用户内容
                skip_patterns = [
                    r"^Base directory for this skill:",
                    r"^This session is being continued from",
                    r"^<context-window-compacted>",
                    r"^Tool loaded\.$",
                    r"^Human:",
                ]
                if any(re.match(p, text) for p in skip_patterns):
                    continue

                msg = {"role": "user", "text": text, "timestamp": timestamp}
                plan_refs = extract_plan_refs(text)
                if plan_refs:
                    msg["plan_refs"] = plan_refs
                messages.append(msg)
                last_ts = timestamp

            elif msg_type == "assistant":
                content_items = d["message"].get("content", [])
                parts = []
                tools_used = []

                for c in content_items:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct == "text":
                        t = c.get("text", "").strip()
                        if t:
                            parts.append(t)
                    elif ct == "tool_use":
                        tool_name = c.get("name", "unknown")
                        tool_input = c.get("input", {})
                        # 简化工具调用记录 / Simplify tool call records
                        if tool_name in ("Glob", "Grep"):
                            param = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("path", "")
                            tools_used.append(f"`{tool_name}`: {param}")
                        elif tool_name == "Read":
                            fp = tool_input.get("file_path", "")
                            tools_used.append(f"`Read`: {fp}")
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            if len(cmd) > 120:
                                cmd = cmd[:120] + "..."
                            tools_used.append(f"`Bash`: `{cmd}`")
                        elif tool_name in ("Edit", "Write"):
                            fp = tool_input.get("file_path", "")
                            tools_used.append(f"`{tool_name}`: {fp}")
                        elif tool_name.startswith("mcp__"):
                            short = tool_name.split("__")[-1]
                            tools_used.append(f"`{short}`")
                        else:
                            tools_used.append(f"`{tool_name}`")
                    # skip thinking, tool_result etc.

                text = "\n\n".join(parts)
                if not text and not tools_used:
                    continue

                # 从文本和工具调用路径中提取计划文件引用 / Extract plan refs from text and tool paths
                scan_text = text
                if tools_used:
                    scan_text += "\n" + "\n".join(tools_used)
                plan_refs = extract_plan_refs(scan_text)
                msg = {"role": "assistant", "text": text, "timestamp": timestamp}
                if tools_used:
                    msg["tools"] = tools_used
                if plan_refs:
                    msg["plan_refs"] = plan_refs
                messages.append(msg)
                last_ts = timestamp

    return messages, session_id, first_ts, last_ts


def extract_topic(messages):
    """提取会话主题：取首条用户消息，按 East Asian Width 截断至 40 单位宽度"""
    for m in messages:
        if m["role"] == "user":
            text = m["text"]
            text = text.replace("\n", " ").strip()
            text = re.sub(r'[#*_`~>\[\]!|\\]', '', text).strip()
            text = re.sub(r'[/\\:*?"<>|]', '', text)
            # East Asian Width 截断：CJK/全角计 2，ASCII 计 1，累计 ≤ 40
            width = 0
            result = []
            for ch in text:
                w = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
                if width + w > 40:
                    break
                result.append(ch)
                width += w
            text = ''.join(result)
            return text if text else "未命名会话"
    return "未命名会话"


def format_timestamp(ts_str):
    """ISO 时间戳 → 北京时间"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt = dt.astimezone(CHINA_TZ)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts_str[:19]


def format_datetime(ts_str):
    """ISO 时间戳 → 北京时间完整格式"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt = dt.astimezone(CHINA_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str[:19]


def escape_obsidian_tags(text):
    """转义 # 号防止 Obsidian 误识别为标签（# 后紧跟字母/数字/中文时转义）"""
    return re.sub(r'(^|[\s\W])#(?=[\w一-鿿])', r'\1\\#', text)


def sanitize_markdown_links(text: str) -> str:
    """去掉 Markdown 链接格式避免 Obsidian 误渲染 / Strip markdown link syntax to avoid Obsidian misrendering"""
    # [text](url) → text (url) — 纯文本保留路径 / plain text with path
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    # 独立 [text] → 去掉方括号 / standalone [text] → remove brackets
    text = re.sub(r'\[([^\]]+)\](?!\()', r'\1', text)
    return text


def generate_markdown(messages, session_id, first_ts, last_ts, filepath, topic, label=None):
    """生成 Markdown 文档（含 frontmatter）"""
    user_count = sum(1 for m in messages if m["role"] == "user")
    assistant_count = sum(1 for m in messages if m["role"] == "assistant")

    # 收集整个会话涉及的计划文件（去重） / Collect session-wide plan refs (deduplicated)
    session_plan_refs: list[str] = []
    _seen_plans: set[str] = set()
    for m in messages:
        for ref in m.get("plan_refs", []):
            if ref not in _seen_plans:
                _seen_plans.add(ref)
                session_plan_refs.append(ref)

    lines = []
    # YAML frontmatter
    lines.append("---")
    lines.append(f"created: {format_frontmatter_datetime(first_ts)}")
    lines.append(f"modified: {format_frontmatter_datetime(last_ts)}")
    if label:
        lines.append(f"label: {label}")
    if session_plan_refs:
        lines.append("plans:")
        for ref in session_plan_refs:
            lines.append(f'  - "{ref}"')
    lines.append("---")
    lines.append(f"> 时间：{format_datetime(first_ts)} ~ {format_datetime(last_ts)}")
    lines.append(f"> 轮数：用户 {user_count} 轮，Claude {assistant_count} 轮")
    lines.append(f"> 来源：`{filepath.name}`")
    if session_id:
        lines.append(f"> Session：`{session_id[:12]}...`")
    lines.append("")
    lines.append("---")
    lines.append("")

    round_num = 0
    for m in messages:
        ts = format_timestamp(m["timestamp"])

        if m["role"] == "user":
            round_num += 1
            lines.append(f"## Round {round_num}")
            lines.append("")
            lines.append(f"**用户** `{ts}`")
            lines.append("")
            # 用户消息原文
            text = escape_obsidian_tags(m["text"])
            text = sanitize_markdown_links(text)
            if len(text) > 2000:
                text = text[:2000] + "\n\n... (已截断)"
            lines.append(text)
            if m.get("plan_refs"):
                for ref in m["plan_refs"]:
                    lines.append(f"> 📋 {ref}")
            lines.append("")

        elif m["role"] == "assistant":
            lines.append(f"**Claude** `{ts}`")
            lines.append("")

            # 回复内容 / Response text
            text = escape_obsidian_tags(m["text"])
            text = sanitize_markdown_links(text)
            if len(text) > 3000:
                text = text[:3000] + "\n\n... (已截断)"
            if text:
                lines.append(text)
                lines.append("")

            # 工具调用（折叠） / Tool calls (folded)
            if m.get("tools"):
                lines.append("<details><summary>工具调用</summary>")
                lines.append("")
                for t in m["tools"]:
                    lines.append(f"- {t}")
                lines.append("")
                lines.append("</details>")
                lines.append("")

            # 计划文件引用（内容下方） / Plan file references (below content)
            if m.get("plan_refs"):
                for ref in m["plan_refs"]:
                    lines.append(f"> 📋 {ref}")
                lines.append("")

            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def find_existing_output(stem: str, output_subdir: Path) -> Path | None:
    """根据 JSONL stem 找到已存在的输出文件，不存在则返回 None"""
    mapping_file = output_subdir / ".session_mapping.json"
    if not mapping_file.exists():
        return None
    try:
        mapping = json.loads(mapping_file.read_text())
        entry = mapping.get(stem)
        if entry is None:
            return None
        p = output_subdir / entry
        return p if p.exists() else None
    except (json.JSONDecodeError, OSError):
        return None


def save_mapping(stem: str, filename: str, output_subdir: Path) -> None:
    """记录 JSONL stem → filename 的映射（每个项目子目录独立维护）"""
    mapping_file = output_subdir / ".session_mapping.json"
    mapping = {}
    if mapping_file.exists():
        try:
            mapping = json.loads(mapping_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    mapping[stem] = filename
    mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))


def process_one(transcript: Path, output_subdir: Path) -> str | None:
    """处理单个转录文件，返回生成的文件名，无变化则返回 None"""
    output_subdir.mkdir(parents=True, exist_ok=True)

    # mtime 增量检测：先做轻量检查，避免不必要的 JSONL 解析
    existing = find_existing_output(transcript.stem, output_subdir)
    if existing and existing.stat().st_mtime >= transcript.stat().st_mtime:
        return None

    messages, session_id, first_ts, last_ts = parse_transcript(transcript)
    if not messages or len(messages) < 2:
        return None

    topic = extract_topic(messages)

    # 生成文件名（纯主题，同名自动加 _2 _3 去重）
    base_filename = f"{topic}.md"
    filename = base_filename
    counter = 2
    while True:
        candidate = output_subdir / filename
        if not candidate.exists() or candidate == existing:
            break
        filename = f"{topic}_{counter}.md"
        counter += 1

    output_path = output_subdir / filename

    # 如果旧文件还在且文件名不同，删掉旧的
    if existing and existing != output_path:
        existing.unlink(missing_ok=True)

    # 获取 VS Code 标签（仅写入 frontmatter 元数据，不做标题）
    label = find_session_name(session_id)

    md = generate_markdown(messages, session_id, first_ts, last_ts, transcript, topic, label)
    output_path.write_text(md, encoding="utf-8")
    save_mapping(transcript.stem, filename, output_subdir)

    is_update = existing is not None
    subdir_name = output_subdir.name
    return f"[{subdir_name}] {filename} (更新)" if is_update else f"[{subdir_name}] {filename}"


def main():
    # 确保输出根目录存在
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

    scan_all = "--scan-all" in sys.argv

    if scan_all:
        # 扫描所有项目子目录的所有 JSONL 文件 / Scan all JSONL files in all project subdirectories
        processed = 0
        total_jsonl = 0
        used_names: set[str] = set()
        name_to_cwd: dict[str, str] = {}  # {folder_name: cwd} / folder name → cwd mapping
        for subdir in sorted(TRANSCRIPTS_DIR.iterdir()):
            if not subdir.is_dir():
                continue
            jsonl_files = sorted(subdir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
            if not jsonl_files:
                continue
            total_jsonl += len(jsonl_files)
            # 从该子目录下任意 JSONL 获取 cwd，生成新文件夹名 / Get cwd from any JSONL in subdir, generate new folder name
            cwd = get_cwd_from_jsonl(jsonl_files[0])
            folder_name = resolve_folder_name(cwd, used_names) if cwd else subdir.name
            output_subdir = OBSIDIAN_DIR / folder_name
            if cwd:
                name_to_cwd[folder_name] = cwd
            for transcript in jsonl_files:
                result = process_one(transcript, output_subdir)
                if result:
                    print(f"  归档: {result}", file=sys.stderr)
                    processed += 1
        # 持久化 cwd → folder_name 映射，供增量 SessionEnd 查询 / Persist cwd → folder_name mapping for SessionEnd to query
        save_cwd_mapping(name_to_cwd)
        print(f"扫描完成: {total_jsonl} 个会话, 新归档 {processed} 个", file=sys.stderr)
    else:
        # 只处理最新的一个（跨所有子目录找最近修改的 JSONL） / Process only the latest one
        transcript = find_latest_transcript()
        if not transcript:
            sys.exit(0)
        # 根据 cwd 生成新文件夹名 / Generate new folder name from cwd
        cwd = get_cwd_from_jsonl(transcript)
        if cwd:
            cwd_to_name = load_cwd_mapping()  # {cwd: folder_name} / cwd → folder name lookup
            if cwd in cwd_to_name:
                # 已有映射，直接复用 / Already mapped, reuse
                folder_name = cwd_to_name[cwd]
            else:
                # 新 cwd，需解析冲突 / New cwd, resolve against existing
                existing_names = set(cwd_to_name.values())
                folder_name = resolve_folder_name(cwd, existing_names)
                # 持久化新映射 / Persist new mapping
                name_to_cwd = {v: k for k, v in cwd_to_name.items()}
                name_to_cwd[folder_name] = cwd
                save_cwd_mapping(name_to_cwd)
        else:
            folder_name = transcript.parent.name
        output_subdir = OBSIDIAN_DIR / folder_name
        result = process_one(transcript, output_subdir)
        if result:
            print(f"Session saved to Obsidian: {result}", file=sys.stderr)


if __name__ == "__main__":
    main()
