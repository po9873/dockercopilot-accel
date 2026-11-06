// Package hubcfg 解析 hubURL（允许配置多个自建加速站）并做主备选择。
//
// 设计要点：同一时刻只用一个站。多个站的 tag->digest 缓存彼此独立，
// 若轮换/并发使用，检测结论会在不同站的 STALE 旧 manifest 之间跳动，
// 界面就会「动不动提示有更新」。这里按配置顺序选第一个可达的站，
// 主站健康期间永不切换，主站恢复后自动切回，因此不会冲突。
package hubcfg

import (
	"crypto/tls"
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

var (
	selectMu   sync.Mutex
	selected   string
	selectedAt time.Time
	configOnce sync.Once
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

// Current 返回本轮应使用的加速站 target。
// 单站：直接返回，零额外请求。多站：按配置顺序探测 /v2/，取第一个可达者。
// 全部不可达时沿用上次选择（没有则取第一个），绝不退回内置第三方站列表。
func Current() string {
	ts := Targets()
	if len(ts) == 0 {
		return ""
	}
	configOnce.Do(func() {
		logx.Infof("hubURL 加速站配置: %s", strings.Join(ts, " > "))
	})
	if len(ts) == 1 {
		return ts[0]
	}

	selectMu.Lock()
	defer selectMu.Unlock()
	if selected != "" && time.Since(selectedAt) < selectTTL && hasTarget(ts, selected) {
		return selected
	}
	for _, t := range ts {
		if reachable(t) {
			if selected != "" && selected != t {
				logx.Infof("加速站主备切换: %s -> %s", selected, t)
			}
			selected, selectedAt = t, time.Now()
			return t
		}
	}
	if selected == "" || !hasTarget(ts, selected) {
		selected, selectedAt = ts[0], time.Now()
		logx.Errorf("所有 hubURL 加速站均不可达，暂用第一个: %s", ts[0])
	}
	return selected
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

func reachable(target string) bool {
	client := &http.Client{
		Timeout: 3 * time.Second,
		Transport: &http.Transport{
			// 自建加速站是内网地址，不跟随进程里的代理环境变量，
			// 避免容器里残留的 HTTP_PROXY 让主备探测全部误判为不可达。
			DialContext:       (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
			TLSClientConfig:   &tls.Config{InsecureSkipVerify: true},
			DisableKeepAlives: true,
		},
	}
	req, err := http.NewRequest(http.MethodGet, target+"/v2/", nil)
	if err != nil {
		return false
	}
	res, err := client.Do(req)
	if err != nil {
		return false
	}
	defer res.Body.Close()
	// 200（匿名 registry）与 401（要求鉴权的 registry）都说明端点活着。
	return res.StatusCode == http.StatusOK || res.StatusCode == http.StatusUnauthorized
}
