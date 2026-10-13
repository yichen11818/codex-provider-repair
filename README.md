# Codex Provider Repair

[![Tests](https://github.com/yichen11818/codex-provider-repair/actions/workflows/tests.yml/badge.svg)](https://github.com/yichen11818/codex-provider-repair/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/yichen11818/codex-provider-repair)](https://github.com/yichen11818/codex-provider-repair/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**检测并可选修复 Codex 历史会话与 SQLite 数据库中的供应商引用，让切换供应商后的旧对话可以继续使用。**

A Codex skill and standalone Python CLI to audit stale `model_provider` references and inconsistencies in session JSONL files and SQLite state databases. Audits run without changing session data; applying repairs currently requires Windows.

## 解决什么问题

切换供应商后，新对话正常，旧对话却提示：

```text
ChatGPT can't load config.toml, so this thread can't resume.
Model provider 'OLD_PROVIDER' not found.
```

历史会话的 `session_meta.payload.model_provider` 和数据库的 `threads.model_provider` 可能仍引用旧供应商。只改全局配置或某个文件的第一行，可能遗漏数据库或后续的元数据记录。

本工具检查实际存在的数据，不假定所有 Codex 版本都使用同一数据库或相同读取优先级。

| 能力 | 行为 |
| --- | --- |
| 缺失供应商检测 | 对照指定 Codex 主目录的全局配置和内置供应商集合 |
| 会话与数据库对照 | 检查会话最后一条元数据与数据库记录是否一致 |
| 多数据库对照 | 检查同一个会话在不同状态数据库中的供应商差异 |
| 完整元数据检查 | 检查所有 `session_meta` 行，包括普通及归档会话 |
| 可选修复 | 默认只检测，显式加 `--apply` 才修改 |
| 精确迁移 | 可填写来源、目标供应商，以及一个或多个会话 ID |
| 自动备份 | 保存原始会话文件、SQLite 在线快照和修改清单 |

## 环境要求

- Python **3.11 或更新版本**；运行工具仅使用标准库，无需 `pip` 或 npm 包。
- **Windows** 支持检测和修复。修复使用 Windows 文件句柄阻止并发写入，因此其他系统目前仅支持检测。
- Everything 的 `es.exe` 为可选文件定位工具，优先从 PATH 查找，也可用 `--everything` 指定。不可用时仅定向搜索会话目录。
- 目标供应商必须已在 `config.toml` 中配置，或为内置供应商。

## 安装为 Codex Skill

在 PowerShell 中执行：

```powershell
$skillRoot = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME 'skills'
} else {
    Join-Path $env:USERPROFILE '.codex\skills'
}

New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
git clone https://github.com/yichen11818/codex-provider-repair.git (Join-Path $skillRoot 'codex-provider-repair')
```

也可从 [Releases](https://github.com/yichen11818/codex-provider-repair/releases) 下载 ZIP，将其中的 `codex-provider-repair` 文件夹放入 Codex 的 `skills` 目录。已安装同名目录时，先核对来源再更新，避免覆盖自己的修改。

安装后在新对话中使用：

```text
$codex-provider-repair 检测所有会话和数据库，只报告
```

```text
$codex-provider-repair 将失效供应商批量修复为 custom
```

```text
$codex-provider-repair 只修复 codex://threads/会话UUID，把 wuzu_api 改为 custom
```

技能会按请求选择检测或修复，先检查受影响项，再备份、执行和复查。完整指令见 [SKILL.md](SKILL.md)。

## 作为独立 CLI 使用

克隆仓库后进入目录：

```powershell
git clone https://github.com/yichen11818/codex-provider-repair.git
Set-Location codex-provider-repair
```

### 1. 检测

```powershell
python .\scripts\fix_codex_providers.py --report .\provider-before.json
```

此命令只读取会话及数据库并生成报告。默认读取 `CODEX_HOME`，未设置时使用 `~/.codex`。

### 2. 备份后修复

```powershell
python .\scripts\fix_codex_providers.py --to-provider custom --apply --report .\provider-repair.json
```

默认只迁移全局配置中已不存在的非内置供应商。已配置供应商及内置 `openai`、`ollama`、`lmstudio` 会保留；省略 `--to-provider` 时，目标取当前配置的 `model_provider`，未设置则为 `openai`。

如果使用了项目、profile 或命令行供应商覆盖，请先确认有效配置。全局未声明的供应商不一定在这些配置中也失效。

### 3. 复查

```powershell
python .\scripts\fix_codex_providers.py --to-provider custom --report .\provider-after.json
```

检查候选修改数量是否为零，分别查看剩余诊断提示。重新打开之前报错的会话；客户端还显示旧提示时，重启 Codex 后再打开。

### 指定来源和会话

填写一个或多个旧供应商：

```powershell
python .\scripts\fix_codex_providers.py `
    --from-provider wuzu_api `
    --from-provider yumi `
    --to-provider custom `
    --apply `
    --report .\provider-repair.json
```

只处理一个会话，UUID 和 `codex://threads/UUID` 都支持：

```powershell
python .\scripts\fix_codex_providers.py `
    --thread-id '11111111-1111-4111-8111-111111111111' `
    --from-provider wuzu_api `
    --to-provider custom `
    --apply `
    --report .\provider-repair.json
```

上面的 UUID 为示例，请替换成自己的会话 ID。复查时沿用相同的 `--home`、`--thread-id`、来源和目标参数。

## 参数

| 参数 | 说明 |
| --- | --- |
| `--home PATH` | Codex 主目录，默认 `CODEX_HOME` 或 `~/.codex` |
| `--thread-id UUID_OR_URL` | 限定会话，可重复 |
| `--from-provider NAME` | 显式选择旧供应商，可重复；覆盖默认的缺失供应商筛选 |
| `--to-provider NAME` | 目标供应商，默认取当前配置 |
| `--apply` | 执行修复，省略时只检测 |
| `--report PATH` | JSON 报告，默认当前目录的 `codex-provider-audit.json` |
| `--backup-root PATH` | 备份父目录，默认 `<Codex 主目录>/provider-repair-backups` |
| `--everything PATH` | 指定 Everything 命令行客户端 |
| `--help` | 查看命令帮助 |

## 检查范围与报告

```text
<Codex 主目录>/
├── config.toml
├── sessions/**/rollout-*.jsonl
├── archived_sessions/**/rollout-*.jsonl
├── state_*.sqlite
└── sqlite/state_*.sqlite
```

报告中的 `summary` 提供扫描文件数、候选文件/元数据数量、各数据库候选行数和问题类别。`files`、`databases` 记录候选修改项；`issues` 记录差异；`errors` 记录无法完成的检查。

重点关注以下诊断：

| 问题 | 含义 |
| --- | --- |
| `provider_not_configured` | 会话或数据库使用了全局未定义的供应商 |
| `db_session_provider_mismatch` | 数据库供应商与文件最后一条元数据不同 |
| `database_provider_mismatch` | 同一会话在不同数据库中的供应商不同 |
| `multiple_metadata_providers` | 同一文件出现多个元数据供应商 |
| `rollout_path_not_scanned` | 数据库路径没有命中本次扫描，可能已归档移动；`found_by_id` 列出找到的候选文件 |

有效供应商之间的差异、归档旧路径和缺失元数据仅作为诊断提示，不自动改路径、删除记录或伪造元数据。不同数据库可能包含同一个会话，不能把各数据库行数相加当作独立会话数。更多解释见 [诊断与恢复说明](references/diagnosis.md)。

退出码 `0` 表示本次操作完成，**不等于没有诊断问题**；`2` 通常表示参数、配置或扫描错误。应用中途异常可能已完成部分独立修改，应读取清单中的各项状态。

## 备份与修改边界

修复只替换 `session_meta.payload.model_provider` 和 `threads.model_provider`，保留聊天正文、其他字段、JSONL 换行、编码标记及文件修改时间。扫描后文件变化、被写入、备份失败或数据库结构未知时会停止，数据库条件更新失败会回滚该事务。

```text
<Codex 主目录>/provider-repair-backups/<时间戳>/
├── manifest.json
├── files/                  # 原始会话文件
└── databases/              # SQLite 在线快照，包含已提交 WAL 数据
```

工具不会自动结束 Codex、删除缓存或修改全局配置。跨文件和跨数据库的整批修改不是一个原子事务；出现中断时，清单会保存已完成项和备份位置。

报告包含本地路径和会话 ID，备份包含完整原始历史，请保存在本机。恢复前先核对修复之后是否有新增数据，不要在 Codex 运行时覆盖整库快照。恢复流程见 [references/diagnosis.md](references/diagnosis.md)。

这个工具修复本地供应商引用，不处理 API 鉴权、网络、额度或模型兼容性问题。

## 开发与测试

```powershell
python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

13 项测试使用临时会话及数据库，覆盖字节保留、后续元数据、写入冲突、WAL 备份、事务回滚、只读检测、范围限制和归档路径识别。GitHub Actions 在 Windows 上验证 Python 3.11、3.12、3.13。

```text
SKILL.md                         # Codex 技能入口
agents/openai.yaml               # 技能名称与默认提示
scripts/fix_codex_providers.py    # 检测/修复 CLI
scripts/test_fix_codex_providers.py
references/diagnosis.md           # 结果解释与恢复边界
```

## 许可

[MIT License](LICENSE)。这是独立社区工具，与 OpenAI 无官方隶属关系。
