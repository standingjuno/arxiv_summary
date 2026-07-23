const DATA_URL = './data/site-data.json';
const POLL_INTERVAL_MS = 2000;

const runtimeConfig = window.ARXIV_SUMMARY_CONFIG || {};
let pollTimer = null;
let liveClearTimer = null;

const state = {
  data: null,
  category: 'all',
  selectedDate: null,
  selectedKeywords: new Set(),
  keywordQuery: '',
  calendarMonth: null,
  apiBaseUrl: normalizeApiBaseUrl(runtimeConfig.apiBaseUrl || ''),
  liveJob: {
    date: null,
    jobId: null,
    status: 'idle',
    progress: 0,
    message: '',
    error: null,
  },
};

const els = {
  metaText: document.querySelector('#metaText'),
  categoryNav: document.querySelector('#categoryNav'),
  allDatesButton: document.querySelector('#allDatesButton'),
  prevMonthButton: document.querySelector('#prevMonthButton'),
  nextMonthButton: document.querySelector('#nextMonthButton'),
  calendarTitle: document.querySelector('#calendarTitle'),
  calendarGrid: document.querySelector('#calendarGrid'),
  keywordSearchInput: document.querySelector('#keywordSearchInput'),
  clearKeywordsButton: document.querySelector('#clearKeywordsButton'),
  keywordList: document.querySelector('#keywordList'),
  activeFilterText: document.querySelector('#activeFilterText'),
  resultTitle: document.querySelector('#resultTitle'),
  resultCount: document.querySelector('#resultCount'),
  liveStatus: document.querySelector('#liveStatus'),
  liveStatusTitle: document.querySelector('#liveStatusTitle'),
  liveStatusMessage: document.querySelector('#liveStatusMessage'),
  liveProgress: document.querySelector('#liveProgress'),
  liveProgressBar: document.querySelector('#liveProgressBar'),
  paperList: document.querySelector('#paperList'),
};

const categoryLabels = {
  'cs.RO': 'Robotics',
  'cs.LG': 'Machine Learning',
  'cs.CV': 'Computer Vision',
};

function normalizeApiBaseUrl(value) {
  return String(value || '').trim().replace(/\/+$/, '');
}

function resolveApiBaseUrl(data) {
  return normalizeApiBaseUrl(runtimeConfig.apiBaseUrl || data?.api_base_url || state.apiBaseUrl || '');
}

function apiUrl(path) {
  if (!state.apiBaseUrl) {
    return null;
  }
  return `${state.apiBaseUrl}${path}`;
}

function parseDate(dateString) {
  const [year, month, day] = dateString.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function toDateString(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function formatMonth(value) {
  return value.toLocaleDateString('en-US', { year: 'numeric', month: 'long' });
}

function formatDate(value) {
  return parseDate(value).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    weekday: 'short',
  });
}

function latestWeekdayDate(data) {
  const withPapers = [...data.dates].reverse().find((day) => day.has_papers && !day.is_weekend);
  if (withPapers) {
    return withPapers.date;
  }
  const weekday = [...data.dates].reverse().find((day) => !day.is_weekend);
  return weekday ? weekday.date : null;
}

function categoryForNav(item) {
  return `${item.category} · ${item.label || categoryLabels[item.category] || item.category}`;
}

function findDateInfo(dateString) {
  const info = (state.data.dates || []).find((day) => day.date === dateString);
  if (info) {
    return info;
  }
  const value = parseDate(dateString);
  return {
    date: dateString,
    is_weekend: value.getDay() === 0 || value.getDay() === 6,
    count: 0,
    has_papers: false,
  };
}

function dateIsInRange(dateString) {
  const range = state.data.range || {};
  if (range.start && dateString < range.start) {
    return false;
  }
  if (range.end && dateString > range.end) {
    return false;
  }
  return true;
}

function hasPapersForDate(dateString) {
  return (state.data.papers || []).some((paper) => paper.listing_date === dateString);
}

function canRequestOnDemand(dateString) {
  if (!dateString || !state.apiBaseUrl || state.data.on_demand_enabled === false) {
    return false;
  }
  const info = findDateInfo(dateString);
  return !info.is_weekend && dateIsInRange(dateString) && !hasPapersForDate(dateString);
}

function renderCategoryNav() {
  const categories = state.data.categories || [];
  const buttons = [
    { category: 'all', label: 'All', count: state.data.papers.length },
    ...categories,
  ];

  els.categoryNav.innerHTML = '';
  for (const item of buttons) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `tab-button${state.category === item.category ? ' active' : ''}`;
    button.textContent = item.category === 'all' ? `All (${item.count})` : `${categoryForNav(item)} (${item.count})`;
    button.addEventListener('click', () => {
      state.category = item.category;
      render();
    });
    els.categoryNav.append(button);
  }
}

function renderCalendar() {
  const current = state.calendarMonth;
  const first = new Date(current.getFullYear(), current.getMonth(), 1);
  const last = new Date(current.getFullYear(), current.getMonth() + 1, 0);
  const startOffset = (first.getDay() + 6) % 7;
  const dateMap = new Map(state.data.dates.map((day) => [day.date, day]));
  const todayString = (state.data.range || {}).end || toDateString(new Date());

  els.calendarTitle.textContent = formatMonth(current);
  els.calendarGrid.innerHTML = '';

  for (let i = 0; i < startOffset; i += 1) {
    const empty = document.createElement('span');
    empty.className = 'date-cell empty';
    els.calendarGrid.append(empty);
  }

  for (let day = 1; day <= last.getDate(); day += 1) {
    const value = new Date(current.getFullYear(), current.getMonth(), day);
    const dateString = toDateString(value);
    const info = dateMap.get(dateString) || {
      date: dateString,
      is_weekend: value.getDay() === 0 || value.getDay() === 6,
      count: 0,
      has_papers: false,
    };
    const unavailable = info.is_weekend || !dateIsInRange(dateString);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = [
      'date-cell',
      info.is_weekend ? 'weekend' : '',
      unavailable && !info.is_weekend ? 'unavailable' : '',
      state.selectedDate === dateString ? 'selected' : '',
      todayString === dateString ? 'today' : '',
    ].filter(Boolean).join(' ');
    button.textContent = String(day);
    button.disabled = unavailable;
    button.title = info.is_weekend ? 'Weekend' : `${dateString} · ${info.count} papers`;

    if (!unavailable) {
      button.addEventListener('click', () => selectDate(dateString));
    }

    if (info.count > 0) {
      const count = document.createElement('span');
      count.className = 'date-count';
      count.textContent = String(info.count);
      button.append(count);
    }

    els.calendarGrid.append(button);
  }
}

function renderKeywords() {
  const query = state.keywordQuery.trim().toLowerCase();
  const keywords = (state.data.keywords || [])
    .filter((item) => item.name.toLowerCase().includes(query))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
    .slice(0, 80);

  els.keywordList.innerHTML = '';
  for (const keyword of keywords) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `keyword-chip${state.selectedKeywords.has(keyword.name) ? ' active' : ''}`;
    button.textContent = keyword.count > 0 ? `${keyword.name} ${keyword.count}` : keyword.name;
    button.addEventListener('click', () => {
      if (state.selectedKeywords.has(keyword.name)) {
        state.selectedKeywords.delete(keyword.name);
      } else {
        state.selectedKeywords.add(keyword.name);
      }
      render();
    });
    els.keywordList.append(button);
  }
}

function paperMatches(paper) {
  if (state.category !== 'all') {
    const categories = new Set([...(paper.matched_categories || []), ...(paper.arxiv_categories || [])]);
    if (!categories.has(state.category)) {
      return false;
    }
  }

  if (state.selectedDate && paper.listing_date !== state.selectedDate) {
    return false;
  }

  for (const keyword of state.selectedKeywords) {
    if (!(paper.keywords || []).includes(keyword)) {
      return false;
    }
  }

  return true;
}

function groupByDate(papers) {
  const groups = new Map();
  for (const paper of papers) {
    if (!groups.has(paper.listing_date)) {
      groups.set(paper.listing_date, []);
    }
    groups.get(paper.listing_date).push(paper);
  }
  return [...groups.entries()].sort((a, b) => b[0].localeCompare(a[0]));
}

function badge(text, className = '') {
  const span = document.createElement('span');
  span.className = `badge ${className}`.trim();
  span.textContent = text;
  return span;
}

function renderPaperCard(paper) {
  const article = document.createElement('article');
  article.className = 'paper-card';

  const meta = document.createElement('div');
  meta.className = 'paper-meta';
  for (const category of paper.matched_categories || []) {
    meta.append(badge(category, 'category'));
  }
  for (const field of paper.fields || []) {
    meta.append(badge(field.replace('_', ' ')));
  }
  article.append(meta);

  const title = document.createElement('h3');
  title.textContent = paper.title_kor || paper.title;
  article.append(title);

  const originalTitle = document.createElement('p');
  originalTitle.className = 'original-title';
  originalTitle.textContent = paper.title;
  article.append(originalTitle);

  const summary = document.createElement('p');
  summary.className = 'summary';
  summary.textContent = paper.summary;
  article.append(summary);

  const footer = document.createElement('div');
  footer.className = 'paper-footer';

  const keywordWrap = document.createElement('div');
  keywordWrap.className = 'paper-keywords';
  for (const keyword of paper.keywords || []) {
    keywordWrap.append(badge(keyword));
  }
  footer.append(keywordWrap);

  const link = document.createElement('a');
  link.className = 'paper-link';
  link.href = paper.link;
  link.target = '_blank';
  link.rel = 'noreferrer';
  link.textContent = 'arXiv';
  footer.append(link);

  article.append(footer);
  return article;
}

function renderPapers() {
  const papers = state.data.papers.filter(paperMatches);
  const selectedCategory = state.category === 'all' ? 'All' : state.category;
  const keywordText = [...state.selectedKeywords].join(', ');

  els.activeFilterText.textContent = [
    selectedCategory,
    state.selectedDate ? formatDate(state.selectedDate) : 'All dates',
    keywordText,
  ].filter(Boolean).join(' / ');
  els.resultTitle.textContent = state.selectedDate ? formatDate(state.selectedDate) : 'Papers';
  els.resultCount.textContent = `${papers.length} papers`;

  els.paperList.innerHTML = '';
  if (papers.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    const job = state.liveJob;
    const activeForDate = job.date === state.selectedDate && ['queued', 'running', 'pending'].includes(job.status);
    empty.textContent = activeForDate ? 'Loading papers' : 'No papers';
    els.paperList.append(empty);
    return;
  }

  for (const [date, datePapers] of groupByDate(papers)) {
    const group = document.createElement('section');
    group.className = 'date-group';
    const heading = document.createElement('h3');
    heading.className = 'date-heading';
    heading.textContent = `${formatDate(date)} · ${datePapers.length}`;
    group.append(heading);
    for (const paper of datePapers) {
      group.append(renderPaperCard(paper));
    }
    els.paperList.append(group);
  }
}

function renderMeta() {
  const generated = state.data.generated_at
    ? new Date(state.data.generated_at).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
    : 'not generated';
  const range = state.data.range || {};
  els.metaText.textContent = `${state.data.papers.length} papers · ${range.start || '-'} to ${range.end || '-'} · ${generated}`;
}

function renderLiveStatus() {
  const job = state.liveJob;
  const visible = job.status !== 'idle' && job.date === state.selectedDate;
  els.liveStatus.className = [
    'live-status',
    visible ? '' : 'hidden',
    job.status === 'failed' ? 'failed' : '',
    job.status === 'completed' ? 'completed' : '',
  ].filter(Boolean).join(' ');

  if (!visible) {
    return;
  }

  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  els.liveStatusTitle.textContent = job.status === 'completed'
    ? 'Ready'
    : job.status === 'failed'
      ? 'Failed'
      : `Loading ${job.date}`;
  els.liveStatusMessage.textContent = job.error || job.message || 'Working';
  els.liveProgress.textContent = `${progress}%`;
  els.liveProgressBar.style.width = `${progress}%`;
}

function render() {
  renderMeta();
  renderCategoryNav();
  renderCalendar();
  renderKeywords();
  renderLiveStatus();
  renderPapers();
}

function moveMonth(delta) {
  state.calendarMonth = new Date(
    state.calendarMonth.getFullYear(),
    state.calendarMonth.getMonth() + delta,
    1,
  );
  renderCalendar();
}

function setLiveJob(details) {
  state.liveJob = {
    ...state.liveJob,
    ...details,
  };
  renderLiveStatus();
  renderPapers();
}

function clearLiveJobSoon() {
  window.clearTimeout(liveClearTimer);
  liveClearTimer = window.setTimeout(() => {
    state.liveJob = {
      date: null,
      jobId: null,
      status: 'idle',
      progress: 0,
      message: '',
      error: null,
    };
    renderLiveStatus();
  }, 4500);
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    cache: 'no-store',
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
}

async function loadSiteData() {
  const configuredApi = normalizeApiBaseUrl(runtimeConfig.apiBaseUrl || state.apiBaseUrl || '');
  const urls = configuredApi ? [`${configuredApi}/api/data`, DATA_URL] : [DATA_URL];
  let lastError = null;

  for (const url of urls) {
    try {
      const data = await fetchJson(url);
      state.data = data;
      state.apiBaseUrl = resolveApiBaseUrl(data);
      return;
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error('Data unavailable');
}

async function refreshDataAfterJob() {
  await loadSiteData();
  render();
}

async function handleJobStatus(job) {
  setLiveJob({
    date: job.date,
    jobId: job.job_id,
    status: job.status,
    progress: job.progress,
    message: job.message,
    error: job.error,
  });

  if (job.status === 'completed') {
    stopPolling();
    await refreshDataAfterJob();
    setLiveJob({
      date: job.date,
      jobId: job.job_id,
      status: 'completed',
      progress: 100,
      message: job.paper_count === 0 ? 'No papers found' : 'Ready',
      error: null,
    });
    clearLiveJobSoon();
  }

  if (job.status === 'failed' || job.status === 'pending') {
    stopPolling();
  }
}

function pollJob(jobId) {
  stopPolling();
  pollTimer = window.setInterval(async () => {
    try {
      const url = apiUrl(`/api/jobs/${jobId}`);
      if (!url) {
        return;
      }
      await handleJobStatus(await fetchJson(url));
    } catch (error) {
      stopPolling();
      setLiveJob({
        status: 'failed',
        progress: 100,
        message: 'Failed',
        error: error.message,
      });
    }
  }, POLL_INTERVAL_MS);
}

async function startOnDemand(dateString) {
  if (!canRequestOnDemand(dateString)) {
    return;
  }
  if (state.liveJob.date === dateString && ['queued', 'running', 'pending'].includes(state.liveJob.status)) {
    return;
  }

  window.clearTimeout(liveClearTimer);
  setLiveJob({
    date: dateString,
    jobId: null,
    status: 'running',
    progress: 2,
    message: 'Starting',
    error: null,
  });

  try {
    const url = apiUrl(`/api/dates/${dateString}/run`);
    if (!url) {
      return;
    }
    const job = await fetchJson(url, { method: 'POST' });
    await handleJobStatus(job);
    if (!['completed', 'failed', 'pending'].includes(job.status) && job.job_id !== 'cached') {
      pollJob(job.job_id);
    }
  } catch (error) {
    stopPolling();
    setLiveJob({
      date: dateString,
      status: 'failed',
      progress: 100,
      message: 'Failed',
      error: error.message,
    });
  }
}

function selectDate(dateString) {
  state.selectedDate = dateString;
  render();
  startOnDemand(dateString);
}

function bindEvents() {
  els.prevMonthButton.addEventListener('click', () => moveMonth(-1));
  els.nextMonthButton.addEventListener('click', () => moveMonth(1));
  els.allDatesButton.addEventListener('click', () => {
    state.selectedDate = null;
    render();
  });
  els.clearKeywordsButton.addEventListener('click', () => {
    state.selectedKeywords.clear();
    state.keywordQuery = '';
    els.keywordSearchInput.value = '';
    render();
  });
  els.keywordSearchInput.addEventListener('input', (event) => {
    state.keywordQuery = event.target.value;
    renderKeywords();
  });
}

async function init() {
  bindEvents();
  try {
    await loadSiteData();
    state.selectedDate = latestWeekdayDate(state.data);
    const calendarDate = state.selectedDate ? parseDate(state.selectedDate) : new Date();
    state.calendarMonth = new Date(calendarDate.getFullYear(), calendarDate.getMonth(), 1);
    render();
  } catch (error) {
    state.data = {
      generated_at: null,
      api_base_url: state.apiBaseUrl,
      on_demand_enabled: false,
      range: {},
      categories: [
        { category: 'cs.RO', label: 'Robotics', count: 0 },
        { category: 'cs.LG', label: 'Machine Learning', count: 0 },
        { category: 'cs.CV', label: 'Computer Vision', count: 0 },
      ],
      dates: [],
      keywords: [],
      papers: [],
    };
    state.calendarMonth = new Date();
    render();
    els.paperList.innerHTML = '<div class="empty-state">Data unavailable</div>';
    console.error(error);
  }
}

init();
