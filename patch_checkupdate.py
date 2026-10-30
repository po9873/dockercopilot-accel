#!/usr/bin/env python3
"""对上游 l429609201/dockerCopilot 的 internal/module/checkupdate.go 打补丁。

在 checkSingleImage 中 GetDigest 失败（或返回空 digest）之后插入
「同一加速站 URL 重试」的逻辑：部分加速站（如 KSpeeder）把请求随机
分发到多个上游节点，其中个别节点故障时表现为 525/403，重试同一 URL
往往能命中健康节点。不依赖任何第三方加速站。

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
		// 加速站重试：部分加速站把请求随机分发到多个上游节点，个别节点
		// 故障时表现为 525/403/空 digest，重试同一 URL 通常能命中健康节点。
		// 最多额外重试 3 次，退避间隔递增，全部失败才放弃。
		for retry := 0; retry < 3 && (err != nil || remoteDigest == ""); retry++ {
			time.Sleep(time.Duration(300*(retry+1)) * time.Millisecond)
			remoteDigest, err = GetDigest(digestURL, token)
		}
		if err != nil {
"""

# BuildManifestURL 中 manifest URL 的 scheme 原来写死 https、host 直接填
# GetRegistryAddress 的返回值。而后者的返回值可能是带 scheme 的完整 target
# （hubURL=https://registry.linkease.net:5443 时归一为 "https://host:port"），
# 直接填进 url.URL.Host 会被转义成 "https:%2F%2F..."，导致 parse:
# invalid URL escape "%2F"，所有镜像检测失败。因此这里必须同时剥离
# http:// 与 https:// 两种前缀，取真正的 host[:port]。
URL_ANCHOR = """	url := url2.URL{
		Scheme: "https",
		Host:   host,
		Path:   fmt.Sprintf("/v2/%s/manifests/%s", img, tag),
	}
"""

URL_REPLACEMENT = """	scheme := "https"
	addr := host
	if s, ok := strings.CutPrefix(host, "http://"); ok {
		scheme, addr = "http", s
	} else if s, ok := strings.CutPrefix(host, "https://"); ok {
		// hubURL 归一后带 https:// 前缀，必须剥掉，否则 // 会被转义为 %2F
		scheme, addr = "https", s
	}
	url := url2.URL{
		Scheme: scheme,
		Host:   addr,
		Path:   fmt.Sprintf("/v2/%s/manifests/%s", img, tag),
	}
"""


def apply(text: str, anchor: str, replacement: str, name: str) -> str:
    count = text.count(anchor)
    if count != 1:
        print(
            f"[patch_checkupdate] 锚点 {name} 匹配到 {count} 次（期望 1 次），"
            "上游 checkupdate.go 可能已重构，请手动核对后更新本脚本",
            file=sys.stderr,
        )
        sys.exit(1)
    return text.replace(anchor, replacement)


def main() -> int:
    if not TARGET.exists():
        print(f"[patch_checkupdate] 未找到 {TARGET}，请确认 upstream 检出路径", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    text = apply(text, ANCHOR, REPLACEMENT, "fallback-retry")
    text = apply(text, URL_ANCHOR, URL_REPLACEMENT, "manifest-url-scheme")
    TARGET.write_text(text, encoding="utf-8")
    print("[patch_checkupdate] checkupdate.go 同站重试 + http scheme 补丁已应用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
