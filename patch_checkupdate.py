#!/usr/bin/env python3
"""对上游 l429609201/dockerCopilot 的 internal/module/checkupdate.go 打补丁。

在 checkSingleImage 中 GetDigest 失败（或返回空 digest）之后插入
「Docker Hub 镜像走兜底加速站重试」的逻辑，与 auth.go 中新增的
FetchDigestViaFallback / IsDockerHubImage 配合使用。

锚点不唯一或未命中时以非零码退出，让构建失败并通知维护者（与
overlay 补丁的重构告警策略一致），避免静默打出无效镜像。
"""

import sys
from pathlib import Path

TARGET = Path("upstream/internal/module/checkupdate.go")

ANCHOR = """		remoteDigest, err = GetDigest(digestURL, token)
		if err != nil {
"""

REPLACEMENT = """		remoteDigest, err = GetDigest(digestURL, token)
		// 加速站兜底：部分加速站只覆盖已缓存的仓库（未缓存仓库返回 403/525
		// 或 200 但无 digest），对 Docker Hub 镜像在主加速站失败时改走兜底
		// 加速站重试一次，避免这类镜像的检测结果全部落入「未知」。
		if (err != nil || remoteDigest == "") && IsDockerHubImage(image) {
			if altDigest, altErr := FetchDigestViaFallback(image); altErr == nil && altDigest != "" {
				logx.Infof("加速站回源失败，已通过兜底源获取 digest [%s]", imageRef)
				remoteDigest, err = altDigest, nil
			}
		}
		if err != nil {
"""


def main() -> int:
    if not TARGET.exists():
        print(f"[patch_checkupdate] 未找到 {TARGET}，请确认 upstream 检出路径", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    count = text.count(ANCHOR)
    if count != 1:
        print(
            f"[patch_checkupdate] 锚点匹配到 {count} 次（期望 1 次），"
            "上游 checkupdate.go 可能已重构，请手动核对后更新本脚本",
            file=sys.stderr,
        )
        return 1

    TARGET.write_text(text.replace(ANCHOR, REPLACEMENT), encoding="utf-8")
    print("[patch_checkupdate] checkupdate.go 兜底重试补丁已应用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
