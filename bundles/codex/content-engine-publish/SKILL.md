---
name: content-engine-publish
description: 首版仅离线校验发布内容；所有平台提交固定关闭，不触碰凭证或旧管线。
---

## 宿主与路径

本技能目录从宿主提供的 `SKILL.md` 绝对路径取其父目录，不猜 cwd、不硬编码仓库或用户路径。下列 `<本技能目录>` 是说明性占位符：执行前替换为实际目录，不是环境变量。使用宿主明确配置的 Python ≥3.11；runtime.json 只声明最低版本与 host-configured，不包含机器解释器路径。脚本与模板资源从脚本 `__file__` 定位，仅使用本包、不运行其它 skills。

视觉检查使用当前宿主实际提供的图片查看工具（例如 view_image 或具备图片能力的文件读取工具），不依赖某个固定工具名；缺能力必须失败关闭。

# 发布边界（固定禁用）

首版只能离线检查任务给定的冻结写作结果，不发 XHS、不投 MP、不生成伪回执。不能调用旧发布脚本、MCP、平台API、浏览器提交、凭证、模型、Cron、数据库或邻包。不能修改文稿/图片/评分/版本/技能/政策/manifest。仅可读授权任务文件和在 attempt 写离线检查结果。

任何发布、提交、commit、重发请求，必须报告 publishing_disabled，不得 confirmed 或伪造 receipt。unknown 状态不盲重发；未来发布入口必须单独上线验收和授权。

```json
{"outcome":"needs_input","reason_code":"publishing_disabled","message":"本版本只离线验证，真实提交未开放"}
```

离线 preflight 只接受与 write 同一 JSON 契约（见 result-schemas.json），不等于审校通过或真实平台就绪。事实仍须本题研究证据，Humanizer 保留事实限定，图片仍须独立 review 实际查看图片 每张并核对 id/hash；本角色不能补签这些结论。

```bash
python "<本技能目录>/scripts/cli.py" validate --file "<attempt绝对路径>/draft.json" --platform xhs
```

只读取本包 policy.json，无绝对仓库路径依赖。validate 是本地纯校验；CLI 的 prepare/submit/publish/commit 固定非零 publishing_disabled，不存在平台适配器。

不 prepare publication、不创建发布工作包。只允许 validate 离线检查；status 可只读查看工作目录，不作为发布授权。人工 interrupt 需显式 --owner-stopped，正常流程不用。
