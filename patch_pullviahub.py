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
  2. 两处 ImagePull 改用加速站引用，拉完用 ImageTag 打回原名，并保留当前
     加速站引用（它携带 RepoDigests，DC 检测靠它判断「已是最新」），
     只清理 hubURL 里「其它加速站」的同名别名（主备切换不会累积 <站域名>/
     别名、也不清空 RepoDigests）；后续 ImageInspect / ContainerCreate 完全不动；
  3. 拉取前调用自建加速站管理口 /api/cache/cleanup?force=1 强制刷新
     manifest 缓存（KSpeeder TTL 不可调，普通 cleanup 只清过期条目），
     避免拉到缓存里的旧 digest。失败只记日志、不阻断；hubCachePurge=off 可关。
  4. 拉取后清理「旧版残留镜像」：删掉不再被任何容器引用、且仍挂着加速站
     前缀 tag 的整张镜像。CleanOtherHubAliases 只管本次拉取的那张，换源
     （qqaazz <-> ghcr）后上一版镜像上的旧 tag 永远没人清，几百 MB 且界面
     删不掉。同时在删除接口加安全兜底：镜像挂多个仓库名被 dockerd 以
     "referenced in multiple repositories" 拒绝时，确认无容器引用后自动
     用 force 重试一次——修掉「界面点删除没反应」，仍在用的镜像保持失败。

不引入代理、不引入第三方镜像站、不改任何宿主机 Docker 配置。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。
"""

import sys
from pathlib import Path

UPDATE = Path("upstream/internal/utiles/updatecontainer.go")
SELF = Path("upstream/internal/utiles/selfupdate.go")
HELPER = Path("upstream/internal/utiles/hubpull.go")
REMOVE = Path("upstream/internal/utiles/removeimage.go")

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
		// 关键：保留当前加速站引用 pullRef（它携带 RepoDigests），只清理
		// 其它加速站同名别名。删掉 pullRef 会清空镜像 RepoDigests，导致
		// 检测落回「未知/误报」。详见 hubpull.go 的 CleanOtherHubAliases。
		CleanOtherHubAliases(ctx, cli, pullRef)
		logx.Infof("加速站引用已保留（携带 RepoDigests，保证检测可用）: %s", pullRef)
		// 换源之后，上一版镜像上的旧加速站 tag 不会自动转移，会永久占着
		// 几百 MB 且界面上删不掉。这里顺手清掉，刚拉的这张用 keepID 保护。
		// docker client v27 没有 ImageInspect，只有 ImageInspectWithRaw。
		if ins, _, ierr := cli.ImageInspectWithRaw(ctx, imageNameAndTag); ierr == nil {
			PurgeStaleHubImages(ctx, cli, ins.ID)
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
		// 同 UpdateContainerOnHost：保留当前加速站引用（携带 RepoDigests，
		// 保证检测可用），只清理其它加速站同名别名，避免主备切换累积别名。
		CleanOtherHubAliases(ctx, svcCtx.DockerClient, pullRef)
		logx.Infof("自更新：加速站引用已保留（携带 RepoDigests）: %s", pullRef)
		// 同 UpdateContainerOnHost：清掉旧版镜像上的历史加速站引用。
		// 自更新时新镜像尚未被容器引用，必须用 keepID 显式保护。
		if ins, _, ierr := svcCtx.DockerClient.ImageInspectWithRaw(ctx, imageNameAndTag); ierr == nil {
			PurgeStaleHubImages(ctx, svcCtx.DockerClient, ins.ID)
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

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
	"github.com/l429609201/dockerCopilot/internal/hubcfg"
	"github.com/l429609201/dockerCopilot/internal/module/appconfig"
	"github.com/zeromicro/go-zero/core/logx"
)

// CleanOtherHubAliases 保留「当前加速站」引用（它携带 RepoDigests，
// 形如 qqaazz.../library/nginx@sha256:...），只清理 hubURL 里「其它加速站」
// 的同名引用，避免主备切换后同一个镜像 ID 不断累积多个 <站域名>/<repo>:<tag>
// 别名。
//
// 为什么必须保留当前加速站引用、不能像旧版那样直接删掉：
// 镜像的 RepoDigests 是跟着「从哪个仓库拉下来」的引用走的。DC 检测只取
// RepoDigests 里 @ 之后的 digest 值（内容寻址，与挂在哪个加速站 host 上无关）
// 和远端同源站 digest 值比对；只要这个值存在且一致就判「已是最新」。旧版
// ImageRemove(pullRef) 把唯一带 digest 的引用删掉，镜像 RepoDigests 被清空，
// 检测落回「未知/误报」。因此这里只清「对向站」别名，保留当前站引用。
// 清理失败不阻断更新（别名可能正被容器引用，忽略即可）。
func CleanOtherHubAliases(ctx context.Context, cli *client.Client, pullRef string) {
	host := hubcfg.Host()
	if host == "" {
		return
	}
	repo := pullRef
	if i := strings.Index(repo, "/"); i >= 0 {
		repo = repo[i+1:]
	}
	for _, t := range hubcfg.Targets() {
		oh := t
		if s, ok := strings.CutPrefix(t, "https://"); ok {
			oh = s
		} else if s, ok := strings.CutPrefix(t, "http://"); ok {
			oh = s
		}
		if oh == host {
			continue
		}
		alias := oh + "/" + repo
		if _, err := cli.ImageRemove(ctx, alias, image.RemoveOptions{}); err == nil {
			logx.Infof("清理对向加速站别名: %s", alias)
		}
	}
}

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

// PurgeStaleHubImages 删掉「旧版本残留镜像」：不再被任何容器引用、且仍挂着
// 加速站前缀 tag 的整张镜像。
//
// 为什么需要：CleanOtherHubAliases 只处理「本次拉取的那张镜像」。每次换源
// （qqaazz <-> ghcr）之后，上一版镜像上那个 <旧站>/<repo>:<tag> 就永远没人
// 清。Docker 的 image prune 只清 <none>:<none>，这类镜像挂着完整 tag 不算
// dangling；界面点删除又会被 dockerd 以 "image is referenced in multiple
// repositories" 拒绝（一张镜像同时挂 <站>/<repo>:<tag> 和 ghcr.io 的 <none>）。
// 结果就是一堆几百 MB 的「未使用」镜像躺在列表里删不掉。
//
// 安全性：keepID 是刚拉取的新镜像，无论有没有容器引用都不删；其余只删
// 「无任何容器（含已停止）引用」的，正在用的版本永远不碰。
func PurgeStaleHubImages(ctx context.Context, cli *client.Client, keepID string) {
	hosts := make([]string, 0, 4)
	for _, t := range hubcfg.Targets() {
		h := t
		if s, ok := strings.CutPrefix(t, "https://"); ok {
			h = s
		} else if s, ok := strings.CutPrefix(t, "http://"); ok {
			h = s
		}
		if h != "" {
			hosts = append(hosts, h+"/")
		}
	}
	if len(hosts) == 0 {
		return
	}
	keep := strings.TrimPrefix(keepID, "sha256:")

	// 收集在用镜像 ID（含已停止容器）；列举失败就整体跳过，不做模糊判断。
	inUse := map[string]bool{}
	ctns, cerr := cli.ContainerList(ctx, container.ListOptions{All: true})
	if cerr != nil {
		logx.Infof("warn: 列举容器失败，跳过旧版镜像清理: %v", cerr)
		return
	}
	for _, c := range ctns {
		inUse[strings.TrimPrefix(c.ImageID, "sha256:")] = true
	}

	imgs, ierr := cli.ImageList(ctx, image.ListOptions{All: true})
	if ierr != nil {
		logx.Infof("warn: 列举镜像失败，跳过旧版镜像清理: %v", ierr)
		return
	}
	cleaned := 0
	for _, im := range imgs {
		id := strings.TrimPrefix(im.ID, "sha256:")
		short := id
		if len(short) > 12 {
			short = short[:12]
		}
		if id == keep || inUse[id] {
			continue
		}
		// RepoTags 只覆盖「仍带 tag」的引用；那些已经变成 <none> 的，只能从
		// RepoDigests 看出它来自哪个加速站（形如 <站>/<repo>@sha256:...）。
		// 两类都要看，否则 <站>/<repo>:<none> 这种会漏清。
		refs := make([]string, 0, len(im.RepoTags)+len(im.RepoDigests))
		refs = append(refs, im.RepoTags...)
		refs = append(refs, im.RepoDigests...)
		hit := ""
		for _, t := range refs {
			for _, h := range hosts {
				if strings.HasPrefix(t, h) {
					hit = t
					break
				}
			}
			if hit != "" {
				break
			}
		}
		if hit == "" {
			continue
		}
		if _, rerr := cli.ImageRemove(ctx, im.ID, image.RemoveOptions{Force: true, PruneChildren: true}); rerr != nil {
			logx.Infof("warn: 清理旧版加速站镜像 %s 失败（忽略）: %v", short, rerr)
			continue
		}
		cleaned++
		logx.Infof("已清理旧版加速站镜像: %s [%s]", short, hit)
	}
	if cleaned > 0 {
		logx.Infof("旧版加速站镜像清理完成，共 %d 个", cleaned)
	}
}
'''

ANCHOR_REMOVE_IMPORT = """import (
	"context"
	"fmt"

	"github.com/docker/docker/api/types/image"
	"github.com/l429609201/dockerCopilot/internal/svc"
)"""

REPL_REMOVE_IMPORT = """import (
	"context"
	"fmt"
	"strings"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/client"
	"github.com/l429609201/dockerCopilot/internal/svc"
	"github.com/zeromicro/go-zero/core/logx"
)"""

ANCHOR_REMOVE_BODY = """	_, err := cli.ImageRemove(context.Background(), imageID, image.RemoveOptions{Force: force})
	if err != nil {
		return err
	}
	return nil
}"""

REPL_REMOVE_BODY = """	rmCtx := context.Background()
	_, err := cli.ImageRemove(rmCtx, imageID, image.RemoveOptions{Force: force})
	if err == nil || force {
		return err
	}
	// 界面删除默认 force=false（RemoveImageReq.Force 的默认值是 false）。
	// 镜像同时挂多个仓库名时（<加速站>/<repo>:<tag> + <repo>:<tag> + ghcr.io 的
	// <none>），dockerd 直接回 "image is referenced in multiple repositories"，
	// 界面上表现为「点了删除没反应」。这类残留通常已无容器引用：确认没有容器
	// 在用后自动用 force 重试一次；仍在被容器引用的镜像保持原错误，绝不强删。
	if imageInUseByContainer(cli, imageID) {
		return err
	}
	if _, err2 := cli.ImageRemove(rmCtx, imageID, image.RemoveOptions{Force: true, PruneChildren: true}); err2 == nil {
		logx.Infof("镜像 %s 挂多个仓库名且无容器引用，已强制删除", imageID)
		return nil
	}
	return err
}

// imageInUseByContainer 判断镜像是否被任何容器（含已停止）引用。
// 查不到镜像信息或列举容器失败时返回 true（保守处理，绝不放行强删）。
func imageInUseByContainer(cli *client.Client, imageID string) bool {
	ins, _, ierr := cli.ImageInspectWithRaw(context.Background(), imageID)
	if ierr != nil {
		return true
	}
	full := strings.TrimPrefix(ins.ID, "sha256:")
	list, lerr := cli.ContainerList(context.Background(), container.ListOptions{All: true})
	if lerr != nil {
		return true
	}
	for _, c := range list {
		if strings.TrimPrefix(c.ImageID, "sha256:") == full {
			return true
		}
	}
	return false
}"""


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
    rc |= apply(REMOVE, ANCHOR_REMOVE_IMPORT, REPL_REMOVE_IMPORT, "removeimage/import")
    rc |= apply(REMOVE, ANCHOR_REMOVE_BODY, REPL_REMOVE_BODY, "removeimage/force-fallback")
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
