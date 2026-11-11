# Corpus Gate 语料治理

处理授权来源的合成文本，提供规范化、去重簇、审查、注释、抽样和发布。重复簇表示相似性连通分量，不等于语义等价；隐私检测只是复核信号。

## 运行环境

使用 Python 3.11，核心功能依赖标准库和 SQLite。演示和测试只使用本地合成数据，不需要账户、设备或外部运行服务。请从项目根目录执行命令。

## 检查与运行

构建检查验证语法，测试使用临时数据目录，不会连接远端服务。

```sh
python3 -m compileall -q corpus_gate
python3 -m unittest discover -s tests -v
python3 -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
```

演示会展示产品的实际数据处理和输出，运行结束自行退出。查看现有接口：

```sh
python3 -m corpus_gate --db :memory: operations
```

## 目录与数据

corpus_gate/ 保存领域逻辑和请求入口，tests/ 包含正常行为、边界与恢复测试。例子中的标识和记录为合成资料，不包含真实人员或生产测量。业务目录和本地数据库由调用者指定，应与源码分开保存。

## 双标轮次与仲裁（adjudication）

`adjudication.*` 操作是独立于普通 `annotation.*` 的双标工作流，用于度量两位标注者之间的一致性，而不是单人注释的增删。

- **轮次冻结**：`adjudication.round_create` 一次性冻结文档版本（含规范化文本哈希）、标签词表和恰好两位参与者。冻结后词表外标签、非参与者提交都会被拒绝。
- **提交前互不可见**：轮次 `open` 期间，标注者只能读取自己的修订；查询对方历史返回 `adjudication_blind`，轮次关闭后才开放互看。
- **修订与历史**：每次提交或撤回都追加不可变修订行。有效版本取该标注者的最高修订；撤回是一块墓碑（tombstone），撤回后无有效提交，但完整历史仍可通过 `adjudication.history` 追踪。
- **三类状态分开统计**：`adjudication.progress` 与报告区分未交（`not_submitted`）、明确空标注（`explicit_empty`，即显式提交零标签/零区间）和文档不可用（`unavailable`，由 `adjudication.mark_unavailable` 标记）。后两类在评估时排除并在报告的 `excluded` 中列明原因。
- **关闭后报告**：`adjudication.close`/`adjudication.report` 输出文档分类的混淆表、原始一致率与 Cohen kappa。当期望分歧为零（如两位标注者始终选同一标签）或没有可评估文档时，kappa 在数学上不可定义，系统返回 `defined:false` 与原因，绝不编造数值。
- **区间一对一重叠匹配**：按标签分别匹配，候选必须有正交集（仅共享边界的相邻区间不算重叠），允许嵌套。匹配为确定性贪心：先按重叠长度降序，再按 `(a.start,a.end,b.start,b.end,a.span_id,b.span_id)` 升序，每个区间一旦配对即被占用，因此一个区间不会匹配多个候选，任何平局都有唯一确定结果。报告同时区分精确一致、仅边界不同和标签理解不同的文档。
- **偏移语义**：区间偏移是冻结文本上的 Unicode 码点偏移，提交时校验非空、不越界，并对切片做哈希固定。
- **争议与仲裁**：`adjudication.disputes` 仅列出存在分歧的文档，并定位到双方各自的提交修订。`adjudication.arbitrate` 的裁决写入独立的 `adjudication_arbitrations` 表，可多次裁决并保留修订历史，绝不修改原始标注；参与者本人不能仲裁自己的轮次。
- **可回溯导出**：`adjudication.export` 的信封内嵌冻结轮次定义与轮次摘要哈希（`round_digest`、`envelope_digest`），每条结果都能回到原轮次。

## 干净环境复现

容器定义固定运行时版本。构建时准备环境，运行时无需网络；每次启动会复制只读源码到临时工作目录，测试产生的文件不会写回仓库。

```sh
docker build -t local-product-check .
docker run --rm --network none local-product-check
```
