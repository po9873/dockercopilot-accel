package module

import (
	"encoding/json"
	"errors"
	"fmt"
	ref "github.com/distribution/reference"
	"github.com/l429609201/dockerCopilot/internal/types"
	"github.com/zeromicro/go-zero/core/logx"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const ChallengeHeader = "WWW-Authenticate"
const (
	DefaultRegistryDomain = "docker.io"
	DefaultRegistryHost   = "index.docker.io"
)

// authHTTPTimeout 认证类请求的超时。原实现用裸 http.Client{}（无超时），
// registry 无响应时会永久挂住，并行检查下会持续占用 worker。
const authHTTPTimeout = 15 * time.Second

func GetToken(image types.Image, registryAuth string) (string, error) {
	// 每个镜像每轮都会走到这里，用 Debug 级别避免刷屏掩盖真实错误
	logx.Debugf("获取 token，镜像: %s", image.ImageName)
	normalizedRef, err := ref.ParseNormalizedNamed(image.ImageName)
	if err != nil {
		return "", err
	}

	URL := GetChallengeURL(normalizedRef)

	var req *http.Request
	if req, err = GetChallengeRequest(URL); err != nil {
		return "", err
	}

	client := &http.Client{Timeout: authHTTPTimeout}
	var res *http.Response
	if res, err = client.Do(req); err != nil {
		return "", err
	}
	defer func(Body io.ReadCloser) {
		err := Body.Close()
		if err != nil {
			logx.Error("GetToken关闭Body失败" + err.Error())
		}
	}(res.Body)
	v := res.Header.Get(ChallengeHeader)

	challenge := strings.ToLower(v)
	if strings.HasPrefix(challenge, "basic") {
		if registryAuth == "" {
			return "", fmt.Errorf("no credentials available")
		}

		return fmt.Sprintf("Basic %s", registryAuth), nil
	}
	if strings.HasPrefix(challenge, "bearer") {
		return GetBearerHeader(challenge, normalizedRef, registryAuth)
	}

	// 免鉴权 registry（/v2/ 直接返回 200 且无 WWW-Authenticate 挑战头，
	// 如 linkease/iStoreOS 本地镜像加速）视为匿名可用：返回空 token，
	// 由调用方走匿名 manifest 请求，不再作为错误中断检测链路。
	return "", nil
}

func GetChallengeRequest(URL url.URL) (*http.Request, error) {
	req, err := http.NewRequest("GET", URL.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", "Watchtower (Docker)")
	return req, nil
}

func GetBearerHeader(challenge string, imageRef ref.Named, registryAuth string) (string, error) {
	client := http.Client{Timeout: authHTTPTimeout}
	authURL, err := GetAuthURL(challenge, imageRef)

	if err != nil {
		return "", err
	}

	var r *http.Request
	if r, err = http.NewRequest("GET", authURL.String(), nil); err != nil {
		return "", err
	}

	if registryAuth != "" {
		r.Header.Add("Authorization", fmt.Sprintf("Basic %s", registryAuth))
		logx.Debug("使用已配置凭证请求 token")
	} else {
		// 公开镜像匿名拉取本就无需凭证，这不是错误，降为 Debug 避免噪音
		logx.Debug("未配置 registry 凭证，按匿名方式获取 token")
	}

	var authResponse *http.Response
	if authResponse, err = client.Do(r); err != nil {
		return "", err
	}

	body, _ := io.ReadAll(authResponse.Body)
	tokenResponse := &types.TokenResponse{}

	err = json.Unmarshal(body, tokenResponse)
	if err != nil {
		return "", err
	}

	return fmt.Sprintf("Bearer %s", tokenResponse.Token), nil
}

func GetAuthURL(challenge string, imageRef ref.Named) (*url.URL, error) {
	loweredChallenge := strings.ToLower(challenge)
	raw := strings.TrimPrefix(loweredChallenge, "bearer")

	pairs := strings.Split(raw, ",")
	values := make(map[string]string, len(pairs))

	for _, pair := range pairs {
		trimmed := strings.Trim(pair, " ")
		if key, val, ok := strings.Cut(trimmed, "="); ok {
			values[key] = strings.Trim(val, `"`)
		}
	}
	if values["realm"] == "" || values["service"] == "" {

		return nil, fmt.Errorf("challenge header did not include all values needed to construct an auth url")
	}

	authURL, _ := url.Parse(values["realm"])
	q := authURL.Query()
	q.Add("service", values["service"])

	scopeImage := ref.Path(imageRef)

	scope := fmt.Sprintf("repository:%s:pull", scopeImage)
	q.Add("scope", scope)

	authURL.RawQuery = q.Encode()
	return authURL, nil
}

func GetChallengeURL(imageRef ref.Named) url.URL {
	host, _ := GetRegistryAddress(imageRef.Name())

	URL := url.URL{
		Scheme: "https",
		Host:   host,
		Path:   "/v2/",
	}
	return URL
}

// DefaultAcceleratorHostList Docker Hub（docker.io）官方镜像的加速站候选列表，
// 与原版 onlyLTY/dockerCopilot 保持一致。仅当源站 index.docker.io 不可达时才会启用，
// 结果进程内缓存，不会逐镜像反复探测。
var DefaultAcceleratorHostList = []string{
	"docker.1ms.run", "docker.m.daocloud.io",
	"docker.1panel.top", "docker.1panel.live", "proxy.1panel.live", "dockerproxy.1panel.live", "docker.1panel.dev",
	"docker.anye.in", "hub.rat.dev", "docker.amingg.com",
}

// probeTimeout 单个站点可达性探测的超时；各站点并行探测，总耗时以该值为上限。
const probeTimeout = 3 * time.Second

// probeHTTPClient 探测专用 client：短超时、不复用连接，与正常检查用的
// digestHTTPClient/authHTTPTimeout 相互独立，探测流量不干扰正常请求。
var probeHTTPClient = &http.Client{
	Timeout: probeTimeout,
	Transport: &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		DialContext:         (&net.Dialer{Timeout: probeTimeout}).DialContext,
		TLSHandshakeTimeout: probeTimeout,
		DisableKeepAlives:   true,
	},
}

var (
	acceleratorOnce sync.Once
	acceleratorHost string
)

// resolveHubHost 决定 docker.io 镜像检查更新时实际请求的 host：
//  1. 设置了 hubURL 环境变量（如 hubURL=https://docker.1ms.run）时直接使用，不做探测；
//  2. 否则先探测源站 index.docker.io，可达即直连（海外环境行为与未打补丁时一致）；
//  3. 源站不可达时并行探测加速站列表，取列表顺序最靠前的可用者；
//  4. 全部不可达则回退源站，让失败以「检测未知」形式暴露而非静默改写。
//
// 结果经 sync.Once 缓存，进程生命周期内保持不变，保证 digest 来源确定
// （这正是上游移除旧实现时担心的点：旧版逐镜像串行探测且结果漂移）。
func resolveHubHost() string {
	acceleratorOnce.Do(func() {
		if host := normalizeHubURL(os.Getenv("hubURL")); host != "" {
			acceleratorHost = host
			logx.Infof("hubURL 已配置，docker.io 镜像更新检查走加速站: %s", host)
			return
		}
		if checkHost(DefaultRegistryHost) {
			acceleratorHost = DefaultRegistryHost
			return
		}
		if host := probeFirstReachable(DefaultAcceleratorHostList); host != "" {
			acceleratorHost = host
			logx.Infof("Docker Hub 源站不可达，docker.io 镜像更新检查改走加速站: %s", host)
			return
		}
		logx.Errorf("Docker Hub 源站与全部加速站均不可达，更新检查将回退直连源站")
		acceleratorHost = DefaultRegistryHost
	})
	return acceleratorHost
}

// normalizeHubURL 把 hubURL 取值归一为 host[:port]，
// 兼容带/不带 scheme、带尾路径等写法；非法值返回空串并回退自动探测。
func normalizeHubURL(raw string) string {
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
	return u.Host
}

// checkHost 探测 registry /v2/ 端点是否可达（200/401 均视为可用）。
func checkHost(host string) bool {
	resp, err := probeHTTPClient.Get("https://" + host + "/v2/")
	if err != nil {
		logx.Debugf("探测站点不可达 %s: %v", host, err)
		return false
	}
	defer func(Body io.ReadCloser) {
		if err := Body.Close(); err != nil {
			logx.Error("checkHost关闭Body失败" + err.Error())
		}
	}(resp.Body)
	return resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusUnauthorized
}

// probeFirstReachable 并行探测 hosts，返回列表顺序最靠前的可用者；全不可用返回空串。
// 失败的站点不会回包，不能等全部返回，故设 probeTimeout+2s 的总截止时间。
func probeFirstReachable(hosts []string) string {
	type probeRes struct {
		idx int
		ok  bool
	}
	ch := make(chan probeRes, len(hosts))
	for i, host := range hosts {
		go func(i int, host string) {
			ch <- probeRes{idx: i, ok: checkHost(host)}
		}(i, host)
	}
	best := -1
	deadline := time.After(probeTimeout + 2*time.Second)
loop:
	for received := 0; received < len(hosts); {
		select {
		case r := <-ch:
			received++
			if r.ok && (best == -1 || r.idx < best) {
				best = r.idx
				if best == 0 {
					break loop
				}
			}
		case <-deadline:
			break loop
		}
	}
	if best == -1 {
		return ""
	}
	return hosts[best]
}

// ---------------------------------------------------------------------------
// 兜底加速站：hubURL 指定的镜像加速站可能只覆盖部分仓库（如 iStoreOS/linkease
// 本地缓存服务对未缓存仓库返回 403/525）。对这些仓库，更新检查通过
// FetchDigestViaFallback 用兜底源重试，避免检测结果全部落入「未知」。
// ---------------------------------------------------------------------------

var (
	fallbackOnce      sync.Once
	fallbackHostValue string
)

// FallbackHubHost 决定兜底 digest 请求使用的 host：
//  1. 可用 hubFALLBACK_URL 环境变量显式指定（如与主加速站不同的另一个站）；
//  2. 否则并行探测内置加速站列表，取列表顺序最靠前的可用者；
//  3. 全部不可用则回退源站 index.docker.io（海外环境可直接命中）。
//
// 结果经 sync.Once 缓存，进程生命周期内保持不变。
func FallbackHubHost() string {
	fallbackOnce.Do(func() {
		if host := normalizeHubURL(os.Getenv("hubFALLBACK_URL")); host != "" {
			fallbackHostValue = host
			logx.Infof("hubFALLBACK_URL 已配置，兜底 digest 检查走: %s", host)
			return
		}
		if host := probeFirstReachable(DefaultAcceleratorHostList); host != "" {
			fallbackHostValue = host
			logx.Infof("兜底 digest 检查走加速站: %s", host)
			return
		}
		fallbackHostValue = DefaultRegistryHost
		logx.Infof("全部加速站不可用，兜底 digest 检查回退源站: %s", DefaultRegistryHost)
	})
	return fallbackHostValue
}

// IsDockerHubImage 判断镜像是否属于 Docker Hub（镜像名无显式 registry 前缀）。
// 只有 Docker Hub 镜像才需要加速站兜底；ghcr.io 等其他 registry 不适用。
func IsDockerHubImage(image types.Image) bool {
	normalizedRef, err := ref.ParseNormalizedNamed(image.ImageName)
	if err != nil {
		return false
	}
	return ref.Domain(normalizedRef) == DefaultRegistryDomain
}

// buildManifestURLForHost 与 checkupdate.go 的 BuildManifestURL 等价，
// 但允许显式指定 registry host（供兜底请求使用）。
func buildManifestURLForHost(host string, image types.Image) (string, error) {
	normalizedRef, err := ref.ParseDockerRef(image.ImageName + ":" + image.ImageTag)
	if err != nil {
		return "", err
	}
	tagged, isTagged := normalizedRef.(ref.NamedTagged)
	if !isTagged {
		return "", errors.New("镜像无tag" + normalizedRef.String())
	}
	u := url.URL{
		Scheme: "https",
		Host:   host,
		Path:   fmt.Sprintf("/v2/%s/manifests/%s", ref.Path(tagged), tagged.Tag()),
	}
	return u.String(), nil
}

// GetTokenForHost 与 GetToken 等价，但允许显式指定 registry host。
// 用于兜底源：主加速站与兜底站的鉴权挑战流程各自独立。
func GetTokenForHost(host string, image types.Image, registryAuth string) (string, error) {
	normalizedRef, err := ref.ParseNormalizedNamed(image.ImageName)
	if err != nil {
		return "", err
	}

	URL := url.URL{
		Scheme: "https",
		Host:   host,
		Path:   "/v2/",
	}

	req, err := GetChallengeRequest(URL)
	if err != nil {
		return "", err
	}

	client := &http.Client{Timeout: authHTTPTimeout}
	res, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer func(Body io.ReadCloser) {
		err := Body.Close()
		if err != nil {
			logx.Error("GetTokenForHost关闭Body失败" + err.Error())
		}
	}(res.Body)

	challenge := strings.ToLower(res.Header.Get(ChallengeHeader))
	if strings.HasPrefix(challenge, "basic") {
		if registryAuth == "" {
			return "", fmt.Errorf("no credentials available")
		}
		return fmt.Sprintf("Basic %s", registryAuth), nil
	}
	if strings.HasPrefix(challenge, "bearer") {
		return GetBearerHeader(challenge, normalizedRef, registryAuth)
	}
	// 免鉴权 registry：匿名请求即可
	return "", nil
}

// FetchDigestViaFallback 用兜底加速站获取指定镜像的最新 manifest digest。
// 由 checkupdate.go 的 checkSingleImage 在主加速站回源失败时调用。
func FetchDigestViaFallback(image types.Image) (string, error) {
	host := FallbackHubHost()
	digestURL, err := buildManifestURLForHost(host, image)
	if err != nil {
		return "", err
	}
	token, err := GetTokenForHost(host, image, "")
	if err != nil {
		// 拿不到 token 时按匿名方式尝试（部分站无需鉴权）
		logx.Debugf("兜底源获取 token 失败，按匿名重试 [%s]: %v", image.ImageName, err)
		token = ""
	}
	return GetDigest(digestURL, token)
}

// GetRegistryAddress 解析镜像引用所属的 registry 地址。
//
//   - 镜像名显式带 registry（如 ghcr.io/x/y、docker.1ms.run/x/y）时原样返回，
//     用户想走哪个镜像源就在镜像名里指定；
//   - 未带 registry 的官方镜像（如 nginx:latest）归一到 Docker Hub，
//     检查更新实际使用的 host 由 resolveHubHost 决定：
//     hubURL 环境变量 > 直连源站 > 加速站自动探测。
//
// 注意：加速站只影响更新检查（token/manifest 元数据请求）；实际拉取镜像
// 始终由 Docker daemon 完成，走宿主机 daemon.json 的 registry-mirrors 配置，
// 与原版行为一致。
func GetRegistryAddress(imageRef string) (string, error) {
	normalizedRef, err := ref.ParseNormalizedNamed(imageRef)
	if err != nil {
		return "", err
	}
	address := ref.Domain(normalizedRef)
	if address == DefaultRegistryDomain {
		address = resolveHubHost()
	}
	return address, nil
}
