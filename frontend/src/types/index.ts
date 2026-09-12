// 后端 /admin/* API 返回的 TS 类型定义

export interface AccountInfo {
  uid: string
  enabled: boolean
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
