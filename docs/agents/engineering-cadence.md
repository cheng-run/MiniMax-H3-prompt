# 工程化常态（2026-11-02，体检 P6）

体检结论（`docs/research/2026-11-02-项目体检.md` §7）：代码质量比文档给人的印象好，
短板集中在**可验证性**与**可导航性**。这份文件是把体检里剩下的「常态动作」写成人
能照着做、机器能照着跑的一页，不重复 ADR 的内容。

## 一条命令看到全部守卫

```bash
scripts/check.sh fast       # 提交前：结构守卫 + 改动文件 lint + 秒级用例
                            #   + 改动 src/契约/前端生成链时自动查契约漂移
                            #   + 前端有改动时跑 eslint/vue-tsc
scripts/check.sh full       # 交验收/推送前：全量测试 + docstring 守卫 + lint + mypy
                            #   + 前端 + 契约漂移
scripts/check.sh lint       # 全量 src 的 ruff（装了才跑）
scripts/check.sh mutate     # 守卫变异检验（无额外依赖，约 5 秒）
scripts/check.sh types      # mypy 全仓（装了才跑）
scripts/check.sh coverage   # 分支覆盖度量（装了才跑，不设门槛）
scripts/check.sh frontend   # 强制跑 eslint + vue-tsc（依赖不完整时跳过并提示）
scripts/check.sh clean      # 干净环境（无 .env）跑全量 + 契约漂移，模拟 CI
scripts/check.sh worktree   # 工作树生命周期冒烟（add/remove）
scripts/check.sh rehash     # 打印关键帧角色提示词的 sha256，供更新基线
```

- **判据是最后一行**：`[check] <档位>: PASS` / `FAIL（退出码 N）`。管道随便接，
  `| tail -3` 也不会把红显示成绿（脚本自己保证，不用记 `PIPESTATUS`）。
- **跑动时机**：提交前 `fast`；**复审修完**再跑 `full` + `clean`。复审还可能改代码时
  先别跑这一对——一轮全量约 2 分钟，复审一改就作废（2026-11-10 #223 实测白跑一轮）。
- **契约漂移比的是工作区文件，不是 HEAD**：`check.sh` 导出到临时文件再与
  `contracts/openapi.json` / `generated.ts` 逐字节比，所以契约改动**未提交时也能跑**
  （与 HEAD 比会把「未提交」误报成「漂移」）。「提交后的文件与代码一致」由 CI 的
  `git diff --exit-code` 负责。
- **本机中文输出**：Git Bash 的控制台默认 cp936，pytest/mypy 的中文失败信息会乱码；
  `check.sh` 已设 `PYTHONIOENCODING=utf-8`，手跑 `pytest` 时自己带上。
- **前端守卫的「装了」判据是 `node_modules/.bin/eslint` 与 `.bin/vue-tsc`**，不是目录
  存在——空/半装目录按「未安装」跳过，不误报成代码坏了（2026-11-10 实测）。
- **工作树借用前端依赖**只能走 `scripts/worktree.sh link-frontend NN` /
  `unlink-frontend NN`（目录联接）；`remove` 删树前会先安全摘联接。`rm -rf` 联接会
  穿透清空主树依赖（2026-11-10 实测）。
- `PYTHONPATH` 由脚本自己指向**本树**的 `src`，工作树里跑也是测工作树。
- 单条用例超时 300 秒（`pyproject.toml`），卡死时以「哪个用例超时」收场；要
  `pytest-timeout` 才生效（安装由用户执行）。
- 结构守卫：`scripts/check_docstring_shape.py`（docstring 不许写成 f-string）、
  `tests/test_adr_guard.py`（`ADR NNNN` 必须指向真实文件；每份 ADR 有且只有一行状态
  行）、`scripts/check_commit_msg.sh`（提交信息格式）。`scripts/ac_trace.py` 只做
  追溯、不是门禁。
- 新机器跑一次 `scripts/bootstrap-dev.sh` 装回 `pre-commit` / `commit-msg` 钩子
  （`.git/hooks/` 不随 clone 走）。

CI（`.github/workflows/ci.yml`）跑的是同一套：docstring 守卫 → ruff（`src tests`）→
mypy → 全量测试 → 变异检验。**CI 与本机同源**，否则「本机绿」又会变回唯一证据。

### 改工作流时的两条约定

1. **actions 一律钉精确版本**，不写 `@v7`/`@v10` 这类移动主版本标签——有些仓库
   不发主版本标签（`astral-sh/setup-uv` 只有 `v10.3.3` 这种），`@v10` 会当场
   "unable to find version"（2026-11-02 首跑 6 秒失败）。推送前验一次：

   ```bash
   gh api repos/<owner>/<repo>/git/matching-refs/tags/<tag> --jq '.[].ref'
   ```

   本地无法复现任何工作流错误，这一验是唯一能提前发现的手段；`actionlint` 只查
   语法与表达式，查不了标签存在。
2. **Windows runner 的控制台是 cp1252**：脚本里 `print` 中文会 `UnicodeEncodeError`，
   整个步骤以崩溃收场。工作流已设 `PYTHONIOENCODING: utf-8`，新脚本入口也自己
   `sys.stdout.reconfigure(encoding="utf-8")` 兜一道。

## 装了才跑的工具

安装一律由用户执行（全局规则：下载由用户本人做）：

```bash
uv add --dev ruff             # 改动文件的 lint；check.sh 用 "$PY" -m ruff，不走 PATH
uv add --dev mypy             # 全仓类型；配置见 pyproject [tool.mypy]
uv add --dev coverage         # 只度量、不设 fail_under，见 pyproject [tool.coverage.*]
uv add --dev pytest-timeout   # 单条用例 300 秒超时；未装时 check.sh 会提示
cd frontend && npx pnpm@11.7.0 install   # eslint / vue-tsc / 契约 TS 生成
```

缺装时守卫按「跳过并提示」处理，不会把缺依赖误报成代码坏了。

- **mypy**：2026-11-02 分六批从 4 个叶子模块推到**全仓 72 个文件零错误**，`files`
  现在就是整个包（白名单会漏掉新增文件）。推进纪律保留：一次一批、修到绿再加下一批；
  新错误不会淹在历史债里，因为历史债已经清零。
- **coverage**：本仓没有历史基线，设 `fail_under` 只会逼人写凑数用例。改动了哪几行、
  哪几行没测到，才是评审要看的东西（`coverage report` 的 `Missing` 列）。
- **变异检验**：`scripts/mutation_check.py` 把关键实现改坏、跑指定守卫用例、再还原。
  用例必须变红。新加守卫时往 `MUTATIONS` 里补一条——**没有变异检验的守卫不算守卫**
  （「永不为红＝没有」）。

## 并行拆票前的对票

`/implement-spec` 按文件簇分组是手工动作，手工分组会漏票：2026-11-03 实测 12 张子票
分三组，漏了 #150，直到文档阶段读「已知缺口」才发现。漏一张票的代价不是少写一段
代码——它可能完全不出现在任何工作树里，直到某个下游文档引用它才暴露。

派发前先对一次集合，**差集非空不许开工**：

```bash
# 全票：父 spec 下 ready-for-agent 的子票（按需替换 label / spec 号）
gh issue list --state open --label ready-for-agent --json number --jq '.[].number' | sort -n > /tmp/all-tickets
# 已映射：把「票号 → 组」写完后，只填票号
printf '%s\n' 145 146 147 148 149 150 151 152 153 154 155 156 | sort -n > /tmp/assigned-tickets
comm -23 /tmp/all-tickets /tmp/assigned-tickets   # 必须为空
```

## 集成分支按票拆回 main（/implement-spec 收尾）

集成分支上的提交是「按票陆续合入」的形状（一票可能有多条：实现 + 复审修复），
而 main 要求**一票一次**。拆法是按票分组重放，不是把集成分支整条 ff 过去：

1. `git log --graph --oneline <base>..<integration>` 先认一遍：**跳过合并提交**
   （工作树分支并进来的那个 `Merge branch ...`）；它带进来的另一票改动，在那一票
   自己的分组里重放。
2. `git switch main`，按票的依赖顺序逐组 `git cherry-pick -n <该票的提交...>`，
   再一次性 `git commit -m "type(scope): 摘要（#N）"`。
3. 复审修复若落在集成分支末尾（时间上晚于后面的票），仍归到它修的票：作为该票的
   第二提交（`fix(...): ...（#N）`）放在组内，不要改挂到别的票上。
4. 收尾必须内容保真：`git diff --stat <integration> main` **为空**。不为空说明漏了
   提交或解冲突时改坏了内容——停下，不要 push。
5. main 上再跑 `scripts/check.sh full` 与 `scripts/check.sh clean`；绿后 push。
   删分支时注意：`git branch --merged main` 只覆盖真祖先，cherry-pick 出来的分支
   不是祖先；先用 `git merge-base --is-ancestor <branch> <integration>` 确认它的
   历史已包含在集成分支里，再 `-D`。

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
