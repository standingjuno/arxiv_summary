const DATA_URL = './data/site-data.json';
const POLL_INTERVAL_MS = 2000;
const DEFAULT_TIMEZONE = 'Asia/Seoul';
const DEFAULT_CALENDAR_MONTHS = 12;

const runtimeConfig = window.ARXIV_SUMMARY_CONFIG || {};
let pollTimer = null;
let liveClearTimer = null;

const state = {
  data: null,
  category: 'all',
  selectedDate: null,
  selectedKeyword: null,
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

function todayDateString(data = state.data) {
  const timezone = data?.timezone || DEFAULT_TIMEZONE;
  try {
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat('en-US', {
        timeZone: timezone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }).formatToParts(new Date()).map((part) => [part.type, part.value]),
    );
    return `${parts.year}-${parts.month}-${parts.day}`;
  } catch (_error) {
    return toDateString(new Date());
  }
}

function monthStart(value) {
  return new Date(value.getFullYear(), value.getMonth(), 1);
}

function addMonths(value, delta) {
  return new Date(value.getFullYear(), value.getMonth() + delta, 1);
}

function compareMonth(left, right) {
  return (left.getFullYear() - right.getFullYear()) || (left.getMonth() - right.getMonth());
}

function calendarMonthCount(data = state.data) {
  const months = Number(data?.range?.months || DEFAULT_CALENDAR_MONTHS);
  return Number.isFinite(months) && months > 0 ? Math.floor(months) : DEFAULT_CALENDAR_MONTHS;
}

function calendarBounds(data = state.data) {
  const end = parseDate(todayDateString(data));
  const maxMonth = monthStart(end);
  const minMonth = addMonths(maxMonth, -(calendarMonthCount(data) - 1));
  return {
    start: toDateString(minMonth),
    end: toDateString(end),
    minMonth,
    maxMonth,
  };
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
  const withPapers = [...data.dates].reverse().find((day) => (
    day.has_papers && !day.is_weekend && dateIsInRange(day.date)
  ));
  if (withPapers) {
    return withPapers.date;
  }
  const weekday = [...data.dates].reverse().find((day) => !day.is_weekend && dateIsInRange(day.date));
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
  const bounds = calendarBounds();
  if (dateString < bounds.start) {
    return false;
  }
  if (dateString > bounds.end) {
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

function papersForSelectedDate() {
  const papers = state.data?.papers || [];
  if (!state.selectedDate) {
    return papers;
  }
  return papers.filter((paper) => paper.listing_date === state.selectedDate);
}

function paperMatchesCategory(paper, category) {
  if (category === 'all') {
    return true;
  }
  const categories = new Set([...(paper.matched_categories || []), ...(paper.arxiv_categories || [])]);
  return categories.has(category);
}

function countPapersForCategory(papers, category) {
  return papers.filter((paper) => paperMatchesCategory(paper, category)).length;
}

function keywordCountsForPapers(papers) {
  const counts = new Map();
  for (const paper of papers) {
    for (const keyword of paper.keywords || []) {
      counts.set(keyword, (counts.get(keyword) || 0) + 1);
    }
  }
  return counts;
}

function normalizeScopedFilters() {
  const scopedPapers = papersForSelectedDate();
  if (state.category !== 'all' && countPapersForCategory(scopedPapers, state.category) === 0) {
    state.category = 'all';
  }
  if (state.selectedKeyword) {
    const keywordCounts = keywordCountsForPapers(scopedPapers);
    if (!keywordCounts.has(state.selectedKeyword)) {
      state.selectedKeyword = null;
    }
  }
}

function renderCategoryNav() {
  const scopedPapers = papersForSelectedDate();
  const categories = state.data.categories || [];
  const buttons = [
    { category: 'all', label: 'All', count: scopedPapers.length },
    ...categories.map((item) => ({
      ...item,
      count: countPapersForCategory(scopedPapers, item.category),
    })),
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
  const bounds = calendarBounds();
  const todayString = bounds.end;

  els.calendarTitle.textContent = formatMonth(current);
  els.prevMonthButton.disabled = compareMonth(addMonths(first, -1), bounds.minMonth) < 0;
  els.nextMonthButton.disabled = compareMonth(addMonths(first, 1), bounds.maxMonth) > 0;
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
    const outOfRange = !dateIsInRange(dateString);
    const unavailable = info.is_weekend || outOfRange;

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
    button.title = info.is_weekend
      ? 'Weekend'
      : outOfRange && dateString > bounds.end
        ? 'Future date'
        : outOfRange
          ? 'Outside the calendar range'
          : `${dateString} · ${info.count} papers`;

    if (!unavailable) {
      button.addEventListener('click', () => selectDate(dateString));
    }

    if (info.count > 0) {
      const marker = document.createElement('span');
      marker.className = 'date-marker';
      marker.setAttribute('aria-hidden', 'true');
      button.append(marker);
    }

    els.calendarGrid.append(button);
  }
}

function renderKeywords() {
  const query = state.keywordQuery.trim().toLowerCase();
  const keywordCounts = keywordCountsForPapers(papersForSelectedDate());
  const keywords = [...keywordCounts.entries()]
    .map(([name, count]) => ({ name, count }))
    .filter((item) => item.name.toLowerCase().includes(query))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
    .slice(0, 80);

  els.keywordList.innerHTML = '';
  for (const keyword of keywords) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `keyword-chip${state.selectedKeyword === keyword.name ? ' active' : ''}`;
    button.textContent = keyword.count > 0 ? `${keyword.name} ${keyword.count}` : keyword.name;
    button.addEventListener('click', () => {
      state.selectedKeyword = state.selectedKeyword === keyword.name ? null : keyword.name;
      render();
    });
    els.keywordList.append(button);
  }
}

function paperMatches(paper) {
  if (state.selectedDate && paper.listing_date !== state.selectedDate) {
    return false;
  }

  if (!paperMatchesCategory(paper, state.category)) {
    return false;
  }

  if (state.selectedKeyword && !(paper.keywords || []).includes(state.selectedKeyword)) {
    return false;
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
  title.textContent = paper.title;
  article.append(title);

  if (paper.title_kor) {
    const koreanTitle = document.createElement('p');
    koreanTitle.className = 'translated-title';
    koreanTitle.textContent = paper.title_kor;
    article.append(koreanTitle);
  }

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
  const keywordText = state.selectedKeyword || '';

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
  const bounds = calendarBounds();
  els.metaText.textContent = `${state.data.papers.length} papers · ${bounds.start} to ${bounds.end} · ${generated}`;
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
  normalizeScopedFilters();
  renderMeta();
  renderCategoryNav();
  renderCalendar();
  renderKeywords();
  renderLiveStatus();
  renderPapers();
}

function moveMonth(delta) {
  const bounds = calendarBounds();
  const nextMonth = new Date(
    state.calendarMonth.getFullYear(),
    state.calendarMonth.getMonth() + delta,
    1,
  );
  if (compareMonth(nextMonth, bounds.minMonth) < 0) {
    state.calendarMonth = bounds.minMonth;
  } else if (compareMonth(nextMonth, bounds.maxMonth) > 0) {
    state.calendarMonth = bounds.maxMonth;
  } else {
    state.calendarMonth = nextMonth;
  }
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

  if (job.status === 'failed') {
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
    if (!['completed', 'failed'].includes(job.status) && job.job_id !== 'cached') {
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
  if (!dateIsInRange(dateString) || findDateInfo(dateString).is_weekend) {
    return;
  }
  state.selectedDate = dateString;
  normalizeScopedFilters();
  render();
  startOnDemand(dateString);
}

function bindEvents() {
  els.prevMonthButton.addEventListener('click', () => moveMonth(-1));
  els.nextMonthButton.addEventListener('click', () => moveMonth(1));
  els.allDatesButton.addEventListener('click', () => {
    state.selectedDate = null;
    normalizeScopedFilters();
    render();
  });
  els.clearKeywordsButton.addEventListener('click', () => {
    state.selectedKeyword = null;
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
    const calendarDate = state.selectedDate ? parseDate(state.selectedDate) : parseDate(calendarBounds().end);
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
    state.calendarMonth = calendarBounds().maxMonth;
    render();
    els.paperList.innerHTML = '<div class="empty-state">Data unavailable</div>';
    console.error(error);
  }
}

init();
