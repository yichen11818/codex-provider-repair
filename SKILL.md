---
name: codex-provider-repair
description: 检测并可选修复 Codex 历史会话 JSONL 与 SQLite 数据库中的 model_provider 缺失引用或不一致。用于切换供应商后旧对话无法恢复、Model provider not found、批量检查会话供应商或指定会话迁移供应商；不用于 API 网络、额度或鉴权故障。
license: MIT
---

# Codex 会话供应商检测与修复

使用 [scripts/fix_codex_providers.py](scripts/fix_codex_providers.py) 检查本地数据。Python 3.11+，仅依赖标准库；执行修复目前只支持 Windows。

## 模式和范围

- 用户只要求“检测、检查、诊断”时，只运行预览，不加 `--apply`。
- 用户已明确要求“修复、迁移、统一改成某供应商”时，在该范围内完成检测、备份、修复和复查，无需再次征求同一操作的确认。
- 修复目标优先采用用户填写的 `--to-provider`；未指定时使用 `config.toml` 当前 `model_provider`，未设置则为 `openai`。示例使用 `custom`，应按用户的有效配置替换。
- 默认只把全局配置中已不存在的非内置供应商作为修复候选。保留已配置供应商及内置 `openai`、`ollama`、`lmstudio`。如果双方供应商都有效但不一致，仅报告；用户要求迁移时使用 `--from-provider` 指定来源。
- 用户给出会话 UUID 或 `codex://threads/UUID`，且只要求处理该会话时，加 `--thread-id`；用户要求“批量找类似问题”时检查全部，不把示例会话当成唯一范围。
- 本技能比较的是指定 Codex 主目录的全局配置。若用户说明还有 profile、项目或命令行供应商覆盖，先核对有效配置，不把全局未声明直接认定为失效。

## 执行

将下列 `<skill-dir>` 替换为本技能目录，`<workspace>` 替换为当前工作目录的绝对路径。报告写入工作目录；不要把聊天历史或执行结果放进技能目录。

```powershell
# 全量检测，不修改会话、数据库或配置
python "<skill-dir>/scripts/fix_codex_providers.py" --report "<workspace>/provider-before.json"

# 已授权修复时：自动备份，再将失效引用改为 custom
python "<skill-dir>/scripts/fix_codex_providers.py" --to-provider custom --apply --report "<workspace>/provider-repair.json"

# 复查；沿用原来的 --home、--thread-id 和供应商参数
python "<skill-dir>/scripts/fix_codex_providers.py" --to-provider custom --report "<workspace>/provider-after.json"
```

可填写参数：

| 参数 | 用途 |
| --- | --- |
| `--home PATH` | Codex 数据目录；默认 `CODEX_HOME`，否则 `~/.codex` |
| `--thread-id UUID_OR_URL` | 仅检测/修复指定会话，可重复 |
| `--from-provider NAME` | 指定待替换供应商，可重复；会覆盖“只选缺失供应商”的默认筛选 |
| `--to-provider NAME` | 目标供应商；执行修复前必须已定义或为内置供应商 |
| `--apply` | 执行修复；省略时只检测并写报告 |
| `--report PATH` | JSON 报告路径，默认当前工作目录下 `codex-provider-audit.json` |
| `--backup-root PATH` | 备份目录，默认指定 Codex 主目录下 `provider-repair-backups` |
| `--everything PATH` | Everything 命令行路径，默认优先从 PATH 查找 |

只迁移指定会话的示例：

```powershell
python "<skill-dir>/scripts/fix_codex_providers.py" --thread-id "会话UUID" --from-provider wuzu_api --to-provider custom --apply --report "<workspace>/provider-repair.json"
```

## 检测结果与修复约束

同时检查 `sessions`、`archived_sessions` 中所有行的 `session_meta`，以及主目录和 `sqlite` 子目录现存的 `state_*.sqlite`。Everything 用于文件定位，并补充数据库引用的现存路径；仅在工具不可用或目标未索引时定向搜索会话目录。

读取报告中的 `summary`、`issues`、`errors`。区分：未定义供应商、文件与 DB 不一致、不同 DB 不一致、同一文件多种元数据供应商、找不到会话文件。后四类是诊断线索，不代表都需要统一供应商。遇到这些情况或恢复备份时，读取 [references/diagnosis.md](references/diagnosis.md)。

脚本只修改实际 `session_meta.payload.model_provider` 字符串和 `threads.model_provider`。它保留聊天文本、其他字段、换行、编码标记和文件修改时间，使用 Windows 文件句柄排斥并发写入；数据库采用条件更新及事务。绝不对整份历史文本做供应商名称替换。

每次执行前保存受影响文件、含已提交 WAL 数据的 SQLite 在线备份及 `manifest.json`。备份失败、结构未知、会话变动或文件被写入时停止本轮修改并说明已完成项；不自动终止 Codex、不删除缓存、不覆盖配置、不安装 npm 工具。数据库锁冲突可在相关会话空闲后重试一次，持续失败则保留报告并明确告知。

执行后的预览应确认目标范围内待修复项为零。仍有路径或其他一致性提示时分别报告，不能声称“全部正常”。主库和子目录数据库可能包含同一个会话，不把行数相加当作独立会话数。

交付说明检测文件数、各数据库修复行数、剩余问题、目标会话供应商，以及脚本/报告/备份位置。只验证本地引用时不要宣称实际模型请求已经成功；提示重新打开报错会话，旧错误仍被缓存时再重启客户端。

## 维护验证

修改脚本后在技能的 `scripts` 目录运行 `python -B -m unittest -v test_fix_codex_providers.py`。测试使用隔离的临时数据，覆盖原文保留、后续元数据、并发写保护、WAL 备份、检测模式、会话范围和数据库差异；不在真实历史数据上制造测试故障。
