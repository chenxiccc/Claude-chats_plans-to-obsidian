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
    # 再次修改同一 plan，H1 与第一个版本相同（应触发 draft 逻辑） / Edit same plan with same H1 as first version (should trigger draft logic)
    {"type":"user","userType":"external","message":{"content":[{"type":"text","text":"继续修改"}]},"timestamp":"2026-05-21T10:04:00Z"},
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":f"/Users/admin/.claude/plans/{TEST_STEM}.md","content":"# 测试计划：修复登录页\n\n## Context\n\n改进后的修复方案"}}]},"timestamp":"2026-05-21T10:05:00Z"},
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
draft_dir = plans_dir / "draft_plans" if plans_dir else None

if plan_mapping_file and plan_mapping_file.exists():
    mapping = json.loads(plan_mapping_file.read_text())
    stem_entry = mapping.get(TEST_STEM, {})
    versions = stem_entry.get("versions", [])
    check("同 stem 三次写入生成 3 个版本", len(versions) == 3)
    # 检查 h1 和 draft 字段 / Check h1 and draft fields
    h1s = [v.get("h1", "") for v in versions]
    check("版本条目包含 h1 字段", all(v.get("h1") is not None for v in versions))
    check("版本条目包含 draft 字段", all("draft" in v for v in versions))
    # 同 H1 组：第一个"测试计划：修复登录页"应为 draft，最新为非 draft / Same H1 group: first should be draft, latest non-draft
    login_versions = [v for v in versions if "修复登录页" in v.get("h1", "")]
    check("两个'修复登录页'版本中一个 draft 一个非 draft",
          sum(1 for v in login_versions if v.get("draft")) == 1 and
          sum(1 for v in login_versions if not v.get("draft")) == 1)
    # 不同 H1：应独立为非 draft / Different H1: should be independent non-draft
    perm_versions = [v for v in versions if "新增用户权限管理" in v.get("h1", "")]
    check("'新增用户权限管理'版本为非 draft",
          len(perm_versions) == 1 and not perm_versions[0].get("draft"))
    # 文件位置 / File locations
    check("draft_plans 目录存在", draft_dir and draft_dir.exists())
    if draft_dir and draft_dir.exists():
        draft_files = list(draft_dir.glob("*.md"))
        check("draft_plans 中有 1 个文件", len(draft_files) == 1)
        if draft_files:
            check("draft 文件名含'修复登录页'", "修复登录页" in draft_files[0].name)
    # plans 根目录应有 2 个文件（不同 H1 的最新版） / plans root should have 2 files (latest of each H1)
    root_plans = [f for f in plans_dir.glob("*.md")]
    check("plans 根目录有 2 个文件", len(root_plans) == 2)

# ===== Frontmatter =====
print("\n=== Frontmatter ===")
check("Session created 字段", "created:" in output.split("---\n")[1])
check("Session modified 字段", "modified:" in output.split("---\n")[1])
check("Session final_plans 字段", "final_plans:" in output.split("---\n")[1])
if plan_files:
    plan_content = plan_files[0].read_text(encoding="utf-8")
    check("Plan created 字段", "created:" in plan_content.split("---\n")[1])
    check("Plan ref_plan_file 字段", "ref_plan_file:" in plan_content.split("---\n")[1])

# ===== final_plans 前端元数据 =====
print("\n=== final_plans ===")
# final_plans 应包含 2 个最终版 wikilink：不同 H1 各一个 / Should have 2 final wikilinks: one per distinct H1
check("final_plans 含 2 个条目", output.count("  - '[[") == 2)
check("final_plans 含'修复登录页'", "修复登录页" in output.split("final_plans:")[1].split("---")[0])
check("final_plans 含'新增用户权限管理'", "新增用户权限管理" in output.split("final_plans:")[1].split("---")[0])
# wikilink 格式为 [[filename.md]] 无路径 / Wikilink format [[filename.md]] without path
check("plan_refs 使用 [[filename]] 无路径格式", "[[plans/" not in output and "[[" in output)

# 清理 / Cleanup
shutil.rmtree(test_dir)
plan_path.unlink(missing_ok=True)

print(f"\n{'='*40}")
if failed:
    print(f"失败: {failed} 项")
    sys.exit(1)
else:
    print("全部通过!")
