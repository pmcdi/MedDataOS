// Evidence views: what a citation rests on, drawn from the server's record of
// the call (citation.table, citation.cells, citation.basis), never from the
// agent's prose. Loaded before app.js, which calls into MedEvidence.

const MedEvidence = (() => {
    const esc = (text) => {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    };

    const day = (text) => {
        const [y, m, d] = String(text || '').slice(0, 10).split('-').map(Number);
        return y && m && d ? Date.UTC(y, m - 1, d) : NaN;
    };

    const fmt = (value) => {
        if (value == null || value === '') return '';
        const number = Number(value);
        if (!Number.isFinite(number)) return String(value);
        return String(Math.round(number * 1000) / 1000);
    };

    // ── What a citation rests on ────────────────────────────────────────
    const BASIS = {
        quote:      { label: 'Quote',      title: 'A passage of the note' },
        cell:       { label: 'Cell',       title: 'Values read from table cells' },
        computed:   { label: 'Computed',   title: 'A value worked out by code' },
        model_read: { label: 'Model read', title: 'A model\'s description of an image' },
        metadata:   { label: 'Metadata',   title: 'Fields of the file header' },
        file:       { label: 'File',       title: 'The file as a whole' },
    };
    const CHECKED = {
        server:     'checked by the server against the file',
        specialist: 'checked by the specialist, not by the server',
        none:       'not checked against the file',
    };

    function basisInfo(citation) {
        const basis = citation && citation.basis;
        if (!basis || !BASIS[basis.type]) return null;
        const by = basis.by === 'router' ? 'the router' : `the ${basis.by} specialist`;
        return {
            type: basis.type,
            checked: basis.checked || 'none',
            by: basis.by || '',
            label: BASIS[basis.type].label,
            text: `${BASIS[basis.type].title}, ${CHECKED[basis.checked] || CHECKED.none}. Read by ${by}.`,
        };
    }

    // A chip for the hover card and the dialog.
    function basisChip(citation) {
        const info = basisInfo(citation);
        if (!info) return '';
        const mark = info.checked === 'server' ? '✓' : info.checked === 'specialist' ? '○' : '!';
        return `<span class="basis-chip basis-${info.type} basis-check-${info.checked}" title="${esc(info.text)}">`
            + `<span class="basis-mark">${mark}</span>${esc(info.label)}`
            + `<span class="basis-note">${esc(info.checked === 'server' ? 'verified' : info.checked === 'specialist' ? 'by specialist' : 'unverified')}</span></span>`;
    }

    // Classes for the inline badge, so an unverified read looks unlike a checked one.
    function basisClass(citation) {
        const info = basisInfo(citation);
        return info ? ` citation-basis-${info.type} citation-check-${info.checked}` : '';
    }

    // ── Trend chart ─────────────────────────────────────────────────────
    function seriesOf(citation) {
        const table = citation && citation.table;
        return table && table.view === 'series' && Array.isArray(table.points) && table.points.length
            ? table : null;
    }

    function citedRows(citation) {
        return new Set((citation.cells || []).map(cell => cell.row));
    }

    // points: [{date, value, flag, row, ref_low, ref_high}], markers: [{date, end, label}]
    function chart(table, cited, markers, opts = {}) {
        const points = table.points.filter(p => Number.isFinite(day(p.date)) && Number.isFinite(Number(p.value)));
        if (!points.length) return '';
        const compact = !!opts.compact;
        const W = compact ? 300 : 640, H = compact ? 84 : 250;
        const pad = compact ? { l: 6, r: 6, t: 8, b: 8 } : { l: 52, r: 18, t: 22, b: 34 };
        const xs = points.map(p => day(p.date));
        const last = points[points.length - 1];
        const lows = [last.ref_low].filter(v => v != null).map(Number);
        const highs = [last.ref_high].filter(v => v != null).map(Number);
        const values = points.map(p => Number(p.value));
        let y0 = Math.min(...values, ...lows), y1 = Math.max(...values, ...highs);
        if (y0 === y1) { y0 -= 1; y1 += 1; }
        const span = y1 - y0;
        y0 -= span * 0.08; y1 += span * 0.12;
        let x0 = Math.min(...xs), x1 = Math.max(...xs);
        if (x0 === x1) { x0 -= 15 * 864e5; x1 += 15 * 864e5; }
        const x = t => pad.l + (t - x0) / (x1 - x0) * (W - pad.l - pad.r);
        const y = v => H - pad.b - (v - y0) / (y1 - y0) * (H - pad.t - pad.b);
        const parts = [];

        // Reference range of the latest result, as a band.
        if (lows.length || highs.length) {
            const top = y(highs.length ? Math.min(highs[0], y1) : y1);
            const bottom = y(lows.length ? Math.max(lows[0], y0) : y0);
            parts.push(`<rect class="chart-ref" x="${pad.l}" y="${top}" width="${W - pad.l - pad.r}" height="${Math.max(0, bottom - top)}"></rect>`);
            if (!compact) {
                const label = [lows.length ? fmt(lows[0]) : '', highs.length ? fmt(highs[0]) : ''].filter(Boolean).join(' – ');
                parts.push(`<text class="chart-ref-label" x="${W - pad.r - 4}" y="${top + 11}" text-anchor="end">reference ${esc(label)}</text>`);
            }
        }

        if (!compact) {
            // Three value ticks and the first and last date.
            [y0 + span * 0.08, (y0 + y1) / 2, y1 - span * 0.12].forEach(v => {
                parts.push(`<line class="chart-grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"></line>`);
                parts.push(`<text class="chart-tick" x="${pad.l - 6}" y="${y(v) + 3}" text-anchor="end">${esc(fmt(Math.round(v * 100) / 100))}</text>`);
            });
            parts.push(`<text class="chart-tick" x="${pad.l}" y="${H - 8}">${esc(points[0].date)}</text>`);
            parts.push(`<text class="chart-tick" x="${W - pad.r}" y="${H - 8}" text-anchor="end">${esc(last.date)}</text>`);
            if (table.unit) parts.push(`<text class="chart-tick" x="${pad.l - 6}" y="${pad.t - 8}" text-anchor="end">${esc(table.unit)}</text>`);

            // Treatment and procedure dates from the tables, inside the chart's dates.
            (markers || []).forEach((marker, index) => {
                const t = day(marker.date);
                if (!(t >= x0 && t <= x1)) return;
                const end = day(marker.end);
                if (end > t) {
                    parts.push(`<rect class="chart-event-span" x="${x(t)}" y="${pad.t}" width="${Math.max(1, x(Math.min(end, x1)) - x(t))}" height="${H - pad.t - pad.b}"></rect>`);
                }
                parts.push(`<line class="chart-event" x1="${x(t)}" x2="${x(t)}" y1="${pad.t}" y2="${H - pad.b}"></line>`);
                const anchor = x(t) > W * 0.7 ? 'end' : 'start';
                parts.push(`<text class="chart-event-label" x="${x(t) + (anchor === 'end' ? -4 : 4)}" y="${pad.t + 10 + (index % 3) * 11}" text-anchor="${anchor}">${esc(String(marker.label).slice(0, 34))}</text>`);
            });
        }

        parts.push(`<polyline class="chart-line" points="${points.map(p => `${x(day(p.date))},${y(Number(p.value))}`).join(' ')}"></polyline>`);
        points.forEach(p => {
            const isCited = cited.has(p.row);
            const cls = `chart-point${p.flag ? ' chart-point-flag' : ''}${isCited ? ' chart-point-cited' : ''}`;
            const cx = x(day(p.date)), cy = y(Number(p.value));
            const tip = `${p.date}: ${fmt(p.value)}${table.unit ? ' ' + table.unit : ''}${p.flag ? ' (' + p.flag + ')' : ''}`;
            parts.push(`<circle class="${cls}" cx="${cx}" cy="${cy}" r="${isCited ? (compact ? 4 : 5.5) : (compact ? 2 : 3)}" data-row="${p.row}"><title>${esc(tip)}</title></circle>`);
            if (isCited && !compact) {
                const anchor = cx > W * 0.8 ? 'end' : 'start';
                parts.push(`<text class="chart-cited-label" x="${cx + (anchor === 'end' ? -8 : 8)}" y="${cy - 8}" text-anchor="${anchor}">${esc(fmt(p.value))}</text>`);
            }
        });

        const title = `${table.item || 'Series'}${table.unit ? ` (${table.unit})` : ''}`;
        return `<svg class="evidence-chart${compact ? ' evidence-chart-compact' : ''}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)} over time">${parts.join('')}</svg>`;
    }

    function statsLine(table) {
        const s = table.stats || {};
        if (!s.n) return '';
        const unit = table.unit ? ` ${table.unit}` : '';
        const item = (label, point) => point
            ? `<span class="stat"><span class="stat-label">${label}</span>${esc(fmt(point.value))}${esc(unit)}<span class="stat-date">${esc(point.date)}</span></span>` : '';
        const change = s.change != null
            ? `<span class="stat"><span class="stat-label">change</span>${s.change > 0 ? '+' : ''}${esc(fmt(s.change))}${esc(unit)}${s.pct_change != null ? `<span class="stat-date">${s.pct_change > 0 ? '+' : ''}${esc(fmt(s.pct_change))}%</span>` : ''}</span>` : '';
        return `<div class="evidence-stats">${item('first', s.first)}${item('last', s.last)}${item('lowest', s.min)}${item('highest', s.max)}${change}</div>`;
    }

    function seriesBlock(citation, markers, compact) {
        const table = seriesOf(citation);
        if (!table) return '';
        const cited = citedRows(citation);
        const head = compact ? '' : `<div class="evidence-title">${esc(table.item || '')}${table.unit ? ` <span class="evidence-unit">${esc(table.unit)}</span>` : ''}</div>`;
        const notes = [];
        if (!compact && Array.isArray(table.superseded) && table.superseded.length) {
            notes.push(`${table.superseded.length} superseded result${table.superseded.length > 1 ? 's' : ''} left out: `
                + table.superseded.map(p => `${fmt(p.value)} on ${p.date}`).join(', '));
        }
        if (!compact && Array.isArray(table.other_units) && table.other_units.length) {
            notes.push(`${table.other_units.length} result${table.other_units.length > 1 ? 's' : ''} in a unit that could not be converted, left out of the chart`);
        }
        const converted = table.points.filter(p => p.reported).length;
        if (!compact && converted) notes.push(`${converted} result${converted > 1 ? 's' : ''} converted to ${table.unit || 'the common unit'}`);
        return `<div class="evidence-series${compact ? ' evidence-series-compact' : ''}">${head}`
            + chart(table, cited, markers, { compact })
            + (compact ? '' : statsLine(table))
            + (notes.length ? `<div class="evidence-notes">${notes.map(esc).join(' · ')}</div>` : '')
            + `</div>`;
    }

    // ── Score: show the inputs ──────────────────────────────────────────
    function scoreOf(citation) {
        const table = citation && citation.table;
        return table && table.view === 'score' && Array.isArray(table.components) ? table : null;
    }

    function scoreBlock(citation, compact) {
        const score = scoreOf(citation);
        if (!score) return '';
        const hasPoints = score.components.some(c => c.points != null);
        const rows = score.components.map(c => {
            const source = c.source && typeof c.source === 'object'
                ? `row ${c.source.row + 1}${c.source.date ? ` · ${c.source.date}` : ''}`
                : 'given with the question';
            const given = !(c.source && typeof c.source === 'object');
            return `<tr class="${given ? 'score-given' : ''}">`
                + `<td>${esc(c.input)}</td>`
                + `<td class="num">${esc(fmt(c.value))}${c.unit ? ` <span class="evidence-unit">${esc(c.unit)}</span>` : ''}</td>`
                + (hasPoints ? `<td class="num">${c.points != null ? esc(c.points) : ''}</td>` : '')
                + `<td class="score-source">${esc(source)}</td></tr>`;
        }).join('');
        const result = score.complete
            ? `<div class="score-result">${esc(fmt(score.value))}${score.unit ? ` <span class="evidence-unit">${esc(score.unit)}</span>` : ''}${score.label ? ` <span class="score-label">${esc(score.label)}</span>` : ''}</div>`
            : `<div class="score-result score-incomplete">Not computed: ${esc((score.missing || []).map(m => m.input).join(', '))} missing</div>`;
        return `<div class="evidence-score${compact ? ' evidence-score-compact' : ''}">`
            + `<div class="evidence-title">${esc(score.title || score.name)}${score.at ? ` <span class="evidence-unit">at ${esc(score.at)}</span>` : ''}</div>`
            + result
            + `<table class="score-table"><thead><tr><th>Input</th><th class="num">Value</th>${hasPoints ? '<th class="num">Points</th>' : ''}<th>From</th></tr></thead><tbody>${rows}</tbody></table>`
            + (compact ? '' : `<div class="score-formula">${esc(score.formula || '')}</div>`)
            + `</div>`;
    }

    // ── Table with the cited cells marked ───────────────────────────────
    function isTable(citation) {
        return !!(citation && citation.table) || /\.(csv|tsv|jsonl|parquet)$/i.test(String(citation && citation.file || ''));
    }

    function cellsLine(citation) {
        const cells = citation.cells || [];
        if (!cells.length) return '';
        return `<div class="evidence-cells">${cells.map(cell =>
            `<span class="evidence-cell"><span class="evidence-cell-where">row ${cell.row + 1}${cell.column ? ` · ${esc(cell.column)}` : ''}${cell.date ? ` · ${esc(cell.date)}` : ''}</span>${esc(fmt(cell.value))}${cell.unit ? ` ${esc(cell.unit)}` : ''}</span>`
        ).join('')}</div>`;
    }

    function tableHTML(view, citation, maxRows) {
        const cells = citation.cells || [];
        const rows = new Set(cells.map(cell => cell.row));
        const marked = new Set(cells.filter(cell => cell.column).map(cell => `${cell.row}|${cell.column}`));
        const valueColumn = citation.table && citation.table.value_column;
        let shown = view.rows.map((row, index) => ({ row, number: view.row_numbers[index] }));
        let note = '';
        if (maxRows && shown.length > maxRows) {
            // A hover card shows the cited rows with one row of context each side.
            const keep = new Set();
            shown.forEach((item, index) => {
                if (rows.has(item.number)) [index - 1, index, index + 1].forEach(i => keep.add(i));
            });
            const picked = shown.filter((_, index) => keep.has(index));
            note = `${view.total} rows · showing ${picked.length ? 'the cited rows' : 'the first ' + maxRows}`;
            shown = picked.length ? picked.slice(0, maxRows) : shown.slice(0, maxRows);
        }
        const head = `<tr><th class="row-number">#</th>${view.columns.map(name =>
            `<th class="col-${esc(view.column_types[name] || 'text')}">${esc(name)}</th>`).join('')}</tr>`;
        const body = shown.map(({ row, number }) => {
            const cited = rows.has(number);
            return `<tr class="${cited ? 'row-cited' : ''}" data-row="${number}"><td class="row-number">${number + 1}</td>`
                + row.map((value, index) => {
                    const name = view.columns[index];
                    const hit = marked.has(`${number}|${name}`) || (cited && !marked.size && name === valueColumn)
                        || (cited && cells.some(c => c.row === number && !c.column) && name === valueColumn);
                    return `<td class="col-${esc(view.column_types[name] || 'text')}${hit ? ' cell-cited' : ''}">${esc(value)}</td>`;
                }).join('') + `</tr>`;
        }).join('');
        return `<div class="evidence-table-wrap"><table class="evidence-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`
            + (note ? `<div class="evidence-notes">${esc(note)}</div>` : '');
    }

    // A score names no cells. Its table inputs are the rows to mark.
    function withScoreRows(citation) {
        const score = scoreOf(citation);
        if (!score || (citation.cells || []).length) return citation;
        const cells = score.components
            .filter(c => c.source && typeof c.source === 'object' && c.source.row != null)
            .map(c => ({ row: c.source.row, column: c.source.column || 'value' }));
        return { ...citation, cells };
    }

    async function loadTable(citation, container, maxRows) {
        const rows = (citation.cells || []).map(cell => cell.row).join(',');
        const url = `/api/tables/view?web_path=${encodeURIComponent(citation.web_path)}&rows=${encodeURIComponent(rows)}`;
        try {
            const response = await fetch(url);
            const view = await response.json();
            if (!response.ok) throw new Error(view.error || response.statusText);
            container.innerHTML = tableHTML(view, citation, maxRows);
            // Bring the first cited cell into view inside the table only, so
            // the chart above it stays where it is.
            const wrap = container.querySelector('.evidence-table-wrap');
            const first = container.querySelector('.cell-cited') || container.querySelector('.row-cited');
            if (wrap && first && !maxRows) {
                const row = first.closest('tr');
                wrap.scrollTop = Math.max(0, row.offsetTop - wrap.clientHeight / 2);
                wrap.scrollLeft = Math.max(0, first.offsetLeft - wrap.clientWidth / 2);
            }
        } catch (err) {
            container.innerHTML = `<div class="card-text card-error">Could not load this table: ${esc(String(err.message || err))}</div>`;
        }
    }

    // ── Sources that disagree ───────────────────────────────────────────
    // badge(citation) is the page's own citation badge, so a value opens its source.
    function discrepanciesHTML(list, citations, badge) {
        if (!Array.isArray(list) || !list.length) return '';
        const byId = {};
        (citations || []).forEach(c => { byId[c.id] = c; });
        const items = list.map(item => {
            const sides = {};
            (item.values || []).forEach(value => {
                (sides[value.side || 0] = sides[value.side || 0] || []).push(value);
            });
            const sideHTML = Object.values(sides).map(values => `<div class="disc-side">${values.map(value => {
                const citation = byId[value.finding];
                const preferred = item.preferred === value.finding ? ' disc-preferred' : '';
                const source = [value.file, value.date].filter(Boolean).join(' · ');
                return `<span class="disc-value${preferred}"><span class="disc-text">${esc(value.value)}${value.unit ? ' ' + esc(value.unit) : ''}</span>`
                    + (citation && badge ? badge(citation, value.finding) : '')
                    + `<span class="disc-source">${esc(source)}</span>`
                    + (preferred ? '<span class="disc-pref-note">preferred</span>' : '')
                    + `</span>`;
            }).join('')}</div>`).join('<div class="disc-vs">versus</div>');
            const attribute = String(item.attribute || '').split('.').pop().replace(/_/g, ' ');
            return `<div class="disc disc-${esc(item.severity || 'medium')}">`
                + `<div class="disc-head">⚠ Sources disagree on ${esc(attribute)}</div>`
                + `<div class="disc-sides">${sideHTML}</div>`
                + (item.rationale ? `<div class="disc-rationale">${esc(item.rationale)}</div>` : '')
                + `</div>`;
        }).join('');
        return `<div class="discrepancies">${items}</div>`;
    }

    // ── What a specialist was asked and what came back ──────────────────
    function consultDetail(step) {
        if (step.kind !== 'consult' || !(step.question || step.reply)) return '';
        const row = (label, text, cls = '') => text
            ? `<div class="consult-row ${cls}"><span class="consult-label">${label}</span><span class="consult-text">${esc(text)}</span></div>` : '';
        const list = (label, items, cls) => Array.isArray(items) && items.length
            ? row(label, items.join('; '), cls) : '';
        return `<div class="consult-detail">`
            + row('Asked', step.question)
            + row('With', step.context, 'consult-context')
            + row(step.status === 'error' ? 'Failed' : 'Replied', String(step.reply || '').replace(/\[cite:[^\]]+\]/g, '').replace(/\s+([.,;])/g, '$1'))
            + list('Needs', step.needs, 'consult-needs')
            + list('Not documented', step.abstentions, 'consult-abstain')
            + `</div>`;
    }

    // ── Patient timeline ────────────────────────────────────────────────
    const LANE_CLASS = { note: 'lane-note', dicom: 'lane-dicom', structured: 'lane-table' };

    function timelineHTML(timeline, highlighted) {
        if (!timeline || !timeline.range || !Array.isArray(timeline.lanes) || !timeline.lanes.length) return '';
        const x0 = day(timeline.range[0]), x1 = day(timeline.range[1]);
        const span = Math.max(1, x1 - x0);
        const pct = t => (4 + (t - x0) / span * 92).toFixed(2);
        const marked = highlighted || new Set();
        const years = [];
        for (let y = new Date(x0).getUTCFullYear(); y <= new Date(x1).getUTCFullYear() + 1; y++) {
            const t = Date.UTC(y, 0, 1);
            if (t >= x0 && t <= x1) years.push({ y, at: pct(t) });
        }
        const step = Math.max(1, Math.ceil(years.length / 8));
        const axis = years.filter((_, index) => index % step === 0).map(year =>
            `<span class="tl-year" style="left:${year.at}%">${year.y}</span>`).join('');
        const lanes = timeline.lanes.map(lane => {
            const marks = lane.events.map(event => {
                const t = day(event.date);
                if (!Number.isFinite(t)) return '';
                const end = day(event.end);
                const on = marked.has(event.date) && (!marked.files || marked.files.has(event.web_path));
                const tip = `${event.date}${event.end ? ' to ' + event.end : ''} · ${event.label}${event.detail ? ' · ' + event.detail : ''}`;
                const cls = `tl-mark${on ? ' tl-mark-cited' : ''}${event.uncertain ? ' tl-mark-uncertain' : ''}`;
                const data = ` data-path="${esc(event.web_path || '')}" data-date="${esc(event.date)}"${event.row != null ? ` data-row="${event.row}"` : ''}`;
                if (end > t || event.open) {
                    const right = event.open ? 96 : Number(pct(end));
                    return `<span class="${cls} tl-span${event.open ? ' tl-span-open' : ''}" style="left:${pct(t)}%;width:${Math.max(0.6, right - Number(pct(t))).toFixed(2)}%" title="${esc(tip)}"${data}></span>`;
                }
                return `<span class="${cls}" style="left:${pct(t)}%" title="${esc(tip)}"${data}></span>`;
            }).join('');
            return `<div class="tl-lane ${LANE_CLASS[lane.modality] || ''}"><span class="tl-name">${esc(lane.name)}</span><span class="tl-track">${marks}</span></div>`;
        }).join('');
        return `<div class="tl-lanes">${lanes}</div><div class="tl-axis"><span class="tl-name"></span><span class="tl-track">${axis}</span></div>`;
    }

    // Dates and files an answer's citations rest on, to mark on the timeline.
    function citedDates(citations) {
        const dates = new Set();
        dates.files = new Set();
        (citations || []).forEach(citation => {
            const found = [];
            (citation.cells || []).forEach(cell => { if (cell.date) found.push(cell.date); });
            (citation.evidence || []).forEach(ev => { if (ev.note_date) found.push(ev.note_date); });
            const table = citation.table || {};
            if (table.view === 'score') {
                (table.components || []).forEach(c => { if (c.source && c.source.date) found.push(c.source.date); });
            }
            if (table.view === 'series' && !(citation.cells || []).length && table.stats) {
                ['first', 'last'].forEach(key => { if (table.stats[key]) found.push(table.stats[key].date); });
            }
            if (citation.dicom && citation.dicom.metadata && citation.dicom.metadata.StudyDate) {
                found.push(citation.dicom.metadata.StudyDate);
            }
            found.forEach(date => dates.add(String(date).slice(0, 10)));
            if (found.length && citation.web_path) dates.files.add(citation.web_path);
        });
        return dates;
    }

    // Procedure and treatment dates from the timeline, for the chart.
    function chartMarkers(timeline) {
        if (!timeline || !Array.isArray(timeline.lanes)) return [];
        const wanted = /^(procedure|surgery|radiotherapy|chemotherapy|treatment)$/i;
        const named = /\b(resection|hepatectomy|EBRT|radiotherapy|end of treatment)\b/i;
        const found = [];
        timeline.lanes.forEach(lane => {
            if (lane.modality !== 'structured') return;
            // A table that says what kind of event each row is decides by that.
            // Otherwise the row's name has to say it is a treatment.
            const typed = lane.events.some(event => event.detail);
            lane.events.forEach(event => {
                if (typed ? wanted.test(event.detail || '') : named.test(event.label || '')) {
                    found.push({ date: event.date, end: event.end, label: event.label });
                }
            });
        });
        // A delivery log has a row per fraction: mark its first and last day.
        timeline.lanes.forEach(lane => {
            if (lane.modality === 'structured' && /fraction/i.test(lane.name) && lane.events.length > 1) {
                found.push({
                    date: lane.events[0].date,
                    end: lane.events[lane.events.length - 1].date,
                    label: 'Radiotherapy',
                });
            }
        });
        return found.slice(0, 8);
    }

    return {
        basisInfo, basisChip, basisClass,
        seriesOf, seriesBlock, scoreOf, scoreBlock,
        isTable, cellsLine, loadTable, tableHTML, withScoreRows,
        discrepanciesHTML, consultDetail,
        timelineHTML, citedDates, chartMarkers,
    };
})();
