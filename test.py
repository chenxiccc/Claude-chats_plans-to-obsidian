#!/usr/bin/env python3
"""测试 Claude-chats_plans-to-obsidian.py"""
import json, os, sys, tempfile, shutil, subprocess
from pathlib import Path

TEST_SESSION_ID = "test-session-001"
TEST_CWD = "/test/project"
TEST_STEM = "test-plan-steady-quail"

# 构造测试 JSONL / Build test JSONL
TEST_MESSAGES = [
    {"type":"user","userType":"external","message":{"content":[{"type":"text","text":"帮我修复登录页的bug"}]},"timestamp":"2026-05-21T10:00:00Z","sessionId":TEST_SESSION_ID,"cwd":TEST_CWD},
    # heredoc 在 Bash 命令中
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git commit -m \"$(cat <<'EOF'\nfeat: new feature\nEOF\n)\""}}]},"timestamp":"2026-05-21T10:00:01Z"},
    # AskUserQuestion 工具调用
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"AskUserQuestion","input":{"questions":[{"question":"倾向哪种方案？","options":[{"label":"方案A","description":"描述A"},{"label":"方案B","description":"描述B"}]}]}}]},"timestamp":"2026-05-21T10:00:02Z"},
    # 用户回答 AskUserQuestion（toolUseResult.answers）
    {"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"User has answered your questions: \"倾向哪种方案？\"=\"方案A\".","tool_use_id":"call_test"}]},"toolUseResult":{"questions":[{"question":"倾向哪种方案？"}],"answers":{"倾向哪种方案？":"方案A"}},"timestamp":"2026-05-21T10:00:03Z"},
    # ExitPlanMode 工具调用
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"ExitPlanMode","input":{"plan":"# 计划：测试\n## Context\n测试内容"}}]},"timestamp":"2026-05-21T10:00:04Z"},
    # 用户拒绝 ExitPlanMode 并输入文字
    {"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"The user doesn't want to proceed with this tool use. The tool use was rejected...","is_error":True}]},"toolUseResult":"Error: The user doesn't want to proceed with this tool use. The tool use was rejected. The user provided the following reason for the rejection: 修改方案A的细节","timestamp":"2026-05-21T10:00:05Z"},
    # Write plan 文件
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":f"/Users/admin/.claude/plans/{TEST_STEM}.md","content":"# 测试计划：修复登录页\n\n## Context\n\n登录页有bug需要修复"}}]},"timestamp":"2026-05-21T10:00:06Z"},
    # 用户回复（编辑周期边界）
    {"type":"user","userType":"external","message":{"content":[{"type":"text","text":"好的，按计划执行"}]},"timestamp":"2026-05-21T10:01:00Z"},
    # 对话文本含 <details> 标签（应原样显示，不干扰 callout）
    {"type":"assistant","message":{"content":[{"type":"text","text":"输出格式如下：\n\n<details><summary>工具调用</summary>\n\n- `Read`: file.ts\n\n</details>\n\n注意空格"}]},"timestamp":"2026-05-21T10:01:01Z"},
    # 对话文本含未闭合的 ```json（sanitize 应自动补全）
    {"type":"assistant","message":{"content":[{"type":"text","text":"映射结构：\n\n```json\n{\n  \"stem\": {\n    \"current\": \"修复登录页bug 20260521-100000.md\"\n  }\n}"}]},"timestamp":"2026-05-21T10:01:02Z"},
    # 模拟截断场景：末尾行是缩进+不完整的围栏（`  ```pyth`），sanitize 应移除 / Simulated truncation: trailing indented incomplete fence should be removed
    {"type":"assistant","message":{"content":[{"type":"text","text":"- **修改**: 新增常量：\n  ```pyth"}]},"timestamp":"2026-05-21T10:01:03Z"},
    # 用户消息含 wikilink 和 #tag
    {"type":"user","userType":"external","message":{"content":[{"type":"text","text":"参考 [[某笔记]] 和 #tag1 的做法"}]},"timestamp":"2026-05-21T10:02:00Z"},
    # Bash tool_result（不应显示为用户消息内容）
    {"type":"user","userType":"external","message":{"content":[{"type":"tool_result","content":"total 168\ndrwxr-xr-x 14 admin staff 448 May 12 11:56 ."}]},"timestamp":"2026-05-21T10:02:01Z"},
    # 重写同一 plan 文件（新编辑周期，应生成新的 plan 版本）
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":f"/Users/admin/.claude/plans/{TEST_STEM}.md","content":"# 新增用户权限管理\n\n## Context\n\n需要加权限系统"}}]},"timestamp":"2026-05-21T10:03:00Z"},
]

failed = 0
def check(description, condition):
    global failed
    if condition:
        print(f"  ✅ {description}")
    else:
        print(f"  ❌ {description}")
        failed += 1

# ===== 设置测试环境 =====
test_dir = Path(tempfile.mkdtemp())
transcripts_dir = test_dir / "transcripts" / f"-{TEST_CWD.replace('/', '-')}"
transcripts_dir.mkdir(parents=True)
obsidian_dir = test_dir / "obsidian-session"

# 写入测试 JSONL
jsonl_path = transcripts_dir / f"{TEST_SESSION_ID}.jsonl"
with open(jsonl_path, 'w') as f:
    for msg in TEST_MESSAGES:
        f.write(json.dumps(msg, ensure_ascii=False) + '\n')

# 创建 plan 源文件 / Create plan source file
plans_source = Path.home() / ".claude" / "plans"
plans_source.mkdir(parents=True, exist_ok=True)
plan_path = plans_source / f"{TEST_STEM}.md"
plan_path.write_text("# 测试计划：修复登录页\n\n## Context\n\n登录页有bug需要修复", encoding="utf-8")

# 通过环境变量覆盖配置，运行原始脚本 / Override config via env vars
script = Path(__file__).parent / "Claude-chats_plans-to-obsidian.py"
env = os.environ.copy()
env["OBSIDIAN_DIR"] = str(obsidian_dir)
env["TRANSCRIPTS_DIR"] = str(test_dir / "transcripts")
result = subprocess.run([sys.executable, str(script), "--scan-all"],
                       capture_output=True, text=True, timeout=30, env=env)
if result.returncode != 0:
    print(f"SCRIPT ERROR: {result.stderr}")
    sys.exit(1)

# 定位输出文件 / Locate output files
output_files = list(obsidian_dir.rglob("*.md"))
session_files = [f for f in output_files if f.parent.name != "plans"]
plan_files = [f for f in output_files if f.parent.name == "plans"]

if not session_files:
    print("❌ 没有生成 session 文件")
    sys.exit(1)

output = session_files[0].read_text(encoding="utf-8")
plans_dir = next((d for d in obsidian_dir.rglob("plans") if d.is_dir()), None)

# ===== 格式破坏 =====
print("\n=== 格式破坏 ===")
check("使用 Obsidian callout 替代 <details>", "> [!note]- 工具调用" in output)
check("Bash heredoc 无换行断裂", output.count("\n> - `Bash`") <= output.count("> [!note]- 工具调用"))
check("ANSI 转义码已清除", "[0;36m" not in output)
check("代码围栏平衡", output.count("\n```") % 2 == 0)
check("截断不完整围栏已移除", "```pyth" not in output)
check("Null bytes 已清除", "\x00" not in output)

# ===== 内容提取 =====
print("\n=== 内容提取 ===")
check("AskUserQuestion 问题可见", "**🤖 倾向哪种方案？**" in output)
check("AskUserQuestion 回答可见", "**回答** 倾向哪种方案？: 方案A" in output)
check("ExitPlanMode 提案可见", "**📋 计划提案**" in output)
check("ExitPlanMode 可见文本标签", "`ExitPlanMode`" in output)
check("AskUserQuestion 可见文本标签", "`AskUserQuestion`" in output)
check("拒绝时用户文字提取", "修改方案A的细节" in output)
check("Bash 输出不显示为用户消息", "total 168" not in output)

# ===== Obsidian 渲染 =====
print("\n=== Obsidian 渲染 ===")
check("无 --- 分隔线（非 frontmatter）", output.count("\n---\n") <= 3)
check("# Round N 前无多余空行", "\n\n\n# Round" not in output)

# ===== 文本清理 =====
print("\n=== 文本清理 ===")
check("wikilink 保留", "[[某笔记]]" in output)
check("#tag 被转义", "\\#tag1" in output)

# ===== Plan 版本管理 =====
print("\n=== Plan 版本管理 ===")
plan_mapping_file = plans_dir / ".plan_mapping.json" if plans_dir else None
check(".plan_mapping.json 存在", plan_mapping_file and plan_mapping_file.exists())
if plan_mapping_file and plan_mapping_file.exists():
    mapping = json.loads(plan_mapping_file.read_text())
    stem_entry = mapping.get(TEST_STEM, {})
    versions = stem_entry.get("versions", [])
    check("同 stem 两次写入生成 2 个版本", len(versions) == 2)
    if len(versions) >= 2:
        check("版本 1 用户名含'修复登录页'", "修复登录页" in versions[0]["name"])
        check("版本 2 用户名含'新增用户权限管理'", "新增用户权限管理" in versions[1]["name"])

# ===== Frontmatter =====
print("\n=== Frontmatter ===")
check("Session created 字段", "created:" in output.split("---\n")[1])
check("Session modified 字段", "modified:" in output.split("---\n")[1])
if plan_files:
    plan_content = plan_files[0].read_text(encoding="utf-8")
    check("Plan created 字段", "created:" in plan_content.split("---\n")[1])
    check("Plan ref_plan_file 字段", "ref_plan_file:" in plan_content.split("---\n")[1])

# 清理 / Cleanup
shutil.rmtree(test_dir)
plan_path.unlink(missing_ok=True)

print(f"\n{'='*40}")
if failed:
    print(f"失败: {failed} 项")
    sys.exit(1)
else:
    print("全部通过!")
