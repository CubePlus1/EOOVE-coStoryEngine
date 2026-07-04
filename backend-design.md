# 后端设计文档 · AI共创轮回世界(黑客松版)

版本:v1.0 | 负责人:zzr | 配套文档:《前端设计文档》(接口契约章节两份文档完全一致,以本文档为准源)

---

## 1. 系统一句话

一个由玩家输入驱动的轮回世界:所有输入经审核后计入全局轮数,由按地点分片的编织引擎实时织成故事,产出结构化事件,事件触发视图投影、热敏打印、邮件外溢、结局卡等 skills;第60轮毁灭降临,按 hope 值结算世界存亡,随后开启新轮回。

## 2. 术语表(前后端共用,不得私自改义)

| 术语 | 定义 |
|---|---|
| 幕 (act) | 一条输入被编织后产出的一段故事,内容单位 |
| 轮 (round) | 全局输入计数器,每条**通过审核**的输入 +1,与时间无关 |
| 局 / 轮回 (cycle) | 60轮为一局,毁灭结算后 cycle+1 开启新局 |
| 节点事件 | 第20轮异象、第40轮打击文明、第60轮毁灭降临,不可被任何输入取消或改期 |
| hope | 隐藏世界变量,每幕结算 hopeDelta,第60轮判定 hope≥阈值 则世界得救 |
| 地点 (location) | 编织分片单位,地点内串行、地点间并行,默认3个(待拍板) |
| directive | 引擎给每个地点的当前任务简报(situation + hint),引导玩家 |
| 神谕 (oracle) | 上帝视角输入,按 (scope, round) 配额抢位,落选进候选池 |
| 心跳 (heartbeat) | 全局空闲超时后系统自产的输入,防空场,同样计轮 |
| 编年史 (chronicle) | 每幕产出的史书体一句话,岁月史书前端动画的数据源 |

## 3. 待拍板项与默认值(不阻塞开发)

| 事项 | 默认值 | 备注 |
|---|---|---|
| 地点数量 | 3个(避难所/废墟/观测站,可换皮) | 编织器数=并发LLM数=成本 |
| 节点事件触发方式 | 全地点同步 | 错开蔓延留 v2 |
| 结算地板线 | 不做,靠心跳10秒托底节奏 | 若demo彩排太快再加 |
| hope 阈值 | 50(初始40,区间0-100) | 调参项 |
| 心跳空闲阈值 | 10秒 | 调参项 |

## 4. 总体架构与并发模型

单进程单服务(Node.js 或 Python FastAPI 均可,建议选你最熟的),SQLite 落盘,前端纯静态。**不上 Redis / 消息队列 / WebSocket / Neo4j。**

并发模型:进程内事件循环。全局轮计数器是内存变量,在"审核通过"回调中同步 +1(单线程天然无竞态),随后异步派发至地点编织器。每个地点编织器持有独立的内存队列 + 一个串行消费循环(同一地点同时只有一次 LLM 编织在进行);地点之间互不阻塞。

```
输入(HTTP) ─→ 审核agent ─→ 全局轮计数器+1 ─→ 节点检查 ─→ 地点路由
                 │驳回                                        │
                 └→ 返回世界内话术                    地点队列(串行消费)
                                                              │
                                                        地点编织器(LLM)
                                                              │
                                                        事件总线(进程内)
                                        ┌──────────┬──────────┼──────────┬─────────┐
                                    视图投影    打印队列    邮件skill   结局卡    三元组写回
```

## 5. 数据模型(SQLite DDL)

```sql
CREATE TABLE world (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cycle INTEGER NOT NULL DEFAULT 1,
  round INTEGER NOT NULL DEFAULT 0,
  hope INTEGER NOT NULL DEFAULT 40,
  status TEXT NOT NULL DEFAULT 'running'  -- running | settling | ended
);

CREATE TABLE characters (
  id TEXT PRIMARY KEY,              -- c_xxxx
  name TEXT NOT NULL,
  profile TEXT NOT NULL,            -- AI生成角色卡全文
  type TEXT NOT NULL,               -- human | ai
  status TEXT NOT NULL,             -- active | leaving | ended
  location TEXT NOT NULL,           -- loc_xxx
  email TEXT,                       -- 可空,用户自愿留
  ending TEXT,                      -- 退场结局文本,null=未退场
  echo TEXT,                        -- 轮回残响:上一cycle压缩记忆碎片
  cycle_joined INTEGER NOT NULL
);

CREATE TABLE acts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 全局事件id,前端轮询游标
  cycle INTEGER NOT NULL,
  round INTEGER NOT NULL,
  location TEXT NOT NULL,
  type TEXT NOT NULL,               -- act | node | settlement | heartbeat
  narrative TEXT NOT NULL,
  chronicle TEXT,
  directive_json TEXT,              -- {situation, hint}
  involved_json TEXT NOT NULL,      -- ["c_01", ...] 不得为空数组
  personal_json TEXT,               -- {"c_01": "你视角的一句话"}
  importance INTEGER NOT NULL,      -- 1-5
  hope_delta INTEGER NOT NULL,
  oracle_applied TEXT,              -- 本幕采纳的神谕原文
  created_at INTEGER NOT NULL
);

CREATE TABLE oracle_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id TEXT NOT NULL,
  scope TEXT NOT NULL,              -- global | loc_xxx
  text TEXT NOT NULL,
  round_submitted INTEGER NOT NULL,
  status TEXT NOT NULL              -- applied | pooled | expired
);

CREATE TABLE triples (               -- 穷人版知识图谱,防设定漂移
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT, relation TEXT, object TEXT,
  cycle INTEGER, round INTEGER
);

CREATE TABLE print_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,               -- charcard | bulletin | apocalypse | ending
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'  -- pending | printed | dropped
);

CREATE TABLE inputs_log (            -- 审计与重放
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT, payload_json TEXT, verdict TEXT, created_at INTEGER
);
```

重启恢复:进程启动时从 world 表恢复 cycle/round/hope,acts 表恢复各地点最近3幕上下文,接着演。

## 6. 输入管线

### 6.1 输入类型(统一事件封装)

```json
{ "type": "join",      "payload": { "selfDesc": "...", "email": "可选" } }
{ "type": "act",       "payload": { "charId": "c_01", "text": "...", "kind": "action | narration" } }
{ "type": "oracle",    "payload": { "charId": "c_01", "scope": "global | loc_xxx", "text": "..." } }
{ "type": "leave",     "payload": { "charId": "c_01" } }
{ "type": "heartbeat", "payload": { "hint": "预埋剧情钩子池随机一条" } }
```

邮件回流(v2 可选):解析回信正文 → 包装为 act,kind=narration,charId 由邮箱反查。

### 6.2 审核 agent

- 模型:最快的小模型;输入 = 世界观铁律(固定短文本)+ 该条输入;输出 `{pass, reason}`。
- 目标延迟 < 2秒,超时默认放行(宁可漏放不可卡死体验)。
- 神谕额外校验一条硬规则:**不得取消/提前/推迟节点事件**,违者驳回。
- 驳回响应必须是世界内话术(reason 由审核模型直接以世界口吻生成),且**不计轮数**。

### 6.3 轮计数与节点检查

审核通过 → `world.round += 1`(同步)→ 若 round ∈ {20, 40, 60}:

- 生成节点事件幕(type=node),强制覆写所有地点的 directive 为节点专属文本;
- 进打印队列(kind=apocalypse);
- round=60 时进入结算流程(见第9节),本条输入正常编织后世界锁定 settling。

### 6.4 神谕仲裁

配额:每轮每 scope 1个名额(global 1 + 每地点各1)。同轮同 scope 的后来者写入 oracle_pool(status=pooled),响应话术:"你的意志尚未汇聚成现实,但已被世界记住"。下一轮编织时,该 scope 若无新神谕,从池中按先入先出取一条升格采纳(status=applied),超过5轮未采纳则 expired。被采纳的神谕成为该地点既成事实,写入下一幕编织上下文。

## 7. 地点编织器(核心)

### 7.1 编织上下文(每次 LLM 调用携带)

1. 世界观全文(固定,置顶,含铁律)
2. 编年史全量(每轮1条,60轮≈2千token,**永不压缩,防漂移之锚**)
3. 本地点最近3幕原文
4. 本幕 involved 角色卡 + 其相关三元组(按 charId 查 triples)
5. 当前 directive、当前采纳神谕(若有)、当前 round/cycle、hope 的模糊档位(低/中/高,不给具体数)
6. 本条输入原文

Token 兜底:上下文超阈值(建议 8k)时仅压缩"本地点旧幕",编年史与世界观不动。

### 7.2 编织输出 schema(v3,严格 JSON)

```json
{
  "narrative": "本幕正文,≤150字",
  "directive": { "situation": "≤40字", "hint": "≤30字" },
  "chronicle": "史书体一句话,≤30字",
  "involved": ["c_01"],
  "personal": { "c_01": "该角色视角一句话" },
  "importance": 3,
  "hopeDelta": 2,
  "stateChanges": { "characters": { "c_01": { "location": "loc_city", "note": "受伤" } } },
  "newTriples": [["c_01", "结盟", "c_07"]],
  "crossLocation": [{ "to": "loc_city", "seed": "废墟方向升起烟柱" }],
  "print": { "worthy": false, "ticket": null }
}
```

硬约束(写进 prompt + 代码双重校验):involved 不得为空;新加入角色必须出现在其地点下一幕的 involved 中;后续输入必须承认本地点已发生事实;神谕 hopeDelta 权重可到 ±5,普通行动 ±3。

解析失败降级:JSON 解析失败 → 重试1次 → 仍失败则产出一条旁白幕("世界轻轻震颤了一下"),importance=1,hopeDelta=0,保证管线永不断。

### 7.3 crossLocation 派发

编织完成后,将 crossLocation 中每条 seed 包装为低优先级环境输入注入目标地点队列(不计轮数,不走审核),目标地点下一幕自然引用。

### 7.4 心跳生成器

全局定时器:距上一条通过审核的输入超过10秒 → 从预埋钩子池取一条生成 heartbeat 输入(正常走审核与计轮)。钩子池由内容组提供(每地点≥10条),用完循环。心跳幕 importance 上限 2,不触发打印与邮件。

## 8. 事件总线与 skills

进程内 pub/sub(一个 EventEmitter 足矣)。每幕落库 acts 后发布,skills 订阅:

| skill | 触发条件 | 动作 |
|---|---|---|
| 视图投影 | 每幕 | 无需动作,前端轮询 acts 表即是投影 |
| 三元组写回 | newTriples 非空 | 写 triples 表 |
| 打印队列 | join(charcard)/ importance≥4(bulletin)/ 节点(apocalypse)/ 退场(ending) | 入 print_queue |
| 邮件 skill | importance≥4 且 involved 含已离场留邮箱用户;或结局卡生成 | 发送(白名单+频控) |
| 结局卡 | leave 输入编织出退场幕后 | 调结局 prompt 生成 ending,渲染卡页 |

### 8.1 打印服务

独立小进程/线程轮询 print_queue(status=pending),按 id 顺序打。队列积压>5张时:丢弃 bulletin(status=dropped),保 charcard / apocalypse / ending。票面模板由内容组提供文案结构,底部固定印世界二维码。热敏打印机走 USB/串口,用现成库(node-thermal-printer / python-escpos)。

### 8.2 邮件 skill

频控:每用户每小时≤1封(结局卡邮件豁免)。demo 白名单模式:环境变量 MAIL_WHITELIST,只对名单内地址真发,其余落库标记 simulated。发件内容必须携带世界内理由(inWorldReason),邮件底部:"直接回复这封邮件,你的话将进入世界"(回流解析 v2 再做)。

## 9. 结算与轮回

round=60 的输入编织完成后:

1. world.status = settling,拒绝新输入(响应:"世界正在被审判");
2. 生成结算幕(type=settlement):hope ≥ 阈值 → 拯救终章;否则毁灭终章;
3. 全体 active 角色批量生成简短结局(可并发调 LLM),状态置 ended;
4. 轮回残响:将本 cycle 编年史压缩为≤100字"既视感碎片",写入随机30%角色的 echo 字段(老用户回归时角色卡携带);
5. cycle+1,round=0,hope 重置,清空 directive,发布新局开场幕,status=running。

全程目标 < 30秒,期间大屏播终章动画(前端负责撑场)。

## 10. HTTP API(接口契约 · 与前端文档逐字一致)

Base:`/api`,JSON,错误统一 `{ "error": { "code": "REJECTED|QUOTA|NOT_FOUND|SETTLING|INTERNAL", "message": "世界内话术" } }`。CORS 全开。

### POST /api/join
req `{ "selfDesc": string, "email"?: string }`
res `{ "charId": "c_x", "name": string, "profile": string, "location": "loc_x", "cycle": int }`

### POST /api/act
req `{ "charId": string, "text": string, "kind": "action" | "narration" | "oracle", "scope"?: "global" | "loc_x" }`(kind=oracle 时 scope 必填)
res 通过 `{ "accepted": true, "round": int, "oracleStatus"?: "applied" | "pooled" }`
res 驳回 `{ "accepted": false, "reason": "世界内话术" }`(HTTP 200,业务驳回不是错误)

### POST /api/leave
req `{ "charId": string }`
res `{ "cardUrl": "/card/c_x" }`(结局异步生成,卡页轮询就绪)

### GET /api/story?after=<actId>
res:
```json
{
  "world": { "cycle": 2, "round": 37, "maxRound": 60, "hopeHint": "low|mid|high", "status": "running" },
  "locations": [ { "id": "loc_x", "name": "避难所", "directive": { "situation": "...", "hint": "..." } } ],
  "acts": [ { "id": 101, "cycle": 2, "round": 37, "location": "loc_x", "type": "act",
              "narrative": "...", "chronicle": "...", "involved": ["c_01"], "importance": 3 } ],
  "characters": [ { "charId": "c_01", "name": "...", "type": "human", "status": "active", "location": "loc_x" } ]
}
```
acts 仅返回 id > after 的增量,上限50条。characters 全量(数量可控)。

### GET /api/me/:charId
res `{ "profile": string, "status": string, "location": string, "timeline": [ { "actId": int, "round": int, "text": "personal视角文本" } ], "ending": string|null, "echo": string|null }`

### GET /card/:charId
HTML 结局卡页(后端渲染或前端页+接口皆可,定为前端页 + 本接口返回 JSON:`{ "name", "profile", "ending", "cycle", "qrUrl" }`,路径改为 GET /api/card/:charId)。

## 11. LLM 调用规范

| 用途 | 档位 | 超时 | 降级 |
|---|---|---|---|
| 审核 | 最快小模型 | 2s | 默认放行 |
| 角色生成 | 快模型 | 8s | 模板角色卡兜底 |
| 地点编织 | 主力模型 | 15s | 旁白幕兜底 |
| 结局/残响/终章 | 主力模型 | 20s | 通用结局模板 |

所有 prompt 文本独立存放 `prompts/` 目录纯文本文件,内容组可直接改,改完重启生效,不进代码。

## 12. 开发里程碑

1. **M1 管道贯通**(最高优先):join / act / story 三接口 + 假编织(echo输入) + SQLite 落库。前端此刻可切真接口。
2. **M2 真编织**:审核 agent + 轮计数 + 单地点编织器 + schema 校验降级。
3. **M3 世界机制**:三地点并行 + 神谕配额 + 节点事件 + 心跳 + hope 结算轮回。
4. **M4 skills**:打印队列与驱动 + 结局卡 + 邮件(白名单)。
5. **M5 加固**:重启恢复、限长限频、灌数据压测、demo 彩排参数调优。

## 13. 目录结构建议

```
server/
  app.(js|py)          # 入口 + 路由
  pipeline/            # 审核、轮计数、路由、心跳
  weaver/              # 地点编织器 + schema校验降级
  skills/              # printer / mailer / ending / triples
  prompts/             # 全部prompt纯文本(内容组维护)
  db.sqlite
```
