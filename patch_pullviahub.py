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
  2. 两处 ImagePull 改用加速站引用，并一律按 digest 拉取：先强刷站点缓存、
     再逐站取该 tag 当前 digest，按镜像构建时间从新到旧挑最新候选，
     用 <站>/<repo>@<digest> 拉取；某站没有该内容就换下一站。
     digest 是内容寻址，从机制上杜绝「点更新反而拉回站点缓存里的旧版」。
     拉完用 ImageTag 打回原名，并保留当前加速站引用（它携带 RepoDigests，
DC 检测靠它判断「已是最新」），
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
HELPER2 = Path("upstream/internal/utiles/helper.go")

ANCHOR_UPDATE = """	cli.NegotiateAPIVersion(ctx)
	// 携带凭据拉取私有镜像；registryAuth 为空时等价于匿名拉取
	reader, err := cli.ImagePull(ctx, imageNameAndTag, image.PullOptions{RegistryAuth: registryAuth})
"""

REPL_UPDATE = """	cli.NegotiateAPIVersion(ctx)
	// 与更新检测共用 hubURL 指定的自建加速站：ImagePull 传原始镜像名时，
	// 请求由宿主机 daemon 直连 registry-1.docker.io，完全绕过加速站。
	// 并且按 digest 拉取：tag 拉取可能命中站点缓存里的旧版（点更新反而降级），
	// digest 是内容寻址，站点上没有该内容就换下一站，绝不会拉回旧版。
	pullRef, viaHub := AcceleratedPullRef(imageNameAndTag, registryAuth, hostID)
	if viaHub {
		PurgeHubCache(ctx)
		logx.Infof("镜像拉取改走自建加速站 [%s] -> %s", imageNameAndTag, pullRef)
		oldTaskProgress.DetailMsg = "正在拉取新镜像（自建加速站 " + pullRef + "）"
		serviceContext.UpdateProgress(taskID, oldTaskProgress)
	}
	reader, pullUsed, err := func() (io.ReadCloser, string, error) {
		if !viaHub {
			r, e := cli.ImagePull(ctx, pullRef, image.PullOptions{RegistryAuth: registryAuth})
			return r, pullRef, e
		}
		return hubPullByDigest(ctx, cli, pullRef, registryAuth)
	}()
"""

ANCHOR_DECODE = """	err = decodePullResp(ctx, reader, serviceContext, taskID)
	if err != nil {
		markTaskFailed(serviceContext, taskID, &oldTaskProgress, "拉取镜像失败", err)
"""

REPL_DECODE = """	err = decodePullResp(ctx, reader, serviceContext, taskID)
	if err == nil && viaHub {
		// 拉完用原名重新打标：后续 inspect / 重建容器仍使用原始镜像名。
		if tagErr := cli.ImageTag(ctx, pullUsed, imageNameAndTag); tagErr != nil {
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
	// 同样按 digest 拉取，避免把站点缓存里的旧版拉回来覆盖当前版本。
	pullRef, viaHub := AcceleratedPullRef(imageNameAndTag, registryAuth, "")
	if viaHub {
		PurgeHubCache(ctx)
		logx.Infof("自更新镜像拉取改走自建加速站 [%s] -> %s", imageNameAndTag, pullRef)
	}
	reader, pullUsed, err := func() (io.ReadCloser, string, error) {
		if !viaHub {
			r, e := svcCtx.DockerClient.ImagePull(ctx, pullRef, image.PullOptions{RegistryAuth: registryAuth})
			return r, pullRef, e
		}
		return hubPullByDigest(ctx, svcCtx.DockerClient, pullRef, registryAuth)
	}()
	if err != nil {
		return err
	}
	if err := decodePullResp(ctx, reader, svcCtx, taskID); err != nil {
		return err
	}
	if viaHub {
		if tagErr := svcCtx.DockerClient.ImageTag(ctx, pullUsed, imageNameAndTag); tagErr != nil {
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
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"strings"
	"time"

	ref "github.com/distribution/reference"
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
//
// 白名单必须取 pullRef 自带的 host，不能用 hubcfg.Host()：hubcfg 的当前站
// 会随健康探测/候选失效在两次调用之间切走（拉取时是 A 站，清理时已切到 B 站），
// 用「当前站」做白名单就会把刚拉取的 A 站引用当成「其它站」删掉，连带清空
// 镜像 RepoDigests —— 检测随即退化为「本地无 repoDigest，无法比对」，
// 界面永远显示无更新。以 pullRef 为准可彻底避免这种自伤。
func CleanOtherHubAliases(ctx context.Context, cli *client.Client, pullRef string) {
	refHost := pullRef
	if i := strings.Index(refHost, "/"); i >= 0 {
		refHost = refHost[:i]
	}
	if refHost == "" {
		return
	}
	repo := strings.TrimPrefix(pullRef, refHost+"/")
	if repo == "" {
		return
	}
	for _, t := range hubcfg.Targets() {
		oh := t
		if s, ok := strings.CutPrefix(t, "https://"); ok {
			oh = s
		} else if s, ok := strings.CutPrefix(t, "http://"); ok {
			oh = s
		}
		if oh == refHost {
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

// hubPullByDigest 按 digest 拉取加速站镜像，按「远端构建时间从新到旧」逐站尝试。
// 为什么不能按 tag 拉：tag 是站点自己解析的，缓存滞后时 tag 指向旧 digest，
// 拉取会「成功地」把旧版装回来（实测 192.168.6.249 被这样降级过）。
// digest 是内容寻址，站点上没有该内容就直接失败，由调用方换下一站，
// 从机制上杜绝「点更新反而降级」。
func hubPullByDigest(ctx context.Context, cli *client.Client, tagPullRef, registryAuth string) (io.ReadCloser, string, error) {
	cands := HubDigestPullRefs(tagPullRef)
	if len(cands) == 0 {
		return nil, "", errors.New("加速站 digest 不可得，拒绝按 tag 拉取（避免拉回旧版）")
	}
	var lastErr error
	for _, cand := range cands {
		r, err := cli.ImagePull(ctx, cand, image.PullOptions{RegistryAuth: registryAuth})
		if err == nil {
			logx.Infof("按 digest 拉取成功: %s", cand)
			return r, cand, nil
		}
		lastErr = err
		logx.Infof("warn: 按 digest 拉取失败，换下一候选 %s: %v", cand, err)
	}
	return nil, "", lastErr
}

// HubDigestPullRefs 把加速站 tag 引用展开为「按 digest 拉取」的候选列表，
// 并按各候选镜像的构建时间从新到旧排序，最新的排第一。
//
// 必须比构建时间：各站缓存进度不同，第一个站返回的 digest 很可能对应旧缓存，
// 直接按它拉取就会「成功地」装回旧版。取所有候选里构建时间最新的，
// 才能保证更新永远不降级。站点顺序只作为构建时间相同/取不到时的兜底，
// 不决定结论；全程只读 hubURL，不写死任何域名。
//
// 调用方应先执行 PurgeHubCache 强刷站点 manifest 缓存，否则各站可能都还停在旧 tag。
func HubDigestPullRefs(tagPullRef string) []string {
	i := strings.Index(tagPullRef, "/")
	if i <= 0 {
		return nil
	}
	rest := tagPullRef[i+1:]
	if j := strings.LastIndex(rest, "@"); j >= 0 {
		rest = rest[:j]
	}
	repo, tag := rest, "latest"
	if k := strings.LastIndex(rest, ":"); k > strings.LastIndex(rest, "/") {
		repo, tag = rest[:k], rest[k+1:]
	}
	if repo == "" || tag == "" {
		return nil
	}

	type hubCand struct {
		ref     string
		created time.Time
		known   bool
		order   int
	}
	targets := hubcfg.Targets()
	ch := make(chan hubCand, len(targets))
	for idx, t := range targets {
		go func(idx int, t string) {
			base := strings.TrimSuffix(t, "/")
			token := hubAuthToken(base, repo)
			dg := hubManifestDigest(base+"/v2/"+repo+"/manifests/"+tag, token)
			if dg == "" {
				ch <- hubCand{order: idx}
				return
			}
			h := strings.TrimPrefix(strings.TrimPrefix(t, "https://"), "http://")
			created, known := hubDigestCreated(base, repo, dg, token)
			ch <- hubCand{ref: h + "/" + repo + "@" + dg, created: created, known: known, order: idx}
		}(idx, t)
	}

	cands := make([]hubCand, 0, len(targets))
	seen := map[string]bool{}
	timeout := time.After(12 * time.Second)
loop:
	for i := 0; i < len(targets); i++ {
		select {
		case c := <-ch:
			if c.ref == "" || seen[c.ref] {
				continue
			}
			seen[c.ref] = true
			cands = append(cands, c)
		case <-timeout:
			break loop
		}
	}
	if len(cands) == 0 {
		logx.Infof("warn: 所有加速站都取不到 digest: %s", tagPullRef)
		return nil
	}
	sort.SliceStable(cands, func(a, b int) bool {
		if cands[a].known != cands[b].known {
			return cands[a].known
		}
		if cands[a].known && !cands[a].created.Equal(cands[b].created) {
			return cands[a].created.After(cands[b].created)
		}
		return cands[a].order < cands[b].order
	})
	out := make([]string, 0, len(cands))
	for _, c := range cands {
		out = append(out, c.ref)
	}
	return out
}

// hubAuthToken 取加速站的匿名 pull token：站不要求认证时返回空串，
// 需要认证时按 WWW-Authenticate 的 realm/service 换一个匿名 token。
// 必须支持这一步：KSpeeder 这类站对 docker.io 代理路径会先给 401 挑战，
// 不带 token 连 manifest HEAD 都拿不到，更新会直接失败。
func hubAuthToken(baseURL, repo string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/v2/", nil)
	if err != nil {
		return ""
	}
	res, err := (&http.Client{Timeout: 6 * time.Second}).Do(req)
	if err != nil {
		return ""
	}
	res.Body.Close()
	if res.StatusCode == http.StatusOK {
		return ""
	}
	realm, service := parseBearerChallenge(res.Header.Get("Www-Authenticate"))
	if realm == "" {
		return ""
	}
	u := realm + "?service=" + url.QueryEscape(service) + "&scope=" + url.QueryEscape("repository:"+repo+":pull")
	req2, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return ""
	}
	res2, err := (&http.Client{Timeout: 6 * time.Second}).Do(req2)
	if err != nil {
		return ""
	}
	defer res2.Body.Close()
	if res2.StatusCode != http.StatusOK {
		return ""
	}
	body, err := io.ReadAll(io.LimitReader(res2.Body, 1<<20))
	if err != nil {
		return ""
	}
	var m map[string]interface{}
	if json.Unmarshal(body, &m) != nil {
		return ""
	}
	if tok, _ := m["token"].(string); tok != "" {
		return tok
	}
	tok, _ := m["access_token"].(string)
	return tok
}

// parseBearerChallenge 解析 `WWW-Authenticate: Bearer realm="...",service="..."`。
func parseBearerChallenge(ch string) (string, string) {
	ch = strings.TrimSpace(ch)
	if len(ch) < 7 || !strings.EqualFold(ch[:7], "bearer ") {
		return "", ""
	}
	realm, service := "", ""
	for _, p := range strings.Split(ch[7:], ",") {
		kv := strings.SplitN(strings.TrimSpace(p), "=", 2)
		if len(kv) != 2 {
			continue
		}
		v := strings.Trim(kv[1], `"`)
		switch strings.ToLower(kv[0]) {
		case "realm":
			realm = v
		case "service":
			service = v
		}
	}
	return realm, service
}

// hubManifestDigest 取某站某个 tag 当前指向的 manifest digest。
// 用 GET 而不是 HEAD：部分加速站对 HEAD 响应很慢甚至不返回头，
// manifest 只有几 KB，GET 后直接丢弃 body 更稳。token 为空表示匿名放行。
func hubManifestDigest(u, token string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return ""
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Accept", strings.Join([]string{
		"application/vnd.oci.image.index.v1+json",
		"application/vnd.docker.distribution.manifest.list.v2+json",
		"application/vnd.docker.distribution.manifest.v2+json",
		"application/vnd.oci.image.manifest.v1+json",
	}, ", "))
	res, err := (&http.Client{Timeout: 8 * time.Second}).Do(req)
	if err != nil {
		logx.Infof("warn: 取加速站 digest 失败 %s: %v", u, err)
		return ""
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return ""
	}
	// 读掉少量 body 以便连接复用；manifest 本身很小，不影响耗时。
	_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<20))
	return strings.TrimSpace(res.Header.Get("Docker-Content-Digest"))
}

// hubDigestCreated 取某站某个 digest 对应镜像的构建时间（沿 manifest -> config blob）。
// 用于在多站候选里挑「最新的那个」，避免按旧缓存的 digest 拉取导致降级。
func hubDigestCreated(baseURL, repo, digest, token string) (time.Time, bool) {
	accept := strings.Join([]string{
		"application/vnd.oci.image.index.v1+json",
		"application/vnd.docker.distribution.manifest.list.v2+json",
		"application/vnd.docker.distribution.manifest.v2+json",
		"application/vnd.oci.image.manifest.v1+json",
	}, ", ")
	getJSON := func(u string) (map[string]interface{}, bool) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return nil, false
		}
		req.Header.Set("Accept", accept)
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		res, err := (&http.Client{Timeout: 5 * time.Second}).Do(req)
		if err != nil {
			return nil, false
		}
		defer res.Body.Close()
		if res.StatusCode != http.StatusOK {
			return nil, false
		}
		body, err := io.ReadAll(io.LimitReader(res.Body, 4<<20))
		if err != nil {
			return nil, false
		}
		var m map[string]interface{}
		if json.Unmarshal(body, &m) != nil {
			return nil, false
		}
		return m, true
	}

	top, ok := getJSON(baseURL + "/v2/" + repo + "/manifests/" + digest)
	if !ok {
		return time.Time{}, false
	}
	cfg := ""
	if list, isList := top["manifests"].([]interface{}); isList && len(list) > 0 {
		for _, item := range list {
			m, _ := item.(map[string]interface{})
			plat, _ := m["platform"].(map[string]interface{})
			arch, _ := plat["architecture"].(string)
			osName, _ := plat["os"].(string)
			d, _ := m["digest"].(string)
			if d != "" && arch == runtime.GOARCH && (osName == "" || osName == runtime.GOOS) {
				cfg = d
				break
			}
		}
		if cfg == "" {
			if first, ok := list[0].(map[string]interface{}); ok {
				cfg, _ = first["digest"].(string)
			}
		}
		if cfg == "" {
			return time.Time{}, false
		}
		child, ok := getJSON(baseURL + "/v2/" + repo + "/manifests/" + cfg)
		if !ok {
			return time.Time{}, false
		}
		c, _ := child["config"].(map[string]interface{})
		cfg, _ = c["digest"].(string)
	} else {
		c, _ := top["config"].(map[string]interface{})
		cfg, _ = c["digest"].(string)
	}
	if cfg == "" {
		return time.Time{}, false
	}
	blob, ok := getJSON(baseURL + "/v2/" + repo + "/blobs/" + cfg)
	if !ok {
		return time.Time{}, false
	}
	s, _ := blob["created"].(string)
	if s == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// looksLikeImageID 判断字符串是否是「用镜像 ID 创建的容器」留下的纯十六进制 ID。
// 这类字符串能被 reference 库当成合法仓库名解析，但补成 tag 会造出
// 07a5582f9e6c:latest 这种莫名其妙的引用，必须排除。
func looksLikeImageID(s string) bool {
	if len(s) != 12 && len(s) != 64 {
		return false
	}
	for _, r := range s {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			return false
		}
	}
	return true
}

// repairMissingImageTags 给「容器在用、但镜像本身已经没有任何 tag」的镜像补回
// 容器创建时使用的名字（取自 ContainerInspect.Config.Image）。
//
// 为什么需要：旧版 hubpull 打回原名后又删掉同名临时引用，会把镜像唯一的 tag
// 一起删掉，容器靠镜像 ID 继续运行，DC 列表于是显示 tag=None，手动删除也被
// dockerd 拒绝。按 digest 拉取的新版不会再制造这种镜像，但历史遗留、以及
// 手动 rmi/tag 操作仍可能产生。补的 tag 就是容器原本的名字，不新增无关别名。
// 这一步不依赖 hubURL，没配加速站的用户同样生效。
func repairMissingImageTags(ctx context.Context, cli *client.Client) {
	imgs, err := cli.ImageList(ctx, image.ListOptions{All: true})
	if err != nil {
		return
	}
	need := map[string]bool{}
	for _, im := range imgs {
		hasTag := false
		for _, t := range im.RepoTags {
			if t != "" && !strings.Contains(t, "<none>") {
				hasTag = true
				break
			}
		}
		if !hasTag {
			need[strings.TrimPrefix(im.ID, "sha256:")] = true
		}
	}
	if len(need) == 0 {
		return
	}
	ctns, cerr := cli.ContainerList(ctx, container.ListOptions{All: true})
	if cerr != nil {
		return
	}
	fixed := map[string]bool{}
	for _, c := range ctns {
		id := strings.TrimPrefix(c.ImageID, "sha256:")
		if !need[id] || fixed[id] {
			continue
		}
		ins, ierr := cli.ContainerInspect(ctx, c.ID)
		if ierr != nil || ins.Config == nil {
			continue
		}
		src := ins.Config.Image
		if looksLikeImageID(src) || strings.Contains(src, "<none>") {
			continue
		}
		parsed, perr := ref.ParseNormalizedNamed(src)
		if perr != nil {
			continue
		}
		name := ref.FamiliarName(parsed)
		if name == "" {
			continue
		}
		tag := "latest"
		if tagged, ok := parsed.(ref.Tagged); ok {
			tag = tagged.Tag()
		}
		target := name + ":" + tag
		if terr := cli.ImageTag(ctx, c.ImageID, target); terr == nil {
			short := id
			if len(short) > 12 {
				short = short[:12]
			}
			logx.Infof("已补回缺失的镜像 tag（容器创建名）: %s -> %s", short, target)
			fixed[id] = true
		}
	}
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
	// 先补回「容器在用却没有 tag」的历史镜像，不依赖 hubURL、不受下面
	// hosts 为空的提前返回影响。
	repairMissingImageTags(ctx, cli)

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


ANCHOR_SELF_IMPORT = """import (
	"context"
	"fmt"
	"os"
	"strings"
"""

REPL_SELF_IMPORT = """import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"
"""

# 容器重建成功之后再清一次旧镜像。拉取后那次清理发生在重建之前：旧容器还
# 引用着旧镜像，被 inUse 跳过；原 tag 又已经转移给新镜像，旧镜像于是以
# <none> / tag=None 的形态留在列表里，直到下一次更新才被清掉。
ANCHOR_UPDATE_DONE = """	oldTaskProgress.Message = "更新成功"
	oldTaskProgress.DetailMsg = "更新成功"
	oldTaskProgress.Percentage = 100
	oldTaskProgress.IsDone = true
	serviceContext.UpdateProgress(taskID, oldTaskProgress)"""

REPL_UPDATE_DONE = """	// 重建完成、新容器已稳定运行后再清一次旧版镜像。保留旧容器备份时，
	// 旧镜像仍在被引用，PurgeStaleHubImages 会自行跳过，绝不强删。
	if ins, _, ierr := cli.ImageInspectWithRaw(context.Background(), imageNameAndTag); ierr == nil {
		PurgeStaleHubImages(context.Background(), cli, ins.ID)
	}

	oldTaskProgress.Message = "更新成功"
	oldTaskProgress.DetailMsg = "更新成功"
	oldTaskProgress.Percentage = 100
	oldTaskProgress.IsDone = true
	serviceContext.UpdateProgress(taskID, oldTaskProgress)"""

ANCHOR_HELPER_DONE = """	logx.Infof("[helper] ✅ DC 自我更新成功！新容器 %s 已启动并运行正常", targetName)"""

REPL_HELPER_DONE = """	logx.Infof("[helper] ✅ DC 自我更新成功！新容器 %s 已启动并运行正常", targetName)

	// 自更新重建完成后同样再清一次：主容器拉取后那次清理时旧 DC 容器
	// 仍在引用旧镜像，被跳过；此刻旧容器已删除，无引用旧镜像可以清掉。
	if ins, _, ierr := cli.ImageInspectWithRaw(ctx, newImage); ierr == nil {
		PurgeStaleHubImages(ctx, cli, ins.ID)
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
    rc |= apply(SELF, ANCHOR_SELF_IMPORT, REPL_SELF_IMPORT, "selfupdate/import-io")
    rc |= apply(REMOVE, ANCHOR_REMOVE_IMPORT, REPL_REMOVE_IMPORT, "removeimage/import")
    rc |= apply(REMOVE, ANCHOR_REMOVE_BODY, REPL_REMOVE_BODY, "removeimage/force-fallback")
    rc |= apply(UPDATE, ANCHOR_UPDATE_DONE, REPL_UPDATE_DONE, "updatecontainer/after-recreate")
    rc |= apply(HELPER2, ANCHOR_HELPER_DONE, REPL_HELPER_DONE, "helper/after-recreate")
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
