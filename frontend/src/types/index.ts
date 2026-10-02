// 后端 /admin/* API 返回的 TS 类型定义

export interface UpdateInfo {
  current_version: string
  latest_version: string | null
  status: 'unchecked' | 'update_available' | 'local_ahead' | 'up_to_date' | 'unavailable'
  checked_at: number | null
  error: string
  project_url: string
  source_url: string
}

export interface AccountInfo {
  uid: string
  enabled: boolean
  /**
   * **展示用**的停用原因：人工位优先，其次是系统自动禁用原因。
   *
   * 后端 `display_disabled_reason()` 已把两个状态位合成一个字符串——
   * 前端只想知道"这个号为什么选不到"，不需要知道是哪个位干的。
   */
  disabled_reason?: string
  /** 人工停用位（= `!enabled`，与 `auto_disabled_reason` 相互独立） */
  manual_disabled?: boolean
  /**
   * **系统**自动禁用原因（session 失效 / 被上游封禁）。空 = 系统位未置。
   *
   * 点「启用」默认**不**清这个位——否则登录态已废的账号会被放回池子，
   * 下一个请求立刻再吃一次同样的错，看起来像"点了没用"。
   * 确要强制放回需显式传 force，或走 `/clear-auto-disable`。
   */
  auto_disabled_reason?: string
  healthy: boolean
  /** 冷却剩余秒数（后端算好，前端不必拿 cooldown_until 再减） */
  cooldown_remaining?: number
  /** 无权威分类的连续失败次数（达阈值会临时出池） */
  fail_streak?: number
  /** 连败降权到期时间戳（秒），0 = 未降权 */
  degrade_until?: number
  /** 当前在途请求数（只统计"建连→首字节"窗口） */
  in_flight?: number
  /** (模型 → 冷却到期时间戳) 只含仍在冷却中的，模型级限流/不可用只冷却这一对 */
  model_cooldowns?: Record<string, number>
  /** (模型 → 每 1k token 积分消耗) 成本台账，仅含 6h 内测得的 */
  model_costs?: Record<string, number>
  /** 选号权重（后端算好的综合值，越大越优先） */
  weight?: number
  /** 积分构成明细（官方控制台"积分明细"同口径） */
  credit_packages?: { name: string; total: number; used: number; remain: number; expire_at?: number | null }[]
  cooldown_until: number
  failure_count: number
  credits_remaining: number | null
  credits_total: number | null
  /** 积分最早到期时间戳（秒），无则 null */
  credits_expire_at?: number | null
  /** 选号优先级（越大权重越高，0=默认） */
  priority?: number
  /**
   * 今日是否已签到。**优先来自上游实时查询**（`checkin_source === 'upstream'`），
   * 上游状态未知时才回落"本网关的签到记录"，此时 `checkin_source === 'local'`。
   */
  checkin_today?: boolean
  /**
   * 本期签到活动是否开启。`false` 表示该账号暂时签不了 —— 此时 `checkin_today`
   * 为 false 是"活动没开"而不是"用户没签"，界面必须与「未签到」区分开。
   * `null`/缺省 = 未知（还没同步过）。
   */
  checkin_active?: boolean | null
  /** 上游签到状态的同步时间戳（秒） */
  checkin_synced_at?: number | null
  /** `checkin_today` 的来源：upstream=上游真值 / local=本网关记账（上游未知时的回落） */
  checkin_source?: 'upstream' | 'local'
  /** 账号来源：project=项目 auths/（上传/扫码），local=本机 CodeBuddy 目录 */
  source?: 'project' | 'local' | 'unknown'
  /** 账号所属域名（取自 auth 文件里的 auth.domain），区域判定的唯一来源 */
  domain?: string
  /** 区域 id：cn=国内版 / global=国际版 */
  region?: 'cn' | 'global'
  /** 区域显示名（"国内版" / "国际版"），由后端给出，避免前端重复维护映射 */
  region_label?: string
  /**
   * $wbEncrypted 加密登录态的字段名列表（如 ["accessToken","refreshToken"]）。
   * 非空表示该账号当前**不可用**——桌面端 5.6.0+ 加密了凭据，本机解不开。
   */
  auth_encrypted_fields?: string[]
}

export interface Settings {
  checkin_hours: string
  credit_refresh_min: string
  model_refresh_hour: string
  /** 模型缓存 TTL（分钟）：推理端点 /v1/models 惰性刷新的缓存新鲜度上限 */
  model_ttl_min?: string
  aa_refresh_hour: string
  keepalive_hour: string
  /** token 保活开关：'1' 开启（默认），'0' 关闭 */
  keepalive_enabled?: string
  aa_api_key?: string
  aa_api_key_masked?: string
  aa_enabled?: boolean
  /** 清除 AA key 的标记 */
  clear_aa_api_key?: boolean
  /** 积分预警开关：'1' 开启，'0' 关闭 */
  alert_enabled?: string
  /** 预警 webhook 地址（Bark/企业微信/飞书自动识别） */
  alert_webhook_url?: string
  /** 余额占比低于该百分比触发预警（1-90） */
  alert_threshold_percent?: string
  /** 积分 N 天内到期触发预警（1-90） */
  alert_expiry_days?: string
  /** 模型别名映射原文（每行一条：别名=真实模型） */
  model_aliases?: string

  // ---- 系统提示词三模式（P2-1）----
  /**
   * passthrough=不动（默认，零改动）/ custom=整段替换 / append=插在开头 system 之后。
   *
   * `append` 是唯一能保住客户端自己的项目规则的插法（有些客户端会把项目规则
   * 塞在开头几条 system/developer 消息里，直接替换会把它们一起吃掉）。
   */
  prompt_mode?: 'passthrough' | 'custom' | 'append'
  /** custom / append 模式下要注入的提示词原文；passthrough 时忽略 */
  prompt_text?: string

  // ---- 补签保连登（P3 试点）----
  /** 补签总开关：'1' 开启，'0' 关闭（**默认关**） */
  makeup_enabled?: string
  /** 演练模式：'1' 只记录不真补（**默认开**）——判据链有一环未验证，不能默认花用户的卡 */
  makeup_dry_run?: string

  // ---- 猫猫旅行（国内版专属）----
  /** 旅行总开关：'1' 开启，'0' 关闭（**默认关**） */
  travel_enabled?: string
  /**
   * 演练模式：'1' 只看状态、不发写请求（**默认开**）。
   *
   * 与补签的默认演练理由**不同**：旅行不消耗任何资产（纯收益，每天 5~10 积分），
   * 这里默认演练纯粹是"先让用户确认状态机判断对不对"。
   */
  travel_dry_run?: string

  // ---- 活跃地图每日提醒 ----
  /**
   * 提醒总开关：'1' 开启，'0' 关闭（**默认开**）。
   *
   * 这是全项目**唯一默认开**的定时任务，因为它**只读 + 只提醒**：不写上游、
   * 不花积分、不动账号状态，最坏结果就是一条用户本来就要的提醒。
   * 所以它不套用补签/旅行那套"默认关"的保守档，也没有演练模式。
   */
  active_map_enabled?: string
  /** 每日检查时间（小时 0-23，**默认 23**）。用 `>=` 判定：服务晚于该点启动也会补跑 */
  active_map_hour?: string

  // ---- 区域与网络 ----
  // 这三项以前只能改环境变量，现在可在 WebUI 设置；DB 值优先于环境变量，
  // 留空表示"回落环境变量 / 自动判定"。
  /** 账号池区域分布（只读，由后端按账号统计） */
  regions?: RegionCount[]
  /** BACKEND：留空 = 按账号 auth.domain 自动选区域（推荐） */
  backend?: string
  /** PROXY：留空 = 直连 */
  proxy?: string
  /** WORKBUDDY_EXE：留空 = 按平台默认位置探测官方客户端 */
  workbuddy_exe?: string
  /** 单账号在途并发上限（0 = 不限制；留空 = 用环境变量） */
  max_in_flight?: string
  /** 国际版在途并发上限（global 风控更严，单独一档；留空 = 用环境变量） */
  max_in_flight_global?: string
  /** 环境变量里已有的值（只读提示，避免用户误以为"没配置"） */
  env_backend?: string
  env_proxy?: string
  env_workbuddy_exe?: string
  env_max_in_flight?: string
  env_max_in_flight_global?: string
}

/** 账号池的区域分布（设置页展示用） */
export interface RegionCount {
  id: string
  label: string
  count: number
}

export interface ModelReasoning {
  supportsReasoning?: boolean
  onlyReasoning?: boolean
  canDisableThinking?: boolean
  defaultEffort?: string
  supportedEfforts?: string[]
}

export interface AABenchmark {
  name?: string
  creator?: string
  intelligence_index?: number
  coding_index?: number
  /** AA 已不再提供 agentic 指标，第三指标为数学 */
  math_index?: number
  source?: string
  aa_url?: string
}

export interface ModelInfo {
  id: string
  name?: string
  context_length?: number
  max_output_tokens?: number
  reasoning?: ModelReasoning
  /** 模态：multimodal=多模态, text=纯文本 */
  modality?: 'multimodal' | 'text'
  supportsToolCall?: boolean
  supportsImages?: boolean
  /** 成本系数（倍率），合并后取"赢了合并"的那个区域的值 */
  credits?: number
  /**
   * 成本系数**按区域**的明细：{ "cn": 0.29, "global": 0.0 }。
   * 上游对同一模型可能按区域给不同价（实测 hy4-preview 国内 0.29 / 国际 0.00），
   * 而合并只按 reasoning 信息量挑赢家、`credits` 不参与比较 → 明细单独给一份。
   * 只含**真的给了值**的区域；空/缺失表示上游没给（不等于 0）。
   */
  credits_by_region?: Record<string, number>
  temperature?: number
  top_p?: number
  vendor?: string
  description?: string
  /**
   * 可用区域（**基于当前账号池**拉到的目录，不是模型本身的归属）：
   * ["cn"] / ["global"] / ["cn","global"]；空数组 = 未知（目录里没这个模型，或还没拉到）。
   */
  regions?: string[]
  /** AA 评测数据（可选） */
  benchmark?: AABenchmark
}

export interface ProtocolStat {
  protocol: string
  count: number
  tokens: number
}

export interface ModelStat {
  model: string
  count: number
  tokens: number
}

export interface UsageSummary {
  total_requests: number
  total_tokens: number
  today_requests: number
  today_tokens: number
  by_protocol: ProtocolStat[]
  by_model: ModelStat[]
  by_app?: { app: string; count: number; tokens: number }[]
}

export interface UsagePoint {
  bucket_ts: number
  bucket: string
  count: number
  tokens: number
}

export interface UsageRecord {
  id: number
  ts: number
  protocol: string
  model: string
  account_uid: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  latency_ms: number
  status: string
  error?: string | null
  input_content?: string | null
  output_content?: string | null
  reasoning_content?: string | null
  credits?: number | null
  app_name?: string | null
}

export interface Overview {
  accounts: AccountInfo[]
  models: string[]
  usage: UsageSummary
  recent: UsageRecord[]
  /** 积分预警横幅（余额低于阈值/积分即将到期），无预警为空数组 */
  alerts?: { level: string; message: string }[]
  prediction?: {
    remaining_credits: number
    tokens_per_credit: number | null
    predicted_tokens: number | null
    credits_used: number
    tokens_used: number
    models: {
      model: string
      credits: number
      tokens: number
      tokens_per_credit: number
      weight: number
      predicted_tokens: number
    }[]
  }
}

export interface CheckinResult {
  uid: string
  ok: boolean
  /** 今日已签到（跳过，非失败） */
  already?: boolean
  /** 本期签到活动未开启（跳过，非失败，也不该重试） */
  inactive?: boolean
  message?: string
}

export interface CheckinResponse {
  ok: boolean
  results: CheckinResult[]
  /** 被跳过的 uid（已签到 或 活动未开启） */
  skipped?: string[]
  /** 签到后的最新账号列表，省一次列表请求 */
  accounts?: AccountInfo[]
}

export interface RecordsResponse {
  records: UsageRecord[]
  /** 符合筛选条件的总数（服务端分页） */
  total: number
  page?: number
  page_size?: number
}

/** 应用（API Key）信息 */
export interface AppInfo {
  id: number
  name: string
  key_prefix: string
  note: string
  enabled: boolean
  created_at: number
  requests: number
  tokens: number
  credits: number
}

/**
 * 单账号的**活跃连登** / 补签卡状态（`GET /admin/streak`，P3 试点的**唯一**观察入口）。
 *
 * 这是能让人在界面上核对"补签判据对不对"的地方——演练模式的日志在容器里，
 * 不开这个入口就只能盲猜。所以字段名与 `do_makeup()` 的判据链一一对应。
 *
 * ⚠️ 这里的"连登"是**活跃连登**（每天有对话才续上），**不是**账号页那个
 * **签到连登**（每天签到）。上游是两条独立的计数：实测同一账号可以"签到连登 4 天、
 * 活跃连登 0 天"。补签卡挂在 growth 域，所以它补的是活跃连登。
 */
export interface StreakRow {
  uid: string
  /** 查询失败时只有这一个字段（其余缺省） */
  error?: string
  /** 当前**活跃**连登天数（连续有对话的天数），不是签到连登 */
  streak_days?: number
  /** 本月累计活跃天数（= 本月活跃地图里 score>0 的格数） */
  month_total_days?: number
  /** 可用补签卡数 */
  makeup_balance?: number
  /** 补签卡持有上限 */
  makeup_max?: number
  /** 本期已补签的日期（YYYY-MM-DD） */
  makeup_dates?: string[]
  /** 判据针对的日期 = 昨日 */
  yesterday?: string
  /**
   * 昨日在活跃地图里的分数。**0 才满足补签条件**。
   *
   * 这个 score 量的就是**对话活跃度**，而活跃连登正是由它驱动的 ——
   * 所以它是补签判据的**直接度量**，不是"不可靠的代理指标"。
   * （别拿它和账号页的签到状态比：两者本来就不该一致，实测同一天会得出相反结论。）
   */
  yesterday_score?: number | null
  /**
   * **今天**在活跃地图里的分数。`>0` = 今天已点亮（活跃连登今天不会断）。
   *
   * `null` / `undefined` = **地图里没有今天这一格**（无判据），与 `0`
   * （有格子但今天还没活跃）严格分开 —— 上游换了窗口口径时前者会出现，
   * 界面要显示成「无数据」而不是「未点亮」，别拿不存在的数据吓人。
   *
   * 为什么这个字段重要：活跃地图**只由官方客户端驱动**，本网关的对话请求
   * 点不亮它（2026-09-22 受控实验：给 score=0 的账号发两次真实请求后仍是 0）。
   * 所以这是用户判断"今天要不要去官方客户端聊一句"的唯一依据。
   */
  heat_today?: number | null
  /** 昨日是否落在当月（上游只允许补当月，跨月为 false） */
  makeup_allowed?: boolean
  /** 连登档位奖励状态（补签卡的**唯一常规来源**） */
  redemption?: RedemptionState
  /** 抽奖可用次数；查询失败为 null（它是锦上添花，不影响连登信息展示） */
  lottery_chances?: number | null
  /**
   * 猫猫旅行状态。**只有国内版有这套体系**，国际版恒为 `null`（后端按区域跳过，
   * 少打一个必然返回空 data 的请求）——界面要把 `null` 显示成"该区域无此活动"，
   * 而不是"查询失败"。
   */
  travel?: TravelState | null
}

/** `StreakRow.travel`：猫猫旅行的当前状态（`billing.fetch_travel_status` 的返回） */
export interface TravelState {
  /**
   * none=未领养（上游给空 data）/ idle=待派出 / traveling=旅行中 / arrived=已到站可领。
   * 未知取值原样透传——上游以后加了新状态要能立刻看见，别当成 idle 处理。
   */
  state: 'none' | 'idle' | 'traveling' | 'arrived' | string
  /** 今日已派出过（CST 自然日重置）→ **每天只有 1 趟** */
  daily_limit_reached: boolean
  /** 领取到站奖励必带的幂等键；0 = 没有 */
  record_id: number
  /** 到站可领的积分数 */
  reward_credit: number
  /** 地点名（idle 时上游给空） */
  location: string
  arrive_at: number
}

/** 连登奖励的一个档位（7d / 14d / 28d，每档每月可领一次） */
export interface RedemptionTier {
  tier: string
  /** 达标所需的活跃连登天数 */
  days: number
  credit: number
  energy: number
  /** 送几张补签卡 —— 这是补签卡唯一的常规来源 */
  cards: number
  /** 送几次抽奖机会 */
  chances: number
}

/** `StreakRow.redemption`：连登档位奖励的当前状态 */
export interface RedemptionState {
  /** 每档的状态：available=可领 / claimed=本月已领 / locked=未达标 */
  status: Record<string, 'available' | 'claimed' | 'locked'>
  /** status 为 available 的档位（后端派生，顺序从低到高） */
  claimable: string[]
  tiers: RedemptionTier[]
  /** 上游给的下一档提示（可能为 null） */
  next_tier: string | null
  /**
   * 上游给的「距下一档」提示。口径已查清（2026-09-22）：
   * `next_tier_remaining = 档位天数 − 当月最长连续段`。
   *
   * ⚠️ 它**不是** `档位天数 − streak_days`。连登断掉后 `streak_days` 归 0 而它仍可能是
   * 个小数字（实测国内版 `0 天 / 差 2 天`，因为本月最长连续段是 5）——照字面读会让人
   * 以为"再连 2 天就够"。所以**只当参考显示，绝不拿它做判据**，展示时也要与
   * `streak_days` 分开说。判档位一律用 `streak_days` 与 `tiers[].days` 直接比。
   *
   * 上游另有一个 `redemption_status.remaining_days` = **当月最长连续段**，
   * 与它互补（两者相加恒等于档位天数）。后端**刻意没透出**那个字段——完全冗余，
   * 需要时用 `tiers[].days − next_tier_remaining` 反推即可。
   */
  next_tier_remaining: number
}

/** `POST /admin/redeem` 的单账号结果 */
export interface RedeemResult {
  uid: string
  /** skip=没有可领档位 / would-redeem=演练判定可领 / redeem=已发起领取 */
  action: 'skip' | 'would-redeem' | 'redeem'
  reason: string
  streak_days?: number | null
  makeup_balance?: number
  /** 本次判定可领的档位（顺序从低到高） */
  claimable?: string[]
  /** 每档的领取回执 */
  claimed?: {
    tier: string
    ok: boolean
    /** true = 上游按业务规则正常拒绝（天数不足 / 本月已领），不是故障 */
    normal: boolean
    message: string
    granted?: Record<string, number>
  }[]
  /** 抽奖结果（只有显式要求抽奖时才有） */
  draw?: {
    ok: boolean
    normal: boolean
    message: string
    prize?: { code: string; name: string; type: string; credit: number }
  } | null
}

export interface RedeemResponse {
  ok: boolean
  dry_run: boolean
  /** 本次是否顺带抽奖 */
  draw: boolean
  results: RedeemResult[]
}

/** `POST /admin/makeup` 的单账号结果（`action` 是判据链走到哪一步的结论） */
export interface MakeupResult {
  uid: string
  yesterday: string
  /** skip=跳过 / would-makeup=演练判定可补 / makeup=已补 / makeup-failed=补签请求失败 */
  action: 'skip' | 'would-makeup' | 'makeup' | 'makeup-failed'
  /** 人话版原因，直接展示 */
  reason: string
  streak_days?: number
  makeup_balance?: number
}

export interface MakeupResponse {
  ok: boolean
  /** 本次是否演练（true = 没有真的花掉补签卡） */
  dry_run: boolean
  results: MakeupResult[]
}

/**
 * `POST /admin/travel` 的单账号结果。
 *
 * 状态机：`none`(无猫→领养) → `idle`(派出) → `traveling`(等下一轮) → `arrived`(领取)。
 * 到站时**同一轮会顺手把当天那趟派出去**（`claim+depart`），所以一轮可能走两步——
 * 只做一步的话节奏会被 `checkin_hours` 绑死。
 *
 * 结果里**只会出现国内版账号**：国际版没有这套体系，调度层按区域过滤掉了。
 */
export interface TravelResult {
  uid: string
  /**
   * skip=不动 / would-adopt=演练将领养 / adopt=已领养 / adopt-skip=领养被拒
   * （对话量未达门槛是**预期行为**）/ would-claim=演练将领取 / claim=已领取 /
   * claim-skip=领取被正常拒绝 / would-depart=演练将派出 / depart=已派出 /
   * claim+depart=先领后派 / depart-skip=派出被正常拒绝 / depart-failed=派出失败
   */
  action:
    | 'skip'
    | 'would-adopt' | 'adopt' | 'adopt-skip'
    | 'would-claim' | 'claim' | 'claim-skip'
    | 'would-depart' | 'depart' | 'claim+depart' | 'depart-skip' | 'depart-failed'
  /** 人话版原因，直接展示 */
  reason: string
  /** 探测到的状态（查询失败时为 null） */
  state?: string | null
  /** 本次实际领到的积分（只有 claim 成功时才有） */
  reward?: number | null
  daily_limit_reached?: boolean
  record_id?: number
  /** 到站可领的积分数 */
  reward_credit?: number
  location?: string
}

export interface TravelResponse {
  ok: boolean
  /** 本次是否演练（true = 一个写请求都没发） */
  dry_run: boolean
  results: TravelResult[]
}

/**
 * 活跃地图每日检查的一行结果（`scheduler.do_active_map_check` 的返回）。
 *
 * 这个任务**只读**：只查 `growth/heatmap` + `growth/streak`，不写上游、不花积分、
 * 不代发任何对话。所以没有 `dry_run`（本来就没有"会改状态"的分支）。
 */
export interface ActiveMapResult {
  uid: string
  /** 判定针对的日期（今天） */
  date: string
  /**
   * lit=今天已点亮（跳过）/ unlit=今天未点亮（要提醒）/ no-cell=地图里没有今天
   * 这一格（无判据，不提醒）/ query-failed=查询失败 / skip=未处理
   */
  action: 'lit' | 'unlit' | 'no-cell' | 'query-failed' | 'skip'
  /** 人话版原因，直接展示 */
  reason: string
  /** 当前活跃连登天数 */
  streak_days?: number
  month_total_days?: number
  /** 今天在活跃地图里的分数（只有 lit / unlit 才有） */
  heat_today?: number
}

export interface ActiveMapResponse {
  ok: boolean
  /** 本次是否推了 webhook（手动检查默认 false，免得点一次骚扰一条） */
  notify: boolean
  results: ActiveMapResult[]
}

/**
 * 实测积分单价台账的一行：**(账号, 模型)** 维度。
 *
 * 与模型目录里的 `credits` 不是一回事——那是上游标称的成本系数（倍率），
 * 这是按真实请求算出来的 `积分 / token × 1000`（EMA 平滑），
 * 也就是"这个模型实际烧多少额度"。只含最近 `COST_TTL` 窗口内的观测。
 */
export interface CostRow {
  uid: string
  /** 账号所属区域：cn / global */
  region?: string
  region_label?: string
  model: string
  /** 每 1k token 消耗的积分 */
  cost_per_1k: number
  /** 累计样本数（EMA 平滑用） */
  samples: number
  /** 最后一次观测时间（Unix 秒） */
  updated_at: number
  /** 0 = 免费（观测到的消耗为 0，如免费额度包），2 = 有消耗 */
  tier: number
}

export interface CostResponse {
  costs: CostRow[]
  /** 台账窗口（秒），超过窗口的观测会被丢弃 */
  ttl_seconds: number
}
