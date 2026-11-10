// Package hubcfg 解析 hubURL（允许配置多个自建加速站）并做主备选择。
//
// 设计要点：同一时刻只用一个站。多个站的 tag->digest 缓存彼此独立，
// 若轮换/并发使用，检测结论会在不同站的 STALE 旧 manifest 之间跳动，
// 界面就会「动不动提示有更新」。这里按配置顺序选第一个可达的站，
// 主站健康期间永不切换，主站恢复后自动切回，因此不会冲突。
// 检测链路明确判定某站「喂的是旧缓存」（远端不比本地新 / 数据不可验证）时，
// 调用 MarkCurrentStale 让该站冷却跳过、自动改用下一站，解决「站活着但
// 一直返回几天前的旧 index」；冷却到期重新参与探测，无需人工改配置顺序。
//
// 健康探测使用真实 manifest GET（默认 library/nginx），而不是 /v2/：
// 自建加速站的 /v2/ 由本地直接应答，上游节点池回源卡死时仍秒回 200，
// 只有真正取一次 manifest 才能识别「上游超时」并切到备站。
package hubcfg

import (
	"crypto/tls"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/zeromicro/go-zero/core/logx"
)

// selectTTL 是选中结果的缓存时长；到期后重新按顺序探测一次，
// 主站恢复即可切回，主站故障最多 60 秒后切到备站。
const selectTTL = 60 * time.Second

// probeTimeout 必须明显短于上游回源卡住的时长（通常 8s 以上），
// 又要容得下公网备站的正常往返（实测中位 0.5s、偶发 2.5s，故取 5s）。
const probeTimeout = 5 * time.Second

// invalidateInterval 限制「请求失败触发重探」的频率，
// 避免一轮检测里多个镜像取不到数据时反复发起探测。
const invalidateInterval = 30 * time.Second

// staleCooldown 是「明确判定某站数据陈旧」后的冷却时长：冷却期内该站
// 不参与主备探测，DC 自动改用配置里的下一站；到期后重新给它机会，
// 加速站缓存一旦收敛就自动恢复，不需要人工改 hubURL 顺序。
const staleCooldown = 10 * time.Minute

// unverifiedCooldown 用于「远端数据暂时不可验证」（上游抖动 / 重建瞬间），
// 比明确旧缓存短得多，避免一次偶发超时就把整站排除十分钟。
const unverifiedCooldown = 2 * time.Minute

const defaultProbeRepo = "library/nginx"

// manifestAccept 与检测链路保持一致，避免加速站因 Accept 头不同而返回 404/406。
const manifestAccept = "application/vnd.oci.image.index.v1+json, " +
	"application/vnd.docker.distribution.manifest.list.v2+json, " +
	"application/vnd.docker.distribution.manifest.v2+json, " +
	"application/vnd.oci.image.manifest.v1+json"

var (
	selectMu       sync.Mutex
	selected       string
	selectedAt     time.Time
	lastInvalidate time.Time
	staleUntil     = map[string]time.Time{}
	configOnce     sync.Once
)

// Targets 把 hubURL 按逗号 / 分号 / 空白拆成多个 target（"scheme://host[:port]"），
// 保序、去重、丢弃非法项。单站写法完全兼容，行为与旧版一致。
// 例：hubURL=https://a.example:5443,https://b.example:5443
func Targets() []string {
	raw := strings.NewReplacer(",", " ", ";", " ", "\n", " ", "\t", " ").Replace(os.Getenv("hubURL"))
	seen := make(map[string]bool)
	out := make([]string, 0, 2)
	for _, item := range strings.Fields(raw) {
		t := normalize(item)
		if t == "" || seen[t] {
			continue
		}
		seen[t] = true
		out = append(out, t)
	}
	return out
}

func normalize(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return ""
	}
	return u.Scheme + "://" + u.Host
}

// probeRepo 返回健康探测用的仓库；可用 hubProbeRepo 覆盖，
// 便于某个加速站没有代理 library/nginx 时自行指定。
func probeRepo() string {
	if v := strings.Trim(strings.TrimSpace(os.Getenv("hubProbeRepo")), "/"); v != "" {
		return v
	}
	return defaultProbeRepo
}

// Current 返回本轮应使用的加速站 target。
// 单站：直接返回，零额外请求。多站：按配置顺序做 manifest 级探测，取第一个可达者。
// 全部不可达时沿用上次选择（没有则取第一个），绝不退回内置第三方站列表。
func Current() string {
	ts := Targets()
	if len(ts) == 0 {
		return ""
	}
	configOnce.Do(func() {
		logx.Infof("hubURL 加速站配置: %s | 健康探测: %s manifest GET, 超时 %s",
			strings.Join(ts, " > "), probeRepo(), probeTimeout)
	})
	if len(ts) == 1 {
		return ts[0]
	}

	selectMu.Lock()
	defer selectMu.Unlock()
	now := time.Now()
	if selected != "" && time.Since(selectedAt) < selectTTL &&
		hasTarget(ts, selected) && !inCooldown(selected, now) {
		return selected
	}

	// 明确判定「数据陈旧」的站冷却期内不参与探测；若所有站都被标记，
	// 清空冷却重新给全部站机会，避免某次集体 STALE 后永久无站可用。
	candidates := make([]string, 0, len(ts))
	for _, t := range ts {
		if inCooldown(t, now) {
			logx.Infof("加速站数据陈旧冷却中，跳过 [%s]", t)
			continue
		}
		candidates = append(candidates, t)
	}
	if len(candidates) == 0 {
		staleUntil = map[string]time.Time{}
		candidates = ts
		logx.Infof("所有 hubURL 加速站均处于陈旧冷却，重新探测")
	}
	for _, t := range candidates {
		ok, why := reachable(t)
		if ok {
			if selected != "" && selected != t {
				logx.Infof("加速站主备切换: %s -> %s", selected, t)
			}
			selected, selectedAt = t, now
			return t
		}
		logx.Errorf("加速站健康探测失败 [%s]: %s", t, why)
	}
	// 全部不可达：沿用上次选择并同样刷新时间戳，避免每个检测周期都重复探测。
	if selected == "" || !hasTarget(ts, selected) {
		selected = ts[0]
		logx.Errorf("所有 hubURL 加速站均不可达，暂用第一个: %s", ts[0])
	} else {
		logx.Errorf("所有 hubURL 加速站均不可达，沿用上次选择: %s", selected)
	}
	selectedAt = now
	return selected
}

// Invalidate 让当前选择立刻失效，下一次 Current() 会重新探测。
// 供检测链路在「远端数据取不到」时调用，使上游超时后不必等满 60 秒 TTL
// 就能切到备站；30 秒内最多生效一次，避免一轮检测里反复探测。
func Invalidate() {
	selectMu.Lock()
	defer selectMu.Unlock()
	if time.Since(lastInvalidate) < invalidateInterval {
		return
	}
	lastInvalidate = time.Now()
	selectedAt = time.Time{}
}

// inCooldown 判断某站是否仍在「数据陈旧」冷却期；调用方需持有 selectMu。
func inCooldown(target string, now time.Time) bool {
	until, ok := staleUntil[target]
	return ok && until.After(now)
}

// MarkCurrentStale 标记当前站「明确返回旧缓存」（远端构建时间不晚于本地）。
// 冷却期内该站不参与探测，下一轮自动改用配置里的下一站；到期自动恢复。
// 只有确凿判定才调用，不因两站 digest 不同就切站，避免主备来回抖动。
func MarkCurrentStale(reason string) {
	markStale(reason, staleCooldown)
}

// MarkCurrentUnverified 标记「远端数据暂时不可验证」（上游超时 / 403 / 429）。
// 冷却较短，恢复后很快重新参与探测；同样会立即切换到备站。
func MarkCurrentUnverified(reason string) {
	markStale(reason, unverifiedCooldown)
}

func markStale(reason string, cooldown time.Duration) {
	selectMu.Lock()
	defer selectMu.Unlock()
	t := selected
	if t == "" {
		// 单站配置直接返回 ts[0]、不设置 selected，也就没有站可切换。
		logx.Errorf("加速站数据陈旧，但当前无选中站（单站配置无法切换）: %s", reason)
		return
	}
	now := time.Now()
	if until, ok := staleUntil[t]; ok && until.After(now) {
		// 同一轮检测里多个镜像都可能命中：只续期（不缩短已有冷却），
		// 不重复失效、不刷日志，避免一次检测把主备来回切。
		if now.Add(cooldown).After(until) {
			staleUntil[t] = now.Add(cooldown)
		}
		return
	}
	staleUntil[t] = now.Add(cooldown)
	selectedAt = time.Time{} // 立即失效，下一次 Current() 重新探测
	logx.Errorf("加速站数据陈旧，冷却 %v 后重试 [%s]: %s", cooldown, t, reason)
}

// Host 返回当前站不含 scheme 的 host（含端口），用于拼镜像引用。
func Host() string {
	u, err := url.Parse(Current())
	if err != nil {
		return ""
	}
	return u.Host
}

// Hostname 返回当前站纯主机名/IP（不含端口），用于推导管理口 :5003。
func Hostname() string {
	u, err := url.Parse(Current())
	if err != nil {
		return ""
	}
	return u.Hostname()
}

func hasTarget(list []string, want string) bool {
	for _, t := range list {
		if t == want {
			return true
		}
	}
	return false
}

// reachable 做一次真实 manifest GET：必须 200、带 sha256 digest 头、
// 且响应体确实是 manifest JSON。上游回源超时 / 403 / 空响应都会被判为不可达。
// 返回 (是否可达, 失败原因)，失败原因写进日志便于定位是哪个站、哪种故障。
func reachable(target string) (bool, string) {
	client := &http.Client{
		Timeout: probeTimeout,
		Transport: &http.Transport{
			// 自建加速站是内网地址，不跟随进程里的代理环境变量，
			// 避免容器里残留的 HTTP_PROXY 让主备探测全部误判为不可达。
			DialContext:       (&net.Dialer{Timeout: probeTimeout}).DialContext,
			TLSClientConfig:   &tls.Config{InsecureSkipVerify: true},
			DisableKeepAlives: true,
		},
	}
	req, err := http.NewRequest(http.MethodGet,
		target+"/v2/"+probeRepo()+"/manifests/latest", nil)
	if err != nil {
		return false, "构造请求失败: " + err.Error()
	}
	req.Header.Set("Accept", manifestAccept)
	res, err := client.Do(req)
	if err != nil {
		return false, "请求失败(含超时): " + err.Error()
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return false, "HTTP " + res.Status
	}
	if dg := res.Header.Get("Docker-Content-Digest"); !strings.HasPrefix(dg, "sha256:") {
		return false, "缺少有效 Docker-Content-Digest"
	}
	body, err := io.ReadAll(io.LimitReader(res.Body, 64*1024))
	if err != nil {
		return false, "读取 manifest 失败: " + err.Error()
	}
	if !strings.Contains(string(body), `"schemaVersion"`) {
		return false, "响应体不是 manifest JSON"
	}
	return true, ""
}

// ---------------------------------------------------------------------------
// 统一远端口径：检测与更新共用「所有站里构建时间最新的那一份 manifest」
//
// 为什么必须统一：两个加速站的 tag->digest 缓存彼此独立，可能相差十几天。
// 检测只问「第一个可达站」会随主备切换在新旧 digest 之间摆动；更新又各自
// 重新取一次 tag digest，两边结论不一致，于是「更新完又提示有更新」死循环。
//
// ResolveLatest 一次遍历所有可用站点，按 manifest 对应的镜像构建时间取最新
// 那份作为唯一权威结论。检测拿它比本地 RepoDigests，更新拿它按 digest 拉取，
// 两边看到的是同一份数据。按构建时间取最新是单调的：旧站缓存永远不会赢，
// 不会因为某次探测顺序不同而翻烧饼。全程不写死任何域名。
// ---------------------------------------------------------------------------

// RemoteVerdict 是某个 repo:tag 在所有可用站点里构建时间最新的 manifest。
type RemoteVerdict struct {
	Repo       string
	Tag        string
	Host       string // 不含 scheme 的 host[:port]
	Digest     string
	Created    time.Time
	HasCreated bool
	Consensus  bool     // 所有可达镜像站 digest 一致，视为 Docker Hub 当前权威状态
	PullRefs   []string // 按构建时间新->旧排序的 <host>/<repo>@<digest>
}

const remoteAccept = "application/vnd.oci.image.index.v1+json, " +
	"application/vnd.docker.distribution.manifest.list.v2+json, " +
	"application/vnd.docker.distribution.manifest.v2+json, " +
	"application/vnd.oci.image.manifest.v1+json"

// splitRemoteRef 解析镜像引用，返回 (显式 host, 不带 host 的 repo, tag)。
// host 为空表示 Docker Hub 官方镜像（走 hubURL 加速站）。
func splitRemoteRef(imageRef string) (string, string, string, bool) {
	s := strings.TrimSpace(imageRef)
	if a, ok := strings.CutPrefix(s, "https://"); ok {
		s = a
	} else if a, ok := strings.CutPrefix(s, "http://"); ok {
		s = a
	}
	if i := strings.Index(s, "@"); i >= 0 {
		s = s[:i]
	}
	// 必须先剥掉 tag 再判断第一段是不是 registry host，否则
	// "nginx:latest" 里的冒号会被当成「host 带端口」，误判成显式 registry。
	namePart, tag := s, "latest"
	if k := strings.LastIndex(namePart, ":"); k > strings.LastIndex(namePart, "/") {
		tag, namePart = namePart[k+1:], namePart[:k]
	}
	first := namePart
	if i := strings.Index(first, "/"); i >= 0 {
		first = first[:i]
	}
	host, repo := "", namePart
	if strings.ContainsAny(first, ".:") || first == "localhost" {
		host = first
		repo = namePart[len(first)+1:]
	} else if !strings.Contains(namePart, "/") {
		repo = "library/" + namePart
	}
	if repo == "" || tag == "" {
		return "", "", "", false
	}
	return host, repo, tag, true
}

// remoteHTTP 对自建加速站放宽证书校验（内网常见自签），公共 registry 保持校验。
func remoteHTTP(base string) *http.Client {
	for _, t := range Targets() {
		if strings.TrimSuffix(t, "/") == base {
			return &http.Client{Timeout: 8 * time.Second, Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
			}}
		}
	}
	return &http.Client{Timeout: 8 * time.Second}
}

func remoteJSON(client *http.Client, u, token string) (map[string]interface{}, bool) {
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, false
	}
	req.Header.Set("Accept", remoteAccept)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := client.Do(req)
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

// remoteToken 换取匿名 pull token；站点 /v2/ 直接 200 时返回空串。
func remoteToken(base, repo string) string {
	client := remoteHTTP(base)
	req, err := http.NewRequest(http.MethodGet, base+"/v2/", nil)
	if err != nil {
		return ""
	}
	res, err := client.Do(req)
	if err != nil {
		return ""
	}
	res.Body.Close()
	if res.StatusCode == http.StatusOK {
		return ""
	}
	realm, service := remoteBearer(res.Header.Get("WWW-Authenticate"))
	if realm == "" {
		return ""
	}
	u := realm + "?service=" + url.QueryEscape(service) + "&scope=" + url.QueryEscape("repository:"+repo+":pull")
	req2, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return ""
	}
	res2, err := client.Do(req2)
	if err != nil {
		return ""
	}
	defer res2.Body.Close()
	if res2.StatusCode != http.StatusOK {
		return ""
	}
	body, _ := io.ReadAll(io.LimitReader(res2.Body, 1<<20))
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

func remoteBearer(ch string) (string, string) {
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

// remoteManifestDigest 用 GET 取 tag 当前指向的 manifest digest（index 优先）。
func remoteManifestDigest(base, repo, tag, token string) string {
	client := remoteHTTP(base)
	req, err := http.NewRequest(http.MethodGet, base+"/v2/"+repo+"/manifests/"+tag, nil)
	if err != nil {
		return ""
	}
	req.Header.Set("Accept", remoteAccept)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := client.Do(req)
	if err != nil {
		return ""
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return ""
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<20))
	return strings.TrimSpace(res.Header.Get("Docker-Content-Digest"))
}

// remoteManifestCreated 沿 index -> 子 manifest -> config blob 取构建时间。
func remoteManifestCreated(base, repo, digest, token string) (time.Time, bool) {
	client := remoteHTTP(base)
	top, ok := remoteJSON(client, base+"/v2/"+repo+"/manifests/"+digest, token)
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
		child, ok := remoteJSON(client, base+"/v2/"+repo+"/manifests/"+cfg, token)
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
	blob, ok := remoteJSON(client, base+"/v2/"+repo+"/blobs/"+cfg, token)
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

// ResolveLatest 遍历所有可用站点，返回构建时间最新的 manifest 及其拉取候选。
// 检测与更新必须都调用本函数，才会看到同一份结论。
func ResolveLatest(imageRef string) (RemoteVerdict, bool) {
	host, repo, tag, ok := splitRemoteRef(imageRef)
	if !ok {
		return RemoteVerdict{}, false
	}
	var targets []string
	if host != "" {
		isHub := false
		for _, t := range Targets() {
			u, _ := url.Parse(t)
			if strings.EqualFold(u.Host, host) {
				isHub = true
				break
			}
		}
		if isHub {
			targets = Targets()
		} else {
			targets = []string{"https://" + host}
		}
	} else {
		targets = Targets()
		if len(targets) == 0 {
			// 未配置加速站时退化为直连 Docker Hub，行为与原版一致。
			targets = []string{"https://registry-1.docker.io"}
		}
	}

	type cand struct {
		host, digest string
		created      time.Time
		has          bool
		order        int
	}
	ch := make(chan cand, len(targets))
	for idx, t := range targets {
		go func(idx int, t string) {
			base := strings.TrimSuffix(t, "/")
			u, _ := url.Parse(base)
			tok := remoteToken(base, repo)
			dg := remoteManifestDigest(base, repo, tag, tok)
			if dg == "" {
				ch <- cand{order: idx}
				return
			}
			cr, has := remoteManifestCreated(base, repo, dg, tok)
			ch <- cand{host: u.Host, digest: dg, created: cr, has: has, order: idx}
		}(idx, t)
	}
	cands := make([]cand, 0, len(targets))
	timeout := time.After(12 * time.Second)
loop:
	for i := 0; i < len(targets); i++ {
		select {
		case c := <-ch:
			if c.digest != "" {
				cands = append(cands, c)
			}
		case <-timeout:
			break loop
		}
	}
	if len(cands) == 0 {
		return RemoteVerdict{}, false
	}
	sort.SliceStable(cands, func(a, b int) bool {
		if cands[a].has != cands[b].has {
			return cands[a].has
		}
		if cands[a].has && !cands[a].created.Equal(cands[b].created) {
			return cands[a].created.After(cands[b].created)
		}
		return cands[a].order < cands[b].order
	})
	best := cands[0]
	// 权威判定（A）：所有可达镜像站返回的 digest 是否完全一致。
	// 一致 -> 视为 Docker Hub 当前权威状态，检测可仅凭 digest 不一致判定（无需构建时间佐证）；
	// 不一致（镜像站缓存漂移）-> 不能仅凭某站说"不同"就判更新，需要远端构建时间佐证，
	// 否则判 Unknown 不报，彻底杜绝"我没发新版却提示有更新"的误报。
	// 某站超时/不可达导致 cands 少于 targets 时也视为不一致，保守处理。
	consensus := false
	if len(cands) == len(targets) && len(cands) > 0 {
		d0 := cands[0].digest
		allSame := true
		for _, c := range cands {
			if c.digest != d0 {
				allSame = false
				break
			}
		}
		consensus = allSame
	}
	v := RemoteVerdict{
		Repo: repo, Tag: tag, Host: best.host, Digest: best.digest,
		Created: best.created, HasCreated: best.has, Consensus: consensus,
	}
	seen := map[string]bool{}
	add := func(ref string) {
		if !seen[ref] {
			seen[ref] = true
			v.PullRefs = append(v.PullRefs, ref)
		}
	}
	for _, c := range cands {
		add(c.host + "/" + repo + "@" + c.digest)
	}
	// 最新那份 digest 在所有可达站上都试一遍：某站可能还没回源该 blob。
	for _, c := range cands {
		add(c.host + "/" + repo + "@" + best.digest)
	}
	logx.Infof("远端最新 manifest [%s:%s] digest=%s host=%s created=%s 候选=%d consensus=%v",
		repo, tag, v.Digest, v.Host, v.Created.Format(time.RFC3339), len(v.PullRefs), v.Consensus)
	return v, true
}
