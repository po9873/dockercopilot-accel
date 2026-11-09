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
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
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

// ---- 检测结论 -> 更新动作的 digest 桥接 ----
//
// 「更新成功却仍提示有更新」的根因：检测阶段按 A digest 判定并亮角标，
// 点更新时加速站缓存可能已刷新成 B digest，更新按 B 拉取，容器镜像没换成
// A；检测结果按镜像 ID 索引，ID 没变，角标自然继续亮。这里把检测确认的
// digest 记下来，更新时优先按它拉取，保证「检测报哪一版就装哪一版」。
// 记忆带 TTL（略长于 30 分钟检测周期），过期自动失效，不会长期误导。

type remoteDigestRecord struct {
	digest string
	at     time.Time
}

var remoteDigestMu sync.RWMutex
var remoteDigestMemo = map[string]remoteDigestRecord{}

// NormalizeDigestKey 把任意镜像引用规范化成 "repo:tag" 作为桥接 key：
// 去掉 scheme、digest、以及显式 registry host（含端口），让检测侧的
// lswl/vertex:stable 与更新侧的 qqaazz.xxx/lswl/vertex:stable 落在同一 key。
func NormalizeDigestKey(imageRef string) string {
	s := imageRef
	if a, ok := strings.CutPrefix(s, "https://"); ok {
		s = a
	} else if a, ok := strings.CutPrefix(s, "http://"); ok {
		s = a
	}
	if i := strings.Index(s, "@"); i >= 0 {
		s = s[:i]
	}
	if i := strings.Index(s, "/"); i >= 0 {
		first := s[:i]
		if strings.ContainsAny(first, ".:") || first == "localhost" {
			s = s[i+1:]
		}
	}
	if !strings.Contains(s, ":") {
		s += ":latest"
	}
	return s
}

// RememberRemoteDigest 记录检测阶段确认「有更新」的远端 digest。
func RememberRemoteDigest(imageRef, digest string) {
	if digest == "" {
		return
	}
	remoteDigestMu.Lock()
	defer remoteDigestMu.Unlock()
	remoteDigestMemo[NormalizeDigestKey(imageRef)] = remoteDigestRecord{digest: digest, at: time.Now()}
}

// ForgetRemoteDigest 清除某个镜像的检测 digest 记忆。
func ForgetRemoteDigest(imageRef string) {
	remoteDigestMu.Lock()
	defer remoteDigestMu.Unlock()
	delete(remoteDigestMemo, NormalizeDigestKey(imageRef))
}

// KnownRemoteDigest 返回最近一次检测确认的远端 digest；超过 35 分钟视为过期。
func KnownRemoteDigest(imageRef string) string {
	remoteDigestMu.RLock()
	defer remoteDigestMu.RUnlock()
	v, ok := remoteDigestMemo[NormalizeDigestKey(imageRef)]
	if !ok || time.Since(v.at) > 35*time.Minute {
		return ""
	}
	return v.digest
}
