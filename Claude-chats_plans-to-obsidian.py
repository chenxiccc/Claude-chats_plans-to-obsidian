#!/usr/bin/env python3
"""
Claude Code 会话记录 → Obsidian 自动归档
SessionEnd hook：每次会话结束时，将完整对话转为 Markdown 写入 Obsidian vault。

TRANSCRIPTS_DIR 下的每个项目子目录映射到 OBSIDIAN_DIR 下的子目录，
文件夹名由 JSONL 中的 cwd 字段派生：取最后一级项目名，冲突时逐级追加父目录。
"""

import bisect
import hashlib
import json
import os
import sqlite3
import sys
import re
import shutil
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass


@dataclass
class ParseResult:
    """parse_transcript() 返回的结构化结果 / Structured result from parse_transcript()"""
    messages: list[dict]
    session_id: str | None
    first_ts: str | None
    last_ts: str | None
    plan_writes: list[dict]
    user_boundary_indices: list[int]  # 用户消息在 messages 中的索引，供 build_plan_versions 使用 / indices of user messages for build_plan_versions


@dataclass
class ProcessResult:
    """process_one() 返回的结构化结果 / Structured result from process_one()"""
    filename: str
    is_update: bool
    skipped: bool = False


# ===== 配置 =====
OBSIDIAN_DIR = Path(os.environ.get("OBSIDIAN_DIR", Path.home() / "Obsidian" / "Project" / "claude" / "session"))
TRANSCRIPTS_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", Path.home() / ".claude" / "projects"))

DISPLAY_TZ = timezone(timedelta(hours=int(os.environ.get("DISPLAY_TZ_OFFSET", "8"))))
PLANS_SOURCE_DIR = Path.home() / ".claude" / "plans"
_CACHE_DIR = Path.home() / ".claude" / "claude_to_obsidian"


def _cache_key(output_subdir: Path) -> str:
    """用 output_subdir 的 folder_name 作为缓存键（resolve_folder_name 已保证唯一） / Use folder_name as cache key (guaranteed unique by resolve_folder_name)"""
    return output_subdir.name

# 截断与限制常量 / Truncation and limit constants
_MAX_CWD_SCAN_LINES = 100
_BASH_CMD_TRUNCATE = 120
_PLAN_TEXT_TRUNCATE = 8000
_USER_TEXT_TRUNCATE = 2000
_ASSISTANT_TEXT_TRUNCATE = 3000
_HASH_TRUNCATE_LEN = 12
_PLAN_FILENAME_MAX_LEN = 80


def find_latest_transcript() -> Path | None:
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
                    if i >= _MAX_CWD_SCAN_LINES:  # 前 100 行内必定有 user 消息 / user message always within first 100 lines
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


def _read_json_file(filepath: Path, default=None):
    """安全读取 JSON 文件，失败返回 default / Safe JSON file read, return default on failure"""
    if not filepath.exists():
        return default
    try:
        return json.loads(filepath.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_file(filepath: Path, data) -> None:
    """原子写入 JSON 文件 / Atomic JSON file write"""
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, filepath)


def load_cwd_mapping() -> dict[str, str]:
    """读取 cwd → folder_name 映射 / Load cwd → folder_name mapping"""
    # 文件存储为 {folder_name: cwd}，反转为 {cwd: folder_name} 便于查询 / File stores {folder_name: cwd}, invert for O(1) lookup
    raw = _read_json_file(_CACHE_DIR / "cwd_mapping.json", {})
    return {cwd: name for name, cwd in raw.items()}


def save_cwd_mapping(name_to_cwd: dict[str, str]) -> None:
    """全量写入 {folder_name: cwd} 映射 / Full rewrite of folder_name → cwd mapping"""
    (_CACHE_DIR / "cwd_mapping.json").parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(_CACHE_DIR / "cwd_mapping.json", name_to_cwd)


# 文件名不安全字符（跨平台交集） / Filesystem-unsafe characters (cross-platform intersection)
_UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|]')

# 标题中需移除的字符：@ 空格 中英文引号 / Characters to strip from titles: @ space quotes
_TITLE_STRIP_RE = re.compile(r'[@ \t\'‘’“”（）【】「」《》#]')

# ANSI 转义序列（终端颜色码等） / ANSI escape sequences (terminal color codes etc.)
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# 预编译正则 / Precompiled regex patterns
_SYSTEM_XML_TAG_RE = re.compile(
    r'<(system-reminder|local-command-caveat|command-name|command-message'
    r'|command-args|local-command-stdout|task-notification'
    r'|ide_opened_file|ide_selection|ide_diagnostics)>[\s\S]*?</\1>'
)
_SKIP_PREFIXES = (
    "Base directory for this skill:",
    "This session is being continued from",
    "<context-window-compacted>",
    "Human:",
    "[Request interrupted by user",
)

# 各函数共用的预编译正则，避免每次调用重复编译 / Shared precompiled regexes to avoid recompilation on every call
_RE_FENCES = re.compile(r'^ {0,3}```', re.MULTILINE)
_RE_TRAILING_FENCE = re.compile(r' {0,3}```\S+$')
_RE_OBSIDIAN_TAG = re.compile(r'(^|\W)#(\S)')
_RE_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)')
_RE_BARE_MD_LINK = re.compile(r'(?<!\[)\[([^\]]+)\](?!\])')
_RE_MARKDOWN_STYLING = re.compile(r'[*_`~>\[\]!|\\^]')  # extract_topic 用 / used by extract_topic

# plan 映射缓存（按输出目录缓存） / Plan mapping cache (keyed by output directory)
_plan_mapping_cache: dict[str, dict] = {}
# plan 哈希索引，加速版本去重查找 / Plan hash index for fast version dedup lookup
# {cache_key: {stem: {content_hash: filename}}}
_plan_hash_index: dict[str, dict[str, dict[str, str]]] = {}


def reset_plan_caches() -> None:
    """仅用于测试：清空 plan 内存缓存，防止跨测试泄漏 / Test-only: clear plan in-memory caches to avoid cross-test leakage"""
    _plan_mapping_cache.clear()
    _plan_hash_index.clear()


def extract_h1_from_content(content: str) -> str | None:
    """从 Markdown 内容中提取第一行 H1 标题 / Extract first-line H1 heading from markdown content"""
    if not content:
        return None
    first_line = content.strip().split("\n")[0].strip()
    if first_line.startswith("# "):
        return first_line[2:].strip()
    return None


def _normalize_h1(h1: str) -> str:
    """规范化 H1 标题用于比较：trim + Unicode NFC / Normalize H1 for comparison: trim + NFC"""
    if not h1:
        return ""
    return unicodedata.normalize("NFC", h1.strip())


def sanitize_plan_filename(text: str) -> str:
    """清理文件名中的不安全字符，替换为 _；移除 @ 空格 中英文引号 / Replace unsafe chars with _, strip @ space quotes"""
    text = _UNSAFE_FILENAME_RE.sub("_", text)
    text = _TITLE_STRIP_RE.sub('', text)
    text = text.strip(". ")
    if not text:
        return "未命名计划"
    return text


def _parse_ts_to_dt(ts_str: str) -> datetime | None:
    """ISO 时间戳 → 本地时区 datetime，失败返回 None / Parse ISO timestamp to local datetime, return None on failure"""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(DISPLAY_TZ)
    except (ValueError, OSError):
        return None


def format_timestamp_for_filename(ts_str: str) -> str:
    dt = _parse_ts_to_dt(ts_str)
    return dt.strftime("%Y%m%d-%H%M%S") if dt else ""


def load_plan_mapping(output_subdir: Path) -> dict[str, dict]:
    """加载项目 plan 映射缓存，同时构建哈希索引加速版本去重 / Load per-project plan mapping, build hash index for fast dedup"""
    cache_key = str(output_subdir)
    if cache_key in _plan_mapping_cache:
        return _plan_mapping_cache[cache_key]
    (_CACHE_DIR / "plan_mappings").mkdir(parents=True, exist_ok=True)
    mapping = _read_json_file(_CACHE_DIR / "plan_mappings" / f"{_cache_key(output_subdir)}.json", {})
    _plan_mapping_cache[cache_key] = mapping
    # 构建 {stem: {hash: filename}} 二级索引 / Build {stem: {hash: filename}} secondary index
    hi: dict[str, dict[str, str]] = {}
    for stem, entry in mapping.items():
        if isinstance(entry, dict):
            versions = entry.get("versions")
            if isinstance(versions, list):
                inner: dict[str, str] = {}
                for v in versions:
                    h = v.get("hash")
                    n = v.get("name")
                    if h and n:
                        inner[h] = n
                if inner:
                    hi[stem] = inner
    _plan_hash_index[cache_key] = hi
    return mapping


def save_plan_mapping(output_subdir: Path, mapping: dict) -> None:
    """持久化项目 plan 映射到本地缓存 / Persist per-project plan mapping to local cache"""
    (_CACHE_DIR / "plan_mappings").mkdir(parents=True, exist_ok=True)
    _write_json_file(_CACHE_DIR / "plan_mappings" / f"{_cache_key(output_subdir)}.json", mapping)
    cache_key = str(output_subdir)
    # 写入成功后更新缓存 / Update caches only after successful write
    _plan_mapping_cache[cache_key] = mapping
    # 重建哈希索引 / Rebuild hash index
    hi: dict[str, dict[str, str]] = {}
    for stem, entry in mapping.items():
        if isinstance(entry, dict):
            versions = entry.get("versions")
            if isinstance(versions, list):
                inner: dict[str, str] = {}
                for v in versions:
                    h = v.get("hash")
                    n = v.get("name")
                    if h and n:
                        inner[h] = n
                if inner:
                    hi[stem] = inner
    _plan_hash_index[cache_key] = hi


# VS Code 会话标签缓存（首次调用时从磁盘加载） / Cached VS Code session labels (loaded from disk on first call)
_vscode_labels: dict[str, str] | None = None


def reset_vscode_labels() -> None:
    """仅用于测试：清空 VS Code 标签缓存 / Test-only: clear VS Code labels cache"""
    global _vscode_labels
    _vscode_labels = None


def _scan_workspace_labels(ws_dir: Path, labels: dict[str, str]) -> None:
    """扫描单个 workspace 的 state.vscdb，提取 sessionId → label / Scan one workspace's state.vscdb for claude-code labels"""
    db = ws_dir / "state.vscdb"
    if not db.exists():
        return
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
                                if sid and sid not in labels:
                                    labels[sid] = label
            except json.JSONDecodeError:
                pass
    except (json.JSONDecodeError, sqlite3.Error):
        pass


def _load_vscode_labels() -> dict[str, str]:
    """从磁盘缓存加载 VS Code 标签索引，仅增量扫描新 workspace / Load label index from disk cache, incrementally scan new workspaces only"""
    vscode_cache = _CACHE_DIR / "vscode_labels_cache.json"
    cache = _read_json_file(vscode_cache, {})
    labels: dict[str, str] = cache.get("labels", {}) if isinstance(cache, dict) else {}
    indexed_dirs: set[str] = set(cache.get("indexed_dirs", [])) if isinstance(cache, dict) else set()

    ws_base = _get_vscode_workspace_path()
    if not ws_base.exists():
        return labels

    # 收集当前所有含 state.vscdb 的 workspace 目录名 / Collect all workspace dirs with state.vscdb
    current_dirs: set[str] = set()
    for wsdir in ws_base.iterdir():
        if wsdir.is_dir() and (wsdir / "state.vscdb").exists():
            current_dirs.add(wsdir.name)

    # 增量扫描新 workspace / Incremental scan for new workspaces
    new_dirs = current_dirs - indexed_dirs
    if new_dirs:
        for dirname in new_dirs:
            _scan_workspace_labels(ws_base / dirname, labels)
        # 写回磁盘 / Write back to disk
        cache_data = {"labels": labels, "indexed_dirs": sorted(current_dirs)}
        try:
            vscode_cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = vscode_cache.with_suffix(vscode_cache.suffix + ".tmp")
            tmp.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2))
            os.replace(tmp, vscode_cache)
        except OSError:
            pass  # 写入失败不影响主流程 / write failure is non-fatal

    return labels


def _get_vscode_workspace_path() -> Path:
    """VS Code workspaceStorage 路径，兼容 macOS/Windows / Cross-platform VS Code workspaceStorage path"""
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Code" / "User" / "workspaceStorage"
    else:
        return Path.home() / "Library/Application Support/Code/User/workspaceStorage"


def find_session_name(session_id: str) -> str | None:
    """从 VS Code workspace state.vscdb 查找会话标签，结果持久化缓存 / Look up session label from VS Code workspace, with persistent cache"""
    global _vscode_labels
    if not session_id:
        return None
    if _vscode_labels is None:
        _vscode_labels = _load_vscode_labels()
    return _vscode_labels.get(session_id)


def _format_ts(ts_str: str, fmt: str) -> str:
    """时间戳 → 格式化字符串 / Timestamp → formatted string"""
    dt = _parse_ts_to_dt(ts_str)
    if dt:
        return dt.strftime(fmt)
    return ts_str[:19] if ts_str else ""


def _yaml_quote(value: str) -> str:
    """YAML 单引号安全引用，内部单引号用 '' 转义 / YAML single-quote a string, escaping internal single quotes"""
    return "'" + value.replace("'", "''") + "'"


def format_frontmatter_datetime(ts_str: str) -> str:
    return _format_ts(ts_str, "%Y-%m-%dT%H:%M:%S")


def _extract_ask_question_text(tool_input: dict, parts: list[str]) -> None:
    """安全提取 AskUserQuestion 问题文本 / Safely extract AskUserQuestion question text"""
    questions = tool_input.get("questions")
    if not questions:
        return
    # 防御：questions 可能是 JSON 字符串 / Defend against JSON string format
    if isinstance(questions, str):
        try:
            questions = json.loads(questions)
        except (json.JSONDecodeError, TypeError):
            # JSON 解析失败，尝试正则提取问题文本 / Try regex extraction on malformed JSON
            m = re.search(r'"question"\s*:\s*"([^"]*)"', questions)
            if m:
                parts.append(f"**🤖 {m.group(1)}**")
            return
    # 统一为列表处理 / Normalize to list
    if isinstance(questions, dict):
        questions = [questions]
    if not isinstance(questions, list):
        return
    for q in questions:
        if isinstance(q, str):
            parts.append(f"**🤖 {q}**")
        elif isinstance(q, dict):
            question = q.get("question", "")
            if question:
                parts.append(f"**🤖 {question}**")
                opts = q.get("options", [])
                if isinstance(opts, list):
                    parts.append("\n".join(
                        f"- {o.get('label', '')}: {o.get('description', '')}"
                        for o in opts if isinstance(o, dict)
                    ))


def _sanitize_text(text: str) -> str:
    """统一清理文本中的干扰内容 / Unified text sanitization"""
    text = text.replace("\x00", "")
    text = _ANSI_ESCAPE_RE.sub('', text)
    # 闭合未关闭的代码围栏，防止后续内容被吞入代码块
    # Close unclosed code fences (CommonMark: up to 3 spaces indent, then 3+ backticks)
    fences = _RE_FENCES.findall(text)
    if len(fences) % 2 != 0:
        lines = text.split('\n')
        # 移除末尾空白行，检查最后有效行是否是被截断的不完整围栏 / Strip trailing blanks, check for truncated fence
        while lines and lines[-1] == '':
            lines.pop()
        if lines and _RE_TRAILING_FENCE.match(lines[-1]):
            # 末尾是带语言标记的未完成围栏 → 字符截断产物，直接移除避免空代码块
            # Trailing incomplete opening fence (language specifier present) → truncation artifact, remove it
            lines.pop()
            text = '\n'.join(lines)
        else:
            text = text.rstrip() + "\n```"
    return text


def _track_plan_write(tool_name: str, tool_input: dict, timestamp: str,
                     plan_writes: list[dict]) -> None:
    """如果是对 plan 文件的 Write/Edit，提取 H1 和内容追加到 plan_writes / Append to plan_writes if it's a Write/Edit to a plan file"""
    if tool_name not in ("Write", "Edit"):
        return
    fp = tool_input.get("file_path", "")
    if "/.claude/plans/" not in fp.replace("\\", "/"):
        return

    # Edit 工具没有 content 字段（只有 old_string/new_string），从磁盘读取当前文件内容
    # Edit tool has no content field — read current file from disk
    if tool_name == "Edit":
        source = Path(fp)
        if not source.exists():
            return
        try:
            content = source.read_text(encoding="utf-8")
        except OSError:
            return
    else:
        content = tool_input.get("content", "")

    if not content:
        return
    h1 = extract_h1_from_content(content)
    if not h1:
        return
    plan_writes.append({
        "stem": Path(fp).stem,
        "timestamp": timestamp,
        "h1": h1,
        "content": content,
    })


def _extract_tool_result_text(c: dict, d: dict, parts: list[str]) -> None:
    """从 tool_result 中提取用户回答文本 / Extract user answer text from tool_result"""
    # AskUserQuestion 回答：从 toolUseResult 中提取结构化问答 / Structured Q&A from AskUserQuestion
    tur = d.get("toolUseResult", {})
    if isinstance(tur, dict) and "answers" in tur:
        for q, a in tur["answers"].items():
            parts.append(f"**回答** {q}: {a}")
        return

    # 用户拒绝工具调用但在对话框里输入了文字（如 ExitPlanMode 的 "tell claude what to do instead"）
    # User rejected tool but typed feedback text
    # 注：以下英文匹配字符串依赖 Claude Code 内部消息格式，版本升级可能失效 / Note: fragile — depends on Claude Code internal message strings
    if c.get("is_error"):
        tr_text = c.get("content", "")
        if "doesn't want to proceed" in tr_text or "rejected" in tr_text:
            # 尝试从 toolUseResult 字符串中提取用户文字 / Try extracting user text from toolUseResult
            if isinstance(tur, str):
                marker = "The user provided the following reason for the rejection: "
                idx = tur.find(marker)
                if idx >= 0:
                    parts.append(tur[idx + len(marker):].strip())
                elif "User chose to stay in plan mode" in tur:
                    parts.append("[留在计划模式]")
                else:
                    parts.append("[已拒绝]")
        return

    # 其他 tool_result（Bash 输出等）不展示 / Skip other tool_results (Bash output etc.)


def parse_transcript(filepath: Path) -> ParseResult:
    """解析 JSONL 转录文件，提取对话内容 / Parse JSONL transcript, extract conversation content"""
    messages = []
    plan_writes: list[dict] = []
    user_boundary_indices: list[int] = []
    session_id = None
    first_ts = None
    last_ts = None

    with open(filepath) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue

            msg_type = d.get("type")
            timestamp = d.get("timestamp", "")

            if msg_type == "user" and d.get("userType") == "external":
                if not session_id:
                    session_id = d.get("sessionId", "")
                if not first_ts and timestamp:
                    first_ts = timestamp

                content = d.get("message", {}).get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        ct_inner = c.get("type") if isinstance(c, dict) else None
                        if ct_inner == "text":
                            parts.append(c.get("text", ""))
                        elif ct_inner == "image":
                            parts.append("[图片]")
                        elif ct_inner == "tool_result":
                            _extract_tool_result_text(c, d, parts)
                    text = "\n".join(parts)
                else:
                    text = str(content)

                text = _SYSTEM_XML_TAG_RE.sub('', text)
                text = _sanitize_text(text)
                text = text.strip()

                if not text:
                    continue

                if text == "Tool loaded." or text.startswith(_SKIP_PREFIXES):
                    continue

                msg = {"role": "user", "text": text, "timestamp": timestamp, "_is_user_boundary": True}
                user_boundary_indices.append(len(messages))
                messages.append(msg)
                last_ts = timestamp

            elif msg_type == "user" and timestamp:
                # 非 external 类型的用户消息（如审批面板输入），仅作为编辑周期边界，不展示内容
                # Non-external user messages (e.g. approval panel inputs): cycle boundaries only, not displayed
                user_boundary_indices.append(len(messages))
                messages.append({"_is_user_boundary": True, "timestamp": timestamp})

            elif msg_type == "assistant":
                content_items = d.get("message", {}).get("content", [])
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

                        # AskUserQuestion / ExitPlanMode：提取文本为可见内容，不放入工具调用折叠 / Extract as visible text, skip tools callout
                        if tool_name == "AskUserQuestion":
                            parts.append("`AskUserQuestion`")
                            _extract_ask_question_text(tool_input, parts)
                        elif tool_name == "ExitPlanMode":
                            parts.append("`ExitPlanMode`")
                            plan = tool_input.get("plan", "")
                            if plan:
                                parts.append(f"**📋 计划提案**\n{plan[:_PLAN_TEXT_TRUNCATE]}")
                        else:
                            # 简化工具调用记录 / Simplify tool call records
                            if tool_name in ("Glob", "Grep"):
                                param = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("path", "")
                                tools_used.append(f"`{tool_name}`: {param}")
                            elif tool_name == "Read":
                                fp = tool_input.get("file_path", "")
                                tools_used.append(f"`Read`: {fp}")
                            elif tool_name == "Bash":
                                cmd = tool_input.get("command", "")
                                cmd = cmd.replace("\n", " ")
                                if len(cmd) > _BASH_CMD_TRUNCATE:
                                    cmd = cmd[:_BASH_CMD_TRUNCATE] + "..."
                                tools_used.append(f"`Bash`: `{cmd}`")
                            elif tool_name in ("Edit", "Write"):
                                fp = tool_input.get("file_path", "")
                                tools_used.append(f"`{tool_name}`: {fp}")
                            elif tool_name.startswith("mcp__"):
                                short = tool_name.split("__")[-1]
                                tools_used.append(f"`{short}`")
                            else:
                                tools_used.append(f"`{tool_name}`")

                        _track_plan_write(tool_name, tool_input, timestamp, plan_writes)
                    # skip thinking, tool_result etc.

                text = "\n\n".join(parts)
                text = _sanitize_text(text)
                if not text and not tools_used:
                    continue

                msg = {"role": "assistant", "text": text, "timestamp": timestamp}
                if tools_used:
                    msg["tools"] = tools_used
                messages.append(msg)
                last_ts = timestamp

    return ParseResult(messages, session_id, first_ts, last_ts, plan_writes, user_boundary_indices)


def extract_topic(messages: list[dict]) -> str:
    """提取会话主题：取首条用户消息，按 East Asian Width 截断至 40 单位宽度"""
    for m in messages:
        if m.get("role") == "user":
            text = m.get("text", "")
            text = text.replace("\n", " ").strip()
            text = _RE_MARKDOWN_STYLING.sub('', text).strip()
            text = _UNSAFE_FILENAME_RE.sub('', text)
            text = _TITLE_STRIP_RE.sub('', text)
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


def format_timestamp(ts_str: str) -> str:
    return _format_ts(ts_str, "%H:%M:%S")


def format_datetime(ts_str: str) -> str:
    return _format_ts(ts_str, "%Y-%m-%d %H:%M:%S")


def escape_obsidian_tags(text: str) -> str:
    """转义 # 号防止 Obsidian 误识别为标签 / Escape # to prevent Obsidian tag recognition

    使用 unicodedata.category() 判断 # 后的字符是否为合法 tag 首字符 ——
    自动覆盖全部 Unicode 字母系统，避免手写 range 遗漏。
    Uses unicodedata.category() to determine if the character after # is a valid
    tag start — automatically covers all Unicode letter systems."""
    def _replacer(m: re.Match) -> str:
        prefix = m.group(1)          # 捕获 (^|\\W) / Captured prefix
        if prefix.endswith('\\'):    # 已转义为 \\#，跳过避免双反斜杠 / Already escaped, skip to avoid double-escaping
            return m.group(0)
        following = m.group(2)       # # 后的首个非空白字符 / First non-whitespace char after #
        cat = unicodedata.category(following)
        # Obsidian tag 合法首字符: Unicode 字母 (L*) / 数字 (N*) / _ / - / /
        # Valid Obsidian tag start chars: Unicode letters, numbers, underscore, hyphen, slash
        if cat.startswith('L') or cat.startswith('N') or following in '_-/':
            return prefix + '\\#' + following
        return m.group(0)            # 非 tag 首字符 → 原样保留 / Not a tag start → leave unchanged

    return _RE_OBSIDIAN_TAG.sub(_replacer, text)


def sanitize_markdown_links(text: str) -> str:
    """去掉 Markdown 链接格式避免 Obsidian 误渲染 / Strip markdown link syntax to avoid Obsidian misrendering"""
    # [text](url) → text (url) — 纯文本保留路径 / plain text with path
    text = _RE_MD_LINK.sub(r'\1 (\2)', text)
    # 独立 [text] → 去掉方括号（保留 [[wikilink]]）/ standalone [text] → remove brackets (preserve [[wikilink]])
    text = _RE_BARE_MD_LINK.sub(r'\1', text)
    return text


def build_plan_versions(plan_writes: list[dict], messages: list[dict],
                        output_subdir: Path,
                        cwd: str | None = None,
                        user_boundary_indices: list[int] | None = None) -> tuple[dict[str, list[tuple[str, str, str]]], list[str]]:
    """按用户消息边界将 plan_writes 分组为编辑周期，创建版本文件，更新 .plan_mapping.json。
    返回: (timeline, final_plans)
    Group plan writes into edit cycles by user-message boundaries, create version files,
    update .plan_mapping.json. Returns (timeline, final_plans)."""
    if not plan_writes:
        return ({}, [])

    # 收集所有用户消息时间戳作为周期边界 / Collect all user message timestamps as cycle boundaries
    user_ts_list: list[str] = []
    if user_boundary_indices is not None:
        user_ts_list = [messages[i]["timestamp"] for i in user_boundary_indices]
    else:
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
                _save_cycle(stem, current_cycle, output_subdir, mapping, cwd)
                start_ts = current_cycle[0]["timestamp"]
                end_ts = current_cycle[-1]["timestamp"]
                version_file = mapping[stem]["current"] if stem in mapping else ""
                cycles.append((start_ts, end_ts, version_file))
                current_cycle = []
            current_cycle.append(w)

        # 最后一个周期 / Last cycle
        if current_cycle:
            _save_cycle(stem, current_cycle, output_subdir, mapping, cwd)
            start_ts = current_cycle[0]["timestamp"]
            end_ts = current_cycle[-1]["timestamp"]
            version_file = mapping[stem]["current"] if stem in mapping else ""
            cycles.append((start_ts, end_ts, version_file))

        timeline[stem] = cycles

    # 统一判定 draft 状态并同步文件系统 / Finalize draft status and sync filesystem
    touched_stems = set(by_stem.keys())
    final_plans = _finalize_plan_versions(mapping, output_subdir, touched_stems)

    save_plan_mapping(output_subdir, mapping)
    return (timeline, final_plans)


def _has_user_boundary_between(ts1: str, ts2: str, user_ts_list: list[str]) -> bool:
    """检查 ts1 和 ts2 之间是否有用户消息边界 / Check if any user message boundary exists between ts1 and ts2"""
    idx = bisect.bisect_right(user_ts_list, ts1)
    return idx < len(user_ts_list) and user_ts_list[idx] < ts2


def _deduplicate_filename(directory: Path, base_name: str, skip: Path | None = None,
                         seen: set[str] | None = None) -> str:
    """去重文件名，同名追加 _2 _3 / Deduplicate filename by appending _2 _3

    seen: 已用文件名的内存集合，传入时优先在内存中检查，避免文件系统 stat 调用。
          Memory set of used filenames; when provided, checks memory first to avoid stat syscalls."""
    filename = base_name
    counter = 2
    while True:
        candidate = directory / filename
        exists = filename in seen if seen is not None else candidate.exists()
        if (not exists or candidate == skip):
            break
        stem = base_name.rsplit(".", 1)[0]
        ext = f".{base_name.rsplit('.', 1)[1]}" if "." in base_name else ""
        filename = f"{stem}_{counter}{ext}"
        counter += 1
    if seen is not None:
        seen.add(filename)
    return filename


def _save_cycle(stem: str, cycle_writes: list[dict], output_subdir: Path,
                mapping: dict, cwd: str | None = None) -> None:
    """保存单个编辑周期为版本文件 / Save a single edit cycle as a version file"""
    last = cycle_writes[-1]
    content = last.get("content", "")
    h1 = last.get("h1", "")
    first_ts = cycle_writes[0]["timestamp"]
    last_ts = last["timestamp"]

    # 防御：如果 entry 无 content（如只读磁盘失败），从源文件回退读取 / Defensive: fallback to source file if no content
    if not content:
        source = PLANS_SOURCE_DIR / f"{stem}.md"
        if source.exists():
            content = source.read_text(encoding="utf-8")
            h1 = extract_h1_from_content(content) or h1

    if not content or not h1:
        return

    # Obsidian 输出清理：转义 #tag 和去除 [text](url) 链接格式 / Apply Obsidian-safe escaping
    content = escape_obsidian_tags(content)
    content = sanitize_markdown_links(content)

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:_HASH_TRUNCATE_LEN]

    # 哈希索引 O(1) 查找，避免遍历版本列表 / Hash index O(1) lookup, avoid scanning version list
    hi = _plan_hash_index.get(str(output_subdir))
    if hi and stem in hi and content_hash in hi[stem]:
        existing_name = hi[stem][content_hash]
        entry = mapping.get(stem)
        if not entry or not isinstance(entry, dict):
            mapping[stem] = {"current": None, "versions": []}
        mapping[stem]["current"] = existing_name
        return

    # Fallback：缓存丢失时从磁盘 plan 文件反向匹配 / Scan disk for existing plan version with same stem+hash
    plans_dir = output_subdir / "plans"
    if plans_dir.exists():
        ref_marker = f"ref_plan_file: {stem}.md"
        for f in plans_dir.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
                if ref_marker not in text:
                    continue
                file_hash = hashlib.sha256(text.encode()).hexdigest()[:_HASH_TRUNCATE_LEN]
                if file_hash == content_hash:
                    # 找到相同内容的已有版本，复用 / Found existing version, reuse it
                    if stem not in mapping or not isinstance(mapping.get(stem), dict):
                        mapping[stem] = {"current": None, "versions": []}
                    mapping[stem]["current"] = f.name
                    mapping[stem]["versions"].append({
                        "name": f.name, "hash": content_hash,
                        "ts": last["timestamp"], "h1": h1,
                    })
                    # 更新哈希索引 / Update hash index
                    _plan_hash_index.setdefault(str(output_subdir), {}).setdefault(stem, {})[content_hash] = f.name
                    return
            except OSError:
                continue

    ts_compact = format_timestamp_for_filename(last["timestamp"])
    friendly_name = sanitize_plan_filename(h1)
    if len(friendly_name) > _PLAN_FILENAME_MAX_LEN:
        friendly_name = friendly_name[:_PLAN_FILENAME_MAX_LEN].rsplit(" ", 1)[0].rsplit("_", 1)[0]
    filename = f"{friendly_name} {ts_compact}.md"

    # 去重（同名文件追加 _2 _3）/ Deduplicate (append _2 _3 for same-name files)
    plans_dir = output_subdir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    filename = _deduplicate_filename(plans_dir, filename)

    fm_lines = ["---"]
    fm_lines.append(f"created: {format_frontmatter_datetime(first_ts)}")
    fm_lines.append(f"modified: {format_frontmatter_datetime(last_ts)}")
    if cwd:
        fm_lines.append(f"cwd: {_yaml_quote(cwd)}")
    fm_lines.append(f"ref_plan_file: {stem}.md")
    fm_lines.append("---")
    fm_lines.append("")
    content = "\n".join(fm_lines) + content

    version_path = plans_dir / filename
    version_path.write_text(content, encoding="utf-8")

    if stem not in mapping or not isinstance(mapping.get(stem), dict):
        mapping[stem] = {"current": None, "versions": []}
    mapping[stem]["current"] = filename
    mapping[stem]["versions"].append({
        "name": filename,
        "hash": content_hash,
        "ts": last["timestamp"],
        "h1": h1,
    })
    # 更新哈希索引 / Update hash index
    _plan_hash_index.setdefault(str(output_subdir), {}).setdefault(stem, {})[content_hash] = filename


def _finalize_plan_versions(mapping: dict, output_subdir: Path,
                            touched_stems: set[str]) -> list[str]:
    """按 stem+H1 分组，标记 draft 状态，移动文件，返回 final_plans 列表。
    直接修改 mapping dict，调用方负责持久化。
    Group versions by stem+H1, mark draft status, move files, return final_plans list.
    Mutates mapping dict in place; caller is responsible for persisting."""
    plans_dir = output_subdir / "plans"
    draft_dir = plans_dir / "draft_plans"

    # 步骤 4a：计算 draft 状态 / Compute draft status
    for stem, entry in mapping.items():
        versions = entry.get("versions")
        if not isinstance(versions, list) or not versions:
            continue

        # 按归一化 H1 分组 / Group by normalized H1
        by_h1: dict[str, list[dict]] = {}
        for v in versions:
            h1_key = _normalize_h1(v.get("h1", ""))
            # 空 H1 每个版本独立分组，避免被误判为同一计划 / Empty H1: each version is its own group
            if h1_key == "":
                v["draft"] = False
                continue
            by_h1.setdefault(h1_key, []).append(v)

        # 每组内最新为非 draft，其余为 draft / Latest in each group is non-draft, rest are draft
        for h1_key, group in by_h1.items():
            group.sort(key=lambda v: v.get("ts", ""))
            for v in group[:-1]:
                v["draft"] = True
            group[-1]["draft"] = False

    # 步骤 4b：同步文件系统（仅降级）/ Sync filesystem (demotion only)
    for entry in mapping.values():
        for v in entry.get("versions", []):
            if v.get("draft", False):
                src = plans_dir / v["name"]
                if src.exists():
                    draft_dir.mkdir(parents=True, exist_ok=True)
                    new_name = _deduplicate_filename(draft_dir, v["name"])
                    shutil.move(str(src), str(draft_dir / new_name))
                    if new_name != v["name"]:
                        v["name"] = new_name

    # 步骤 4c：收集 final_plans（仅 touched stems）/ Collect final_plans (touched stems only)
    final_plans: list[str] = []
    for stem in touched_stems:
        entry = mapping.get(stem)
        if entry:
            for v in entry.get("versions", []):
                if not v.get("draft", False):
                    final_plans.append(v["name"])
    final_plans.sort()
    return final_plans


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
                if start_ts <= ts <= end_ts:
                    active = filename
                    break
            if active:
                refs.append(f"[[{active}]]")
        if refs:
            m["plan_refs"] = refs


def _truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    text = text[:max_len] + "\n\n... (已截断)"
    return _sanitize_text(text)


def _build_frontmatter(messages: list[dict], session_id: str | None,
                       first_ts: str | None, last_ts: str | None,
                       filepath: Path, cwd: str | None = None,
                       label: str | None = None,
                       final_plans: list[str] | None = None) -> list[str]:
    """构建会话笔记 YAML frontmatter + 元数据头 / Build YAML frontmatter + metadata header"""
    user_count = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")

    lines = []
    # YAML frontmatter
    lines.append("---")
    lines.append(f"created: {format_frontmatter_datetime(first_ts)}")
    lines.append(f"modified: {format_frontmatter_datetime(last_ts)}")
    if cwd:
        lines.append(f"cwd: {_yaml_quote(cwd)}")
    if label:
        lines.append(f"label: {_yaml_quote(label)}")
    if final_plans:
        lines.append("final_plans:")
        for fn in final_plans:
            lines.append(f"  - {_yaml_quote(f'[[{fn}]]')}")
    lines.append("---")
    lines.append(f"> 时间：{format_datetime(first_ts)} ~ {format_datetime(last_ts)}")
    lines.append(f"> 轮数：用户 {user_count} 轮，Claude {assistant_count} 轮")
    lines.append(f"> 来源：`{filepath.name}`")
    if session_id:
        lines.append(f"> Session：`{session_id}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def generate_markdown(messages: list[dict], session_id: str | None, first_ts: str | None, last_ts: str | None,
                     filepath: Path, topic: str, cwd: str | None = None, label: str | None = None,
                     final_plans: list[str] | None = None) -> str:
    lines = _build_frontmatter(messages, session_id, first_ts, last_ts, filepath,
                               cwd=cwd, label=label, final_plans=final_plans)

    round_num = 0
    pending_tools: list[str] = []  # 缓存连续纯工具调用 / Buffer for consecutive pure-tool messages
    pending_ts: str = ""

    def _render_tools(tools: list[str]):
        lines.append("> [!note]- 工具调用")
        for t in tools:
            lines.append(f"> - {escape_obsidian_tags(t).replace('\n', ' ')}")
        lines.append("")

    def flush_pending_tools():
        nonlocal pending_ts
        if not pending_tools:
            return
        lines.append(f"**Claude** `{pending_ts}`")
        _render_tools(pending_tools)
        pending_tools.clear()
        pending_ts = ""

    for m in messages:
        if "role" not in m:
            continue

        if m["role"] == "user":
            flush_pending_tools()
            round_num += 1
            lines.append(f"# Round {round_num}")
            lines.append(f"**用户** `{format_timestamp(m['timestamp'])}`")
            text = escape_obsidian_tags(m["text"])
            text = sanitize_markdown_links(text)
            text = _truncate_text(text, _USER_TEXT_TRUNCATE)
            lines.append(text)
            if m.get("plan_refs"):
                lines.append("")
                lines.extend(f"## 📋 {ref}" for ref in m["plan_refs"])
            lines.append("")

        elif m["role"] == "assistant":
            text = escape_obsidian_tags(m.get("text", ""))
            text = sanitize_markdown_links(text)
            text = text.strip()
            has_text = bool(text)
            has_plans = bool(m.get("plan_refs"))
            has_tools = bool(m.get("tools"))

            if not has_text and not has_plans and has_tools:
                if not pending_tools:
                    pending_ts = format_timestamp(m["timestamp"])
                pending_tools.extend(m["tools"])
                continue

            # 有意义消息 → 先 flush 缓存的工具调用 / Meaningful message → flush pending tools first
            flush_pending_tools()

            lines.append(f"**Claude** `{format_timestamp(m['timestamp'])}`")

            if has_tools:
                _render_tools(m["tools"])

            if has_text:
                lines.append(_truncate_text(text, _ASSISTANT_TEXT_TRUNCATE))

            if has_plans:
                lines.append("")
                lines.extend(f"## 📋 {ref}" for ref in m["plan_refs"])

            lines.append("")

    # 末尾可能还有未 flush 的工具调用 / Flush remaining pending tools at end
    flush_pending_tools()

    return "\n".join(lines)


def find_existing_output(stem: str, output_subdir: Path,
                       mapping: dict[str, str] | None = None) -> Path | None:
    """根据 JSONL stem 找到已存在的输出文件，不存在则返回 None / Find existing output file by JSONL stem, with disk fallback

    当 mapping 参数传入时使用内存 dict（--scan-all 批量模式），避免重复磁盘 I/O。
    When mapping is provided, use in-memory dict to avoid repeated disk I/O (--scan-all batch mode)."""
    if mapping is None:
        mapping_file = _CACHE_DIR / "session_mappings" / f"{_cache_key(output_subdir)}.json"
        mapping = _read_json_file(mapping_file, {})
    entry = mapping.get(stem)
    session_marker = f"> Session：`{stem}`"

    if entry is not None and isinstance(entry, str):
        p = output_subdir / entry
        if p.exists():
            # 验证文件确实属于该 stem，防止 mapping 过期指针 / Verify file belongs to this stem
            try:
                with open(p, encoding="utf-8") as fh:
                    head = "".join(fh.readline() for _ in range(20))
                if session_marker in head:
                    return p
                # 不匹配：mapping 过期，fall through 到磁盘扫描 / Mismatch: stale mapping, fall through
            except OSError:
                pass

    # Fallback：mapping 丢失时从磁盘 .md 文件反向匹配 session ID / Scan .md files for matching session ID
    for f in output_subdir.glob("*.md"):
        try:
            with open(f, encoding="utf-8") as fh:
                head = "".join(fh.readline() for _ in range(20))
            if session_marker in head:
                # 找到匹配文件，补登记 mapping / Found match, update mapping
                mapping[stem] = f.name
                return f
        except OSError:
            continue
    return None


def save_mapping(stem: str, filename: str, output_subdir: Path,
                mapping: dict[str, str] | None = None) -> None:
    """记录 JSONL stem → filename 的映射到本地缓存 / Save stem→filename mapping to local cache

    当 mapping 参数传入时仅更新内存 dict，调用方负责持久化。
    When mapping is provided, only update in-memory dict; caller handles persistence."""
    if mapping is not None:
        mapping[stem] = filename
        return
    (_CACHE_DIR / "session_mappings").mkdir(parents=True, exist_ok=True)
    mapping_file = _CACHE_DIR / "session_mappings" / f"{_cache_key(output_subdir)}.json"
    disk_mapping = _read_json_file(mapping_file, {})
    disk_mapping[stem] = filename
    _write_json_file(mapping_file, disk_mapping)


def _flush_session_mapping(output_subdir: Path, mapping: dict[str, str]) -> None:
    """持久化 session 映射到磁盘 / Persist session mapping to disk"""
    (_CACHE_DIR / "session_mappings").mkdir(parents=True, exist_ok=True)
    mapping_file = _CACHE_DIR / "session_mappings" / f"{_cache_key(output_subdir)}.json"
    _write_json_file(mapping_file, mapping)


def process_one(transcript: Path, output_subdir: Path, cwd: str | None = None,
                session_mapping: dict[str, str] | None = None) -> ProcessResult | None:
    """处理单个转录文件，返回 ProcessResult，无变化则返回 None
    session_mapping: --scan-all 批量模式下传入内存 dict，避免重复磁盘 I/O / in-memory dict for --scan-all to skip repeated disk I/O"""
    output_subdir.mkdir(parents=True, exist_ok=True)

    # mtime 增量检测：先做轻量检查，避免不必要的 JSONL 解析
    existing = find_existing_output(transcript.stem, output_subdir, mapping=session_mapping)
    if existing and existing.stat().st_mtime >= transcript.stat().st_mtime:
        return ProcessResult(filename="", is_update=False, skipped=True)

    r = parse_transcript(transcript)
    if not r.messages or len(r.messages) < 2:
        return None

    # 构建 plan 版本时间线并创建版本文件 / Build plan version timeline and create version files
    plan_timeline, final_plans = build_plan_versions(r.plan_writes, r.messages, output_subdir, cwd,
                                                    user_boundary_indices=r.user_boundary_indices)

    # 按消息时间戳匹配活跃 plan 版本 / Match active plan versions per message timestamp
    resolve_plan_refs_from_timeline(r.messages, plan_timeline)

    topic = extract_topic(r.messages)

    # 生成文件名（纯主题，同名自动加 _2 _3 去重）
    base_filename = f"{topic}.md"
    filename = _deduplicate_filename(output_subdir, base_filename, existing)

    output_path = output_subdir / filename

    # 如果旧文件还在且文件名不同，删掉旧的
    if existing and existing != output_path:
        existing.unlink(missing_ok=True)

    # 获取 VS Code 标签（仅写入 frontmatter 元数据，不做标题）
    label = find_session_name(r.session_id)

    md = generate_markdown(r.messages, r.session_id, r.first_ts, r.last_ts, transcript, topic, cwd, label,
                           final_plans=final_plans)
    output_path.write_text(md, encoding="utf-8")
    save_mapping(transcript.stem, filename, output_subdir, mapping=session_mapping)

    is_update = existing is not None
    return ProcessResult(filename=filename, is_update=is_update)


def _run_scan_all() -> None:
    """扫描所有项目子目录的所有 JSONL 文件 / Scan all JSONL files in all project subdirectories"""
    processed = 0
    total_jsonl = 0
    used_names: set[str] = set()
    name_to_cwd: dict[str, str] = {}  # {folder_name: cwd} / folder name → cwd mapping
    # 批量映射：每个 output_subdir 一个内存 dict，循环结束后统一持久化 / Batch mappings: one in-memory dict per output_subdir, flushed after loop
    batch_mappings: dict[Path, dict[str, str]] = {}
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
        # 为该 output_subdir 加载或复用内存 mapping / Load or reuse in-memory mapping for this output_subdir
        sm = batch_mappings.get(output_subdir)
        if sm is None:
            sm = _read_json_file(
                _CACHE_DIR / "session_mappings" / f"{_cache_key(output_subdir)}.json", {})
            batch_mappings[output_subdir] = sm
        for transcript in jsonl_files:
            result = process_one(transcript, output_subdir, cwd, session_mapping=sm)
            if result and not result.skipped:
                tag = "(更新)" if result.is_update else ""
                print(f"  归档: [{output_subdir.name}] {result.filename} {tag}", file=sys.stderr)
                processed += 1
    # 批量持久化 session mappings / Batch flush session mappings
    for output_subdir, sm in batch_mappings.items():
        _flush_session_mapping(output_subdir, sm)
    # 持久化 cwd → folder_name 映射，供增量 SessionEnd 查询 / Persist cwd → folder_name mapping for SessionEnd to query
    save_cwd_mapping(name_to_cwd)
    print(f"扫描完成: {total_jsonl} 个会话, 新归档 {processed} 个", file=sys.stderr)


def _run_session_end() -> None:
    """处理最新的一个转录文件（跨所有子目录找最近修改的 JSONL）/ Process only the latest transcript"""
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
    if result and not result.skipped:
        tag = "(更新)" if result.is_update else ""
        print(f"Session saved to Obsidian: [{output_subdir.name}] {result.filename} {tag}", file=sys.stderr)


def main() -> None:
    """CLI 入口：根据 --scan-all 标志分发 / CLI entry: dispatch by --scan-all flag"""
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    if "--scan-all" in sys.argv:
        _run_scan_all()
    else:
        _run_session_end()


if __name__ == "__main__":
    main()
