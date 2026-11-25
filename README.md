# 楼宇保温报告合算核算

Flask + SQLite 的楼宇保温试点核算页。先录入至多 20 栋楼的固定正方差，再按报告编号录入或重报至多 12 份“覆盖楼宇 + 等权均值”报告。

## 启动

### Docker Compose

```bash
docker compose up --build
```

浏览器打开：

- <http://127.0.0.1:5000/>
- 同样映射了 <http://127.0.0.1:8000/>

健康检查：<http://127.0.0.1:5000/healthz>。

SQLite 数据库保存在 Compose 命名卷 `accounting-data` 的 `/data/accounting.db`。

### 本地直接运行

```bash
pip install -r requirements.txt
python app.py
# 可选：ACCOUNTING_DB=/path/to/accounting.db PORT=5000
```

## 核算规则

设第 \(b\) 栋楼的估计为 \(X_b\)，各楼估计独立且

\[
E[X_b]=\mu,\qquad \operatorname{Var}(X_b)=\sigma_b^2>0.
\]

报告 \(i\) 覆盖楼宇集合 \(S_i\)，楼数 \(k_i=|S_i|\)，其等权均值为

\[
Y_i=\frac1{k_i}\sum_{b\in S_i}X_b.
\]

因此：

\[
\operatorname{Var}(Y_i)=\frac{1}{k_i^2}\sum_{b\in S_i}\sigma_b^2,
\]

\[
\operatorname{Cov}(Y_i,Y_j)=
\frac{1}{k_i k_j}\sum_{b\in S_i\cap S_j}\sigma_b^2.
\]

程序用 Python `Fraction` 对小数、Decimal 和 `a/b` 分数做精确运算。若报告均值向量与 incidence matrix 组成的线性系统 \(Ax=y\) 不相容，则不存在一组共同的楼宇估计值能同时产生这些均值。程序枚举至多 12 份报告的子集，给出一组基数最小、按编号自然顺序确定的极小冲突报告；API 还返回全部极小冲突组。

相容时，对奇异协方差矩阵也能求 BLUE。解

\[
Ca=\mathbf 1,\qquad d=\mathbf 1^\top a,
\]

则

\[
\hat\mu=\frac{a^\top y}{d},\qquad
\operatorname{Var}(\hat\mu)=\frac1d.
\]

重复报告或任何可由已有报告线性得到的冗余报告不会提高精度；协方差矩阵奇异时该公式仍成立。

## 事务与重报

- 相同 `report_id` 的合规提交整体替换旧报告及其覆盖楼宇。
- 若新稿导致均值冲突，报告仍被接收，但合算值/方差被撤下，状态变为 `conflict`。
- 报告写入、覆盖楼宇替换、合算状态写入在同一个 `BEGIN IMMEDIATE` 事务中提交；任何异常都会回滚，保留原报告和原合算状态。
- 不合规输入（空均值、错误数字、未知楼号、超过数量上限等）返回错误且不改变原状。

## 页面与 JSON 接口

页面：

- `GET /`：核算页，可录入方差、报告、重报、删除和查看冲突/协方差/权重。
- `POST /buildings`：表单保存方差。
- `POST /reports`：表单录入或重报报告。
- `POST /reports/<编号>/delete`：删除报告。
- `POST /recompute`：用 SQLite 当前数据重新核算。
- `POST /reset`：清空本轮数据。

JSON：

```bash
# 录入楼宇方差
curl -sS localhost:5000/api/buildings \
  -H 'Content-Type: application/json' \
  -d '{"variances":["1/2",0.8,"2"]}'

# 录入/重报报告
curl -sS localhost:5000/api/reports \
  -H 'Content-Type: application/json' \
  -d '{"report_id":"R1","mean":"1/4","building_ids":[1,2]}'

# 也可 PUT /api/reports/R1
curl -sS -X PUT localhost:5000/api/reports/R1 \
  -H 'Content-Type: application/json' \
  -d '{"mean":"0.25","building_ids":[1,2]}'

curl -sS localhost:5000/api/state
curl -sS localhost:5000/api/computation
```

主要 JSON 字段：

- `computation.status`：`no_buildings`、`no_reports`、`ok` 或 `conflict`
- `computation.estimate` / `variance`：精确分数字符串
- `computation.estimate_float` / `variance_float`：便于数值断言
- `computation.conflict_ids`：一组不能同时成立的报告编号
- `computation.minimal_conflicts`：全部极小冲突组
- `buildings`、`reports`：SQLite 中当前已提交的数据

## 自测

测试使用标准库 `unittest`，无需 pytest：

```bash
python -m unittest discover -s tests -v
```

已覆盖：精确小数/分数、独立报告、重叠协方差、重复/线性冗余不提高精度、两人冲突、多报告冲突、Flask 表单/JSON、12 份上限与同号重报、重启持久化、以及注入合算状态写入失败后的事务回滚。
