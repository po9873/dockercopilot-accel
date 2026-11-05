#!/usr/bin/env python3
"""抑制「自建加速站 manifest 缓存滞后」造成的「更新完又提示」死循环。

现象：KSpeeder 之类自建 pull-through 缓存的 manifest TTL 不可调，缓存里
可能长期停留旧 digest。用户点更新后本地 RepoDigests 变成新值，而远端
检测仍返回旧 digest；上游 checkupdate.go 的判据是「远端 digest 不在本地
RepoDigests 里就算有更新」，于是刚更新完立刻又被标成有更新，无限循环。

本补丁在 checkSingleImage 的比对前插入一个轻量状态机（状态持久化到
/data，DC 容器重启不丢）：
  - 本地 digest 发生变化  -> 说明用户刚更新过，把当前远端 digest 记为「已抑制」；
  - 远端仍返回同一 digest  -> 不再报更新；
  - 远端 digest 发生变化  -> 缓存刷新或真有新版本，清除抑制，恢复正常判定。

不依赖任何第三方、不碰代理、不改宿主机 Docker 配置。
锚点未命中（上游重构）时以非零码退出，让构建失败并通知维护者。
"""

import sys
from pathlib import Path

CHECKUPDATE = Path("upstream/internal/module/checkupdate.go")
STATE_FILE = Path("upstream/internal/module/update_suppress.go")

# 在「远端 digest 为空」检查之后、正式比对之前插入抑制判断。
ANCHOR = """	if remoteDigest == "" {
		logx.Errorf("远端返回的 digest 为空 [%s]", imageRef)
		markUnknown("远端返回的 digest 为空")
		return
	}
"""

REPLACEMENT = """	if remoteDigest == "" {
		logx.Errorf("远端返回的 digest 为空 [%s]", imageRef)
		markUnknown("远端返回的 digest 为空")
		return
	}

	// 自建加速站 manifest 缓存可能长期停留在旧 digest，与刚更新的本地镜像
	// 永远不相等，导致「更新完又提示」死循环。这里按镜像引用记录本地/远端
	// digest：本地变化即抑制同一远端 digest，直到远端真的变了才恢复判定。
	localDigestKey := strings.Join(image.RepoDigests, ",")
	if ReconcileUpdateState(imageRef, remoteDigest, localDigestKey) {
		i.setResult(image.ID, ImageCheckList{NeedUpdate: false, Status: StatusLatest})
		return
	}
"""

STATE_GO = '''package module

import (
	"encoding/json"
	"os"
	"sync"
)

// 加速站 manifest 缓存滞后抑制状态（DC 容器内 /data 为持久化卷）。
const updateSuppressPath = "/data/image_update_suppress.json"

type updateSuppressState struct {
	LastLocal  map[string]string `json:"lastLocal"`
	Suppressed map[string]string `json:"suppressed"`
}

type updateSuppressStore struct {
	mu    sync.Mutex
	state updateSuppressState
}

var imageUpdateSuppress = newUpdateSuppressStore()

func newUpdateSuppressStore() *updateSuppressStore {
	s := &updateSuppressStore{state: updateSuppressState{
		LastLocal:  map[string]string{},
		Suppressed: map[string]string{},
	}}
	b, err := os.ReadFile(updateSuppressPath)
	if err != nil {
		return s
	}
	var loaded updateSuppressState
	if json.Unmarshal(b, &loaded) != nil {
		return s
	}
	if loaded.LastLocal != nil {
		s.state.LastLocal = loaded.LastLocal
	}
	if loaded.Suppressed != nil {
		s.state.Suppressed = loaded.Suppressed
	}
	return s
}

func (s *updateSuppressStore) save() {
	b, err := json.Marshal(s.state)
	if err != nil {
		return
	}
	// /data 不存在（本地构建/测试）时忽略写失败，内存状态仍然有效。
	_ = os.WriteFile(updateSuppressPath, b, 0644)
}

// ReconcileUpdateState 返回本轮是否应抑制「有更新」提示。
// ref 为镜像名:tag，remoteDigest 为加速站返回的 digest，localDigestKey
// 为本地 RepoDigests 拼接值。返回 true 表示本地已更新过且远端仍是同一
// （可能滞后的）digest，不应再报更新。
func (s *updateSuppressStore) ReconcileUpdateState(ref, remoteDigest, localDigestKey string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()

	changed := false
	prevLocal, known := s.state.LastLocal[ref]
	if known && localDigestKey != "" && prevLocal != "" && localDigestKey != prevLocal {
		// 本地镜像刚被更新过：记住当前远端 digest，避免缓存滞后反复误报。
		if remoteDigest != "" {
			s.state.Suppressed[ref] = remoteDigest
			changed = true
		}
	}
	if !known || s.state.LastLocal[ref] != localDigestKey {
		s.state.LastLocal[ref] = localDigestKey
		changed = true
	}

	if sup, ok := s.state.Suppressed[ref]; ok {
		if sup == remoteDigest {
			if changed {
				s.save()
			}
			return true
		}
		// 远端 digest 变化：缓存已刷新或确实推了新版本，恢复正常判定。
		delete(s.state.Suppressed, ref)
		changed = true
	}
	if changed {
		s.save()
	}
	return false
}

// ReconcileUpdateState 供 checkupdate.go 调用（同包，无需 import）。
func ReconcileUpdateState(ref, remoteDigest, localDigestKey string) bool {
	return imageUpdateSuppress.ReconcileUpdateState(ref, remoteDigest, localDigestKey)
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
    STATE_FILE.write_text(STATE_GO, encoding="utf-8", newline="\n")
    print("[patch_cachelag] 加速站缓存滞后抑制补丁已应用（状态持久化到 /data）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
