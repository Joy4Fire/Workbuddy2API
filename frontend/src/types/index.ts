// 后端 /admin/* API 返回的 TS 类型定义

export interface AccountInfo {
  uid: string
  enabled: boolean
  /** 禁用原因（"手动停用"/"保活连续失败…"），启用时为空 */
  disabled_reason?: string
  healthy: boolean
  cooldown_until: number
  failure_count: number
  credits_remaining: number | null
  credits_total: number | null
  /** 积分最早到期时间戳（秒），无则 null */
  credits_expire_at?: number | null
  /** 选号优先级（越大权重越高，0=默认） */
  priority?: number
  checkin_today?: boolean
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
  /** 环境变量里已有的值（只读提示，避免用户误以为"没配置"） */
  env_backend?: string
  env_proxy?: string
  env_workbuddy_exe?: string
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
  /** 成本系数（倍率） */
  credits?: number
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
  already: boolean
  message?: string
}

export interface CheckinResponse {
  ok: boolean
  results: CheckinResult[]
  skipped?: string[]
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
