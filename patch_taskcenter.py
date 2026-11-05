#!/usr/bin/env python3
"""让任务中心「清除已完成 / 单条 ×」在刷新页面后依然生效。

后端 ProgressStore 是纯内存 map，且只提供 GET /api/progress/:taskid、
cancel 与 /api/progress/stream（每 2 秒推一次全量任务列表），没有任何
删除接口。前端原本把被清除的任务 ID 存在 React state 的 hidden Set 里，
刷新页面 state 重建 -> 后端全量列表重新推回 -> 记录集体「复活」。

本补丁把 hidden 持久化到 localStorage（只保留最近 500 个 ID），
不改变后端行为，也不影响 SSE 实时性。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。
"""

import sys
from pathlib import Path

TARGET = Path("upstream/frontend-react/src/hooks/useTasks.jsx")

CONST_ANCHOR = """const TaskContext = createContext(null)
"""

CONST_REPLACEMENT = """const TaskContext = createContext(null)

// 被用户清除的任务 ID 持久化键（后端无删除接口，只能前端记住已清除项）
const HIDDEN_KEY = 'docker_copilot_hidden_tasks'
"""

INIT_ANCHOR = """  const [hidden, setHidden] = useState(() => new Set()) // 被用户清除的任务ID
"""

INIT_REPLACEMENT = """  // 被用户清除的任务ID：必须持久化，否则刷新页面后 SSE 会把后端仍保留的
  // 全量任务重新推回来，已清除的记录又会复活。
  const [hidden, setHidden] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]')
      return new Set(Array.isArray(saved) ? saved : [])
    } catch (e) { return new Set() }
  })

  // hidden 变化即落盘，只保留最近 500 个 ID，避免长期运行无限增长。
  useEffect(() => {
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hidden].slice(-500))) } catch (e) {}
  }, [hidden])
"""


def apply(text: str, anchor: str, replacement: str, name: str) -> str:
    count = text.count(anchor)
    if count != 1:
        print(
            f"[patch_taskcenter] 锚点 {name} 匹配到 {count} 次（期望 1 次），"
            "上游 useTasks.jsx 可能已重构，请手动核对后更新本脚本",
            file=sys.stderr,
        )
        sys.exit(1)
    return text.replace(anchor, replacement)


def main() -> int:
    if not TARGET.exists():
        print(f"[patch_taskcenter] 未找到 {TARGET}，请确认 upstream 检出路径", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = apply(text, CONST_ANCHOR, CONST_REPLACEMENT, "hidden-storage-key")
    text = apply(text, INIT_ANCHOR, INIT_REPLACEMENT, "persist-hidden-state")
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print("[patch_taskcenter] 任务中心清除记录已持久化到 localStorage，刷新不再复活")
    return 0


if __name__ == "__main__":
    sys.exit(main())
