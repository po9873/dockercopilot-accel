#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
署名补丁：把镜像内 DockerCopilot 的维护者身份从 l429609201 改为 po9873。

在 build.yml 中于「检出上游源码」之后执行，只做精确字符串替换：
  - frontend-react/src/components/Header.jsx   侧边栏「团队信息」Fork 维护 l429609201 -> po9873
  - docker/Dockerfile                          镜像 LABEL authors -> po9873
  - frontend-react/src/components/About.jsx    「本项目 GitHub」/ Issues 链接 -> 本仓库
  - internal/handler/icons/obtainhandler.go    DC 自身容器图标识别的镜像名 -> 本仓库 GHCR 名

原作者 onlyLTY · DongShu 的署名保留不动；Go 模块导入路径
（github.com/l429609201/dockerCopilot/...）严禁替换，否则无法编译。
任何一处匹配数量不符预期即使构建失败——说明上游结构变了，需更新本脚本。
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "upstream")

# (相对路径, 原字符串, 新字符串, 期望出现次数)
PATCHES = [
    (
        "frontend-react/src/components/Header.jsx",
        ">l429609201</p>",
        ">po9873</p>",
        1,
    ),
    (
        "docker/Dockerfile",
        'LABEL authors="l429609201 (fork of onlyLTY/dockerCopilot)"',
        'LABEL authors="po9873 (fork of l429609201/dockerCopilot, original by onlyLTY)"',
        1,
    ),
    (
        "frontend-react/src/components/About.jsx",
        "https://github.com/l429609201/dockerCopilot",
        "https://github.com/po9873/dockercopilot-accel",
        2,
    ),
    (
        "internal/handler/icons/obtainhandler.go",
        "ghcr.io/l429609201/dockercopilot",
        "ghcr.io/po9873/dockercopilot-accel",
        2,
    ),
]


def main() -> int:
    failed = False
    for rel, old, new, expect in PATCHES:
        p = ROOT / rel
        if not p.exists():
            print(f"[MISS] {rel} 不存在（上游结构可能已变）")
            failed = True
            continue
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n != expect:
            print(f"[WARN] {rel}: 期望 {expect} 处 {old[:48]!r}，实际 {n} 处")
            failed = True
            continue
        p.write_text(text.replace(old, new), encoding="utf-8")
        print(f"[ OK ] {rel}: 替换 {n} 处 -> {new[:60]!r}")
    if failed:
        print("署名补丁应用失败：上游源码结构可能已变化，请核对后更新 patch_attribution.py")
        return 1
    print("署名补丁全部应用完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
