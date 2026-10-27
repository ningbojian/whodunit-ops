"""业务工具包。

按开发计划逐个放入工具模块，现已全部注册进 app/agent.py 的 ReAct Agent：

  1. extract    文本抽取    解析微信拼场文案 / 预约消息 → 场次 JSON   [已完成]
  2. dedupe     场次去重    门店+剧本+开场时间 作联合唯一键，字段级合并  [已完成]
  3. expiry     过期过滤    剔除已开场失效场次                     [已完成]
  4. risk       空场风险    剩余时长 + 空位 → 会不会黄 / 该先救哪场  [已完成]
  5. pricing    动态定价    沉没成本 + 边际成本 → 推荐售价 / 保本底价  [已完成]
  6. storage    表格读写    双表模型：场次表 + 预约明细表         [已完成]
  7. copywriter 文案生成    两套朋友圈/社群文案，不泄露保本底价        [已完成]
  8. scripts    剧本库      剧本档案 / 缩写别名 / 缺失字段         [已完成]
  9. resources  资源管理    房间 / DM / 班次与缺口清单           [已完成]
 10. scheduling 资源排期    固定场次的房间 / DM 自动分配         [已完成]
 11. dm_control 场控查询    阶段 / 计时 / 超时 / 硬件状态，只读   [已完成]
 12. web_search 联网搜索    剧本公开信息（Tavily），未配置时禁用  [已完成]

开发顺序（先确定性、后 LLM）：
  storage(已完成) → expiry(已完成) → dedupe(已完成) → risk(已完成) → pricing(已完成) → extract(已完成) → copywriter(已完成) → scripts(已完成) → resources(已完成) → scheduling(已完成) → dm_control(已完成) → web_search(已完成) → 组装(已完成)

关于 Agent 层（app/agent.py）：

  build_store_tools() 用 @tool 把 12 个业务函数包装成 Agent 工具，统一返回
  JSON 字符串信封；build_agent() 用 create_agent + InMemorySaver 组装，
  run_turn() 按 thread_id 隔离会话。Agent 层只做参数适配、依赖注入、
  路由与结果转述，不重复实现任何业务规则；写入只发生在 store_data 的
  写 action、script_library 的 upsert、manage_resources 的资源写 action
  与 schedule_sessions 的 auto_assign / apply / resolve；dm_control 只读。

关于 storage 的双表模型：

  data/sessions.xlsx   场次表     —— 有哪些场、什么时候开、几位、多少钱
  data/bookings.xlsx   预约明细表 —— 谁约了哪一场
  data/scripts.xlsx    剧本库     —— 剧本本身的信息与缩写，独立于排期

  已约人数不直接存在场次表里当真相来源，而是从预约明细汇总出来。
  场次表里的 booked_seats / remaining_seats 都是派生字段，每次读取时重算，
  文件里存的数字一律不信任。

关于场次合并：

  「同一场怎么合并」全项目只有一份实现（storage 第四节），
  dedupe 负责分组、冲突报告与现有表比对，upsert_sessions 落盘时复用同一份规则。
  依赖方向只有 dedupe -> storage；storage 不 import dedupe，不会循环导入。

关于文本抽取：

  extract 只读文本、不写库，输出 sessions 与 bookings 两组记录。
  sessions 经 sessions_frame() 可直接喂 dedupe；bookings 的 candidate_session_id
  只是候选，Agent 必须先查库确认场次存在，再调 storage 的预约 / 跳车接口。
  LLM 调用走 app/llm.py，测试可注入 FakeLLM，不需要 API key 或网络。

关于文案生成：

  copywriter 接收 load_sessions_with_counts() 的场次表，内部调用 risk / pricing
  完成选场：高危 → 关注 → 正常逐级回退，满员 / 过期 / 已取消 / 信息不完整
  不进入文案；一次只处理一个门店。选场只读，不写库、不自动发布。

  脱敏有两层：pricing.public_view() 剔除四个内部财务列，copywriter 再用
  COPY_FIELDS 白名单挡住 public_view 仍保留的 contribution_per_seat、风险列
  与 source_text。生成后若出现内部财务词或未授权价格 / 折扣，整次失败，
  community 与 moments 同时为空。店长复核是发布前最后一步。

关于剧本库：

  scripts 与 storage 是两张独立的表：场次回答「什么时候开、谁约了」，
  剧本库回答「这个本是什么类型、几人本、多久、有哪些缩写」。
  剧本库允许残缺：只要店长给出全名或至少一个别名，就先落一条草稿，
  返回 missing_fields 让下一轮继续补；补全时用 script_id 定位同一条档案。

  缩写联想由 Agent 的模型提出候选，工具本身不联网、不猜名字；
  search 只查本地剧本库与场次历史，模型候选必须经店长确认后才能 upsert。
  这样不会把「年轮」和「年轮（重制版）」之类的相似本静默合并。

关于联网搜索（app/integrations/web_search.py）：

  web_search 是唯一联网工具，实现在 integrations 而非 tools（业务工具包保持离线）。
  三层成本闸门全部在调用 Tavily 之前：入参校验（长度 / 拒绝名单 / 剧本域
  正向信号）、租户日配额（默认 10）、全局日配额（默认 30）。未配置
  WEB_SEARCH_API_KEY 时恒返回 not_configured，不产生任何费用。
  搜索结果只作参考：绝不自动写剧本库，店长确认全名后走 script_library。
"""
