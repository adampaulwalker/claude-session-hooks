#!/usr/bin/env python3
"""PostToolUse hook: Track activity, tasks, and update live progress.

Receives JSON from stdin with tool_name, tool_input, tool_output.
- Logs all activity to .claude/activity.jsonl
- Tracks task completions from TaskUpdate calls
- Updates LIVE-PROGRESS.md every 5 meaningful actions
- Can auto-update CLAUDE.md roadmap checkboxes
"""

import json
import sys
import os
import re
from datetime import datetime
from pathlib import Path

# Configuration
UPDATE_INTERVAL = 5  # Update LIVE-PROGRESS.md every N meaningful actions
MEANINGFUL_TOOLS = {"Edit", "Write", "MultiEdit", "Bash", "NotebookEdit", "TaskUpdate"}
ROADMAP_PATTERNS = [
    r"^[-*]\s*\[\s*\]\s*(.+)$",  # - [ ] task or * [ ] task
    r"^(\d+)\.\s*\[\s*\]\s*(.+)$",  # 1. [ ] task
]


def get_claude_dir() -> Path:
    """Get or create .claude directory in current working directory."""
    claude_dir = Path.cwd() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    return claude_dir


def load_state(claude_dir: Path) -> dict:
    """Load session state from file."""
    state_file = claude_dir / ".session-state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "action_count": 0,
        "completed_tasks": [],
        "session_start": datetime.now().strftime("%H:%M:%S"),
        "milestones": []
    }


def save_state(claude_dir: Path, state: dict):
    """Save session state to file."""
    state_file = claude_dir / ".session-state.json"
    state_file.write_text(json.dumps(state, indent=2))


def append_activity(claude_dir: Path, data: dict):
    """Append activity entry to jsonl log."""
    activity_file = claude_dir / "activity.jsonl"

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "time_local": datetime.now().strftime("%H:%M:%S"),
        "tool": data.get("tool_name", "unknown"),
        "session_id": os.environ.get("CLAUDE_SESSION_ID", "unknown"),
    }

    # Add context based on tool type
    tool_input = data.get("tool_input", {})
    if isinstance(tool_input, dict):
        if "file_path" in tool_input:
            entry["file"] = tool_input["file_path"]
        elif "command" in tool_input:
            cmd = tool_input["command"]
            entry["command"] = cmd[:100] + "..." if len(cmd) > 100 else cmd

        # Track task updates
        if data.get("tool_name") == "TaskUpdate":
            entry["task_id"] = tool_input.get("taskId")
            entry["task_status"] = tool_input.get("status")
            if tool_input.get("status") == "completed":
                entry["task_completed"] = True

    with open(activity_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return entry


def extract_completed_tasks(claude_dir: Path) -> list:
    """Extract completed tasks from activity log."""
    activity_file = claude_dir / "activity.jsonl"
    completed = []

    if not activity_file.exists():
        return completed

    today = datetime.now().strftime("%Y-%m-%d")

    with open(activity_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("timestamp", "").startswith(today):
                    if entry.get("task_completed"):
                        completed.append({
                            "id": entry.get("task_id"),
                            "time": entry.get("time_local")
                        })
            except json.JSONDecodeError:
                continue

    return completed


def count_activity(claude_dir: Path) -> tuple:
    """Count activity by tool type from today's log."""
    activity_file = claude_dir / "activity.jsonl"
    counts = {"edits": 0, "writes": 0, "reads": 0, "commands": 0, "tasks": 0, "other": 0}
    files_touched = set()

    if not activity_file.exists():
        return counts, files_touched

    today = datetime.now().strftime("%Y-%m-%d")

    with open(activity_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if not entry.get("timestamp", "").startswith(today):
                    continue

                tool = entry.get("tool", "")
                if tool in ("Edit", "MultiEdit"):
                    counts["edits"] += 1
                elif tool == "Write":
                    counts["writes"] += 1
                elif tool == "Read":
                    counts["reads"] += 1
                elif tool == "Bash":
                    counts["commands"] += 1
                elif tool in ("TaskUpdate", "TaskCreate"):
                    counts["tasks"] += 1
                else:
                    counts["other"] += 1

                if "file" in entry:
                    file_path = entry["file"]
                    try:
                        file_path = str(Path(file_path).relative_to(Path.cwd()))
                    except ValueError:
                        pass
                    files_touched.add(file_path)
            except json.JSONDecodeError:
                continue

    return counts, files_touched


def update_live_progress(claude_dir: Path, state: dict):
    """Update LIVE-PROGRESS.md with current session stats."""
    counts, files_touched = count_activity(claude_dir)
    completed_tasks = extract_completed_tasks(claude_dir)

    now = datetime.now()
    session_start = state.get("session_start", now.strftime("%H:%M:%S"))

    # Format files list
    files_list = "\n".join(f"- {f}" for f in sorted(files_touched)[:15]) or "- (none yet)"
    if len(files_touched) > 15:
        files_list += f"\n- ... and {len(files_touched) - 15} more"

    # Format completed tasks
    tasks_list = ""
    if completed_tasks:
        tasks_list = "\n## Completed Tasks\n"
        for task in completed_tasks[-10:]:  # Last 10
            tasks_list += f"- Task #{task['id']} at {task['time']}\n"

    # Format milestones
    milestones_list = ""
    if state.get("milestones"):
        milestones_list = "\n## Milestones\n"
        for m in state["milestones"][-5:]:
            milestones_list += f"- [{m['time']}] {m['description']}\n"

    content = f"""# Live Session Progress

*Auto-updated: {now.strftime("%H:%M:%S")} | Actions: {state['action_count']}*

## Activity (since {session_start})
| Type | Count |
|------|-------|
| Edits | {counts['edits']} |
| Writes | {counts['writes']} |
| Commands | {counts['commands']} |
| Tasks | {counts['tasks']} |
| Reads | {counts['reads']} |
{tasks_list}
{milestones_list}
## Files Touched
{files_list}

---
*Updates every {UPDATE_INTERVAL} meaningful actions. Full summary on session end.*
"""

    progress_file = claude_dir / "LIVE-PROGRESS.md"
    progress_file.write_text(content)


def try_update_roadmap(claude_dir: Path, completed_task_subject: str):
    """Try to check off matching items in CLAUDE.md roadmap."""
    claude_md = Path.cwd() / "CLAUDE.md"
    if not claude_md.exists():
        return

    try:
        content = claude_md.read_text()

        # Look for unchecked items that match the task subject
        # Convert "- [ ] Fix login bug" to "- [x] Fix login bug"
        subject_lower = completed_task_subject.lower()

        lines = content.split('\n')
        modified = False

        for i, line in enumerate(lines):
            # Check for unchecked checkbox
            match = re.match(r'^(\s*[-*]\s*)\[\s*\](\s*.+)$', line)
            if match:
                item_text = match.group(2).strip().lower()
                # Fuzzy match - if task subject contains key words from the item
                words = [w for w in item_text.split() if len(w) > 3]
                if words and any(w in subject_lower for w in words):
                    lines[i] = f"{match.group(1)}[x]{match.group(2)}"
                    modified = True

        if modified:
            claude_md.write_text('\n'.join(lines))
    except Exception:
        pass  # Don't fail on roadmap update errors


def main():
    try:
        # Read input from stdin
        input_data = json.load(sys.stdin)

        tool_name = input_data.get("tool_name", "")

        # Skip if it's a .claude/ file operation to prevent recursion
        tool_input = input_data.get("tool_input", {})
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "")
            if ".claude/" in file_path or ".claude\\" in file_path:
                print(json.dumps({}))
                return

        claude_dir = get_claude_dir()
        state = load_state(claude_dir)

        # Log the activity
        entry = append_activity(claude_dir, input_data)

        # Track task completions
        if tool_name == "TaskUpdate" and tool_input.get("status") == "completed":
            task_id = tool_input.get("taskId")
            subject = tool_input.get("subject", f"Task #{task_id}")
            state["completed_tasks"].append({
                "id": task_id,
                "subject": subject,
                "time": datetime.now().strftime("%H:%M:%S")
            })
            # Try to update CLAUDE.md roadmap
            try_update_roadmap(claude_dir, subject)

        # Count toward progress for meaningful tools
        if tool_name in MEANINGFUL_TOOLS:
            state["action_count"] += 1

            # Update live progress every N actions
            if state["action_count"] % UPDATE_INTERVAL == 0:
                update_live_progress(claude_dir, state)
                # Provide feedback that progress was updated
                save_state(claude_dir, state)
                print(json.dumps({
                    "systemMessage": f"📊 Progress updated ({state['action_count']} actions) → .claude/LIVE-PROGRESS.md"
                }))
                sys.exit(0)

        save_state(claude_dir, state)

        # Provide feedback for task completions
        if tool_name == "TaskUpdate" and tool_input.get("status") == "completed":
            print(json.dumps({
                "systemMessage": f"✅ Task #{tool_input.get('taskId')} logged as completed"
            }))
        else:
            # Silent for regular activity
            print(json.dumps({}))

    except Exception as e:
        # Never fail the hook
        print(json.dumps({"systemMessage": f"Activity tracker: {e}"}))

    sys.exit(0)


if __name__ == "__main__":
    main()
