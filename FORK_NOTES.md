# Fork Notes

本仓库是 `cft0808/edict` 的个人 Fork，保留了本地工作流与三省六部扩展。

## Remotes

- `origin`: `https://github.com/totma/edict.git`
- `upstream`: `https://github.com/cft0808/edict.git`

检查远端：

```bash
git remote -v
```

## 日常同步

先抓取上游：

```bash
git fetch upstream --prune
```

查看本地与上游差异：

```bash
git log --oneline --left-right HEAD...upstream/main
git diff --stat upstream/main
```

如果只想吸收稳定改进，优先按文件挑选合入，而不是直接整体 merge：

```bash
git restore --source=upstream/main -- Dockerfile docker-compose.yml scripts/run_loop.sh scripts/skill_manager.py
```

完成后提交到自己的分支：

```bash
git checkout -b feat/<topic>
git add -A
git commit -m "feat: <summary>"
git push -u origin feat/<topic>
```

## 本 Fork 当前保留的本地定制

- 动态模型升降级链路：`scripts/model_escalation.py`
- 史官缓冲/归档钩子：`scripts/shiguan_hooks.py`
- 新增史官 Agent：`agents/shiguan/SOUL.md`
- 前端时间修复模块：`edict/frontend/src/time.ts`
- 看板与调度增强：`dashboard/server.py`、`scripts/kanban_update.py`、`scripts/sync_from_openclaw_runtime.py`

这些文件与上游差异较大，后续同步时建议逐文件比对后再合入。

## 本次已吸收的上游改进

- `Dockerfile`：补充跨平台构建/运行平台声明
- `docker-compose.yml`：增加 `platform` 配置，减少架构不匹配问题
- `scripts/run_loop.sh`：给循环脚本增加超时保护，避免单脚本卡死拖住整轮刷新
- `scripts/skill_manager.py`：增强下载超时、重试与失败提示

## 建议工作流

- 先在自己的功能分支提交本地改动
- 再按文件吸收上游改进
- 每次同步后至少做一轮基础验证
- 避免把 `.runtime/`、临时日志和本机状态文件提交到仓库

## 基础验证命令

```bash
python3 -m py_compile dashboard/server.py scripts/kanban_update.py scripts/apply_model_changes.py scripts/sync_from_openclaw_runtime.py scripts/skill_manager.py
bash -n scripts/run_loop.sh
```
