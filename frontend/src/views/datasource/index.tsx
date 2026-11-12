import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/datasource'
const columns = [
  "连接编号",
  "连接名称",
  "数据库类型",
  "连接地址",
  "默认库名",
  "连接池大小",
  "探测超时",
  "探活模式",
  "复检结论",
  "失败分级",
  "失败阶段",
  "失败原因",
  "最近成功探测时间",
  "最近探测时间",
]

type Row = Record<string, string | number | null>
type Rules = {
  supported_db_types: string[]
  probe_timeout: { min: number; max: number; default: number }
  connection_pool: { min: number; max: number; default: number }
  address_format: string
  probe_modes: string[]
  save_conditions: string[]
}

const emptyForm: Record<string, string | number> = {
  连接编号: '',
  连接名称: '',
  数据库类型: 'MySQL',
  连接地址: '',
  认证凭据: '',
  默认库名: '',
  连接池大小: 10,
  探测超时: 5,
  探活模式: '模拟',
}

function conclusionClass(conclusion: string | null | undefined): string {
  if (conclusion === '通过') return 'tag tag-ok'
  if (conclusion === '未通过') return 'tag tag-bad'
  return 'tag tag-idle'
}

export default function DatasourceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [rules, setRules] = useState<Rules | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<Record<string, string | number>>(emptyForm)
  const [probingId, setProbingId] = useState<number | null>(null)
  const filterFields = useMemo(() => ["连接编号", "复检结论"], [])

  const loadRules = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/rules`)
      if (response.ok) setRules(await response.json())
    } catch {
      // 规则拉取失败不阻断列表，表单仍可用占位提示。
    }
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('数据源连接列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据源列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void loadRules()
  }, [loadRules])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runProbe(row: Row) {
    setErrorMessage('')
    setNotice('')
    setProbingId(Number(row.id))
    try {
      const response = await request(`${ENDPOINT}/${row.id}/probe`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        // 复检未通过属于正常业务结果（HTTP 200 + ok=false），用提示色展示而非报错弹窗。
        setNotice(payload.message ?? '复检未通过')
      } else {
        setNotice(payload.message ?? '复检通过')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连通性复检失败')
    } finally {
      setProbingId(null)
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? '动作未生效')
      } else {
        setNotice(payload.message)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据源操作失败')
    }
  }

  async function submitForm(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        setErrorMessage(payload.message ?? '数据源连接保存失败')
        return
      }
      setNotice(payload.message)
      setShowForm(false)
      setForm(emptyForm)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据源连接保存失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '数据源总数', value: total },
    { label: '复检通过', value: rows.filter((r) => r['复检结论'] === '通过').length },
    { label: '复检未通过', value: rows.filter((r) => r['复检结论'] === '未通过').length },
  ]), [rows, total])

  return (
    <section className="page" data-module="datasource">
      <header className="page-head">
        <div>
          <h2>分析数据源管理</h2>
          <p className="page-desc">
            维护连接配置与连通性复检；复检未通过的数据源不能被分析数据集引用，复检结果同步到全部依赖数据集。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowForm((v) => !v)}>
            {showForm ? '收起登记表单' : '登记数据源连接'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {rules && (
        <div className="rule-panel">
          <strong>连通规则（不满足规则的连接不允许保存）</strong>
          <ul className="rule-list">
            <li>数据库类型标准：{rules.supported_db_types.join('、')}</li>
            <li>探测超时阈值：{rules.probe_timeout.min}~{rules.probe_timeout.max} 秒（默认 {rules.probe_timeout.default} 秒）</li>
            <li>连接池可用范围：{rules.connection_pool.min}~{rules.connection_pool.max}（默认 {rules.connection_pool.default}）</li>
            <li>连接地址：{rules.address_format}</li>
            {rules.save_conditions.map((cond) => <li key={cond}>{cond}</li>)}
          </ul>
        </div>
      )}

      {showForm && (
        <form className="editor-card" onSubmit={submitForm}>
          <h3>登记数据源连接</h3>
          <div className="form-grid">
            <label><span>连接编号 *</span><input value={String(form['连接编号'])} onChange={(e) => setForm((p) => ({ ...p, 连接编号: e.target.value }))} placeholder="如 DBC-0010" /></label>
            <label><span>连接名称 *</span><input value={String(form['连接名称'])} onChange={(e) => setForm((p) => ({ ...p, 连接名称: e.target.value }))} /></label>
            <label>
              <span>数据库类型 *</span>
              <select value={String(form['数据库类型'])} onChange={(e) => setForm((p) => ({ ...p, 数据库类型: e.target.value }))}>
                {(rules?.supported_db_types ?? ['MySQL']).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label><span>连接地址 *</span><input value={String(form['连接地址'])} onChange={(e) => setForm((p) => ({ ...p, 连接地址: e.target.value }))} placeholder="主机:端口，如 10.0.0.8:3306" /></label>
            <label><span>认证凭据 *</span><input value={String(form['认证凭据'])} onChange={(e) => setForm((p) => ({ ...p, 认证凭据: e.target.value }))} placeholder="用户名:密码" /></label>
            <label><span>默认库名 *</span><input value={String(form['默认库名'])} onChange={(e) => setForm((p) => ({ ...p, 默认库名: e.target.value }))} placeholder="如 analytics_dw" /></label>
            <label>
              <span>连接池大小 *</span>
              <input type="number" value={Number(form['连接池大小'])} min={rules?.connection_pool.min} max={rules?.connection_pool.max}
                onChange={(e) => setForm((p) => ({ ...p, 连接池大小: Number(e.target.value) }))} />
            </label>
            <label>
              <span>探测超时(秒) *</span>
              <input type="number" value={Number(form['探测超时'])} min={rules?.probe_timeout.min} max={rules?.probe_timeout.max}
                onChange={(e) => setForm((p) => ({ ...p, 探测超时: Number(e.target.value) }))} />
            </label>
            <label>
              <span>探活模式</span>
              <select value={String(form['探活模式'])} onChange={(e) => setForm((p) => ({ ...p, 探活模式: e.target.value }))}>
                {(rules?.probe_modes ?? ['模拟', '真实']).map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
          </div>
          <div className="form-actions">
            <button className="btn primary" type="submit">保存连接</button>
            <button className="btn ghost" type="button" onClick={() => setShowForm(false)}>取消</button>
          </div>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            {field === '复检结论' ? (
              <select value={filters[field] ?? ''} onChange={(e) => setFilters((prev) => ({ ...prev, [field]: e.target.value }))}>
                <option value="">全部</option>
                <option value="通过">通过</option>
                <option value="未通过">未通过</option>
                <option value="未复检">未复检</option>
              </select>
            ) : (
              <input value={filters[field] ?? ''} placeholder={`按${field}检索`}
                onChange={(e) => setFilters((prev) => ({ ...prev, [field]: e.target.value }))} />
            )}
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['复检结论'] === '未通过' ? 'row-bad' : ''}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '复检结论' ? (
                    <span className={conclusionClass(row[column] as string)}>{row[column] ?? '—'}</span>
                  ) : (
                    (row[column] ?? '—')
                  )}
                </td>
              ))}
              <td className="row-actions">
                <button className="link" type="button" disabled={probingId === Number(row.id)}
                  onClick={() => void runProbe(row)}>
                  {probingId === Number(row.id) ? '复检中…' : '连通性复检'}
                </button>
                <button className="link" type="button" onClick={() => void runAction('启用配置', row)}>启用配置</button>
                <button className="link" type="button" onClick={() => void runAction('停用配置', row)}>停用配置</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={columns.length + 1}>暂无数据源连接，可先登记并复检</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据源记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
