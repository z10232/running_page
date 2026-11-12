# Keep 跑步同步到 RUNALYZE

每天北京时间 08:00，`Run Data Sync` 同步 Keep 跑步到个人主页，随后把尚未上传的跑步文件交给 RUNALYZE 处理。主页发布与平台上传是独立任务，平台上传失败仍可发布主页。2026 年 11 月 9 日按用户授权把自动上传目标由高驰切换至 RUNALYZE，并导入全部现有历史跑步。高驰原有活动和回执保留，日常任务已停用高驰上传。

## 免费个人 API 配置

在 [RUNALYZE Personal API](https://runalyze.com/settings/personal-api) 创建 Token，选择 `Activity → Write (File)` 并设置到期日期。把值直接保存到 GitHub 仓库的 `Settings → Secrets and variables → Actions`：

| Secret / 环境变量 | 内容 |
| --- | --- |
| `KEEP_MOBILE` | Keep 国内手机号 |
| `KEEP_PASSWORD` | Keep 登录密码 |
| `RUNALYZE_API_TOKEN` | RUNALYZE 个人 API Token |

Token 到期后重新生成并更新同名 Secret。Token 不写入源代码或日志。该流程使用免费账户支持的文件上传接口，不依赖付费活动读取接口。

## 上传回执与重试

跑步使用现有 FIT 导出，保留 Keep 官方距离、运动用时、累计爬升，以及实际提供的轨迹、海拔和心率。缺失的数据保持缺失。

没有路线而有独立海拔图的跑步，会从 Keep 图表接口读取高度和实际采样时间，写入 FIT 海拔记录；不会生成经纬度或补齐未测量的起点。Keep 没有提供官方累计下降统计，因此下降由 RUNALYZE 根据海拔曲线及其平滑算法计算。图表经过降采样（11 月 8 日样例约每 15 秒一个点），计算结果可能遗漏区间内的小幅起伏，不能视为 Keep 官方累计下降。官方累计爬升仍保持原值。

海拔接口请求或解码失败时，该条活动保留到下次重试，不会把缺少高度的文件误记为上传成功。没有独立海拔图的记录仍保持高度缺失；RUNALYZE 的下降 0 在此情况下不能说明实际下降为 0。已经上传的历史记录由成功清单跳过，修复代码不会自动重新导入全部历史。

`activities/keep2runalyze.json` 按 Keep ID 保存两类回执：`pending` 是文件已被接收的队列编号，`accepted` 是已经取得 RUNALYZE 活动编号、确认入库的记录。每次提交后立即保存队列编号，再通过免费队列查询接口确认入库。后续运行只检查未完成队列，不会重新上传这些文件；已经入库的活动直接跳过。

**HTTP 201 仅表示文件进入处理队列。** 只有队列查询返回有效活动编号才记为成功。任务等待一段时间后仍未入库的记录保留在 `pending`，本轮显示未完成，下次继续查询。只读查询遇到连接异常时等待 3 秒重试一次；文件提交不自动重试，避免服务器已经接收时重复上传。接口异常、Token 无效或限流时停止本轮，已经保存的回执仍写回仓库。处理失败的队列也保留编号，应在 RUNALYZE 网页核对原因后处理，避免盲目重传。

清单缺失或损坏时停止上传，恢复清单后继续。任务串行运行，避免定时任务与手动任务同时提交相同跑步。请保留这份清单。首次按授权导入所有历史跑步，完成后每天只处理新增或尚未入库的记录。高驰旧回执不会被当成 RUNALYZE 已上传记录。

## 手动运行

打开 Actions → `Run Data Sync` → `Run workflow`，选择 `master`。日常运行将 `runalyze_sample_id` 留空；指定一个 Keep 数字 ID 时，仅处理这一条样例，已经入库的仍会跳过。

本地用相同环境变量运行：

```sh
python run_page/keep_to_runalyze_sync.py
```

修复工作流后从 `Run workflow` 启动新任务；`Re-run jobs` 会继续使用原任务版本。

接口契约来源：[官方免费 OpenAPI](https://runalyze.com/doc/api/free?format=jsonopenapi)、[官方 Personal API 说明](https://runalyze.com/help/article/personal-api)、[官方上传示例](https://github.com/Runalyze/personal-api/blob/main/bash/upload-activities.sh)。
