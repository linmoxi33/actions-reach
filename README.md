# actions-reach

离线回答一个问题：**这次 `push` 之后，这个 workflow 里到底有哪些 job 会真的启动？**

GitHub 不回答这个问题——它只在运行时给出结果，而 `needs` + `if` + `strategy.matrix` + `needs.*.outputs`
四者叠在一起时，"某个 job 永远不会跑"和"某个 job 会被静默跳过"这两类错误，静态看 YAML 看不出来。
actions-reach 把这条运行路径在本地算出来，并给每条结论一份**可复核的 1-minimal 事件见证**。

Upstream: https://github.com/vercel-labs/agent-browser

## 上游是什么，它缺什么

`vercel-labs/agent-browser` 是一个 50k★ 的浏览器自动化 CLI，它的发布链路完全靠 GitHub Actions 驱动：
`.github/workflows/ci.yml`（11 个 job）与 `release.yml`（4 个 job）。这不是玩具配置——发布 job 的启动条件是
三个条件的合取：

- `build-binaries` 只有在 `needs.check-release.outputs.should_release == 'true' || ... needs_github_release == 'true'`
  时才启动（[release.yml#L93](https://github.com/vercel-labs/agent-browser/blob/f88c40b7ed7c2344af54b804b440b11169f98906/.github/workflows/release.yml#L93)）；
- `publish-packages` 用 `always() && ... && (should_release != 'true' || needs.build-binaries.result == 'success')`
  兜住"只发子包"的分支（[release.yml#L236](https://github.com/vercel-labs/agent-browser/blob/f88c40b7ed7c2344af54b804b440b11169f98906/.github/workflows/release.yml#L236)）；
- `github-release` 又要求 `needs.publish-packages.result` 是 `success` 或 `skipped`
  （[release.yml#L346](https://github.com/vercel-labs/agent-browser/blob/f88c40b7ed7c2344af54b804b440b11169f98906/.github/workflows/release.yml#L346)）。

这些 job id、output 名、result 判断全靠手写字符串对齐，而仓库里**没有任何 workflow 语义检查**：
仓库树里只列出两个 workflow 文件，没有 actionlint 配置、没有 reachability 测试
（[workflows 目录](https://github.com/vercel-labs/agent-browser/tree/f88c40b7ed7c2344af54b804b440b11169f98906/.github/workflows)）。
换句话说：它有一张 15 个节点的启动依赖图，但没有任何办法在推之前证明"这次真的会发"。
`needs.check-release.outputs.<拼错的名字>` 会安静地得到空串，比较永远是 false，job 直接不启动，日志里也没有
"因为你读错了 output 名"的注解。这就是本工具补的那一段。

## 这个工具做什么 / 不做什么

**做**：解析 workflow 的 YAML 子集，展开 `strategy.matrix`（cartesian → exclude → include，顺序按 GitHub 语义），
用三值逻辑（true / false / unknown）求值 `if:`（含 `needs.*.outputs.*`、`needs.*.result`、`always()` /
`success()` 的隐式状态检查），得到每个 job 的"能跑 / 必须跑 / 可能被跳过 / 确定死掉"，并用 15 条规则报告缺口。

**不**：不联网、不调用 GitHub API、不执行 workflow、不猜 secret 或哈希值；`hashFiles()`、`fromJSON()`
这类运行时函数一律返回 unknown，且 unknown 不会被打扮成 true 或 false。

## 安装 / 用法

零第三方依赖，只需要 Python 3.8+：

```bash
git clone https://github.com/linmoxi33/actions-reach.git && cd actions-reach
python3 -m unittest discover -s tests          # 15 个用例
python3 bin/actions-reach examples/in/pipeline.yml
```

`pipeline.yml` 里有一个拼错的 output 名、一个永远不会触发的 `release` 门槛、一个由 `always()` 兜底的发布 job：

```text
error  AR004   build   if reads needs.check.outputs.shold_release, but check declares no such output; the value is always empty
error  AR001   docs    if is false under every trigger; the job is dead code
error  AR012   docs    if requires event_name == 'release' but `on:` never triggers it
warnin AR010   publish if uses always() with needs that can fail; it will run even when its dependency failed (artifacts may be missing)
```

**场景模拟 + 见证**（这是本工具的主功能）：给一组观测事实，算出每个 job 的启动结论，并把事实集删到不能再删：

```bash
python3 bin/actions-reach --trigger push \
  --output check.should_release=true --assume check=success,build=success \
  --json examples/in/scenario.yml
```

```text
check    success  witness 2/5  result:check=success, trigger=push
build    success  witness 4/5  output:check.should_release=true, result:build=success,
                                result:check=success, trigger=push
publish  runs     witness 2/5  result:build=success, trigger=push
```

`publish` 的见证只有两个事实：它有 `always()`，所以 `check` 的死活与它无关——**删掉见证里的任何一条，结论就不再成立**
（`tests/test_reach.py::test_witness_is_one_minimal` 逐条删除后重跑模拟来验证）。

`--json` 输出可被 `python3 -c "import json,sys;json.load(sys.stdin)"` 解析；完整样例见
`examples/out/report.json`、`examples/out/scenario.json`。

退出码：`0` 没有 error 级发现（且场景结论全部可定）、`1` 检出问题或有无法判定的结论、`2` 用法错误 / 文件读不了。

`--self-test` 跑规则自证：15 条规则各配一对文件，**干净文件与缺陷文件只差 1 行**，要求缺陷文件触发、
干净文件不触发、且 diff 恰好 1 行——这样"负例最小"是被机器检查的，而不是写在 README 里的一句话。

## 真实语料上的结果

对上游 `ci.yml`（11 job）+ `release.yml`（4 job）实跑：`0 error / 2 warning`。
两条 warning 都是真的：`publish-packages` 与 `github-release` 都用 `always()` 挂在一个会失败的 need 上，
上游确实需要这个语义（发布失败后补 GitHub Release），所以工具只报 warning 不报 error——**没有把"设计如此"报成 bug**。
4 个 job 的启动与否由 `needs.check-release.outputs.*` 决定，而这 4 个 job 在工具的 DAG 里都有确定的启动路径。

另在 27 个真实 workflow（`openai/codex-security` 的 26 个 + `addyosmani/agent-skills` 的 1 个，共 44 个 job）上跑通，
无崩溃；其中 8 个文件用了 YAML 锚点，工具会报 `AR016` 警告并声明结果可能不完整（而不是静默给出"没有 job"）。
这 27 个文件上剩下的 3 条 error 级发现我**没有**逐条回上游核实，所以不在这里当成上游 bug 宣称。

## 局限

- YAML 只覆盖 workflow 常见子集：多行块标量里的**空行会被折叠**；锚点 / 别名（`&a` / `*a`）不支持，遇到会解析成普通标量。
- 表达式是近似实现：不处理 `fromJSON` / `hashFiles` / `inputs.*`（一律 unknown）；字符串真值判定把 `''`、`'false'`、`'0'` 当假，
  GitHub 的真实强转规则更细。
- 不是模拟器：不执行 step、不建模 `concurrency`、`environment` 审批、`continue-on-error` 对 result 的影响。
- 场景结论依赖你给的观测（`--assume` / `--output`）；不给就是不猜，报 `unknown`。
- `github.ref` 与 `github.repository` 默认是 unknown（离线不知道你在哪个仓库/哪个 ref 上跑），
  因此 `if: github.repository == '...'` 这类门槛不会被判成死代码——宁可 unknown，也不猜。

## 与同类项目的差异

- **actionlint**（rhysd/actionlint）：静态类型检查，能查未定义的 needs、表达式类型错，但**不做启动可达性判定**，
  也不给见证。本工具的输出是"这个 job 在什么事实组合下会/不会启动"，不是"这行表达式类型不对"。
- **act**：本地执行 workflow，需要 Docker，执行的是 step 本身；本工具不执行任何 step、也不需要容器，只算启动路径。
- **matrix-reach**（本账号早期项目）：只做 matrix 展开；本工具补上了 `needs` + `if` 的运行路径，并对每条结论给出可删减的见证。

## 本轮创新

**第一次把"启动结论"变成可删减的事实见证**：不是再打印一份规则清单，而是对每条判定跑一次"删掉一个事实、重放整张 DAG、
看结论还成不成立"的最小化，把 7 条观测压到 2–5 条；配合"干净/缺陷只差 1 行"的 15 对规则负例，
证明规则既不会漏报也不会在边界上误触发。和已发布的 matrix-reach（只展开矩阵）、以及 actionlint（只做静态类型）不同，
这里回答的是"这次 push 哪些 job 真的会启动"，并且答案可以被第三方用同样的事实集重放验证。

MIT License. 上游是 Apache-2.0，本仓库不复制其代码，只读取它的公开文件格式。
