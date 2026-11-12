import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataset'
const DATASOURCE_ENDPOINT = '/api/datasource'
const columns = [
  "数据集编号",
  "数据集名称",
  "来源表名",
  "依赖数据源连接编号",
  "数据源复检结论",
  "数据源失败阶段",
  "数据源失败原因",
  "数据源最近成功探测时间",
  "可刷新",
  "刷新频率",
  "最近刷新",
  "是否启用",
]

type Row = Record<string, string | number | boolean | null>

const emptyForm: Record<string, string> = {
  数据集编号: '',
  数据集名称: '',
  来源表名: '',
  依赖数据源连接编号: '',
  刷新频率: '手动',
}

function conclusionClass(conclusion: string | null | undefined): string {
  if (conclusion === '通过') return 'tag tag-ok'
  if (conclusion === '未通过') return 'tag tag-bad'
  return 'tag tag-idle'
}

export default function DatasetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [datasources, setDatasources] = useState<Row[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState<Record<string, string>>(emptyForm)
  const filterFields = useMemo(() => ["数据集编号", "可刷新"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('数据集列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集列表读取失败')
    }
  }, [filters])

  const loadDatasources = useCallback(async () => {
    try {
      const response = await request(`${DATASOURCE_ENDPOINT}?size=200`)
      if (response.ok) {
        const payload = await response.json()
        setDatasources(payload.items ?? [])
      }
    } catch {
      // 数据源下拉失败不阻断数据集页面渲染。
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void loadDatasources()
  }, [loadDatasources])

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
      setErrorMessage(error instanceof Error ? error.message : '分析数据集操作失败')
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
        setErrorMessage(payload.message ?? '数据集保存失败')
        return
      }
      setNotice(payload.message)
      setShowForm(false)
      setForm(emptyForm)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据集保存失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '数据集总数', value: total },
    { label: '可刷新', value: rows.filter((r) => r['可刷新'] === true).length },
    { label: '不可刷新（数据源异常）', value: rows.filter((r) => r['可刷新'] === false).length },
  ]), [rows, total])

  // 只允许选择复检通过的数据源作为新建数据集的引用。
  const usableDatasources = useMemo(
    () => datasources.filter((d) => d['复检结论'] === '通过'),
    [datasources],
  )

  return (
    <section className="page" data-module="dataset">
      <header className="page-head">
        <div>
          <h2>分析数据集管理</h2>
          <p className="page-desc">
            数据集引用数据源的最新复检结论；复检未通过时不可新建、不可启用，历史已启用数据集保留查看但暂停刷新。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowForm((v) => !v)}>
            {showForm ? '收起登记表单' : '登记数据集'}
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

      {showForm && (
        <form className="editor-card" onSubmit={submitForm}>
          <h3>登记分析数据集（仅可绑定复检通过的数据源）</h3>
          <div className="form-grid">
            <label><span>数据集编号 *</span><input value={form['数据集编号']} onChange={(e) => setForm((p) => ({ ...p, 数据集编号: e.target.value }))} placeholder="如 DS-010" /></label>
            <label><span>数据集名称 *</span><input value={form['数据集名称']} onChange={(e) => setForm((p) => ({ ...p, 数据集名称: e.target.value }))} /></label>
            <label><span>来源表名 *</span><input value={form['来源表名']} onChange={(e) => setForm((p) => ({ ...p, 来源表名: e.target.value }))} /></label>
            <label>
              <span>依赖数据源 *</span>
              <select value={form['依赖数据源连接编号']} onChange={(e) => setForm((p) => ({ ...p, 依赖数据源连接编号: e.target.value }))}>
                <option value="">请选择复检通过的数据源</option>
                {usableDatasources.map((d) => (
                  <option key={String(d.id)} value={String(d['连接编号'])}>
                    {d['连接编号']}（{d['数据库类型']} / {d['默认库名']}）
                  </option>
                ))}
              </select>
            </label>
            <label><span>刷新频率</span><input value={form['刷新频率']} onChange={(e) => setForm((p) => ({ ...p, 刷新频率: e.target.value }))} /></label>
          </div>
          {usableDatasources.length === 0 && (
            <p className="error-text">当前没有复检通过的数据源，请先在「分析数据源」完成复检。</p>
          )}
          <div className="form-actions">
            <button className="btn primary" type="submit" disabled={usableDatasources.length === 0}>保存数据集</button>
            <button className="btn ghost" type="button" onClick={() => setShowForm(false)}>取消</button>
          </div>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            {field === '可刷新' ? (
              <select value={filters[field] ?? ''} onChange={(e) => setFilters((prev) => ({ ...prev, [field]: e.target.value }))}>
                <option value="">全部</option>
                <option value="true">可刷新</option>
                <option value="false">不可刷新</option>
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
          {rows.map((row) => {
            const refreshable = row['可刷新'] === true
            const unhealthy = row['数据源复检结论'] === '未通过'
            return (
              <tr key={String(row.id)} className={unhealthy ? 'row-bad' : ''}>
                {columns.map((column) => (
                  <td key={column}>
                    {column === '数据源复检结论' ? (
                      <span className={conclusionClass(row[column] as string)}>{row[column] ?? '—'}</span>
                    ) : column === '可刷新' ? (
                      <span className={refreshable ? 'tag tag-ok' : 'tag tag-bad'}>{refreshable ? '可刷新' : '不可刷新'}</span>
                    ) : (
                      (row[column] ?? '—')
                    )}
                  </td>
                ))}
                <td className="row-actions">
                  <button className="link" type="button" disabled={!refreshable}
                    title={refreshable ? '立即刷新数据集' : '数据源复检未通过，暂停刷新'}
                    onClick={() => void runAction('刷新数据集', row)}>
                    刷新数据集
                  </button>
                  <button className="link" type="button" disabled={unhealthy}
                    title={unhealthy ? '数据源复检未通过，不允许启用' : '启用数据集'}
                    onClick={() => void runAction('启用配置', row)}>
                    启用配置
                  </button>
                  <button className="link" type="button" onClick={() => void runAction('停用配置', row)}>停用配置</button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={columns.length + 1}>暂无分析数据集数据</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据集记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
