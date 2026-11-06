#!/usr/bin/env python3
"""让 DC 的「镜像拉取」与「更新检测」共用 hubURL 指定的自建加速站。

根因：UpdateContainerOnHost / SelfUpdate 都调用
    cli.ImagePull(ctx, imageNameAndTag, ...)
传的是原始镜像名，请求由宿主机 Docker daemon 发出，走 registry-mirrors 或
直连 registry-1.docker.io，完全绕过 DC 里配置的 hubURL。所以检测（走加速站）
能看到新 digest，点更新却报 connection reset by peer。

补丁做三件事：
  1. 新增 internal/utiles/hubpull.go：把 Docker Hub 镜像改写为
     <hubURL host>/<repo>:<tag>；单段官方镜像名补 library/ 前缀
     （KSpeeder 对 <host>/alpine:latest 返回 401，必须写成
     <host>/library/alpine:latest）；仅对本地主机、匿名、无显式 registry
     前缀的镜像生效，GHCR / 私有 registry / 带凭据镜像保持原名。
  2. 两处 ImagePull 改用加速站引用，拉完用 ImageTag 打回原名、
     再用 ImageRemove 删掉临时引用（主备切换不会累积 <站域名>/ 别名），
     后续 ImageInspect / ContainerCreate 完全不动；
  3. 拉取前调用自建加速站管理口 /api/cache/cleanup?force=1 强制刷新
     manifest 缓存（KSpeeder TTL 不可调，普通 cleanup 只清过期条目），
     避免拉到缓存里的旧 digest。失败只记日志、不阻断；hubCachePurge=off 可关。

不引入代理、不引入第三方镜像站、不改任何宿主机 Docker 配置。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。
"""

import sys
from pathlib import Path

UPDATE = Path("upstream/internal/utiles/updatecontainer.go")
SELF = Path("upstream/internal/utiles/selfupdate.go")
HELPER = Path("upstream/internal/utiles/hubpull.go")

ANCHOR_UPDATE = """	cli.NegotiateAPIVersion(ctx)
	// 携带凭据拉取私有镜像；registryAuth 为空时等价于匿名拉取
	reader, err := cli.ImagePull(ctx, imageNameAndTag, image.PullOptions{RegistryAuth: registryAuth})
"""

REPL_UPDATE = """	cli.NegotiateAPIVersion(ctx)
	// 与更新检测共用 hubURL 指定的自建加速站：ImagePull 传原始镜像名时，
	// 请求由宿主机 daemon 直连 registry-1.docker.io，完全绕过加速站。
	pullRef, viaHub := AcceleratedPullRef(imageNameAndTag, registryAuth, hostID)
	if viaHub {
		PurgeHubCache(ctx)
		logx.Infof("镜像拉取改走自建加速站 [%s] -> %s", imageNameAndTag, pullRef)
		oldTaskProgress.DetailMsg = "正在拉取新镜像（自建加速站 " + pullRef + "）"
		serviceContext.UpdateProgress(taskID, oldTaskProgress)
	}
	// 携带凭据拉取私有镜像；registryAuth 为空时等价于匿名拉取
	reader, err := cli.ImagePull(ctx, pullRef, image.PullOptions{RegistryAuth: registryAuth})
"""

ANCHOR_DECODE = """	err = decodePullResp(ctx, reader, serviceContext, taskID)
	if err != nil {
		markTaskFailed(serviceContext, taskID, &oldTaskProgress, "拉取镜像失败", err)
"""

REPL_DECODE = """	err = decodePullResp(ctx, reader, serviceContext, taskID)
	if err == nil && viaHub {
		// 拉完用原名重新打标：后续 inspect / 重建容器仍使用原始镜像名。
		if tagErr := cli.ImageTag(ctx, pullRef, imageNameAndTag); tagErr != nil {
			markTaskFailed(serviceContext, taskID, &oldTaskProgress, "镜像重新打标失败", tagErr)
			logx.Errorf("Failed to tag image %s -> %s: %s", pullRef, imageNameAndTag, tagErr)
			return tagErr
		}
		// 打标成功后删掉加速站域名的临时引用。原名 tag 已存在，ImageRemove
		// 只会 untag 这一个引用，不会删除镜像层。主备切换时若不清理，镜像列表
		// 里同一个 ID 会不断累积 <站域名>/<repo>:<tag> 别名。
		// 清理失败不影响更新结果，仅记日志。
		if _, rmErr := cli.ImageRemove(ctx, pullRef, image.RemoveOptions{}); rmErr != nil {
			logx.Infof("warn: 临时镜像引用清理失败（不影响更新）: %s -> %v", pullRef, rmErr)
		} else {
			logx.Infof("临时镜像引用已清理: %s", pullRef)
		}
	}
	if err != nil {
		markTaskFailed(serviceContext, taskID, &oldTaskProgress, "拉取镜像失败", err)
"""

ANCHOR_SELF = """func pullImageForSelfUpdate(ctx context.Context, svcCtx *svc.ServiceContext, imageNameAndTag, registryAuth, taskID string) error {
	svcCtx.DockerClient.NegotiateAPIVersion(ctx)
	reader, err := svcCtx.DockerClient.ImagePull(ctx, imageNameAndTag, image.PullOptions{RegistryAuth: registryAuth})
	if err != nil {
		return err
	}
	return decodePullResp(ctx, reader, svcCtx, taskID)
}
"""

REPL_SELF = """func pullImageForSelfUpdate(ctx context.Context, svcCtx *svc.ServiceContext, imageNameAndTag, registryAuth, taskID string) error {
	svcCtx.DockerClient.NegotiateAPIVersion(ctx)
	// 自更新同样必须走自建加速站，否则新版 DC 镜像自身也拉不下来。
	pullRef, viaHub := AcceleratedPullRef(imageNameAndTag, registryAuth, "")
	if viaHub {
		PurgeHubCache(ctx)
		logx.Infof("自更新镜像拉取改走自建加速站 [%s] -> %s", imageNameAndTag, pullRef)
	}
	reader, err := svcCtx.DockerClient.ImagePull(ctx, pullRef, image.PullOptions{RegistryAuth: registryAuth})
	if err != nil {
		return err
	}
	if err := decodePullResp(ctx, reader, svcCtx, taskID); err != nil {
		return err
	}
	if viaHub {
		if tagErr := svcCtx.DockerClient.ImageTag(ctx, pullRef, imageNameAndTag); tagErr != nil {
			return tagErr
		}
		// 同 UpdateContainerOnHost：删掉加速站域名的临时引用，避免主备切换后
		// 镜像列表累积别名。清理失败只记日志，不阻断自更新。
		if _, rmErr := svcCtx.DockerClient.ImageRemove(ctx, pullRef, image.RemoveOptions{}); rmErr != nil {
			logx.Infof("warn: 临时镜像引用清理失败（不影响更新）: %s -> %v", pullRef, rmErr)
		} else {
			logx.Infof("临时镜像引用已清理: %s", pullRef)
		}
	}
	return nil
}
"""

HELPER_GO = '''package utiles

import (
	"context"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/l429609201/dockerCopilot/internal/hubcfg"
	"github.com/l429609201/dockerCopilot/internal/module/appconfig"
	"github.com/zeromicro/go-zero/core/logx"
)

// AcceleratedPullRef 把 Docker Hub 镜像名改写为 hubURL 指定的自建加速站地址。
// 返回 (改写后的引用, 是否改写)。仅对「本地 Docker 主机 + 匿名 + 无显式
// registry 前缀」的 Docker Hub 镜像生效；远程主机（可能访问不到内网域名）、
// GHCR / 私有 registry、带凭据的私有镜像都保持原名，避免绕错源站或凭据错配。
func AcceleratedPullRef(imageNameAndTag, registryAuth, hostID string) (string, bool) {
	if hostID != "" && hostID != appconfig.DockerHostLocalID {
		return imageNameAndTag, false
	}
	if registryAuth != "" {
		return imageNameAndTag, false
	}
	// 与更新检测共用 hubcfg 选出的「当前站」：hubURL 配了多个自建站时，
	// 拉取和检测必须落在同一个站，否则 tag->digest 映射会互相打架。
	host := hubcfg.Host()
	if host == "" {
		return imageNameAndTag, false
	}
	// 去掉 tag / digest 后取第一段，判断是否为显式 registry。
	name := imageNameAndTag
	if i := strings.LastIndex(name, "@"); i >= 0 {
		name = name[:i]
	}
	if i := strings.LastIndex(name, ":"); i > strings.LastIndex(name, "/") {
		name = name[:i]
	}
	first := name
	if i := strings.Index(first, "/"); i >= 0 {
		first = first[:i]
	}
	if first == "localhost" {
		return imageNameAndTag, false
	}
	if strings.ContainsAny(first, ".:") {
		// 显式 registry 域名：只改写白名单内的源站（默认官方 KSpeeder 站），
		// 剥掉域名后走自建加速站上的同名仓库，避免直连官方站撞
		// 100 次/小时/IP 匿名限流，导致「界面提示有更新，点更新却拉不动」。
		// GHCR / 私有 registry 不在白名单内，保持原名以免绕错源站。
		allow := strings.TrimSpace(os.Getenv("hubRewriteDomains"))
		if allow == "" {
			allow = "registry.kspeeder.com"
		}
		for _, d := range strings.Split(allow, ",") {
			if !strings.EqualFold(strings.TrimSpace(d), first) {
				continue
			}
			repo := strings.TrimPrefix(name, first+"/")
			if repo == "" || repo == first {
				return imageNameAndTag, false
			}
			return host + "/" + repo + imageNameAndTag[len(name):], true
		}
		return imageNameAndTag, false
	}
	if strings.HasPrefix(imageNameAndTag, host+"/") {
		return imageNameAndTag, false
	}
	// 官方单段镜像名（alpine:latest、nginx:1.25 等）在部分自建加速站
	// （如 KSpeeder）上必须写成 library/<name>，直接拼 <host>/<name> 会返回
	// 401 unauthorized。Docker Hub 本身也把单段名字归一到 library/，
	// 这里保持一致；打回原名仍用 imageNameAndTag，不影响容器镜像引用。
	if !strings.Contains(name, "/") {
		return host + "/library/" + imageNameAndTag, true
	}
	return host + "/" + imageNameAndTag, true
}

// PurgeHubCache 在拉取前强制刷新自建加速站的 manifest 缓存。
// KSpeeder 的 TTL 不可调，普通 /api/cache/cleanup 只清过期条目；带 force=1
// 才会清未过期条目，否则会拉到缓存里的旧 digest。管理口地址由 hubURL 的
// host 推导（同机 5003 端口），失败只记日志、不阻断拉取。
// 设置 hubCachePurge=off 可关闭。
func PurgeHubCache(ctx context.Context) {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("hubCachePurge")), "off") {
		return
	}
	hn := hubcfg.Hostname()
	if hn == "" {
		return
	}
	target := "http://" + net.JoinHostPort(hn, "5003") + "/api/cache/cleanup?force=1"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, nil)
	if err != nil {
		return
	}
	client := &http.Client{Timeout: 5 * time.Second}
	res, err := client.Do(req)
	if err != nil {
		logx.Infof("warn: 自建加速站清缓存请求失败（不影响拉取）: %v", err)
		return
	}
	defer res.Body.Close()
	logx.Infof("自建加速站缓存已强制刷新: %s status=%d", target, res.StatusCode)
}
'''


def apply(path: Path, anchor: str, replacement: str, name: str) -> int:
    if not path.exists():
        print(f"[patch_pullviahub] 未找到 {path}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    count = text.count(anchor)
    if count != 1:
        print(
            f"[patch_pullviahub] {name} 锚点匹配 {count} 次（期望 1 次），"
            "上游源码可能已重构，请核对后更新本脚本",
            file=sys.stderr,
        )
        return 1
    path.write_text(text.replace(anchor, replacement), encoding="utf-8", newline="\n")
    return 0


def main() -> int:
    rc = 0
    rc |= apply(UPDATE, ANCHOR_UPDATE, REPL_UPDATE, "updatecontainer/pull")
    rc |= apply(UPDATE, ANCHOR_DECODE, REPL_DECODE, "updatecontainer/tag")
    rc |= apply(SELF, ANCHOR_SELF, REPL_SELF, "selfupdate/pull")
    if rc:
        return rc
    if not HELPER.parent.is_dir():
        print(f"[patch_pullviahub] 目录不存在: {HELPER.parent}", file=sys.stderr)
        return 1
    HELPER.write_text(HELPER_GO, encoding="utf-8", newline="\n")
    print("[patch_pullviahub] 拉取链路已改走自建加速站（含自更新、缓存强制刷新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
