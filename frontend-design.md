# 前端设计文档 · AI共创轮回世界(黑客松版)

版本:v1.0 | 配套文档:《后端设计文档》(第7节接口契约与后端文档逐字一致,契约以后端文档为准源)

---

## 1. 前端一句话

三个交付物:**手机端 H5**(扫码即玩的参与端)、**大屏端**(展位的上帝视角舞台)、**结局卡页**(可分享的传播物料)。全部纯静态部署,通过 2 秒轮询消费同一个增量接口,不用 WebSocket。

## 2. 术语速览(与后端共用)

轮(round)=全局输入计数,60轮一局;局(cycle)=一次轮回;节点事件=第20轮异象/第40轮打击文明/第60轮毁灭;directive=每个地点当前的任务简报;hope=隐藏世界变量,前端只拿到 low/mid/high 三档暗示;神谕=上帝视角输入,可能"applied(生效)"或"pooled(存入候选池)"。

## 3. 手机端 H5

### 3.1 页面流

```
扫码落地页 → 自述输入页 → 角色卡揭示页 → 主界面(常驻) → 退场确认 → 结局卡页
```

**落地页**:世界观三句话 + 当前局面一行字("第2轮回·第37轮·毁灭将至")+「进入世界」按钮。加载时请求一次 /api/story 拿世界状态。

**自述输入页**:一个输入框("用一句话描述你自己,世界会为你写下角色")+ 可选邮箱框(文案:"留下邮箱,你的角色死后会给你写信")。提交 POST /api/join,等待期间播放"世界正在感知你…"动效(最长8秒,后端有兜底)。

**角色卡揭示页**:名字 + 角色卡全文 + 所在地点,卡片翻转/浮现动效(这是第一个高光时刻,值得做动画)。charId 写入 localStorage(key: `world_charId`),用户刷新/断线后凭此恢复,跳过 join。

**主界面**(用户停留最久的页面),自上而下:

1. 世界进度条:`第 {round}/60 轮`,进度条颜色随 hopeHint 变化(low=暗红 / mid=灰蓝 / high=金),接近节点轮(18-20、38-40、58-60)时脉冲闪烁提示"异象将至"。
2. directive 横幅:当前所在地点的 situation + hint,常驻置顶,内容变化时高亮滚动一次。
3. 故事流 tab ×2:「世界」(本地点最近幕的 narrative 流)/「我的故事」(GET /api/me/:charId 的 timeline,personal 视角文本)。
4. 输入区:文本框 + 三态切换器「行动 / 旁白 / 神谕」。神谕态下追加 scope 选择(本地 / 全世界)并给出提示文案"神谕稀少,每轮只有一道意志能成真"。
5. 退场按钮(次要位置):"离开这个世界"。

### 3.2 提交反馈状态机(关键体验)

```
提交 → pending("你的行动正被编入命运…")
  → accepted:轻提示"已入戏,第{round}轮",数秒后新幕出现在故事流,自己的名字高亮
  → accepted + oracleStatus=pooled:"你的意志尚未汇聚成现实,但已被世界记住"
  → accepted=false:展示 reason(世界内话术),输入框内容保留供修改,不清空
  → error.code=SETTLING:"世界正在被审判",禁用输入直到新局
```

驳回与降级永远用世界内话术呈现,绝不出现"审核失败/请求被拒绝"字样。

### 3.3 退场与结局卡页

点击退场 → 确认弹层("离开后,世界会为你写下结局")→ POST /api/leave 拿 cardUrl → 跳转结局卡页。卡页轮询 GET /api/card/:charId 直至 ending 就绪(期间播"命运正在收束…")。

结局卡页 = 传播物料,设计要素:角色名 + 一句最点睛的结局文案(取 ending 前两行)+ 所属轮回编号("第2轮回·幸存者/牺牲者")+ 世界二维码 + 「保存长图」按钮(html2canvas 生成)。竖版 9:16,发朋友圈/小红书不用裁剪。

## 4. 大屏端

展位的脸面,视觉预算最高的页面。1080p 横屏,自动全屏,无任何交互(纯展示,现场没人操作它)。

### 4.1 布局

```
┌────────────────────────────────────────────────┐
│  顶栏:第{cycle}轮回 · 第{round}/60轮 · 天色条(hope)  │
├───────────────┬───────────────┬────────────────┤
│  地点A 分区     │  地点B 分区    │   地点C 分区     │
│  directive     │  directive    │   directive    │
│  故事流(打字机) │  故事流        │   故事流        │
├───────────────┴───────────────┴────────────────┤
│  岁月史书带:编年史条目横向流动(chronicle,史书体)      │
├────────────────────────────────────────────────┤
│  角色卡墙:头像名牌横排,新加入者放大入场动效,离场者变灰 │
└────────────────────────────────────────────────┘
```

### 4.2 关键表现

- **打字机效果**:新幕 narrative 逐字打出,每地点分区独立节奏,大屏永远"在动"。
- **hope 暗示**:不显示数字。顶栏天色条 + 全屏色调滤镜随 hopeHint 渐变(low 时整屏压暗偏红,high 时透金光),玩家隐约感到世界在变好或变坏。
- **岁月史书带**:chronicle 条目从右向左缓慢流动,衬线字体+卷轴质感,是"编年史正在被书写"的视觉化。
- **节点事件全屏接管**:轮询发现 type=node 的幕 → 全屏特效接管3-5秒(异象=天空撕裂闪白;打击=震屏+裂纹;毁灭=整屏熄灭再亮起结算文字),配合现场打印机同时吐"天启公告",这是展位每局三次的仪式感巅峰。
- **结算与新局**:type=settlement 幕触发终章画面(拯救=金色史诗滚动字幕 / 毁灭=灰烬飘落),停留20-30秒(正好覆盖后端重置耗时),然后"第{cycle+1}轮回 开启"。
- **高亮新人**:新 join 的角色卡在卡墙放大展示3秒并伴随入场一句话——玩家扫码后抬头就能在大屏看到自己,3分钟承诺的高光时刻。

### 4.3 数据消费

单一循环:每2秒 GET /api/story?after={本地最大actId},增量渲染。断网静默重试,画面维持不闪断。启动时 after=0 全量回放最近50幕快速填充画面。

## 5. 状态管理与工程约定

- 技术栈:任意熟悉的(Vue/React/原生均可),不引入重型状态库,一个全局 store 对象足够。
- localStorage:`world_charId`、`world_lastActId`(手机端故事流断点)。
- 环境变量:`API_BASE`(联调切换 mock/真实后端)。
- 轮询规范:手机端主界面 3s 轮询 story(after 游标)+ 10s 轮询 me;大屏 2s 轮询 story。页面不可见时(document.hidden)暂停轮询。
- 所有时间显示用轮数不用时钟(世界没有现实时间)。

## 6. Mock 先行(并行开发的关键)

后端 M1 贯通前,前端用本地 mock 起跑。建一个 `mock/story.js`,内置下方样例并每3秒自动追加一条假幕(轮数+1),即可开发全部页面与动效。样例数据:

```json
{
  "world": { "cycle": 2, "round": 37, "maxRound": 60, "hopeHint": "low", "status": "running" },
  "locations": [
    { "id": "loc_shelter", "name": "避难所", "directive": { "situation": "地下水源正在枯竭,居民出现幻觉", "hint": "找到复苏水源的方法,或安抚恐慌的居民" } },
    { "id": "loc_ruins", "name": "废墟", "directive": { "situation": "废墟深处传出规律的敲击声", "hint": "查明声源,它可能是希望也可能是灾祸" } },
    { "id": "loc_observatory", "name": "观测站", "directive": { "situation": "天象仪指向了不存在的星座", "hint": "破译星图,异象的答案藏在其中" } }
  ],
  "acts": [
    { "id": 101, "cycle": 2, "round": 36, "location": "loc_ruins", "type": "act",
      "narrative": "林拾荒者撬开了那扇锈门,敲击声戛然而止。门后是一间完好的水泵房,墙上用旧世界的文字写着:泵还活着,人还没死绝。",
      "chronicle": "第三十六轮,废墟深处,拾荒者寻得活泵。", "involved": ["c_01"], "importance": 4 },
    { "id": 102, "cycle": 2, "round": 37, "location": "loc_shelter", "type": "act",
      "narrative": "神谕降下:让孩子们唱歌。歌声在幻觉蔓延的走廊里响起,恐慌像退潮一样安静下去。",
      "chronicle": "第三十七轮,歌声抚平避难所。", "involved": ["c_02", "c_03"], "importance": 3 }
  ],
  "characters": [
    { "charId": "c_01", "name": "林拾荒者", "type": "human", "status": "active", "location": "loc_ruins" },
    { "charId": "c_02", "name": "守夜人梅", "type": "ai", "status": "active", "location": "loc_shelter" },
    { "charId": "c_03", "name": "小满", "type": "human", "status": "active", "location": "loc_shelter" }
  ]
}
```

/api/me mock:

```json
{ "profile": "林拾荒者:靠一双手在废墟里活了十年,不信神,只信工具。",
  "status": "active", "location": "loc_ruins",
  "timeline": [
    { "actId": 98, "round": 33, "text": "你听见了废墟深处的敲击声,那节奏像是某种求救。" },
    { "actId": 101, "round": 36, "text": "你撬开锈门,找到了活着的水泵——你可能刚刚救了所有人。" }
  ], "ending": null, "echo": "你总觉得这扇门,你好像开过一次。" }
```

## 7. HTTP API(接口契约 · 与后端文档逐字一致)

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
acts 仅返回 id > after 的增量,上限50条。characters 全量。

### GET /api/me/:charId
res `{ "profile": string, "status": string, "location": string, "timeline": [ { "actId": int, "round": int, "text": "personal视角文本" } ], "ending": string|null, "echo": string|null }`

### GET /api/card/:charId
res `{ "name": string, "profile": string, "ending": string|null, "cycle": int, "qrUrl": string }`(ending=null 表示生成中,卡页每3秒轮询)

## 8. 视觉方向建议

基调:末日编年史。深底色 + 羊皮纸/星图纹理点缀;标题衬线体(思源宋体/霞鹜文楷),正文无衬线;三个高光时刻优先投入动效预算——角色卡揭示、大屏新人入场、节点事件全屏接管。移动端一切按钮≥44px,展会现场单手快速操作。

## 9. 开发里程碑

1. **M1 Mock 起跑**:手机端全页面流跑通(mock 数据),localStorage 恢复逻辑。
2. **M2 大屏**:三分区布局 + 打字机 + 史书带 + 卡墙(仍是 mock)。
3. **M3 联调**:切 API_BASE 对接后端 M1/M2,消化真实延迟与驳回话术。
4. **M4 高光时刻**:节点全屏特效、hope 色调、角色卡揭示动效、终章画面。
5. **M5 结局卡与打磨**:卡页 + 保存长图 + 真机排版走查 + 展会灯光下的对比度检查。
