# dockercopilot-accel

## 署名

- **原作者**：[onlyLTY · DongShu](https://github.com/onlyLTY)（DockerCopilot 原始作者）
- **上游**：[l429609201/dockerCopilot](https://github.com/l429609201/dockerCopilot)（fork 自 onlyLTY，移除了加速站）
- **本仓库 Fork 维护**：[@po9873](https://github.com/po9873)（在 l429609201 基础上加回 Docker Hub 加速站并自动构建发布）

## 加速站补丁做了什么

恢复原版 onlyLTY/dockerCopilot 被 fork 移除的 Docker Hub 加速站机制（仅影响"检查更新"的元数据请求，实际拉取仍由 Docker daemon 的 registry-mirrors 决定，与原版一致），并做了三点改进：

1. **支持 `hubURL` 环境变量**：如 `hubURL=https://docker.1ms.run`，显式指定加速站，不做任何探测；
2. 未设置 `hubURL` 时：先探测 Docker Hub 源站，可达即直连（海外环境零开销）；源站不可达才**并行**探测加速站列表（原版是逐个串行，最慢 50 秒），取列表顺序最靠前的可用者；
3. 探测结果**进程内缓存**，保证 digest 来源确定（避免 fork 作者担心的"不知道 digest 来自哪个站"的问题）。

内置加速站列表与原版一致：docker.1ms.run、docker.m.daocloud.io、docker.1panel.top、docker.1panel.live、proxy.1panel.live、dockerproxy.1panel.live、docker.1panel.dev、docker.anye.in、hub.rat.dev、docker.amingg.com。

镜像名显式带 registry（如 `docker.1ms.run/library/nginx:latest`）时仍原样使用，行为与 fork 上游一致。

## 镜像地址

- GHCR：`ghcr.io/po9873/dockercopilot-accel:latest`
- Docker Hub：`po9873/dockercopilot:latest`（需配置仓库 secrets，见 workflow 注释）

版本号严格沿用上游 `version` 文件，不再添加 `-accel.N` 后缀。加速站支持属于构建补丁，不改变 DockerCopilot 程序版本号；是否包含补丁以本仓库构建来源为准。

许可

本仓库的 `auth.go` 派生自 AGPLv3 项目 [l429609201/dockerCopilot](https://github.com/l429609201/dockerCopilot) / [onlyLTY/dockerCopilot](https://github.com/onlyLTY/dockerCopilot) 的同名文件，按 AGPLv3 继续开源，版权归原作者及贡献者所有。
