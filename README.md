# 发酵罐温度日志质控系统（Flask + SQLite + NDJSON）

把发酵设备持续追加的 NDJSON 温度日志，按“只增不丢、非法整轮拒绝”的规则整理成
质控记录，提供网页查看、CSV 下载，并附带可演示**半行**与**日志轮转**的本地示例。

## 一、设备日志模型

设备只维护挂载目录（默认 `./data`，容器内 `/data`）中的两个文件：

| 文件 | 含义 |
| --- | --- |
| `active.ndjson` | 当前正在追加的活动日志，每行一条 NDJSON |
| `active.ndjson.1` | 归档文件：设备把 `active.ndjson` **重命名**而来，不删除、不覆盖；随后新建同名 `active.ndjson` |

- 目录最多保留这两份；归档后旧文件仍可能被续写。
- 合法记录形如：`{"seq": 12, "ts": "2026-11-15T08:30:00Z", "temp": 26.315}`
  - `seq`：整数，全局按 **+1 连续递增**
  - `ts`：UTC 的 ISO-8601 字符串（必须带时区且偏移为 0，推荐以 `Z` 结尾）
  - `temp`：有限数值（拒绝 `NaN` / `Infinity` / 非数值）

## 二、采集正确性保证（核心需求）

1. **只处理上次成功后新增的字节**：采集进度持久化在 SQLite 的 `file_state` 表，
   每次从已确认的字节偏移继续读。
2. **只纳入以换行结束的合法记录**：读取后按最后一个 `\n` 切分，换行之后的
   **半行**保留在文件中、偏移不前移，下一轮再读。
3. **按 inode 跟踪进度，而非路径**：
   - 轮转后旧文件换了路径（变成 `.1`）但 inode 不变，仍从原偏移续写采集；
   - 新文件 inode 未见过，从头采集；
   - 因而**轮转与采集交错、服务重启、重复点击**都能接续，不漏记、不重记。
   - 另含防御：inode 被文件系统复用（路径角色变化）或文件被截断时从头读，
     由 `seq` 唯一约束与原文比对兜底，陈旧 inode 进度自动清理。
4. **一轮采集 = 一个事务**，任一完整行出现下列情况即**整轮拒绝**，
   已入库记录与采集进度均不变：
   - 非法 JSON / 不是对象 / 空行；
   - `seq` 非整数、`ts` 非 UTC 时刻、`temp` 非有限数值；
   - 序号未按 +1 连续递增；
   - 同一 `seq` 出现不同内容（批内或与库内冲突）。
   - 同序号且内容完全一致视为重复传输，跳过计数、不重复入库。
5. **并发保护**：进程内采集锁 + SQLite 唯一约束，重复/并发点击不会重复入库。

每次采集（含被拒绝的轮次）都在 `collect_runs` 表留痕，页面“最近采集轮次”可见
状态、完整行数、新增/重复数、半行字节数与拒绝原因。

## 三、目录结构

```
.
├── app/
│   ├── __init__.py        # Flask 路由：页面 / 采集 / CSV / 演示重置 / 健康检查
│   ├── collector.py       # 采集核心：增量读取、半行切分、校验、事务、进度表
│   └── templates/index.html
├── demo/
│   └── device_sim.py      # 设备模拟器（仅标准库）：write/half/rotate/illegal/seed/auto ...
├── tests/
│   └── test_collector.py  # 15 个 unittest 用例（采集器 + Web 端到端）
├── scripts/               # 本地启动 / 测试 / 演示的快捷脚本
├── data/                  # Compose 挂载的日志目录（git 仅保留 .gitkeep）
├── db/                    # SQLite 持久化目录（容器内 /instance）
├── run.py                 # 启动入口
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 四、用 Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 网页：`http://localhost:8000/`
- CSV：`http://localhost:8000/records.csv`
- 健康检查：`http://localhost:8000/healthz`
- 卷挂载：`./data -> /data`（日志）、`./db -> /instance`（SQLite）
- 演示重置开关 `ENABLE_DEMO_RESET=1` 已在 compose 中开启，页面显示“演示重置”按钮。

另开一个终端，用模拟器对挂载目录造数据（容器与宿主机共享 `./data`）：

```bash
# 方式 A：一键完整剧本（自动调用网页的 /collect，含半行、轮转、非法拒绝、恢复）
python3 -I demo/device_sim.py --data-dir ./data auto --base-url http://localhost:8000

# 方式 B：手动逐步演示
python3 -I demo/device_sim.py --data-dir ./data seed     # 预置：.1 含半行、双文件样本
# 浏览器点“立即采集” -> 5 条入库，页面显示半行 30 字节
python3 -I demo/device_sim.py --data-dir ./data finish-half
# 再点“立即采集” -> 半行补录为 seq 6
```

> 模拟器与应用代码分离：`demo/` 是独立目录、只用标准库，可单独拷贝到宿主机运行。
> 半行辅助元数据 `.half.json` 仅模拟器使用，采集端只读 `active.ndjson*` 两个文件。

## 五、不用 Docker 的本地运行

需要 Python 3.11+ 与 Flask：

```bash
python3 -m pip install -r requirements.txt   # 或用虚拟环境
./scripts/run_local.sh                       # 默认 http://localhost:8000
# 另开终端
./scripts/demo.sh seed
./scripts/demo.sh auto
```

## 六、模拟器子命令

| 命令 | 作用 |
| --- | --- |
| `write N` | 追加 N 条完整合法记录（序号自动接续，统计时计入末尾半行占用的序号） |
| `half` | 写入一条不带换行的半行，并记录 `.half.json` 元数据 |
| `finish-half` | 在两份日志中找到半行并无损补全、加换行 |
| `rotate` | 模拟归档：`active.ndjson` 重命名为 `.1`，再新建空 `active.ndjson` |
| `illegal KIND` | 写一条完整但非法的行：`nan` / `bad-json` / `bad-ts` / `dup-conflict` |
| `seed` | 预置含半行与轮转文件的演示样本 |
| `reset` | 清空两份日志与半行元数据（数据库用网页“演示重置”清理） |
| `auto` | 一键编排 8 步完整剧本并自动调用采集接口 |

## 七、测试

```bash
# 标准库 unittest；若 Flask 在非标准 site-packages，用 TEST_SITE 指定
TEST_SITE=/path/to/site-packages ./scripts/run_tests.sh
# 或
python3 -m unittest discover -s tests -v
```

覆盖：基础增量与幂等点击、半行留待下次、轮转后旧文件续写+新文件接续、
双轮转与陈旧 inode 清理、inode 复用防御、各类非法行整轮拒绝且进度不变、
序号断档/同序号异内容/同序号同内容、重启恢复、轮转与采集交错、Web 页面与 CSV。

## 八、数据库表

- `records`：质控记录（`seq` 唯一）、原文 `raw`、来源文件与 inode、采集时间。
- `file_state`：按 inode 的采集偏移、观测大小、当前路径、更新时间。
- `collect_runs`：每轮采集的状态（`ok`/`rejected`/`error`）与明细、错误原因。
