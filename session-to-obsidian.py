#!/usr/bin/env python3
"""
Claude Code 会话记录 → Obsidian 自动归档
SessionEnd hook：每次会话结束时，将完整对话转为 Markdown 写入 Obsidian vault。

TRANSCRIPTS_DIR 下的每个项目子目录映射到 OBSIDIAN_DIR 下的子目录，
文件夹名由 JSONL 中的 cwd 字段派生：取最后一级项目名，冲突时逐级追加父目录。
"""

import hashlib
import json
import os
import sqlite3
import sys
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===== 配置 =====
OBSIDIAN_DIR = Path.home() / "Obsidian" / "Project" / "claude" / "session"
TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"

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


def get_cwd_from_jsonl(*jsonl_paths: Path) -> str | None:
    """从 JSONL 文件列表中提取工作目录（cwd），逐个尝试直到找到 / Try each JSONL in order until cwd is found"""
    for jsonl_path in jsonl_paths:
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
                        break  # 有 user 消息但无 cwd，跳过此文件 / user message found but no cwd, try next file
        except OSError:
            continue
    return None


def resolve_folder_name(cwd: str, used_names: set[str]) -> str:
    """根据 cwd 生成唯一文件夹名，冲突时逐级追加父目录 / Generate unique folder name from cwd, append parent dirs on conflict"""
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]  # 兼容 Unix / 和 Windows \ / cross-platform separators
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


# 文件名不安全字符（跨平台交集） / Filesystem-unsafe characters (cross-platform intersection)
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|]')

# plan 映射缓存（按输出目录缓存） / Plan mapping cache (keyed by output directory)
_plan_mapping_cache: dict[str, dict] = {}


def extract_h1_from_content(content: str) -> str | None:
    """从 Markdown 内容中提取第一行 H1 标题 / Extract first-line H1 heading from markdown content"""
    if not content:
        return None
    first_line = content.strip().split("\n")[0].strip()
    if first_line.startswith("# "):
        return first_line[2:].strip()
    return None


def sanitize_plan_filename(text: str) -> str:
    """清理文件名中的不安全字符，替换为 _ / Replace filesystem-unsafe chars with _"""
    text = _UNSAFE_FILENAME_RE.sub("_", text)
    text = text.strip(". ")
    if not text:
        return "未命名计划"
    return text


def format_timestamp_for_filename(ts_str: str) -> str:
    """ISO 时间戳 → YYYYMMDD-HHMMSS（北京时间，无冒号等特殊符号） / ISO timestamp → compact Beijing time format"""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        dt = dt.astimezone(CHINA_TZ)
        return dt.strftime("%Y%m%d-%H%M%S")
    except Exception:
        return ts_str[:19].replace(":", "").replace("T", "-")


def load_plan_mapping(output_subdir: Path) -> dict:
    """加载项目 plan 映射文件 / Load per-project plan mapping {stem: {current, versions}}"""
    cache_key = str(output_subdir)
    if cache_key in _plan_mapping_cache:
        return _plan_mapping_cache[cache_key]

    plans_dir = output_subdir / "plans"
    mapping_file = plans_dir / ".plan_mapping.json"
    if not mapping_file.exists():
        _plan_mapping_cache[cache_key] = {}
        return {}

    try:
        mapping = json.loads(mapping_file.read_text())
        _plan_mapping_cache[cache_key] = mapping
        return mapping
    except (json.JSONDecodeError, OSError):
        _plan_mapping_cache[cache_key] = {}
        return {}


def save_plan_mapping(output_subdir: Path, mapping: dict) -> None:
    """持久化项目 plan 映射 / Persist per-project plan mapping"""
    cache_key = str(output_subdir)
    _plan_mapping_cache[cache_key] = mapping
    plans_dir = output_subdir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = plans_dir / ".plan_mapping.json"
    mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))


# VS Code 会话标签缓存（首次调用时构建）
_vscode_labels: dict[str, str] | None = None


def _get_vscode_workspace_path() -> Path:
    """VS Code workspaceStorage 路径，兼容 macOS/Windows / Cross-platform VS Code workspaceStorage path"""
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Code" / "User" / "workspaceStorage"
    else:
        return Path.home() / "Library/Application Support/Code/User/workspaceStorage"


def build_vscode_label_index() -> dict[str, str]:
    """扫描所有 VS Code workspace state.vscdb，构建 sessionId → label 索引"""
    index: dict[str, str] = {}
    ws_base = _get_vscode_workspace_path()
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
    """解析 JSONL 转录文件，提取对话内容 / Parse JSONL transcript, extract conversation content
    返回: (messages, session_id, first_ts, last_ts, plan_writes)"""
    messages = []
    plan_writes: list[dict] = []  # 追踪 plan 写入操作 / Track plan write operations
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

                msg = {"role": "user", "text": text, "timestamp": timestamp, "_is_user_boundary": True}
                messages.append(msg)
                last_ts = timestamp

            elif msg_type == "user" and timestamp:
                # 非 external 类型的用户消息（如审批面板输入），仅作为编辑周期边界，不展示内容
                # Non-external user messages (e.g. approval panel inputs): cycle boundaries only, not displayed
                messages.append({"_is_user_boundary": True, "timestamp": timestamp})

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

                        # 追踪 plan 写入操作（用于版本管理和逐轮引用） / Track plan writes for versioning and per-round references
                        if tool_name in ("Write", "Edit"):
                            fp = tool_input.get("file_path", "")
                            if "/.claude/plans/" in fp.replace("\\", "/"):
                                stem = Path(fp).stem
                                content = tool_input.get("content", "")
                                if content:
                                    h1 = extract_h1_from_content(content)
                                    if h1:
                                        plan_writes.append({
                                            "stem": stem,
                                            "timestamp": timestamp,
                                            "h1": h1,
                                            "content": content,
                                        })
                    # skip thinking, tool_result etc.

                text = "\n\n".join(parts)
                if not text and not tools_used:
                    continue

                msg = {"role": "assistant", "text": text, "timestamp": timestamp}
                if tools_used:
                    msg["tools"] = tools_used
                messages.append(msg)
                last_ts = timestamp

    return messages, session_id, first_ts, last_ts, plan_writes


def extract_topic(messages):
    """提取会话主题：取首条用户消息，按 East Asian Width 截断至 40 单位宽度"""
    for m in messages:
        if m.get("role") == "user":
            text = m.get("text", "")
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
    # 独立 [text] → 去掉方括号（保留 [[wikilink]]）/ standalone [text] → remove brackets (preserve [[wikilink]])
    text = re.sub(r'(?<!\[)\[([^\]]+)\](?!\])', r'\1', text)
    return text


def build_plan_versions(plan_writes: list[dict], messages: list[dict],
                        output_subdir: Path) -> dict[str, list[tuple[str, str, str]]]:
    """按用户消息边界将 plan_writes 分组为编辑周期，创建版本文件，更新 .plan_mapping.json。
    返回: {stem: [(start_ts, end_ts, version_filename), ...]} 时间线
    Group plan writes into edit cycles by user-message boundaries, create version files,
    update .plan_mapping.json. Returns timeline dict."""
    if not plan_writes:
        return {}

    # 收集所有用户消息时间戳作为周期边界 / Collect all user message timestamps as cycle boundaries
    user_ts_list: list[str] = []
    for m in messages:
        if m.get("_is_user_boundary"):
            user_ts_list.append(m["timestamp"])

    # 按 stem 分组 / Group writes by stem
    by_stem: dict[str, list[dict]] = {}
    for pw in plan_writes:
        by_stem.setdefault(pw["stem"], []).append(pw)

    # 加载现有映射 / Load existing mapping
    mapping = load_plan_mapping(output_subdir)

    timeline: dict[str, list[tuple[str, str, str]]] = {}

    for stem, writes in by_stem.items():
        writes.sort(key=lambda w: w["timestamp"])
        cycles: list[tuple[str, str, str]] = []  # [(start_ts, end_ts, version_filename)]

        current_cycle: list[dict] = []
        for w in writes:
            if current_cycle and _has_user_boundary_between(
                current_cycle[-1]["timestamp"], w["timestamp"], user_ts_list
            ):
                # 保存上一个周期 / Save previous cycle
                _save_cycle(stem, current_cycle, output_subdir, mapping)
                start_ts = current_cycle[0]["timestamp"]
                end_ts = current_cycle[-1]["timestamp"]
                version_file = mapping[stem]["current"] if stem in mapping else ""
                cycles.append((start_ts, end_ts, version_file))
                current_cycle = []
            current_cycle.append(w)

        # 最后一个周期 / Last cycle
        if current_cycle:
            _save_cycle(stem, current_cycle, output_subdir, mapping)
            start_ts = current_cycle[0]["timestamp"]
            end_ts = current_cycle[-1]["timestamp"]
            version_file = mapping[stem]["current"] if stem in mapping else ""
            cycles.append((start_ts, end_ts, version_file))

        timeline[stem] = cycles

    save_plan_mapping(output_subdir, mapping)
    return timeline


def _has_user_boundary_between(ts1: str, ts2: str, user_ts_list: list[str]) -> bool:
    """检查 ts1 和 ts2 之间是否有用户消息边界 / Check if any user message boundary exists between ts1 and ts2"""
    return any(ts1 < uts < ts2 for uts in user_ts_list)


def _save_cycle(stem: str, cycle_writes: list[dict], output_subdir: Path,
                mapping: dict) -> None:
    """保存单个编辑周期为版本文件 / Save a single edit cycle as a version file"""
    # 找最后一次 Write 操作（有完整 content） / Find the last Write operation (has full content)
    last = cycle_writes[-1]
    content = last.get("content", "")
    h1 = last.get("h1", "")

    # 如果周期内只有 Edit 没有 Write，从源文件读取当前内容 / If cycle has only Edits, read from source
    if not content:
        source = Path.home() / ".claude" / "plans" / f"{stem}.md"
        if source.exists():
            content = source.read_text(encoding="utf-8")
            h1 = extract_h1_from_content(content) or h1

    if not content or not h1:
        return

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]

    # 检查是否已存在相同 hash 的版本 / Check if version with same hash already exists
    entry = mapping.get(stem)
    if entry and isinstance(entry, dict):
        for v in entry.get("versions", []):
            if v.get("hash") == content_hash:
                # 已存在，更新 current 指向 / Already exists, update current pointer
                entry["current"] = v["name"]
                return

    # 生成文件名：{H1} {YYYYMMDD-HHMMSS}.md / Generate filename
    ts_compact = format_timestamp_for_filename(last["timestamp"])
    friendly_name = sanitize_plan_filename(h1)
    # 截断过长的文件名 / Truncate overly long filenames
    if len(friendly_name) > 80:
        friendly_name = friendly_name[:80].rsplit(" ", 1)[0].rsplit("_", 1)[0]
    filename = f"{friendly_name} {ts_compact}.md"

    # 去重（同名文件追加 _2 _3）/ Deduplicate (append _2 _3 for same-name files)
    plans_dir = output_subdir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    base = filename
    counter = 2
    while (plans_dir / filename).exists():
        stem_part = base.rsplit(".md", 1)[0]
        filename = f"{stem_part}_{counter}.md"
        counter += 1

    # 写入版本文件 / Write version file
    version_path = plans_dir / filename
    version_path.write_text(content, encoding="utf-8")

    # 更新映射 / Update mapping
    if stem not in mapping:
        mapping[stem] = {"current": None, "versions": []}
    if not isinstance(mapping[stem], dict):
        mapping[stem] = {"current": None, "versions": []}
    mapping[stem]["current"] = filename
    mapping[stem]["versions"].append({
        "name": filename,
        "hash": content_hash,
        "ts": last["timestamp"],
    })


def resolve_plan_refs_from_timeline(messages: list[dict],
                                    plan_timeline: dict[str, list[tuple[str, str, str]]]) -> None:
    """按消息时间戳匹配活跃 plan 版本，直接设置 m['plan_refs'] / Match active plan versions per message timestamp"""
    if not plan_timeline:
        return
    for m in messages:
        ts = m.get("timestamp")
        if not ts:
            continue
        refs: list[str] = []
        for stem, cycles in plan_timeline.items():
            active: str | None = None
            for start_ts, end_ts, filename in cycles:
                if start_ts <= ts:
                    active = filename
            if active:
                refs.append(f"[[plans/{active}]]")
        if refs:
            m["plan_refs"] = refs


def generate_markdown(messages, session_id, first_ts, last_ts, filepath, topic, cwd=None, label=None):
    """生成 Markdown 文档（含 frontmatter） / Generate Markdown document with frontmatter"""
    user_count = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")

    lines = []
    # YAML frontmatter
    lines.append("---")
    lines.append(f"created: {format_frontmatter_datetime(first_ts)}")
    lines.append(f"modified: {format_frontmatter_datetime(last_ts)}")
    if cwd:
        lines.append(f"cwd: '{cwd}'")
    if label:
        lines.append(f"label: {label}")
    lines.append("---")
    lines.append(f"> 时间：{format_datetime(first_ts)} ~ {format_datetime(last_ts)}")
    lines.append(f"> 轮数：用户 {user_count} 轮，Claude {assistant_count} 轮")
    lines.append(f"> 来源：`{filepath.name}`")
    if session_id:
        lines.append(f"> Session：`{session_id}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    round_num = 0
    pending_tools: list[str] = []  # 缓存连续纯工具调用 / Buffer for consecutive pure-tool messages
    pending_ts: str = ""

    def flush_pending_tools():
        """输出缓存的纯工具调用（合并为一个 <details>）/ Flush buffered pure-tool calls as a single <details>"""
        nonlocal pending_ts
        if not pending_tools:
            return
        lines.append(f"**Claude** `{pending_ts}`")
        lines.append("<details><summary>工具调用</summary>")
        for t in pending_tools:
            lines.append(f"- {escape_obsidian_tags(t)}")
        lines.append("</details>")
        pending_tools.clear()
        pending_ts = ""

    for m in messages:
        if "role" not in m:
            continue

        if m["role"] == "user":
            flush_pending_tools()
            round_num += 1
            if round_num > 1:
                lines.append("---")
            lines.append(f"# Round {round_num}")
            lines.append(f"**用户** `{format_timestamp(m['timestamp'])}`")
            text = escape_obsidian_tags(m["text"])
            text = sanitize_markdown_links(text)
            if len(text) > 2000:
                text = text[:2000] + "\n\n... (已截断)"
            lines.append(text)
            if m.get("plan_refs"):
                for ref in m["plan_refs"]:
                    lines.append(f"## 📋 {ref}")
            lines.append("")

        elif m["role"] == "assistant":
            text = escape_obsidian_tags(m.get("text", ""))
            text = sanitize_markdown_links(text)
            text = text.strip()
            has_text = bool(text)
            has_plans = bool(m.get("plan_refs"))
            has_tools = bool(m.get("tools"))

            # 纯工具调用（无文本、无计划引用）→ 缓存合并 / Pure tool calls → buffer for merging
            if not has_text and not has_plans and has_tools:
                if not pending_tools:
                    pending_ts = format_timestamp(m["timestamp"])
                pending_tools.extend(m["tools"])
                continue

            # 有意义消息 → 先 flush 缓存的工具调用 / Meaningful message → flush pending tools first
            flush_pending_tools()

            lines.append(f"**Claude** `{format_timestamp(m['timestamp'])}`")

            if has_text:
                if len(text) > 3000:
                    text = text[:3000] + "\n\n... (已截断)"
                lines.append(text)

            if has_tools:
                lines.append("<details><summary>工具调用</summary>")
                for t in m["tools"]:
                    lines.append(f"- {escape_obsidian_tags(t)}")
                lines.append("</details>")

            if has_plans:
                for ref in m["plan_refs"]:
                    lines.append(f"## 📋 {ref}")

            lines.append("")

    # 末尾可能还有未 flush 的工具调用 / Flush remaining pending tools at end
    flush_pending_tools()

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


def process_one(transcript: Path, output_subdir: Path, cwd: str | None = None) -> str | None:
    """处理单个转录文件，返回生成的文件名，无变化则返回 None"""
    output_subdir.mkdir(parents=True, exist_ok=True)

    # mtime 增量检测：先做轻量检查，避免不必要的 JSONL 解析
    existing = find_existing_output(transcript.stem, output_subdir)
    if existing and existing.stat().st_mtime >= transcript.stat().st_mtime:
        return None

    messages, session_id, first_ts, last_ts, plan_writes = parse_transcript(transcript)
    if not messages or len(messages) < 2:
        return None

    # 构建 plan 版本时间线并创建版本文件 / Build plan version timeline and create version files
    plan_timeline = build_plan_versions(plan_writes, messages, output_subdir)

    # 按消息时间戳匹配活跃 plan 版本 / Match active plan versions per message timestamp
    resolve_plan_refs_from_timeline(messages, plan_timeline)

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

    md = generate_markdown(messages, session_id, first_ts, last_ts, transcript, topic, cwd, label)
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
            # 按 mtime 降序尝试提取 cwd，避免 stub 文件 / Try newest JSONLs first to avoid stub files with no cwd
            jsonl_files = sorted(subdir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not jsonl_files:
                continue
            total_jsonl += len(jsonl_files)
            cwd = get_cwd_from_jsonl(*jsonl_files)
            folder_name = resolve_folder_name(cwd, used_names) if cwd else subdir.name
            output_subdir = OBSIDIAN_DIR / folder_name
            if cwd:
                name_to_cwd[folder_name] = cwd
            for transcript in jsonl_files:
                result = process_one(transcript, output_subdir, cwd)
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
        result = process_one(transcript, output_subdir, cwd)
        if result:
            print(f"Session saved to Obsidian: {result}", file=sys.stderr)


if __name__ == "__main__":
    main()
