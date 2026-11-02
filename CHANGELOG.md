# 变更记录

形状照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；版本号与
`pyproject.toml` 同步（当前 `0.1.0`，尚未打 tag）。

写法约定：**按用户能感知的行为写**，不抄提交列表。内部重构若不改行为，归到
「工程化」一节一句话带过即可。

## [未发布]

### 修复

- 导演台 4 秒窗口不再落位到 3.04 秒：落位层守住 4 秒下限（#141）。受影响的
  规范用例（702 帧 / 5 段）从 `90+153×4` 变为 `107+136+153×3`。
- 切镜段的导演包固定写 `continuityFromPrev=false`（#142）——缺这个字段时插件
  按 `true` 处理，导入后会把切镜段当成续接。
- 资产生图纪律块补「以本节为准」压盖语（#139）：与角色提示词里的通用条款冲突时，
  以模型档纪律为准。

### 工程化

- `tests/`、`docs/`、`scripts/`、`AGENTS.md` 重新入库，加 GitHub Actions CI
  （docstring 守卫 → ruff → 全量测试 → 变异检验）。
- 异常分类：`errors.py` 四个分类（用户输入 / 模型输出 / 配置 / 外部服务），
  全部继承原内置异常，存量 `except ValueError` 不受影响。
- `config/agent.yaml` 加 schema 校验：未知键报错并给最接近的候选；
  `models.vision.provider` 退役（视觉档恒走 OpenAI 兼容协议）。
- 结构化运行日志 `output/logs/<时间>-<阶段>.jsonl`：事件、耗时、token、错误码；
  只记元数据，不记提示词正文（`H3_RUN_LOG=0` 可关）。
- 状态契约机读化：`PipelineState` 与持久化白名单双向核对，两张带理由的例外表。
- 文本判据（时间戳 / SHA256 / `[Shot N]` / `<Picture N>` / `<Subject N>`）收成
  `text_patterns.py` 单一来源，用对象身份守住。
- 死代码清理约 400 行；`ui/wizard.py` 抽出展示层，并加「不许继续变长」的闸。
- 三路线矩阵（base / ref / director）与外部契约（网关 usage、导演包字面契约、
  ffmpeg 参数形状）进入用例。

## 发布前检查

```bash
scripts/check.sh full
scripts/check.sh mutate
```

打 tag 与推送都要等用户验收（见 `docs/agents/engineering-cadence.md`「发布管理」）。
