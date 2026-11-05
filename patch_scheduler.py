#!/usr/bin/env python3
"""让定时更新检测始终执行，只有「推送通知」仍受 Telegram 开关控制。

上游 scheduler/notifier.go 的 runUpdateCheck() 第一行就因未启用 Telegram
直接 return，导致 HubImageInfo 与容器 haveUpdate 快照永远不刷新：上游推了
新版检不出来，镜像更新完界面上「有更新」标记也一直挂着。

本补丁把这个 gate 从函数开头挪到通知阶段之前：
  检测 + 标记容器  -> 无条件执行
  发通知           -> 仍由 Telegram.Enabled / NotifyUpdate 控制

不碰代理、不碰第三方，只在现有定时器周期内多跑一次本地 registry 检测。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。
"""

import sys
from pathlib import Path

TARGET = Path("upstream/internal/module/scheduler/notifier.go")

# ① 移除函数开头的提前 return
GATE_ANCHOR = """	cfg := s.svcCtx.AppConfig.Get()
	// 仅在 TG 启用且更新通知开关打开时才检测推送，避免无谓开销。
	if !cfg.Telegram.Enabled || !cfg.Telegram.NotifyUpdate {
		return
	}
"""

GATE_REPLACEMENT = """	cfg := s.svcCtx.AppConfig.Get()
	// 检测与状态刷新无条件执行：HubImageInfo / 容器 haveUpdate 是界面判断
	// 「有无更新」的唯一数据源。若被通知开关 gate 住，就会出现上游已推新版
	// 却检不出、镜像已更新却一直挂着有更新标记。Telegram 开关只在下方通知阶段生效。
"""

# ② 在收集完待通知项之后、真正发通知之前，恢复 Telegram 开关判断
NOTIFY_ANCHOR = """	if len(pending) == 0 && len(mutedPending) == 0 {
		return
	}

	// 优先调用带交互式键盘的通知（Telegram Bot 实现了 notify.UpdateNotifierWithMuted）
"""

NOTIFY_REPLACEMENT = """	if len(pending) == 0 && len(mutedPending) == 0 {
		return
	}

	// 检测与标记已完成；只有推送通知仍受 Telegram 开关控制。
	if !cfg.Telegram.Enabled || !cfg.Telegram.NotifyUpdate {
		return
	}

	// 优先调用带交互式键盘的通知（Telegram Bot 实现了 notify.UpdateNotifierWithMuted）
"""


def apply(text: str, anchor: str, replacement: str, name: str) -> str:
    count = text.count(anchor)
    if count != 1:
        print(
            f"[patch_scheduler] 锚点 {name} 匹配到 {count} 次（期望 1 次），"
            "上游 notifier.go 可能已重构，请手动核对后更新本脚本",
            file=sys.stderr,
        )
        sys.exit(1)
    return text.replace(anchor, replacement)


def main() -> int:
    if not TARGET.exists():
        print(f"[patch_scheduler] 未找到 {TARGET}，请确认 upstream 检出路径", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = apply(text, GATE_ANCHOR, GATE_REPLACEMENT, "remove-early-return")
    text = apply(text, NOTIFY_ANCHOR, NOTIFY_REPLACEMENT, "gate-notification-only")
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print("[patch_scheduler] 定时检测已解绑 Telegram：检测始终执行，仅通知受开关控制")
    return 0


if __name__ == "__main__":
    sys.exit(main())
