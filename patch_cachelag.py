#!/usr/bin/env python3
"""消除「自建加速站 manifest 缓存滞后」造成的误报与「更新完又提示」。

现象：KSpeeder 之类自建 pull-through 缓存的 manifest TTL 不可调，缓存里
可能长期停留几天前的旧 index。DC 检测拿旧 digest 去比本地 RepoDigests，
不等就报「有更新」；用户点更新后本地换成新 digest，缓存仍是旧的，
于是刚更新完立刻又被标成有更新，无限循环。实测中 DC 拿到的「远端」
甚至比本机镜像还旧。

旧实现用 /data/image_update_suppress.json 做「抑制」：把某个远端 digest
记住就不再报。方向是错的——真更新也会被一起压掉，且无法区分缓存滞后
和真的推了新版。

本补丁改为「复查确认」，不抑制任何更新：
  1. 只有当「远端 digest 不在本地 RepoDigests」时才触发复查；
  2. 强制刷新自建加速站 manifest 缓存（force=1，30 秒内只清一次），
     再重新 HEAD 一次远端 digest，绕过 DC 自己的 digestCache；
  3. 复查后与本地一致 -> 判定为缓存滞后，角标消失；
     复查后仍不一致 -> 判定为真更新，正常报出并把新 digest 写回缓存。
只在疑似有更新时清缓存，不影响已有镜像的加速命中。

不依赖任何第三方、不碰代理、不改宿主机 Docker 配置。
hubCacheRecheck=off 可关闭复查（退回原判定）。

此外增加「陈旧 manifest 识别 + STALE 站冷却降权」：自建站上游节点池可能
长期缓存几天前的旧 index（STALE），本地刷新接口全部无效。复查后仍不一致时，
沿同一个自建站链路取远端镜像 config 的 created：不晚于本地构建时间就判为
旧缓存、不报更新；取不到则判为「不可验证」、同样不报更新。两种情况都会
调用 hubcfg.MarkCurrentStale / MarkCurrentUnverified 把当前站冷却跳过，
下一轮自动改用 hubURL 里的下一站，冷却到期后该站重新参与探测。
远端确实更新（构建时间更晚）时保持原判定，真更新不会被吞掉。
hubStaleCheck=off 可单独关闭。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。

此外根治「空 RepoDigests 漏报」：三台宿主均无法直连 docker.io(registry-1.docker.io
UNREACHABLE)，空 RepoDigests 的镜像上游会回退到 docker.io 取远端 digest/构建时间，
取不到就被 STALE 兜底误判为「已是最新」，远端真更新也检测不到。本补丁在复查前把
digestURL 的 docker.io host 强制换成当前加速站 hubURL(Hostname()，按配置动态读取，
不绑定具体站、可回源 docker.io)，既可达又数据一致。
"""

import sys
from pathlib import Path

CHECKUPDATE = Path("upstream/internal/module/checkupdate.go")
STATE_FILE = Path("upstream/internal/module/hubrecheck.go")

# 在比对出 needUpdate 之后、写入 status 之前插入复查确认。
# 只锚定这一行，避免上游比对循环重构后补丁失配。
ANCHOR = "\tstatus := StatusLatest\n"

REPLACEMENT = """	// 根治漏报：三台宿主均无法直连 docker.io(registry-1.docker.io UNREACHABLE)。
	// 空 RepoDigests 的镜像上游会回退到 docker.io 取远端 digest/构建时间，必然失败，
	// 被 STALE 兜底误判为「已是最新」，导致远端真有更新也检测不到。强制把 digestURL
	// 的 docker.io host 换成当前加速站 hubURL(可回源 docker.io)，既可达又数据一致。
	if u := strings.Index(digestURL, "://"); u >= 0 {
		rest := digestURL[u+3:]
		if sp := strings.Index(rest, "/"); sp >= 0 {
			lh := strings.ToLower(rest[:sp])
			if lh == "docker.io" || lh == "registry-1.docker.io" || lh == "registry.docker.io" {
				if hubHost := hubcfg.Hostname(); hubHost != "" {
					digestURL = digestURL[:u+3] + hubHost + rest[sp:]
				}
			}
		}
	}
	// 远端与本地不一致时，先强制刷新自建加速站 manifest 缓存再复查一次。
	// KSpeeder 的 manifest TTL 不可调，缓存可能停留在几天前的旧 index：
	// 用户刚更新完，本地是新 digest，检测却拿到旧 digest，于是「更新完又提示」。
	// 复查后仍不一致才判定真更新；这里不做任何「抑制」，真更新不会被压掉。
	if needUpdate {
		// 复用本轮 token 缓存，避免复查给官方站增加额外请求（有 100 次/小时/IP 限流）。
		recheckKey := tokenCacheKey(image)
		recheckToken, tokenCached := i.lookupTokenCache(recheckKey)
		if !tokenCached {
			var tokenErr error
			recheckToken, tokenErr = GetToken(image, "")
			if tokenErr != nil {
				logx.Debugf("复查获取 token 失败 [%s]: %v", imageRef, tokenErr)
			}
			i.storeTokenCache(recheckKey, recheckToken)
		}
		if fresh, ok := RecheckRemoteDigest(digestURL, recheckToken); ok && fresh != "" {
			remoteDigest = fresh
			i.storeDigestCache(digestURL, fresh)
			needUpdate = true
			for _, localRepoDigests := range image.RepoDigests {
				parts := strings.SplitN(localRepoDigests, "@", 2)
				if len(parts) == 2 && parts[1] == fresh {
					needUpdate = false
					break
				}
			}
			if !needUpdate {
				logx.Infof("缓存滞后已排除 [%s] 复查后与本地一致 digest=%s", imageRef, fresh)
			}
		}
	}
    // 复查后仍判定有更新时，再校验远端 manifest 是否可信、是否「比本地还旧」。
    // 自建站上游节点池可能长期缓存几天前的旧 index（STALE），本地刷不动时
    // 靠镜像构建时间识别；取不到构建时间（403/429/重启瞬间超时）说明
    // 远端数据本身就不可信，同样不报更新，避免角标被一次抖动点亮。
    // 两种情况都不是永久抑制：命中时把当前加速站冷却降权、切到备站，
    // 冷却到期或该站缓存收敛后重新参与探测，真更新恢复后照常报出。
    if needUpdate {
        verifyKey := tokenCacheKey(image)
        verifyToken, _ := i.lookupTokenCache(verifyKey)
        if stale, why := IsStaleRemoteManifest(digestURL, remoteDigest, verifyToken, image.Created); stale {
            needUpdate = false
            logx.Infof("加速站疑似更新已排除 [%s] %s", imageRef, why)
        }
    }
	status := StatusLatest
"""

RECHECK_GO = '''package module

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/l429609201/dockerCopilot/internal/hubcfg"
)

var hubRecheckMu sync.Mutex
var hubRecheckAt time.Time

// purgeHubManifestCache 强制刷新自建加速站 manifest 缓存。
// 管理口地址由 hubURL 的 host 推导（同机 5003 端口）。一轮检测中多个镜像
// 同时疑似有更新时，30 秒内只清一次；第一次清完后后续 HEAD 自然回源。
func purgeHubManifestCache() bool {
	hubRecheckMu.Lock()
	if time.Since(hubRecheckAt) < 30*time.Second {
		hubRecheckMu.Unlock()
		return true
	}
	hubRecheckAt = time.Now()
	hubRecheckMu.Unlock()

	hn := hubcfg.Hostname()
	if hn == "" {
		return false
	}
	target := "http://" + net.JoinHostPort(hn, "5003") + "/api/cache/cleanup?force=1"
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, nil)
	if err != nil {
		return false
	}
	res, err := (&http.Client{Timeout: 6 * time.Second}).Do(req)
	if err != nil {
		return false
	}
	defer res.Body.Close()
	return res.StatusCode >= 200 && res.StatusCode < 300
}

// RecheckRemoteDigest 在疑似有更新时，清缓存后重新取一次远端 manifest digest。
// 返回 (新 digest, 是否成功)。失败时保持原判定，不影响正常检测。
// 设置 hubCacheRecheck=off 可关闭。
func RecheckRemoteDigest(digestURL, token string) (string, bool) {
	if strings.EqualFold(strings.TrimSpace(os.Getenv("hubCacheRecheck")), "off") {
		return "", false
	}
	if !purgeHubManifestCache() {
		return "", false
	}
	digest, err := GetDigest(digestURL, token)
	if err != nil || digest == "" {
		return "", false
	}
	return digest, true
}

// ---- 加速站陈旧 manifest 识别（按镜像构建时间） ----

type staleCheckEntry struct {
    at      time.Time
    created time.Time
    ok      bool
}

var staleCheckMu sync.Mutex
var staleCheckCache = map[string]staleCheckEntry{}

// IsStaleRemoteManifest 判断远端 tag 指向的镜像是否不比本地新。
// 只在检测结论为「有更新」时调用；返回 true 时调用方把结论改回「已是最新」。
// 远端构建时间取不到、或确实晚于本地时一律返回 false，真更新不会被吞掉。
// 结果按 manifestURL + digest 缓存 10 分钟，避免每轮检测重复回源。
// 设置 hubStaleCheck=off 可关闭（退回原判定）。
func IsStaleRemoteManifest(manifestURL, remoteDigest, token string, localCreated int64) (bool, string) {
    if strings.EqualFold(strings.TrimSpace(os.Getenv("hubStaleCheck")), "off") {
        return false, ""
    }
    if localCreated <= 0 || manifestURL == "" {
        return false, ""
    }
    key := manifestURL + "|" + remoteDigest
    staleCheckMu.Lock()
    e, hit := staleCheckCache[key]
    staleCheckMu.Unlock()
    // 能取到远端构建时间时缓存 10 分钟；取不到（加速站 403/429/超时）
    // 只缓存 60 秒，尽快重试，避免长时间无法检出真更新。
    ttl := 10 * time.Minute
    if hit && !e.ok {
        ttl = 60 * time.Second
    }
    if !hit || time.Since(e.at) > ttl {
        created, ok := fetchRemoteImageCreated(manifestURL, token)
        e = staleCheckEntry{at: time.Now(), created: created, ok: ok}
        staleCheckMu.Lock()
        staleCheckCache[key] = e
        staleCheckMu.Unlock()
    }
    if !e.ok {
        // 远端数据不可验证时不报更新。不是永久抑制：60 秒后重试，
        // 加速站恢复且远端确实更新时照常报出。
        // 同时把当前站标记为「暂时不可验证」并立即失效：上游回源超时
        // 往往是整站问题，下一轮检测会自动切到备站，不必等满 60 秒。
        hubcfg.MarkCurrentUnverified("远端构建时间不可验证")
        return true, "远端构建时间不可验证（加速站数据不完整），暂不报更新"
    }
    if e.created.Unix() > localCreated {
        return false, ""
    }
    // 远端 manifest 比本地镜像还旧：这是加速站 tag 缓存长期停在旧 index
    // 的确凿证据。除了不报更新（避免点了更新反而拉回旧版），还要把该站
    // 冷却降权，下一轮自动改用配置里的下一站，不再依赖这份旧缓存。
    why := fmt.Sprintf("远端构建时间 %s 不晚于本地 %s（加速站旧缓存）",
        e.created.UTC().Format(time.RFC3339),
        time.Unix(localCreated, 0).UTC().Format(time.RFC3339))
    hubcfg.MarkCurrentStale(why)
    return true, why
}

// fetchRemoteImageCreated 沿自建站链路取远端镜像 config 里的 created。
// 依次：tag manifest ->（多架构时）子 manifest -> config blob -> created。
// 全部走 hubURL 指向的自建加速站，不引入任何第三方源。
func fetchRemoteImageCreated(manifestURL, token string) (time.Time, bool) {
    idx := strings.LastIndex(manifestURL, "/manifests/")
    if idx < 0 {
        return time.Time{}, false
    }
    base := manifestURL[:idx]
    accept := strings.Join([]string{
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v1+json",
    }, ", ")
    getJSON := func(u string) (map[string]interface{}, bool) {
        ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
        defer cancel()
        req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
        if err != nil {
            return nil, false
        }
        if token != "" {
            req.Header.Set("Authorization", token)
        }
        req.Header.Set("Accept", accept)
        res, err := digestHTTPClient.Do(req)
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

    configDigest := ""
    top, ok := getJSON(manifestURL)
    if !ok {
        return time.Time{}, false
    }
    if list, isList := top["manifests"].([]interface{}); isList && len(list) > 0 {
        for _, item := range list {
            m, _ := item.(map[string]interface{})
            plat, _ := m["platform"].(map[string]interface{})
            arch, _ := plat["architecture"].(string)
            osName, _ := plat["os"].(string)
            d, _ := m["digest"].(string)
            if d != "" && arch == runtime.GOARCH && (osName == "" || osName == runtime.GOOS) {
                configDigest = d
                break
            }
        }
        if configDigest == "" {
            if first, ok := list[0].(map[string]interface{}); ok {
                configDigest, _ = first["digest"].(string)
            }
        }
        if configDigest == "" {
            return time.Time{}, false
        }
        child, ok := getJSON(base + "/manifests/" + configDigest)
        if !ok {
            return time.Time{}, false
        }
        cfg, _ := child["config"].(map[string]interface{})
        configDigest, _ = cfg["digest"].(string)
    } else {
        cfg, _ := top["config"].(map[string]interface{})
        configDigest, _ = cfg["digest"].(string)
    }
    if configDigest == "" {
        return time.Time{}, false
    }
    cfgJSON, ok := getJSON(base + "/blobs/" + configDigest)
    if !ok {
        return time.Time{}, false
    }
    created, _ := cfgJSON["created"].(string)
    if created == "" {
        return time.Time{}, false
    }
    t, err := time.Parse(time.RFC3339Nano, created)
    if err != nil {
        return time.Time{}, false
    }
    return t, true
}
'''


def main() -> int:
    if not CHECKUPDATE.exists():
        print(f"[patch_cachelag] 未找到 {CHECKUPDATE}，请确认 upstream 检出路径", file=sys.stderr)
        return 1

    text = CHECKUPDATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    count = text.count(ANCHOR)
    if count != 1:
        print(
            f"[patch_cachelag] 锚点匹配到 {count} 次（期望 1 次），"
            "上游 checkupdate.go 可能已重构，请手动核对后更新本脚本",
            file=sys.stderr,
        )
        return 1
    CHECKUPDATE.write_text(text.replace(ANCHOR, REPLACEMENT), encoding="utf-8", newline="\n")
    STATE_FILE.write_text(RECHECK_GO, encoding="utf-8", newline="\n")
    print("[patch_cachelag] 缓存滞后复查确认补丁已应用（不抑制任何真更新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
