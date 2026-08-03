/* DataTable - the one table implementation.
 *
 * Sticky headers, search, sort, filters, pagination and expandable rows, built
 * once so no page has to re-implement any of them and no two tables behave
 * differently. Every table in the dashboard is an instance of this.
 *
 * The important property is that state survives a re-render: these pages poll,
 * and a naive table would throw away the user's search, sort and page position
 * every few seconds. `setRows` replaces the data and keeps everything else - if
 * the current page no longer exists it clamps, rather than dumping you back at
 * page one.
 *
 *   new DataTable('#el', {
 *     columns: [{ key, label, num, sortable, sticky, render(row), sortValue(row) }],
 *     searchKeys: ['name', 'server'],
 *     filters: [{ key, label, options: [{ value, label }], test(row, value) }],
 *     chips:   [{ key, label, test(row) }],
 *     expand:  (row) => html,
 *     pageSize: 25,
 *   })
 */

class DataTable {
  constructor(target, options = {}) {
    this.el = typeof target === 'string' ? document.querySelector(target) : target;
    this.columns = options.columns || [];
    this.rows = options.rows || [];

    this.searchKeys = options.searchKeys || [];
    this.searchPlaceholder = options.searchPlaceholder || 'Search…';
    this.searchable = options.searchable !== false && this.searchKeys.length > 0;
    this.filters = options.filters || [];
    this.chips = options.chips || [];
    this.expand = options.expand || null;
    this.onRowClick = options.onRowClick || null;
    this.rowClass = options.rowClass || null;

    this.title = options.title || '';
    this.hint = options.hint || '';
    this.toolbarExtra = options.toolbarExtra || '';
    this.exportHref = options.exportHref || '';
    this.legend = options.legend || '';
    this.emptyTitle = options.emptyTitle || 'Nothing to show';
    this.emptyDetail = options.emptyDetail || '';
    this.maxHeight = options.maxHeight || '';
    // Paging is on by default. Turning it off is for tables that are known
    // small (a handful of servers), never for ones that merely happen to be.
    this.paginate = options.paginate !== false;

    this.state = {
      query: '',
      sortKey: (options.sort && options.sort.key) || null,
      sortDir: (options.sort && options.sort.dir) || 'desc',
      page: 1,
      pageSize: options.pageSize || 25,
      filters: {},
      chips: new Set(options.activeChips || []),
      expanded: new Set(),
    };

    this.id = `dt-${Math.random().toString(36).slice(2, 9)}`;
    this.build();
  }

  /* --------------------------------------------------------- structure */

  build() {
    const hasToolbar = this.searchable || this.filters.length || this.chips.length
      || this.toolbarExtra || this.exportHref;

    this.el.innerHTML = `
      <div class="panel" id="${this.id}">
        ${this.title ? `<div class="panel-head">
          <h2>${esc(this.title)}</h2>
          ${this.hint ? `<span class="hint">${this.hint}</span>` : ''}
        </div>` : ''}
        ${hasToolbar ? `<div class="toolbar">
          ${this.searchable ? `<input type="search" data-role="search"
             placeholder="${esc(this.searchPlaceholder)}" autocomplete="off"
             aria-label="${esc(this.searchPlaceholder)}">` : ''}
          ${this.filters.map((f) => `<label class="field">
            <select data-role="filter" data-key="${esc(f.key)}" aria-label="${esc(f.label)}">
              <option value="">${esc(f.label)}: all</option>
              ${f.options.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}
            </select></label>`).join('')}
          ${this.chips.map((c) => `<button class="pill" data-role="chip" data-key="${esc(c.key)}"
             aria-pressed="false">${esc(c.label)} <span class="n" data-count="${esc(c.key)}"></span></button>`).join('')}
          ${this.toolbarExtra}
          <span class="spacer"></span>
          <span class="result-count" data-role="count" aria-live="polite"></span>
          ${this.exportHref ? `<a class="btn small" href="${esc(this.exportHref)}"
             download title="Download this table as CSV">Download CSV</a>` : ''}
        </div>` : ''}
        <div class="table-scroll"${this.maxHeight ? ` style="--table-max-h:${this.maxHeight}"` : ''}>
          <table>
            <thead><tr data-role="head"></tr></thead>
            <tbody data-role="body"></tbody>
          </table>
        </div>
        ${this.paginate ? '<div class="pager" data-role="pager" hidden></div>' : ''}
        ${this.legend}
      </div>`;

    this.$search = this.el.querySelector('[data-role="search"]');
    this.$head = this.el.querySelector('[data-role="head"]');
    this.$body = this.el.querySelector('[data-role="body"]');
    this.$count = this.el.querySelector('[data-role="count"]');
    this.$pager = this.el.querySelector('[data-role="pager"]');

    this.wire();
    this.renderHead();
    this.render();
  }

  wire() {
    if (this.$search) {
      // Debounced: re-sorting and re-rendering on every keystroke of a 112-row
      // table is fine, but the same code runs over comparison rows in the tens
      // of thousands.
      let timer;
      this.$search.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          this.state.query = this.$search.value.trim().toLowerCase();
          this.state.page = 1;
          this.render();
        }, 140);
      });
    }

    this.el.querySelectorAll('[data-role="filter"]').forEach((select) => {
      select.addEventListener('change', () => {
        this.state.filters[select.dataset.key] = select.value;
        this.state.page = 1;
        this.render();
      });
    });

    this.el.querySelectorAll('[data-role="chip"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.key;
        if (this.state.chips.has(key)) this.state.chips.delete(key);
        else this.state.chips.add(key);
        this.state.page = 1;
        this.render();
      });
    });

    // Delegated, because rows are replaced on every render.
    this.$body.addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-key]');
      if (!tr) return;
      // A link or button inside a row does its own thing.
      if (e.target.closest('a, button')) return;
      const row = this.visibleRows.find((r) => String(this.keyOf(r)) === tr.dataset.key);
      if (!row) return;

      if (this.expand) {
        const key = tr.dataset.key;
        if (this.state.expanded.has(key)) this.state.expanded.delete(key);
        else this.state.expanded.add(key);
        this.render();
      } else if (this.onRowClick) {
        this.onRowClick(row);
      }
    });

    this.$body.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const tr = e.target.closest('tr[data-key]');
      if (!tr) return;
      e.preventDefault();
      tr.click();
    });
  }

  renderHead() {
    this.$head.innerHTML = this.columns.map((col) => {
      const classes = [
        col.num ? 'num' : '',
        col.sortable !== false ? 'sortable' : '',
        col.sticky ? 'sticky-col' : '',
        this.state.sortKey === (col.sortKey || col.key) ? 'sorted' : '',
      ].filter(Boolean).join(' ');
      const arrow = col.sortable !== false ? this.arrowFor(col) : '';
      return `<th class="${classes}" data-key="${esc(col.sortKey || col.key)}"
        ${col.title ? `title="${esc(col.title)}"` : ''}
        ${col.sortable !== false ? 'role="button" tabindex="0" aria-sort="' + this.ariaSort(col) + '"' : ''}
        >${col.label}${arrow}</th>`;
    }).join('');

    this.$head.querySelectorAll('th.sortable').forEach((th) => {
      const activate = () => {
        const key = th.dataset.key;
        if (this.state.sortKey === key) {
          this.state.sortDir = this.state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          this.state.sortKey = key;
          const col = this.columns.find((c) => (c.sortKey || c.key) === key);
          this.state.sortDir = (col && col.sortDefault) || 'desc';
        }
        this.renderHead();
        this.render();
      };
      th.addEventListener('click', activate);
      th.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
      });
    });
  }

  arrowFor(col) {
    const key = col.sortKey || col.key;
    if (this.state.sortKey !== key) return ' <span class="arrow">↕</span>';
    return ` <span class="arrow">${this.state.sortDir === 'asc' ? '↑' : '↓'}</span>`;
  }

  ariaSort(col) {
    const key = col.sortKey || col.key;
    if (this.state.sortKey !== key) return 'none';
    return this.state.sortDir === 'asc' ? 'ascending' : 'descending';
  }

  /* ------------------------------------------------------------ data */

  keyOf(row) {
    return row.id ?? row.name ?? row.url ?? row.server_id ?? JSON.stringify(row).slice(0, 60);
  }

  setRows(rows) {
    this.rows = rows || [];
    // Clamp rather than reset: losing your place on every poll is worse than
    // landing on the last page when rows disappear.
    const maxPage = Math.max(1, Math.ceil(this.filtered().length / this.state.pageSize));
    if (this.state.page > maxPage) this.state.page = maxPage;
    this.render();
  }

  filtered() {
    let rows = this.rows;

    if (this.state.query) {
      const q = this.state.query;
      rows = rows.filter((row) => this.searchKeys.some((key) => {
        const value = typeof key === 'function' ? key(row) : row[key];
        return value !== null && value !== undefined
          && String(value).toLowerCase().includes(q);
      }));
    }

    for (const filter of this.filters) {
      const value = this.state.filters[filter.key];
      if (!value) continue;
      rows = filter.test
        ? rows.filter((row) => filter.test(row, value))
        : rows.filter((row) => String(row[filter.key]) === value);
    }

    // Chips are OR within the set: ticking "Failed" and "Warning" shows both,
    // which is what a set of severity toggles is expected to do.
    if (this.state.chips.size) {
      const active = this.chips.filter((c) => this.state.chips.has(c.key));
      rows = rows.filter((row) => active.some((c) => c.test(row)));
    }

    if (this.state.sortKey) {
      const col = this.columns.find((c) => (c.sortKey || c.key) === this.state.sortKey);
      const sign = this.state.sortDir === 'asc' ? 1 : -1;
      const valueOf = (row) => (col && col.sortValue ? col.sortValue(row) : row[this.state.sortKey]);
      rows = rows.slice().sort((a, b) => {
        const x = valueOf(a);
        const y = valueOf(b);
        // Nulls sort to the bottom whichever direction we are going - a blank
        // is never "the smallest value", it is an absence.
        if (x === null || x === undefined || x === '') return 1;
        if (y === null || y === undefined || y === '') return -1;
        if (typeof x === 'string' && typeof y === 'string') return sign * x.localeCompare(y);
        return sign * (x - y);
      });
    }
    return rows;
  }

  /* ---------------------------------------------------------- render */

  render() {
    const rows = this.filtered();
    const total = rows.length;

    const pageSize = this.paginate ? this.state.pageSize : total;
    const pages = Math.max(1, Math.ceil(total / (pageSize || 1)));
    if (this.state.page > pages) this.state.page = pages;
    const start = (this.state.page - 1) * pageSize;
    const visible = this.paginate ? rows.slice(start, start + pageSize) : rows;
    this.visibleRows = visible;

    if (this.$count) {
      this.$count.textContent = total === this.rows.length
        ? `${fmtNum(total)} row${total === 1 ? '' : 's'}`
        : `${fmtNum(total)} of ${fmtNum(this.rows.length)}`;
    }

    this.el.querySelectorAll('[data-role="chip"]').forEach((btn) => {
      const key = btn.dataset.key;
      const chip = this.chips.find((c) => c.key === key);
      const on = this.state.chips.has(key);
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', String(on));
      const count = btn.querySelector(`[data-count="${key}"]`);
      if (count && chip) count.textContent = this.rows.filter(chip.test).length;
    });

    if (!visible.length) {
      this.$body.innerHTML = `<tr><td colspan="${this.columns.length}">
        ${emptyState(this.rows.length ? 'Nothing matches those filters' : this.emptyTitle,
                     this.rows.length ? 'Try clearing the search or filters.' : this.emptyDetail)}
      </td></tr>`;
      if (this.$pager) this.$pager.hidden = true;
      return;
    }

    this.$body.innerHTML = visible.map((row) => {
      const key = String(this.keyOf(row));
      const open = this.state.expanded.has(key);
      const extra = this.rowClass ? this.rowClass(row) : '';
      const interactive = this.expand || this.onRowClick;

      const cells = this.columns.map((col, index) => {
        const classes = [col.num ? 'num' : '', col.sticky ? 'sticky-col' : '', col.className || ''].filter(Boolean).join(' ');
        const chevron = this.expand && index === 0
          ? `<span class="expander" aria-hidden="true">▸</span> ` : '';
        return `<td class="${classes}">${chevron}${col.render ? col.render(row) : esc(row[col.key] ?? '—')}</td>`;
      }).join('');

      const main = `<tr data-key="${esc(key)}"
        class="${extra} ${this.expand ? 'expandable' : ''} ${open ? 'open' : ''}"
        ${interactive ? 'tabindex="0"' : ''}
        ${this.expand ? `aria-expanded="${open}"` : ''}>${cells}</tr>`;

      if (!open || !this.expand) return main;
      return `${main}<tr class="detail-row"><td colspan="${this.columns.length}">${this.expand(row)}</td></tr>`;
    }).join('');

    this.renderPager(total, pages, start, visible.length);
  }

  renderPager(total, pages, start, shown) {
    if (!this.$pager) return;
    // One page of results needs no pager, but the page-size control is still
    // worth having once a table is big enough to have grown one.
    if (pages <= 1 && total <= 25) { this.$pager.hidden = true; return; }
    this.$pager.hidden = false;

    const window_ = [];
    const push = (n) => window_.push(n);
    const current = this.state.page;
    // First, last, and two either side of the current page - enough to navigate
    // 112 partners or 40,000 comparison rows with the same control.
    for (let n = 1; n <= pages; n += 1) {
      if (n === 1 || n === pages || Math.abs(n - current) <= 1) push(n);
      else if (window_[window_.length - 1] !== '…') push('…');
    }

    this.$pager.innerHTML = `
      <span class="pager-info">
        Showing ${fmtNum(start + 1)}–${fmtNum(start + shown)} of ${fmtNum(total)}
      </span>
      <div class="pager-controls">
        <select data-role="page-size" aria-label="Rows per page">
          ${[25, 50, 100, 250].map((n) =>
            `<option value="${n}"${n === this.state.pageSize ? ' selected' : ''}>${n} / page</option>`).join('')}
        </select>
        <button data-page="${current - 1}" ${current === 1 ? 'disabled' : ''} aria-label="Previous page">‹</button>
        ${window_.map((n) => (n === '…'
          ? '<span class="gap">…</span>'
          : `<button data-page="${n}" class="${n === current ? 'current' : ''}"
               ${n === current ? 'aria-current="page"' : ''}>${n}</button>`)).join('')}
        <button data-page="${current + 1}" ${current === pages ? 'disabled' : ''} aria-label="Next page">›</button>
      </div>`;

    this.$pager.querySelectorAll('button[data-page]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const target = Number(btn.dataset.page);
        if (target < 1 || target > pages) return;
        this.state.page = target;
        this.render();
        // Back to the top of the table, not the top of the document: the point
        // of paging is that the table stays where it is.
        this.el.querySelector('.table-scroll').scrollTop = 0;
      });
    });

    const size = this.$pager.querySelector('[data-role="page-size"]');
    if (size) {
      size.addEventListener('change', () => {
        this.state.pageSize = Number(size.value);
        this.state.page = 1;
        this.render();
      });
    }
  }
}

/* A table whose rows come from the server one page at a time.
 *
 * Used for comparison rows, where a partner can have 80,000 missing records:
 * sending them all so the browser can slice out 50 would be slow to fetch,
 * heavy to hold and pointless. Search and paging become query parameters; the
 * rendering is identical.
 */
class RemoteTable extends DataTable {
  constructor(target, options) {
    super(target, { ...options, paginate: true });
    this.fetchPage = options.fetchPage;
    this.serverTotal = 0;
    this.loading = false;
    this.load();
  }

  async load() {
    if (!this.fetchPage) return;
    this.loading = true;
    try {
      const { rows, total } = await this.fetchPage({
        offset: (this.state.page - 1) * this.state.pageSize,
        limit: this.state.pageSize,
        query: this.state.query,
      });
      this.rows = rows || [];
      this.serverTotal = total || 0;
      this.renderRemote();
    } catch (err) {
      this.$body.innerHTML = `<tr><td colspan="${this.columns.length}">
        ${emptyState('Could not load rows', err.message)}</td></tr>`;
    } finally {
      this.loading = false;
    }
  }

  /* The server has already filtered, sorted and paged, so the local pipeline is
   * bypassed entirely - running it again would filter a single page against a
   * query the whole set was already matched on. */
  renderRemote() {
    const total = this.serverTotal;
    const pages = Math.max(1, Math.ceil(total / this.state.pageSize));
    const start = (this.state.page - 1) * this.state.pageSize;
    this.visibleRows = this.rows;

    if (this.$count) this.$count.textContent = `${fmtNum(total)} row${total === 1 ? '' : 's'}`;

    if (!this.rows.length) {
      this.$body.innerHTML = `<tr><td colspan="${this.columns.length}">
        ${emptyState(this.state.query ? 'Nothing matches that search' : this.emptyTitle,
                     this.state.query ? '' : this.emptyDetail)}</td></tr>`;
      if (this.$pager) this.$pager.hidden = true;
      return;
    }

    this.$body.innerHTML = this.rows.map((row) => {
      const cells = this.columns.map((col) => {
        const classes = [col.num ? 'num' : '', col.sticky ? 'sticky-col' : '', col.className || ''].filter(Boolean).join(' ');
        return `<td class="${classes}">${col.render ? col.render(row) : esc(row[col.key] ?? '—')}</td>`;
      }).join('');
      return `<tr data-key="${esc(String(this.keyOf(row)))}">${cells}</tr>`;
    }).join('');

    this.renderPager(total, pages, start, this.rows.length);
  }

  // Every interaction becomes a fetch rather than a local re-filter.
  render() {
    if (this.fetchPage && this.serverTotal !== undefined && !this.loading && this.rows) {
      this.renderRemote();
      return;
    }
    super.render();
  }

  wire() {
    super.wire();
    if (this.$search) {
      let timer;
      this.$search.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          this.state.query = this.$search.value.trim();
          this.state.page = 1;
          this.load();
        }, 260);
      });
    }
  }

  renderPager(total, pages, start, shown) {
    super.renderPager(total, pages, start, shown);
    if (!this.$pager || this.$pager.hidden) return;
    // Re-point the pager at the server.
    this.$pager.querySelectorAll('button[data-page]').forEach((btn) => {
      const clone = btn.cloneNode(true);
      btn.replaceWith(clone);
      clone.addEventListener('click', () => {
        const target = Number(clone.dataset.page);
        if (target < 1 || target > pages) return;
        this.state.page = target;
        this.load();
      });
    });
    const size = this.$pager.querySelector('[data-role="page-size"]');
    if (size) {
      const clone = size.cloneNode(true);
      size.replaceWith(clone);
      clone.addEventListener('change', () => {
        this.state.pageSize = Number(clone.value);
        this.state.page = 1;
        this.load();
      });
    }
  }
}
