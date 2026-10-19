/** Charts are projections of published rows, not another source or answer generator. */
export type ResultChartKind = 'bar' | 'line' | 'scatter';
export type ChartableAnswer = {
  status: string; scope_stale?: boolean;
  sources?: { id: string; sha256?: string; parser_version?: string }[];
  result: { verification?: { status?: string }; columns?: unknown; rows?: unknown;
    column_units?: Record<string, string | null>; query?: { truncated?: boolean; [key: string]: unknown }; [key: string]: unknown } | null;
};
export type ChartOptions = { kind: ResultChartKind; xColumn: string; yColumns: string[]; maxPoints?: number; allowHistorical?: boolean };
export type ChartSeries = { column: string; unit: string | null };
export type ChartPoint = { rowIndex: number; x: string | number | null; values: (number | null)[];
  originalX: unknown; originalValues: unknown[] };
export type ResultChart = { status: 'ready'; kind: ResultChartKind; xColumn: string; xUnit: string | null;
  series: ChartSeries[]; points: ChartPoint[]; yDomain: [number, number]; xDomain?: [number, number];
  zeroBaseline: true; connectGaps: false; rowsTruncated: boolean; notes: string[];
  provenance: 'published_result_rows'; rowCount: number; historical: boolean };
export type ResultChartOutcome = ResultChart | { status: 'unavailable'; reason: string };
const fail = (reason: string): ResultChartOutcome => ({ status: 'unavailable', reason });
/** A chart is optional: text, dates used as values and a single row stay as a table. */
export function defaultChartOptions(answer:ChartableAnswer):ChartOptions|null{
 const columns=answer.result?.columns,rows=answer.result?.rows;
 if(!Array.isArray(columns)||!columns.every(c=>typeof c==='string')||!Array.isArray(rows)||rows.length<2)return null;
 const dimensions=/编号|序号|编码|代码|日期|年份|月份|(?:^|[_. ])id$|Id$|ID$|index|code|date|year|month/i;
 const measure=/金额|收入|销售|数量|工时|时长|合计|总计|均值|平均|比例|占比|增长|利润|费用|成本|hours|amount|total|count|sum|average|rate|price|revenue/i;
 const candidates=columns.filter(c=>!dimensions.test(c)).sort((a,b)=>Number(measure.test(b))-Number(measure.test(a)));
 for(const y of candidates){
  const yi=columns.indexOf(y);if(!rows.some(r=>Array.isArray(r)&&r[yi]!=null))continue;
  if(!rows.every(r=>Array.isArray(r)&&numberCell(r[yi]).valid))continue;
  for(const x of columns.filter(c=>c!==y)){
   const options:ChartOptions={kind:'bar',xColumn:x,yColumns:[y],allowHistorical:true};
   if(buildResultChart(answer,options).status==='ready')return options;
  }
 }
 return null;
}
const HEADER_UNIT = /[（(]\s*(%|％|‰|元|千元|万元|百万元|亿元|美元|万美元|人|万人|户|家|个|件|次|台|辆|吨|千克|公斤|克|千米|公里|米|平方米|万平方米|平方千米|亩|公顷|秒|分钟|小时|天|月|年|kWh|Wh|MWh|W|kW|MW|mg\/L|g\/L|L|m³)\s*[）)]\s*$/;
function headerUnit(column: string): string | null { return column.match(HEADER_UNIT)?.[1].replace('％', '%') ?? null; }
function unitFor(column: string, metadata: Record<string, string | null> | undefined): string | null {
  const value = metadata?.[column];
  return typeof value === 'string' && value.trim() ? value.trim().replace('％', '%') : headerUnit(column);
}
function numberCell(value: unknown): { value: number | null; suffix: string | null; valid: boolean } {
  if (value === null || value === undefined || typeof value === 'string' && /^(?:\s*|—+|--+|N\/A)$/i.test(value))
    return { value: null, suffix: null, valid: true };
  if (typeof value === 'number') return { value, suffix: null, valid: Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER };
  if (typeof value !== 'string') return { value: null, suffix: null, valid: false };
  // The entire actual cell must be numeric. Do not extract a number from a sentence.
  const match = value.trim().match(/^([+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+\-]?\d+)?)(%|％|‰)?$/);
  if (!match) return { value: null, suffix: null, valid: false };
  const parsed = Number(match[1].replaceAll(',', '').replace('−', '-'));
  const underflow = parsed === 0 && /[1-9]/.test(match[1].split(/[eE]/)[0]);
  return { value: parsed, suffix: match[2]?.replace('％', '%') ?? null,
    valid: Number.isFinite(parsed) && Math.abs(parsed) <= Number.MAX_SAFE_INTEGER && !underflow };
}
function domain(values: number[]): [number, number] {
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  return min === max ? [0, 1] : [min, max];
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
/** Limited is an execution/review scope, never upgraded to semantic or human approval. */
function publishedSQL(answer: ChartableAnswer, allowHistorical = false): boolean {
  const result = answer.result, query = result?.query;
  if (!(answer.scope_stale === false || allowHistorical && answer.scope_stale === true) || result?.verification?.status !== 'limited' || !query ||
      typeof query.sql !== 'string' || !query.sql.trim() ||
      !Array.isArray(result.columns) || !result.columns.length || !result.columns.every(c => typeof c === 'string') ||
      !Array.isArray(result.rows) || result.rows.some(row => !Array.isArray(row) || row.length !== (result.columns as unknown[]).length) ||
      !Number.isInteger(query.returned_rows) || query.returned_rows !== result.rows.length ||
      typeof query.elapsed_ms !== 'number' || !Number.isFinite(query.elapsed_ms) || query.elapsed_ms < 0 ||
      !(Array.isArray(query.parameters) || record(query.parameters)) ||
      !Array.isArray(query.used_tables) || !query.used_tables.length || !query.used_tables.every(t => typeof t === 'string' && t.length) ||
      !Array.isArray(result.claims) || !result.claims.length || !Array.isArray(result.evidence) ||
      !answer.sources?.length) return false;
  const sources = new Map(answer.sources.map(source => [source.id, source]));
  const evidence = new Map<string, Record<string, unknown>>();
  for (const item of result.evidence) {
    if (!record(item) || typeof item.id !== 'string' || item.kind !== 'table' || !(item.stale === false || allowHistorical && answer.scope_stale === true && item.stale === true) ||
        typeof item.source_id !== 'string' || typeof item.table_name !== 'string') continue;
    const source = sources.get(item.source_id);
    if (!source || !source.sha256 || !source.parser_version || source.sha256 !== item.sha256 ||
        source.parser_version !== item.parser_version || evidence.has(item.id)) return false;
    evidence.set(item.id, item);
  }
  const cited = new Set<string>();
  for (const claim of result.claims) {
    if (!record(claim) || typeof claim.text !== 'string' || !claim.text.trim() ||
        !Array.isArray(claim.evidence_ids) || !claim.evidence_ids.length ||
        claim.evidence_ids.some(id => typeof id !== 'string' || !evidence.has(id))) return false;
    claim.evidence_ids.forEach(id => cited.add(id));
  }
  return query.used_tables.every(table => [...cited].some(id => evidence.get(id)?.table_name === table));
}
export function buildResultChart(answer: ChartableAnswer, options: ChartOptions): ResultChartOutcome {
  // Explicitly opt in only for viewing the original, saved answer. No current
  // validation, export, or scenario contract is changed by historical viewing.
  const historical = options.allowHistorical === true && answer.scope_stale === true;
  const limitedSQL = publishedSQL(answer, historical);
  if (answer.status !== 'completed' || answer.scope_stale && !historical || !answer.result ||
      answer.result.verification?.status !== 'passed' && !limitedSQL)
    return fail('仅可绘制当前版本已完成的核验结果，或具备完整执行与来源记录的只读 SQL 结果。');
  const result = answer.result;
  // Never fall back to query.rows, display_rows, tool candidates, evidence rows or calculations.
  if (!Array.isArray(result.columns) || !result.columns.every(c => typeof c === 'string') || !Array.isArray(result.rows))
    return fail('没有已发布的结果表。');
  const columns = result.columns as string[], rows = result.rows;
  if (new Set(columns).size !== columns.length) return fail('结果列名重复，请先明确绘图字段。');
  if (!['bar', 'line', 'scatter'].includes(options.kind) || !columns.includes(options.xColumn) ||
      !options.yColumns.length || options.yColumns.length > 4 || new Set(options.yColumns).size !== options.yColumns.length ||
      options.yColumns.some(c => !columns.includes(c) || c === options.xColumn)) return fail('绘图字段无效或不唯一。');
  if (options.kind === 'scatter' && options.yColumns.length !== 1) return fail('散点图需要一对明确的数值字段。');
  const max = options.maxPoints ?? 500;
  if (!Number.isInteger(max) || max < 1 || max > 5000 || !rows.length || rows.length > max)
    return fail('结果为空或超过绘图行数上限，请先筛选结果；未擅自抽样。');
  const xi = columns.indexOf(options.xColumn), yi = options.yColumns.map(c => columns.indexOf(c));
  if (rows.some(row => !Array.isArray(row) || row.length !== columns.length)) return fail('结果行列不完整，不能可靠绘图。');
  const units = options.yColumns.map(column => unitFor(column, result.column_units));
  if ([options.xColumn, ...options.yColumns].some(column => headerUnit(column) && result.column_units?.[column] &&
      headerUnit(column) !== unitFor(column, result.column_units))) return fail('列名单位与已发布元数据单位冲突。');
  const declaredUnits = [...units], bareNumbers = units.map(() => false), suffixedNumbers = units.map(() => false);
  const points: ChartPoint[] = [];
  const labels = new Set<string>(); const seenY: number[] = []; const seenX: number[] = [];
  const xUnit = unitFor(options.xColumn, result.column_units);
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
    const row = rows[rowIndex] as unknown[];
    const originalX = row[xi]; let x: string | number | null;
    if (options.kind === 'scatter') {
      const cell = numberCell(originalX);
      if (!cell.valid || cell.suffix && cell.suffix !== xUnit) return fail('散点横轴含无效值或单位不一致。');
      x = cell.value; if (x !== null) seenX.push(x);
    } else {
      if (!['string', 'number'].includes(typeof originalX) || originalX === '' ||
          typeof originalX === 'number' && !Number.isFinite(originalX)) return fail('分类或时间字段存在缺失值。');
      x = String(originalX);
      if (labels.has(x)) return fail('横轴对象重复，请先明确分组；不会擅自合并求和。');
      labels.add(x);
    }
    const values: (number | null)[] = [];
    for (let i = 0; i < yi.length; i++) {
      const cell = numberCell(row[yi[i]]);
      if (!cell.valid) return fail('数值列包含非数值、无穷值或超出安全精度的值。');
      if (cell.suffix) {
        if (units[i] && units[i] !== cell.suffix) return fail('字段单位与数值单元格单位冲突。');
        units[i] = cell.suffix;
        suffixedNumbers[i] = true;
      }
      else if (cell.value !== null) bareNumbers[i] = true;
      values.push(cell.value); if (cell.value !== null) seenY.push(cell.value);
    }
    points.push({ rowIndex, x, values, originalX, originalValues: yi.map(i => row[i]) });
  }
  if (!seenY.length || options.kind === 'scatter' && !points.some(p => p.x !== null && p.values[0] !== null))
    return fail('所选字段没有可绘制的实际数值。');
  if (declaredUnits.some((unit, i) => unit === null && bareNumbers[i] && suffixedNumbers[i]))
    return fail('同一列混有带单位和无单位数值，不能推测其数量级。');
  if (units.length > 1 && (units.some(unit => unit === null) || new Set(units).size !== 1))
    return fail('多个系列的单位不明确或不同，请分别绘制。');
  const rowsTruncated = result.query?.truncated === true;
  const notes = [options.kind === 'line' ? '按已发布结果的行顺序绘制；未推测时间排序。' : '每个图形对应已发布结果的一行；未额外计算或聚合。'];
  if (historical) notes.push('历史图表：仅呈现回答时保存的结果，不代表当前来源或统计口径。');
  if (limitedSQL) notes.push('图表来自本次查询结果；统计口径仍需结合业务定义核对。');
  if (units.some(unit => unit === null)) notes.push('来源未标注单位；不猜测单位。');
  if (points.some(p => p.values.some(v => v === null) || p.x === null)) notes.push('缺失值保留为空，不补零、不插值。');
  if (rowsTruncated) notes.push(`查询结果已截断，仅绘制已返回的 ${rows.length} 行。`);
  return { status: 'ready', kind: options.kind, xColumn: options.xColumn, xUnit,
    series: options.yColumns.map((column, i) => ({ column, unit: units[i] })), points,
    yDomain: domain(seenY), ...(options.kind === 'scatter' ? { xDomain: domain(seenX) } : {}),
    zeroBaseline: true, connectGaps: false, rowsTruncated, notes, provenance: 'published_result_rows', rowCount: rows.length, historical };
}
