#!/usr/bin/env python3
"""
Claude Code 会话记录 → Obsidian 自动归档
SessionEnd hook：每次会话结束时，将完整对话转为 Markdown 写入 Obsidian vault。

TRANSCRIPTS_DIR 下的每个项目子目录映射到 OBSIDIAN_DIR 下的同名子目录，
一一对应组织历史对话。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===== 配置 =====
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"
MAX_TOOL_RESULT_LEN = 500  # 工具结果最大显示长度
MAX_THINKING_LEN = 0       # thinking 不记录（设 0 跳过）
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

                # 过滤系统注入内容
                text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text)
                text = re.sub(r"<local-command-caveat>[\s\S]*?</local-command-caveat>", "", text)
                text = re.sub(r"<command-name>[\s\S]*?</command-name>", "", text)
                text = re.sub(r"<command-message>[\s\S]*?</command-message>", "", text)
                text = re.sub(r"<command-args>[\s\S]*?</command-args>", "", text)
                text = re.sub(r"<local-command-stdout>[\s\S]*?</local-command-stdout>", "", text)
                text = re.sub(r"<task-notification>[\s\S]*?</task-notification>", "", text)
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

                messages.append({
                    "role": "user",
                    "text": text,
                    "timestamp": timestamp,
                })
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
                        # 简化工具调用记录
                        if tool_name in ("Read", "Glob", "Grep"):
                            param = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("path", "")
                            tools_used.append(f"`{tool_name}`: {param}")
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            if len(cmd) > 120:
                                cmd = cmd[:120] + "..."
                            tools_used.append(f"`Bash`: `{cmd}`")
                        elif tool_name == "Edit":
                            fp = tool_input.get("file_path", "")
                            tools_used.append(f"`Edit`: {fp}")
                        elif tool_name == "Write":
                            fp = tool_input.get("file_path", "")
                            tools_used.append(f"`Write`: {fp}")
                        elif tool_name.startswith("mcp__"):
                            short = tool_name.split("__")[-1]
                            tools_used.append(f"`{short}`")
                        else:
                            tools_used.append(f"`{tool_name}`")
                    # skip thinking, tool_result etc.

                text = "\n\n".join(parts)
                if not text and not tools_used:
                    continue

                msg = {"role": "assistant", "text": text, "timestamp": timestamp}
                if tools_used:
                    msg["tools"] = tools_used
                messages.append(msg)
                last_ts = timestamp

    return messages, session_id, first_ts, last_ts


def extract_topic(messages, session_id=None):
    """提取会话主题：优先 VS Code 重命名，回退第一条用户消息（前20字，去格式符）"""
    # 优先：VS Code 重命名的自定义标题（也需清洗非法字符）
    if session_id:
        name = find_session_name(session_id)
        if name:
            name = re.sub(r'[/\\:*?"<>|]', '', name).strip()
            if name:
                return name

    # 回退：第一条用户消息
    for m in messages:
        if m["role"] == "user":
            text = m["text"]
            # 1. 换行替换为空格
            text = text.replace("\n", " ").strip()
            # 2. 去掉 Markdown 格式符（保留普通文字）
            text = re.sub(r'[#*_`~>\[\]!|\\]', '', text)
            # 3. 去掉首尾空格
            text = text.strip()
            # 4. 去掉路径不安全字符
            text = re.sub(r'[/\\:*?"<>|]', '', text)
            # 5. 截取前 20 字
            if len(text) > 20:
                text = text[:20]
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


def generate_markdown(messages, session_id, first_ts, last_ts, filepath, topic):
    """生成 Markdown 文档（含 frontmatter）"""
    user_count = sum(1 for m in messages if m["role"] == "user")
    assistant_count = sum(1 for m in messages if m["role"] == "assistant")
    all_tools = []
    for m in messages:
        all_tools.extend(m.get("tools", []))

    lines = []
    # YAML frontmatter
    lines.append("---")
    lines.append(f"created: {format_frontmatter_datetime(first_ts)}")
    lines.append(f"updated: {format_frontmatter_datetime(last_ts)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {topic}")
    lines.append("")
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
            if len(text) > 2000:
                text = text[:2000] + "\n\n... (已截断)"
            lines.append(text)
            lines.append("")

        elif m["role"] == "assistant":
            lines.append(f"**Claude** `{ts}`")
            lines.append("")

            # 工具调用
            if m.get("tools"):
                lines.append("<details><summary>工具调用</summary>")
                lines.append("")
                for t in m["tools"]:
                    lines.append(f"- {t}")
                lines.append("")
                lines.append("</details>")
                lines.append("")

            # 回复内容
            text = escape_obsidian_tags(m["text"])
            if len(text) > 3000:
                text = text[:3000] + "\n\n... (已截断)"
            if text:
                lines.append(text)
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def find_existing_output(stem: str, output_subdir: Path) -> tuple[Path | None, str | None]:
    """根据 JSONL stem 找到已存在的输出文件和旧 topic。
    返回 (Path, old_topic)，不存在则返回 (None, None)。
    兼容旧 mapping 格式（字符串值）和新格式（dict 含 filename + topic）。
    """
    mapping_file = output_subdir / ".session_mapping.json"
    if not mapping_file.exists():
        return None, None
    try:
        mapping = json.loads(mapping_file.read_text())
        entry = mapping.get(stem)
        if entry is None:
            return None, None
        # 向后兼容旧格式：字符串值
        if isinstance(entry, str):
            p = output_subdir / entry
            return (p, None) if p.exists() else (None, None)
        # 新格式：dict 含 filename + topic
        if isinstance(entry, dict):
            filename = entry.get("filename", "")
            old_topic = entry.get("topic")
            p = output_subdir / filename
            return (p, old_topic) if p.exists() else (None, old_topic)
        return None, None
    except (json.JSONDecodeError, OSError):
        return None, None


def save_mapping(stem: str, filename: str, output_subdir: Path, topic: str) -> None:
    """记录 JSONL stem → {filename, topic} 的映射（每个项目子目录独立维护）"""
    mapping_file = output_subdir / ".session_mapping.json"
    mapping = {}
    if mapping_file.exists():
        try:
            mapping = json.loads(mapping_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    mapping[stem] = {"filename": filename, "topic": topic}
    mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))


def process_one(transcript: Path, output_subdir: Path) -> str | None:
    """处理单个转录文件，返回生成的文件名，无变化则返回 None"""
    output_subdir.mkdir(parents=True, exist_ok=True)

    messages, session_id, first_ts, last_ts = parse_transcript(transcript)
    if not messages or len(messages) < 2:
        return None

    # 计算当前 topic（优先 VS Code name，回退首条用户消息）
    topic = extract_topic(messages, session_id)

    # 查找已有输出和旧 topic
    existing, old_topic = find_existing_output(transcript.stem, output_subdir)

    # 判断是否需要重新生成
    need_regenerate = False
    if existing is None:
        need_regenerate = True
    elif existing.stat().st_mtime < transcript.stat().st_mtime:
        need_regenerate = True  # JSONL 比 MD 新，内容变了
    elif old_topic is not None and topic != old_topic:
        need_regenerate = True  # 标题变了（仅新格式 mapping 可检测）

    if not need_regenerate:
        return None

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

    md = generate_markdown(messages, session_id, first_ts, last_ts, transcript, topic)
    output_path.write_text(md, encoding="utf-8")
    save_mapping(transcript.stem, filename, output_subdir, topic)

    is_update = existing is not None
    subdir_name = output_subdir.name
    return f"[{subdir_name}] {filename} (更新)" if is_update else f"[{subdir_name}] {filename}"


def main():
    # 确保输出根目录存在
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

    scan_all = "--scan-all" in sys.argv

    if scan_all:
        # 扫描所有项目子目录的所有 JSONL 文件
        processed = 0
        total_jsonl = 0
        for subdir in sorted(TRANSCRIPTS_DIR.iterdir()):
            if not subdir.is_dir():
                continue
            jsonl_files = sorted(subdir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
            if not jsonl_files:
                continue
            total_jsonl += len(jsonl_files)
            output_subdir = OBSIDIAN_DIR / subdir.name
            for transcript in jsonl_files:
                result = process_one(transcript, output_subdir)
                if result:
                    print(f"  归档: {result}", file=sys.stderr)
                    processed += 1
        print(f"扫描完成: {total_jsonl} 个会话, 新归档 {processed} 个", file=sys.stderr)
    else:
        # 只处理最新的一个（跨所有子目录找最近修改的 JSONL）
        transcript = find_latest_transcript()
        if not transcript:
            sys.exit(0)
        # 根据 JSONL 所在子目录确定输出子目录
        output_subdir = OBSIDIAN_DIR / transcript.parent.name
        result = process_one(transcript, output_subdir)
        if result:
            print(f"Session saved to Obsidian: {result}", file=sys.stderr)


if __name__ == "__main__":
    main()
