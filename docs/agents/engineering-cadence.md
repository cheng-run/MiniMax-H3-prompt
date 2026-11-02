# 工程化常态（2026-11-02，体检 P6）

体检结论（`docs/research/2026-11-02-项目体检.md` §7）：代码质量比文档给人的印象好，
短板集中在**可验证性**与**可导航性**。这份文件是把体检里剩下的「常态动作」写成人
能照着做、机器能照着跑的一页，不重复 ADR 的内容。

## 一条命令看到全部守卫

```bash
scripts/check.sh fast       # 提交前：结构守卫 + 改动文件 lint + 秒级用例
scripts/check.sh full       # 推送前：全量测试 + docstring 守卫 + lint
scripts/check.sh lint       # 全量 src 的 ruff（装了才跑）
scripts/check.sh mutate     # 守卫变异检验（无额外依赖，约 5 秒）
scripts/check.sh types      # mypy（装了才跑）
scripts/check.sh coverage   # 分支覆盖度量（装了才跑，不设门槛）
```

CI（`.github/workflows/ci.yml`）跑的是同一套：docstring 守卫 → ruff → 全量测试 →
变异检验。**CI 与本机同源**，否则「本机绿」又会变回唯一证据。

## 三条「装了才有」的工具

安装一律由用户执行（全局规则：下载由用户本人做）：

```bash
uv add --dev mypy      # 先只查叶子模块，配置见 pyproject [tool.mypy]
uv add --dev coverage  # 只度量、不设 fail_under，见 pyproject [tool.coverage.*]
```

- **mypy**：`files` 刻意只列 `errors.py` / `kinds.py` / `text_patterns.py` /
  `observability.py`——它们没有项目内循环依赖，注解能真表达出来。一次放开全仓会
  刷出上千条历史错误，把新错误一起淹掉。加文件是**一步一步**的事，每加一个就修到绿。
- **coverage**：本仓没有历史基线，设 `fail_under` 只会逼人写凑数用例。改动了哪几行、
  哪几行没测到，才是评审要看的东西（`coverage report` 的 `Missing` 列）。
- **变异检验**：`scripts/mutation_check.py` 把关键实现改坏、跑指定守卫用例、再还原。
  用例必须变红。新加守卫时往 `MUTATIONS` 里补一条——**没有变异检验的守卫不算守卫**
  （「永不为红＝没有」）。

## ADR 的写法与引用

2026-11-02（#143 整顿）定下的三条，**新写或改 ADR 时一律遵守**：

| 约定 | 判据 | 守卫 |
|---|---|---|
| 状态行 | 标题下第一段必须是 `> **状态（YYYY-MM-DD 复核）**：…`；**只追加、不覆写**（旧行保留，标「已被下一条取代」） | `tests/test_adr_guard.py` |
| 引用写法 | 本仓引用写 `ADR 0019`（空格）或 `docs/adr/0019`；**外部项目的 ADR 写 `ADR-0006`（连字符）**，不参与本仓核对 | 同上 |
| 索引 | **不落静态清单**，需要时 `uv run python scripts/adr_index.py [--status]` 从状态行现生成 | — |

为什么状态行必须机读：27 份里哪份已被取代、哪份已撤除，只有这一行说得清；手写
索引与 27 份文件是同一事实的两处口径，改了 ADR 忘了改索引不会有任何东西报错
（issue #56 的同型）。

**写 ADR 的门槛**：只在「有一个被否决的有力方案」或「未来的读者会问为什么这样」
时写；纯执行细节进代码注释或 issue。ADR 记录的是**决定与为什么**，不是任务日志。

## 需求追溯

```bash
uv run python scripts/ac_trace.py               # 覆盖概览
uv run python scripts/ac_trace.py --issue 141   # 一张票落到哪些文件
```

真实来源是文件本身：本仓约定注释与测试 docstring 里写 `#NNN`。脚本只读注释与
docstring（tokenize + ast），不看普通字符串，因此提示词正文里的 `#1` 不会被当成票号。

它**不是门禁**：乱写票号照样能过。它解决的是「哪张票只有代码、没有测试」——
这类条目以前只能靠人记。

## 发布管理

体检 §7 判本仓「发布管理 L1（无 tag / CHANGELOG / 回滚目标）」。补法是最小可用的一套：

| 动作 | 规则 |
|---|---|
| 版本号 | `pyproject.toml` 的 `version`；与 `CHANGELOG.md` 的最新条目一致 |
| 变更记录 | `CHANGELOG.md`（Keep a Changelog 形状）。**按「用户能感知的行为」写**，不抄提交列表 |
| 打标签 | `git tag -a vX.Y.Z -m "…"`，只在用户说「可以发布」之后；推送标签同样等验收 |
| 回滚目标 | 上一个 tag。README 里不写回滚步骤——`git checkout <上一个 tag>` 就是全部 |
| 发布前必过 | `scripts/check.sh full` + `scripts/check.sh mutate`；全绿只是前提，不是发布决定 |

用户验收前不 push、不打 tag：这是全局 `~/.claude/AGENTS.md` 的验收门，本文件不覆盖它。
