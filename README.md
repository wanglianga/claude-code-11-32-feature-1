# 广播受众估计服务（Audience Estimation Service）

由**虚构**的入户招募资料与个人收听日记，计算可复算的节目收听人数（reach）与人均收听时长，并在
报告中明确指出**哪些目标群体覆盖不足**，同时**拒绝**用这些数字比较两档节目的表现。

在此之上，服务还提供**推广方案组合触达分析**：以同一调查期、同一权重版本的个人日记为基础，
对任意节目集合计算联合触达、仅听、交叉、缺访上下界与边际贡献，并在预算、必选节目与互斥时段
约束下做边际重算选品——方案的价值是**新增听众**，而不是单档加权人数的相加。

不接入真实人口库、真实收听设备或任何真实个人数据。数据集见 `demo/fixtures.py`，全部为构造值。

- 技术栈：Python 3.12 / FastAPI / PostgreSQL 18 / psycopg 3（无 ORM，显式 SQL）
- 项目类型：**后端服务**，因此提供 OpenAPI 3.x 与 Swagger UI：
  - Swagger UI: `http://127.0.0.1:8000/docs`
  - OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`（OpenAPI 3.1.0，37 条路径）

---

## 1. 一键启动

### 1.1 Docker Compose（推荐）

```bash
docker compose up --build -d          # 启动 db + api，自动建表并写入虚构样本
docker compose ps                     # 等待 api 变为 healthy
curl -s http://127.0.0.1:8000/health  # {"status":"ok",...}
docker compose --profile demo run --rm demo   # 完整演示脚本（见第 5 节）
docker compose down -v                # 停止并清除容器
```

`api` 容器启动时执行 `docker/entrypoint.sh`：等待 PostgreSQL 就绪 → 幂等写入虚构数据（已写入则跳过）→ 启动 uvicorn。

### 1.2 本地开发（不使用 Docker）

容器内已预装 PostgreSQL 18（`127.0.0.1:5432`，用户/库/密码均为 `ccdev`）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export AUDIENCE_DATABASE_URL=postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev
export AUDIENCE_DB_SCHEMA=audience_dev      # 任意合法 schema，服务会自动建表
export AUDIENCE_ADMIN_API_KEY=dev-admin-key
export AUDIENCE_ANALYST_API_KEY=dev-analyst-key

.venv/bin/python scripts/seed.py --reset    # 重建 schema 并写入虚构数据
.venv/bin/uvicorn app.main:app --port 8000
```

`scripts/seed.py` 不带 `--reset` 时若库里已有批次会直接退出，不会重复写入。

---

## 2. 账号与鉴权

无需登录账号；使用 API Key（`X-API-Key` 请求头）：

| Key | 值 | 用途 |
| --- | --- | --- |
| 管理员 | `dev-admin-key` | 全部接口 |
| 分析员 | `dev-analyst-key` | 全部接口（当前实现两者权限相同，便于演示） |

`/health` 不需要鉴权。生产环境请通过环境变量改掉默认 Key。

---

## 3. 数据模型要点

| 概念 | 说明 |
| --- | --- |
| 招募批次 `recruitment_batches` | 只追加。每行注明抽样层、家庭标识、个人标识、入样概率、调查期间。入样概率为 `null` 时**不能**形成正式总体估计。 |
| 目标批次 `target_batches` | 年龄 / 地区 / 性别三个维度的已知边际总量；三个维度合计必须都等于 `population_total`，否则拒绝入库。 |
| 节目批次 `program_batches` | 节目单，含时区与 `[start_local, end_local)` 半开时段（`end_local` 允许 `24:30` 之类跨午夜写法）。 |
| 日记提交 `diary_submissions` | 只追加，`submission_seq` 即截止序号。重交必须声明 `replacement_scope`；旧行用 `superseded_by_submission_seq` 标记退出现行计算，**不删除历史**。 |
| 运行 `runs` | 创建时锁定四个输入序号（招募 / 目标 / 节目 / 日记截止）、配置与随机种子，并计算 `input_hash`。阶段化执行，可中断续跑。 |
| 报告 `run_reports` | 发布后不可更新（数据库触发器强制）。迟到日记或更正人口目标只会产生**新运行**。 |

### 运行阶段

`validate_inputs → build_sample → program_exposure → calibrate → bootstrap → estimate → publish_report`

`POST /v1/runs/{id}/execute` 带 `max_steps`，每次推进若干阶段；中断后再次调用同一接口会从上次位置继续，输入与种子不变。

---

## 4. 估计方法（与需求逐条对应）

### 4.1 基本权重与校准

- 基本权重 = `1 / inclusion_probability`。任一人概率未知 → 创建运行即返回 `422 inclusion_probability_unknown`，不做“猜测补齐”。
- 分析员登记年龄 / 地区 / 性别边际总量；入库时三维合计必须相等，否则 `422 target_totals_inconsistent`。
- 校准为**有界 raking（迭代比例拟合）**：
  - 每轮遍历三个维度调整权重 → **每轮后限幅** → 再检查全部边际；
  - 权重上下界为 `[0.3 × 本人设计权重, 4.0 × 本人设计权重]`（可在 `config.raking` 调整）；
  - 容差 `1e-4`，默认最多 100 轮；
  - 以下情况**保留残差并拒绝发布**，不以任何方式补造样本：
    - `empty_cell`：某边际单元样本为 0；
    - `bounds_unreachable`：限幅使目标不可达；
    - `non_convergence`：迭代结束仍未达容差。
- 允许分析员显式合并抽样层（`config.raking.stratum_merges`）或用新配置另跑一次；失败的运行整行保留，可查 `/calibration`。

### 4.2 日记重建

- 原始起止、台站、来源序号与修订号全部保存；统一转换为 `Asia/Shanghai` 后再按半开区间 `[start, end)` 求交。
- 日记日 = 当地 `00:00 → 次日 00:00`。跨午夜记录（如 `23:30 → 00:20`）被拆成两天的 `30` / `20` 分钟，合计不变。
- 同人同台重叠只算一次（区间并集）；不同台重叠生成 `run_conflicts`，状态 `pending`，**不按最后上传选择**。
  未解决冲突所涉及的分钟计为 `unknown`，并且该人在样本构建阶段被排除（`unresolved_conflict`），绝不折算为“没听”。
- 重交：必须先声明替换范围。替换范围跨调查日时，起止必须落在调查日边界上，否则 `422 diary_partial_day_replacement`。
  只替换范围内的时间；跨日替换不会把未涉及的那一天抹掉。

### 4.3 每日时段分类

对每个调查日，把声明窗口精确划分为四个互斥且合计等于窗口长度的部分：

| 分类 | 含义 |
| --- | --- |
| `listening` | 已填且有收听 |
| `no_listening` | 已填但确认没收听 |
| `unknown` | 已填但台站冲突，无法确认 |
| `uncovered` | 未填（漏访） |

`missing = unknown + uncovered`。**空白一律不当作没听广播**：漏填计入 `uncovered`，缺失日记的人整人被排除，其分钟数不被填 0。

### 4.4 分析样本

采用**预先声明**的完整回收条件（默认 `full_coverage`：每个调查日 `missing ≤ 0`），与节目表现无关，
因此在看到节目数字之前就已确定。可改为 `min_complete_days` 并给出最少完整天数。

### 4.5 估计量

- 节目覆盖人数：收听该节目达到 `reach_minutes`（默认 5 分钟）阈值的**独立个人权重求和**。
- 平均时长同时给出两种分母：
  - `mean_minutes_per_person` = 加权分钟 / 全样本权重；
  - `mean_minutes_per_listener` = 加权分钟 / 听众权重。
- 并列未加权样本数 `unweighted_n`、听众样本数 `listener_unweighted_n`、权重范围 `weight_min`/`weight_max`、
  有效样本量 `effective_sample_size`（Kish）。
- 另按年龄 / 地区 / 性别给出分组估计，并标注 `reliability`。

### 4.6 误差估计

- 抽样层内**整户重采样**：在每个有效抽样层内对家庭做有放回抽样，家庭被抽中多次即为多次独立副本，
  **不因标识相同去重**。
- 每个重复样本**重新执行权重校准**；校准失败的副本单独记录，不替换、不隐藏。
- 固定种子（默认 `20260901`），结果可复现。
- 95% 百分位区间。若校准失败副本比例超过 `bootstrap.max_calibration_failure_rate`（默认 10%），
  **停止输出区间**，写入 `interval_status = suppressed_calibration_failure_rate`，并在 `/bootstrap` 列出失败副本与原因。
- 同一节目内的分组差值可给区间（`/programs/{key}/differences`）。

### 4.7 覆盖不足

- 覆盖判定使用**合格日记样本的未加权占比**与目标人口占比之比（`representation_ratio`），
  低于 `thresholds.under_representation_ratio`（默认 0.70）或样本数低于 `min_cell_persons`（默认 5）即标为
  `undercovered`；样本为 0 标为 `no_sample`。
- 不用校准后权重判定：raking 会强行把加权合计对齐边际，用加权份额检查永远“看起来完整”，
  反而掩盖“某群体几乎没交日记”这一事实。
- 报告与 `/coverage-gaps` 都给出覆盖不足的组；同时说明**不能据此比较两档节目**。

### 4.8 跨节目比较

服务**明确拒绝**跨节目表现比较：

- 运行配置 `allow_program_comparison=true` → 创建运行返回 `422 cross_program_comparison_not_supported`；
- `GET /v1/runs/{id}/compare?program_a=&program_b=` 返回 `supported: false`、`difference_estimate: null`，并解释原因；
- 报告中固定包含 `comparison.supported = false` 的理由。

理由：每档节目的估计建立在不同的、覆盖不完整的子总体上，跨节目差值会把覆盖差异误读成节目表现差异。

### 4.9 推广方案组合触达（plan task）

**推广方案的价值在于新增听众，而非单档加权人数的相加。** plan task 把"同一调查期、同一权重版本"
冻结成一次任务，然后在其上对任意节目集合做试算：

- **权重版本**：任务创建时锁定四个输入批次（招募 / 目标 / 节目 / 日记截止），按任务的纳入规则
  构建样本并做**一次**有界 raking 校准（与 run 相同的算法与限幅），得到该任务的权重版本
  （`weight_version=1`）。此后该任务内的所有集合试算都复用这同一份权重。
- **样本纳入规则（与 run 不同）**：只要某人**至少一个候选节目可判定**（相关时段完整覆盖或已确认
  收听），就进入加权样本。run 的 `full_coverage` 会把部分缺访的人整人排除；plan task 保留他们，
  因为排除会把"没填"偷偷变成"没听"。完全不可判定的人（如整本日记未交）不进入样本、不携带权重，
  仅在 `/persons` 中列示。
- **相关时段未完整覆盖的人不能被放进未触达**：对每个节目，若其播出时段（裁剪到调查期内）未被人
  的日记完整覆盖（含未解决冲突的 unknown 分钟），该人对该节目为 `unknown`。集合指标中：
  - `union_reach.lower` = 确认触达的加权和（下界）；
  - `union_reach.upper` = 确认触达 + 缺访权重（上界，假设缺访者全部收听）；
  - `union_reach.not_reached` = 完整覆盖且未达阈值的加权和——**缺访者绝不计入未触达**；
  - 仅听（`exclusive_listen`）、交叉（`crossover`，含两两 `pairwise`）同样给出 `[lower, upper]`；
  - `intersections` 支持**任意阶**交集查询，按需精确计算——不依赖预先物化的交叉表，也不截断高阶交集。
- **边际贡献**：`marginal_candidates` 对集合给出加入某档后的边际区间
  `[确定新增, 可能新增]`。确定新增 = 对集合确认未触达且对该档确认触达；可能新增把缺访者计入，
  但绝不用猜测填补。
- **两条计算路径，结果完全一致**：
  - `scan`：逐人扫描节目集合并加权去重，表达直接，`include_persons=true` 可解释到个人；
  - `bitmap`：为每档维护按个人索引的压缩位图（Python 大整数位图，未置位区域不占对象），临时组合
    以位图并集再汇总权重。组合仍是精确的（集合并交，不是近似）。代价是索引构建、权重修订失效
    （rebind 后旧代际位图标记 `stale`）与内存管理（`/index` 展示各代际状态与内存估计）。
  - `engine=auto`（默认）按候选池大小选择（> `bitmap_program_threshold`，默认 12，用 bitmap）；
    单次试算可用 `engine` 覆盖。测试断言两条路径对同一集合返回**逐位相同**的结果。
- **预算、必选与互斥时段进入同一次边际重算**：优化作业以贪心执行——每一轮对全部剩余候选重算
  边际确认触达（下界），必选节目先行入集并占用预算，互斥组（不可同时购买的时段）在选中一项后
  立即封锁同组其余项，预算装不下的候选同轮标注 `cost_exceeds_remaining_budget`。每一轮都记录
  全部候选的边际、得分与排除原因（选择理由可审计）。优化目标是**确认触达（下界）**；缺访带来的
  不确定性只以区间呈现，不参与排序。
- **调查期切换或权重更新时旧任务不得写回**：`POST /rebind` 用新输入重建任务并把
  `weight_version` 加一；优化作业创建时快照版本号，执行（`POST .../execute`）时在行锁下重新核对，
  版本不符即标记 `stale_rejected` 并返回 `409 plan_job_stale`，**不写回任何结果**；rebind 也会
  主动把排队的旧作业标记为 `stale_rejected`，旧位图索引同时失效（`stale`）。

---

## 5. 验证方式（以下均为实际执行过的验证）

### 5.1 单元 + 集成测试

```bash
export AUDIENCE_TEST_DATABASE_URL=postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev
.venv/bin/python -m pytest tests/ -q
```

实测结果：**83 passed**（`tests/test_core.py` 18 项纯算法测试；`tests/test_api.py` 35 项接口集成测试；
`tests/test_plan.py` 30 项组合触达测试，均使用独立随机 schema，结束后自动清理）。

覆盖的断言包括：

- 半开区间求交：端点相接贡献 0 分钟；
- 跨午夜 `23:30→00:20` 拆为 30 / 20；
- 已填无收听 = `no_listening`，未填 = `uncovered`，两者绝不混淆；
- 同台重叠只算一次，跨台重叠记为 `unknown`；
- 重交只退役范围内旧行、历史仍可查，跨日部分替换被拒绝；
- raking 收敛达标 / 空单元 / 限幅不可达 / 不收敛四种结局都保留残差；
- 报告不可变（重新执行不再改写、数据库 UPDATE 触发异常）；
- 迟到日记与更正目标各自生成新运行，旧报告哈希不变；
- 未知入样概率、跨节目比较、空单元、限幅失败均按预期拒绝或标记；
- **按日记份数直接平均会高估青年节目**：实测 `youth_drive` 未加权均值 80.00 分钟，校准加权后 46.70 分钟（1.71 倍）；
  偏老节目方向相反（`midnight_news` 5.23 → 7.22），说明偏差并非常数。

组合触达（plan task）的断言包括：

- 状态分类：确认分钟达阈值 = `reached`；未达阈值但相关时段完整覆盖 = `not_reached`；否则 `unknown`；
- 缺访者只进入上界，**绝不**计入 `not_reached`；`lower + unknown + not_reached = 样本总权重`；
- 仅听 / 交叉（含两两）/ 任意阶交集均给出精确 `[lower, upper]`，高阶交集不截断；
- 边际贡献区分"确定新增"与"可能新增"（缺访只进入后者）；
- **scan 与 bitmap 两条路径**对同一集合（含边际与交集）返回完全一致的结果；
- 贪心优化：预算装不下的候选、互斥组被封锁的候选都在同一轮边际重算中标注原因；
  必选节目先行入集；`must_include` 互相冲突或超预算直接 422；
- **高重叠样例**：`morning_block` 的观众完全落在 `youth_drive` 内——联合触达 = `youth_drive` 单档，
  单档相加 ≈ 2 倍联合触达，`morning_block` 的确定边际 = 0；
- **高边际样例**：`midnight_news` 与 `youth_drive` 观众几乎不相交——边际上界 = 其自身全部触达；
- **缺访界限样例**：45-64 岁中第 2 天 04:00-20:00 未填的人对白天节目为 `unknown`（上下界分离），
  对 `midnight_news` 完全可判定（上下界重合为点）；
- **冲突时段样例**：互斥组 `[youth_drive, morning_block]`、`[afternoon_talk, evening_talk]`
  在优化结果中绝不同时出现，被封锁项在轮次日志中记为 `exclusive_conflict`；
- **旧任务不得写回**：rebind 后旧版本排队作业执行返回 `409 plan_job_stale` 且结果为空，
  旧位图索引标记 `stale`；权重更新（迟到日记）后新试算使用新版本，旧试算仍按原版本可查。

### 5.2 完整演示脚本

服务启动并 seed 后：

```bash
.venv/bin/python demo/run_demo.py --base-url http://127.0.0.1:8000
```

实测输出依次覆盖：

1. 已发布运行的校准状态、bootstrap 成功/失败数、区间是否可用；
2. 三档节目：加权覆盖人数、两种分母的平均时长、未加权样本数、权重范围、有效样本量、覆盖区间；
3. 覆盖不足群体与 `comparison.supported=false`；
4. 单个节目数字的血缘：样本行数、权重调整方法、排除原因汇总；
5. `S404` 已填但零收听（`no_listening=1440`、`uncovered=0`）对比 `S601` 漏填一小时（`uncovered=60`）；
6. 完全没有日记的人被排除（`treat_missing_as_zero_listening=false`）；
7. 四人家庭 `H04` 在 bootstrap 中只算 1 户（`households` 小于人数，`duplicate_identifier_copies_deduplicated=false`）；
8. `S301` 跨午夜 30 / 20 拆分；
9. `S501` 跨台冲突保持 `pending`；
10. 重交前后 `S701` 当前分钟数变化，且退役行数 = 2；
11. 空单元目标 → `rejected` + `empty_cells`，报告 404；
12. 限幅不可达目标 → `rejected` + `unreachable_cells` + 残差；
13. 空样本抽样层 `rural-remote` → `strata_without_eligible_sample` 列出，无任何合成样本；
14. 未知入样概率 → `422` 且列出具体人；
15. 迟到日记 → 新运行发布，旧报告哈希完全不变；
16. **组合触达演示**（第 11 段，使用 6 节目的 `plan-schedule-v1` 批次）：
    - 高重叠：`youth_drive` 单档 2409.46，并入 `morning_block` 后联合触达仍为 2409.46，
      而单档相加 4818.92；`morning_block` 的确定边际 = 0；
    - 高边际：`midnight_news` 对 `{youth_drive}` 的边际 = [1325.08, 1982.48]（其上界 = 自身全部触达）；
    - 缺访界限：`youth_drive` 触达区间 [2409.46, 3066.86]（缺访 6 人、657.40 权重），
      `midnight_news` 区间退化为点 [1982.48, 1982.48]；
    - 引擎一致：scan 与 bitmap 对同一集合均返回 6642.34（逐字段相同）；
    - 优化：预算 100 下贪心依次选 `evening_talk`(20) → `drive_extended`(25) → `midnight_news`(40)，
      每轮记录全部候选的边际与排除原因（`afternoon_talk` 因互斥被封锁，末轮预算耗尽）；
    - 权重更新：rebind 后 `weight_version=2`，旧版本排队作业执行返回 `409 plan_job_stale`。

### 5.3 手工最小验证

```bash
KEY='X-API-Key: dev-admin-key'
curl -s localhost:8000/health
# 先取当前可用日记截止序号，作为运行输入之一
CUT=$(curl -s -H "$KEY" localhost:8000/v1/diary/cutoff | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['available_max_submission_seq'])")
# 建运行：锁定招募 / 目标 / 节目批次序号与日记截止序号
RID=$(curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST localhost:8000/v1/runs \
  -d "{\"note\":\"manual\",\"inputs\":{\"recruitment_batch_seq\":1,\"target_batch_seq\":1,\"program_batch_seq\":1,\"diary_seq_cutoff\":$CUT}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['run']['run_id'])")
# 执行并取报告
curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST \
  localhost:8000/v1/runs/$RID/execute -d '{"max_steps":20}'
curl -s -H "$KEY" localhost:8000/v1/runs/$RID/report
```

已实际执行上式（本地端口 8000 换成当时端口）：运行状态 `published`，报告含三档节目，
`/docs` 返回 200，`/openapi.json` 为 OpenAPI `3.1.0`，共 37 条路径。

组合触达的最小验证（已实际执行）：

```bash
CUT=$(curl -s -H "$KEY" localhost:8000/v1/diary/cutoff | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['available_max_submission_seq'])")
# 创建 plan task（6 节目批次，seed 写入的第二个节目批次 program_batch_seq=2）
TID=$(curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST localhost:8000/v1/plan/tasks \
  -d "{\"inputs\":{\"recruitment_batch_seq\":1,\"target_batch_seq\":1,\"program_batch_seq\":2,\"diary_seq_cutoff\":$CUT}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['task']['task_id'])")
# 高重叠：联合触达 = youth_drive 单档（2409.46），而非两档相加（4818.92）
curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST localhost:8000/v1/plan/tasks/$TID/evaluations \
  -d '{"set":["youth_drive","morning_block"]}'
# 预算 + 互斥时段的优化作业（两阶段：创建 → 执行）
JID=$(curl -s -H "$KEY" -H 'Content-Type: application/json' -X POST localhost:8000/v1/plan/tasks/$TID/optimizations \
  -d '{"budget":100,"costs":{"youth_drive":45,"morning_block":30,"drive_extended":25,"afternoon_talk":35,"evening_talk":20,"midnight_news":40},"exclusive_groups":[["youth_drive","morning_block"],["afternoon_talk","evening_talk"]]}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job']['job_id'])")
curl -s -H "$KEY" -X POST localhost:8000/v1/plan/tasks/$TID/optimizations/$JID/execute
# 实测：选中 ['evening_talk','drive_extended','midnight_news']，成本 85 ≤ 100，联合触达 6642.34
```

### 5.4 关于 Docker 的说明

当前执行环境**未安装 Docker**，因此 `docker compose up` 未在本机实际运行。
已完成的静态校验：`docker-compose.yml` 可被 YAML 解析且包含 `db/api/demo` 三个服务与 db 健康检查；
`docker/entrypoint.sh` 通过 `sh -n` 语法检查；`Dockerfile` 的依赖列表与本地实测通过的
`requirements.txt` 完全一致。**Docker 路径未经实际执行验证**，请以第 1.2 节本地流程为准。

---

## 6. 接口一览

OpenAPI 3.1.0 共 **37 条路径**（同路径的 GET / POST 在表内合并为一行）。完整可交互文档见 `/docs`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/v1/meta/config` | 维度、默认配置、策略说明 |
| POST/GET | `/v1/recruitment/batches` | 招募批次（只追加，支持 `batch_ref` 幂等） |
| POST/GET | `/v1/targets/batches` | 人口目标边际（三维合计必须相等） |
| POST/GET | `/v1/programs/batches`、`/v1/programs/batches/{seq}` | 节目单 |
| POST/GET | `/v1/diary/submissions`、`/v1/diary/submissions/{seq}` | 日记提交与历史 |
| GET | `/v1/diary/cutoff` | 当前可用最大提交序号 |
| POST/GET | `/v1/runs`、`/v1/runs/{id}` | 创建 / 查看运行（锁定输入） |
| POST | `/v1/runs/{id}/execute` | 分步执行或续跑 |
| GET | `/v1/runs/{id}/status` | 阶段、校准、bootstrap、区间状态 |
| GET | `/v1/runs/{id}/report` | 已发布报告（不可变） |
| GET | `/v1/runs/{id}/programs/{key}` | **单个节目数字的血缘追溯** |
| GET | `/v1/runs/{id}/programs/{key}/sample` | 支撑该数字的样本行 |
| GET | `/v1/runs/{id}/programs/{key}/differences` | 同节目内分组差值 + 区间 |
| GET | `/v1/runs/{id}/compare` | 跨节目比较：**明确拒绝**并说明原因 |
| GET | `/v1/runs/{id}/calibration` | 边际目标、残差、轮次、限幅人数 |
| GET | `/v1/runs/{id}/bootstrap` | 副本成功/失败明细 |
| GET | `/v1/runs/{id}/exclusions` | 排除原因分组与明细 |
| GET | `/v1/runs/{id}/diary-usage` | 每条日记行的处置（使用 / 退役 / 冲突） |
| GET | `/v1/runs/{id}/conflicts` | 跨台冲突（默认 `pending`） |
| POST | `/v1/conflicts/resolutions` | 分析员确认冲突（只影响**新运行**） |
| GET | `/v1/runs/{id}/coverage-gaps` | 覆盖不足群体 |
| GET | `/v1/runs/{id}/days`、`/v1/runs/{id}/persons` | 每日分类明细、人员权重与排除 |
| POST/GET | `/v1/plan/tasks`、`/v1/plan/tasks/{id}` | 创建 / 查看组合触达任务（锁定调查期与权重版本） |
| POST | `/v1/plan/tasks/{id}/rebind` | 调查期切换 / 权重更新：`weight_version` 加一，旧作业与旧位图失效 |
| GET | `/v1/plan/tasks/{id}/persons` | 任务样本与权重（含未入样原因） |
| GET | `/v1/plan/tasks/{id}/index` | 位图索引各代际状态、内存估计、每档置位人数 |
| POST/GET | `/v1/plan/tasks/{id}/evaluations`、`/evaluations/{eid}` | 任意节目集合试算（联合触达 / 仅听 / 交叉 / 缺访上下界 / 边际）与历史 |
| POST/GET | `/v1/plan/tasks/{id}/optimizations`、`/optimizations/{jid}` | 预算 + 必选 + 互斥的优化作业（创建时快照版本） |
| POST | `/v1/plan/tasks/{id}/optimizations/{jid}/execute` | 执行作业：版本核对 → 边际重算 → 写回；过期作业 `409` 拒绝 |

### 从一个节目数字追溯回去

```text
GET /v1/runs/{id}/programs/youth_drive
  ├─ number.estimates          加权覆盖人数 / 两种平均时长 / 有效样本量
  ├─ number.intervals          95% 家庭 bootstrap 区间（含分组区间）
  ├─ trace.inputs              四个输入序号（招募 / 目标 / 节目 / 日记截止）
  ├─ trace.stages              各阶段载荷：样本量、排除计数、分层诊断、校准摘要
  ├─ trace.sample_rows         实际参与该节目计算的样本行数
  ├─ trace.weight_adjustment   → /calibration（每单元目标、合计、残差、轮次、限幅）
  ├─ trace.interval_decomposition → /bootstrap（成功 / 失败副本）
  ├─ trace.exclusions          每类排除原因与示例
  ├─ trace.conflicts           → /conflicts
  └─ trace.diary_usage         → /diary-usage
```

---

## 7. 目录结构

```
app/
  main.py            FastAPI 路由、鉴权、错误映射
  config.py          环境变量配置
  db.py              连接池 + schema 引导
  schema.sql         全部表、索引、报告不可变触发器
  models.py          Pydantic 请求模型与校验
  ingest.py          招募 / 目标 / 节目 / 日记的只追加入库
  timeutil.py        半开区间运算、时区、调查日与节目时段
  analysis.py        样本构建、日记重建、冲突裁决、节目暴露
  runs.py            运行编排：校验→样本→暴露→校准→bootstrap→估计→发布
  plan.py            组合触达任务编排：建版→校准→位图→试算/优化→rebind 失效
  report.py          报告组装（覆盖不足、缺口、限制）
  lineage.py         单个数字的血缘追溯
  differences.py     组内差值区间；跨节目比较的明确拒绝
  analytics/
    diary.py         日记重建与四分类日核算
    raking.py        有界 raking 与失败状态
    estimate.py      估计量、家庭分层 bootstrap、百分位区间
    reach.py         组合触达：scan/bitmap 双路径、缺访上下界、边际、贪心选品
demo/
  fixtures.py        虚构家庭 / 个人 / 节目 / 目标 / 日记（含全部边界场景与 6 节目组合批次）
  run_demo.py        端到端演示脚本（含第 11 段组合触达演示）
scripts/seed.py      幂等种子写入
tests/               83 项单元 + 集成测试
docker/              entrypoint
Dockerfile, docker-compose.yml, requirements.txt, pytest.ini
```

---

## 8. 已知限制

- 数据全部虚构，不接入真实人口库或收听设备。
- bootstrap 副本数与 `reach_minutes` 阈值可配置；区间宽度随副本数与样本量变化。
- 日记日固定为当地日历日（`00:00→24:00`）；若改用广播日，需要新的 `diary_window` 约定并重跑。
- 分析员确认冲突时点会影响后续运行结果，但**不会**改写已发布报告。
- plan task 的试算与优化是**点估计 + 缺访区间**：抽样不确定性（bootstrap 区间）目前只在 run 的
  单节目估计中给出，组合触达暂未传播该误差；优化排序因此只使用确认触达下界。
- plan task 的位图索引随任务版本持久化在数据库中；样本规模极大时，构建耗时与存储随
  `人数 × 节目数` 增长（`/index` 的 `memory_bytes` 可观测）。
- Docker 配置未在本次环境实际运行（该环境没有 Docker），见 5.4。
