#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备份页显示旧版「容器配置快照」补丁。

在 build.yml 中于「检出上游源码」之后执行，仅做精确字符串替换：
  - frontend-react/src/components/Backups.jsx
        上游把 Web 备份页改成只渲染 <DataBackups />（新数据归档），旧配置快照
        (backup-*.json/.yaml) 虽仍在 /data/backups 且后端 /container/listBackups、
        /container/backups(delete) 接口都保留，却不再出现在 Web 上。
        本补丁把 Backups 组件改回「容器配置快照区块(显示/下载/删除) + <DataBackups/>」，
        让两类备份都在 Web 备份页可见、可管理。
  - frontend-react/src/api/client.js
        补 containerAPI.downloadBackup(filename)，下载旧配置快照（blob）。
  - internal/handler/container/delrestorehandler.go
        新增 DownloadBackupHandler：GET /container/backups/download?filename=...，
        复用 utiles.ResolveBackupPath(".json",".yaml") 防目录穿越，流式返回文件。
  - internal/handler/routes.go
        注入 GET /container/backups/download 路由。

旧快照文件名格式（见 utiles/backupcontainer.go）：
  本地           backup-2006-01-02.json
  非本地主机     backup-<hostID>-2006-01-02.json
前端 parseName 兼容这两种。

任何一处匹配数量不符预期即令构建失败——说明上游结构变了，需更新本脚本。
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "upstream")

# (相对路径, 原字符串, 新字符串, 期望出现次数)
PATCHES = [

    # 1) Backups.jsx：旧快照区块 + 保留 DataBackups
    (
        "frontend-react/src/components/Backups.jsx",
        "import React from 'react'\n"
        "import { DataBackups } from './DataBackups.jsx'\n"
        "\n"
        "// 备份页只保留实际数据归档，不再请求或展示旧配置快照。\n"
        "export function Backups() {\n"
        "  return <DataBackups />\n"
        "}",
        "import React, { useCallback, useEffect, useState } from 'react'\n"
        "import { containerAPI } from '../api/client.js'\n"
        "import { DataBackups } from './DataBackups.jsx'\n"
        "import { Download, Trash2, RefreshCw, Loader2, FileJson } from 'lucide-react'\n"
        "\n"
        "// 备份页同时展示两类：旧版「容器配置快照」(backup-*.json/.yaml，定时/指令触发)\n"
        "// 与新版「数据归档」(DataBackups)。两者存储互不相干，此处并列呈现。\n"
        "export function Backups() {\n"
        "  const [snapshots, setSnapshots] = useState([])\n"
        "  const [loading, setLoading] = useState(true)\n"
        "  const [error, setError] = useState('')\n"
        "  const [dlId, setDlId] = useState('')\n"
        "  const [delTarget, setDelTarget] = useState(null)\n"
        "  const [deleting, setDeleting] = useState(false)\n"
        "\n"
        "  const loadSnapshots = useCallback(async () => {\n"
        "    setLoading(true)\n"
        "    try {\n"
        "      const r = await containerAPI.listBackups()\n"
        "      if (r.data?.code !== 200) throw new Error(r.data?.msg || '请求失败')\n"
        "      setSnapshots(r.data.data || [])\n"
        "      setError('')\n"
        "    } catch (e) { setError(e.response?.data?.msg || e.message) }\n"
        "    finally { setLoading(false) }\n"
        "  }, [])\n"
        "\n"
        "  useEffect(() => { loadSnapshots() }, [loadSnapshots])\n"
        "\n"
        "  // backup-2006-01-02.json 或 backup-<hostID>-2006-01-02.json\n"
        "  const parseName = (name) => {\n"
        "    const m = name.match(/^backup-(?:([^-]+)-)?(\\d{4}-\\d{2}-\\d{2})\\.(json|yaml|yml)$/)\n"
        "    if (!m) return { date: name, host: '' }\n"
        "    return { date: m[2], host: m[1] || '本地' }\n"
        "  }\n"
        "\n"
        "  const download = async (name) => {\n"
        "    setDlId(name); setError('')\n"
        "    try {\n"
        "      const response = await containerAPI.downloadBackup(name)\n"
        "      const url = URL.createObjectURL(response.data)\n"
        "      const link = document.createElement('a')\n"
        "      link.href = url; link.download = name\n"
        "      document.body.appendChild(link); link.click(); link.remove()\n"
        "      setTimeout(() => URL.revokeObjectURL(url), 60000)\n"
        "    } catch (e) { setError(e.response?.data?.msg || e.message) }\n"
        "    finally { setDlId('') }\n"
        "  }\n"
        "\n"
        "  const remove = async () => {\n"
        "    if (!delTarget || deleting) return\n"
        "    setDeleting(true)\n"
        "    try { await containerAPI.deleteBackup(delTarget); setDelTarget(null); await loadSnapshots() }\n"
        "    catch (e) { setError(e.response?.data?.msg || e.message) }\n"
        "    finally { setDeleting(false) }\n"
        "  }\n"
        "\n"
        "  return (\n"
        "    <div className=\"w-full px-2 sm:px-6 py-4 text-gray-900 dark:text-gray-100\">\n"
        "      <header className=\"flex flex-wrap items-center justify-between gap-3 pb-5\">\n"
        "        <div className=\"flex items-center gap-3\"><FileJson className=\"h-6 w-6 text-emerald-600\" /><h2 className=\"text-xl font-semibold\">容器配置快照</h2><span className=\"text-sm text-gray-500\">{snapshots.length} 个 · 旧版定时/指令备份</span></div>\n"
        "        <button onClick={loadSnapshots} disabled={loading} title=\"刷新\" aria-label=\"刷新\" className=\"inline-flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40\"><RefreshCw className={`${'h-4 w-4 '}${loading ? 'animate-spin' : ''}`} /></button>\n"
        "      </header>\n"
        "      {error && <div role=\"alert\" className=\"mb-3 rounded-lg bg-red-50 dark:bg-red-950/30 px-3 py-2 text-sm text-red-600 break-words\">{error}</div>}\n"
        "      <div className=\"overflow-x-auto\">\n"
        "        <table className=\"w-full min-w-[560px] text-left text-sm\">\n"
        "          <thead className=\"text-xs text-gray-500 bg-gray-50 dark:bg-gray-800/50\"><tr>{['文件名', '日期', '来源', '操作'].map((l) => <th key={l} className=\"px-3 py-3 font-medium\">{l}</th>)}</tr></thead>\n"
        "          <tbody className=\"divide-y divide-gray-200 dark:divide-gray-700\">\n"
        "            {snapshots.map((name) => { const p = parseName(name); return (\n"
        "              <tr key={name} className=\"hover:bg-gray-50 dark:hover:bg-gray-800/40\">\n"
        "                <td className=\"px-3 py-4 font-mono break-all\">{name}</td>\n"
        "                <td className=\"px-3 py-4 whitespace-nowrap\">{p.date}</td>\n"
        "                <td className=\"px-3 py-4 whitespace-nowrap\">{p.host}</td>\n"
        "                <td className=\"px-3 py-4\"><div className=\"flex gap-1\"><button onClick={() => download(name)} disabled={!!dlId} title=\"下载\" aria-label=\"下载\" className=\"inline-flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40\">{dlId === name ? <Loader2 className=\"h-4 w-4 animate-spin\" /> : <Download className=\"h-4 w-4\" />}</button><button onClick={() => { setError(''); setDelTarget(name) }} title=\"删除\" aria-label=\"删除\" className=\"inline-flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 hover:text-red-600\"><Trash2 className=\"h-4 w-4\" /></button></div></td>\n"
        "              </tr>\n"
        "            )})}\n"
        "          </tbody>\n"
        "        </table>\n"
        "      </div>\n"
        "      {loading && !snapshots.length ? <div className=\"flex items-center justify-center gap-2 py-12 text-sm text-gray-500\"><Loader2 className=\"h-4 w-4 animate-spin\" />加载快照</div> : !snapshots.length && <div className=\"flex flex-col items-center py-12 text-center\"><FileJson className=\"h-8 w-8 text-gray-300 dark:text-gray-600 mb-3\" /><h3 className=\"font-medium\">暂无容器配置快照</h3><p className=\"mt-1 text-xs text-gray-400\">定时或手动触发的容器配置备份会出现在此处</p></div>}\n"
        "      {delTarget && <div className=\"fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-3 sm:p-6\" onClick={(e) => { if (e.target === e.currentTarget && !deleting) setDelTarget(null) }}><div role=\"alertdialog\" aria-label=\"删除快照\" className=\"max-w-sm rounded-lg bg-white dark:bg-gray-800 p-5 shadow-xl\"><h3 className=\"font-semibold\">删除快照</h3><p className=\"text-sm text-gray-500 leading-6 my-4\">删除 {delTarget}？此操作不可恢复。</p><div className=\"flex justify-end gap-3\"><button disabled={deleting} onClick={() => setDelTarget(null)} className=\"text-sm text-gray-500\">取消</button><button disabled={deleting} onClick={remove} className=\"inline-flex gap-2 items-center rounded-lg bg-red-600 text-white px-4 py-2 text-sm\">{deleting ? <Loader2 className=\"h-4 w-4 animate-spin\" /> : <Trash2 className=\"h-4 w-4\" />}删除</button></div></div></div>}\n"
        "      <div className=\"mt-8 border-t border-gray-200 dark:border-gray-700 pt-6\">\n"
        "        <DataBackups />\n"
        "      </div>\n"
        "    </div>\n"
        "  )\n"
        "}",
        1,
    ),

    # 2) client.js：补 downloadBackup
    (
        "frontend-react/src/api/client.js",
        "  deleteBackup: (filename) => apiClient.delete(`/api/container/backups?filename=${encodeURIComponent(filename)}`),",
        "  deleteBackup: (filename) => apiClient.delete(`/api/container/backups?filename=${encodeURIComponent(filename)}`),\n"
        "  downloadBackup: (filename) => apiClient.get(`/api/container/backups/download?filename=${encodeURIComponent(filename)}`, { responseType: 'blob', timeout: 0 }),",
        1,
    ),

    # 3) delrestorehandler.go：import 追加依赖（标准库插在 net/http 前；utiles 插在 types 后）
    (
        "internal/handler/container/delrestorehandler.go",
        "\t\"net/http\"\n",
        "\t\"fmt\"\n"
        "\t\"io\"\n"
        "\t\"net/http\"\n"
        "\t\"os\"\n"
        "\t\"path/filepath\"\n"
        "\t\"strconv\"\n",
        1,
    ),
    (
        "internal/handler/container/delrestorehandler.go",
        "\t\"github.com/l429609201/dockerCopilot/internal/types\"\n",
        "\t\"github.com/l429609201/dockerCopilot/internal/types\"\n"
        "\t\"github.com/l429609201/dockerCopilot/internal/utiles\"\n",
        1,
    ),
    (
        "internal/handler/container/delrestorehandler.go",
        "\t\t} else {\n"
        "\t\t\thttpx.WriteJson(w, resp.Code, resp)\n"
        "\t\t}\n"
        "\t}\n"
        "}",
        "\t\t} else {\n"
        "\t\t\thttpx.WriteJson(w, resp.Code, resp)\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
        "\n"
        "// DownloadBackupHandler 下载备份文件（旧配置快照）。\n"
        "// 仅接受 .json/.yaml，复用 utiles.ResolveBackupPath 做防穿越校验。\n"
        "func DownloadBackupHandler(svcCtx *svc.ServiceContext) http.HandlerFunc {\n"
        "\treturn func(w http.ResponseWriter, r *http.Request) {\n"
        "\t\tfilename := r.URL.Query().Get(\"filename\")\n"
        "\t\tfull, err := utiles.ResolveBackupPath(filename, \".json\", \".yaml\")\n"
        "\t\tif err != nil {\n"
        "\t\t\thttpx.ErrorCtx(r.Context(), w, err)\n"
        "\t\t\treturn\n"
        "\t\t}\n"
        "\t\tf, err := os.Open(full)\n"
        "\t\tif err != nil {\n"
        "\t\t\thttpx.ErrorCtx(r.Context(), w, err)\n"
        "\t\t\treturn\n"
        "\t\t}\n"
        "\t\tdefer f.Close()\n"
        "\t\tinfo, err := f.Stat()\n"
        "\t\tif err != nil {\n"
        "\t\t\thttpx.ErrorCtx(r.Context(), w, err)\n"
        "\t\t\treturn\n"
        "\t\t}\n"
        "\t\tw.Header().Set(\"Content-Type\", \"application/octet-stream\")\n"
        "\t\tw.Header().Set(\"Content-Disposition\", fmt.Sprintf(\"attachment; filename=%q\", filepath.Base(full)))\n"
        "\t\tw.Header().Set(\"Content-Length\", strconv.FormatInt(info.Size(), 10))\n"
        "\t\tio.Copy(w, f)\n"
        "\t}\n"
        "}",
        1,
    ),

    # 4) routes.go：注入 GET /container/backups/download 路由（缩进 3 个 tab）
    (
        "internal/handler/routes.go",
        "\t\t\t{\n"
        "\t\t\t\tMethod:  http.MethodDelete,\n"
        "\t\t\t\tPath:    \"/container/backups\",\n"
        "\t\t\t\tHandler: container.DelRestoreHandler(serverCtx),\n"
        "\t\t\t},",
        "\t\t\t{\n"
        "\t\t\t\tMethod:  http.MethodGet,\n"
        "\t\t\t\tPath:    \"/container/backups/download\",\n"
        "\t\t\t\tHandler: container.DownloadBackupHandler(serverCtx),\n"
        "\t\t\t},\n"
        "\t\t\t{\n"
        "\t\t\t\tMethod:  http.MethodDelete,\n"
        "\t\t\t\tPath:    \"/container/backups\",\n"
        "\t\t\t\tHandler: container.DelRestoreHandler(serverCtx),\n"
        "\t\t\t},",
        1,
    ),
]


def main():
    ok = True
    for rel, anchor, repl, expect in PATCHES:
        p = ROOT / rel
        if not p.exists():
            sys.stderr.write(f"[FAIL] 文件不存在: {p}\n")
            ok = False
            continue
        text = p.read_text(encoding="utf-8")
        cnt = text.count(anchor)
        if cnt != expect:
            sys.stderr.write(f"[FAIL] 锚点匹配数 {cnt} != {expect} : {rel}\n")
            ok = False
            continue
        text = text.replace(anchor, repl, 1)
        p.write_text(text, encoding="utf-8")
        print(f"[OK] {rel}  (替换 {cnt} 处)")
    if not ok:
        sys.exit(2)
    print("patch_backups 完成")


if __name__ == "__main__":
    main()
