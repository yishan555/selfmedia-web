# selfmedia-web

这是 hermes-selfmedia 的跨机内容归档与工作台镜像。

```powershell
pwsh -File open-workbench.ps1 -Port 5051
```

浏览器访问 http://127.0.0.1:5051。服务只绑定回环地址；发布始终关闭。

- `YYYY-MM-DD/`：按日计划、正文、配图、审校、回执和 revision。
- `library/`：素材收件箱与证据就绪选题。
- `apps/workbench/`、`skills/content-engine/`、`tools/skill-packager/`：源码镜像，由生产机生成，不在此直接修改。
- `bundles/codex|claude|hermes/`：预构建的五角色技能包。
- `_pipeline/`：脱敏后的任务/claim/事件/回执摘要，不含 token、owner 会话或原始工作包。
- `_logs/`：归档同步日志。
- `_sync/`：同步基线、日期索引和最近一次同步摘要。

生产机每天写作前和最后一次审校后运行：

```powershell
py -3.12 -B -m apps.workbench --workspace "<本仓库绝对路径>" archive --repo "<本仓库绝对路径>" --mode sync
```

同步先拉取远端并做三方比对；远端有新内容则导入，本地有新内容则上传。冲突时停止，不覆盖，不 force push。
